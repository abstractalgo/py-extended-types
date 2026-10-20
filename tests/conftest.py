import pytest

from py_sat.core import start_collecting


@pytest.fixture(autouse=True)
def _collect_declarations() -> None:
    # These tests decorate classes in-process, as `py-sat check` would.
    start_collecting()
