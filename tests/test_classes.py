from pathlib import Path

import pytest
from helpers import fields, shape

from py_extended_types.analysis.errors import ShapeError
from py_extended_types.analysis.fields import HAS_DEFAULT, NO_DEFAULT


def test_fields_of_a_pydantic_model(tmp_path: Path) -> None:
    # Given
    source = """
class Base(BaseModel):
    id: int
    name: str = "base"

class User(Base):
    name: str = Field(description="Display name", max_length=50)
    email: str | None = None
    tags: list[str] = Field(default_factory=list)
    secret: str = Field("", exclude=True)
    token: str = Field(...)
    score: Annotated[int, Field(default=3)]
    created_at: Final[str]
    kind: ClassVar[str] = "user"
    _cache: dict[str, str] = {}
"""

    # Then
    assert fields(tmp_path, source, "User") == {
        "id": ("int", NO_DEFAULT),
        "name": ("str", NO_DEFAULT),
        "email": ("str | None", None),
        "tags": ("list[str]", HAS_DEFAULT),
        "secret": ("str", ""),
        "token": ("str", NO_DEFAULT),
        "score": ("int", 3),
        "created_at": ("str", NO_DEFAULT),
    }


def test_fields_of_dataclasses_and_typeddicts(tmp_path: Path) -> None:
    # Given
    source = """
from typing import NotRequired
from typing_extensions import ReadOnly

@dataclasses.dataclass
class Point:
    x: Final[int]
    y: int = 0
    labels: list[str] = dataclasses.field(default_factory=list)
    z: float = dataclasses.field(default=1.5)
    scale: dataclasses.InitVar[int] = 1
    origin: ClassVar[str] = "0"

class Movie(TypedDict):
    title: ReadOnly[str]
    year: NotRequired[int]
"""

    # Then
    assert fields(tmp_path, source, "Point") == {
        "x": ("int", NO_DEFAULT),
        "y": ("int", 0),
        "labels": ("list[str]", HAS_DEFAULT),
        "z": ("float", 1.5),
    }
    assert fields(tmp_path, source, "Movie") == {
        "title": ("str", NO_DEFAULT),
        "year": ("int", HAS_DEFAULT),
    }


def test_fields_of_namedtuples_and_attrs_classes(tmp_path: Path) -> None:
    # Given
    source = """
import attrs

class Pair(NamedTuple):
    left: int
    right: str = ""

@attrs.define
class Item:
    sku: str
    count: int = 0
    tags: list[str] = attrs.Factory(list)
    size: int = attrs.field(default=1)
"""

    # Then
    assert fields(tmp_path, source, "Pair") == {
        "left": ("int", NO_DEFAULT),
        "right": ("str", ""),
    }
    assert fields(tmp_path, source, "Item") == {
        "sku": ("str", NO_DEFAULT),
        "count": ("int", 0),
        "tags": ("list[str]", HAS_DEFAULT),
        "size": ("int", 1),
    }


def test_fields_of_plain_classes(tmp_path: Path) -> None:
    # Given
    source = """
class Base:
    id: int
    kind: ClassVar[str] = "base"

class Plain(Base):
    name: str = ""
    tags: list[str] | None = None
"""

    # Then
    assert fields(tmp_path, source, "Plain") == {
        "id": ("int", NO_DEFAULT),
        "name": ("str", ""),
        "tags": ("list[str] | None", None),
    }


def test_classes_can_come_from_other_modules(tmp_path: Path) -> None:
    # Given
    (tmp_path / "base_models.py").write_text(
        "from pydantic import BaseModel\n\n\nclass Account(BaseModel):\n    id: int\n"
    )
    source = "import base_models\nfrom base_models import Account as Renamed\n"

    # Then
    assert fields(tmp_path, source, "base_models.Account") == {
        "id": ("int", NO_DEFAULT)
    }
    assert repr(shape(tmp_path, source, "Renamed")) == "Account"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "class Target(Protocol):\n    id: int\n",
            "it's a Protocol",
        ),
        (
            "import enum\nclass Target(enum.Enum):\n    A = 1\n",
            "its metaclass EnumMeta may store fields its own way",
        ),
        (
            "from abc import ABC\nclass Target(ABC):\n    id: int\n",
            "its metaclass ABCMeta may store fields its own way",
        ),
        (
            "from argparse import Namespace\nclass Target(Namespace):\n    id: int\n",
            "it derives from argparse.Namespace, which py-extended-types can't read",
        ),
        (
            "class Desc:\n    def __get__(self, obj, owner): return 1\n"
            "class Target:\n    id: int = Desc()\n",
            "field 'id' is a descriptor, not a plain default",
        ),
        (
            "class Target(missing_base):\n    id: int\n",
            "its base class missing_base can't be resolved",
        ),
    ],
)
def test_classes_that_cant_be_read_reliably_are_shape_errors(
    tmp_path: Path, source: str, reason: str
) -> None:
    with pytest.raises(ShapeError, match=f"can't read the fields of Target: {reason}"):
        shape(tmp_path, source, "Target")


def test_unresolvable_annotations_are_shape_errors(tmp_path: Path) -> None:
    # Given
    source = """
@dataclasses.dataclass
class Forward:
    other: "Missing"
"""

    # When / Then
    with pytest.raises(
        ShapeError, match="can't resolve the annotation of Forward.other"
    ):
        shape(tmp_path, source, "Forward")


def test_classes_in_functions_cant_be_read(tmp_path: Path) -> None:
    # Given
    source = """
def make():
    class Local(BaseModel):
        id: int
    return Local

Made = make()
"""

    # When / Then
    with pytest.raises(ShapeError, match="can't read make\\(\\) statically"):
        shape(tmp_path, source, "Made")
