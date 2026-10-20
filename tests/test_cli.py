import json
from pathlib import Path

import pytest

from py_extended_types import cli

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def test_check_reports_stylish_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.chdir(FIXTURES)

    # When
    exit_code = cli.main(["check", "--no-venv", "demo_pkg/models.py"])

    # Then
    assert exit_code == 1
    assert capsys.readouterr().out == (
        "demo_pkg/models.py\n"
        "  19:5  error  CreateUser.name has type 'int', expected 'str'  py-extended-types(type-mismatch)\n"
        "\n"
        "✖ 1 problem (1 error, 0 warnings)\n"
    )


def test_check_reports_import_errors_at_failing_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.chdir(FIXTURES)

    # When
    exit_code = cli.main(["check", "--no-venv", "demo_pkg/broken.py"])

    # Then
    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.startswith("demo_pkg/broken.py\n  6:14  error")
    assert (
        "could not import module: NameError: name 'missing_base' is not defined  py-extended-types(import-error)"
        in out
    )


def test_check_is_silent_on_success_unless_verbose(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    (tmp_path / "ok_models.py").write_text(
        "from pydantic import BaseModel\n"
        "from py_extended_types import Partial, assert_shape\n\n\n"
        "class User(BaseModel):\n    id: int\n\n\n"
        "@assert_shape(Partial(User))\n"
        "class Patch(BaseModel):\n    id: int | None = None\n"
    )
    monkeypatch.chdir(tmp_path)

    # When
    quiet = cli.main(["check", "--no-venv", "."])
    quiet_out = capsys.readouterr().out
    verbose = cli.main(["check", "--no-venv", "-v", "."])

    # Then
    assert (quiet, quiet_out) == (0, "")
    assert verbose == 0
    assert "0 problems" in capsys.readouterr().out


def test_show_prints_shape(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.chdir(FIXTURES)

    # When
    exit_code = cli.main(
        ["show", "--no-venv", "demo_pkg.models:Partial(Omit(User, 'id'))"]
    )

    # Then
    assert exit_code == 0
    assert (
        capsys.readouterr().out == "Partial[Omit[User, 'id']]:\n  name?: str | None\n"
    )


def test_check_json_format(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.chdir(FIXTURES)

    # When
    exit_code = cli.main(
        ["check", "--no-venv", "--format", "json", "demo_pkg/models.py"]
    )

    # Then
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert (report["version"], report["classes"], report["files"]) == (1, 2, 1)
    assert report["diagnostics"] == [
        {
            "path": str((FIXTURES / "demo_pkg" / "models.py").resolve()),
            "line": 19,
            "column": 5,
            "endLine": 19,
            "endColumn": 9,
            "severity": "error",
            "rule": "py-extended-types(type-mismatch)",
            "message": "CreateUser.name has type 'int', expected 'str'",
            "origin": None,
            "fix": {
                "title": "Change to 'name: str'",
                "edits": [
                    {
                        "line": 19,
                        "column": 5,
                        "endLine": 19,
                        "endColumn": 14,
                        "newText": "name: str",
                    }
                ],
            },
        }
    ]
