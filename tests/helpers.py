"""Write modules to a temporary directory and check them statically, as the CLI would."""

import ast
import os
import textwrap
from pathlib import Path

from py_extended_types.analysis.check import check, declarations
from py_extended_types.analysis.project import Environment, Project
from py_extended_types.analysis.shapes import Scope, Shape, evaluate
from py_extended_types.cli import CACHE_DIR_VAR

PRELUDE = """\
import dataclasses
from typing import Annotated, ClassVar, Final, Literal, NamedTuple, Optional, Protocol, TypedDict

from pydantic import BaseModel, Field

from py_extended_types import (
    KeyOf, Omit, Partial, Pick, Record, Required, assert_field, assert_shape,
)
"""

USER = """
class User(BaseModel):
    id: int
    name: str
    email: str
    nickname: str | None = None
    tags: list[str] = Field(default_factory=list)
"""


def write(
    directory: Path, source: str, *, name: str = "models", prelude: bool = True
) -> Path:
    path = directory / f"{name}.py"
    path.write_text((PRELUDE if prelude else "") + textwrap.dedent(source))
    return path


def analyze(*paths: Path) -> Project:
    return Project.analyze(
        list(paths),
        environment=Environment.current(),
        cache_dir=Path(os.environ[CACHE_DIR_VAR]),
    )


def problems(directory: Path, source: str) -> dict[str, list[str]]:
    """`{class name: ["rule: message", ...]}` for every declaration in `source`."""
    path = write(directory, source)
    project = analyze(path)
    result: dict[str, list[str]] = {}
    for declaration in declarations(project, path):
        result.setdefault(declaration.class_name, []).extend(
            f"{problem.rule}: {problem.message}"
            for problem in check(project, declaration)
        )
    return result


def shape(directory: Path, source: str, expression: str) -> Shape:
    """The shape `expression` evaluates to at the end of `source`."""
    path = write(directory, source)
    project = analyze(path)
    module = project.module_for_path(path)
    assert module is not None
    return evaluate(Scope(project, module), ast.parse(expression, mode="eval").body)


def fields(
    directory: Path, source: str, expression: str
) -> dict[str, tuple[str, object]]:
    """`{name: (type, default)}` of a shape, with types as they're printed."""
    from py_extended_types.analysis.types import format_type

    return {
        name: (format_type(member.annotation), member.default)
        for name, member in shape(directory, source, expression).fields.items()
    }
