from demo_pkg.models import User
from py_extended_types import expect_shape


@expect_shape(User)
class Broken(missing_base):  # type: ignore[name-defined]  # noqa: F821
    id: int
