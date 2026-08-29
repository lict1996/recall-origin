"""Verify that an SPDX SBOM covers a published wheel environment."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

Distribution = tuple[str, str]

_REQUIRED_RELEASE_PACKAGES = frozenset({"mcp", "fastapi", "uvicorn"})
_DISTRIBUTION_PROBE = """\
import importlib.metadata
import json

print(json.dumps([
    {
        "name": distribution.metadata["Name"],
        "version": distribution.version,
    }
    for distribution in importlib.metadata.distributions()
]))
"""


class VerificationError(Exception):
    """A release SBOM cannot be proven complete."""


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(f"{field} must be a non-empty string")
    return value


def _decode_json(text: str, source: str) -> object:
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"{source} is not valid JSON: {exc.msg}") from exc
    return payload


def _installed_distributions(venv_python: Path) -> set[Distribution]:
    try:
        process = subprocess.run(
            [str(venv_python), "-c", _DISTRIBUTION_PROBE],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"could not run venv Python {venv_python}: {exc}") from exc
    if process.returncode != 0:
        raise VerificationError(
            f"venv Python {venv_python} exited with status {process.returncode}"
        )

    payload = _decode_json(process.stdout, "venv distribution inventory")
    if not isinstance(payload, list):
        raise VerificationError("venv distribution inventory must be a JSON list")

    distributions: set[Distribution] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise VerificationError(f"venv distribution inventory item {index} must be an object")
        name = _required_string(item.get("name"), f"venv distribution item {index} name")
        version = _required_string(
            item.get("version"),
            f"venv distribution item {index} version",
        )
        distributions.add((_canonical_name(name), version))
    return distributions


def _spdx_packages(sbom: Path) -> set[Distribution]:
    try:
        text = sbom.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"could not read SPDX SBOM {sbom}: {exc}") from exc

    payload = _decode_json(text, f"SPDX SBOM {sbom}")
    if not isinstance(payload, dict):
        raise VerificationError("SPDX SBOM root must be an object")
    packages = payload.get("packages")
    if not isinstance(packages, list):
        raise VerificationError("SPDX SBOM packages must be a list")

    distributions: set[Distribution] = set()
    for index, item in enumerate(packages):
        if not isinstance(item, dict):
            raise VerificationError(f"SPDX package {index} must be an object")
        name = _required_string(item.get("name"), f"SPDX package {index} name")
        if "versionInfo" not in item:
            # Syft emits the scanned directory as a versionless SPDX document root.
            continue
        version = _required_string(item.get("versionInfo"), f"SPDX package {index} versionInfo")
        distributions.add((_canonical_name(name), version))
    return distributions


def _format_distributions(distributions: set[Distribution]) -> str:
    return ", ".join(f"{name}=={version}" for name, version in sorted(distributions))


def verify_sbom(sbom: Path, venv_python: Path, expected_version: str) -> None:
    expected_version = _required_string(expected_version, "expected recall-origin version")
    expected = _installed_distributions(venv_python)

    recall_versions = {version for name, version in expected if name == "recall-origin"}
    if recall_versions != {expected_version}:
        found = ", ".join(sorted(recall_versions)) or "none"
        raise VerificationError(
            "installed recall-origin version does not match workflow version "
            f"{expected_version!r}; found: {found}"
        )

    installed_names = {name for name, _version in expected}
    missing_release_packages = _REQUIRED_RELEASE_PACKAGES - installed_names
    if missing_release_packages:
        raise VerificationError(
            "published environment is missing required release packages: "
            + ", ".join(sorted(missing_release_packages))
        )

    actual = _spdx_packages(sbom)
    missing_from_sbom = expected - actual
    if missing_from_sbom:
        raise VerificationError(
            "SPDX SBOM is missing installed distributions: "
            + _format_distributions(missing_from_sbom)
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify an SPDX SBOM against a published virtual environment."
    )
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--venv-python", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    sbom = cast(Path, arguments.sbom)
    venv_python = cast(Path, arguments.venv_python)
    expected_version = cast(str, arguments.expected_version)
    try:
        verify_sbom(sbom, venv_python, expected_version)
    except VerificationError as exc:
        print(f"SBOM verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"SBOM verified against published environment: {sbom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
