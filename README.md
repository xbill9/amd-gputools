# amd-gputools

A Python + shell toolkit for AMD Instinct MI300-class accelerators, plus an MCP
server (`server.py`) that drives the DigitalOcean GPU droplets carrying them.

**There is no GPU on the machine you run this from.** `rocm-smi`, `rocminfo`,
`amd-smi` and `hipcc` do not exist locally. Every ROCm command in this repo is
executed over SSH against a droplet; every lifecycle operation goes through the
DigitalOcean v2 API. A ROCm command in a local shell is a bug, not a check.

The card is reached through [AMD Developer Cloud](https://devcloud.amd.com),
which is DigitalOcean underneath — same v2 API, same droplet ids.

Write-up: [An MI300X over MCP — what the matrix cores execute, and what they
don't](https://dev.to/gde/an-mi300x-over-mcp-what-the-matrix-cores-execute-and-what-they-dont-1me9)

## Quick start

```bash
make install          # deps into the SYSTEM python3 — never a virtualenv
cp /dev/null .env && chmod 600 .env   # then put DIGITALOCEAN_ACCESS_TOKEN=... in it
make ssh              # shell on the droplet, address resolved from the API
make scaffold         # bare image → working GPU → vLLM image on disk
make check            # lint + offline unit tests
```

The token is read from `$DIGITALOCEAN_ACCESS_TOKEN`, then `.env`, then
`~/ocean.txt` — the same order in `server.py` and `ssh-droplet.sh`, so a token
that works for one works for the other. It must come from the **My AMD Team**
account, not a personal DigitalOcean one.

`amd.env` holds everything else and is **committed on purpose**: it is all
identifiers and no secrets. A real environment variable always wins over it.

## The MCP server

`.mcp.json` launches `python3 server.py`. The key there must equal the
directory name, because it prefixes every tool as `mcp__amd-gputools__<tool>`.

Every lookup is scoped by `DROPLET_TAG` (`gemma`), so the server cannot touch a
droplet nobody tagged for it. There is **no destroy tool, by design.**

| tool | what it does |
| --- | --- |
| `list_droplets` | every tagged droplet, with state and address |
| `droplet_status` | one droplet in detail, by id or name |
| `create_droplet` | two-step: orders nothing until `confirm=true` |
| `start_droplet` / `stop_droplet` / `reboot_droplet` | power actions |
| `action_status` | poll an action id returned above |
| `ssh_command` | print the ssh line, with the current address |
| `run_on_droplet` | run one command over SSH, return its output |
| `gpu_status` | what the card reports, and why if it reports nothing |
| `hardware_scan` | host, GPU, firmware, ROCm packages, installed tools |
| `prepare_droplet` | apt, firmware, docker — the first half of scaffolding |
| `scaffold_droplet` | prepare, reboot if the card needs it, verify, pull the image |
| `vllm_image_status` | is the image down yet, and can it load Gemma 4 |
| `list_gpu_sizes` | the **public** GPU catalogue and its prices |
| `get_help` | list the above from the live server |

**`scaffold_droplet` is the one call that takes a bare droplet to a working
GPU.** `prepare_droplet` is only its first half — it installs everything and
hands back a droplet whose card has still not bound. Both are idempotent, so
`scaffold_droplet` against a healthy box reboots nothing, pulls nothing, and
works as a health check.

DigitalOcean's own `@digitalocean/mcp` covers eight of these, but it is an API
client and stops at the droplet object: it has no `ssh`, `exec` or `console`
tooling at all, which leaves `ssh_command`, `run_on_droplet`, `gpu_status` and
`hardware_scan` with no equivalent. Its `droplet-get` says `active` about a
droplet whose `amdgpu` never bound. Run it alongside this one if you need the
account, image or volume tools; do not reimplement them here.

## The shell interface

Everything the MCP server does has a shell equivalent, for when the server is
not loaded.

```bash
./ssh-droplet.sh                  # interactive shell
./ssh-droplet.sh rocm-smi         # run one command
./scaffold-droplet.sh             # apt, firmware, docker, reboot if needed, pull the image
./scaffold-droplet.sh --no-pull   # skip the 35-62 GB image
./scaffold-droplet.sh --probe     # also ask the image whether it can load Gemma 4
make sync                         # copy the working tree to /opt/amd-gputools
```

The remote half lives in `scaffold/remote-prepare.sh` and the Gemma 4 check in
`scaffold/gemma4-probe.py`. **`server.py` reads those same two files** rather
than keeping its own copy; a test enforces that the driver script does not grow
its own version of the apt edit.

## The droplet

| | |
| --- | --- |
| name | `debian-gpu-mi300x1-192gb-devcloud-atl1` |
| tag | `gemma` |
| size | `gpu-mi300x1-192gb-devcloud` — 1× MI300X, 192 GiB VRAM, 20 vCPU, 240 GB RAM |
| region | `atl1` |
| cost | **$1.99/hour** — $48/day, $1,433 for 30 days |
| OS | Debian 13 (trixie), kernel 6.12, Xeon Platinum 8568Y+ |

**Never hardcode the id or the address** — resolve both per call. The current
box is the third id this project has had, and a rebuild changes both.

**Powering a droplet off does not stop the billing.** The resources stay
reserved and the hourly rate keeps running. Only destroying it stops the meter,
which is a deliberate human step in the console.

**The v2 API cannot sell you this card.** `POST /v2/droplets` with the devcloud
slug returns `422: This size is unavailable`; the MI300X sizes are
`available: true` with an empty `regions` list, and the console allocates from a
fleet the API has no route for. So `create_droplet` is the preflight and the
audit trail — it reports size, region, image, keys and price, and refuses by
default if anything is already tagged — while the box itself gets built in the
devcloud console. When you do build one there, expand **"Show details and
additional options"** and set the tag: an untagged droplet is invisible to every
tool here, and you pay for it anyway.

### A fresh droplet needs firmware and a reboot

`amdgpu` has failed to bind the MI300X VF on every stock droplet so far, and the
failure changes shape between boots:

| boot | dmesg | cause |
| --- | --- | --- |
| first | `Doesn't get msg:1 from pf, error=-62` | PF handshake |
| after one reboot | `failed to load amdgpu/gc_9_4_3_rlc.bin (-2)` | **no firmware installed** |
| after firmware + reboot | clean | works: `gfx942`, 191.7 GiB |

`/lib/firmware/amdgpu` is **empty** on the stock image. The blobs ship in
`firmware-amd-graphics`, which lives in `non-free-firmware`, so the apt
components have to be enabled before the fix is installable — which is why
`remote-prepare.sh` treats them as a prerequisite, and installs from backports
rather than trixie.

**`/dev/kfd` is a bad test for any of this.** It and `/dev/dri/renderD128` are
created before the probe fails, so they can be present with `amdgpu` in `lsmod`
while the card is entirely unusable. The test that works is whether `rocminfo`
reports a `gfx` agent; `hardware_scan` and `remote-prepare.sh` both say so
directly, and the latter distinguishes "no firmware" from "needs a reboot".

The stock image also carries **no ROCm userspace at all** — of eleven tools
probed, only `python3` existed. `gpu_status` returning "command not found" on a
brand-new box is the image, not a fault.

## Serving with vLLM

Two paths in `vllm/`, both driven from `amd.env`:

- **`vllm/docker-serve.sh`** — AMD's `rocm/vllm` or `vllm/vllm-openai-rocm`
  image. The supported path. The container brings its own complete ROCm
  userspace; only the kernel `amdgpu` and the device nodes come from the host,
  so ROCm 7.13 in the image drives the host's in-tree 6.12 driver fine.
- **`vllm/baremetal-install.sh`** — PyTorch ROCm wheels into the system python3.
  **This gets PyTorch, not vLLM**, and that is not a shortcut: there is no
  prebuilt vLLM wheel for ROCm anywhere, and building from source needs
  libraries Debian 13 does not ship.

Pass the GPU groups as **numeric GIDs resolved on the host** — `--group-add
render` fails outright, because the container image has no `render` group of its
own.

**With Gemma 4, newer is not safer.** The newest image AMD publishes cannot load
the model at all: it lacks `Gemma4ModelArchConfigConvertor`, so config parsing
raises `AmbiguousGlobalPerLayerAttributeError` on `head_dim` before the GPU is
touched. An older image works from the other side, since its transformers
predates per-layer attributes — so the broken build sits *between* the two that
work. Check an image with `scaffold/gemma4-probe.py` before pulling 35-62 GB,
or with `vllm_image_status(probe=true)` after.

Verified live: `google/gemma-4-E2B-it` on `vllm/vllm-openai-rocm:nightly-rocm100`,
`max_model_len` 32768, 9,026,017 tokens of KV cache, 275.45x concurrency, with
text, thinking, tool calling and vision all exercised. **Audio is unreachable**
on every ROCm image tried — none ships the `vllm[audio]` extras — which is why
`LIMIT_MM_PER_PROMPT` sets `audio: 0`.

### Quantization: fp8 is the only win on this card

8192³ matmul, 30 iterations, measured in the serving container **while the card
was also serving live traffic**, so the ratios are the result and not the
absolute rates:

| dtype | TFLOP/s | vs bf16 |
| --- | ---: | ---: |
| bf16 | 664.3 | 1.00x |
| fp16 | 662.5 | 1.00x |
| **fp8 `e4m3fnuz`** | **1172.5** | **1.77x** |
| int8 | 455.6 | **0.69x** |

- fp16 is not a change: same matrix cores, same peak. It costs dynamic range
  and buys nothing.
- int8 measured *slower than bf16* despite sharing fp8's spec peak —
  `torch._int_mm` is not reaching tuned kernels on this stack.
- fp8 here is **`e4m3fnuz`, not `e4m3fn`**. The OCP flavour every NVIDIA
  checkpoint ships in raises `HIPBLAS_STATUS_NOT_SUPPORTED`, so an H100 fp8
  checkpoint is not drop-in. Quantize online from bf16 with `--quantization fp8`.
- **fp4 does not exist on gfx942**, and vLLM's mxfp4 path here is emulation:
  bf16 speed, no resident-memory saving, plus dequant overhead and the full fp4
  error. It answers "would this model survive fp4", not "deploy this".
- **GGUF is compiled out of the image entirely** — absent, not gated.

## Layout

```
server.py                  the MCP server — thirteen tools, no destroy
amd.env                    configuration, committed, no secrets
scaffold-droplet.sh        driver: bare droplet → working GPU
scaffold/remote-prepare.sh   the remote half, shared with prepare_droplet
scaffold/gemma4-probe.py     can this image load Gemma 4
ssh-droplet.sh             shell on the droplet, address from the API
vllm/docker-serve.sh       serve from the container (supported path)
vllm/baremetal-install.sh  PyTorch ROCm on the host python3
tests/test_server.py       offline unittest — mocks mcp, needs no token
evidence/                  raw output behind the numbers quoted here
```

## Development

- `make lint` — `ruff format --check`, `ruff check`, `shellcheck`
- `make test` — `unittest`, never pytest; runs offline, needs no token
- `make check` — both; run before committing

Conventions that are deliberate and should not be "fixed":

- **System `python3`, never a virtualenv.** `.mcp.json` launches the server with
  a bare `python3`, so a venv makes it work in your shell and fail in the client.
- **mcp 2.x**: `from mcp.server.mcpserver import MCPServer`. `mcp.server.fastmcp`
  does not exist in the installed SDK.
- Tools are `async def ... -> str` returning markdown with an emoji prefix.
  **Errors are returned, not raised** — an exception escaping a tool kills the
  server for every later call. A test enforces this.
- Every subprocess goes through `run_command(cmd: list[str])`. Never `shell=True`.
- `Optional[str]`, not `str | None`. `ruff.toml` pins the rule set on purpose;
  `UP045` and `BLE001` are disabled deliberately.
- **Push counting and totalling into the code**, and have the caller quote the
  result — `list_droplets` and `_summarize_rocm_smi` compute their own tallies
  rather than printing rows for a reader to add up.

Two lessons this codebase has already paid for:

- **`rocm-smi` and `amd-smi` exit 0 when they fail.** They print "Driver not
  initialized" to stderr, nothing to stdout, and exit 0. Parse the output, never
  the exit status.
- **A probe that did not run is not a probe that answered.** `gpu_status` once
  diagnosed a healthy driver on a droplet that was refusing SSH, because the API
  said `active` and the code read "not absent" as "present". Fixtures for remote
  tooling get pasted from the box, never typed from memory.
