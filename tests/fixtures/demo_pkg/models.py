from pydantic import BaseModel

from py_sat import Omit, Partial, expect_shape


class User(BaseModel):
    id: int
    name: str


@expect_shape(Partial(User))
class PartialUser(BaseModel):
    id: int | None = None
    name: str | None = None


@expect_shape(Omit(User, "id"))
class CreateUser(BaseModel):
    name: int
