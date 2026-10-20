from pydantic import BaseModel

from py_extended_types import Omit, Partial, assert_shape


class User(BaseModel):
    id: int
    name: str


@assert_shape(Partial(User))
class PartialUser(BaseModel):
    id: int | None = None
    name: str | None = None


@assert_shape(Omit(User, "id"))
class CreateUser(BaseModel):
    name: int
