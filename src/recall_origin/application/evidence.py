"""Atomic filesystem lifecycle for managed Evidence Packs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_PACK_BYTES = 20 * 1024 * 1024
MAX_RESOURCE_BYTES = 5 * 1024 * 1024
ORPHAN_RECONCILIATION_GRACE_SECONDS = 300
_PACK_DIRECTORY = re.compile(r"^pack_[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_STAGING_DIRECTORY = re.compile(r"^\.pack_[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\.[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class PackWriteResult:
    root: Path
    manifest_sha256: str
    resource_paths: tuple[str, ...]


def _is_managed_pack_directory_name(name: str) -> bool:
    return bool(_PACK_DIRECTORY.fullmatch(name) or _STAGING_DIRECTORY.fullmatch(name))


def reconcile_managed_pack_root(
    *,
    managed_root: Path,
    registered_roots: tuple[Path, ...],
    grace_seconds: int = ORPHAN_RECONCILIATION_GRACE_SECONDS,
) -> tuple[Path, ...]:
    """Remove stale, recognizable crash orphans without following links.

    A grace period prevents another process's in-flight staging or just-renamed
    pack from being mistaken for an orphan before its database registration.
    Unknown names, non-direct children, symlinks, and registered roots are
    deliberately left untouched.
    """

    if grace_seconds < 0:
        raise ValueError("managed Evidence Pack orphan grace cannot be negative")
    if not managed_root.exists():
        return ()
    if managed_root.is_symlink():
        raise ValueError("managed Evidence Pack root must not be a symbolic link")
    root = managed_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("managed Evidence Pack root must be a directory")

    registered_names: set[str] = set()
    for registered in registered_roots:
        expanded = registered.expanduser()
        try:
            parent = expanded.parent.resolve(strict=True)
        except OSError:
            continue
        if parent == root:
            registered_names.add(expanded.name)

    cutoff = time.time() - grace_seconds
    removed: list[Path] = []
    for candidate in root.iterdir():
        if (
            candidate.name in registered_names
            or not _is_managed_pack_directory_name(candidate.name)
            or candidate.is_symlink()
        ):
            continue
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except OSError:
            continue
        if not candidate.is_dir() or metadata.st_mtime > cutoff:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.parent != root or resolved == root:
            continue
        shutil.rmtree(resolved)
        removed.append(resolved)
    return tuple(removed)


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe Evidence Pack resource path: {value!r}")
    return path


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def write_managed_pack(
    *,
    managed_root: Path,
    pack_id: str,
    resources: dict[str, str | bytes],
    manifest_payload: dict[str, Any],
) -> PackWriteResult:
    """Write a bounded pack to a private temp directory and rename atomically."""

    if not pack_id or "/" in pack_id or "\\" in pack_id or pack_id in {".", ".."}:
        raise ValueError("pack_id is not safe for a managed directory")
    managed_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if managed_root.is_symlink():
        raise ValueError("managed Evidence Pack root must not be a symbolic link")
    final_root = managed_root / pack_id
    if final_root.exists():
        raise FileExistsError(f"Evidence Pack already exists: {pack_id}")

    normalized: dict[str, bytes] = {}
    total_bytes = 0
    for relative, content in resources.items():
        safe = _safe_relative_path(relative).as_posix()
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        if len(encoded) > MAX_RESOURCE_BYTES:
            raise ValueError(f"Evidence Pack resource exceeds limit: {safe}")
        total_bytes += len(encoded)
        if total_bytes > MAX_PACK_BYTES:
            raise ValueError("Evidence Pack exceeds the managed size limit")
        normalized[safe] = encoded

    integrity = {
        relative: hashlib.sha256(content).hexdigest()
        for relative, content in sorted(normalized.items())
    }
    complete_manifest = {
        **manifest_payload,
        "integrity": {
            "algorithm": "sha256",
            "resources": integrity,
        },
    }
    manifest_bytes = json.dumps(
        complete_manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if total_bytes + len(manifest_bytes) > MAX_PACK_BYTES:
        raise ValueError("Evidence Pack exceeds the managed size limit")

    temporary = Path(tempfile.mkdtemp(prefix=f".{pack_id}.", dir=managed_root))
    try:
        for relative, content in normalized.items():
            _write_private(temporary / relative, content)
        _write_private(temporary / "manifest.json", manifest_bytes)
        os.replace(temporary, final_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return PackWriteResult(
        root=final_root,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        resource_paths=tuple([*sorted(normalized), "manifest.json"]),
    )


def read_managed_resource(
    *,
    managed_root: Path,
    pack_root: Path,
    relative_path: str,
) -> bytes:
    """Read one bounded regular file without permitting path traversal."""

    safe = _safe_relative_path(relative_path)
    configured_root = managed_root.resolve(strict=True)
    if pack_root.is_symlink():
        raise FileNotFoundError(relative_path)
    root = pack_root.resolve(strict=True)
    if root.parent != configured_root or root == configured_root:
        raise FileNotFoundError(relative_path)
    unresolved = root / Path(*safe.parts)
    if unresolved.is_symlink():
        raise FileNotFoundError(relative_path)
    candidate = unresolved.resolve(strict=True)
    if root not in candidate.parents or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    size = candidate.stat().st_size
    if size > MAX_RESOURCE_BYTES:
        raise ValueError("managed Evidence Pack resource exceeds the read limit")
    return candidate.read_bytes()


def export_pack_snapshot(
    *,
    destination: Path,
    resources: dict[str, bytes],
) -> Path:
    """Create a private, unmanaged copy without overwriting an existing path."""

    expanded = destination.expanduser()
    parent = expanded.parent.resolve()
    target = parent / expanded.name
    if not expanded.name or expanded.name in {".", ".."}:
        raise ValueError("Evidence Pack export destination must name a directory")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=False)
    try:
        total_bytes = 0
        for relative, content in resources.items():
            safe = _safe_relative_path(relative)
            if len(content) > MAX_RESOURCE_BYTES:
                raise ValueError(f"Evidence Pack resource exceeds limit: {relative}")
            total_bytes += len(content)
            if total_bytes > MAX_PACK_BYTES:
                raise ValueError("Evidence Pack exceeds the export size limit")
            _write_private(target / Path(*safe.parts), content)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def remove_managed_pack(*, managed_root: Path, pack_root: Path) -> None:
    """Remove one registered pack while refusing paths outside its managed root."""

    root = managed_root.resolve(strict=True)
    if pack_root.is_symlink():
        raise ValueError("managed Evidence Pack directory must not be a symbolic link")
    candidate = pack_root.resolve(strict=True)
    if candidate.parent != root or candidate == root:
        raise ValueError("registered Evidence Pack path escaped its managed root")
    shutil.rmtree(candidate)
