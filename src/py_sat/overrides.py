"""Import modules from in-memory source instead of disk, for checking unsaved editor buffers.

`install({"/abs/path/models.py": "<source>"})` makes any import that resolves to one of
those files use the given source. Bytecode caching is bypassed (a cached `.pyc` matches
the file on disk, not the buffer), and `linecache` is primed so `inspect`, tracebacks and
the reporter see the buffer's lines.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import linecache
import sys
from collections.abc import Mapping, Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType


class _OverrideLoader(importlib.abc.SourceLoader):
    # No `path_stats`/`set_data`: SourceLoader then neither reads nor writes bytecode.
    def __init__(self, filename: str, source: str) -> None:
        self._filename = filename
        self._source = source

    def get_filename(self, fullname: str) -> str:
        return self._filename

    def get_data(self, path: str) -> bytes:
        return self._source.encode("utf-8")


class _OverrideFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources: Mapping[Path, str]) -> None:
        self._sources = sources

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None:
            return None
        source = self._sources.get(Path(spec.origin).resolve())
        if source is None:
            return None
        _prime_linecache(spec.origin, source)
        return importlib.util.spec_from_file_location(
            fullname,
            spec.origin,
            loader=_OverrideLoader(spec.origin, source),
            submodule_search_locations=spec.submodule_search_locations,
        )


def read(stream_text: str) -> dict[Path, str]:
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


def install(sources: Mapping[Path, str]) -> None:
    if not sources:
        return
    for path, source in sources.items():
        _prime_linecache(str(path), source)
    sys.meta_path.insert(0, _OverrideFinder(sources))


def _prime_linecache(filename: str, source: str) -> None:
    lines = source.splitlines(keepends=True)
    # An mtime of None makes `linecache.checkcache` keep the entry instead of re-reading disk.
    linecache.cache[filename] = (len(source), None, lines, filename)
