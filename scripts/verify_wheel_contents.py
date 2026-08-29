"""Compare two wheels by their extracted, security-checked file contents."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path, PurePosixPath


def _wheel_contents(path: Path) -> dict[str, bytes]:
    if not path.is_file() or path.suffix != ".whl":
        raise ValueError(f"Not a wheel file: {path}")

    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            normalized = PurePosixPath(name)
            if name.endswith("/") and not normalized.is_absolute() and ".." not in normalized.parts:
                continue
            if not name or normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError(f"Unsafe wheel member in {path}: {name!r}")
            if name in contents:
                raise ValueError(f"Duplicate wheel member in {path}: {name!r}")
            contents[name] = archive.read(info)
    return contents


def verify_equivalent(first: Path, second: Path) -> None:
    first_contents = _wheel_contents(first)
    second_contents = _wheel_contents(second)
    first_names = set(first_contents)
    second_names = set(second_contents)
    missing = sorted(first_names - second_names)
    extra = sorted(second_names - first_names)
    changed = sorted(
        name for name in first_names & second_names if first_contents[name] != second_contents[name]
    )
    if missing or extra or changed:
        sections = []
        if missing:
            sections.append(f"missing from rebuilt wheel: {missing}")
        if extra:
            sections.append(f"extra in rebuilt wheel: {extra}")
        if changed:
            sections.append(f"content differs: {changed}")
        raise ValueError("; ".join(sections))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that two wheels contain exactly the same files and bytes."
    )
    parser.add_argument("published_wheel", type=Path)
    parser.add_argument("rebuilt_wheel", type=Path)
    arguments = parser.parse_args()
    try:
        verify_equivalent(arguments.published_wheel, arguments.rebuilt_wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Wheel equivalence check failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wheel contents are equivalent: {arguments.published_wheel} == {arguments.rebuilt_wheel}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
