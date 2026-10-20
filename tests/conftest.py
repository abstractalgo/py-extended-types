import pytest

from py_extended_types.core import start_collecting


@pytest.fixture(autouse=True)
def _collect_declarations() -> None:
    # These tests decorate classes in-process, as `py-extended-types check` would.
    start_collecting()
