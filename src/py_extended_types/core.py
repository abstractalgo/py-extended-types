from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from typing import Literal, TypeVar

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

Extra = Literal["forbid", "allow"]


@dataclasses.dataclass(frozen=True)
class Registration:
    cls: type
    shape: Shape
    extra: Extra
    filename: str
    lineno: int


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


_REGISTRY: list[Registration] = []
# Off in the running app, so `@expect_shape` costs nothing there; `py-extended-types check` turns it on
# before importing anything.
_collecting = False


def start_collecting() -> None:
    """Make `@expect_shape` record declarations from now on (the CLI calls this)."""
    global _collecting
    _collecting = True


def registrations() -> list[Registration]:
    return list(_REGISTRY)


def _unchanged(cls: _C) -> _C:
    return cls


def expect_shape(
    target: ShapeLike, /, *, extra: Extra = "forbid"
) -> Callable[[_C], _C]:
    """Declare that the decorated class has exactly the fields of `target`.

    Every field must exist on both sides with an equivalent type (`Optional[str]`
    and `str | None` are the same; `Literal["a"]` and `str` are not) and the same
    required-ness (a default on one side means a default on the other).

    This is a dev-time contract: in the running app the decorator returns the class
    untouched and records nothing. Only `py-extended-types check` collects and verifies it.

    Args:
        target: a class, or a shape such as `Partial(User)` / `Omit(User, "id")`.
        extra: `"forbid"` (default) rejects fields the target doesn't declare;
            `"allow"` permits them.
    """
    if not _collecting:
        return _unchanged
    expected = as_shape(target)
    caller = sys._getframe(1)
    filename = caller.f_code.co_filename
    lineno = caller.f_lineno

    def decorate(cls: _C) -> _C:
        _REGISTRY.append(Registration(cls, expected, extra, filename, lineno))
        return cls

    return decorate


def check(registration: Registration) -> list[Problem]:
    """Return the reasons the class doesn't match its shape (empty if it does)."""
    cls_name = registration.cls.__name__
    target = repr(registration.shape)
    try:
        expected = registration.shape.fields
    except ShapeError as error:
        return [Problem("invalid-shape", str(error))]
    actual = fields_of(registration.cls)
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

    if registration.extra == "forbid":
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
