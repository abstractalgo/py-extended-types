# py-extended-types

`@assert_shape` contracts for Python classes: declare that a class has exactly the
fields of another, or of a `Partial` / `Required` / `Omit` / `Pick` of it. `@assert_field`
does the same for one field's type, like TypeScript's `T["key"]`.

Checking is **static**: `py-extended-types check` reads your code with mypy's semantic
analysis and never imports or runs it, so modules with import-time side effects are as
safe to check as any other.

Supported classes are Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs
classes and plain annotated classes, on either side of the contract. In a plain class, a
field with a class-level value has that default and one without is required (`ClassVar`s
are skipped). Classes these rules can't read reliably are reported as `invalid-shape`
rather than guessed at: anything with its own metaclass (SQLAlchemy's declarative base,
`Protocol`, `Enum`, `ABC`), deriving from a class of the standard library or an installed
package, or with a descriptor in place of a field.

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
`invalid-shape`, `analysis-error` (a file that can't be analyzed, e.g. a syntax error).

`--format json` emits the same diagnostics for editors, each with an optional
`fix` (`title` + text `edits`) that makes the field match the shape. With `--stdin`,
it reads a JSON object of `{path: source}` and checks those sources instead of the
files on disk (editors use this for unsaved buffers; other modules importing them
see the unsaved version too).

## Installing

```bash
uv add py-extended-types
```

The package depends on mypy, which the checker builds on. Your app only imports the
decorators and helpers, which are no-ops at runtime and don't load mypy.

## Semantics

| Helper | Effect |
| --- | --- |
| `Partial(T, nullable=True)` | every field optional; widens to `T \| None` unless `nullable=False` |
| `Required(T)` | every field required and non-nullable: `T \| None` becomes `T` |
| `Pick(T, keys)` / `Omit(T, keys)` | keep / drop fields; `keys` is one name or a list of them; unknown keys are an error |
| `Record(keys, value, *, name=None)` | a required field of type `value` per key, like TypeScript's `Record<K, V>`: `keys` is a `Literal[...]`, `KeyOf(T)` or a list of names |
| `KeyOf(T)` | `T`'s field names, like `keyof T`, as `Record`'s keys |

Helpers compose: `Partial(Omit(User, "id"))`.

A shape expression is read from the source, not run, so it may only contain:

- classes, by name, dotted name (`models.User`) or import alias,
- the helpers above,
- literal arguments: strings, lists of strings, `True` / `False`, `Literal[...]`, and types
  such as `list[int] | None` for `Record`'s value,
- module-level names bound to any of these, in any module: `Public = Omit(User,
  "password")`, `KEYS = ["id", "name"]`.

Anything else, such as calling your own function or passing a `lambda`, is an
`invalid-shape` problem.

## Matching

A class matches a shape when:

- it has **every field** of the shape, required or optional,
- each field's type is **equivalent** to the shape's, as mypy decides it: `Optional[str]`
  and `str | None` match, `Annotated` metadata is ignored, but narrower types such as
  `Literal["a"]` for `str` or `bool` for `int` do not,
- each field's **required-ness** is the same: an optional field in the shape needs
  a default here, and a required one must not have one,
- there are **no extra fields** (`exact=False` to permit them).

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

from py_extended_types import KeyOf, Omit, Partial, Pick, Record, Required, assert_shape

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

Both `Pick` and `Omit` take one name or a list: `Omit(User, ["id", "password"])`. They
report a misspelled key (`Omit(User, "idd")`) as `invalid-shape`, listing the fields that do
exist.

### `Record`

One required field per key, all of the same type:

```python
@assert_shape(Record(Literal["en", "de"], str))
class Translations(BaseModel):
    en: str
    de: str
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

### Composing and naming shapes

Each helper accepts a class or another helper's result, and a shape bound to a
module-level name can be reused anywhere:

```python
Public = Omit(User, "password")

@assert_shape(Partial(Omit(Public, "id")))
class ProfilePatch(BaseModel):
    name: str | None = None
    email: str | None = None
    tags: list[str] | None = None
```

`py-extended-types show 'app.models:Partial(Omit(User, "id"))'` prints the fields of any of
these shapes.

## CLI

```
py-extended-types check [PATHS...] [-v]           # verify @assert_shape / @assert_field declarations in PATHS (default .); -v prints a summary on success
py-extended-types show 'pkg.mod:Partial(User)'    # print a class's / shape's fields
```

The checker analyzes the files under `PATHS` that mention `assert_shape(` or
`assert_field(`, and `py_extended_types`, plus everything they import. It finds your
project's installed packages in its environment (`--python`, else `$VIRTUAL_ENV`, else the
nearest `.venv`) by reading the venv's `site-packages` and `.pth` files, without running
its interpreter; `--no-venv` uses the checker's own environment instead.

The first run analyzes the standard library and your dependencies, which takes a second
or two; later runs reuse mypy's cache in `~/.cache/py-extended-types` (or
`$XDG_CACHE_HOME`, or `$PY_EXTENDED_TYPES_CACHE_DIR`) and take a fraction of that.

## Limitations

**Type checkers don't know about shapes.** mypy and Pylance see `Partial(User)` as an
opaque `Shape` object, so they never report a mismatch and can't autocomplete or validate
field names: `Pick(User, "nmae")` is only caught by `py-extended-types check`. Python's
type system has no `keyof` or mapped types (PEP 827 proposes them), so this can't be fixed
inside the library.

**Shapes are read, never run.** There are no custom helpers: a shape can't be built by a
function of yours, and `Record`'s value is a type, not a function of the key. There is no
equivalent of TypeScript's key remapping (`[K in keyof T as ...]`) either.

**Only some declarations are found.** Classes are found at the top level of a module or
nested in other classes; a class defined inside a function or an `if` block (including
`if TYPE_CHECKING:`) isn't checked. Files are skipped unless they contain `assert_shape(`
or `assert_field(`, and `py_extended_types`, so re-exporting the decorators from another
module of yours hides the files that use it.

**Installed packages are found from files.** Packages are read from the environment's
`site-packages` and the plain directory entries of its `.pth` files. Editable installs
that work through an import hook (some setuptools versions) aren't followed; a class from
such a package can't be resolved and is reported as `invalid-shape`.

**Type comparison is mypy's.**
- Fields match only when their types are equivalent, i.e. each is assignable to the other.
  `bool` doesn't match `int`, and `int` doesn't match `float`, even though a type checker
  accepts either in place of the second.
- `Any` matches anything, so a field typed with `Any` isn't really checked.
- A field annotated with an undefined name is an `invalid-shape` problem, but an undefined
  name nested in a type (`list["Missing"]`) becomes `Any` and matches anything.

**Only names, types and required-ness are compared.**
- Default values aren't compared: `x: int = 0` and `x: int = 1` match.
- `Field(...)` arguments such as `alias` or `max_length` aren't compared.
- `Final` / `ReadOnly` qualifiers aren't compared.
- Field order doesn't matter.

**Some classes can't be read.**
- Supported: Pydantic models, dataclasses, TypedDicts, NamedTuples, attrs classes and
  plain annotated classes. Anything else is an `invalid-shape` problem, including
  SQLAlchemy models, `Protocol`s, `Enum`s and plain classes deriving from a class of the
  standard library or an installed package.
- In a plain class, attributes assigned in `__init__` aren't fields, and a field whose
  default is an instance of a descriptor class is rejected.

**Quick fixes are textual.**
- A fix writes types the way the checker prints them, e.g. `list[Foo] | None`, so the file
  may need an import it doesn't have yet.
- No fix is offered when it can't be done safely, e.g. removing a default from inside
  `Field(...)`.

## Development

```bash
uv sync                                       # dev tools: pytest, ruff, mypy, pydantic, attrs
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
uv tool install --editable .                  # optional global `py-extended-types`
```

The VS Code extension's version (`vscode/package.json`) always matches the package's
(`pyproject.toml`); bump both together.

The runtime API (`core.py`, `shapes.py`) must only import the standard library; the checker
lives in `analysis/` and builds on mypy (`analysis/project.py` runs its semantic analysis).

Projects using it can depend on this repo by path while developing it, e.g. with uv:
`py-extended-types = { path = "../py-extended-types", editable = true }` under `[tool.uv.sources]`.
