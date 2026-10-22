"""Evaluating shape expressions such as `Partial(Omit(User, ["id"]))` from source.

Only what can be read without running code is accepted: classes, the built-in helpers,
literal arguments, and module-level names bound to any of those (`Public = Omit(User,
"password")`, `KEYS = ["id", "name"]`). Anything else is an `invalid-shape` problem.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Callable
from typing import Any

from mypy.nodes import Decorator, FuncBase, TypeInfo, Var

from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.fields import (
    HAS_DEFAULT,
    NO_DEFAULT,
    Member,
    class_name,
    fields_of,
)
from py_extended_types.analysis.project import Project
from py_extended_types.analysis.types import (
    add_none,
    format_type,
    remove_none,
    type_from_expr,
)

_PACKAGE = "py_extended_types.shapes"
PARTIAL = f"{_PACKAGE}.Partial"
REQUIRED = f"{_PACKAGE}.Required"
PICK = f"{_PACKAGE}.Pick"
OMIT = f"{_PACKAGE}.Omit"
RECORD = f"{_PACKAGE}.Record"
KEY_OF = f"{_PACKAGE}.KeyOf"
_LITERAL = frozenset({"typing.Literal", "typing_extensions.Literal"})


@dataclasses.dataclass(frozen=True)
class Shape:
    """A named, ordered set of fields."""

    name: str
    fields: dict[str, Member]

    def __repr__(self) -> str:
        return self.name


@dataclasses.dataclass(frozen=True)
class Scope:
    """Where an expression is evaluated: a module, and the names being evaluated (for cycles).

    Names the module doesn't define are looked up in `fallback`, if given (`show` uses
    `py_extended_types`, so its expressions can use helpers the module doesn't import).
    """

    project: Project
    module: str
    evaluating: frozenset[str] = frozenset()
    fallback: str | None = None

    def resolve(self, node: ast.expr) -> Any:
        found = self.project.resolve(self.module, node)
        if found is None and self.fallback is not None:
            found = self.project.resolve(self.fallback, node)
        return found

    def fullname(self, node: ast.expr) -> str | None:
        found = self.resolve(node)
        return found.fullname if found is not None else None


def evaluate(scope: Scope, node: ast.expr) -> Shape:
    """The shape `node` describes: a class, a helper call, or a name bound to either."""
    if isinstance(node, ast.Call):
        helper = _HELPERS.get(scope.fullname(node.func) or "")
        if helper is not None:
            return helper(scope, node)
        if scope.fullname(node.func) == KEY_OF:
            raise ShapeError("KeyOf(...) lists field names; use it as Record's keys")
        raise _unreadable(node, "a class or a shape")
    resolved = scope.resolve(node)
    if isinstance(resolved, TypeInfo):
        return Shape(class_name(resolved), fields_of(scope.project, resolved))
    bound = _bound_value(scope, node, resolved)
    if bound is not None:
        return evaluate(*bound)
    raise _unreadable(node, "a class or a shape")


def constant(scope: Scope, node: ast.expr) -> Any:
    """A literal value (`"id"`, `["id", "name"]`, `True`), or a name bound to one."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(constant(scope, element) for element in node.elts)
    bound = _bound_value(scope, node, scope.resolve(node))
    if bound is not None:
        return constant(*bound)
    raise _unreadable(node, "a literal value")


def _bound_value(
    scope: Scope, node: ast.expr, resolved: Any
) -> tuple[Scope, ast.expr] | None:
    """For a module-level variable, the expression last assigned to it, in its module."""
    if not isinstance(resolved, Var) or resolved.fullname in scope.evaluating:
        return None
    module, _, name = resolved.fullname.rpartition(".")
    if scope.project.module(module) is None:
        return None
    value = None
    for statement in scope.project.tree(module).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            value = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            value = statement.value
    if value is None:
        return None
    return Scope(scope.project, module, scope.evaluating | {resolved.fullname}), value


def _unreadable(node: ast.expr, expected: str) -> ShapeError:
    return ShapeError(
        f"can't read {ast.unparse(node)} statically: expected {expected}, "
        "without calling any function but py-extended-types's helpers"
    )


@dataclasses.dataclass(frozen=True)
class _Arguments:
    positional: list[ast.expr]
    keywords: dict[str, ast.expr]


def arguments(
    name: str, call: ast.Call, positional: int, keywords: frozenset[str] = frozenset()
) -> _Arguments:
    """`call`'s arguments, checked against what `name` accepts."""
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        raise ShapeError(f"{name}(): can't read *args or **kwargs statically")
    if len(call.args) != positional:
        raise ShapeError(
            f"{name}() takes {positional} positional argument{'s' if positional != 1 else ''}, "
            f"got {len(call.args)}"
        )
    unknown = [k.arg for k in call.keywords if k.arg not in keywords]
    if unknown:
        raise ShapeError(f"{name}() got an unexpected keyword argument '{unknown[0]}'")
    return _Arguments(
        list(call.args), {k.arg: k.value for k in call.keywords if k.arg is not None}
    )


def _flag(scope: Scope, name: str, value: ast.expr | None, default: bool) -> bool:
    if value is None:
        return default
    flag = constant(scope, value)
    if not isinstance(flag, bool):
        raise ShapeError(f"{name} must be True or False, got {ast.unparse(value)}")
    return flag


def _partial(scope: Scope, call: ast.Call) -> Shape:
    args = arguments("Partial", call, 1, frozenset({"nullable"}))
    base = evaluate(scope, args.positional[0])
    nullable = _flag(scope, "nullable", args.keywords.get("nullable"), True)
    suffix = "" if nullable else ", nullable=False"

    def optional(member: Member) -> Member:
        if nullable:
            return member.replace(annotation=add_none(member.annotation), default=None)
        return member if not member.required else member.replace(default=HAS_DEFAULT)

    return Shape(
        f"Partial[{base!r}{suffix}]",
        {name: optional(member) for name, member in base.fields.items()},
    )


def _required(scope: Scope, call: ast.Call) -> Shape:
    base = evaluate(scope, arguments("Required", call, 1).positional[0])
    return Shape(
        f"Required[{base!r}]",
        {
            name: member.replace(
                annotation=remove_none(member.annotation), default=NO_DEFAULT
            )
            for name, member in base.fields.items()
        },
    )


def _pick(scope: Scope, call: ast.Call) -> Shape:
    return _select(scope, call, "Pick", keep=True)


def _omit(scope: Scope, call: ast.Call) -> Shape:
    return _select(scope, call, "Omit", keep=False)


def _select(scope: Scope, call: ast.Call, helper: str, *, keep: bool) -> Shape:
    target, keys_node = arguments(helper, call, 2).positional
    base = evaluate(scope, target)
    keys = constant(scope, keys_node)
    keys = (keys,) if not isinstance(keys, tuple) else keys
    name = f"{helper}[{base!r}, {_format_keys(keys)}]"
    if not keys:
        raise ShapeError(f"{name}: at least one key is required")
    invalid = [key for key in keys if not isinstance(key, str)]
    if invalid:
        raise ShapeError(f"{name}: keys must be strings, got {invalid[0]!r}")
    unknown = [key for key in keys if key not in base.fields]
    if unknown:
        raise ShapeError(
            f"{name}: unknown field(s) {_format_keys(tuple(unknown))}; "
            f"{base!r} has {_format_keys(tuple(base.fields))}"
        )
    return Shape(
        name,
        {
            field: member
            for field, member in base.fields.items()
            if (field in keys) == keep
        },
    )


def _record(scope: Scope, call: ast.Call) -> Shape:
    args = arguments("Record", call, 2, frozenset({"name"}))
    keys_node, value_node = args.positional
    keys, described = _record_keys(scope, keys_node)
    if isinstance(value_node, ast.Lambda) or isinstance(
        scope.resolve(value_node), (FuncBase, Decorator)
    ):
        raise ShapeError(
            f"Record's value must be a type, got {ast.unparse(value_node)}: "
            "py-extended-types doesn't call functions"
        )
    value = type_from_expr(scope.project, scope.module, value_node)
    custom = args.keywords.get("name")
    name = constant(scope, custom) if custom is not None else None
    if name is not None and not isinstance(name, str):
        raise ShapeError(f"Record's name must be a string, got {ast.unparse(custom)}")  # type: ignore[arg-type]
    label = name or f"Record[{described}, {format_type(value)}]"
    if not keys:
        raise ShapeError(f"{label}: at least one key is required")
    invalid = [key for key in keys if not isinstance(key, str)]
    if invalid:
        raise ShapeError(f"{label}: keys must be strings, got {invalid[0]!r}")
    return Shape(label, {key: Member(key, value) for key in dict.fromkeys(keys)})


def _record_keys(scope: Scope, node: ast.expr) -> tuple[list[Any], str]:
    """Record's keys and how they're written in its name."""
    if isinstance(node, ast.Call) and scope.fullname(node.func) == KEY_OF:
        target = evaluate(scope, arguments("KeyOf", node, 1).positional[0])
        return list(target.fields), f"KeyOf[{target!r}]"
    literal = _literal_values(scope, node)
    if literal is not None:
        return literal, " | ".join(repr(key) for key in literal)
    keys = constant(scope, node)
    if not isinstance(keys, tuple):
        raise ShapeError(
            f"Record's keys must be a Literal[...], KeyOf(...) or a list of names, got {keys!r}"
        )
    return list(keys), " | ".join(repr(key) for key in keys)


def _literal_values(scope: Scope, node: ast.expr) -> list[Any] | None:
    """The values of `Literal["a", "b"]` or `Literal["a"] | Literal["b"]`."""
    if isinstance(node, ast.Subscript) and scope.fullname(node.value) in _LITERAL:
        elements = (
            node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        )
        return [constant(scope, element) for element in elements]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _literal_values(scope, node.left)
        right = _literal_values(scope, node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _format_keys(keys: tuple[Any, ...]) -> str:
    return " | ".join(repr(key) for key in keys)


_HELPERS: dict[str, Callable[[Scope, ast.Call], Shape]] = {
    PARTIAL: _partial,
    REQUIRED: _required,
    PICK: _pick,
    OMIT: _omit,
    RECORD: _record,
}
