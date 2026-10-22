"""Shape expressions: `Partial(User)`, `Pick(User, ["id", "name"])`, `Record(...)`, ...

They are markers for `py-extended-types check`, which reads them from the source without
running anything. At runtime each call only returns an opaque `Shape`, so decorating a class
costs nothing and never reads its fields.
"""

from __future__ import annotations

import typing
from collections.abc import Sequence
from typing import Any


class Shape:
    """A shape expression. Opaque at runtime: `py-extended-types check` reads it statically."""

    __slots__ = ()


ShapeLike = typing.Union[type, Shape]  # noqa: UP007


def Partial(target: ShapeLike, /, *, nullable: bool = True) -> Shape:  # noqa: N802
    """Every field optional, like TypeScript's `Partial<T>`.

    Args:
        nullable: also widen each type with `| None` and default it to `None` (the usual
            Pydantic idiom `field: T | None = None`). Pass `False` to keep the types.
    """
    return Shape()


def Required(target: ShapeLike, /) -> Shape:  # noqa: N802
    """Every field required and non-nullable: `T | None` becomes `T`."""
    return Shape()


def Pick(target: ShapeLike, keys: str | Sequence[str], /) -> Shape:  # noqa: N802
    """Only the given fields: one name, or a list of them. Unknown keys are an error."""
    return Shape()


def Omit(target: ShapeLike, keys: str | Sequence[str], /) -> Shape:  # noqa: N802
    """Every field but the given ones: one name, or a list of them. Unknown keys are an error."""
    return Shape()


class KeyOf:
    """The field names of a class or shape, like TypeScript's `keyof T`, for `Record`'s keys."""

    __slots__ = ()

    def __init__(self, target: ShapeLike, /) -> None:
        pass


def Record(  # noqa: N802
    keys: Any, value: Any, /, *, name: str | None = None
) -> Shape:
    """A required field of type `value` for each key, like TypeScript's `Record<K, V>`.

    Args:
        keys: a `Literal["a", "b"]` (or a union of `Literal`s), `KeyOf(T)`, or a list of
            names.
        value: every field's type.
        name: how the shape is written in messages; defaults to `Record[<keys>, <value>]`.
    """
    return Shape()
