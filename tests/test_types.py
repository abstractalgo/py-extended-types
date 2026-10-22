from pathlib import Path

from helpers import fields, problems

# (field type in the source class, field type in the checked class, whether they match)
PAIRS = [
    ("int", "int", True),
    ("bool", "int", False),
    ("int", "float", False),
    ("Optional[str]", "str | None", True),
    ("Literal['a']", "str", False),
    ("Literal['a', 'b']", "Literal['b'] | Literal['a']", True),
    ("list[int]", "list[int]", True),
    ("list[bool]", "list[int]", False),
    ("dict[str, int]", "dict[str, Any]", True),
    ("tuple[int, int]", "tuple[int, ...]", False),
    ("Annotated[int, 'meta']", "int", True),
    ("Any", "int", True),
    ("str", "object", False),
    ("Ids", "list[int]", True),
    ("UserId", "int", False),
    ("'Other'", "Other", True),
]


def test_field_types_match_when_mypy_finds_them_equivalent(tmp_path: Path) -> None:
    # Given
    source = "from typing import Any, NewType\n\nIds = list[int]\nUserId = NewType('UserId', int)\n"
    source += "\nclass Other(BaseModel):\n    id: int\n"
    for index, (left, right, _) in enumerate(PAIRS):
        source += (
            f"\nclass Source{index}(BaseModel):\n    x: {left}\n"
            f"\n@assert_field('x', Source{index}, 'x')\nclass Checked{index}(BaseModel):\n    x: {right}\n"
        )

    # When
    found = problems(tmp_path, source)

    # Then
    assert {
        (left, right): not found[f"Checked{index}"]
        for index, (left, right, _) in enumerate(PAIRS)
    } == {(left, right): matches for left, right, matches in PAIRS}


def test_types_are_printed_as_written(tmp_path: Path) -> None:
    # Given
    source = """
class Model(BaseModel):
    nested: dict[str, list[int | None]]
    optional: Optional[int]
    literal: Literal["a", "b"]
"""

    # Then
    assert {name: t for name, (t, _) in fields(tmp_path, source, "Model").items()} == {
        "nested": "dict[str, list[int | None]]",
        "optional": "int | None",
        "literal": "Literal['a', 'b']",
    }
