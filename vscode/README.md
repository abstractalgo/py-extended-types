# py-extended-types for VS Code

Shows [py-extended-types](../README.md) `@assert_shape` / `@assert_field` contract errors as
diagnostics (Problems panel + squiggles on the offending field), with quick fixes
(the gutter lightbulb / `Cmd+.`):

| Problem | Quick fix |
| --- | --- |
| `type-mismatch`, `required-mismatch` | Rewrite the field as the shape expects, e.g. `read_only: bool \| None = None`; an existing value is kept and `Field(...)` gets `default=None` |
| `missing-field` | Add the field after the class's last field |
| `unexpected-field` | Remove the field |

plus **Fix all py-extended-types problems in this file**. A fix is offered only while the
document is at the version that was checked, so after an edit it reappears with
the next check. A fix can be missing when it can't be done safely, e.g. removing
a default from inside `Field(...)`. Types are written as py-extended-types prints them
(`list[Foo] | None`), so a fix may need an import the file doesn't have yet.

## How it works

Per workspace folder, it runs

```
<py-extended-types> check --format json [--stdin] <pyExtendedTypes.paths...>
```

on startup, 400ms after you stop typing (`pyExtendedTypes.run: "onType"`, the default), and
on every save, then replaces that folder's diagnostics with the result. Unsaved
Python buffers are sent on stdin as `{path: source}`, so the check sees what's in
the editor, including an unsaved base model in another file. The whole set of
paths is re-checked rather than just the edited file, because changing a model in
one file can break a contract declared in another. A run still in flight when you
type or save again is cancelled.

Half-typed code often doesn't parse yet. When a check of unsaved code hits an
`analysis-error`, its result is discarded and the previous diagnostics stay (the
language server already reports syntax errors); a check of saved files reports
analysis errors normally.

Checking is static: the CLI reads your modules with mypy's semantic analysis and never
imports or runs them, so checking as you type is safe for any code.

The status bar shows the problem count; click it for the log. **py-extended-types: Restart**
kills any running check, clears all diagnostics and starts again with freshly read
settings.

`<py-extended-types>` is `pyExtendedTypes.path` if set, else `.venv/bin/py-extended-types` in the workspace
folder, else `py-extended-types` from `PATH`. It finds the project's installed packages in
the nearest `.venv` without running its interpreter.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `pyExtendedTypes.enable` | `true` | Turn the extension off for a folder |
| `pyExtendedTypes.path` | `[]` | Command to run, e.g. `["uv", "run", "--no-sync", "py-extended-types"]` |
| `pyExtendedTypes.paths` | `["."]` | Paths to check, relative to the workspace folder |
| `pyExtendedTypes.run` | `"onType"` | `onType`: re-check as you type and on save; `onSave`: only on save, from disk |

## Develop

```bash
npm install
npm run compile          # or `npm run watch`
```

Press F5 with this folder open to launch an Extension Development Host, or build
and install a package:

```bash
npm run package
code --install-extension py-extended-types-0.6.0.vsix   # then "Developer: Reload Window"
```

Requires the CLI's JSON output (`py-extended-types check --format json`, report `version: 1`).
