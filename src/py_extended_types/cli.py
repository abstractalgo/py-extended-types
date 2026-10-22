"""`py-extended-types` command line.

Checking is static: mypy's semantic analysis reads the checked files and everything they
import from source, so no module is imported and no code runs, not even the project's
interpreter. The project's environment (`--python`, `$VIRTUAL_ENV`, or the nearest `.venv`)
is only used to find its installed packages on disk.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from collections.abc import Iterator, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from py_extended_types.analysis.check import check, declarations
from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.project import AnalysisFailed, Environment, Project
from py_extended_types.analysis.shapes import Scope, evaluate
from py_extended_types.analysis.types import format_type
from py_extended_types.report import (
    Diagnostic,
    analysis_failure,
    diagnostics_for,
    render_json,
    render_stylish,
    use_color,
)

CACHE_DIR_VAR = "PY_EXTENDED_TYPES_CACHE_DIR"
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
    }
)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    options = parser.parse_args(args)

    if options.command is None:
        parser.print_help()
        return 2

    environment = _environment(options.python, no_venv=options.no_venv)
    if options.command == "check":
        sources = _read_stdin(sys.stdin.read()) if options.stdin else {}
        return _run_check(
            options.paths,
            environment=environment,
            output_format=options.format,
            verbose=options.verbose,
            sources=sources,
        )
    return _run_show(options.target, environment)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py-extended-types",
        description="Verify `@assert_shape` / `@assert_field` contracts between Python classes, statically.",
    )
    parser.add_argument(
        "--version", action="version", version=f"py-extended-types {_version()}"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--python",
        help="the project's interpreter, used only to find its installed packages; never run "
        "(default: $VIRTUAL_ENV or nearest .venv)",
    )
    common.add_argument(
        "--no-venv",
        action="store_true",
        help="find installed packages in py-extended-types's own environment instead",
    )
    subcommands = parser.add_subparsers(dest="command")

    check_parser = subcommands.add_parser(
        "check",
        parents=[common],
        help="verify every @assert_shape / @assert_field declaration under PATHS",
    )
    check_parser.add_argument("paths", nargs="*", default=["."], type=Path)
    check_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read a JSON object of {path: source} from stdin and check those "
        "sources instead of the files on disk (for unsaved editor buffers)",
    )
    check_parser.add_argument(
        "--format",
        choices=["stylish", "json"],
        default="stylish",
        help="output format (default: stylish)",
    )
    check_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print a summary when there are no problems",
    )

    show_parser = subcommands.add_parser(
        "show", parents=[common], help="print the fields of a class or shape expression"
    )
    show_parser.add_argument(
        "target", help="'module:Expr', e.g. 'app.models:Partial(Omit(User, \"id\"))'"
    )
    return parser


def _version() -> str:
    try:
        return version("py-extended-types")
    except PackageNotFoundError:
        return "unknown"


def _environment(explicit: str | None, *, no_venv: bool) -> Environment:
    if no_venv:
        return Environment.current()
    python = _resolve_python(explicit)
    return Environment.of_interpreter(python) if python else Environment.current()


def _resolve_python(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = Path(virtual_env) / "bin" / "python"
        if candidate.exists():
            return candidate
    for directory in [Path.cwd(), *Path.cwd().parents]:
        candidate = directory / ".venv" / "bin" / "python"
        if candidate.exists():
            return candidate
    return None


def _cache_dir(environment: Environment) -> Path:
    """mypy's incremental cache, per project directory and environment."""
    explicit = os.environ.get(CACHE_DIR_VAR)
    if explicit:
        return Path(explicit)
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = json.dumps(
        [str(Path.cwd()), environment.package_dirs, environment.python_version]
    )
    return base / "py-extended-types" / hashlib.sha256(key.encode()).hexdigest()[:16]


def _read_stdin(stream_text: str) -> dict[Path, str]:
    """Parse the `--stdin` payload: a JSON object of `{path: source}`."""
    payload = json.loads(stream_text) if stream_text.strip() else {}
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ValueError(
            "--stdin expects a JSON object mapping file paths to source text"
        )
    return {Path(key).resolve(): value for key, value in payload.items()}


def _run_check(
    paths: list[Path],
    *,
    environment: Environment,
    output_format: str,
    verbose: bool,
    sources: dict[Path, str] | None = None,
) -> int:
    sources = sources or {}
    files = sorted(set(_discover(paths, sources)))
    diagnostics: list[Diagnostic] = []
    classes: set[tuple[Path, str]] = set()
    if files:
        try:
            project = Project.analyze(
                files,
                overrides=sources,
                environment=environment,
                cache_dir=_cache_dir(environment),
            )
        except AnalysisFailed as error:
            diagnostics.extend(analysis_failure(failure) for failure in error.failures)
        else:
            for path in files:
                for declaration in declarations(project, path):
                    classes.add((path, declaration.class_name))
                    diagnostics.extend(
                        diagnostics_for(
                            declaration,
                            check(project, declaration),
                            project.source(declaration.module),
                        )
                    )

    if output_format == "json":
        print(render_json(diagnostics, classes=len(classes), files=len(files)))
        return 1 if diagnostics else 0
    if diagnostics:
        print(render_stylish(diagnostics, color=use_color()))
        return 1
    if verbose:
        print(
            f"✔ {len(classes)} class{'es' if len(classes) != 1 else ''} checked in "
            f"{len(files)} file{'s' if len(files) != 1 else ''}, 0 problems"
        )
    return 0


def _discover(paths: list[Path], sources: dict[Path, str]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path.resolve()
            continue
        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                file = Path(root, filename).resolve()
                # Cheap pre-filter so only modules that declare something are analyzed.
                source = sources.get(file)
                if source is None:
                    try:
                        source = file.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                if (
                    "assert_shape(" in source or "assert_field(" in source
                ) and "py_extended_types" in source:
                    yield file


def _run_show(target: str, environment: Environment) -> int:
    module, separator, expression = target.partition(":")
    if not separator:
        print("error: target must look like 'module:Expr'", file=sys.stderr)
        return 1
    try:
        node = ast.parse(expression, mode="eval").body
        project = Project.analyze(
            [],
            modules=[module, "py_extended_types"],
            environment=environment,
            cache_dir=_cache_dir(environment),
        )
        if project.module(module) is None:
            raise ShapeError(f"can't find module {module}")
        shape = evaluate(Scope(project, module, fallback="py_extended_types"), node)
    except (ShapeError, AnalysisFailed, SyntaxError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{shape!r}:")
    for name, member in shape.fields.items():
        marker = "" if member.required else "?"
        print(f"  {name}{marker}: {format_type(member.annotation)}")
    return 0
