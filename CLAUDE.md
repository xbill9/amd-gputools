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

**The VRAM percentage key is `GPU Memory Allocated (VRAM%)`.** Not "GPU Memory Use (%)" or
any other plausible spelling — measured on ROCM-SMI 2.2.0 here 2026-09-16, while vLLM held
168.3 GiB of the 191.7 GiB. `gpu_status` printed `-` in that column for hours and the card
read as idle. The unit test used the invented key too, so it passed against a payload no
machine emits: fixtures for remote tooling get pasted from the box, never typed from memory.

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
`DIGITALOCEAN_ACCESS_TOKEN` in the environment, then `.env` (gitignored, mode 0600),
then `~/ocean.txt` — the same order in `server.py` and `ssh-droplet.sh`, so a token
that works for one works for the other. A real environment variable always wins
over `amd.env`.

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

Verified 2026-09-16, twice. First: image 44.3 GB unpacked, vLLM 0.19.1 serving
Qwen2.5-7B-Instruct **84 seconds** after container start, `max_model_len` 32768,
186.6 GB of the 191.7 GB VRAM preallocated at the default `gpu_memory_utilization`.
Then the box was moved to its actual job — `google/gemma-4-E2B-it` on
`vllm/vllm-openai-rocm:nightly-rocm100` (vLLM 0.29.1rc1.dev187, torch 2.12.0+rocm10.0.0,
transformers 5.17.0), 9,026,017 tokens of KV at `max_model_len` 32768 and 275.45x
concurrency, with text, thinking, tool calling and vision all verified live.

**With Gemma 4, newer is not safer.** `rocm/vllm:rocm10.0.0_..._vllm_0.27.0` is the
newest image AMD publishes and it **cannot load the model at all**: it lacks
`Gemma4ModelArchConfigConvertor`, so config parsing raises
`AmbiguousGlobalPerLayerAttributeError` on `head_dim` before the GPU is touched. Gemma 4
runs 256-wide heads on its sliding-attention layers and 512 on its full-attention ones;
transformers >= 5.15 reports that as a per-layer attribute and raises on a global read,
and vLLM's `getattr(..., 0)` default cannot catch it. The 0.19.1 image above works from
the other side, since transformers 5.8.1 predates per-layer attributes — so **the broken
build sits between the two that work**, and the pip channel vLLM's own recipe names is
staler than all three at 0.20.2rc1. The three-image comparison table now lives in the
companion article `~/gemma4-dev/gpu-vllm-mi300x-2b/devto-gemma4-mi300x-mcp.md`, which is
the deployment write-up; `devto-gemma4-mi300x-vllm.md` is the card-and-control-plane
article and keeps only a prose summary of the finding.

Check before pulling 35-62 GB: run the candidate image's `python3` and print
`MODEL_ARCH_CONFIG_CONVERTORS.get("gemma4")` plus `torch.cuda.get_arch_list()`. Import
`vllm.config` first, or a circular-import `ImportError` makes a good image look broken,
and map `/dev/kfd` and `/dev/dri` in, or `get_arch_list()` returns `[]` and looks like a
build with no kernels for you.

**The two images take different argv.** `vllm/vllm-openai-rocm` sets
`ENTRYPOINT ["vllm","serve"]`, so the model id is the first argument; AMD's images have
no entrypoint and need `vllm serve` spelled out. `vllm/docker-serve.sh` keys off the
image name.

**Audio is unreachable on every ROCm image tried.** E2B has a conformer audio encoder,
but none of these images ships the `vllm[audio]` extras — `librosa` and `soundfile` are
absent — so audio fails at request time whatever `--limit-mm-per-prompt` says. That is
why `LIMIT_MM_PER_PROMPT` sets `audio: 0`; it also skips allocating encoder memory for a
path that cannot be used. Audio needs a derived image, not a flag.

The default model is still deliberately ungated: a gated repo (Llama, and every Gemma
one before Gemma 4) turns a first run into a Hugging Face login problem instead of a
vLLM problem. `google/gemma-4-E2B-it` is Apache-2.0 and ungated, so it keeps that
property while being the thing this box is actually for. Set `HF_TOKEN` in `.env` if you
switch `VLLM_MODEL` to something gated.

## Quantization: fp8 is the only win on this card

Measured 2026-09-16 in the serving container (torch 2.12.0+rocm10.0.0, vLLM 0.29.1rc1), 8192³
matmul, 30 iterations, **while the card was also serving live traffic** — so the absolute rates
are depressed and the ratios are the result.

| dtype | TFLOP/s | % of spec peak | vs bf16 |
| --- | ---: | ---: | ---: |
| bf16 | 664.3 | 50.8% | 1.00x |
| fp16 | 662.5 | 50.7% | 1.00x |
| **fp8 `e4m3fnuz`** | **1172.5** | 44.8% | **1.77x** |
| int8 | 455.6 | 17.4% | **0.69x** |

- **bf16 is the baseline and fp16 is not a change.** Both go through the same CDNA 3 matrix cores
  at the same 1307.4 TFLOP/s peak. Gemma 4's config says `dtype: bfloat16` and the engine confirms
  `dtype=torch.bfloat16, quantization=None`. Moving to fp16 costs dynamic range and buys nothing.
- **int8 measured *slower than bf16*** — 0.69x, 17% of its own 2614.9 TOPS peak — even though the
  spec gives int8 and fp8 the same peak. `torch._int_mm` is not reaching tuned kernels on this
  stack. Do not reach for int8 here on the strength of the spec sheet.
- **fp8 is `e4m3fnuz`, not `e4m3fn`, and that is a trap rather than a detail.** `float8_e4m3fn` —
  the OCP flavour every NVIDIA checkpoint is published in — does not merely run slowly here, it
  raises `RuntimeError: HIPBLAS_STATUS_NOT_SUPPORTED`. vLLM agrees from its own side:
  `is_fp8_fnuz()` keys on `"gfx94"` and `fp8_dtype()` returns `torch.float8_e4m3fnuz`. **An fp8
  checkpoint built for H100 is therefore not drop-in.** Quantize online from the bf16 weights with
  `--quantization fp8` instead of hunting for a checkpoint.
- **fp4 does not exist on gfx942.** torch names the allowlist in its own error: `Block-wise scaling
  for Float8_e8m0fnu is only supported on gfx950,gfx1250`. You cannot even cast to it — `copy_()
  does not support casting Float4_e2m1fn_x2 to different types`. vLLM gates it identically:
  `supports_mx()` is `any(gfx in _GCN_ARCH for gfx in ["gfx95", "gfx1250"])`, which is **False**
  here. `mxfp4` appearing in `supported_quantization` is a generic list, not a hardware claim.
- **vLLM's mxfp4 emulation is a research instrument, not a deployment path.** With `supports_mx()`
  False the kernel is `EmulationMxfp4LinearKernel.apply_weights`: `dequant_mxfp4(...)` widens the
  weights back to bf16 *on every forward pass*, activations go through quantize-dequantize, and
  `F.linear` runs at bf16. Bf16 speed, no resident-memory saving during compute, plus dequant
  overhead and the full fp4 error. It answers "would this model survive fp4" before buying MI355X.
- **GGUF is compiled out of the image entirely.** Not gated — absent. `'gguf'` is not in
  `QUANTIZATION_METHODS` (the global registry, not the platform list),
  `vllm.model_executor.layers.quantization.gguf` is `ModuleNotFoundError`, and there are **no**
  ggml/gguf symbols in `vllm._custom_ops`. GGUF here means changing inference engine, not adding
  a flag.
- **The 4-bit that does work is weight-only.** `awq`/`gptq` are in the registry and
  `awq_dequantize` is compiled in. Storage and bandwidth only — the matmul is still bf16. With a
  ~2B model on 192 GB and KV cache at 0.2% utilisation there is no memory pressure to relieve, so
  it buys nothing here.

The attention backend in use is `TRITON_ATTN`, not AITER; `VLLM_ROCM_USE_AITER=1` is a separate
and so far untested lever.

## Not a gemma4-dev rig

This project borrows conventions from `~/gemma4-dev` but is not one of its rigs. The
four-slot `<platform>-<runtime>-<hardware>-<model>` naming scheme in its `NAMING.md`
does not apply here, and this directory does not need to conform to it.
