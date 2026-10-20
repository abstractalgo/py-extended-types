from collections.abc import Sequence
from typing import Annotated, Any, Literal, Optional

import pytest

from py_extended_types.typecheck import format_type, is_assignable


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (int, int, True),
        (bool, int, True),
        (int, float, True),
        (float, int, False),
        (int, int | None, True),
        (int | None, int, False),
        (Optional[str], str | None, True),
        (Literal["a"], str, True),
        (Literal["a", 1], str, False),
        (Literal["a"], Literal["a", "b"], True),
        (str, Literal["a"], False),
        (list[int], list[int], True),
        (list[bool], list[int], False),
        (list[bool], Sequence[int], True),
        (dict[str, int], dict[str, Any], True),
        (tuple[int, int], tuple[int, ...], True),
        (tuple[int, ...], tuple[int, int], False),
        (Annotated[int, "meta"], int, True),
        (Any, int, True),
        (str, object, True),
    ],
)
def test_is_assignable(source: Any, target: Any, expected: bool) -> None:
    assert is_assignable(source, target) is expected


def test_format_type() -> None:
    assert format_type(dict[str, list[int | None]]) == "dict[str, list[int | None]]"
    assert format_type(Optional[int]) == "int | None"
