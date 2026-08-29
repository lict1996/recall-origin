"""Stable command-line adapter for the embedded memory engine.

The public command name is ``recallctl``.  During the package-name transition
the distribution may still expose another console-script alias, but help text,
configuration paths, and the machine-readable command catalogue deliberately
use the public name.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, overload
from uuid import uuid4

import typer
from platformdirs import user_data_path
from pydantic import BaseModel, ValidationError

from recall_origin.application.engine import MemoryEngine
from recall_origin.contracts.errors import (
    FEATURE_NOT_ENABLED,
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    PURGE_REGISTRY_REQUIRED,
    REVISION_CONFLICT,
    SCOPE_DENIED,
    TEMPORARY_FAILURE,
    VALIDATION_ERROR,
    ErrorSpec,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    CaptureRequest,
    ContextQuery,
    FeedbackRequest,
    ForgetRequest,
    ForgetTarget,
    GovernRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
    SubjectRef,
)
from recall_origin.domain.enums import (
    FeedbackType,
    GovernAction,
    MemoryKind,
    MemorySubtype,
)
from recall_origin.providers import StructuredEventProvider
from recall_origin.storage.sqlite.errors import SQLiteFeatureError

CONTRACT_VERSION = "1"
DATA_SCHEMA_VERSION = 1
PUBLIC_COMMAND = "recallctl"
DATABASE_ENV = "RECALLCTL_DB"
PURGE_KEY_ENV = "RECALL_ORIGIN_PURGE_KEY"
DEFAULT_DATA_DIR = user_data_path(PUBLIC_COMMAND, appauthor=False)
DEFAULT_DB_PATH = (DEFAULT_DATA_DIR / "memory.sqlite3").resolve()
_GLOBAL_DATABASE_PATH: Path | None = None

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit exactly one stable JSON envelope on stdout.",
    ),
]
DbOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        envvar=DATABASE_ENV,
        help=(
            "Absolute SQLite path. Defaults to the platform user-data directory; "
            f"{DATABASE_ENV} is also supported."
        ),
        show_default=False,
    ),
]


def _json_intent(arguments: Sequence[str]) -> bool:
    return "--json" in arguments


def _parser_command(arguments: Sequence[str]) -> str:
    """Return a schema-safe best-effort command name for parser failures."""

    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--db":
            skip_next = True
            continue
        if argument.startswith("-"):
            continue
        candidate = argument.lower().replace("_", "-")
        candidate = "".join(
            character if character.isalnum() or character in ".-" else "-"
            for character in candidate
        ).strip("-.")
        if candidate and candidate[0].isalpha():
            return candidate[:128]
    return "unknown"


def _is_json_envelope(value: str) -> bool:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(decoded, dict)
        and decoded.get("contract_version") == CONTRACT_VERSION
        and isinstance(decoded.get("ok"), bool)
    )


class ProtocolTyper(typer.Typer):
    """Typer application with an outer machine-protocol error boundary."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        arguments = tuple(sys.argv[1:])
        if not _json_intent(arguments):
            return super().__call__(*args, **kwargs)

        stdout = StringIO()
        stderr = StringIO()
        original_argv = sys.argv
        # A missing option value can consume the sole ``--json`` token. A
        # trailing duplicate keeps the caller's machine-mode intent visible.
        sys.argv = [*sys.argv, "--json"]
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = super().__call__(*args, **kwargs)
        except SystemExit as exc:
            rendered = stdout.getvalue()
            if _is_json_envelope(rendered):
                sys.stdout.write(rendered)
                sys.stdout.flush()
                raise
            request_id = _request_id()
            usage_error = "\n".join(
                item for item in (rendered.strip(), stderr.getvalue().strip()) if item
            )
            _write_json(
                _error_envelope(
                    _parser_command(arguments),
                    VALIDATION_ERROR,
                    "Invalid command syntax.",
                    {"usage_error": usage_error[:4096]},
                    request_id=request_id,
                    duration_ms=0,
                    fallback_scope=None,
                )
            )
            raise SystemExit(
                exc.code
                if isinstance(exc.code, int) and exc.code != 0
                else VALIDATION_ERROR.exit_code
            ) from None
        else:
            sys.stdout.write(stdout.getvalue())
            sys.stdout.flush()
            return result
        finally:
            sys.argv = original_argv


app = ProtocolTyper(
    name=PUBLIC_COMMAND,
    help="Local-first, source-traceable memory for agents.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
config_app = typer.Typer(
    name="config",
    help="Inspect the effective local configuration.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")


@app.callback()
def _root_options(
    context: typer.Context,
    db: DbOption = None,
) -> None:
    """Apply options shared by every command.

    Command-local ``--db`` remains accepted for shell ergonomics, while this
    callback supports the protocol form ``recallctl --db PATH command`` used by
    MCP hosts and automation.
    """

    del context
    global _GLOBAL_DATABASE_PATH
    _GLOBAL_DATABASE_PATH = db


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Effective CLI configuration after defaults and environment are applied."""

    database_path: Path
    database_source: str
    purge_registry_path: Path
    purge_key_path: Path
    purge_key_source: str
    durable: bool = True


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """One successful command result before envelope rendering."""

    data: Mapping[str, Any]
    scope: str | None = None
    partition_ids: tuple[str, ...] = ()
    degraded: bool = False
    warnings: tuple[str, ...] = ()


T = TypeVar("T")


def _request_id() -> str:
    return f"req_{uuid4().hex}"


def _external_event_id(request_id: str) -> str:
    # UUID-backed request IDs provide 122 random bits and are safe to use as a
    # producer-local idempotency namespace when callers do not supply one.
    return f"cli:{request_id}"


def _resolve_database_path(db: Path | None) -> RuntimeConfig:
    if db is None:
        db = _GLOBAL_DATABASE_PATH
    if db is None:
        database_path = DEFAULT_DB_PATH
        source = "default"
    else:
        expanded = db.expanduser()
        if not expanded.is_absolute():
            raise ValueError("--db must be an absolute path (a leading '~' is allowed)")
        database_path = expanded.resolve()
        source = "environment" if os.environ.get(DATABASE_ENV) == str(db) else "option"

    registry_path = Path(f"{database_path}.purge.sqlite3")
    key_path = Path(f"{registry_path}.key")
    if os.environ.get(PURGE_KEY_ENV):
        purge_key_source = "environment"
    elif key_path.is_file():
        purge_key_source = "file"
    else:
        purge_key_source = "generated_on_init"
    return RuntimeConfig(
        database_path=database_path,
        database_source=source,
        purge_registry_path=registry_path,
        purge_key_path=key_path,
        purge_key_source=purge_key_source,
    )


def _require(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _parse_scope(value: str | None, *, required: bool = True) -> PartitionRef | None:
    if value is None:
        if required:
            raise ValueError("--scope is required")
        return None
    return PartitionRef.parse(value)


def _parse_subject(value: str | None) -> SubjectRef | None:
    if value is None:
        return None
    subject_type, separator, subject_id = value.partition(":")
    if not separator or not subject_type or not subject_id:
        raise ValueError("--subject must use '<type>:<id>'")
    return SubjectRef(subject_type=subject_type, subject_id=subject_id)


def _parse_subjects(values: Sequence[str] | None) -> tuple[SubjectRef, ...]:
    return tuple(
        subject for value in values or () if (subject := _parse_subject(value)) is not None
    )


def _parse_payload(value: str | None, *, parse_json: bool) -> str | dict[str, Any] | list[Any]:
    payload = _require(value, "payload")
    if not parse_json:
        return payload
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("--payload-json requires valid JSON") from exc
    if not isinstance(parsed, str | dict | list):
        raise ValueError("--payload-json must decode to a string, object, or array")
    return parsed


def _parse_datetime(value: str | None, option_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{option_name} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{option_name} must include a timezone offset")
    return parsed


@overload
def _parse_int(value: str, option_name: str) -> int: ...


@overload
def _parse_int(value: None, option_name: str) -> None: ...


def _parse_int(value: str | None, option_name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{option_name} must be an integer") from exc


def _parse_cascade_policy(value: str) -> Literal["safe", "purge"]:
    if value == "safe":
        return "safe"
    if value == "purge":
        return "purge"
    raise ValueError("--cascade-policy must be 'safe' or 'purge'")


def _parse_context_mode(value: str) -> Literal["fast", "evidence"]:
    if value == "fast":
        return "fast"
    if value == "evidence":
        return "evidence"
    raise ValueError("--mode must be 'fast' or 'evidence'")


def _model_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _model_data(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _model_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set | frozenset):
        return [_model_data(item) for item in value]
    return value


def _bounded_message(message: str) -> str:
    compact = message.strip() or "The command failed."
    return compact[:4096]


def _validation_details(error: ValidationError) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    for issue in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "command"
        issues.append(
            {
                "field": location,
                "message": str(issue.get("msg", "Invalid value."))[:1024],
                "type": str(issue.get("type", "value_error"))[:128],
            }
        )
    return {"errors": issues}


def _classify_error(error: Exception) -> tuple[ErrorSpec, str, dict[str, Any]]:
    if isinstance(error, RecallOriginError):
        return error.spec, _bounded_message(error.message), _model_data(error.details)
    if isinstance(error, ValidationError):
        return VALIDATION_ERROR, "Invalid command input.", _validation_details(error)
    if isinstance(error, (ValueError, TypeError)):
        return VALIDATION_ERROR, _bounded_message(str(error)), {}
    if isinstance(error, SQLiteFeatureError):
        return FEATURE_NOT_ENABLED, _bounded_message(str(error)), {}
    if isinstance(error, (OSError, sqlite3.Error)):
        return (
            TEMPORARY_FAILURE,
            "The local memory store is temporarily unavailable.",
            {"exception_type": type(error).__name__},
        )
    return (
        TEMPORARY_FAILURE,
        "The command failed because of an unexpected internal error.",
        {"exception_type": type(error).__name__},
    )


def _meta(
    *,
    request_id: str,
    duration_ms: int,
    outcome: CommandOutcome | None = None,
    fallback_scope: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "request_id": request_id,
        "data_schema_version": DATA_SCHEMA_VERSION,
        "duration_ms": max(0, duration_ms),
    }
    scope = outcome.scope if outcome is not None else fallback_scope
    if scope and len(scope) <= 512:
        result["scope"] = scope
    if outcome is not None:
        if outcome.partition_ids:
            partition_ids = [
                partition_id
                for partition_id in dict.fromkeys(outcome.partition_ids)
                if 0 < len(partition_id) <= 128
            ]
            if partition_ids:
                result["partition_ids"] = partition_ids
        if outcome.degraded:
            result["degraded"] = True
        if outcome.warnings:
            result["warnings"] = list(outcome.warnings)
    return result


def _write_json(value: Mapping[str, Any]) -> None:
    # Keep this as the only JSON-mode stdout write in the adapter.
    sys.stdout.write(
        json.dumps(
            _model_data(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    sys.stdout.flush()


def _success_envelope(
    command: str,
    outcome: CommandOutcome,
    *,
    request_id: str,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "command": command,
        "data": dict(outcome.data),
        "meta": _meta(
            request_id=request_id,
            duration_ms=duration_ms,
            outcome=outcome,
        ),
    }


def _error_envelope(
    command: str,
    spec: ErrorSpec,
    message: str,
    details: Mapping[str, Any],
    *,
    request_id: str,
    duration_ms: int,
    fallback_scope: str | None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": spec.code,
        "message": message,
        "retryable": spec.retryable,
    }
    if details:
        error["details"] = dict(details)
    return {
        "contract_version": CONTRACT_VERSION,
        "ok": False,
        "command": command,
        "error": error,
        "meta": _meta(
            request_id=request_id,
            duration_ms=duration_ms,
            fallback_scope=fallback_scope,
        ),
    }


def _dispatch(
    command: str,
    *,
    json_output: bool,
    fallback_scope: str | None,
    action: Callable[[str], CommandOutcome],
    human: Callable[[CommandOutcome], str],
) -> None:
    started = time.monotonic_ns()
    request_id = _request_id()
    try:
        outcome = action(request_id)
    except Exception as error:
        spec, message, details = _classify_error(error)
        duration_ms = (time.monotonic_ns() - started) // 1_000_000
        if json_output:
            _write_json(
                _error_envelope(
                    command,
                    spec,
                    message,
                    details,
                    request_id=request_id,
                    duration_ms=duration_ms,
                    fallback_scope=fallback_scope,
                )
            )
        else:
            typer.echo(f"Error [{spec.code}]: {message}", err=True)
        raise typer.Exit(code=spec.exit_code) from None

    duration_ms = (time.monotonic_ns() - started) // 1_000_000
    if json_output:
        _write_json(
            _success_envelope(
                command,
                outcome,
                request_id=request_id,
                duration_ms=duration_ms,
            )
        )
    else:
        typer.echo(human(outcome))


def _with_engine(
    db: Path | None,
    operation: Callable[[MemoryEngine, RuntimeConfig], T],
) -> T:
    config = _resolve_database_path(db)
    with MemoryEngine.local(config.database_path, durable=config.durable) as engine:
        return operation(engine, config)


def _model_outcome(
    value: BaseModel,
    *,
    scope: PartitionRef | None = None,
    degraded: bool = False,
    warnings: Sequence[str] = (),
) -> CommandOutcome:
    scope_text = scope.serialize() if scope is not None else None
    return CommandOutcome(
        data=value.model_dump(mode="json"),
        scope=scope_text,
        partition_ids=(scope_text,) if scope_text else (),
        degraded=degraded,
        warnings=tuple(warnings),
    )


@app.command("init")
def initialize(
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Initialize or verify the database and signed purge sidecar."""

    def action(_: str) -> CommandOutcome:
        config = _resolve_database_path(db)
        already_present = config.database_path.is_file()
        with MemoryEngine.local(config.database_path, durable=config.durable) as engine:
            data = {
                "initialized": True,
                "created": not already_present,
                "database_path": str(config.database_path),
                "database_id": engine.store.database_id,
                "schema_version": engine.store.schema_version,
                "purge_registry_path": str(config.purge_registry_path),
            }
        return CommandOutcome(data=data)

    _dispatch(
        "init",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: (
            f"Initialized recallctl at {outcome.data['database_path']} "
            f"(schema {outcome.data['schema_version']})."
        ),
    )


@app.command("doctor")
def doctor(
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Check SQLite features, durability, integrity, index drift, and purge state."""

    def action(_: str) -> CommandOutcome:
        result = _with_engine(db, lambda engine, __: engine.doctor())
        degraded = not bool(result.get("ok", False)) or bool(result.get("index_drift", 0))
        warnings = ("One or more health checks require attention.",) if degraded else ()
        return CommandOutcome(data=result, degraded=degraded, warnings=warnings)

    def human(outcome: CommandOutcome) -> str:
        status = "DEGRADED" if outcome.degraded else "OK"
        return (
            f"{status}: {outcome.data.get('db_path', outcome.data.get('database', {}))} "
            f"(ledger={outcome.data.get('ledger_head', 0)}, "
            f"index_drift={outcome.data.get('index_drift', 0)})."
        )

    _dispatch(
        "doctor",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=human,
    )


@app.command("remember")
def remember(
    content: Annotated[
        str | None,
        typer.Argument(help="Memory content to record."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Authorization scope as '<kind>:<id>'."),
    ] = None,
    kind: Annotated[
        str,
        typer.Option("--kind", help="semantic, episodic, or procedural."),
    ] = MemoryKind.SEMANTIC.value,
    subtype: Annotated[
        str,
        typer.Option(
            "--subtype",
            help="fact, preference, outcome, decision, workflow, or gotcha.",
        ),
    ] = MemorySubtype.FACT.value,
    memory_key: Annotated[
        str | None,
        typer.Option("--memory-key", help="Stable semantic key for supersession."),
    ] = None,
    subject: Annotated[
        str | None,
        typer.Option("--subject", help="Optional subject as '<type>:<id>'."),
    ] = None,
    external_event_id: Annotated[
        str | None,
        typer.Option(
            "--external-event-id",
            help="Producer-local idempotency ID; safely generated when omitted.",
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Optional caller idempotency key."),
    ] = None,
    producer_id: Annotated[
        str,
        typer.Option("--producer-id", help="Producer identity for event namespacing."),
    ] = PUBLIC_COMMAND,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Origin session metadata."),
    ] = None,
    host_agent_id: Annotated[
        str | None,
        typer.Option("--host-agent-id", help="Origin agent metadata."),
    ] = None,
    valid_from: Annotated[
        str | None,
        typer.Option("--valid-from", help="ISO 8601 validity start with timezone."),
    ] = None,
    valid_to: Annotated[
        str | None,
        typer.Option("--valid-to", help="ISO 8601 validity end with timezone."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Record an explicit memory and return its evidence receipt."""

    def action(request_id: str) -> CommandOutcome:
        partition = _parse_scope(scope)
        assert partition is not None
        request = RememberRequest(
            content=_require(content, "content"),
            scope=partition,
            kind=MemoryKind(kind),
            subtype=MemorySubtype(subtype),
            memory_key=memory_key,
            subject=_parse_subject(subject),
            external_event_id=(
                _require(external_event_id, "--external-event-id")
                if external_event_id is not None
                else _external_event_id(request_id)
            ),
            idempotency_key=idempotency_key,
            origin=OriginContext(
                session_id=session_id,
                host_agent_id=host_agent_id,
                producer_id=producer_id,
                request_id=request_id,
            ),
            valid_from=_parse_datetime(valid_from, "--valid-from"),
            valid_to=_parse_datetime(valid_to, "--valid-to"),
        )
        receipt = _with_engine(db, lambda engine, __: engine.remember(request))
        return _model_outcome(receipt, scope=partition)

    _dispatch(
        "remember",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=lambda outcome: (
            f"Remembered {outcome.data['claim_id']} "
            f"(revision {outcome.data['revision_id']}, "
            f"{outcome.data['source_count']} source(s))."
        ),
    )


@app.command("capture")
def capture(
    payload: Annotated[
        str | None,
        typer.Argument(help="Raw event payload, or JSON when --payload-json is set."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Authorization scope as '<kind>:<id>'."),
    ] = None,
    external_event_id: Annotated[
        str | None,
        typer.Option("--external-event-id", help="Required producer-local event identity."),
    ] = None,
    event_type: Annotated[
        str,
        typer.Option("--event-type", help="Stable host event type."),
    ] = "host_observation",
    payload_json: Annotated[
        bool,
        typer.Option("--payload-json", help="Parse the positional payload as strict JSON."),
    ] = False,
    subject: Annotated[
        list[str] | None,
        typer.Option("--subject", help="Repeatable subject as '<type>:<id>'."),
    ] = None,
    persist: Annotated[
        bool,
        typer.Option(
            "--persist/--no-persist",
            help="Persist the event and enqueue formation (default: persist).",
        ),
    ] = True,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Optional caller idempotency key."),
    ] = None,
    producer_id: Annotated[
        str,
        typer.Option("--producer-id", help="Producer identity for event namespacing."),
    ] = PUBLIC_COMMAND,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Origin session metadata."),
    ] = None,
    host_agent_id: Annotated[
        str | None,
        typer.Option("--host-agent-id", help="Origin agent metadata."),
    ] = None,
    event_time: Annotated[
        str | None,
        typer.Option("--event-time", help="ISO 8601 event time with timezone."),
    ] = None,
    formation_version: Annotated[
        str,
        typer.Option("--formation-version", help="Versioned formation policy/input contract."),
    ] = "formation:v1",
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Durably capture an event for asynchronous memory formation."""

    def action(request_id: str) -> CommandOutcome:
        partition = _parse_scope(scope)
        assert partition is not None
        request = CaptureRequest(
            scope=partition,
            external_event_id=_require(external_event_id, "--external-event-id"),
            event_type=event_type,
            payload=_parse_payload(payload, parse_json=payload_json),
            origin=OriginContext(
                session_id=session_id,
                host_agent_id=host_agent_id,
                producer_id=producer_id,
                request_id=request_id,
            ),
            subjects=_parse_subjects(subject),
            persist=persist,
            event_time=_parse_datetime(event_time, "--event-time"),
            idempotency_key=idempotency_key,
        )
        receipt = _with_engine(
            db,
            lambda engine, __: engine.capture(
                request,
                formation_version=formation_version,
            ),
        )
        return _model_outcome(receipt, scope=partition)

    _dispatch(
        "capture",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=lambda outcome: (
            "Event was not stored by policy."
            if outcome.data["status"] == "no_store"
            else (
                f"Captured {outcome.data['event_id']} for formation job {outcome.data['job_id']}."
            )
        ),
    )


@app.command("formation-process")
def formation_process(
    job_id: Annotated[
        str | None,
        typer.Option("--job-id", help="Process one job; omit to claim the next ready job."),
    ] = None,
    worker_id: Annotated[
        str,
        typer.Option("--worker-id", help="Opaque worker identity used for the lease."),
    ] = "recallctl",
    lease_seconds: Annotated[
        str,
        typer.Option("--lease-seconds", help="Lease duration in seconds (>=1)."),
    ] = "60",
    max_attempts: Annotated[
        str,
        typer.Option("--max-attempts", help="Attempts before dead-lettering (>=1)."),
    ] = "3",
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Process one queued structured-event formation job."""

    def action(_: str) -> CommandOutcome:
        parsed_lease_seconds = _parse_int(lease_seconds, "--lease-seconds")
        parsed_max_attempts = _parse_int(max_attempts, "--max-attempts")
        if parsed_lease_seconds < 1:
            raise ValueError("--lease-seconds must be at least 1")
        if parsed_max_attempts < 1:
            raise ValueError("--max-attempts must be at least 1")
        receipt = _with_engine(
            db,
            lambda engine, __: engine.process_formation_job(
                StructuredEventProvider(),
                job_id=job_id,
                worker_id=worker_id,
                lease_seconds=parsed_lease_seconds,
                max_attempts=parsed_max_attempts,
            ),
        )
        return CommandOutcome(
            data={
                "processed": receipt is not None,
                "job": None if receipt is None else receipt.model_dump(mode="json"),
            }
        )

    def human(outcome: CommandOutcome) -> str:
        receipt = outcome.data["job"]
        if receipt is None:
            return "No ready formation job."
        return (
            f"Formation job {receipt['job_id']}: {receipt['status']} "
            f"({len(receipt['committed_claim_ids'])} claim(s))."
        )

    _dispatch(
        "formation-process",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=human,
    )


@app.command("formation-status")
def formation_status(
    job_id: Annotated[
        str | None,
        typer.Argument(help="Opaque automatic-formation job ID."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect one durable automatic-formation job."""

    def action(_: str) -> CommandOutcome:
        status = _with_engine(
            db,
            lambda engine, __: engine.formation_job_status(_require(job_id, "job_id")),
        )
        return _model_outcome(status, scope=status.partition)

    _dispatch(
        "formation-status",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: (
            f"Formation job {outcome.data['job_id']}: {outcome.data['status']} "
            f"(attempt {outcome.data['attempt']})."
        ),
    )


@app.command("feedback")
def feedback(
    claim_id: Annotated[
        str | None,
        typer.Argument(help="Opaque memory claim ID."),
    ] = None,
    revision_id: Annotated[
        str | None,
        typer.Option("--revision-id", help="Revision the feedback applies to."),
    ] = None,
    feedback_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="helpful, not_helpful, incorrect, stale, or proposal.",
        ),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional bounded feedback reason."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Append feedback without mutating memory governance."""

    def action(_: str) -> CommandOutcome:
        request = FeedbackRequest(
            claim_id=_require(claim_id, "claim_id"),
            revision_id=_require(revision_id, "--revision-id"),
            feedback_type=FeedbackType(_require(feedback_type, "--type")),
            reason=reason,
        )
        receipt = _with_engine(db, lambda engine, __: engine.feedback(request))
        return _model_outcome(receipt)

    _dispatch(
        "feedback",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: (
            f"Appended feedback {outcome.data['feedback_id']} for {outcome.data['claim_id']}."
        ),
    )


@app.command("search")
def search(
    query: Annotated[
        str | None,
        typer.Argument(help="Lexical search query."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Authorization scope as '<kind>:<id>'."),
    ] = None,
    limit: Annotated[
        str,
        typer.Option("--limit", help="Maximum results (1-100)."),
    ] = "8",
    valid_at: Annotated[
        str | None,
        typer.Option("--valid-at", help="ISO 8601 valid-time query with timezone."),
    ] = None,
    known_at_seq: Annotated[
        str | None,
        typer.Option("--known-at-seq", help="Historical ledger sequence (>=1)."),
    ] = None,
    include_candidates: Annotated[
        bool,
        typer.Option("--include-candidates", help="Include unconfirmed candidates."),
    ] = False,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Search canonical memory after authorization and deletion checks."""

    def action(_: str) -> CommandOutcome:
        partition = _parse_scope(scope)
        assert partition is not None
        request = SearchRequest(
            query=_require(query, "query"),
            scope=partition,
            limit=_parse_int(limit, "--limit"),
            valid_at=_parse_datetime(valid_at, "--valid-at"),
            known_at_seq=_parse_int(known_at_seq, "--known-at-seq"),
            include_candidates=include_candidates,
        )
        result = _with_engine(db, lambda engine, __: engine.search_result(request))
        scope_text = partition.serialize()
        return CommandOutcome(
            data={
                "hits": [_model_data(hit) for hit in result.items],
                "count": len(result.items),
                "candidate_count": result.candidate_count,
                "retrieval_id": result.retrieval_id,
                "ranking_policy_version": result.ranking_policy_version,
                "degradation_reasons": list(result.degradation_reasons),
            },
            scope=scope_text,
            partition_ids=(scope_text,),
            degraded=result.degraded,
        )

    def human(outcome: CommandOutcome) -> str:
        hits = outcome.data["hits"]
        if not hits:
            return "No memories found."
        return "\n".join(f"{hit['rank']}. {hit['content']} [{hit['claim_id']}]" for hit in hits)

    _dispatch(
        "search",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=human,
    )


@app.command("context")
def context(
    query: Annotated[
        str | None,
        typer.Argument(help="Task or question used to select context."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Authorization scope as '<kind>:<id>'."),
    ] = None,
    token_budget: Annotated[
        str,
        typer.Option("--token-budget", help="Approximate token budget (32-100000)."),
    ] = "800",
    limit: Annotated[
        str,
        typer.Option("--limit", help="Maximum candidates (1-100)."),
    ] = "8",
    valid_at: Annotated[
        str | None,
        typer.Option("--valid-at", help="ISO 8601 valid-time query with timezone."),
    ] = None,
    known_at_seq: Annotated[
        str | None,
        typer.Option("--known-at-seq", help="Historical ledger sequence (>=1)."),
    ] = None,
    mode: Annotated[
        str,
        typer.Option("--mode", help="Context form: 'fast' or 'evidence'."),
    ] = "fast",
    ttl_seconds: Annotated[
        str,
        typer.Option(
            "--ttl-seconds",
            help="Managed Evidence Pack lifetime (60-604800 seconds).",
        ),
    ] = "86400",
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help=(
                "Export an Evidence Pack to a new directory. The exported copy "
                "is outside managed purge guarantees."
            ),
        ),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Build a bounded Fast Context Pack or managed Evidence Pack."""

    def action(_: str) -> CommandOutcome:
        partition = _parse_scope(scope)
        assert partition is not None
        selected_mode = _parse_context_mode(mode)
        if selected_mode == "fast" and out is not None:
            raise ValueError("--out requires --mode evidence")
        request = ContextQuery(
            query=_require(query, "query"),
            scope=partition,
            token_budget=_parse_int(token_budget, "--token-budget"),
            limit=_parse_int(limit, "--limit"),
            valid_at=_parse_datetime(valid_at, "--valid-at"),
            known_at_seq=_parse_int(known_at_seq, "--known-at-seq"),
        )
        parsed_ttl = _parse_int(ttl_seconds, "--ttl-seconds")
        assert parsed_ttl is not None

        def build(engine: MemoryEngine, _: RuntimeConfig) -> CommandOutcome:
            if selected_mode == "fast":
                fast_pack = engine.context(request)
                return _model_outcome(
                    fast_pack,
                    scope=partition,
                    degraded=fast_pack.degraded,
                )

            evidence_pack = engine.evidence_context(request, ttl_seconds=parsed_ttl)
            data = evidence_pack.model_dump(mode="json")
            warnings: tuple[str, ...] = ()
            if out is not None:
                exported = engine.export_evidence_pack(evidence_pack.pack_id, out)
                data["exported_to"] = str(exported)
                data["export_managed"] = False
                warnings = (
                    "The exported Evidence Pack is an unmanaged external copy; "
                    "later engine purge cannot remove it.",
                )
            return CommandOutcome(
                data=data,
                scope=partition.serialize(),
                partition_ids=(partition.serialize(),),
                degraded=evidence_pack.degraded,
                warnings=warnings,
            )

        return _with_engine(db, build)

    def human(outcome: CommandOutcome) -> str:
        if outcome.data["mode"] == "evidence":
            lines = [
                f"Evidence Pack {outcome.data['pack_id']}",
                str(outcome.data["manifest"]),
            ]
            if exported_to := outcome.data.get("exported_to"):
                lines.extend(
                    (
                        f"Exported to: {exported_to}",
                        "Warning: this copy is outside engine-managed purge.",
                    )
                )
            return "\n".join(lines)
        items = outcome.data["items"]
        if not items:
            return "No context selected."
        header = f"Context: {outcome.data['token_count']}/{outcome.data['token_budget']} tokens"
        return "\n".join([header] + [f"- {item['content']} [{item['claim_id']}]" for item in items])

    _dispatch(
        "context",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=human,
    )


@app.command("get")
def get_memory(
    claim_id: Annotated[
        str | None,
        typer.Argument(help="Opaque memory claim ID."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Optional exact scope constraint."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Get one canonical memory with revision and evidence history."""

    def action(_: str) -> CommandOutcome:
        partition = _parse_scope(scope, required=False)
        record = _with_engine(
            db,
            lambda engine, __: engine.get(
                _require(claim_id, "claim_id"),
                scope=partition,
            ),
        )
        return _model_outcome(record, scope=record.partition)

    def human(outcome: CommandOutcome) -> str:
        return (
            f"{outcome.data['content']}\n"
            f"{outcome.data['claim_id']} @ {outcome.data['revision_id']} "
            f"({outcome.data['status']}, {outcome.data['source_count']} source(s))"
        )

    _dispatch(
        "get",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=human,
    )


@app.command("govern")
def govern(
    claim_id: Annotated[
        str | None,
        typer.Argument(help="Opaque memory claim ID."),
    ] = None,
    expected_revision_id: Annotated[
        str | None,
        typer.Option(
            "--expected-revision-id",
            help="Required compare-and-swap revision.",
        ),
    ] = None,
    action_name: Annotated[
        str | None,
        typer.Option("--action", help="confirm, reject, quarantine, or activate."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Required audit reason."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Create a governance revision using compare-and-swap."""

    def action(_: str) -> CommandOutcome:
        request = GovernRequest(
            claim_id=_require(claim_id, "claim_id"),
            expected_revision_id=_require(
                expected_revision_id,
                "--expected-revision-id",
            ),
            action=GovernAction(_require(action_name, "--action")),
            reason=_require(reason, "--reason"),
        )
        receipt = _with_engine(db, lambda engine, __: engine.govern(request))
        return _model_outcome(receipt)

    _dispatch(
        "govern",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: (
            f"Governed {outcome.data['claim_id']} -> {outcome.data['status']} "
            f"(revision {outcome.data['revision_id']})."
        ),
    )


@app.command("forget")
def forget(
    target: Annotated[
        str | None,
        typer.Argument(help="Typed target as '<event|claim|subject|partition>:<id>'."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Required for subjects; optional exact constraint."),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Required retry-safe deletion key."),
    ] = None,
    expected_revision_id: Annotated[
        str | None,
        typer.Option("--expected-revision-id", help="Optional claim revision guard."),
    ] = None,
    cascade_policy: Annotated[
        str,
        typer.Option("--cascade-policy", help="safe or purge."),
    ] = "safe",
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Create an immediate deletion fence and engine-managed purge job."""

    def action(_: str) -> CommandOutcome:
        partition = _parse_scope(scope, required=False)
        request = ForgetRequest(
            target=ForgetTarget.parse(_require(target, "target")),
            scope=partition,
            idempotency_key=_require(idempotency_key, "--idempotency-key"),
            expected_revision_id=expected_revision_id,
            cascade_policy=_parse_cascade_policy(cascade_policy),
        )
        receipt = _with_engine(db, lambda engine, __: engine.forget(request))
        return _model_outcome(receipt, scope=partition)

    _dispatch(
        "forget",
        json_output=json_output,
        fallback_scope=scope,
        action=action,
        human=lambda outcome: (
            f"Deletion {outcome.data['deletion_id']}: {outcome.data['state']} "
            f"for {outcome.data['target']['target_type']}:"
            f"{outcome.data['target']['target_id']}."
        ),
    )


@app.command("forget-status")
def forget_status(
    deletion_id: Annotated[
        str | None,
        typer.Argument(help="Opaque deletion receipt ID."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect every managed layer of a deletion receipt."""

    def action(_: str) -> CommandOutcome:
        receipt = _with_engine(
            db,
            lambda engine, __: engine.deletion_status(_require(deletion_id, "deletion_id")),
        )
        return _model_outcome(receipt)

    _dispatch(
        "forget-status",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: (
            f"Deletion {outcome.data['deletion_id']}: {outcome.data['state']} "
            f"({len(outcome.data['layers'])} managed layer(s))."
        ),
    )


@app.command("purge")
def purge(
    deletion_id: Annotated[
        str | None,
        typer.Argument(help="Opaque deletion receipt ID."),
    ] = None,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Physically clear engine-managed text for a deletion receipt."""

    def action(_: str) -> CommandOutcome:
        receipt = _with_engine(
            db,
            lambda engine, __: engine.purge(_require(deletion_id, "deletion_id")),
        )
        return _model_outcome(receipt)

    _dispatch(
        "purge",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=lambda outcome: f"Deletion {outcome.data['deletion_id']}: {outcome.data['state']}.",
    )


@app.command("reindex")
def reindex(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report planned work without changing the index."),
    ] = False,
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Rebuild the disposable FTS projection from canonical memory."""

    def action(_: str) -> CommandOutcome:
        result = _with_engine(db, lambda engine, __: engine.reindex(dry_run=dry_run))
        return CommandOutcome(data=result)

    def human(outcome: CommandOutcome) -> str:
        if outcome.data["dry_run"]:
            return (
                f"Would consider {outcome.data['would_index']} claim(s); "
                f"{outcome.data['currently_indexed']} currently indexed."
            )
        return f"Reindexed {outcome.data['indexed']} of {outcome.data['considered']} claim(s)."

    _dispatch(
        "reindex",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=human,
    )


def _redacted_config(config: RuntimeConfig) -> dict[str, Any]:
    key_present = bool(os.environ.get(PURGE_KEY_ENV)) or config.purge_key_path.is_file()
    return {
        "database": {
            "path": str(config.database_path),
            "source": config.database_source,
        },
        "durable": config.durable,
        "purge_registry": {
            "path": str(config.purge_registry_path),
            "key_path": str(config.purge_key_path),
            "key_source": config.purge_key_source,
            "key": "<redacted>" if key_present else None,
        },
        "environment": {
            DATABASE_ENV: (str(config.database_path) if os.environ.get(DATABASE_ENV) else None),
            PURGE_KEY_ENV: "<redacted>" if os.environ.get(PURGE_KEY_ENV) else None,
        },
    }


@config_app.command("show")
def config_show(
    db: DbOption = None,
    json_output: JsonOption = False,
) -> None:
    """Show effective paths and settings without revealing key material."""

    def action(_: str) -> CommandOutcome:
        return CommandOutcome(data=_redacted_config(_resolve_database_path(db)))

    def human(outcome: CommandOutcome) -> str:
        database = outcome.data["database"]
        registry = outcome.data["purge_registry"]
        return (
            f"Database: {database['path']} ({database['source']})\n"
            f"Purge registry: {registry['path']} (key: {registry['key_source']})\n"
            f"Durable writes: {'on' if outcome.data['durable'] else 'off'}"
        )

    _dispatch(
        "config.show",
        json_output=json_output,
        fallback_scope=None,
        action=action,
        human=human,
    )


HELP_CATALOG: tuple[dict[str, Any], ...] = (
    {"name": "init", "summary": "Initialize or verify local storage."},
    {"name": "doctor", "summary": "Run storage and index health checks."},
    {
        "name": "remember",
        "summary": "Record explicit memory.",
        "required": ["content", "--scope"],
    },
    {
        "name": "capture",
        "summary": "Capture an event for automatic formation.",
        "required": ["payload", "--scope", "--external-event-id"],
    },
    {
        "name": "formation-process",
        "summary": "Process one ready automatic-formation job.",
    },
    {
        "name": "formation-status",
        "summary": "Inspect one automatic-formation job.",
        "required": ["job_id"],
    },
    {
        "name": "feedback",
        "summary": "Append non-governing memory feedback.",
        "required": ["claim_id", "--revision-id", "--type"],
    },
    {
        "name": "search",
        "summary": "Search canonical memory.",
        "required": ["query", "--scope"],
    },
    {
        "name": "context",
        "summary": "Build a bounded context pack.",
        "required": ["query", "--scope"],
    },
    {"name": "get", "summary": "Get memory with provenance.", "required": ["claim_id"]},
    {
        "name": "govern",
        "summary": "Create a governance revision.",
        "required": [
            "claim_id",
            "--expected-revision-id",
            "--action",
            "--reason",
        ],
    },
    {
        "name": "forget",
        "summary": "Create a typed deletion fence.",
        "required": ["target", "--idempotency-key"],
    },
    {
        "name": "forget-status",
        "summary": "Inspect a deletion receipt.",
        "required": ["deletion_id"],
    },
    {
        "name": "purge",
        "summary": "Purge engine-managed text.",
        "required": ["deletion_id"],
    },
    {"name": "reindex", "summary": "Rebuild the FTS projection."},
    {"name": "config show", "summary": "Show redacted effective configuration."},
    {"name": "help-json", "summary": "Describe the machine-facing CLI surface."},
)

ERROR_SPECS = (
    VALIDATION_ERROR,
    SCOPE_DENIED,
    NOT_FOUND,
    REVISION_CONFLICT,
    IDEMPOTENCY_KEY_REUSED,
    TEMPORARY_FAILURE,
    FEATURE_NOT_ENABLED,
    PURGE_REGISTRY_REQUIRED,
)


@app.command("help-json")
def help_json(
    json_output: JsonOption = False,
) -> None:
    """Describe commands for agent-side capability discovery."""

    data = {
        "name": PUBLIC_COMMAND,
        "contract_version": CONTRACT_VERSION,
        "data_schema_version": DATA_SCHEMA_VERSION,
        "json_contract": {
            "stdout_values": 1,
            "success_exit_code": 0,
            "errors": [
                {
                    "code": spec.code,
                    "exit_code": spec.exit_code,
                    "retryable": spec.retryable,
                }
                for spec in ERROR_SPECS
            ],
        },
        "common_options": {
            "--db": "Absolute SQLite path; defaults to platform user-data storage.",
            "--json": "Emit one stable JSON envelope on stdout.",
        },
        "commands": list(HELP_CATALOG),
    }

    _dispatch(
        "help-json",
        json_output=json_output,
        fallback_scope=None,
        action=lambda _: CommandOutcome(data=data),
        human=lambda outcome: json.dumps(
            outcome.data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    app()
