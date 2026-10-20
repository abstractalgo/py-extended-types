"""`@expect_shape` contracts for Python classes, with shape helpers built on `Map` / `Filter`."""

from py_sat.core import Problem, check, expect_shape, registrations
from py_sat.shapes import (
    HAS_DEFAULT,
    NO_DEFAULT,
    Factory,
    Filter,
    KeyOf,
    Map,
    Member,
    Omit,
    Partial,
    Pick,
    Record,
    Required,
    Shape,
    ShapeError,
)
from py_sat.typecheck import (
    add_none,
    includes_none,
    is_assignable,
    is_equivalent,
    remove_none,
)

__all__ = [
    "HAS_DEFAULT",
    "NO_DEFAULT",
    "Factory",
    "Filter",
    "KeyOf",
    "Map",
    "Member",
    "Omit",
    "Partial",
    "Pick",
    "Problem",
    "Record",
    "Required",
    "Shape",
    "ShapeError",
    "add_none",
    "check",
    "expect_shape",
    "includes_none",
    "is_assignable",
    "is_equivalent",
    "registrations",
    "remove_none",
]
