# Contributing to tai42-toolbox

`tai42-toolbox` is the reference contrib package of generic **tools** and **tool
extensions** for the TAI ecosystem. The hard rule (the plugin rule): **it depends
on `tai42-contract` + `tai42-kit` only and never imports the skeleton.** Everything
registers through the `tai42_app` handle from `tai42_contract.app` and is loaded by
the host from the manifest (`tools[].module` / `extensions_modules`) by dynamic
import — there is no import edge to the skeleton in either direction.

> A plugin extends the platform; a *tool extension* extends a single tool.

## Ground rules

- **No skeleton import — ever.** The package is contract-facing; the ban is
  enforced by ruff (`flake8-tidy-imports`), so a stray import fails lint:
  ```bash
  grep -rn "tai42_skeleton" src/   # must be empty
  ```
- **The base install stays light.** Only `tai42-contract`, `tai42-kit`, and
  `makefun` are base dependencies. Every heavier dependency is opt-in behind its
  own extra; a module whose extra is missing must fail loudly at import with a
  copy-pasteable `install tai42-toolbox[extra]` hint — never a silent skip.
- **Loud errors.** No swallowed exceptions, silent fallbacks, or silent
  truncation. A bound exceeded, a missing input, or a failed sub-step raises.
- **Typed package** (`py.typed`). Pyright runs clean; a missing optional backing
  library is a warning, not an error.

## Layout

- `src/tai42_toolbox/extensions/` — the tool extensions (`cache`, `proxy`,
  `prometheus`, `batch`, `chain`, `output_schema`), each registered with its
  `ExtensionKind`.
- `src/tai42_toolbox/tools/` — the tools (`generate_embeddings`, `pad_embeddings`,
  `request`, `generate_uuid`, `current_time_info`).
- `src/tai42_toolbox/_internal/` — private helpers, kept out of the public surface
  by the package name (e.g. `_internal/tools/http_client.py`,
  `_internal/extensions/socket_routing.py`, `_internal/extensions/signature.py`).
- `tests/` mirrors `src/` (`tests/extensions/`, `tests/tools/`).

## Naming

PyPI is a flat namespace with no owner in the path, so distributions carry the
`tai42-` prefix. GitHub repositories keep their `tai-` names, because the
`tai42ai` organisation already namespaces them. Import packages follow the
distribution.

| Surface | Form |
| --- | --- |
| Distribution — PyPI, `pip install`, dependency pins | `tai42-<name>` |
| Import package | `tai42_<name>` |
| GitHub repository and sibling checkout directory | `tai-<name>` |

So a dependency is declared as `tai42-<name>` but resolved from `../tai-<name>`
during local development, and both spellings are correct in their own context.

Some surfaces are deliberately neither, and must not be renamed: the `tai` CLI
command (`tai42` is an alias), the Prometheus metric namespace (`tai_tool_*`),
`TAI_*` environment variables, and the `tai-plugin.yml` descriptor filename.

## Dev

```bash
uv sync --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

For local cross-repo work, `make dev` editable-installs the sibling `tai-*`
checkouts this package builds on into the venv. While `[tool.uv.sources]` pins
those siblings to local paths, `uv sync` already installs them editable and
`make dev` changes nothing; once the lock resolves them from the registry,
`uv sync` / `uv run` installs the published builds instead, so re-run
`make dev` afterward to restore the editable links.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
