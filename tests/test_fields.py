import json
import subprocess
import sys
import textwrap
from pathlib import Path

from helpers import USER, problems


def test_source_can_be_a_shape(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", Partial(User), "name")
class Project(BaseModel):
    owner: str
""",
    ) == {
        "Project": [
            "type-mismatch: Project.owner has type 'str', expected 'str | None' from Partial[User]['name']"
        ]
    }


def test_matching_fields_pass_regardless_of_defaults(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", User, "name")
@assert_field("contact", User, "nickname")
@assert_field("labels", User, "tags")
class Project(BaseModel):
    title: str
    owner: str = "anon"
    contact: str | None
    labels: list[str]
""",
    ) == {"Project": []}


def test_type_mismatch_names_the_source(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", User, "name")
class Project(BaseModel):
    owner: int
""",
    ) == {
        "Project": [
            "type-mismatch: Project.owner has type 'int', expected 'str' from User['name']"
        ]
    }


def test_missing_field_is_reported(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", User, "name")
class Project(BaseModel):
    title: str
""",
    ) == {
        "Project": [
            "missing-field: Project is missing field 'owner: str' from User['name']"
        ]
    }


def test_unknown_source_key_is_an_invalid_shape(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", User, "nmae")
class Project(BaseModel):
    owner: str
""",
    ) == {
        "Project": [
            "invalid-shape: User['nmae']: unknown field 'nmae'; "
            "User has 'id' | 'name' | 'email' | 'nickname' | 'tags'"
        ]
    }


def test_field_and_key_must_be_strings(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_field("owner", User, 1)
@assert_field(OWNER, User, "name")
class Project(BaseModel):
    owner: str

OWNER = "owner"
""",
    ) == {
        "Project": [
            "invalid-shape: @assert_field: the key must be a string, got 1",
        ]
    }


def test_combines_with_assert_shape(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Pick(User, "id"), exact=False)
@assert_field("owner", User, "nickname")
class Project(BaseModel):
    id: int
    owner: str
""",
    ) == {
        "Project": [
            "type-mismatch: Project.owner has type 'str', expected 'str | None' from User['nickname']"
        ]
    }


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
        (15, "py-extended-types(type-mismatch)", "Change to 'owner: str = 0'"),
        (
            13,
            "py-extended-types(missing-field)",
            "Add field 'contact: str | None = None'",
        ),
    ]
