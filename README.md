# opencontextengine-client

Standalone synchronous Python client for OpenContextEngine workspace and blob
management. The package owns local inventory, ignore rules, upload planning,
checkpoint state, and retrieval adapters. It does not depend on Auggie SDK.

Install the distribution package with `uv add opencontextengine-client` (or
`pip install opencontextengine-client`). The installed command remains
`oce-client`.

## Version Information

Run `oce-client --version` to print the installed client version.

| Item | Value |
| --- | --- |
| PyPI distribution | `opencontextengine-client` |
| Python package | `oce_client` |
| CLI | `oce-client` |
| MCP server | `oce-client-mcp` (separate interface) |
| Version command | `oce-client --version` |

The authoritative version is declared in `pyproject.toml` and mirrored by
`oce_client.__version__`.

## Release Preparation

The first release uses the current version because the repository has no
release tag yet:

```powershell
uv run python scripts/release.py 0.1.0 --dry-run
uv run python scripts/release.py 0.1.0
```

For later releases, use `major`, `minor`, `patch`, or an exact higher version.
The script updates version metadata, generates the changelog, builds the
package, creates a release commit, and creates an annotated tag locally. It
never pushes or publishes automatically; review the result before pushing the
branch and tag.

## CLI

Install the package with `uv` and configure the service endpoint and key through
the environment:

```powershell
# These are the built-in defaults; override them only when needed.
$env:OCE_API_URL = "http://127.0.0.1:8986"
$env:OCE_API_KEY = "sk-opencontextengine"
$env:OCE_WORKSPACE = (Get-Location).Path
uv run oce-client sync
uv run oce-client retrieve "where is request authentication implemented?"
```

If unset, `OCE_API_URL` defaults to `http://127.0.0.1:8986` and `OCE_API_KEY`
defaults to `sk-opencontextengine`. `status` is local-only and does not require
an API key. `observe` and `remove`
stage explicit editor changes in SQLite; run `sync` to publish them. Add
`--json` to `sync`, `status`, `retrieve`, `observe`, or `remove` for
machine-readable output. CLI options are placed before the subcommand, for
example `oce-client --root C:\src\project sync`; `--root` falls back to
`OCE_WORKSPACE`, and `--api-url`, `--state-path`, and repeated `--ignore`
override `OCE_API_URL`, `OCE_STATE_PATH`, and `OCE_IGNORE`.

The two interfaces have different lifecycles:

| Interface | Workspace selection | State selection | Index lifecycle |
| --- | --- | --- | --- |
| CLI | one `--root` or `OCE_WORKSPACE` | `--state-path` or `OCE_STATE_PATH` | explicit `sync`, optional `watch` |
| MCP | repeated `--workspace`, `OCE_WORKSPACE`, `OCE_WORKSPACES`, or the launch directory by default | one `--state-path`, or per-workspace `--state-dir` | process-owned background and incremental sync |

## Ignore Rules

Files are filtered by a layered matcher, highest precedence first:

| Layer | Source |
| --- | --- |
| Hard | `.git/`, `.oce-client/` (cannot be re-included) |
| Runtime | `--ignore` / `OCE_IGNORE` |
| Project | `.oceignore` in the workspace root |
| Git | `.gitignore` in the root and in every subdirectory |
| Built-in | Language build outputs and caches |

Both `.gitignore` and `.oceignore` are read from the workspace root **and from
every subdirectory**, following git semantics: each file governs its own
directory subtree and its patterns are relative to that directory, so
`sub/.gitignore` containing `generated/` hides `sub/generated/` but not
`other/generated/`. Deeper files take precedence over shallower ones, so a
nested `!local.py` can re-include a file excluded by the root `.gitignore`.

This makes a workspace root holding several projects behave predictably: rules
in `proj-a/` only affect `proj-a/`, rules in `proj-b/` only affect `proj-b/`,
and each project keeps its own `.oceignore` overrides. The built-in layer
applies to every project regardless of which language it is written in.

The built-in layer keeps the index free of noise even when a project has no
`.gitignore`: `node_modules/`, `target/`, `__pycache__/`, `.next/`, `dist/`,
`build/`, `_build/`, `.dart_tool/`, `obj/`, and similar. It is the lowest
precedence layer, so `!pattern` in `.gitignore` or `.oceignore` re-includes an
entry. Deliberately excluded are ambiguous names such as `bin/`, `lib/`,
`out/`, `packages/`, and `vendor/`, which are normal source directories in
C++, Go, and monorepo layouts.

## MCP

Install the optional MCP extra and expose the stdio server to an MCP host:

```powershell
uv sync --extra mcp
uv run oce-client-mcp --workspace C:\path\to\workspace
```

The server exposes one tool, `codebase-retrieval`. Workspace indexing belongs
to the MCP process rather than the coding agent: the server starts the initial
index in the background, watches the filesystem, and synchronizes only changed
paths. Unchanged files are identified by stored filesystem metadata and are not
read or rehashed on restart.

If no workspace is configured (no `--workspace`, `OCE_WORKSPACE`, or
`OCE_WORKSPACES`), the server indexes the directory it was launched from —
coding agents start MCP servers with the project as the working directory, so a
single-project setup needs no workspace configuration at all. As a safety guard,
an implicit fallback to the filesystem root is rejected.

Declare each allowed workspace with a repeated `--workspace` argument. With one
workspace, the tool's `workspace_folder` input is optional. With multiple
workspaces it is required and must exactly match an allowed path. Other paths
are rejected. For an environment-only setup, use `OCE_WORKSPACE` for one path
or `OCE_WORKSPACES` with paths separated by the platform path separator.

```powershell
oce-client-mcp `
  --workspace C:\src\project-a `
  --workspace C:\src\project-b `
  --state-dir $env:LOCALAPPDATA\oce-client `
  --initial-sync background `
  --debounce-ms 500 `
  --ready-timeout 3
```

`--initial-sync` accepts `background` (default), `blocking`, or `off`; `off`
defers initialization until the first retrieval call. A tool call waits up to
`--ready-timeout` seconds for the latest observed filesystem generation. Its
result status is `ready`, `indexing`, or `error`; only a `ready` result contains
retrieval context. `OCE_API_URL`, `OCE_API_KEY`, `OCE_STATE_PATH`, `OCE_STATE_DIR`, `OCE_IGNORE`,
`OCE_DEBOUNCE_MS`, `OCE_INITIAL_SYNC`, `OCE_READY_TIMEOUT`, and
`OCE_LOG_LEVEL` provide environment equivalents. `--state-path` and
`OCE_STATE_PATH` are for one workspace; use `--state-dir` or `OCE_STATE_DIR`
for multiple workspaces. Keep the API key in the environment rather than
command arguments.

The service endpoint, API key, and ignore patterns are shared through the same
environment variables. State selection follows the interface table above. A
Codex-ready skill with the host configuration and command guidance is included
at `skills/oce-client/SKILL.md`.

After installing a wheel, locate or install that skill with:

```powershell
uv run oce-client skill path
uv run oce-client skill install
```

The default installation target is `$CODEX_HOME/skills/oce-client` or
`$HOME/.codex/skills/oce-client`. Existing skill directories are preserved;
pass `--force` only when intentionally updating one.

Keep `OCE_API_KEY` in the host's environment or secret manager; do not commit it
to an MCP configuration file.
