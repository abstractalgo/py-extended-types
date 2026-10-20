"""Structural assignability between runtime type annotations.

This is a pragmatic approximation of what mypy/pyright would accept, good enough
for comparing field annotations of models. Anything it cannot reason about
(TypeVars, unresolved forward refs, callables) is accepted rather than flagged.
"""

from __future__ import annotations

import types
import typing
from typing import Annotated, Any, Literal, TypeVar, Union, get_args, get_origin

NoneType = type(None)

_INVARIANT_ORIGINS: frozenset[type] = frozenset({list, dict, set})
_NUMERIC_PROMOTIONS: frozenset[tuple[type, type]] = frozenset(
    {(int, float), (int, complex), (float, complex)}
)


def normalize(tp: Any) -> Any:
    if tp is None:
        return NoneType
    if get_origin(tp) is Annotated:
        return normalize(get_args(tp)[0])
    return tp


def union_members(tp: Any) -> tuple[Any, ...] | None:
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        return tuple(normalize(arg) for arg in get_args(tp))
    return None


def make_union(members: list[Any]) -> Any:
    unique: list[Any] = []
    for member in members:
        if member not in unique:
            unique.append(member)
    if not unique:
        return typing.Never
    if len(unique) == 1:
        return unique[0]
    return Union[tuple(unique)]  # noqa: UP007 - members are runtime values


def add_none(tp: Any) -> Any:
    tp = normalize(tp)
    members = union_members(tp)
    if members is None:
        members = (tp,)
    if NoneType in members:
        return tp
    return make_union([*members, NoneType])


def remove_none(tp: Any) -> Any:
    """`T | None` -> `T`; a bare `None` is left alone rather than becoming `Never`."""
    tp = normalize(tp)
    members = union_members(tp)
    if members is None:
        return tp
    return make_union([member for member in members if member is not NoneType])


def includes_none(tp: Any) -> bool:
    tp = normalize(tp)
    members = union_members(tp)
    if members is None:
        return tp is NoneType
    return NoneType in members


def is_assignable(source: Any, target: Any) -> bool:  # noqa: C901
    """Whether a value typed `source` may be used where `target` is expected."""
    source = normalize(source)
    target = normalize(target)

    if source is Any or target is Any or target is object:
        return True
    if source == target:
        return True
    if isinstance(source, (TypeVar, str, typing.ForwardRef)) or isinstance(
        target, (TypeVar, str, typing.ForwardRef)
    ):
        return True

    source_members = union_members(source)
    if source_members is not None:
        return all(is_assignable(member, target) for member in source_members)
    target_members = union_members(target)
    if target_members is not None:
        return any(is_assignable(source, member) for member in target_members)

    if get_origin(source) is Literal:
        if get_origin(target) is Literal:
            return set(get_args(source)) <= set(get_args(target))
        return all(is_assignable(type(value), target) for value in get_args(source))
    if get_origin(target) is Literal:
        return False

    return _is_class_assignable(source, target)


def _is_class_assignable(source: Any, target: Any) -> bool:  # noqa: C901
    source_origin = get_origin(source) or source
    target_origin = get_origin(target) or target
    if not isinstance(source_origin, type) or not isinstance(target_origin, type):
        return False

    if (source_origin, target_origin) in _NUMERIC_PROMOTIONS:
        return True
    try:
        if not issubclass(source_origin, target_origin):
            return False
    except TypeError:
        return False

    source_args = get_args(source)
    target_args = get_args(target)
    # A bare generic on either side means "any parameters".
    if not source_args or not target_args:
        return True
    # Parameters of a subclass don't line up positionally with the base's.
    if source_origin is not target_origin:
        return True
    if target_origin is tuple:
        return _is_tuple_assignable(source_args, target_args)
    if len(source_args) != len(target_args):
        return True
    if target_origin in _INVARIANT_ORIGINS:
        return all(
            is_equivalent(s, t) for s, t in zip(source_args, target_args, strict=True)
        )
    return all(
        is_assignable(s, t) for s, t in zip(source_args, target_args, strict=True)
    )


def _is_tuple_assignable(
    source_args: tuple[Any, ...], target_args: tuple[Any, ...]
) -> bool:
    if len(target_args) == 2 and target_args[1] is Ellipsis:
        element = target_args[0]
        if len(source_args) == 2 and source_args[1] is Ellipsis:
            return is_assignable(source_args[0], element)
        return all(is_assignable(arg, element) for arg in source_args)
    if len(source_args) == 2 and source_args[1] is Ellipsis:
        return False
    if len(source_args) != len(target_args):
        return False
    return all(
        is_assignable(s, t) for s, t in zip(source_args, target_args, strict=True)
    )


def is_equivalent(left: Any, right: Any) -> bool:
    return is_assignable(left, right) and is_assignable(right, left)


def format_type(tp: Any) -> str:  # noqa: C901
    tp = normalize(tp)
    if tp is NoneType:
        return "None"
    if tp is Any:
        return "Any"
    if tp is Ellipsis:
        return "..."
    if isinstance(tp, str):
        return tp
    if isinstance(tp, typing.ForwardRef):
        return tp.__forward_arg__
    members = union_members(tp)
    if members is not None:
        return " | ".join(format_type(member) for member in members)
    origin = get_origin(tp)
    if origin is Literal:
        return f"Literal[{', '.join(repr(value) for value in get_args(tp))}]"
    if origin is not None:
        args = get_args(tp)
        name = getattr(origin, "__name__", None) or repr(origin)
        if not args:
            return name
        return f"{name}[{', '.join(format_type(arg) for arg in args)}]"
    if isinstance(tp, type):
        return tp.__qualname__
    return repr(tp).replace("typing.", "")
