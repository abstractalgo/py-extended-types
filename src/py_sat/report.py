"""Diagnostics output: ESLint/oxlint `stylish` for humans, JSON for editors.

Stylish groups problems per file, one aligned row each:

path/to/models.py
  12:5  error  User.name has type 'int', expected 'str'  py-sat(type-mismatch)

✖ 1 problem (1 error, 0 warnings)
"""

from __future__ import annotations

import ast
import inspect
import json
import linecache
import os
import sys
import textwrap
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from py_sat.core import Problem, Registration
from py_sat.shapes import Member
from py_sat.typecheck import format_type, includes_none


@dataclass(frozen=True)
class TextEdit:
    """Replace the 1-based span (`end_column` exclusive) with `new_text`."""

    line: int
    column: int
    end_line: int
    end_column: int
    new_text: str


@dataclass(frozen=True)
class Fix:
    title: str
    edits: tuple[TextEdit, ...]


@dataclass(frozen=True)
class Diagnostic:
    """A located problem. `path` is absolute; positions are 1-based, `end_column` exclusive."""

    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    rule: str
    message: str
    fix: Fix | None = None
    # For import errors: the absolute path of the file the exception was raised in.
    origin: str | None = None


def diagnostics_for(
    registration: Registration, problems: Sequence[Problem]
) -> list[Diagnostic]:
    """Pin each problem to its field's name, or to the decorator when the field isn't declared here."""
    path = str(Path(registration.filename).resolve())
    decorator = _decorator_span(registration)
    source = _class_source(registration.cls)
    removed = {p.field for p in problems if p.rule == "unexpected-field" and p.field}
    append_at = _append_position(source, removed) if source else None
    result = []
    for problem in problems:
        declared = source.fields.get(problem.field or "") if source else None
        if declared is None:
            line, column, end_column = decorator
        else:
            line, column, end_column = (
                declared.line,
                declared.column,
                declared.name_end_column,
            )
        result.append(
            Diagnostic(
                path,
                line,
                column,
                line,
                end_column,
                f"py-sat({problem.rule})",
                problem.message,
                _fix_for(problem, source, append_at),
            )
        )
    return result


def import_failure(path: Path, error: BaseException) -> Diagnostic:
    """Locate an import failure in `path`, naming the real origin when it's another file."""
    line, column, end_line, end_column, origin = 1, 1, 1, 1, None
    if isinstance(error, SyntaxError) and error.filename:
        line, column = error.lineno or 1, error.offset or 1
        end_line = error.end_lineno or line
        end_column = error.end_offset or column + 1
        origin = (error.filename, line)
    else:
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            origin = (frames[-1].filename, frames[-1].lineno or 1)
        for frame in frames:
            if Path(frame.filename).resolve() == path:
                line = frame.lineno or 1
                column = (frame.colno or 0) + 1
                end_line = frame.end_lineno or line
                end_column = (frame.end_colno or frame.colno or 0) + 1
    message = f"could not import module: {type(error).__name__}: {error}"
    if origin is not None and Path(origin[0]).resolve() != path:
        message += f" (at {display_path(origin[0])}:{origin[1]})"
    return Diagnostic(
        str(path),
        line,
        column,
        end_line,
        end_column,
        "py-sat(import-error)",
        message,
        origin=str(Path(origin[0]).resolve()) if origin is not None else None,
    )


def render_stylish(diagnostics: Sequence[Diagnostic], *, color: bool) -> str:
    if not diagnostics:
        return ""
    style = _Style(color)
    by_path: dict[str, list[Diagnostic]] = {}
    for diagnostic in diagnostics:
        by_path.setdefault(display_path(diagnostic.path), []).append(diagnostic)

    blocks = []
    for path in sorted(by_path):
        rows = sorted(by_path[path], key=lambda d: (d.line, d.column))
        line_width = max(len(str(d.line)) for d in rows)
        column_width = max(len(str(d.column)) for d in rows)
        message_width = max(len(d.message) for d in rows)
        lines = [style.underline(path)]
        for d in rows:
            location = (
                f"{str(d.line).rjust(line_width)}:{str(d.column).ljust(column_width)}"
            )
            lines.append(
                f"  {style.dim(location)}  {style.red('error')}  {d.message.ljust(message_width)}  {style.dim(d.rule)}"
            )
        blocks.append("\n".join(lines))

    count = len(diagnostics)
    summary = f"✖ {count} problem{'s' if count != 1 else ''} ({count} error{'s' if count != 1 else ''}, 0 warnings)"
    return "\n\n".join(blocks) + "\n\n" + style.bold_red(summary)


def render_json(diagnostics: Sequence[Diagnostic], *, classes: int, files: int) -> str:
    """Machine-readable report, consumed by the VS Code extension."""
    return json.dumps(
        {
            "version": 1,
            "classes": classes,
            "files": files,
            "diagnostics": [
                {
                    "path": d.path,
                    "line": d.line,
                    "column": d.column,
                    "endLine": d.end_line,
                    "endColumn": d.end_column,
                    "severity": "error",
                    "rule": d.rule,
                    "message": d.message,
                    "fix": _fix_json(d.fix),
                    "origin": d.origin,
                }
                for d in diagnostics
            ],
        }
    )


def _fix_json(fix: Fix | None) -> dict[str, object] | None:
    if fix is None:
        return None
    return {
        "title": fix.title,
        "edits": [
            {
                "line": edit.line,
                "column": edit.column,
                "endLine": edit.end_line,
                "endColumn": edit.end_column,
                "newText": edit.new_text,
            }
            for edit in fix.edits
        ],
    }


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def display_path(path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class _DeclaredField:
    """Where an annotated field sits in the source; positions 1-based, end columns exclusive."""

    line: int
    column: int
    name_end_column: int
    end_line: int
    end_column: int
    value: str | None
    value_node: ast.expr | None


@dataclass(frozen=True)
class _ClassSource:
    fields: dict[str, _DeclaredField]
    # (field name or None, last line) for each statement in the class body, in order.
    statements: tuple[tuple[str | None, int], ...]
    # Line just before the first body statement: the end of the class header.
    header_line: int
    body_indent: str
    line_lengths: dict[int, int]


def _class_source(cls: type) -> _ClassSource | None:
    try:
        source_lines, first_line = inspect.getsourcelines(cls)
        source = textwrap.dedent("".join(source_lines))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    class_def = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef)), None
    )
    if class_def is None:
        return None
    indent = len(source_lines[0]) - len(source_lines[0].lstrip())

    def to_line(lineno: int) -> int:
        return first_line + lineno - 1

    fields = {}
    for statement in class_def.body:
        if not (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
        ):
            continue
        column = indent + statement.col_offset + 1
        name = statement.target.id
        value = (
            ast.get_source_segment(source, statement.value)
            if statement.value is not None
            else None
        )
        fields[name] = _DeclaredField(
            line=to_line(statement.lineno),
            column=column,
            name_end_column=column + len(name),
            end_line=to_line(statement.end_lineno or statement.lineno),
            end_column=indent + (statement.end_col_offset or 0) + 1,
            value=value,
            value_node=statement.value,
        )

    statements = tuple(
        (
            statement.target.id
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            else None,
            to_line(statement.end_lineno or statement.lineno),
        )
        for statement in class_def.body
    )
    return _ClassSource(
        fields=fields,
        statements=statements,
        header_line=to_line(class_def.body[0].lineno) - 1,
        body_indent=" " * (indent + class_def.body[0].col_offset),
        line_lengths={
            first_line + index: len(text.rstrip("\r\n"))
            for index, text in enumerate(source_lines)
        },
    )


def _append_position(source: _ClassSource, removed: set[str]) -> tuple[int, int]:
    """End of the last body statement that survives the fixes (or of the class header)."""
    kept = [line for name, line in source.statements if name not in removed]
    line = kept[-1] if kept else source.header_line
    return line, source.line_lengths.get(line, 0) + 1


def _fix_for(
    problem: Problem,
    source: _ClassSource | None,
    append_at: tuple[int, int] | None,
) -> Fix | None:
    name = problem.field
    if source is None or append_at is None or name is None:
        return None
    declared = source.fields.get(name)

    if problem.rule == "unexpected-field":
        if declared is None:
            return None
        # Delete the whole statement, line break included.
        edit = TextEdit(declared.line, 1, declared.end_line + 1, 1, "")
        return Fix(f"Remove field '{name}'", (edit,))

    if problem.expected is None:
        return None
    declaration = _declaration(name, problem.expected, declared)
    if declaration is None:
        return None

    if problem.rule == "missing-field" and declared is None:
        edit = TextEdit(*append_at, *append_at, f"\n{source.body_indent}{declaration}")
        return Fix(f"Add field '{declaration}'", (edit,))

    if problem.rule in ("type-mismatch", "required-mismatch") and declared is not None:
        # One rewrite of the whole statement covers every problem on the field, so
        # applying one fix never leaves another with stale positions.
        edit = TextEdit(
            declared.line,
            declared.column,
            declared.end_line,
            declared.end_column,
            declaration,
        )
        return Fix(f"Change to '{declaration}'", (edit,))
    return None


def _declaration(
    name: str, spec: Member, declared: _DeclaredField | None
) -> str | None:
    """The declaration the shape expects, keeping the current value where it still fits."""
    annotation = format_type(spec.annotation)
    value = declared.value if declared else None
    call = _field_call(declared.value_node if declared else None)

    if spec.required:
        if value is None or call is None:
            return f"{name}: {annotation}"
        if not _has_default(call):
            # `Field(title=...)` without a default leaves the field required.
            return f"{name}: {annotation} = {value}"
        # Removing a default from inside `Field(...)` is left to the user.
        return None

    if value is not None:
        if call is not None and not _has_default(call):
            return f"{name}: {annotation} = {_with_default_none(value, call)}"
        return f"{name}: {annotation} = {value}"
    if type(spec.default) in _LITERAL_DEFAULTS:
        return f"{name}: {annotation} = {spec.default!r}"
    if includes_none(spec.annotation):
        return f"{name}: {annotation} = None"
    return None


# Defaults whose `repr` is valid source for the same value.
_LITERAL_DEFAULTS = frozenset({type(None), bool, int, float, str})


def _field_call(node: ast.expr | None) -> ast.Call | None:
    if isinstance(node, ast.Call):
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if callee == "Field":
            return node
    return None


def _has_default(call: ast.Call) -> bool:
    if call.args:
        return True
    return any(
        keyword.arg in ("default", "default_factory") for keyword in call.keywords
    )


def _with_default_none(value: str, call: ast.Call) -> str:
    opening = value.index("(") + 1
    separator = ", " if call.keywords else ""
    return f"{value[:opening]}default=None{separator}{value[opening:]}"


def _decorator_span(registration: Registration) -> tuple[int, int, int]:
    text = linecache.getline(registration.filename, registration.lineno).rstrip()
    if not text:
        return registration.lineno, 1, 1
    column = len(text) - len(text.lstrip()) + 1
    return registration.lineno, column, len(text) + 1


class _Style:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._enabled else text

    def underline(self, text: str) -> str:
        return self._wrap("4", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def bold_red(self, text: str) -> str:
        return self._wrap("1;31", text)
