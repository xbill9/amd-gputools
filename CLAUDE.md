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

## The droplet

One droplet, reached through AMD Developer Cloud (`devcloud.amd.com`), which is
DigitalOcean underneath — same v2 API, same droplet ids. The token comes from the
**My AMD Team** account, not a personal DigitalOcean one.

| | |
| --- | --- |
| name | `debian-gpu-mi300x1-192gb-devcloud-atl1` |
| id | `601142018` |
| tag | **`gemma`** — this is what `DROPLET_TAG` must be, not `amd-gputools` |
| size | `gpu-mi300x1-192gb-devcloud` — 1× MI300X, 192 GiB VRAM, 20 vCPU, 240 GB RAM |
| cost | **$1.99/hour**, running now |
| OS | Debian 13 (trixie), kernel 6.12.94+deb13-amd64 |

Verified 2026-09-16. Re-read the address rather than trusting this table — it changes.

### The GPU does not currently work

`lspci` shows the MI300X VF at `83:00.0` and `/dev/dri/renderD128` exists, but
**`/dev/kfd` does not**, so no ROCm process can use the card. The stock Debian
`amdgpu` does not bring up the compute node for an MI300X VF; that needs the ROCm
driver stack installed on the droplet. `rocminfo`, `rocm-smi` and `amd-smi` are in
`/usr/bin`, there is no `/opt/rocm`, and PyTorch is not installed.

**`rocm-smi` and `amd-smi` exit 0 when they fail.** Measured on this droplet:
`rocm-smi` printed "Driver not initialized" to stderr, printed nothing to stdout, and
exited 0. Never branch on their exit status — parse the output. `gpu_status` does,
after an earlier version reported a healthy "✅" with an empty table.

The devcloud size slug is not in `GET /v2/sizes`, which lists the public
`gpu-mi300x1-192gb` at $2.59/hr instead. `list_gpu_sizes` shows the public catalogue,
not what devcloud sells.

## Commands

- `make lint` — `ruff format --check .` then `ruff check .`
- `make test` — `python3 -m unittest discover -s tests -v`
- `make check` — both; run this before committing
- `make install` — `pip install -r requirements.txt` into the system python3
- `make ssh` / `./ssh-droplet.sh [command]` — shell on the droplet, or run one command
  there. It resolves the address from the API on every call; pass `DROPLET_IP` to skip
  that. The token comes from `$DIGITALOCEAN_ACCESS_TOKEN`, then `.env`, then `~/ocean.txt`.

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
