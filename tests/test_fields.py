import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from py_extended_types import (
    Omit,
    Partial,
    Pick,
    ShapeError,
    assert_field,
    assert_shape,
    core,
)
from py_extended_types.core import check, registrations


class User(BaseModel):
    id: int
    name: str
    email: str | None = None
    tags: list[str] = Field(default_factory=list)


def _problems(cls: type) -> list[str]:
    return [
        f"{problem.rule}: {problem.message}"
        for registration in registrations()
        if registration.cls is cls
        for problem in check(registration)
    ]


def test_pick_takes_a_single_name() -> None:
    assert list(Pick(User, "name").fields) == ["name"]
    assert repr(Pick(User, "name")) == "Pick[User, 'name']"


def test_pick_takes_a_list_of_names_in_the_class_order() -> None:
    # Given
    shape = Pick(User, ["email", "id"])

    # When / Then
    assert list(shape.fields) == ["id", "email"]
    assert repr(shape) == "Pick[User, 'email' | 'id']"


def test_pick_rejects_non_string_keys_lazily() -> None:
    # Given
    shape = Pick(User, ["id", 1])  # type: ignore[list-item]

    # When / Then
    with pytest.raises(ShapeError, match="keys must be strings, got 1"):
        _ = shape.fields


def test_pick_rejects_an_empty_list() -> None:
    with pytest.raises(ShapeError, match="at least one key is required"):
        _ = Pick(User, []).fields


def test_source_can_be_a_shape() -> None:
    @assert_field("owner", Partial(User), "name")
    class Project(BaseModel):
        owner: str

    assert _problems(Project) == [
        "type-mismatch: Project.owner has type 'str', expected 'str | None' from Partial[User]['name']"
    ]


def test_matching_fields_pass_regardless_of_defaults() -> None:
    @assert_field("owner", User, "name")
    @assert_field("contact", User, "email")
    @assert_field("labels", User, "tags")
    class Project(BaseModel):
        title: str
        owner: str = "anon"
        contact: str | None
        labels: list[str]

    assert _problems(Project) == []


def test_type_mismatch_names_the_source() -> None:
    @assert_field("owner", User, "name")
    class Project(BaseModel):
        owner: int

    assert _problems(Project) == [
        "type-mismatch: Project.owner has type 'int', expected 'str' from User['name']"
    ]


def test_missing_field_is_reported() -> None:
    @assert_field("owner", User, "name")
    class Project(BaseModel):
        title: str

    assert _problems(Project) == [
        "missing-field: Project is missing field 'owner: str' from User['name']"
    ]


def test_unknown_source_key_is_an_invalid_shape() -> None:
    @assert_field("owner", User, "nmae")
    class Project(BaseModel):
        owner: str

    assert _problems(Project) == [
        "invalid-shape: User['nmae']: unknown field 'nmae'; "
        "User has 'id' | 'name' | 'email' | 'tags'"
    ]


def test_key_must_be_a_string() -> None:
    @assert_field("owner", User, 1)  # type: ignore[arg-type]
    class Project(BaseModel):
        owner: str

    assert _problems(Project) == [
        "invalid-shape: @assert_field: the key must be a string, got 1"
    ]


def test_combines_with_assert_shape() -> None:
    @assert_shape(Pick(User, "id"), exact=False)
    @assert_field("owner", User, "email")
    class Project(BaseModel):
        id: int
        owner: str

    assert _problems(Project) == [
        "type-mismatch: Project.owner has type 'str', expected 'str | None' from User['email']"
    ]


def test_decorator_is_a_no_op_outside_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(core, "_collecting", False)
    before = len(registrations())

    # When
    @assert_field("owner", User, "name")
    class Bad(BaseModel):
        owner: int

    # Then
    assert len(registrations()) == before
    assert list(Bad.model_fields) == ["owner"]


MODELS = textwrap.dedent(
    """
    from pydantic import BaseModel

    from py_extended_types import assert_field


    class User(BaseModel):
        name: str
        email: str | None = None


    @assert_field("owner", User, "name")
    @assert_field("contact", User, "email")
    class Project(BaseModel):
        owner: int = 0
    """
)


def test_cli_reports_and_fixes_field_declarations(tmp_path: Path) -> None:
    # Given
    (tmp_path / "field_models.py").write_text(MODELS)

    # When
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
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(completed.stdout)

    # Then
    assert report["classes"] == 1
    assert [
        (d["line"], d["rule"], d["fix"]["title"] if d["fix"] else None)
        for d in report["diagnostics"]
    ] == [
        # Decorators apply bottom-up, so `contact` is checked first.
        (
            13,
            "py-extended-types(missing-field)",
            "Add field 'contact: str | None = None'",
        ),
        (15, "py-extended-types(type-mismatch)", "Change to 'owner: str = 0'"),
    ]


def test_omit_takes_a_single_name_or_a_list() -> None:
    assert list(Omit(User, "tags").fields) == ["id", "name", "email"]
    assert list(Omit(User, ["id", "tags"]).fields) == ["name", "email"]
    assert repr(Omit(User, ["id", "tags"])) == "Omit[User, 'id' | 'tags']"


def test_pick_reports_a_non_list_lazily() -> None:
    # Given
    shape = Pick(User, 5)  # type: ignore[arg-type]

    # When / Then
    with pytest.raises(ShapeError, match="keys must be strings, got 5"):
        _ = shape.fields
