from pathlib import Path

import pytest
from helpers import USER, fields, problems, shape
from pydantic import BaseModel

from py_extended_types import Omit, Partial, Pick, Shape, assert_field, assert_shape
from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.fields import HAS_DEFAULT, NO_DEFAULT


def test_partial_makes_fields_optional_and_nullable(tmp_path: Path) -> None:
    # When
    result = shape(tmp_path, USER, "Partial(User)")

    # Then
    assert repr(result) == "Partial[User]"
    assert all(not member.required for member in result.fields.values())
    assert fields(tmp_path, USER, "Partial(User)")["name"] == ("str | None", None)
    assert fields(tmp_path, USER, "Partial(User)")["nickname"] == ("str | None", None)


def test_partial_without_nullable_keeps_types(tmp_path: Path) -> None:
    assert fields(tmp_path, USER, "Partial(User, nullable=False)") == {
        "id": ("int", HAS_DEFAULT),
        "name": ("str", HAS_DEFAULT),
        "email": ("str", HAS_DEFAULT),
        "nickname": ("str | None", None),
        "tags": ("list[str]", HAS_DEFAULT),
    }


def test_required_makes_every_field_required_and_non_nullable(tmp_path: Path) -> None:
    assert fields(tmp_path, USER, "Required(User)") == {
        "id": ("int", NO_DEFAULT),
        "name": ("str", NO_DEFAULT),
        "email": ("str", NO_DEFAULT),
        "nickname": ("str", NO_DEFAULT),
        "tags": ("list[str]", NO_DEFAULT),
    }


def test_required_undoes_partial(tmp_path: Path) -> None:
    assert fields(tmp_path, USER, "Required(Partial(User))") == fields(
        tmp_path, USER, "Required(User)"
    )


def test_required_class_matches_without_none(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Required(Pick(User, ["id", "nickname"])))
class Strict(BaseModel):
    id: int
    nickname: str

@assert_shape(Required(Pick(User, "nickname")))
class StillNullable(BaseModel):
    nickname: str | None
""",
    ) == {
        "Strict": [],
        "StillNullable": [
            "type-mismatch: StillNullable.nickname has type 'str | None', expected 'str'"
        ],
    }


def test_pick_and_omit_select_fields(tmp_path: Path) -> None:
    assert list(shape(tmp_path, USER, 'Pick(User, ["name", "id"])').fields) == [
        "id",
        "name",
    ]
    assert list(shape(tmp_path, USER, "Pick(User, 'name')").fields) == ["name"]
    assert list(shape(tmp_path, USER, 'Omit(User, ["id", "tags"])').fields) == [
        "name",
        "email",
        "nickname",
    ]
    assert repr(shape(tmp_path, USER, 'Omit(User, ["id", "tags"])')) == (
        "Omit[User, 'id' | 'tags']"
    )


def test_keys_and_shapes_can_be_module_level_names(tmp_path: Path) -> None:
    # Given
    source = USER + '\nKEYS = ["id", "name"]\nPublic = Pick(User, KEYS)\n'

    # Then
    assert repr(shape(tmp_path, source, "Partial(Public)")) == (
        "Partial[Pick[User, 'id' | 'name']]"
    )


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ('Omit(User, "idd")', "unknown field\\(s\\) 'idd'; User has 'id' \\| 'name'"),
        ("Pick(User, [])", "at least one key is required"),
        ("Pick(User, ['id', 1])", "keys must be strings, got 1"),
        ("Pick(User, 5)", "keys must be strings, got 5"),
        ("Pick(User, 'id', 'name')", "Pick\\(\\) takes 2 positional arguments, got 3"),
        ("Partial(User, nullable=1)", "nullable must be True or False"),
        ("Partial(make_user())", "can't read make_user\\(\\) statically"),
        ("Partial(KeyOf(User))", "KeyOf\\(...\\) lists field names"),
    ],
)
def test_invalid_shapes_are_shape_errors(
    tmp_path: Path, expression: str, message: str
) -> None:
    with pytest.raises(ShapeError, match=message):
        shape(tmp_path, USER + "\ndef make_user(): return User\n", expression)


def test_partial_user_matches(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Partial(User))
class PartialUser(BaseModel):
    id: int | None = None
    name: str | None = None
    email: Optional[str] = None
    nickname: str | None = None
    tags: list[str] | None = None
""",
    ) == {"PartialUser": []}


def test_missing_optional_field_is_reported(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Partial(User))
class UserPatch(BaseModel):
    id: int | None = None
    name: str | None = None
    email: str | None = None
    nickname: str | None = None
""",
    ) == {
        "UserPatch": [
            "missing-field: UserPatch is missing optional field 'tags: list[str] | None' from Partial[User]"
        ]
    }


def test_required_field_where_partial_expects_optional_is_reported(
    tmp_path: Path,
) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Partial(Pick(User, ["id", "name"])))
class Patch(BaseModel):
    id: int | None
    name: str
""",
    ) == {
        "Patch": [
            "required-mismatch: Patch.id is optional in Partial[Pick[User, 'id' | 'name']] and needs a default",
            "type-mismatch: Patch.name has type 'str', expected 'str | None'",
            "required-mismatch: Patch.name is optional in Partial[Pick[User, 'id' | 'name']] and needs a default",
        ]
    }


def test_reports_type_mismatch_missing_and_extra_fields(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Omit(User, "id"))
class CreateUser(BaseModel):
    name: int
    nickname: str | None = None
    tags: list[str] = []
    password: str
""",
    ) == {
        "CreateUser": [
            "type-mismatch: CreateUser.name has type 'int', expected 'str'",
            "missing-field: CreateUser is missing required field 'email: str' from Omit[User, 'id']",
            "unexpected-field: CreateUser.password is not in Omit[User, 'id']",
        ]
    }


def test_exact_false_permits_additional_fields(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Pick(User, "id"), exact=False)
class UserWithMeta(BaseModel):
    id: int
    meta: dict[str, str]
""",
    ) == {"UserWithMeta": []}


def test_required_field_with_default_is_rejected(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Pick(User, "name"))
class Named(BaseModel):
    name: str = "anon"
""",
    ) == {
        "Named": [
            "required-mismatch: Named.name is required in Pick[User, 'name'] and must not have a default"
        ]
    }


def test_narrower_types_do_not_match(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Pick(User, ["name", "id"]))
class Narrow(BaseModel):
    name: Literal["a", "b"]
    id: bool
""",
    ) == {
        "Narrow": [
            "type-mismatch: Narrow.id has type 'bool', expected 'int'",
            "type-mismatch: Narrow.name has type 'Literal['a', 'b']', expected 'str'",
        ]
    }


def test_annotated_metadata_is_ignored(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
@assert_shape(Pick(User, "name"))
class Named(BaseModel):
    name: Annotated[str, Field(min_length=1)]
""",
    ) == {"Named": []}


def test_nested_classes_are_checked(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        USER
        + """
class Api:
    @assert_shape(Pick(User, "id"))
    class Ref(BaseModel):
        id: str
""",
    ) == {"Api.Ref": ["type-mismatch: Api.Ref.id has type 'str', expected 'int'"]}


def test_record_gives_every_key_the_same_type(tmp_path: Path) -> None:
    # When
    result = shape(tmp_path, "", 'Record(Literal["da", "ne"], float)')

    # Then
    assert repr(result) == "Record['da' | 'ne', float]"
    assert fields(tmp_path, "", 'Record(Literal["da", "ne"], float)') == {
        "da": ("float", NO_DEFAULT),
        "ne": ("float", NO_DEFAULT),
    }


def test_record_accepts_unions_of_literals_and_generic_values(tmp_path: Path) -> None:
    # Given
    expression = 'Record(Literal["a"] | Literal["b"], list[int] | None, name="Lists")'

    # Then
    assert repr(shape(tmp_path, "", expression)) == "Lists"
    assert [t for t, _ in fields(tmp_path, "", expression).values()] == [
        "list[int] | None",
        "list[int] | None",
    ]


def test_record_checks_a_class(tmp_path: Path) -> None:
    assert problems(
        tmp_path,
        """
@assert_shape(Partial(Record(Literal["da", "ne"], float)))
class Scores(BaseModel):
    da: float | None = None
    ne: int | None = None
""",
    ) == {
        "Scores": [
            "type-mismatch: Scores.ne has type 'int | None', expected 'float | None'"
        ]
    }


def test_record_over_key_of_another_class(tmp_path: Path) -> None:
    # When
    result = shape(tmp_path, USER, 'Record(KeyOf(Pick(User, ["id", "email"])), str)')

    # Then
    assert repr(result) == "Record[KeyOf[Pick[User, 'id' | 'email']], str]"
    assert list(result.fields) == ["id", "email"]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("Record(Literal[1, 2], int)", "keys must be strings, got 1"),
        ('Record("da", int)', "keys must be a Literal"),
        ("Record([], int)", "at least one key is required"),
        ('Record(["a"], lambda key: int)', "Record's value must be a type"),
        ('Record(["a"], make_type)', "Record's value must be a type"),
        ('Record(["a"], "Missing")', "can't read Missing as a type"),
    ],
)
def test_invalid_records_are_shape_errors(
    tmp_path: Path, expression: str, message: str
) -> None:
    with pytest.raises(ShapeError, match=message):
        shape(tmp_path, "\ndef make_type(key): return int\n", expression)


def test_decorators_and_helpers_do_nothing_at_runtime() -> None:
    # Given
    class User(BaseModel):
        id: int

    # When
    @assert_shape(Partial(Omit(object(), "typo")), exact=False)  # type: ignore[arg-type]
    @assert_field("id", Pick(User, "missing"), "id")
    class Bad(BaseModel):
        id: str

    # Then
    assert isinstance(Partial(User), Shape)
    assert list(Bad.model_fields) == ["id"]
