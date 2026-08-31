from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python3.10 CI.
    import tomli as tomllib

from quant_paper_sim import __version__

ROOT = Path(__file__).resolve().parents[1]
EXECUTION_TAG = "v0.5.1"
EXECUTION_COMMIT = "15e4e5c9dbaf2fe9b438732b2e94db295d5ea58c"


def test_release_metadata_uses_exact_tag_schema_and_lock_contracts() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    workspace = metadata["tool"]["quant-workspace"]
    dependency = next(
        item for item in project["dependencies"] if item.startswith("quant-execution")
    )
    schemas = {(item["id"], item["version"]) for item in workspace["schemas"]}

    assert project["version"] == __version__ == "0.2.2"
    assert dependency.endswith(f"@{EXECUTION_TAG}")
    assert workspace["layer"] == "execution"
    assert workspace["lock-files"] == ["requirements.lock"]
    assert schemas == {
        ("quant-paper-execution-log", "1.0.0"),
        ("puresaber.execution.order", "1.1.0"),
        ("puresaber.execution.order-event", "1.1.0"),
        ("puresaber.execution.fill", "1.1.0"),
        ("puresaber.execution.fee", "1.1.0"),
        ("puresaber.execution.ledger-transaction", "1.1.0"),
        ("puresaber.execution.run-result", "1.1.0"),
    }


def test_lock_readme_and_ci_preserve_reproducible_release_evidence() -> None:
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pip-compile with Python 3.10" in lock
    assert f"quant-execution.git@{EXECUTION_TAG}" in lock
    assert "quant-data-kit.git@v0.8.1" in lock
    assert 'exceptiongroup==1.3.1 ; python_version < "3.11"' in lock
    assert 'tomli==2.4.1 ; python_version < "3.11"' in lock
    assert "setuptools==84.0.0" in lock
    assert EXECUTION_COMMIT in readme
    assert "git revert <merge-commit>" in readme
    assert "pip install --no-deps --requirement requirements.lock" in workflow
    assert "pip install --no-deps --no-build-isolation --editable ." in workflow
    assert (
        "check_branch_coverage.py coverage.json --threshold 90 state execution engine" in workflow
    )
