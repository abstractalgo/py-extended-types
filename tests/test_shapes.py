from typing import Annotated, Literal, Optional

import pytest
from pydantic import BaseModel, Field

from py_extended_types import (
    Omit,
    Partial,
    Pick,
    Required,
    ShapeError,
    assert_shape,
    core,
)
from py_extended_types.core import Declaration, check, registrations


class User(BaseModel):
    id: int
    name: str
    email: str
    nickname: str | None = None
    tags: list[str] = Field(default_factory=list)


def _problems(cls: type) -> list[str]:
    registration: Declaration = next(r for r in registrations() if r.cls is cls)
    return [f"{problem.rule}: {problem.message}" for problem in check(registration)]


def test_partial_makes_fields_optional_and_nullable() -> None:
    # Given
    shape = Partial(User)

    # When
    fields = shape.fields

    # Then
    assert repr(shape) == "Partial[User]"
    assert all(not spec.required for spec in fields.values())
    assert fields["name"].annotation == str | None
    assert fields["nickname"].annotation == str | None


def test_required_makes_every_field_required_and_non_nullable() -> None:
    # Given
    shape = Required(User)

    # When
    fields = shape.fields

    # Then
    assert all(spec.required for spec in fields.values())
    assert fields["name"].annotation is str
    assert fields["nickname"].annotation is str


def test_required_undoes_partial() -> None:
    assert Required(Partial(User)).fields == Required(User).fields


def test_required_class_matches_without_none() -> None:
    @assert_shape(Required(Pick(User, ["id", "nickname"])))
    class Strict(BaseModel):
        id: int
        nickname: str

    @assert_shape(Required(Pick(User, "nickname")))
    class StillNullable(BaseModel):
        nickname: str | None

    assert _problems(Strict) == []
    assert _problems(StillNullable) == [
        "type-mismatch: StillNullable.nickname has type 'str | None', expected 'str'"
    ]


def test_pick_and_omit_select_fields() -> None:
    assert list(Pick(User, ["id", "name"]).fields) == ["id", "name"]
    assert list(Omit(User, ["id", "tags"]).fields) == ["name", "email", "nickname"]


def test_unknown_key_is_reported_lazily() -> None:
    # Given
    shape = Omit(User, "idd")

    # When / Then
    with pytest.raises(ShapeError, match="unknown field"):
        _ = shape.fields


def test_partial_user_matches() -> None:
    @assert_shape(Partial(User))
    class PartialUser(BaseModel):
        id: int | None = None
        name: str | None = None
        email: Optional[str] = None
        nickname: str | None = None
        tags: list[str] | None = None

    assert _problems(PartialUser) == []


def test_missing_optional_field_is_reported() -> None:
    @assert_shape(Partial(User))
    class UserPatch(BaseModel):
        id: int | None = None
        name: str | None = None
        email: str | None = None
        nickname: str | None = None

    assert _problems(UserPatch) == [
        "missing-field: UserPatch is missing optional field 'tags: list[str] | None' from Partial[User]"
    ]


def test_required_field_where_partial_expects_optional_is_reported() -> None:
    @assert_shape(Partial(Pick(User, ["id", "name"])))
    class Patch(BaseModel):
        id: int | None
        name: str

    assert _problems(Patch) == [
        "required-mismatch: Patch.id is optional in Partial[Pick[User, 'id' | 'name']] and needs a default",
        "type-mismatch: Patch.name has type 'str', expected 'str | None'",
        "required-mismatch: Patch.name is optional in Partial[Pick[User, 'id' | 'name']] and needs a default",
    ]


def test_reports_type_mismatch_missing_and_extra_fields() -> None:
    @assert_shape(Omit(User, "id"))
    class CreateUser(BaseModel):
        name: int
        nickname: str | None = None
        tags: list[str] = []
        password: str

    assert _problems(CreateUser) == [
        "type-mismatch: CreateUser.name has type 'int', expected 'str'",
        "missing-field: CreateUser is missing required field 'email: str' from Omit[User, 'id']",
        "unexpected-field: CreateUser.password is not in Omit[User, 'id']",
    ]


def test_extra_allow_permits_additional_fields() -> None:
    @assert_shape(Pick(User, "id"), exact=False)
    class UserWithMeta(BaseModel):
        id: int
        meta: dict[str, str]

    assert _problems(UserWithMeta) == []


def test_required_field_with_default_is_rejected() -> None:
    @assert_shape(Pick(User, "name"))
    class Named(BaseModel):
        name: str = "anon"

    assert _problems(Named) == [
        "required-mismatch: Named.name is required in Pick[User, 'name'] and must not have a default"
    ]


def test_narrower_types_do_not_match() -> None:
    @assert_shape(Pick(User, ["name", "id"]))
    class Narrow(BaseModel):
        name: Literal["a", "b"]
        id: bool

    assert _problems(Narrow) == [
        "type-mismatch: Narrow.id has type 'bool', expected 'int'",
        "type-mismatch: Narrow.name has type 'Literal['a', 'b']', expected 'str'",
    ]


def test_annotated_metadata_is_ignored() -> None:
    @assert_shape(Pick(User, "name"))
    class Named(BaseModel):
        name: Annotated[str, Field(min_length=1)]

    assert _problems(Named) == []


def test_decorator_is_a_no_op_outside_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(core, "_collecting", False)
    before = len(registrations())

    # When
    @assert_shape(Pick(User, "id"))
    class Bad(BaseModel):
        id: str

    # Then
    assert len(registrations()) == before
    assert list(Bad.model_fields) == ["id"]
