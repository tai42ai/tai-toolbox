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
| GitHub repository | `tai-<name>` |

So a dependency is declared as `tai42-<name>` while its repository is named
`tai-<name>`, and both spellings are correct in their own context.

Some surfaces are deliberately neither, and must not be renamed: the `tai` CLI
command (`tai42` is an alias), the Prometheus metric namespace (`tai_tool_*`),
`TAI_*` environment variables, and the `tai-plugin.yml` descriptor filename.

## Dev

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev,http,prometheus,chain,embeddings,proxy]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

`make dev` installs the sibling `tai-contract` and `tai-kit` repos as editable installs for local cross-repo development.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## Dependency resolution

`uv.lock` pins the `tai42-*` siblings to their released index versions while `[tool.uv.sources]` points them at local `../tai-*` checkouts. The two disagree deliberately: CI sets `UV_NO_SOURCES=1` and asserts the lock with `uv sync --locked`, so it resolves the artifacts a user installs. A bare `uv lock` beside sibling checkouts re-couples the lock to editable path entries, which then fails that `--locked` check — run `uv lock --no-sources` instead. See [How dependencies resolve](https://tai42.ai/contributing#how-dependencies-resolve).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
