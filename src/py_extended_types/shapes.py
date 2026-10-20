"""Shapes: the field-level description of a class, and the helpers that derive new ones.

A shape is an ordered set of `Member`s. `Map` and `Filter` are the primitives that
derive one shape from another; `Partial`, `Required`, `Omit` and `Pick` are built on
them, and project-specific helpers can be too.

Every shape is lazy: building one only records the operation, and the fields (and any
function passed to `Map` / `Filter`) are only computed when `py-extended-types check` asks for
them. Decorating a class therefore runs none of this in the app, and mistakes such as
omitting an unknown key surface as check problems rather than import errors.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, Literal

from py_extended_types.typecheck import add_none, format_type, remove_none


class ShapeError(Exception):
    """Raised when a shape expression is invalid, e.g. `Omit(User, "typo")`."""


class _Sentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


NO_DEFAULT: Any = _Sentinel("NO_DEFAULT")
"""`Member.default` of a required field."""

HAS_DEFAULT: Any = _Sentinel("HAS_DEFAULT")
"""`Member.default` of an optional field whose default value isn't known or doesn't matter."""


@dataclasses.dataclass(frozen=True)
class Factory:
    """`Member.default` of a field whose default comes from `default_factory`."""

    factory: Callable[[], Any]


Qualifier = Literal["Final", "ReadOnly"]

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_KEEP: Any = _Sentinel("_KEEP")


@dataclasses.dataclass(frozen=True)
class Member:
    """One field of a class or shape.

    Attributes:
        name: the field's name.
        annotation: its type, e.g. `str | None`, with typing qualifiers moved to `quals`.
        quals: `"Final"` / `"ReadOnly"` qualifiers on the annotation.
        default: `NO_DEFAULT` (required), `HAS_DEFAULT`, a `Factory`, or the default value.
        field: the keyword arguments given to Pydantic's `Field(...)`; empty otherwise.
    """

    name: str
    annotation: Any
    quals: frozenset[Qualifier] = frozenset()
    default: Any = NO_DEFAULT
    field: Mapping[str, Any] = dataclasses.field(default_factory=lambda: _EMPTY)

    @property
    def required(self) -> bool:
        return self.default is NO_DEFAULT

    def replace(
        self,
        *,
        annotation: Any = _KEEP,
        quals: frozenset[Qualifier] = _KEEP,
        default: Any = _KEEP,
        field: Mapping[str, Any] = _KEEP,
    ) -> Member:
        """A copy with the given attributes changed. The name can't change: shapes match by name."""
        changes = {
            "annotation": annotation,
            "quals": quals,
            "default": default,
            "field": field,
        }
        return dataclasses.replace(
            self, **{key: value for key, value in changes.items() if value is not _KEEP}
        )


class Shape:
    """A named, lazily computed mapping of field name to `Member`."""

    def __init__(self, name: str, compute: Callable[[], Mapping[str, Member]]) -> None:
        self._name = name
        self._compute = compute
        self._fields: dict[str, Member] | None = None

    @property
    def fields(self) -> dict[str, Member]:
        if self._fields is None:
            self._fields = dict(self._compute())
        return self._fields

    def __repr__(self) -> str:
        return self._name


ShapeLike = typing.Union[type, Shape]  # noqa: UP007


def as_shape(target: ShapeLike) -> Shape:
    """Wrap a class as a shape. An invalid target only raises once the fields are read."""
    if isinstance(target, Shape):
        return target
    if isinstance(target, type):
        return Shape(target.__qualname__, lambda: fields_of(target))

    def invalid() -> dict[str, Member]:
        raise ShapeError(f"expected a class or a shape, got {target!r}")

    return Shape(repr(target), invalid)


def Map(  # noqa: N802
    target: ShapeLike,
    fn: Callable[[Member], Member],
    *,
    name: str | None = None,
) -> Shape:
    """Transform every field with `fn`, keeping their order.

    `fn` gets each `Member` and returns its replacement, usually `m.replace(...)`. It
    can't drop fields (use `Filter`) or rename them.

    Args:
        name: how the shape is written in messages; defaults to `Map[<target>, <fn>]`.
    """
    base = as_shape(target)
    label = name or f"Map[{base!r}, {_callable_name(fn)}]"

    def compute() -> dict[str, Member]:
        result = {}
        for member in base.fields.values():
            mapped = _call(label, member, fn)
            if not isinstance(mapped, Member):
                raise ShapeError(
                    f"{label}: expected a Member for field '{member.name}', got {mapped!r}"
                )
            if mapped.name != member.name:
                raise ShapeError(
                    f"{label}: can't rename field '{member.name}' to '{mapped.name}'"
                )
            result[member.name] = mapped
        return result

    return Shape(label, compute)


def Filter(  # noqa: N802
    target: ShapeLike,
    predicate: Callable[[Member], bool],
    *,
    name: str | None = None,
) -> Shape:
    """Keep the fields for which `predicate` returns true.

    Args:
        name: how the shape is written in messages; defaults to `Filter[<target>, <predicate>]`.
    """
    base = as_shape(target)
    label = name or f"Filter[{base!r}, {_callable_name(predicate)}]"

    def compute() -> dict[str, Member]:
        return {
            member.name: member
            for member in base.fields.values()
            if _call(label, member, predicate)
        }

    return Shape(label, compute)


def _call(label: str, member: Member, fn: Callable[[Member], Any]) -> Any:
    try:
        return fn(member)
    except ShapeError:
        raise
    except Exception as error:
        raise ShapeError(
            f"{label}: {type(error).__name__} for field '{member.name}': {error}"
        ) from error


def _callable_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", None) or repr(fn)


def Partial(target: ShapeLike, *, nullable: bool = True) -> Shape:  # noqa: N802
    """Make every field optional.

    Args:
        nullable: also widen each type with `| None` and default it to `None` (the usual
            Pydantic idiom `field: T | None = None`). Pass `False` to keep the types.
    """
    base = as_shape(target)
    suffix = "" if nullable else ", nullable=False"

    def optional(member: Member) -> Member:
        if nullable:
            return member.replace(annotation=add_none(member.annotation), default=None)
        return member if not member.required else member.replace(default=HAS_DEFAULT)

    return Map(base, optional, name=f"Partial[{base!r}{suffix}]")


def Required(target: ShapeLike) -> Shape:  # noqa: N802
    """Make every field required and non-nullable: `T | None` becomes `T`.

    The counterpart of `Partial`, like TypeScript's `Required<T>` dropping `undefined`.
    """
    base = as_shape(target)
    return Map(
        base,
        lambda member: member.replace(
            annotation=remove_none(member.annotation), default=NO_DEFAULT
        ),
        name=f"Required[{base!r}]",
    )


def Pick(target: ShapeLike, *keys: str) -> Shape:  # noqa: N802
    """Keep only the given fields; unknown keys are an error."""
    base = as_shape(target)
    label = f"Pick[{base!r}, {_format_keys(keys)}]"
    return _with_known_keys(
        Filter(base, lambda member: member.name in keys, name=label), base, keys
    )


def Omit(target: ShapeLike, *keys: str) -> Shape:  # noqa: N802
    """Drop the given fields; unknown keys are an error."""
    base = as_shape(target)
    label = f"Omit[{base!r}, {_format_keys(keys)}]"
    return _with_known_keys(
        Filter(base, lambda member: member.name not in keys, name=label), base, keys
    )


def _with_known_keys(shape: Shape, base: Shape, keys: tuple[str, ...]) -> Shape:
    def compute() -> dict[str, Member]:
        if not keys:
            raise ShapeError(f"{shape!r}: at least one key is required")
        unknown = [key for key in keys if key not in base.fields]
        if unknown:
            raise ShapeError(
                f"{shape!r}: unknown field(s) {_format_keys(tuple(unknown))}; "
                f"{base!r} has {_format_keys(tuple(base.fields))}"
            )
        return shape.fields

    return Shape(repr(shape), compute)


class KeyOf:
    """The field names of a class or shape, like TypeScript's `keyof T`, for `Record`'s keys.

    Lazy like every shape: the names are only read when `py-extended-types check` needs them, which
    is why this isn't a `Literal` itself. `.literal` builds that `Literal["a", "b"]`.
    """

    def __init__(self, target: ShapeLike) -> None:
        self._shape = as_shape(target)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._shape.fields)

    @property
    def literal(self) -> Any:
        names = self.names
        if not names:
            raise ShapeError(f"{self!r}: {self._shape!r} has no fields")
        return Literal[names]

    def __repr__(self) -> str:
        return f"KeyOf[{self._shape!r}]"


def Record(  # noqa: N802
    keys: Any,
    # Spelled as a union with `Callable` first so type checkers infer a lambda's `key: str`.
    value: Callable[[str], Any] | object,
    *,
    name: str | None = None,
) -> Shape:
    """A required field for each key, like TypeScript's `Record<K, V>`.

    Args:
        keys: a `Literal["a", "b"]` (or a union of `Literal`s), `KeyOf(T)`, or an iterable
            of names.
        value: every field's type, or a function (`def` / `lambda`) that takes the key
            and returns that field's type.
        name: how the shape is written in messages; defaults to `Record[<keys>, <value>]`.
    """
    if (
        _literal_keys(keys) is None
        and isinstance(keys, Iterable)
        and not isinstance(keys, (str, bytes))
    ):
        keys = tuple(keys)  # a generator would be used up by the label below
    per_key = _is_function(value)
    label = name or _record_label(keys, value, per_key)

    def compute() -> dict[str, Member]:
        result = {}
        for key in _record_keys(label, keys):
            annotation = value
            if per_key:
                try:
                    annotation = value(key)  # type: ignore[operator]
                except Exception as error:
                    raise ShapeError(
                        f"{label}: {type(error).__name__} for key '{key}': {error}"
                    ) from error
            result[key] = Member(name=key, annotation=annotation)
        return result

    return Shape(label, compute)


def _record_label(keys: Any, value: Any, per_key: bool) -> str:
    # Built when the decorator line runs, so it must never raise there.
    try:
        shown = _callable_name(value) if per_key else format_type(value)
        return f"Record[{_describe_keys(keys)}, {shown}]"
    except Exception:  # noqa: BLE001
        return "Record[...]"


def _is_function(value: Any) -> bool:
    # Classes and generic aliases such as `list[int]` are callable too, but they're types.
    return (
        inspect.isfunction(value)
        or inspect.ismethod(value)
        or isinstance(value, functools.partial)
    )


def _literal_keys(keys: Any) -> list[Any] | None:
    """The values of `Literal["a", "b"]` or `Literal["a"] | Literal["b"]`; None for anything else."""
    origin = typing.get_origin(keys)
    if origin is Literal:
        return list(typing.get_args(keys))
    if origin is typing.Union or origin is types.UnionType:
        values = [_literal_keys(member) for member in typing.get_args(keys)]
        if all(found is not None for found in values):
            return [key for found in values if found is not None for key in found]
    return None


def _describe_keys(keys: Any) -> str:
    literal = _literal_keys(keys)
    if literal is not None:
        return " | ".join(repr(key) for key in literal)
    if isinstance(keys, Iterable) and not isinstance(keys, (str, bytes)):
        return " | ".join(repr(key) for key in keys)
    return repr(keys)


def _record_keys(label: str, keys: Any) -> list[str]:
    found = list(keys.names) if isinstance(keys, KeyOf) else _literal_keys(keys)
    if found is None:
        if not isinstance(keys, Iterable) or isinstance(keys, (str, bytes)):
            raise ShapeError(
                f"{label}: keys must be a Literal[...], KeyOf(...) or an iterable of names, got {keys!r}"
            )
        found = list(keys)
    if not found:
        raise ShapeError(f"{label}: at least one key is required")
    invalid = [key for key in found if not isinstance(key, str)]
    if invalid:
        raise ShapeError(f"{label}: keys must be strings, got {invalid[0]!r}")
    return list(dict.fromkeys(found))


def _format_keys(keys: tuple[str, ...]) -> str:
    return " | ".join(repr(key) for key in keys)


SUPPORTED_KINDS = "Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs classes and plain classes"


def fields_of(cls: type) -> dict[str, Member]:
    """Extract the fields of a Pydantic model, dataclass, TypedDict, NamedTuple, attrs class
    or plain class (see `_plain_class_defaults`).

    Raises:
        ShapeError: for any other class. Each kind stores its fields and defaults in its
            own way, so guessing would misread e.g. SQLAlchemy models.
    """
    model_fields = _pydantic_fields(cls)
    if model_fields is not None:
        quals = _qualifiers(cls)
        return {
            name: Member(
                name=name,
                annotation=info.annotation,
                quals=quals.get(name, frozenset()),
                default=_pydantic_default(info),
                field=_pydantic_field_arguments(info),
            )
            for name, info in model_fields.items()
        }

    if typing.is_typeddict(cls):
        required_keys: frozenset[str] = getattr(cls, "__required_keys__", frozenset())
        return _members(
            cls,
            {
                name: NO_DEFAULT if name in required_keys else HAS_DEFAULT
                for name in _type_hints(cls)
            },
        )
    if dataclasses.is_dataclass(cls):
        return _members(
            cls,
            {
                field.name: _dataclass_default(field)
                for field in dataclasses.fields(cls)
            },
        )
    if _is_namedtuple(cls):
        names: tuple[str, ...] = getattr(cls, "_fields", ())
        defaults: Mapping[str, Any] = getattr(cls, "_field_defaults", {})
        return _members(cls, {name: defaults.get(name, NO_DEFAULT) for name in names})
    attrs_fields = getattr(cls, "__attrs_attrs__", None)
    if isinstance(attrs_fields, tuple):
        return _members(
            cls,
            {attribute.name: _attrs_default(attribute) for attribute in attrs_fields},
        )
    return _members(cls, _plain_class_defaults(cls))


def _plain_class_defaults(cls: type) -> dict[str, Any]:
    """Defaults of a plain class: its class-level values, so a field without one is required.

    That reading only holds for classes no framework has processed, so it requires the
    plain `type` metaclass (Pydantic, SQLAlchemy's declarative base, Protocol and Enum have
    their own) and no descriptor in place of a field (NamedTuple accessors, SQLAlchemy's
    mapped attributes, `__slots__` members, properties).
    """
    unsupported = f"can't read the fields of {_class_name(cls)}"
    if type(cls) is not type:
        raise ShapeError(
            f"{unsupported}: its metaclass {type(cls).__qualname__} may store fields its own "
            f"way; py-extended-types supports {SUPPORTED_KINDS}"
        )
    defaults: dict[str, Any] = {}
    for name, annotation in _type_hints(cls).items():
        if typing.get_origin(annotation) is typing.ClassVar:
            continue
        default = _class_default(cls, name)
        if hasattr(type(default), "__get__"):
            raise ShapeError(
                f"{unsupported}: field '{name}' is a {type(default).__qualname__}, not a plain "
                f"default; py-extended-types supports {SUPPORTED_KINDS}"
            )
        defaults[name] = default
    return defaults


def _class_default(cls: type, name: str) -> Any:
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    return NO_DEFAULT


def _members(cls: type, defaults: Mapping[str, Any]) -> dict[str, Member]:
    """Members for the given field names (in order), typed from the class's annotations."""
    hints = _type_hints(cls)
    result = {}
    for name, default in defaults.items():
        annotation, quals = _strip_qualifiers(hints.get(name, Any))
        result[name] = Member(
            name=name, annotation=annotation, quals=quals, default=default
        )
    return result


def _type_hints(cls: type) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls, include_extras=True)
    except Exception as error:
        raise ShapeError(
            f"can't resolve the annotations of {_class_name(cls)}: {type(error).__name__}: {error}"
        ) from error


def _class_name(cls: object) -> str:
    return getattr(cls, "__qualname__", None) or repr(cls)


def _is_namedtuple(cls: type) -> bool:
    return isinstance(cls, type) and issubclass(cls, tuple) and hasattr(cls, "_fields")


def _attrs_default(attribute: Any) -> Any:
    # Duck-typed like the Pydantic reader: `attrs.NOTHING` marks "no default", and
    # `attrs.Factory(fn)` wraps a default factory.
    default = attribute.default
    if type(default).__name__ == "_Nothing":
        return NO_DEFAULT
    if type(default).__name__ == "Factory":
        return Factory(default.factory)
    return default


def _pydantic_fields(cls: type) -> Mapping[str, Any] | None:
    # Duck-typed so this package never imports pydantic itself.
    if not hasattr(cls, "model_config"):
        return None
    fields = getattr(cls, "__pydantic_fields__", None)
    if isinstance(fields, Mapping):
        return fields
    return None


def _pydantic_default(info: Any) -> Any:
    if info.is_required():
        return NO_DEFAULT
    if info.default_factory is not None:
        return Factory(info.default_factory)
    return info.default


def _pydantic_field_arguments(info: Any) -> Mapping[str, Any]:
    # Pydantic records the arguments a `FieldInfo` was created with (`Field(...)` keywords
    # plus the annotation, which `Member.annotation` already covers), except constraints
    # such as `max_length`, which it moves into `metadata` objects.
    recorded = getattr(info, "_attributes_set", None)
    arguments = {
        key: value
        for key, value in (recorded.items() if isinstance(recorded, Mapping) else ())
        if key != "annotation"
    }
    for constraint in getattr(info, "metadata", ()):
        if dataclasses.is_dataclass(constraint):
            arguments.update(
                (item.name, getattr(constraint, item.name))
                for item in dataclasses.fields(constraint)
            )
        elif hasattr(constraint, "__dict__"):
            arguments.update(vars(constraint))
    return MappingProxyType(arguments) if arguments else _EMPTY


def _dataclass_default(field: dataclasses.Field[Any]) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        return Factory(field.default_factory)
    return NO_DEFAULT


def _qualifiers(cls: type) -> dict[str, frozenset[Qualifier]]:
    # Pydantic drops `Final` from `FieldInfo.annotation`, so read it from the class itself.
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolvable hints just mean no qualifiers
        return {}
    return {
        name: _strip_qualifiers(annotation)[1] for name, annotation in hints.items()
    }


# Matched by name so `typing_extensions.ReadOnly` (Python < 3.13) counts too.
_RECORDED_QUALIFIERS: dict[str, Qualifier] = {"Final": "Final", "ReadOnly": "ReadOnly"}
_DROPPED_QUALIFIERS = frozenset({"Required", "NotRequired"})


def _strip_qualifiers(annotation: Any) -> tuple[Any, frozenset[Qualifier]]:
    """`Final[ReadOnly[int]]` -> `(int, {"Final", "ReadOnly"})`; `Required` and `NotRequired` are dropped."""
    quals: set[Qualifier] = set()
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            inner, inner_quals = _strip_qualifiers(typing.get_args(annotation)[0])
            if not inner_quals:
                break
            annotation, quals = inner, quals | inner_quals
            continue
        qualifier = getattr(origin, "_name", None)
        if qualifier in _RECORDED_QUALIFIERS:
            quals.add(_RECORDED_QUALIFIERS[qualifier])
        elif qualifier not in _DROPPED_QUALIFIERS:
            break
        annotation = typing.get_args(annotation)[0]
    return annotation, frozenset(quals)
