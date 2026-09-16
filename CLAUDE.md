# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

amd-gputools is a Python + shell toolkit for AMD Instinct MI300-class accelerators,
plus an MCP server (`server.py`) that drives the DigitalOcean droplets carrying them.

## There is no GPU on this machine

The workstation has no AMD GPU. `rocm-smi`, `amd-smi`, `rocminfo`, `hipcc` and
anything else from ROCm **do not exist locally and never will**. The hardware is a
DigitalOcean GPU droplet.

- Never run a ROCm command directly, and never write code that assumes a local
  device. A ROCm command in a local shell is a bug, not a check.
- Reach the hardware through the MCP server's tools (`mcp__amd-gputools__gpu_status`,
  `run_on_droplet`, `ssh_command`) or through explicit `ssh`.
- **Powering a droplet off does not stop DigitalOcean billing it** — the resources
  stay reserved and the hourly rate keeps running. Only destroying it stops the
  meter, and this repo deliberately has no tool that destroys one.

## Commands

- `make lint` — `ruff format --check .` then `ruff check .`
- `make test` — `python3 -m unittest discover -s tests -v`
- `make check` — both; run this before committing
- `make install` — `pip install -r requirements.txt` into the system python3

## Environment

Use the **system `python3`** and install into it. **Never create a virtualenv** —
`.mcp.json` launches the server with a bare `python3`, so a venv makes the server
work in your shell and fail in the client. Add new dependencies to
`requirements.txt`.

`amd.env` is the source of truth for configuration. It is committed on purpose and
must never be gitignored. **It holds no secrets.** The DigitalOcean token is
`DIGITALOCEAN_ACCESS_TOKEN` in the environment or in `.env` (gitignored, mode 0600).
A real environment variable always wins over `amd.env`.

## MCP server conventions

`server.py` follows the sibling servers in `~/gemma4-dev`. Match them:

- **mcp 2.x, not 1.x**: `from mcp.server.mcpserver import MCPServer`. `mcp.server.fastmcp`
  does not exist in the installed SDK; 2.x renamed FastMCP to MCPServer.
- Tools are `async def ... -> str` returning **markdown with an emoji prefix** —
  `✅` success, `❌` error, `📡` in progress, `🛑` stopping. Never a dict, never TextContent.
- **Errors are returned, not raised.** Every tool ends in `except Exception as exc:
  return _error(exc)`. An exception escaping a tool kills the server process for
  every later call. A test enforces this.
- Every subprocess call goes through `run_command(cmd: list[str])` with
  `asyncio.create_subprocess_exec`. **Never `shell=True`.**
- Droplet lookups are scoped by `DROPLET_TAG`, so the server cannot touch a droplet
  nobody tagged for it. Keep new tools inside that scope.
- Never hardcode a droplet IP — resolve it per call. A rebuilt droplet gets a new one.
- The key in `.mcp.json` must equal the directory name: it prefixes every tool as
  `mcp__amd-gputools__<tool>`.

## Code style

- `Optional[str]`, not `str | None` — matches the sibling servers.
- `ruff.toml` pins the rule set deliberately. Do not delete it and do not "fix"
  `UP045` or `BLE001`: ruff's implicit defaults drift between releases and flag the
  `Optional[]` style and the blind `except Exception` that this codebase uses on
  purpose. Lint through `make lint` so the config is picked up.
- Tests are **`unittest`, never pytest**, and run offline: `tests/test_server.py`
  mocks the whole `mcp` package before importing `server`, so no test needs a token
  or a network. Keep it that way.
- Push counting, filtering and totalling into the code and have the caller quote the
  result — `list_droplets` and `_summarize_rocm_smi` compute their own tallies rather
  than printing rows for a reader to add up.

## Not a gemma4-dev rig

This project borrows conventions from `~/gemma4-dev` but is not one of its rigs. The
four-slot `<platform>-<runtime>-<hardware>-<model>` naming scheme in its `NAMING.md`
does not apply here, and this directory does not need to conform to it.
