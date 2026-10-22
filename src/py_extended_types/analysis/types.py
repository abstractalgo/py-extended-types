"""Comparing, transforming and printing field types, as mypy types."""

from __future__ import annotations

import ast

from mypy.messages import format_type_bare
from mypy.nodes import TypeAlias, TypeInfo, type_aliases
from mypy.options import Options
from mypy.subtypes import is_subtype
from mypy.types import (
    AnyType,
    LiteralType,
    NoneType,
    ProperType,
    TupleType,
    Type,
    TypeAliasType,
    TypeOfAny,
    TypeType,
    UnionType,
    get_proper_type,
)

from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.project import Project

_FORMAT_OPTIONS = Options()

_ANY = frozenset({"typing.Any", "typing_extensions.Any"})
_OPTIONAL = frozenset({"typing.Optional", "typing_extensions.Optional"})
_UNION = frozenset({"typing.Union", "typing_extensions.Union"})
_LITERAL = frozenset({"typing.Literal", "typing_extensions.Literal"})
_ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})
_TUPLE = frozenset({"builtins.tuple", "typing.Tuple"})
_TYPE = frozenset({"builtins.type", "typing.Type"})


def is_equivalent(left: Type, right: Type) -> bool:
    """Whether each type is assignable to the other, as mypy decides it."""
    return is_subtype(left, right) and is_subtype(right, left)


def _members(tp: Type) -> list[Type]:
    proper = get_proper_type(tp)
    if isinstance(proper, UnionType):
        return [member for item in proper.items for member in _members(item)]
    return [tp]


def _is_none(tp: Type) -> bool:
    return isinstance(get_proper_type(tp), NoneType)


def includes_none(tp: Type) -> bool:
    return any(_is_none(member) for member in _members(tp))


def add_none(tp: Type) -> Type:
    if includes_none(tp):
        return tp
    return UnionType.make_union([*_members(tp), NoneType()])


def remove_none(tp: Type) -> Type:
    """`T | None` -> `T`; a bare `None` is left alone rather than becoming `Never`."""
    kept = [member for member in _members(tp) if not _is_none(member)]
    if not kept or len(kept) == len(_members(tp)):
        return tp
    return UnionType.make_union(kept)


def format_type(tp: Type) -> str:
    return format_type_bare(tp, _FORMAT_OPTIONS)


def is_unresolved(tp: Type) -> bool:
    """Whether `tp` stands for a name mypy couldn't resolve, e.g. an undefined class.

    mypy types an annotation naming an undefined class as a "special form" `Any`.
    """
    proper = get_proper_type(tp)
    return isinstance(proper, AnyType) and proper.type_of_any in (
        TypeOfAny.from_error,
        TypeOfAny.special_form,
    )


def type_from_expr(project: Project, module: str, node: ast.expr) -> Type:
    """The type a type expression in `module` (e.g. `list[int] | None`) stands for."""
    if isinstance(node, ast.Constant):
        if node.value is None:
            return NoneType()
        if isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError as error:
                raise _not_a_type(node) from error
            return type_from_expr(project, module, parsed)
        raise _not_a_type(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return UnionType.make_union(
            [
                *_members(type_from_expr(project, module, node.left)),
                *_members(type_from_expr(project, module, node.right)),
            ]
        )
    if isinstance(node, ast.Subscript):
        return _subscripted(project, module, node)
    resolved = project.resolve(module, node)
    if resolved is None:
        raise _not_a_type(node)
    fullname = type_aliases.get(resolved.fullname, resolved.fullname)
    if fullname in _ANY:
        return AnyType(TypeOfAny.explicit)
    if isinstance(resolved, TypeAlias):
        if resolved.alias_tvars:
            raise ShapeError(
                f"can't use the generic alias {ast.unparse(node)} without arguments"
            )
        return TypeAliasType(resolved, [])
    info = _type_info(project, fullname, resolved)
    if info is None:
        raise _not_a_type(node)
    return project.named_type(
        info.fullname, [AnyType(TypeOfAny.special_form)] * len(info.type_vars)
    )


def _subscripted(project: Project, module: str, node: ast.Subscript) -> Type:
    resolved = project.resolve(module, node.value)
    if resolved is None:
        raise _not_a_type(node)
    fullname = type_aliases.get(resolved.fullname, resolved.fullname)
    elements = (
        list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
    )
    if fullname in _LITERAL:
        return UnionType.make_union(
            [_literal(project, element) for element in elements]
        )
    if fullname in _ANNOTATED:
        return type_from_expr(project, module, elements[0])
    if fullname in _OPTIONAL:
        return add_none(type_from_expr(project, module, elements[0]))
    if fullname in _UNION:
        return UnionType.make_union(
            [type_from_expr(project, module, element) for element in elements]
        )
    if fullname in _TUPLE:
        if (
            len(elements) == 2
            and isinstance(elements[1], ast.Constant)
            and elements[1].value is Ellipsis
        ):
            item = type_from_expr(project, module, elements[0])
            return project.named_type("builtins.tuple", [item])
        items = [type_from_expr(project, module, element) for element in elements]
        return TupleType(
            items,
            project.named_type("builtins.tuple", [AnyType(TypeOfAny.special_form)]),
        )
    if fullname in _TYPE:
        item = get_proper_type(type_from_expr(project, module, elements[0]))
        return TypeType.make_normalized(item)
    args = [type_from_expr(project, module, element) for element in elements]
    if isinstance(resolved, TypeAlias):
        if len(args) != len(resolved.alias_tvars):
            raise _not_a_type(node)
        return TypeAliasType(resolved, args)
    info = _type_info(project, fullname, resolved)
    if info is None or len(args) != len(info.type_vars):
        raise _not_a_type(node)
    return project.named_type(info.fullname, args)


def _type_info(project: Project, fullname: str, resolved: object) -> TypeInfo | None:
    if isinstance(resolved, TypeInfo) and resolved.fullname == fullname:
        return resolved
    try:
        return project.named_type(fullname).type
    except KeyError:
        return None


def _literal(project: Project, node: ast.expr) -> ProperType:
    value: object
    if isinstance(node, ast.Constant):
        value = node.value
    elif (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        value = -node.operand.value
    else:
        raise _not_a_type(node)
    if value is None:
        return NoneType()
    for kind, fallback in (
        (bool, "builtins.bool"),
        (int, "builtins.int"),
        (str, "builtins.str"),
    ):
        if isinstance(value, kind):
            return LiteralType(value, project.named_type(fallback))
    raise _not_a_type(node)


def _not_a_type(node: ast.expr) -> ShapeError:
    return ShapeError(f"can't read {ast.unparse(node)} as a type")
