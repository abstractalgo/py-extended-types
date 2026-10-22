class ShapeError(Exception):
    """A shape or class that can't be read, e.g. `Omit(User, "typo")`: an `invalid-shape` problem."""
