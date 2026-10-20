# py-extended-types

`@assert_shape` contracts for Python classes (Pydantic models, dataclasses, TypedDicts,
annotated classes): declare that a class has exactly the fields of another, or of
a `Partial` / `Required` / `Omit` / `Pick` of it.

This repo holds the Python package (`src/`, `tests/`) and its VS Code extension
([`vscode/`](vscode/README.md)), which shows the checker's problems and quick fixes in the editor.

```python
from py_extended_types import Omit, Partial, Pick, Required, assert_shape

@assert_shape(Partial(User))
class UserPatch(BaseModel):
    id: int | None = None
    name: str | None = None

@assert_shape(Omit(User, "id", "created_at"))
class CreateUser(BaseModel): ...
```

`@assert_shape` does nothing at runtime; `py-extended-types check` verifies it:

```
$ py-extended-types check src/
src/app/models.py
  15:1  error  CreateUser is missing required field 'email: str' from Omit[User, 'id']  py-extended-types(missing-field)
  17:5  error  CreateUser.name has type 'int', expected 'str'                         py-extended-types(type-mismatch)

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
| `Pick(T, *keys)` / `Omit(T, *keys)` | keep / drop fields; unknown keys are an error |
| `Record(keys, value)` | a required field per key, like TypeScript's `Record<K, V>`: `keys` is a `Literal[...]`, `KeyOf(T)` or an iterable of names; `value` is a type, or a function of the key returning one |
| `KeyOf(T)` | `T`'s field names, like `keyof T`; read lazily, so it's a placeholder rather than a `Literal` (`.literal` builds one) |

Helpers compose: `Partial(Omit(User, "id"))`.

```python
Scores = Record(Literal["da", "ne"], float)                    # {da: float, ne: float}
Mixed = Record(["id", "label"], lambda key: str if key == "label" else int)
Flags = Record(KeyOf(User), bool)                              # a bool per User field
```

### Your own helpers: `Map` and `Filter`

The four helpers above are built from two primitives, which you can use too:

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

### Matching

A class matches a shape when:

- it has **every field** of the shape, required or optional,
- each field's type is **equivalent** to the shape's: `Optional[str]` and
  `str | None` match, `Annotated` metadata is ignored, but narrower types such as
  `Literal["a"]` for `str` or `bool` for `int` do not,
- each field's **required-ness** is the same: an optional field in the shape needs
  a default here, and a required one must not have one,
- there are **no extra fields** (`extra="allow"` to permit them).

py-extended-types is a dev-time tool only: in the running app `@assert_shape` returns the class
untouched and records nothing, and shapes are lazy, so no fields are read and no `Map` /
`Filter` function is called. Only `py-extended-types check` (which switches collection on before
importing your modules) gathers and verifies the declarations.

## CLI

```
py-extended-types check [PATHS...] [-v]           # verify @assert_shape declarations in PATHS (default .); -v prints a summary on success
py-extended-types show 'pkg.mod:Partial(User)'    # print a class's / shape's fields
```

The checker imports your modules, so it runs under **your project's**
interpreter: `--python`, else `$VIRTUAL_ENV`, else the nearest `.venv`. It puts
this package on `PYTHONPATH` (it has no dependencies), so the project doesn't
need it installed just to run the check. `--no-venv` disables this. Only files
mentioning both `assert_shape(` and `py_extended_types` are imported.

## Development

```bash
uv sync                                       # dev tools: pytest, ruff, mypy, pydantic
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
uv tool install --editable .                  # optional global `py-extended-types`
```

Projects using it can depend on this repo by path while developing it, e.g. with uv:
`py-extended-types = { path = "../py-extended-types", editable = true }` under `[tool.uv.sources]`.
