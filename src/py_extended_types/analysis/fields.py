"""The fields of a class, read statically.

Names and order come from the class bodies (mypy's cached symbol tables don't keep the
order), types from mypy, and defaults from each kind's own rules: Pydantic's `Field(...)`,
dataclasses' and attrs' metadata as mypy's plugins record it, a TypedDict's required keys,
and class-level values for NamedTuples and plain classes.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Any

from mypy.nodes import TypeInfo, Var
from mypy.types import AnyType, Type, TypeOfAny

from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.project import Project
from py_extended_types.analysis.types import is_unresolved


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


NO_DEFAULT: Any = _Sentinel("NO_DEFAULT")
"""`Member.default` of a required field."""

HAS_DEFAULT: Any = _Sentinel("HAS_DEFAULT")
"""`Member.default` of an optional field whose default isn't a literal."""

_KEEP: Any = _Sentinel("_KEEP")


@dataclasses.dataclass(frozen=True)
class Member:
    """One field: its name, type and default (`NO_DEFAULT`, `HAS_DEFAULT` or a literal value)."""

    name: str
    annotation: Type
    default: Any = NO_DEFAULT

    @property
    def required(self) -> bool:
        return self.default is NO_DEFAULT

    def replace(self, *, annotation: Type = _KEEP, default: Any = _KEEP) -> Member:
        return Member(
            self.name,
            self.annotation if annotation is _KEEP else annotation,
            self.default if default is _KEEP else default,
        )


SUPPORTED_KINDS = "Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs classes and plain classes"

_BASE_MODEL = "pydantic.main.BaseModel"
_PYDANTIC_FIELD = "pydantic.fields.Field"
_FIELD_FUNCTIONS = frozenset(
    {
        "dataclasses.field",
        "attr.ib",
        "attr.attrib",
        "attr.attr",
        "attrs.field",
        "attr._make.attrib",
        "attr._next_gen.field",
    }
)
_ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})


def class_name(info: TypeInfo) -> str:
    """The name a class is written with in messages: `User`, or `Outer.Inner`."""
    return info.fullname[len(info.module_name) + 1 :]


def fields_of(project: Project, info: TypeInfo) -> dict[str, Member]:
    """The fields of a Pydantic model, dataclass, TypedDict, NamedTuple, attrs class or
    plain class.

    Raises:
        ShapeError: for any other class, or one whose annotations can't be resolved.
    """
    if info.fallback_to_any:
        raise ShapeError(
            f"can't read the fields of {class_name(info)}: {_unresolved_base(project, info)} "
            "can't be resolved (is its package installed in the project's environment?)"
        )
    if info.typeddict_type is not None:
        typed_dict = info.typeddict_type
        return _checked(
            info,
            {
                name: Member(
                    name,
                    annotation,
                    NO_DEFAULT if name in typed_dict.required_keys else HAS_DEFAULT,
                )
                for name, annotation in typed_dict.items.items()
            },
        )
    if info.is_named_tuple and info.tuple_type is not None:
        names: list[str] = info.metadata.get("namedtuple", {}).get("fields", [])
        defaults = _body_defaults(project, info)
        return _checked(
            info,
            {
                name: Member(name, annotation, defaults.get(name, NO_DEFAULT))
                for name, annotation in zip(names, info.tuple_type.items, strict=False)
            },
        )
    if info.has_base(_BASE_MODEL):
        return _checked(info, _pydantic_fields(project, info))
    for plugin in ("dataclass", "attrs"):
        if plugin in info.metadata:
            return _checked(info, _plugin_fields(project, info, plugin))
    return _checked(info, _plain_fields(project, info))


def _unresolved_base(project: Project, info: TypeInfo) -> str:
    for klass in info.mro:
        located = project.class_def(klass) if klass.fallback_to_any else None
        if located is None:
            continue
        module, class_def = located
        for base in class_def.bases:
            if not isinstance(project.resolve(module, base), TypeInfo):
                return f"its base class {ast.unparse(base)}"
    return "one of its base classes"


def _checked(info: TypeInfo, fields: dict[str, Member]) -> dict[str, Member]:
    for member in fields.values():
        if is_unresolved(member.annotation):
            raise ShapeError(
                f"can't resolve the annotation of {class_name(info)}.{member.name}"
            )
    return fields


@dataclasses.dataclass(frozen=True)
class _Declared:
    """An annotated assignment in a class body."""

    name: str
    statement: ast.AnnAssign
    module: str
    owner: TypeInfo


def _declared(project: Project, info: TypeInfo) -> list[_Declared]:
    located = project.class_def(info)
    if located is None:
        raise ShapeError(
            f"can't find the source of {class_name(info)}; only classes defined at the top "
            "level of a module, or inside another class, can be read"
        )
    module, class_def = located
    return [
        _Declared(statement.target.id, statement, module, info)
        for statement in class_def.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    ]


def _variable(declared: _Declared) -> Var | None:
    found = declared.owner.names.get(declared.name)
    return found.node if found is not None and isinstance(found.node, Var) else None


def _annotation(declared: _Declared) -> Type:
    variable = _variable(declared)
    if variable is None or variable.type is None:
        return AnyType(TypeOfAny.unannotated)
    return variable.type


def _is_class_var(declared: _Declared) -> bool:
    variable = _variable(declared)
    return variable is not None and variable.is_classvar


def _literal(node: ast.expr | None) -> Any:
    """A default written as a literal (`0`, `"x"`, `None`, `-1`), else `HAS_DEFAULT`."""
    if isinstance(node, ast.Constant):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    return HAS_DEFAULT


def _call_to(
    project: Project, module: str, node: ast.expr | None, names: frozenset[str]
) -> ast.Call | None:
    if isinstance(node, ast.Call) and project.fullname(module, node.func) in names:
        return node
    return None


def _keyword(call: ast.Call, *names: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg in names), None)


def _pydantic_fields(project: Project, info: TypeInfo) -> dict[str, Member]:
    fields: dict[str, Member] = {}
    # Base models first, so a redefined field keeps its base's position, as in Pydantic.
    for klass in reversed(info.mro):
        if not klass.has_base(_BASE_MODEL) or klass.fullname == _BASE_MODEL:
            continue
        for declared in _declared(project, klass):
            if declared.name.startswith("_") or _is_class_var(declared):
                continue
            fields[declared.name] = Member(
                declared.name,
                _annotation(declared),
                _pydantic_default(project, declared),
            )
    return fields


def _pydantic_default(project: Project, declared: _Declared) -> Any:
    value = declared.statement.value
    field = _call_to(project, declared.module, value, frozenset({_PYDANTIC_FIELD}))
    if field is None:
        if value is not None:
            return _literal(value)
        field = _annotated_field(
            project, declared.module, declared.statement.annotation
        )
        if field is None:
            return NO_DEFAULT
    if _keyword(field, "default_factory") is not None:
        return HAS_DEFAULT
    default = field.args[0] if field.args else _keyword(field, "default")
    if default is None or (
        isinstance(default, ast.Constant) and default.value is Ellipsis
    ):
        return NO_DEFAULT
    return _literal(default)


def _annotated_field(
    project: Project, module: str, annotation: ast.expr
) -> ast.Call | None:
    """The `Field(...)` in `Annotated[T, Field(...)]`."""
    if not (
        isinstance(annotation, ast.Subscript)
        and project.fullname(module, annotation.value) in _ANNOTATED
        and isinstance(annotation.slice, ast.Tuple)
    ):
        return None
    for metadata in annotation.slice.elts[1:]:
        field = _call_to(project, module, metadata, frozenset({_PYDANTIC_FIELD}))
        if field is not None:
            return field
    return None


def _plugin_fields(project: Project, info: TypeInfo, plugin: str) -> dict[str, Member]:
    """Fields of a dataclass or attrs class, as mypy's plugin for it recorded them."""
    fields: dict[str, Member] = {}
    defaults = _body_defaults(project, info)
    for attribute in info.metadata[plugin].get("attributes", []):
        name = attribute["name"]
        if attribute.get("is_init_var"):
            continue
        found = info.get(name)
        variable = found.node if found is not None else None
        annotation = (
            variable.type
            if isinstance(variable, Var) and variable.type is not None
            else AnyType(TypeOfAny.unannotated)
        )
        default = (
            defaults.get(name, HAS_DEFAULT) if attribute["has_default"] else NO_DEFAULT
        )
        fields[name] = Member(name, annotation, default)
    return fields


def _body_defaults(project: Project, info: TypeInfo) -> dict[str, Any]:
    """Literal defaults written in the class bodies, for fixes that add them back.

    `field(default=0)` and `attrs.field(default=0)` count as `0`; anything else, such as
    a factory, is `HAS_DEFAULT`.
    """
    defaults: dict[str, Any] = {}
    for klass in reversed(info.mro):
        if (
            not project.is_project_module(klass.module_name)
            or project.class_def(klass) is None
        ):
            continue
        for declared in _declared(project, klass):
            value = declared.statement.value
            if value is None:
                defaults.pop(declared.name, None)
                continue
            call = _call_to(project, declared.module, value, _FIELD_FUNCTIONS)
            if call is not None:
                value = _keyword(call, "default")
                if value is None:
                    defaults[declared.name] = HAS_DEFAULT
                    continue
            defaults[declared.name] = _literal(value)
    return defaults


def _plain_fields(project: Project, info: TypeInfo) -> dict[str, Member]:
    """A plain class's annotated attributes; a class-level value is the default.

    That reading only holds for the project's own classes that no framework processes,
    so it rejects other metaclasses (Protocol, Enum, ABC, SQLAlchemy's declarative base),
    base classes from installed packages, and descriptors in place of fields.
    """
    unsupported = f"can't read the fields of {class_name(info)}"
    if info.is_protocol:
        raise ShapeError(
            f"{unsupported}: it's a Protocol; py-extended-types supports {SUPPORTED_KINDS}"
        )
    metaclass = info.metaclass_type
    if metaclass is not None and metaclass.type.fullname != "builtins.type":
        raise ShapeError(
            f"{unsupported}: its metaclass {metaclass.type.name} may store fields its own "
            f"way; py-extended-types supports {SUPPORTED_KINDS}"
        )
    bases = [klass for klass in info.mro if klass.fullname != "builtins.object"]
    foreign = next(
        (klass for klass in bases if not project.is_project_module(klass.module_name)),
        None,
    )
    if foreign is not None:
        raise ShapeError(
            f"{unsupported}: it derives from {foreign.fullname}, which py-extended-types "
            f"can't read; it supports {SUPPORTED_KINDS}"
        )
    fields: dict[str, Member] = {}
    for klass in reversed(bases):
        for declared in _declared(project, klass):
            if _is_class_var(declared):
                continue
            value = declared.statement.value
            if _is_descriptor(project, declared.module, value):
                raise ShapeError(
                    f"{unsupported}: field '{declared.name}' is a descriptor, not a plain "
                    f"default; py-extended-types supports {SUPPORTED_KINDS}"
                )
            fields[declared.name] = Member(
                declared.name,
                _annotation(declared),
                NO_DEFAULT if value is None else _literal(value),
            )
    return fields


def _is_descriptor(project: Project, module: str, value: ast.expr | None) -> bool:
    if not isinstance(value, ast.Call):
        return False
    created = project.resolve(module, value.func)
    return isinstance(created, TypeInfo) and created.get("__get__") is not None
