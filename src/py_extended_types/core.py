"""The decorators. Both are dev-time contracts that `py-extended-types check` reads from
the source; at runtime they return the class untouched."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from py_extended_types.shapes import ShapeLike

_C = TypeVar("_C", bound=type)


def _unchanged(cls: _C) -> _C:
    return cls


def assert_shape(target: ShapeLike, /, *, exact: bool = True) -> Callable[[_C], _C]:
    """Declare that the decorated class has exactly the fields of `target`.

    Every field must exist on both sides with an equivalent type (`Optional[str]`
    and `str | None` are the same; `Literal["a"]` and `str` are not) and the same
    required-ness (a default on one side means a default on the other).

    Args:
        target: a class, or a shape such as `Partial(User)` / `Omit(User, "id")`.
        exact: `True` (default) rejects fields the target doesn't declare;
            `False` permits them.
    """
    return _unchanged


def assert_field(field: str, target: ShapeLike, key: str, /) -> Callable[[_C], _C]:
    """Declare that the decorated class's `field` has the type of `target`'s field `key`.

    Like TypeScript's `field: T["key"]`: only the type is compared, with the same
    equivalence as `@assert_shape`, so the field may be required or have a default
    whatever the source does. Stack one decorator per field.

    Args:
        field: the name of a field declared on the decorated class.
        target: a class, or a shape such as `Partial(User)`.
        key: the name of the field in `target` to match.
    """
    return _unchanged
