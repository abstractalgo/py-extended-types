"""`@assert_shape` / `@assert_field` contracts for Python classes, checked statically by
`py-extended-types check`."""

from py_extended_types.core import assert_field, assert_shape
from py_extended_types.shapes import (
    KeyOf,
    Omit,
    Partial,
    Pick,
    Record,
    Required,
    Shape,
)

__all__ = [
    "KeyOf",
    "Omit",
    "Partial",
    "Pick",
    "Record",
    "Required",
    "Shape",
    "assert_field",
    "assert_shape",
]
