import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from py_extended_types.cli import CACHE_DIR_VAR


@pytest.fixture(autouse=True, scope="session")
def _mypy_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    # One mypy cache for the whole run (also inherited by CLI subprocesses), so the
    # standard library and Pydantic are only analyzed once.
    cache = tmp_path_factory.mktemp("mypy-cache")
    previous = os.environ.get(CACHE_DIR_VAR)
    os.environ[CACHE_DIR_VAR] = str(cache)
    yield cache
    if previous is None:
        del os.environ[CACHE_DIR_VAR]
    else:
        os.environ[CACHE_DIR_VAR] = previous
