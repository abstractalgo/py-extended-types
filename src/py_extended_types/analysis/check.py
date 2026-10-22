"""Finding `@assert_shape` / `@assert_field` declarations in source, and checking them."""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from mypy.nodes import TypeInfo

from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.fields import Member, fields_of
from py_extended_types.analysis.project import Project
from py_extended_types.analysis.shapes import Scope, arguments, constant, evaluate
from py_extended_types.analysis.types import format_type, is_equivalent

ASSERT_SHAPE = "py_extended_types.core.assert_shape"
ASSERT_FIELD = "py_extended_types.core.assert_field"


@dataclasses.dataclass(frozen=True)
class Declaration:
    """One decorator on one class."""

    path: Path
    module: str
    class_def: ast.ClassDef
    class_name: str
    decorator: ast.Call
    kind: str  # ASSERT_SHAPE or ASSERT_FIELD


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


def declarations(project: Project, path: Path) -> list[Declaration]:
    """The declarations in `path`, in source order.

    Only classes at the top level of the module, or nested in such classes, are found:
    a class defined inside a function has no name to look it up by.
    """
    module = project.module_for_path(path)
    if module is None:
        return []
    found = []
    for class_def, qualname in _classes(project.tree(module).body, ""):
        for decorator in class_def.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            kind = project.fullname(module, decorator.func)
            if kind in (ASSERT_SHAPE, ASSERT_FIELD):
                found.append(
                    Declaration(path, module, class_def, qualname, decorator, kind)
                )
    return found


def _classes(body: list[ast.stmt], prefix: str) -> Iterator[tuple[ast.ClassDef, str]]:
    for statement in body:
        if isinstance(statement, ast.ClassDef):
            qualname = f"{prefix}{statement.name}"
            yield statement, qualname
            yield from _classes(statement.body, f"{qualname}.")


def check(project: Project, declaration: Declaration) -> list[Problem]:
    """The reasons the class doesn't match its declaration (empty if it does)."""
    try:
        if declaration.kind == ASSERT_FIELD:
            return _check_field(project, declaration)
        return _check_shape(project, declaration)
    except ShapeError as error:
        return [Problem("invalid-shape", str(error))]


def _class_fields(project: Project, declaration: Declaration) -> dict[str, Member]:
    info: object = None
    for part in declaration.class_name.split("."):
        found = (
            project.lookup(declaration.module, part)
            if info is None
            else info.names.get(part)  # type: ignore[attr-defined]
        )
        info = found.node if found is not None else None
        if not isinstance(info, TypeInfo):
            raise ShapeError(f"can't find class {declaration.class_name}")
    assert isinstance(info, TypeInfo)
    return fields_of(project, info)


def _check_shape(project: Project, declaration: Declaration) -> list[Problem]:
    scope = Scope(project, declaration.module)
    args = arguments("assert_shape", declaration.decorator, 1, frozenset({"exact"}))
    exact_node = args.keywords.get("exact")
    exact = True if exact_node is None else constant(scope, exact_node)
    if not isinstance(exact, bool):
        raise ShapeError(f"exact must be True or False, got {ast.unparse(exact_node)}")  # type: ignore[arg-type]
    shape = evaluate(scope, args.positional[0])
    expected = shape.fields
    actual = _class_fields(project, declaration)
    cls_name = declaration.class_name
    target = repr(shape)
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
            problems.extend(_compare(f"{cls_name}.{name}", target, name, have, want))

    if exact:
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


def _compare(
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


def _check_field(project: Project, declaration: Declaration) -> list[Problem]:
    scope = Scope(project, declaration.module)
    field_node, target, key_node = arguments(
        "assert_field", declaration.decorator, 3
    ).positional
    name = constant(scope, field_node)
    key = constant(scope, key_node)
    for label, value in (("field", name), ("key", key)):
        if not isinstance(value, str):
            raise ShapeError(
                f"@assert_field: the {label} must be a string, got {value!r}"
            )
    shape = evaluate(scope, target)
    source = f"{shape!r}[{key!r}]"
    if key not in shape.fields:
        raise ShapeError(
            f"{source}: unknown field '{key}'; {shape!r} has "
            + " | ".join(repr(field) for field in shape.fields)
        )
    want = shape.fields[key]
    actual = _class_fields(project, declaration)
    cls_name = declaration.class_name

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
