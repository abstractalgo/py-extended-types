import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

BASE = textwrap.dedent(
    """
    from pydantic import BaseModel


    class User(BaseModel):
        id: int
        name: str
    """
)

CONTRACTS = textwrap.dedent(
    """
    from pydantic import BaseModel

    from py_sat import Partial, expect_shape
    from stdin_base import User


    @expect_shape(Partial(User))
    class Patch(BaseModel):
        id: int | None = None
        name: str | None = None
    """
)


def _check(
    directory: Path, sources: dict[Path, str] | None = None
) -> list[dict[str, Any]]:
    args = [
        sys.executable,
        "-m",
        "py_sat",
        "check",
        "--no-venv",
        "--format",
        "json",
        ".",
    ]
    if sources is not None:
        args.append("--stdin")
    completed = subprocess.run(
        args,
        cwd=directory,
        input=json.dumps({str(path): text for path, text in (sources or {}).items()}),
        capture_output=True,
        text=True,
        check=False,
    )
    diagnostics: list[dict[str, Any]] = json.loads(completed.stdout)["diagnostics"]
    return diagnostics


def _project(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "stdin_base.py"
    contracts = tmp_path / "stdin_contracts.py"
    base.write_text(BASE)
    contracts.write_text(CONTRACTS)
    # Import once from disk so stale bytecode exists for the override to bypass.
    assert _check(tmp_path) == []
    return base, contracts


def test_unsaved_source_replaces_the_file_on_disk(tmp_path: Path) -> None:
    # Given
    _, contracts = _project(tmp_path)
    unsaved = CONTRACTS.replace(
        "class Patch(BaseModel):\n", "class Patch(BaseModel):\n    # new line\n"
    ).replace("name: str | None = None", "name: int | None = None")

    # When
    diagnostics = _check(tmp_path, {contracts: unsaved})

    # Then
    assert [
        (d["line"], d["rule"], d["fix"]["edits"][0]["newText"]) for d in diagnostics
    ] == [(12, "py-sat(type-mismatch)", "name: str | None = None")]


def test_unsaved_base_model_affects_other_files(tmp_path: Path) -> None:
    # Given
    base, _ = _project(tmp_path)
    unsaved_base = BASE.replace("    name: str\n", "    name: str\n    email: str\n")

    # When
    diagnostics = _check(tmp_path, {base: unsaved_base})

    # Then
    assert [(Path(d["path"]).name, d["rule"]) for d in diagnostics] == [
        ("stdin_contracts.py", "py-sat(missing-field)")
    ]


def test_import_error_names_the_unsaved_file_it_came_from(tmp_path: Path) -> None:
    # Given
    base, contracts = _project(tmp_path)
    unsaved_base = BASE + "\nUser.oops(\n"

    # When
    diagnostics = _check(tmp_path, {base: unsaved_base})

    # Then
    assert [(Path(d["path"]).name, d["rule"], d["origin"]) for d in diagnostics] == [
        ("stdin_contracts.py", "py-sat(import-error)", str(base.resolve()))
    ]
