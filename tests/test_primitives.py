import dataclasses
from typing import Final, Literal, NamedTuple, NotRequired, Protocol, TypedDict

import attrs
import pytest
from pydantic import BaseModel, Field
from typing_extensions import ReadOnly

from py_extended_types import (
    HAS_DEFAULT,
    NO_DEFAULT,
    Factory,
    Filter,
    KeyOf,
    Map,
    Member,
    Omit,
    Partial,
    Pick,
    Record,
    ShapeError,
    add_none,
    assert_shape,
    core,
    is_assignable,
)
from py_extended_types.core import Registration, check, registrations


class User(BaseModel):
    id: int
    name: str = Field(description="Display name", max_length=50)
    email: str | None = None
    tags: list[str] = Field(default_factory=list)
    secret: str = Field("", exclude=True)
    created_at: Final[str]  # type: ignore[misc]  # Pydantic: a frozen field


def _problems(cls: type) -> list[str]:
    registration: Registration = next(r for r in registrations() if r.cls is cls)
    return [f"{problem.rule}: {problem.message}" for problem in check(registration)]


def test_members_of_a_pydantic_model() -> None:
    # When
    fields = Map(User, lambda member: member).fields

    # Then
    assert fields["id"] == Member("id", int)
    assert fields["name"].default is NO_DEFAULT
    assert dict(fields["name"].field) == {
        "description": "Display name",
        "max_length": 50,
    }
    assert fields["email"].default is None
    assert fields["tags"].default == Factory(list)
    assert fields["secret"].field["exclude"] is True
    assert fields["created_at"].quals == {"Final"}
    assert fields["created_at"].annotation is str


def test_members_of_dataclasses_and_typeddicts() -> None:
    # Given
    @dataclasses.dataclass
    class Point:
        x: Final[int]  # type: ignore[misc]  # dataclasses: set once by __init__
        y: int = 0
        labels: list[str] = dataclasses.field(default_factory=list)

    class Movie(TypedDict):
        title: ReadOnly[str]
        year: NotRequired[int]

    # When
    point = Map(Point, lambda member: member).fields
    movie = Map(Movie, lambda member: member).fields

    # Then
    assert point["x"] == Member("x", int, frozenset({"Final"}))
    assert point["y"].default == 0
    assert point["labels"].default == Factory(list)
    assert movie["title"] == Member("title", str, frozenset({"ReadOnly"}))
    assert movie["year"] == Member("year", int, default=HAS_DEFAULT)


def test_map_transforms_every_field_in_order() -> None:
    # Given
    shape = Map(
        User,
        lambda m: m.replace(annotation=add_none(m.annotation)),
        name="Nullable[User]",
    )

    # When
    fields = shape.fields

    # Then
    assert repr(shape) == "Nullable[User]"
    assert list(fields) == ["id", "name", "email", "tags", "secret", "created_at"]
    assert fields["id"].annotation == int | None
    assert fields["id"].required


def test_filter_keeps_matching_fields() -> None:
    # Given
    strings = Filter(User, lambda m: is_assignable(m.annotation, str))
    public = Filter(User, lambda m: not m.field.get("exclude"), name="Public[User]")
    mutable = Filter(User, lambda m: "Final" not in m.quals)

    # Then
    assert repr(strings) == "Filter[User, <lambda>]"
    assert list(strings.fields) == ["name", "secret", "created_at"]
    assert "secret" not in public.fields
    assert "created_at" not in mutable.fields


def test_custom_helpers_compose_with_builtin_ones() -> None:
    @assert_shape(
        Partial(
            Filter(User, lambda m: m.field.get("exclude") is not True, name="Public")
        )
    )
    class UserPatch(BaseModel):
        id: int | None = None
        name: str | None = None
        email: str | None = None
        tags: list[str] | None = None
        created_at: str | None = None

    assert _problems(UserPatch) == []


def test_map_cannot_rename_fields() -> None:
    # Given
    shape = Map(User, lambda m: Member(m.name.upper(), m.annotation))

    # When / Then
    with pytest.raises(ShapeError, match="can't rename field 'id' to 'ID'"):
        _ = shape.fields


def test_errors_in_helper_functions_are_shape_errors() -> None:
    @assert_shape(Filter(User, lambda m: m.field["exclude"], name="Broken"))
    class Anything(BaseModel):
        pass

    assert _problems(Anything) == [
        "invalid-shape: Broken: KeyError for field 'id': 'exclude'"
    ]


def test_shapes_do_no_work_outside_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(core, "_collecting", False)
    calls: list[str] = []

    def spy(member: Member) -> bool:
        calls.append(member.name)
        return True

    # When
    @assert_shape(Partial(Filter(Map(object(), lambda m: m), spy)))  # type: ignore[arg-type]
    class Unchecked(BaseModel):
        id: int

    # Then
    assert calls == []
    assert list(Unchecked.model_fields) == ["id"]


def test_record_gives_every_key_the_same_type() -> None:
    # Given
    shape = Record(Literal["da", "ne"], float)

    # When
    fields = shape.fields

    # Then
    assert repr(shape) == "Record['da' | 'ne', float]"
    assert fields == {"da": Member("da", float), "ne": Member("ne", float)}


def test_record_value_can_depend_on_the_key() -> None:
    # Given
    shape = Record(
        ["id", "count", "label"],
        lambda key: str if key == "label" else int,
    )

    # Then
    assert repr(shape) == "Record['id' | 'count' | 'label', <lambda>]"
    assert {name: m.annotation for name, m in shape.fields.items()} == {
        "id": int,
        "count": int,
        "label": str,
    }


def test_record_accepts_unions_of_literals_and_generic_values() -> None:
    # Given
    shape = Record(Literal["a"] | Literal["b"], list[int])

    # Then
    assert repr(shape) == "Record['a' | 'b', list[int]]"
    assert [m.annotation for m in shape.fields.values()] == [list[int], list[int]]


def test_record_checks_a_class() -> None:
    @assert_shape(Partial(Record(Literal["da", "ne"], float)))
    class Scores(BaseModel):
        da: float | None = None
        ne: int | None = None

    assert _problems(Scores) == [
        "type-mismatch: Scores.ne has type 'int | None', expected 'float | None'"
    ]


@pytest.mark.parametrize(
    ("keys", "value", "message"),
    [
        (Literal[1, 2], int, "keys must be strings, got 1"),
        ("da", int, "keys must be a Literal"),
        ([], int, "at least one key is required"),
        (["a"], lambda key: {}[key], "KeyError for key 'a'"),
    ],
)
def test_invalid_records_are_shape_errors(
    keys: object, value: object, message: str
) -> None:
    # Given
    shape = Record(keys, value)

    # When / Then
    with pytest.raises(ShapeError, match=message):
        _ = shape.fields


def test_key_of_lists_a_shapes_field_names() -> None:
    # Given
    keys = KeyOf(Omit(User, "secret", "created_at"))

    # Then
    assert repr(keys) == "KeyOf[Omit[User, 'secret' | 'created_at']]"
    assert keys.names == ("id", "name", "email", "tags")
    assert keys.literal == Literal["id", "name", "email", "tags"]


def test_record_over_key_of_another_class() -> None:
    # Given
    shape = Record(
        KeyOf(Pick(User, "id", "email")), lambda key: list[str] if key == "id" else str
    )

    # Then
    assert repr(shape) == "Record[KeyOf[Pick[User, 'id' | 'email']], <lambda>]"
    assert {name: m.annotation for name, m in shape.fields.items()} == {
        "id": list[str],
        "email": str,
    }


def test_key_of_reads_nothing_until_checked() -> None:
    # Given
    calls: list[str] = []

    def spy(member: Member) -> bool:
        calls.append(member.name)
        return member.name != "typo"

    # When
    shape = Record(KeyOf(Filter(Omit(User, "typo"), spy)), str)

    # Then
    assert calls == []
    with pytest.raises(ShapeError, match="unknown field"):
        _ = shape.fields


def test_members_of_namedtuples_and_attrs_classes() -> None:
    # Given
    class Pair(NamedTuple):
        left: int
        right: str = ""

    @attrs.define
    class Item:
        sku: str
        count: int = 0
        tags: list[str] = attrs.Factory(list)

    # When
    pair = Map(Pair, lambda member: member).fields
    item = Map(Item, lambda member: member).fields

    # Then
    assert pair == {
        "left": Member("left", int),
        "right": Member("right", str, default=""),
    }
    assert item == {
        "sku": Member("sku", str),
        "count": Member("count", int, default=0),
        "tags": Member("tags", list[str], default=Factory(list)),
    }


class Plain:
    id: int
    name: str = ""


class Readable(Protocol):
    id: int


@pytest.mark.parametrize("target", [Plain, Readable])
def test_unsupported_classes_are_shape_errors(target: type) -> None:
    # Given
    shape = Partial(target)

    # When / Then
    with pytest.raises(
        ShapeError,
        match=f"can't read the fields of {target.__qualname__}: .*supports Pydantic",
    ):
        _ = shape.fields


def test_unsupported_decorated_class_is_reported() -> None:
    @assert_shape(Pick(User, "id"))
    class Untyped:
        id: int

    assert _problems(Untyped) == [
        "invalid-shape: can't read the fields of "
        "test_unsupported_decorated_class_is_reported.<locals>.Untyped: py-extended-types supports "
        "Pydantic models, dataclasses, TypedDicts, NamedTuples and attrs classes"
    ]


def test_unresolvable_annotations_are_shape_errors() -> None:
    # Given
    @dataclasses.dataclass
    class Forward:
        other: "Missing"  # type: ignore[name-defined]  # noqa: F821

    # When / Then
    with pytest.raises(
        ShapeError, match="can't resolve the annotations of .*Forward: NameError"
    ):
        _ = Partial(Forward).fields
