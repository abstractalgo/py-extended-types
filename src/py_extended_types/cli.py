"""`py-extended-types` command line.

The checker must import the user's modules, which need the user's dependencies,
so when installed as a standalone tool (`uv tool install`) it re-executes itself
under the target project's interpreter (`--python`, `$VIRTUAL_ENV`, or the
nearest `.venv`), putting this package on `PYTHONPATH`. That works because
`py_extended_types` has no dependencies of its own.
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import py_extended_types
from py_extended_types import overrides
from py_extended_types.core import check, registrations, start_collecting
from py_extended_types.report import (
    Diagnostic,
    diagnostics_for,
    import_failure,
    render_json,
    render_stylish,
    use_color,
)
from py_extended_types.shapes import Shape, ShapeError, as_shape
from py_extended_types.typecheck import format_type

CHILD_ENV_VAR = "PY_EXTENDED_TYPES_CHILD"
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

    if not options.no_venv and os.environ.get(CHILD_ENV_VAR) != "1":
        python = _resolve_python(options.python)
        if python is not None and not _is_current_interpreter(python):
            return _reexec(python, args)

    if options.command == "check":
        sources = overrides.read(sys.stdin.read()) if options.stdin else {}
        return _run_check(
            options.paths,
            output_format=options.format,
            verbose=options.verbose,
            sources=sources,
        )
    return _run_show(options.target)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py-extended-types",
        description="Verify `@assert_shape` contracts between Python classes.",
    )
    parser.add_argument(
        "--version", action="version", version=f"py-extended-types {_version()}"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--python",
        help="interpreter to import the project with (default: $VIRTUAL_ENV or nearest .venv)",
    )
    common.add_argument(
        "--no-venv",
        action="store_true",
        help="run in py-extended-types's own interpreter",
    )
    subcommands = parser.add_subparsers(dest="command")

    check_parser = subcommands.add_parser(
        "check",
        parents=[common],
        help="verify every @assert_shape declaration under PATHS",
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


def _is_current_interpreter(python: Path) -> bool:
    venv = python.absolute().parent.parent
    return venv.resolve() == Path(sys.prefix).resolve()


def _reexec(python: Path, args: list[str]) -> int:
    package_root = str(Path(py_extended_types.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env[CHILD_ENV_VAR] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [package_root, env.get("PYTHONPATH")])
    )
    return subprocess.call([str(python), "-m", "py_extended_types", *args], env=env)


def _run_check(
    paths: list[Path],
    *,
    output_format: str,
    verbose: bool,
    sources: dict[Path, str] | None = None,
) -> int:
    sources = sources or {}
    start_collecting()
    overrides.install(sources)
    files = sorted(set(_discover(paths, sources)))
    diagnostics: list[Diagnostic] = []
    for path in files:
        try:
            _import_file(path)
        except BaseException as error:  # user modules may call sys.exit()
            diagnostics.append(import_failure(path, error))

    # Only report classes declared in the files asked about, not ones imported along the way.
    scanned = set(files)
    checked = [r for r in registrations() if Path(r.filename).resolve() in scanned]
    for registration in checked:
        diagnostics.extend(diagnostics_for(registration, check(registration)))

    if output_format == "json":
        print(render_json(diagnostics, classes=len(checked), files=len(files)))
        return 1 if diagnostics else 0
    if diagnostics:
        print(render_stylish(diagnostics, color=use_color()))
        return 1
    if verbose:
        print(
            f"✔ {len(checked)} class{'es' if len(checked) != 1 else ''} checked in "
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
                # Cheap pre-filter so we only import modules that declare something.
                source = sources.get(file)
                if source is None:
                    try:
                        source = file.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                if "assert_shape(" in source and "py_extended_types" in source:
                    yield file


def _import_file(path: Path) -> None:
    root, module_name = _module_name(path)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    importlib.import_module(module_name)


def _module_name(path: Path) -> tuple[Path, str]:
    """Walk up through `__init__.py` packages to find the import root and dotted name."""
    parts = [] if path.stem == "__init__" else [path.stem]
    directory = path.parent
    while (directory / "__init__.py").exists():
        parts.insert(0, directory.name)
        directory = directory.parent
    return directory, ".".join(parts)


def _run_show(target: str) -> int:
    try:
        shape = _evaluate_target(target)
        fields = shape.fields
    except (
        ShapeError,
        ImportError,
        SyntaxError,
        NameError,
        AttributeError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{shape!r}:")
    for name, member in fields.items():
        marker = "" if member.required else "?"
        quals = f"  [{', '.join(sorted(member.quals))}]" if member.quals else ""
        print(f"  {name}{marker}: {format_type(member.annotation)}{quals}")
    return 0


def _evaluate_target(target: str) -> Shape:
    module_name, separator, expression = target.partition(":")
    if not separator:
        raise ValueError("target must look like 'module:Expr'")
    sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module(module_name)
    namespace: dict[str, Any] = dict(vars(module))
    namespace.update(
        {name: getattr(py_extended_types, name) for name in py_extended_types.__all__}
    )
    return as_shape(eval(expression, namespace))  # noqa: S307 - developer-supplied expression
