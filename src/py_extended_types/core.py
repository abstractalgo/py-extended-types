from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from typing import TypeVar

from py_extended_types.shapes import (
    Member,
    Shape,
    ShapeError,
    ShapeLike,
    as_shape,
    fields_of,
)
from py_extended_types.typecheck import format_type, is_equivalent

_C = TypeVar("_C", bound=type)


@dataclasses.dataclass(frozen=True)
class Registration:
    cls: type
    shape: Shape
    exact: bool
    filename: str
    lineno: int


@dataclasses.dataclass(frozen=True)
class FieldRegistration:
    cls: type
    field: str
    shape: Shape
    key: str
    filename: str
    lineno: int

    @property
    def source(self) -> str:
        """How the source field is written in messages: `User['name']`."""
        return f"{self.shape!r}[{self.key!r}]"


Declaration = Registration | FieldRegistration


@dataclasses.dataclass(frozen=True)
class Problem:
    """One mismatch.

    `field` names the field it is about (it may be absent from the class, for
    `missing-field`); `expected` is that field in the shape, when it has one.
    """

    rule: str
    message: str
    field: str | None = None
    expected: Member | None = None


_REGISTRY: list[Declaration] = []
# Off in the running app, so `@assert_shape` / `@assert_field` cost nothing there; `py-extended-types check` turns it on
# before importing anything.
_collecting = False


def start_collecting() -> None:
    """Make `@assert_shape` / `@assert_field` record declarations from now on (the CLI calls this)."""
    global _collecting
    _collecting = True


def registrations() -> list[Declaration]:
    return list(_REGISTRY)


def _unchanged(cls: _C) -> _C:
    return cls


def assert_shape(target: ShapeLike, /, *, exact: bool = True) -> Callable[[_C], _C]:
    """Declare that the decorated class has exactly the fields of `target`.

    Every field must exist on both sides with an equivalent type (`Optional[str]`
    and `str | None` are the same; `Literal["a"]` and `str` are not) and the same
    required-ness (a default on one side means a default on the other).

    This is a dev-time contract: in the running app the decorator returns the class
    untouched and records nothing. Only `py-extended-types check` collects and verifies it.

    Args:
        target: a class, or a shape such as `Partial(User)` / `Omit(User, "id")`.
        exact: `True` (default) rejects fields the target doesn't declare;
            `False` permits them.
    """
    if not _collecting:
        return _unchanged
    expected = as_shape(target)
    caller = sys._getframe(1)
    filename = caller.f_code.co_filename
    lineno = caller.f_lineno

    def decorate(cls: _C) -> _C:
        _REGISTRY.append(Registration(cls, expected, exact, filename, lineno))
        return cls

    return decorate


def assert_field(field: str, target: ShapeLike, key: str, /) -> Callable[[_C], _C]:
    """Declare that the decorated class's `field` has the type of `target`'s field `key`.

    Like TypeScript's `field: T["key"]`: only the type is compared, with the same
    equivalence as `@assert_shape`, so the field may be required or have a default
    whatever the source does. Stack one decorator per field.

    This is a dev-time contract: in the running app the decorator returns the class
    untouched and records nothing. Only `py-extended-types check` collects and verifies it.

    Args:
        field: the name of a field declared on the decorated class.
        target: a class, or a shape such as `Partial(User)`.
        key: the name of the field in `target` to match.
    """
    if not _collecting:
        return _unchanged
    shape = as_shape(target)
    caller = sys._getframe(1)
    filename = caller.f_code.co_filename
    lineno = caller.f_lineno

    def decorate(cls: _C) -> _C:
        _REGISTRY.append(FieldRegistration(cls, field, shape, key, filename, lineno))
        return cls

    return decorate


def check(registration: Declaration) -> list[Problem]:
    """Return the reasons the class doesn't match its declaration (empty if it does)."""
    if isinstance(registration, FieldRegistration):
        return _check_one_field(registration)
    cls_name = registration.cls.__name__
    target = repr(registration.shape)
    try:
        expected = registration.shape.fields
    except ShapeError as error:
        return [Problem("invalid-shape", str(error))]
    try:
        actual = fields_of(registration.cls)
    except ShapeError as error:
        return [Problem("invalid-shape", str(error))]
    problems: list[Problem] = []

    for name, want in expected.items():
        have = actual.get(name)
        if have is None:
            kind = "required" if want.required else "optional"
            problems.append(
                Problem(
                    "missing-field",
                    f"{cls_name} is missing {kind} field '{name}: {format_type(want.annotation)}' from {target}",
                    field=name,
                    expected=want,
                )
            )
        else:
            problems.extend(
                _check_field(f"{cls_name}.{name}", target, name, have, want)
            )

    if registration.exact:
        for name in actual:
            if name not in expected:
                problems.append(
                    Problem(
                        "unexpected-field",
                        f"{cls_name}.{name} is not in {target}",
                        field=name,
                    )
                )

    return problems


def _check_field(
    qualified: str, target: str, name: str, have: Member, want: Member
) -> list[Problem]:
    problems: list[Problem] = []
    if not is_equivalent(have.annotation, want.annotation):
        problems.append(
            Problem(
                "type-mismatch",
                f"{qualified} has type '{format_type(have.annotation)}', expected '{format_type(want.annotation)}'",
                field=name,
                expected=want,
            )
        )
    if want.required and not have.required:
        problems.append(
            Problem(
                "required-mismatch",
                f"{qualified} is required in {target} and must not have a default",
                field=name,
                expected=want,
            )
        )
    elif have.required and not want.required:
        problems.append(
            Problem(
                "required-mismatch",
                f"{qualified} is optional in {target} and needs a default",
                field=name,
                expected=want,
            )
        )
    return problems


def _check_one_field(registration: FieldRegistration) -> list[Problem]:
    cls_name = registration.cls.__name__
    name = registration.field
    source = registration.source
    try:
        want = _source_field(registration)
        actual = fields_of(registration.cls)
    except ShapeError as error:
        return [Problem("invalid-shape", str(error))]

    have = actual.get(name)
    if have is None:
        return [
            Problem(
                "missing-field",
                f"{cls_name} is missing field '{name}: {format_type(want.annotation)}' from {source}",
                field=name,
                expected=want,
            )
        ]
    if is_equivalent(have.annotation, want.annotation):
        return []
    return [
        Problem(
            "type-mismatch",
            f"{cls_name}.{name} has type '{format_type(have.annotation)}', expected '{format_type(want.annotation)}' from {source}",
            field=name,
            # Only the type is part of the contract, so the fix keeps this field's default.
            expected=have.replace(annotation=want.annotation),
        )
    ]


def _source_field(registration: FieldRegistration) -> Member:
    for label, value in (("field", registration.field), ("key", registration.key)):
        if not isinstance(value, str):
            raise ShapeError(
                f"@assert_field: the {label} must be a string, got {value!r}"
            )
    fields = registration.shape.fields
    if registration.key not in fields:
        names = " | ".join(repr(name) for name in fields)
        raise ShapeError(
            f"{registration.source}: unknown field '{registration.key}'; "
            f"{registration.shape!r} has {names}"
        )
    return fields[registration.key]
