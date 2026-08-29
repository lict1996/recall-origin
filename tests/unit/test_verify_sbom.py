from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_sbom.py"
EXPECTED_VERSION = "1.2.3"


def _installed_distributions() -> list[dict[str, str]]:
    return [
        {"name": "recall_origin", "version": EXPECTED_VERSION},
        {"name": "MCP", "version": "1.20.0"},
        {"name": "FastAPI", "version": "0.116.1"},
        {"name": "uvicorn", "version": "0.35.0"},
        {"name": "transitive_dep", "version": "4.5.6"},
    ]


def _spdx_packages() -> list[dict[str, str]]:
    return [
        {"name": ".venv", "SPDXID": "SPDXRef-DocumentRoot-Directory-venv"},
        {"name": "recall-origin", "versionInfo": EXPECTED_VERSION},
        {"name": "mcp", "versionInfo": "1.20.0"},
        {"name": "fastapi", "versionInfo": "0.116.1"},
        {"name": "uvicorn", "versionInfo": "0.35.0"},
        {"name": "transitive.dep", "versionInfo": "4.5.6"},
        {"name": "scanner-extra", "versionInfo": "9.9.9"},
    ]


def _write_sbom(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "release.spdx.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_probe(
    tmp_path: Path,
    *,
    stdout: str | None = None,
    distributions: object | None = None,
    returncode: int = 0,
) -> Path:
    if stdout is None:
        stdout = json.dumps(distributions)
    path = tmp_path / "venv-python"
    path.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                (
                    'if len(sys.argv) != 3 or sys.argv[1] != "-c" '
                    'or "importlib.metadata" not in sys.argv[2]:'
                ),
                "    raise SystemExit(97)",
                f"sys.stdout.write({stdout!r})",
                f"raise SystemExit({returncode})",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run_verifier(
    sbom: Path,
    probe: Path,
    *,
    expected_version: str = EXPECTED_VERSION,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sbom",
            str(sbom),
            "--venv-python",
            str(probe),
            "--expected-version",
            expected_version,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_accepts_all_installed_distributions_with_canonical_names_and_sbom_extras(
    tmp_path: Path,
) -> None:
    sbom = _write_sbom(tmp_path, {"packages": _spdx_packages()})
    probe = _write_probe(tmp_path, distributions=_installed_distributions())

    process = _run_verifier(sbom, probe)

    assert process.returncode == 0, process.stderr
    assert "SBOM verified" in process.stdout
    assert process.stderr == ""


def test_rejects_an_installed_transitive_distribution_missing_from_sbom(tmp_path: Path) -> None:
    packages = [package for package in _spdx_packages() if package.get("name") != "transitive.dep"]
    sbom = _write_sbom(tmp_path, {"packages": packages})
    probe = _write_probe(tmp_path, distributions=_installed_distributions())

    process = _run_verifier(sbom, probe)

    assert process.returncode == 1
    assert "transitive-dep==4.5.6" in process.stderr
    assert "Traceback" not in process.stderr


def test_rejects_recall_origin_version_that_differs_from_workflow_version(
    tmp_path: Path,
) -> None:
    installed = _installed_distributions()
    installed[0] = {"name": "recall-origin", "version": "9.9.9"}
    packages = _spdx_packages()
    packages[1] = {"name": "recall-origin", "versionInfo": "9.9.9"}
    sbom = _write_sbom(tmp_path, {"packages": packages})
    probe = _write_probe(tmp_path, distributions=installed)

    process = _run_verifier(sbom, probe)

    assert process.returncode == 1
    assert EXPECTED_VERSION in process.stderr
    assert "9.9.9" in process.stderr
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize("required_name", ["mcp", "fastapi", "uvicorn"])
def test_requires_release_extras_in_installed_environment(
    tmp_path: Path,
    required_name: str,
) -> None:
    installed = [
        distribution
        for distribution in _installed_distributions()
        if distribution["name"].lower() != required_name
    ]
    sbom = _write_sbom(tmp_path, {"packages": _spdx_packages()})
    probe = _write_probe(tmp_path, distributions=installed)

    process = _run_verifier(sbom, probe)

    assert process.returncode == 1
    assert required_name in process.stderr
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize(
    ("sbom_payload", "probe_stdout", "probe_returncode"),
    [
        pytest.param("{", None, 0, id="malformed-sbom-json"),
        pytest.param({"packages": _spdx_packages()}, "{", 0, id="malformed-probe-json"),
        pytest.param({"packages": _spdx_packages()}, None, 23, id="probe-process-failure"),
        pytest.param({"packages": {}}, None, 0, id="packages-not-a-list"),
        pytest.param(
            {"packages": [{"versionInfo": "1.0"}]},
            None,
            0,
            id="spdx-package-missing-name",
        ),
    ],
)
def test_json_process_and_spdx_field_errors_fail_closed_without_traceback(
    tmp_path: Path,
    sbom_payload: object,
    probe_stdout: str | None,
    probe_returncode: int,
) -> None:
    if isinstance(sbom_payload, str):
        sbom = tmp_path / "release.spdx.json"
        sbom.write_text(sbom_payload, encoding="utf-8")
    else:
        sbom = _write_sbom(tmp_path, sbom_payload)
    probe = _write_probe(
        tmp_path,
        stdout=probe_stdout,
        distributions=_installed_distributions(),
        returncode=probe_returncode,
    )

    process = _run_verifier(sbom, probe)

    assert process.returncode == 1
    assert process.stderr.startswith("SBOM verification failed:")
    assert "Traceback" not in process.stderr


def test_distribution_field_errors_fail_closed_without_traceback(tmp_path: Path) -> None:
    installed: list[dict[str, str]] = [
        {"name": "recall-origin", "version": EXPECTED_VERSION},
        {"name": "mcp"},
    ]
    sbom = _write_sbom(tmp_path, {"packages": _spdx_packages()})
    probe = _write_probe(tmp_path, distributions=installed)

    process = _run_verifier(sbom, probe)

    assert process.returncode == 1
    assert process.stderr.startswith("SBOM verification failed:")
    assert "Traceback" not in process.stderr
