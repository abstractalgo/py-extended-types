# py-extended-types

`@assert_shape` contracts for Python classes: declare that a class has exactly the
fields of another, or of a `Partial` / `Required` / `Omit` / `Pick` of it. `@assert_field`
does the same for one field's type, like TypeScript's `T["key"]`.

Supported classes are Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs
classes and plain annotated classes, on either side of the contract. In a plain class, a
field with a class-level value has that default and one without is required (`ClassVar`s
are skipped). Classes these rules can't read reliably are reported as `invalid-shape`
rather than guessed at: anything with its own metaclass (SQLAlchemy's declarative base,
`Protocol`, `Enum`) or with a descriptor in place of a field (SQLAlchemy-mapped classes,
`__slots__`, properties).

This repo holds the Python package (`src/`, `tests/`) and its VS Code extension
([`vscode/`](vscode/README.md)), which shows the checker's problems and quick fixes in the editor.

```python
from py_extended_types import Omit, Partial, Pick, Required, assert_shape

@assert_shape(Partial(User))
class UserPatch(BaseModel):
    id: int | None = None
    name: str | None = None

@assert_shape(Omit(User, ["id", "created_at"]))
class CreateUser(BaseModel): ...
```

`@assert_shape` (and `@assert_field`) do nothing at runtime; `py-extended-types check` verifies them:

```
$ py-extended-types check src/
src/app/models.py
  15:1  error  CreateUser is missing required field 'email: str' from Omit[User, 'id' | 'created_at']  py-extended-types(missing-field)
  17:5  error  CreateUser.name has type 'int', expected 'str'                                        py-extended-types(type-mismatch)

✖ 2 problems (2 errors, 0 warnings)
```

Output uses ESLint's [`stylish`](https://oxc.rs/docs/guide/usage/linter/output-formats.html#format-stylish)
format: problems grouped by file, pinned to the field's line (or the decorator's,
for missing fields), coloured on a TTY (`NO_COLOR` / `FORCE_COLOR` override).
Nothing is printed when everything matches; exit code is 1 on any problem.
Rules: `type-mismatch`, `required-mismatch`, `missing-field`, `unexpected-field`,
`invalid-shape`, `import-error`.

`--format json` emits the same diagnostics for editors, each with an optional
`fix` (`title` + text `edits`) that makes the field match the shape. With `--stdin`,
it reads a JSON object of `{path: source}` and checks those sources instead of the
files on disk (editors use this for unsaved buffers; other modules importing them
see the unsaved version too).

## Semantics

| Helper | Effect |
| --- | --- |
| `Partial(T, nullable=True)` | every field optional; widens to `T \| None` unless `nullable=False` |
| `Required(T)` | every field required and non-nullable: `T \| None` becomes `T` |
| `Pick(T, keys)` / `Omit(T, keys)` | keep / drop fields; `keys` is one name or a list of them; unknown keys are an error |
| `Record(keys, value)` | a required field per key, like TypeScript's `Record<K, V>`: `keys` is a `Literal[...]`, `KeyOf(T)` or an iterable of names; `value` is a type, or a function of the key returning one |
| `KeyOf(T)` | `T`'s field names, like `keyof T`; read lazily, so it's a placeholder rather than a `Literal` (`.literal` builds one) |

Helpers compose: `Partial(Omit(User, "id"))`.

## Your own helpers: `Map` and `Filter`

`Partial`, `Required`, `Pick` and `Omit` are built from two primitives, which you can use too:

| Primitive | Effect |
| --- | --- |
| `Map(T, fn, *, name=None)` | replace every field with `fn(member)`, in order; it can't drop or rename fields |
| `Filter(T, predicate, *, name=None)` | keep the fields where `predicate(member)` is true |

`name` is how the shape appears in messages (otherwise `Map[User, <lambda>]`). Both
receive a `Member` for each field:

| Attribute | |
| --- | --- |
| `name` | the field's name |
| `annotation` | its type, e.g. `str \| None`; `Final` / `ReadOnly` are moved to `quals` |
| `quals` | `frozenset` of `"Final"` / `"ReadOnly"` |
| `default` | `NO_DEFAULT` (required), `HAS_DEFAULT` (optional, value unknown), `Factory(fn)`, or the value |
| `field` | Pydantic `Field(...)` keyword arguments, including constraints (`{"alias": ..., "max_length": 50}`); empty otherwise |
| `required` | `default is NO_DEFAULT` |
| `replace(...)` | a copy with `annotation`, `quals`, `default` or `field` changed |

```python
from py_extended_types import Filter, Map, Partial, add_none, assert_shape, is_assignable

def Nullable(T):  # `T | None`, still required
    return Map(T, lambda m: m.replace(annotation=add_none(m.annotation)), name="Nullable")

Public = Filter(User, lambda m: not m.field.get("exclude"), name="Public[User]")
Strings = Filter(User, lambda m: is_assignable(m.annotation, str))

@assert_shape(Partial(Public))
class UserPatch(BaseModel): ...
```

`add_none`, `remove_none`, `includes_none`, `is_assignable` and `is_equivalent` are
exported for use in these functions. An exception raised in one is reported as an
`invalid-shape` problem.

## Matching

A class matches a shape when:

- it has **every field** of the shape, required or optional,
- each field's type is **equivalent** to the shape's: `Optional[str]` and
  `str | None` match, `Annotated` metadata is ignored, but narrower types such as
  `Literal["a"]` for `str` or `bool` for `int` do not,
- each field's **required-ness** is the same: an optional field in the shape needs
  a default here, and a required one must not have one,
- there are **no extra fields** (`exact=False` to permit them).

py-extended-types is a dev-time tool only: in the running app `@assert_shape` / `@assert_field`
return the class untouched and record nothing, and shapes are lazy, so no fields are read and no `Map` /
`Filter` function is called. Only `py-extended-types check` (which switches collection on before
importing your modules) gathers and verifies the declarations.

## One field: `@assert_field`

`@assert_field(name, T, key)` declares that the class's field `name` has the same
type as `T`'s field `key`, like `name: T["key"]` in TypeScript. Only the type is compared,
with the same equivalence as above; required-ness and other fields are not checked. Stack
one decorator per field, and add or remove them without touching the fields themselves:

```python
from py_extended_types import assert_field

@assert_field("owner_name", User, "name")
@assert_field("owner_email", User, "email")
class Project(BaseModel):
    title: str
    owner_name: str
    owner_email: str | None = None
```

`T` can be a class or any shape (`Partial(User)`'s `"name"` is `str | None`).
A misspelled key is reported as `invalid-shape`, a missing field as `missing-field` and
a different type as `type-mismatch`.

## Examples

Every example uses this model, and each class below passes `py-extended-types check`:

```python
from typing import Literal

from pydantic import BaseModel, Field

from py_extended_types import (
    Filter, KeyOf, Map, Omit, Partial, Pick, Record, Required, add_none, assert_shape,
)

class User(BaseModel):
    id: int
    name: str
    email: str | None = None
    tags: list[str] = Field(default_factory=list)
    password: str = Field(exclude=True)
```

### `Partial`

Every field becomes optional and nullable, the usual shape of a PATCH body:

```python
@assert_shape(Partial(User))
class UserPatch(BaseModel):
    id: int | None = None
    name: str | None = None
    email: str | None = None
    tags: list[str] | None = None
    password: str | None = None
```

With `nullable=False`, fields only need a default and keep their types:

```python
@assert_shape(Partial(User, nullable=False))
class UserDefaults(BaseModel):
    id: int = 0
    name: str = ""
    email: str | None = None
    tags: list[str] = []
    password: str = ""
```

### `Required`

Every field becomes required, and `None` is removed from its type:

```python
@assert_shape(Required(User))
class CompleteUser(BaseModel):
    id: int
    name: str
    email: str  # was `str | None = None`
    tags: list[str]
    password: str
```

### `Pick`

Only the listed fields are kept, with their types and defaults:

```python
@assert_shape(Pick(User, ["id", "name"]))
class UserSummary(BaseModel):
    id: int
    name: str
```

### `Omit`

The listed fields are dropped; everything else stays as it is:

```python
@assert_shape(Omit(User, "id"))
class CreateUser(BaseModel):
    name: str
    email: str | None = None
    tags: list[str] = []
    password: str
```

Both `Pick` and `Omit` take one name or a list: `Omit(User, ["id", "password"])`. They report a misspelled key (`Omit(User, "idd")`) as `invalid-shape`, listing
the fields that do exist.

### `Record`

One required field per key, all of the same type:

```python
@assert_shape(Record(Literal["en", "de"], str))
class Translations(BaseModel):
    en: str
    de: str
```

Or with a function that picks each field's type from its key:

```python
@assert_shape(Record(["count", "label"], lambda key: str if key == "label" else int))
class Counter(BaseModel):
    count: int
    label: str
```

### `KeyOf`

Another class's (or shape's) field names, as keys for `Record`:

```python
@assert_shape(Record(KeyOf(Omit(User, "password")), bool))
class UserFieldVisibility(BaseModel):
    id: bool
    name: bool
    email: bool
    tags: bool
```

`KeyOf(User).literal` gives `Literal['id', 'name', 'email', 'tags', 'password']`, read when
you access it.

### `Map`

Changes every field with a function of its `Member`. Here each type gets `| None`, but
required fields stay required:

```python
Nullable = Map(User, lambda m: m.replace(annotation=add_none(m.annotation)), name="Nullable[User]")

@assert_shape(Nullable)
class NullableUser(BaseModel):
    id: int | None
    name: str | None
    email: str | None = None
    tags: list[str] | None = []
    password: str | None
```

### `Filter`

Keeps the fields its predicate accepts. Here it drops fields excluded from serialization:

```python
Public = Filter(User, lambda m: not m.field.get("exclude"), name="Public[User]")

@assert_shape(Public)
class UserResponse(BaseModel):
    id: int
    name: str
    email: str | None = None
    tags: list[str] = []
```

### Composing

Each helper accepts a class or another helper's result:

```python
@assert_shape(Partial(Omit(User, ["id", "password"])))
class ProfilePatch(BaseModel):
    name: str | None = None
    email: str | None = None
    tags: list[str] | None = None
```

`py-extended-types show 'app.models:Partial(Omit(User, "id"))'` prints the fields of any of
these shapes. In VS Code, Pylance doesn't autocomplete inside a class decorator's
arguments, so write shapes that use a `lambda` as module-level variables (like `Nullable`
and `Public` above) to get completions for `m`.

## CLI

```
py-extended-types check [PATHS...] [-v]           # verify @assert_shape / @assert_field declarations in PATHS (default .); -v prints a summary on success
py-extended-types show 'pkg.mod:Partial(User)'    # print a class's / shape's fields
```

The checker imports your modules, so it runs under **your project's**
interpreter: `--python`, else `$VIRTUAL_ENV`, else the nearest `.venv`. It puts
this package on `PYTHONPATH` (it has no dependencies), so the project doesn't
need it installed just to run the check. `--no-venv` disables this. Only files
mentioning `assert_shape(` or `assert_field(`, and `py_extended_types`, are imported.

## Limitations

**Type checkers don't know about shapes.** mypy and Pylance see `Partial(User)` as an
opaque `Shape` object, so they never report a mismatch and can't autocomplete or validate
field names: `Pick(User, "nmae")` is only caught by `py-extended-types check`. Python's
type system has no `keyof` or mapped types (PEP 827 proposes them), so this can't be fixed
inside the library. In VS Code, Pylance also shows `Unknown` for names inside a class
decorator's arguments; define shapes that use a `lambda` as module-level variables.

**Checking runs your code.** The CLI imports every module it checks, which executes their
top-level code with your project's interpreter. Modules with import-time side effects
(connecting to services, reading required environment variables) run those too, and a
module that fails to import is reported as `import-error` instead of being checked.

**Only declarations that run on import are checked.** `@assert_shape` and `@assert_field` record a class when
the decorator executes, so a class defined inside a function, or under
`if TYPE_CHECKING:`, is never seen. Files are also skipped unless they contain
`assert_shape(` or `assert_field(`, and `py_extended_types`, so re-exporting the decorators from another module
of yours hides the files that use it.

**Type comparison approximates mypy and pyright.**
- Fields match only when their types are equivalent, i.e. each is assignable to the other.
  `bool` doesn't match `int`, and `int` doesn't match `float`, even though a type checker
  accepts either in place of the second.
- `Any`, `TypeVar`s and forward references that can't be resolved match anything, so
  generic fields aren't really checked.
- `Protocol`s are compared by class, not structurally: a class that implements a protocol
  doesn't match the protocol type.

**Only names, types and required-ness are compared.**
- Default values aren't compared: `x: int = 0` and `x: int = 1` match.
- `Field(...)` arguments such as `alias` or `max_length` aren't compared (`Filter` and
  `Map` can read them).
- `Final` / `ReadOnly` qualifiers are recorded in `Member.quals` but not compared.
- Field order doesn't matter.

**Some classes can't be read.**
- Supported: Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs classes and
  plain annotated classes. Anything else is an `invalid-shape` problem, including
  SQLAlchemy models, `Protocol`s and `Enum`s.
- In a plain class, attributes assigned in `__init__` aren't fields, and a field whose
  default is a function is rejected (functions are descriptors, like the ones frameworks
  put in place of fields).
- Pydantic's `Field(...)` arguments come from its private `FieldInfo._attributes_set`.
  If a Pydantic release changes it, `Member.field` comes back empty.

**Helpers can't rename fields.** `Map` changes a field's type, default or qualifiers but
keeps its name, and there is no equivalent of TypeScript's key remapping
(`[K in keyof T as ...]`).

**Quick fixes are textual.**
- A fix writes types the way the checker prints them, e.g. `list[Foo] | None`, so the file
  may need an import it doesn't have yet.
- No fix is offered when it can't be done safely, e.g. removing a default from inside
  `Field(...)`.

**It isn't free at import.** `@assert_shape` does nothing at runtime, but its argument is
still evaluated: `import py_extended_types` takes about 2 ms, and each decorator builds a
few small, lazy objects. No fields are read and no `Map` / `Filter` / `Record` function is
called.

## Development

```bash
uv sync                                       # dev tools: pytest, ruff, mypy, pydantic
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
uv tool install --editable .                  # optional global `py-extended-types`
```

Projects using it can depend on this repo by path while developing it, e.g. with uv:
`py-extended-types = { path = "../py-extended-types", editable = true }` under `[tool.uv.sources]`.
