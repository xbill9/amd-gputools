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

### The GPU works — but a freshly provisioned droplet needs one reboot

Verified working 2026-09-16: `gfx942`, AMD Instinct MI300X VF, 304 CUs, 191.7 GiB
VRAM, ISA `amdgcn-amd-amdhsa--gfx942:sramecc+:xnack-`.

**On a fresh droplet `/dev/kfd` is missing until you reboot once.** Before the
reboot `lspci` shows the card and `/dev/dri/renderD128` exists, but `amdgpu` has
already failed to bind and unloaded, so no ROCm process can use it. Nothing needs
installing — Debian's in-tree `amdgpu` plus the ROCm 6.1.2 userspace already on the
image are enough. Reboot, don't debug: an hour went into diagnosing a driver stack
that was fine.

These dmesg lines are benign on a VF and are not the problem: `failed to load
amdgpu/psp_13_0_6_cap.bin (-2)`, `Unsupported TA type: 8`, `TMZ feature not
supported`.

ROCm is the Debian packaging (`rocm-smi`, `rocminfo` in `/usr/bin`, ROCm 6.1.2), not
AMD's own repo — there is no `/opt/rocm` and no `amdgpu-dkms`. PyTorch is not
installed.

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

## Serving with vLLM

Two paths, both in `vllm/`, both driven from `amd.env`:

- **`vllm/docker-serve.sh`** — AMD's `rocm/vllm` image. This is the supported path and
  the one to reach for first. The image carries its own ROCm userspace; only the
  kernel `amdgpu` and `/dev/kfd` come from the host.
- **`vllm/baremetal-install.sh`** — PyTorch ROCm wheels into the system python3.

**Bare metal gets PyTorch, not vLLM, and that is not a shortcut.** There is no prebuilt
vLLM wheel for ROCm anywhere: PyPI's `vllm` wheels are CUDA builds, AMD's manylinux
index at `repo.radeon.com` carries torch and triton but no vllm, and the
per-architecture nightly index is not published (all checked 2026-09-16). Building from
source needs `hipcc` plus `rocblas`, `hipblaslt`, `miopen` and `rccl`; Debian 13 ships
`hipcc` 5.7.1 and none of those libraries, and AMD's repo does not support trixie.
vLLM therefore comes from the container. Getting it bare metal means either moving the
droplet to Ubuntu 24.04 or installing AMD's ROCm on an unsupported distro — a decision,
not a missing step.

The torch ROCm wheel bundles its own ROCm runtime libraries in `torch/lib`, which is why
bare metal works with no `/opt/rocm` at all. Install with `pip --break-system-packages`;
Debian 13 marks the system python externally-managed (PEP 668) and the thing it steers
you toward is a virtualenv, which this project does not use.

Verified 2026-09-16: `torch 2.9.1+rocm6.4` (HIP 6.4.43484) on the system python3,
**557.7 TFLOP/s** on an 8192³ fp16 matmul. `numpy` is not installed, so torch prints a
"Failed to initialize NumPy" warning — harmless for this, worth installing before real
work.

Pick the image tag by GPU architecture: `gfx94X-dcgpu` is MI300-series (this box),
`cdna` is the same family at twice the size, and `gfx110X`/`gfx120X` are RDNA and will
not run here.

**Pass the GPU groups as numeric GIDs resolved on the host.** `--group-add render`
fails outright — `Unable to find group render: no matching entries in group file` —
because the Ubuntu 24.04 image has no `render` group of its own. `video` exists in both
with no guarantee the numbers agree. On this host they are `video:44`, `render:991`.

A newer container ROCm than the host's is fine: ROCm **7.13** userspace in the image
drives the host's in-tree **6.12** `amdgpu` correctly, reporting "AMD Instinct MI300X
VF" and 304 CUs. Only the kernel driver and `/dev/kfd` come from the host.

Verified 2026-09-16: image 44.3 GB unpacked, vLLM 0.19.1 serving Qwen2.5-7B-Instruct
**84 seconds** after container start, `max_model_len` 32768, 186.6 GB of the 191.7 GB
VRAM preallocated at the default `gpu_memory_utilization`.

The default model is deliberately ungated. A gated repo (every Gemma one, Llama) turns
a first run into a Hugging Face login problem instead of a vLLM problem; set `HF_TOKEN`
in `.env` and switch `VLLM_MODEL` once the path itself is known to work.

## Not a gemma4-dev rig

This project borrows conventions from `~/gemma4-dev` but is not one of its rigs. The
four-slot `<platform>-<runtime>-<hardware>-<model>` naming scheme in its `NAMING.md`
does not apply here, and this directory does not need to conform to it.
