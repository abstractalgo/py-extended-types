"""A static view of the checked files and everything they import.

mypy's semantic analysis resolves names, imports, base classes and annotations across
the project and its installed packages, all from source and stubs: nothing is imported
or executed, not even the project's interpreter. This module runs that analysis and
answers lookups against it; the rest of the package reads class bodies from Python's
`ast`, since mypy only keeps symbol tables for modules it loads from its cache.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mypy import build
from mypy.errors import CompileError
from mypy.find_sources import InvalidSourceList, create_source_list
from mypy.modulefinder import BuildSource
from mypy.nodes import (
    MypyFile,
    SymbolNode,
    SymbolTableNode,
    TypeInfo,
)
from mypy.options import Options
from mypy.types import Instance, Type


@dataclass(frozen=True)
class Environment:
    """Where the project's installed packages live, read from its venv's files."""

    package_dirs: tuple[str, ...]
    python_version: tuple[int, int]

    @classmethod
    def current(cls) -> Environment:
        """py-extended-types's own environment (its `sys.path` is used as is)."""
        return cls((), sys.version_info[:2])

    @classmethod
    def of_interpreter(cls, python: Path) -> Environment:
        """The environment `python` belongs to, found without running it."""
        prefix = python.absolute().parent.parent
        site_dirs = sorted(
            {
                *prefix.glob("lib/python3*/site-packages"),
                *prefix.glob("Lib/site-packages"),
            }
        )
        package_dirs: list[str] = []
        for site in site_dirs:
            package_dirs.append(str(site))
            package_dirs.extend(_pth_dirs(site))
        return cls(tuple(dict.fromkeys(package_dirs)), _python_version(prefix))


def _pth_dirs(site: Path) -> Iterator[str]:
    """Directories that `.pth` files add to `sys.path`, e.g. editable installs.

    Lines starting with `import` are code, which is never run, so packages that install
    themselves through an import hook aren't found.
    """
    for pth in sorted(site.glob("*.pth")):
        try:
            lines = pth.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", "import ", "import\t")):
                continue
            directory = (site / line).resolve()
            if directory.is_dir():
                yield str(directory)


def _python_version(prefix: Path) -> tuple[int, int]:
    try:
        config = (prefix / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        config = ""
    found = re.search(r"^\s*version(?:_info)?\s*=\s*(\d+)\.(\d+)", config, re.MULTILINE)
    if found is None:
        found = next(
            (
                re.fullmatch(r"python(\d+)\.(\d+)", lib.name)
                for lib in prefix.glob("lib/python3*")
            ),
            None,
        )
    if found is None:
        return sys.version_info[:2]
    return int(found.group(1)), int(found.group(2))


@dataclass(frozen=True)
class Failure:
    """A file that couldn't be analyzed, e.g. a syntax error. Positions are 1-based."""

    path: Path
    line: int
    column: int
    message: str


class AnalysisFailed(Exception):
    def __init__(self, failures: Sequence[Failure]) -> None:
        super().__init__("; ".join(failure.message for failure in failures))
        self.failures = list(failures)


# `path:line:column: error: message`, as mypy reports blocking errors.
_BLOCKER = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: error: (?P<message>.*?)(?:  \[[a-z-]+\])?$"
)


class Project:
    """mypy's analysis of a set of files, with their sources and syntax trees."""

    def __init__(
        self, result: build.BuildResult, overrides: Mapping[Path, str]
    ) -> None:
        self._manager = result.manager
        self._graph = result.graph
        self._overrides = overrides
        self._trees: dict[str, ast.Module] = {}
        self._sources: dict[str, str] = {}
        self.options = result.manager.options
        self._modules_by_path = {
            Path(state.path).resolve(): module
            for module, state in result.graph.items()
            if state.path
        }

    @classmethod
    def analyze(
        cls,
        files: Sequence[Path],
        *,
        modules: Sequence[str] = (),
        overrides: Mapping[Path, str] | None = None,
        environment: Environment,
        cache_dir: Path,
    ) -> Project:
        """Analyze `files` and `modules` (and unsaved `overrides`, which replace files on disk).

        Raises:
            AnalysisFailed: when a file can't be analyzed at all, e.g. a syntax error.
        """
        overrides = dict(overrides or {})
        options = _options(environment, cache_dir)
        if overrides:
            # mypy's native parser reads files from disk, ignoring the source text it's given.
            options.native_parser = False
        paths = list(dict.fromkeys([*files, *(p for p in overrides if p.is_file())]))
        try:
            sources = create_source_list([str(path) for path in paths], options)
        except InvalidSourceList as error:
            raise AnalysisFailed([Failure(paths[0], 1, 1, str(error))]) from error
        sources.extend(BuildSource(None, module) for module in modules)
        for source in sources:
            if source.path is not None:
                source.text = overrides.get(Path(source.path).resolve())

        # mypy reads installed packages from this interpreter's `sys.path`, so add the
        # project's environment to it for the duration of the build.
        saved = list(sys.path)
        sys.path.extend(d for d in environment.package_dirs if d not in sys.path)
        try:
            result = build.build(sources, options)
        except CompileError as error:
            raise AnalysisFailed(_failures(error.messages)) from error
        finally:
            sys.path[:] = saved

        # Modules that didn't change since the last run aren't loaded from the cache unless
        # something needs them; load all of them so every symbol table can be looked up.
        fresh = [
            module for module in result.graph if module not in result.manager.modules
        ]
        build.process_fresh_modules(result.graph, fresh, result.manager)
        project = cls(result, overrides)
        project._parse(paths)
        return project

    def _parse(self, paths: Sequence[Path]) -> None:
        """Parse the checked files up front, so a syntax error is a failure, not a crash."""
        failures = []
        for path in paths:
            module = self.module_for_path(path)
            if module is None:
                continue
            try:
                self.tree(module)
            except SyntaxError as error:
                failures.append(
                    Failure(
                        path.resolve(), error.lineno or 1, error.offset or 1, error.msg
                    )
                )
        if failures:
            raise AnalysisFailed(failures)

    def module_for_path(self, path: Path) -> str | None:
        return self._modules_by_path.get(path.resolve())

    def path_of(self, module: str) -> Path | None:
        state = self._graph.get(module)
        return Path(state.path).resolve() if state is not None and state.path else None

    def source(self, module: str) -> str:
        if module not in self._sources:
            path = self.path_of(module)
            if path is None:
                raise KeyError(module)
            text = self._overrides.get(path)
            self._sources[module] = (
                text if text is not None else path.read_text(encoding="utf-8")
            )
        return self._sources[module]

    def tree(self, module: str) -> ast.Module:
        if module not in self._trees:
            self._trees[module] = ast.parse(
                self.source(module), filename=str(self.path_of(module))
            )
        return self._trees[module]

    def module(self, module: str) -> MypyFile | None:
        return self._manager.modules.get(module)

    def lookup(self, module: str, name: str) -> SymbolTableNode | None:
        """`name` as seen at the top level of `module`, falling back to builtins."""
        tree = self.module(module)
        found = tree.names.get(name) if tree is not None else None
        if found is None:
            builtins = self.module("builtins")
            found = builtins.names.get(name) if builtins is not None else None
        return found

    def resolve(self, module: str, node: ast.expr) -> SymbolNode | None:
        """The definition a name or dotted name (`models.User`) in `module` refers to."""
        if isinstance(node, ast.Name):
            found = self.lookup(module, node.id)
            return found.node if found is not None else None
        if isinstance(node, ast.Attribute):
            base = self.resolve(module, node.value)
            if isinstance(base, MypyFile):
                member = base.names.get(node.attr)
                if member is None:
                    submodule = self.module(f"{base.fullname}.{node.attr}")
                    return submodule
                return member.node
            if isinstance(base, TypeInfo):
                member = base.names.get(node.attr)
                return member.node if member is not None else None
        return None

    def fullname(self, module: str, node: ast.expr) -> str | None:
        resolved = self.resolve(module, node)
        return resolved.fullname if resolved is not None else None

    def is_project_module(self, module: str) -> bool:
        """Whether `module` is the project's own code, not an installed package or stub."""
        state = self._graph.get(module)
        if state is None or not state.path:
            return False
        return not build.is_silent_import_module(self._manager, state.path)

    def class_def(self, info: TypeInfo) -> tuple[str, ast.ClassDef] | None:
        """The `class` statement that defines `info`, if it's at module or class level."""
        module = info.module_name
        if module not in self._graph or "@" in info.fullname:
            return None
        body: list[ast.stmt] = self.tree(module).body
        found: ast.ClassDef | None = None
        for name in info.fullname[len(module) + 1 :].split("."):
            found = next(
                (
                    statement
                    for statement in reversed(body)
                    if isinstance(statement, ast.ClassDef) and statement.name == name
                ),
                None,
            )
            if found is None:
                return None
            body = found.body
        return (module, found) if found is not None else None

    def named_type(self, fullname: str, args: Sequence[Type] = ()) -> Instance:
        module, _, name = fullname.rpartition(".")
        tree = self.module(module)
        found = tree.names.get(name) if tree is not None else None
        if found is None or not isinstance(found.node, TypeInfo):
            raise KeyError(fullname)
        return Instance(found.node, list(args))


def _options(environment: Environment, cache_dir: Path) -> Options:
    options = Options()
    # Only names, imports and annotations are needed, not checking function bodies.
    options.semantic_analysis_only = True
    options.incremental = True
    options.cache_dir = str(cache_dir)
    # The running interpreter: mypy then reads installed packages from `sys.path` instead
    # of running the project's interpreter to ask it.
    options.python_executable = sys.executable
    options.python_version = max(environment.python_version, (3, 10))
    options.follow_imports = "silent"
    options.follow_untyped_imports = True
    options.ignore_missing_imports = True
    options.show_column_numbers = True
    return options


def _failures(messages: Sequence[str]) -> list[Failure]:
    failures = []
    for message in messages:
        found = _BLOCKER.match(message)
        if found is None:
            continue
        failures.append(
            Failure(
                Path(found["path"]).resolve(),
                int(found["line"]),
                int(found["column"] or 1),
                found["message"],
            )
        )
    if not failures:
        failures.append(Failure(Path.cwd(), 1, 1, " ".join(messages) or "mypy failed"))
    return failures
