import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

MODELS = textwrap.dedent(
    '''
    from pydantic import BaseModel, Field

    from py_extended_types import Omit, Partial, expect_shape


    class User(BaseModel):
        id: int
        name: str
        email: str
        flag: bool = Field(title="Flag")


    @expect_shape(Partial(User))
    class Patch(BaseModel):
        """Docstring stays first."""

        id: int
        name: str | None = Field(title="Name")
        flag: bool = Field(title="Flag")
        extra: str = "x"


    @expect_shape(Omit(User, "id"))
    class Create(BaseModel):
        name: str = "anon"


    @expect_shape(Omit(User, "id"))
    class OnlyExtras(BaseModel):
        legacy: int
    '''
)


def _check(directory: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "py_extended_types",
            "check",
            "--no-venv",
            "--format",
            "json",
            ".",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    return dict(json.loads(completed.stdout))


def _apply(text: str, edits: list[dict[str, Any]]) -> str:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(line: int, column: int) -> int:
        return starts[line - 1] + column - 1

    # Apply back to front; inserts at the same position keep their listed order.
    spans = sorted(
        (
            (
                offset(e["line"], e["column"]),
                offset(e["endLine"], e["endColumn"]),
                index,
                e["newText"],
            )
            for index, e in enumerate(edits)
        ),
        reverse=True,
    )
    for (_, end, _, _), (next_start, _, _, _) in zip(spans[1:], spans, strict=False):
        assert end <= next_start, "fixes overlap"
    for start, end, _, new_text in spans:
        text = text[:start] + new_text + text[end:]
    return text


def test_applying_every_fix_makes_the_file_match(tmp_path: Path) -> None:
    # Given
    models = tmp_path / "fix_models.py"
    models.write_text(MODELS)
    before = _check(tmp_path)
    fixes = [d["fix"] for d in before["diagnostics"]]

    # When
    unique_edits = {
        json.dumps(edit, sort_keys=True): edit
        for fix in fixes
        if fix
        for edit in fix["edits"]
    }
    models.write_text(_apply(MODELS, list(unique_edits.values())))
    after = _check(tmp_path)

    # Then
    assert all(fix is not None for fix in fixes)
    assert after["diagnostics"] == []
    assert models.read_text() == textwrap.dedent(
        '''
        from pydantic import BaseModel, Field

        from py_extended_types import Omit, Partial, expect_shape


        class User(BaseModel):
            id: int
            name: str
            email: str
            flag: bool = Field(title="Flag")


        @expect_shape(Partial(User))
        class Patch(BaseModel):
            """Docstring stays first."""

            id: int | None = None
            name: str | None = Field(default=None, title="Name")
            flag: bool | None = Field(default=None, title="Flag")
            email: str | None = None


        @expect_shape(Omit(User, "id"))
        class Create(BaseModel):
            name: str
            email: str
            flag: bool


        @expect_shape(Omit(User, "id"))
        class OnlyExtras(BaseModel):
            name: str
            email: str
            flag: bool
        '''
    )
