from demo_pkg.models import User
from py_extended_types import assert_shape


@assert_shape(User)
class Broken(missing_base):  # type: ignore[name-defined]  # noqa: F821
    id: int
