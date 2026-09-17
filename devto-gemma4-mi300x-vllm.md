---
title: "An MI300X Over MCP: What the Matrix Cores Execute, and What They Don't"
published: false
description: "One AMD Instinct MI300X on AMD Developer Cloud, managed entirely through a tag-scoped Python MCP server, with every figure read off the card rather than a spec sheet. fp8 e4m3fnuz runs 1.77x bf16; int8, which AMD rates identically to fp8, runs 0.69x; fp4 is not on this silicon at all. One droplet, $1.99 an hour, and two readings that were wrong the first time."
tags: amd, mcp, rocm, machinelearning
cover_image: https://raw.githubusercontent.com/xbill9/amd-gputools/main/devto-cover.376dac15.jpg
---

This article provides a step by step inventory and measurement of a single AMD Instinct MI300X on an AMD Developer Cloud hosted GPU enabled system. A suite of Python MCP tools is built to simplify management of the droplet, and the same server is used to read the card's native numeric format support off the hardware.

https://github.com/xbill9/amd-gputools

The workstation writing this has no AMD GPU and never will. The hardware is one MI300X droplet billed at $1.99 an hour, and everything that reaches it goes through twelve tag-scoped MCP tools for inventory, power, reboot, hardware scan, GPU state and remote execution.

Two of the readings those tools returned were wrong the first time, and both were corrected by parsing output instead of trusting a status. The format table at the end disagrees with AMD's published peaks in two places, and the disagreement is the result rather than a footnote to it.

#### Prerequisites

- An AMD Developer Cloud account with a GPU droplet already created. `devcloud.amd.com` is
  DigitalOcean underneath — same v2 API, same droplet ids — and the token comes from the
  **My AMD Team** account, not a personal DigitalOcean one.
- The droplet tagged. Every lookup in this server is scoped by `tag_name`, so an untagged
  droplet is invisible to it and a tagged one belonging to someone else is not.
- `DIGITALOCEAN_ACCESS_TOKEN` in the environment, or in a mode 0600 `.env`, or in
  `~/ocean.txt`. The same order is used by the server and by `ssh-droplet.sh`.
- An SSH key on the droplet as `root`. `BatchMode=yes` throughout, so a wrong key fails
  rather than waiting on a password prompt.
- Python 3 with `httpx` and `python-dotenv` in the system interpreter. No virtualenv —
  `.mcp.json` launches the server with a bare `python3`.

#### The Box, In Detail

Everything in these three tables was read off the machine by `hardware_scan`, `rocminfo` and `lspci` through the MCP server, on 2026-09-16.

**Host**

| | |
| --- | --- |
| CPU | INTEL(R) XEON(R) PLATINUM 8568Y+ |
| Cores | 20 vCPU — 1 socket, 20 cores, **1 thread per core** (no SMT) |
| Cache | L1d 640 KiB (20 instances), L2 80 MiB (20 instances) |
| NUMA | 1 node |
| RAM | 236 GB usable, 138 GB free, 90 GB in buff/cache |
| Disk | 720 GB, 595 G free |
| OS | Debian 13 (trixie), kernel `6.12.94+deb13-amd64` |
| Droplet | `gpu-mi300x1-192gb-devcloud`, region `atl1`, id `601142018` |

**GPU**

| | |
| --- | --- |
| Device | AMD Instinct MI300X **VF** (virtual function), `[1002:74b5]` at `83:00.0` |
| Target | `gfx942`, ISA `amdgcn-amd-amdhsa--gfx942:sramecc+:xnack-` |
| Compute | **304 CUs**, 4 SIMDs per CU, wavefront 64, max 32 waves per CU |
| Clock | 2100 MHz max |
| Workgroup | max 1024 threads, LDS (GROUP segment) 64 KB |
| Cacheline | 128 B |
| Link | PCIe **Gen5 x16** — `LnkSta: Speed 32GT/s, Width x16` |
| BAR | Region 0 is 256 G prefetchable, so the whole framebuffer is host-visible |
| VBIOS | `113-M3000108-103`, SKU `M3000108`, SMC firmware `00.85.129.03` |
| UUID | `GPU-2896ede4ddd2a8b6` |

**GPU memory** — the numbers that decide what fits:

| Pool | Total | Used |
| --- | --- | --- |
| VRAM | 205,822,885,888 B = **191.69 GiB** | 180,717,051,904 B = 168.31 GiB (87%†) |
| VIS_VRAM (host-visible) | 205,822,885,888 B = 191.69 GiB | 180,717,051,904 B = 168.31 GiB |
| GTT (system memory aperture) | 126,676,250,624 B = 117.98 GiB | 21,327,872 B = 0.02 GiB |

† 87 is what `rocm-smi` reports; the division gives 87.8%. The tool truncates, and this article quotes the tool.

Two things worth reading off that table. **VIS_VRAM equals VRAM**: this is a large-BAR configuration, the entire 191.69 GiB is CPU-mappable, and no part of the framebuffer is hidden behind the old 256 MB window. And `rocminfo` reports three GLOBAL pools — coarse grained, fine grained and extended fine grained — each at 200,998,912 KB, which is the same 191.69 GiB described three ways, not three separate allocations.

The "VF" matters. This is a virtualized MI300X, not a bare card: firmware queries like ASD, PFP, MES and SOS answer *"Not supported on the given system"*, and `amd-smi` cannot see partitioning. MEC (32948), RLC (65), SDMA (24), SMC and the RAS/XGMI TAs do report.

#### The Control Plane: Twelve Tools, Tag-Scoped

`get_help` is the tool that describes the others, and its real output is the shape of the whole server:

```
📡 amd-gputools — DigitalOcean control plane for this project's AMD MI300 droplets.

Scoped to droplets tagged `gemma`; SSH as `root`; checkout at `/opt/amd-gputools`.

- list_droplets   — List every droplet tagged for this project, with state and address.
- droplet_status  — Show one tagged droplet in detail, by numeric id or by name.
- start_droplet   — Power on a tagged droplet. No-op if it is already active.
- stop_droplet    — Power off a tagged droplet.
- reboot_droplet  — Reboot a tagged droplet.
- action_status   — Poll a droplet action returned by start_droplet or stop_droplet.
- ssh_command     — Print the ssh command for a tagged droplet, with its current address.
- run_on_droplet  — Run one shell command on a tagged droplet over SSH and return its output.
- gpu_status      — Report the AMD GPUs on a tagged droplet, and say why if there are none.
- hardware_scan   — Inventory a droplet: host, GPU, firmware, ROCm packages and installed tools.
- list_gpu_sizes  — List DigitalOcean GPU droplet sizes and their prices, cheapest first.
- get_help        — List the tools this server exposes.

No create or destroy tools, by design: both are dollar-per-hour decisions, so they stay a
deliberate step in the DigitalOcean console.

Powering a droplet off does not stop DigitalOcean billing it.
```

Three design decisions are visible in that text.

**Every lookup is scoped by tag.** The server resolves droplets through `GET /v2/droplets?tag_name=gemma`, so a tool cannot name, reboot or power off a droplet nobody tagged for it. The token in use is an account token with full reach; the tag is what keeps a reboot from landing on someone else's box.

**There is no create and no destroy.** Not an oversight. Creating an MI300X starts a meter and destroying one throws away state, and neither is a thing a model should be one tool call away from. The console is a fine place for a decision that costs dollars per hour.

**Powering off does not stop the billing.** This is the misconception the server exists to correct, so it is written into the tool output rather than a README. DigitalOcean reserves the resources of a stopped droplet and charges the full rate; only destroying it stops the meter. `stop_droplet` says so in its return value every time, and a test enforces that the word survives refactoring.

#### Worked Example 1: Inventory

`list_droplets` takes no arguments. What comes back is the table, already tallied:

```
| Droplet | ID | Size | Status | Public IP | Region |
| --- | --- | --- | --- | --- | --- |
| `debian-gpu-mi300x1-192gb-devcloud-atl1` | `601142018` | gpu-mi300x1-192gb-devcloud | active | 165.245.134.217 | atl1 |

📡 1 droplet(s) tagged `gemma`: 1 active.
```

The last line is the point. **The tool counts; the model quotes.** One droplet is trivially countable by eye, but the same code path handles twenty, and a model asked to count twenty rows is a coin flip. Every tool in this server that returns rows also returns the tally — `list_gpu_sizes` sorts and names the cheapest, `hardware_scan` counts present and missing tools, `gpu_status` counts cards.

`droplet_status 601142018` drills in, and ends with the sentence that costs money:

```
**debian-gpu-mi300x1-192gb-devcloud-atl1** (`601142018`)

- status: `active`
- size: `gpu-mi300x1-192gb-devcloud` — 20 vCPU, 240 GB RAM, 720 GB disk
- region: atl1
- image: debian-13-x64
- public IPv4: 165.245.134.217
- tags: gemma
- created: 2026-09-16T18:09:45Z

✅ Reachable as `root@165.245.134.217`. Billing is running.
```

Note what is *not* here: a hardcoded IP anywhere in the server. Every tool resolves the address per call. A rebuilt droplet gets a new one, and a cached address turns into an SSH timeout that reads like a dead GPU.

#### Worked Example 2: One Round Trip For The Whole Box

`hardware_scan` answers "what can this box actually run" in a single SSH round trip, instead of ten tool calls that each pay connection setup. It ships one shell script that prints `<<<marker>>>`-fenced sections, and the server parses them into a report. Abridged real output:

```
## GPU

- `/dev/kfd`: **present**
- amdgpu module: loaded
- pci: `83:00.0 Processing accelerators [1200]: ... [Instinct MI300X VF] [1002:74b5]`
- gfx targets: `gfx942`  (1 GPU agent(s))
- device: AMD Instinct MI300X VF
- vram: `card0,205822885888,180717051904`

## Tools

- present (9): `rocm-smi`, `rocminfo`, `amd-smi`, `clinfo`, `docker`, `python3`, `pip3`, `git`, `tmux`
- **missing (2)**: `hipcc`, `podman`
- ROCM-SMI version: 2.2.0+unknown
- ROCM-SMI-LIB version: 7.2.0

## ROCm packages (8)

libhsa-runtime64-1:amd64  6.1.2-3
librocm-smi64-1           6.1.2-1
rocm-smi                  6.1.2-1
rocminfo                  6.1.2-2

## Python

- torch 2.9.1+rocm6.4 hip 6.4.43484-123eb5128 avail True
- ModuleNotFoundError: No module named 'vllm'
```

That last pair of lines is the whole vLLM-on-ROCm situation in two lines: **torch is installed and sees the GPU; vLLM is not installed and cannot be.** There is no prebuilt vLLM wheel for ROCm — PyPI's are CUDA builds, AMD's manylinux index carries torch and triton but no vllm, and building from source needs `hipcc` plus `rocblas`, `hipblaslt`, `miopen` and `rccl`, of which Debian 13 ships exactly one (`hipcc` 5.7.1, and the scan shows even that is absent here). vLLM comes from a container on this box, and the scan is what proves it rather than asserts it.

One parsing detail that cost a wrong number: `rocminfo` uses the key `Name:` for both the agent (`Name: gfx942`) and its ISA (`Name: amdgcn-amd-amdhsa--gfx942:sramecc+:xnack-`). Matching on `gfx` alone counts one MI300X as **two GPU agents**. The scan only counts a bare `gfx<digits>` target.

#### Worked Example 3: The Reboot That Is The Fix

A freshly provisioned GPU droplet here does not work, and looks like a driver problem. `lspci` shows the card. `/dev/dri/renderD128` exists. `/dev/kfd` does not, because `amdgpu` failed to bind the VF and unloaded itself. Nothing needs installing — Debian's in-tree `amdgpu` and the ROCm 6.1.2 userspace on the image are enough.

```
reboot_droplet("601142018")

📡 Rebooting `debian-gpu-mi300x1-192gb-devcloud-atl1` (`601142018`). Action `<id>` is
`in-progress`.

Poll `action_status`; SSH came back about 20s after the action completed when this was
measured. Then check `gpu_status`.
```

The action id in that reply is what `action_status` then polls, until the reboot action reports `completed` with its start and finish timestamps. The sequence is three calls and no guessing: reboot, poll, `gpu_status`.

An hour went into diagnosing a driver stack that was fine, which is why that instruction now lives in the tool's own docstring where a model will read it before it starts debugging. These dmesg lines are benign on a VF and are *not* the problem: `failed to load amdgpu/psp_13_0_6_cap.bin (-2)`, `Unsupported TA type: 8`, `TMZ feature not supported`.

The tool refuses to reboot anything that is not `active`, pointing at `start_droplet` instead, and it is annotated `DESTRUCTIVE` in its MCP annotations — whatever is running on the card dies with the reboot.

#### Worked Example 4: Two Readings That Were Wrong

Both were corrected the same way, and neither would have been caught by piping `ssh rocm-smi` into a model.

**The exit code is not a signal.** With the driver uninitialised, `rocm-smi` printed `Driver not initialized (amdgpu not found in modules)` to **stderr**, printed nothing to stdout, and **exited 0**. `amd-smi list` printed three ERROR lines and also exited 0. An early version of `gpu_status` branched on the exit status and reported `✅` above an empty table. Neither tool sets a useful exit code, so the current version does not consult the exit code at all: it parses `rocm-smi --json`, and if the parse does not yield at least one card it falls through to `amd-smi list`, and if that fails too it says so and explains why.

**A key name taken from memory.** On 2026-09-16 `gpu_status` returned this, and it reads as an idle card with nothing running:

```
| Card | Product | GPU use % | VRAM used % |
| --- | --- | --- | --- |
| card0 | Aqua Vanjaram [Instinct MI300X VF] | 0 | - |

📡 1 GPU(s) reported by rocm-smi.
```

The card was not idle. vLLM had been up for three hours holding **168.31 GiB of the 191.69 GiB**. The `-` came from the summariser looking up `GPU Memory Use (%)`, a spelling that does not exist. ROCM-SMI 2.2.0 on this box emits:

```json
{"card0": {"Device Name": "Aqua Vanjaram [Instinct MI300X VF]", "GPU use (%)": "0",
           "GPU Memory Allocated (VRAM%)": "87", "Card Series": "Aqua Vanjaram [Instinct MI300X VF]"}}
```

`GPU Memory Allocated (VRAM%)`. The unit test that should have caught it used the invented key too, so it passed against a payload no machine produces. The fix is three words in a lookup list. **A fixture written from memory tests the memory, not the tool**, so the regression test now carries the verbatim payload above, and the fallback spellings stay for other rocm-smi builds.

Both have the same shape: a successful-looking result from a tool that had failed. A model handed raw `rocm-smi` output reproduces both, because neither is visible without parsing what the tool printed. The control plane is where that parsing happens once.

#### Worked Example 5: Driving The Workload Over The Same Server

`run_on_droplet` hands one command to the remote shell as a single argument — the local side never invokes a shell, and `BatchMode=yes` means a wrong key fails instead of hanging on a password prompt. It is how the workload gets checked:

```
run_on_droplet("601142018", "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'; curl -s localhost:8000/v1/models")

✅ exited 0.

vllm	vllm/vllm-openai-rocm:nightly-rocm100	Up 3 hours
{"object":"list","data":[{"id":"google/gemma-4-E2B-it","max_model_len":32768,...}]}
```

And `list_gpu_sizes("mi300")` reads the catalogue rather than trusting a slug, which surfaces an oddity: **the devcloud size this droplet runs is not in `GET /v2/sizes` at all.** The public catalogue lists `gpu-mi300x1-192gb` at $2.59/hr; the droplet's own `size_slug` is `gpu-mi300x1-192gb-devcloud` at $1.99/hr, and it appears only on the droplet object. The tool shows the public catalogue and says which one it is showing.

#### Rules the Server Follows

Each of these came out of a specific failure:

- **Return markdown with an emoji prefix** — `✅` success, `❌` error, `📡` in progress, `🛑` stopping. Never a dict, never raw JSON for the model to interpret.
- **Errors are returned, not raised.** Every tool ends in `except Exception as exc: return _error(exc)`. An exception escaping a tool kills the server process for every later call in the session. A test walks the module and fails if any tool lacks the handler.
- **Never `shell=True`.** Every subprocess goes through one `run_command(cmd: list[str])` using `asyncio.create_subprocess_exec`. A test greps the source to keep it that way.
- **Push the arithmetic into the tool.** Counts, totals, sorts and the cheapest-size pick happen in Python. The model quotes a number it did not compute.
- **Put the measurement in the docstring.** `reboot_droplet` explains the `/dev/kfd` symptom; `gpu_status` explains that the exit code is worthless and names the date it was measured. The docstring is the model's context window, and it is the cheapest place to prevent an hour of misdiagnosis.
- **Annotate write and destructive tools.** `READ_ONLY`, `WRITE`, `DESTRUCTIVE` — so a client can gate the ones that cost money or kill a running job.
- **Tests run offline.** The whole `mcp` package is mocked before `server` is imported, so no test needs a token or a network.

#### What The Silicon Will Actually Compute

The control plane exists to report what the box is. The most consequential thing it reports is not capacity — it is **which numeric formats the matrix cores execute natively**, because that decides every quantization choice made afterwards, and it is the question a spec sheet answers least reliably.

Measured on the card 2026-09-16: 8192³ matmul, 30 iterations, torch 2.12.0+rocm10.0.0. The card was concurrently serving, so the absolute rates are depressed and **the ratios are the result**.

| dtype | ms | TFLOP/s | Spec peak | % of peak | vs bf16 |
| --- | ---: | ---: | ---: | ---: | ---: |
| bf16 | 1.655 | 664.3 | 1307.4 | 50.8% | 1.00x |
| fp16 | 1.660 | 662.5 | 1307.4 | 50.7% | 1.00x |
| **fp8 `e4m3fnuz`** | **0.938** | **1172.5** | 2614.9 | 44.8% | **1.77x** |
| int8 | 2.413 | 455.6 | 2614.9 | 17.4% | **0.69x** |

Four readings, and three of them contradict something plausible.

**bf16 and fp16 are the same number because they are the same hardware.** CDNA 3 runs both through one matrix pipeline at one peak of 1307.4 TFLOP/s. fp16 is not a cheaper precision you can trade down to here; it is the same speed with less exponent range. There is no decision to make between them.

**fp8 is the only format on this card faster than bf16** — 1.77x measured against 2.00x theoretical, which is what a genuinely native path looks like once the parts of a matmul that are not the multiply are accounted for.

**int8 measured slower than bf16, and the spec says it should not have.** AMD rates int8 at 2614.9 TOPS — exactly fp8's number, exactly double bf16's. On that basis the two are interchangeable and int8 is the better-supported choice. Measured, int8 reached 17.4% of its own peak and **0.69x bf16**. The silicon is not the problem; the kernels are, and `torch._int_mm` is not reaching a tuned path on this stack. **An equal number in a spec table is not an equal number on the machine.**

**fp4 is not present at all**, and the capability list says otherwise. `supported_quantization` on this platform includes `mxfp4` and `mxfp8`, and torch 2.12 defines `torch.float4_e2m1fn_x2`. Neither is a statement about this GPU. The hardware answers directly:

```
>>> torch._scaled_mm(a4, b4, scale_a=s, scale_b=s, out_dtype=torch.bfloat16)
NotImplementedError: Block-wise scaling for Float8_e8m0fnu is only supported on gfx950,gfx1250
```

vLLM gates on exactly the same boundary:

```python
@classmethod
def supports_mx(cls) -> bool:
    return any(gfx in _GCN_ARCH for gfx in ["gfx95", "gfx1250"])
```

`gfx942` is CDNA 3; MX formats arrive with CDNA 4 (`gfx950`, MI350X/MI355X). Here `supports_mx()` is `False`, and rather than refusing, the stack falls back to an emulation kernel whose entire forward pass is:

```python
dq_w  = dequant_mxfp4(layer.weight, layer.weight_scale, x.dtype)
qdq_x = self.quant_dequant_func(x)
return F.linear(qdq_x, dq_w, bias)
```

It widens the 4-bit weights back to bf16 **on every forward pass**, round-trips the activations through quantize-dequantize to reproduce fp4's error, and runs an ordinary bf16 `F.linear`. The result is bf16 speed, no resident saving while computing, dequantization overhead on top, and the full quantization error. Its own log line calls it simulated. It answers whether a model survives fp4 before the hardware that runs fp4 is bought, and it is not a serving path.

#### `fnuz` Is Not `fn`

fp8 being native makes a ready-made fp8 checkpoint from the Hub look like the short path. It is not. **CDNA 3 implements a different fp8 than Hopper and Blackwell do.** From `torch.finfo` on this box:

| | `e4m3fn` (NVIDIA, OCP) | `e4m3fnuz` (CDNA 3) |
| --- | ---: | ---: |
| Largest finite value | 448.0 | **240.0** |
| Smallest normal | 0.015625 | **0.0078125** |

The exponent bias differs by one. The cleanest way to see what that means is to read a single byte as both types:

```
bit pattern 0b01000000 as float8_e4m3fn   -> 2.0
bit pattern 0b01000000 as float8_e4m3fnuz -> 1.0
```

**One bit pattern, two values, a factor of two apart.** Reinterpreting an `e4m3fn` tensor as `e4m3fnuz` halves every number in it. At the top of the range it is worse than halved:

```
float8_e4m3fn   round-trip [1.0, 2.0, 240.0, 448.0] -> [1.0, 2.0, 240.0, 448.0]
float8_e4m3fnuz round-trip [1.0, 2.0, 240.0, 448.0] -> [1.0, 2.0, 240.0, nan]
```

448 is an ordinary weight on an H100 and is **NaN** on an MI300X.

Asking for the NVIDIA flavour fails at the call rather than silently:

```
float8_e4m3fn:   FAILED -> RuntimeError: HIPBLAS_STATUS_NOT_SUPPORTED
float8_e4m3fnuz: _scaled_mm OK  out=(4096, 4096) torch.bfloat16
```

vLLM agrees from its own side — `is_fp8_fnuz()` keys on the string `"gfx94"`, and `fp8_dtype()` returns `torch.float8_e4m3fnuz`. So the safe route is to quantize **online, from the bf16 weights**, and never to go looking for a checkpoint: the scales are then derived on the machine that will run them.

#### GGUF Is Not Compiled In At All

Worth stating because it fails differently from everything above — not gated, absent:

| Check | Result |
| --- | --- |
| `'gguf' in supported_quantization` | False |
| `'gguf' in QUANTIZATION_METHODS` (the global registry) | **False** |
| `vllm.model_executor.layers.quantization.gguf` | `ModuleNotFoundError` |
| ggml/gguf symbols in `_custom_ops` | **none** |

Older builds accepted `--quantization gguf`; this one has no such module. GGUF here means changing inference engine rather than passing a flag — and since its k-quants are weight-only anyway, dequantized in-kernel with the arithmetic still at bf16, it would not have unlocked a format the matrix cores lack.

**The answer for this card is one line: fp8 `e4m3fnuz`, quantized online, and nothing else.** Three sources pointed elsewhere to get there — AMD's peak table rates int8 identically to fp8, the platform capability list includes `mxfp4`, and the Hub's fp8 checkpoints are the wrong flavour.

#### The Workload, Briefly

The card is not idle hardware. It serves `google/gemma-4-E2B-it` through vLLM in a container, and that workload is what the memory figures below are measured against. The engine reports `dtype=torch.bfloat16, quantization=None`, matching the checkpoint's own `dtype: bfloat16` — so everything in this article is measured against an **unquantized** baseline, and the 1.77x above is available and unclaimed.

The deployment itself is the subject of a companion article and is not repeated here. One finding from it carries across: **the newest ROCm vLLM image AMD publishes cannot load Gemma 4 at all.** It lacks `Gemma4ModelArchConfigConvertor`, so a *global* read of `head_dim` raises during config parsing, before the GPU is touched — Gemma 4 runs 256-wide heads on its sliding-attention layers and 512 on its full-attention ones, and transformers 5.15+ reports that as a per-layer attribute. The oldest image works because its transformers predates per-layer attributes; the nightly works because it has the convertor. **The broken build sits between the two that work**, which is not a thing version intuition predicts.

#### Where The 191.69 GiB Goes

This is the number the control plane exists to report, so it is worth showing both sides of it. vLLM's own startup accounting for the serving run:

```
Free memory on device (191.36/191.69 GiB) on startup. Desired GPU memory utilization is
(0.9, 172.52 GiB). Actual usage is 11.88 GiB for consumed memory (weights + non-torch),
5.59 GiB for peak activation, and 3.9 GiB for CUDAGraph memory. Current kv cache memory
in use is 155.04 GiB.

GPU KV cache size: 9,026,017 tokens
Maximum concurrency for 32,768 tokens per request: 275.45x
init engine (profile, create kv cache, warmup model) took 84.91 s (compilation: 30.98 s)
```

| Claim | GiB |
| --- | --- |
| Weights + non-torch | 11.88 |
| Peak activation | 5.59 |
| CUDA graphs | 3.90 |
| KV cache in use | 155.04 |
| **vLLM's total** | **176.41** |
| rocm-smi resident VRAM | 168.31 (87%) |

The two disagree by about 8 GiB, and they are measured in different places — vLLM reports its own profiling arithmetic, `rocm-smi` reports what the driver sees resident. Both are quoted here rather than reconciled, because no experiment was run to explain the gap.

9,026,017 tokens of KV works out to roughly 18 KB per token at bf16: E2B shares KV across 20 of its 35 layers and runs one KV head at 256, so 32k of context is cheap here. `--max-model-len 32768` is a workload choice, not a capacity one — the checkpoint supports 131072 and this card would hold it. vLLM also volunteers that `--kv-cache-memory=182368940032` (169.84 GiB) would fully use the card.

Text, thinking, tool calling and vision were each exercised against the live endpoint — riddle with `reasoning` populated at 610 reasoning tokens, a `get_weather` call returning `finish_reason: tool_calls`, and a 158-byte synthetic red/blue checkerboard described correctly. **Audio is unreachable on every image tried**: E2B carries a conformer audio encoder, but none of these images ships the `vllm[audio]` extras (`librosa` and `soundfile` are both missing), so `--limit-mm-per-prompt '{"audio": 0}'` is the setting that matches, and it also skips reserving encoder memory for a path that cannot be used.

#### What This Does Not Cover

No serving throughput was measured. Nothing here is a serving benchmark: the KV figure is vLLM's own allocation report and no tokens per second number is claimed for the deployment.

The dtype table is a GEMM ratio rather than an end to end result. One matmul shape, on a card that was concurrently serving, which is why every absolute rate sits near half of peak and why the ratios are what is claimed. A 1.77x on `torch._scaled_mm` does not imply 1.77x tokens per second, because attention and kernel launch overhead do not shrink with the weights and for a 2B model they are a large share of decode.

The int8 result is a statement about this software stack rather than about CDNA 3. `torch._int_mm` reaching 17.4 percent of its own peak says a tuned kernel was not found, and a different library or a hand written MFMA path could plausibly close it. That was not attempted.

Three further gaps: fp8 was measured but never deployed, so the serving arm ran bf16 throughout and no output quality evaluation of fp8 on this checkpoint exists; the 8 GiB accounting difference between vLLM and `rocm-smi` is reported from both sides and not explained; and the card is a virtual function, so 191.69 GiB is what SR-IOV exposes and partitioning on a bare card was not examined.

#### Summary

The goal of this article was to manage one MI300X droplet entirely through MCP tools, and to read off the hardware which numeric formats its matrix cores execute. The key to the solution was parsing what each tool printed rather than branching on what it returned, and measuring every dtype on the card rather than reading a peak out of a table. The measured results were:

- fp8 `e4m3fnuz` is the only format on this card faster than bf16 — 1172.5 against 664.3
  TFLOP/s, 1.77x, on an 8192³ matmul.
- bf16 and fp16 are one pipeline at one peak, 664.3 and 662.5 TFLOP/s, so there is no choice
  to make between them.
- int8 is rated 2614.9 TOPS, identical to fp8 and double bf16, and measured 455.6 TFLOP/s.
  That is 17.4 percent of its own peak and 0.69x bf16.
- fp4 is not on `gfx942`. `supports_mx()` gates it to `gfx950` and `gfx1250`, and the
  fallback kernel dequantizes to bf16 on every forward pass.
- `e4m3fnuz` is not `e4m3fn`. The same byte is 2.0 on an H100 and 1.0 here, 448 round-trips
  to `NaN`, and an fp8 checkpoint built for NVIDIA is not drop-in. Quantize online from bf16.
- `rocm-smi` and `amd-smi` exit 0 when they fail. Parse the output; never branch on status.
- The VRAM column read `-` for hours because the summariser and its unit test both used a key
  name no machine emits. The real one is `GPU Memory Allocated (VRAM%)`.
- A freshly provisioned droplet needs one reboot. `/dev/kfd` is missing until then and
  nothing needs installing.
- Powering a droplet off does not stop the billing. Only destroying it does.
- 191.69 GiB, 304 CUs and PCIe Gen5 x16 on one card, holding 9,026,017 tokens of KV at
  275.45x concurrency — all of it at bf16, with the 1.77x available and unclaimed.

Scope: one `gpu-mi300x1-192gb-devcloud` droplet in `atl1` on 2026-09-16, a virtual function exposing all 304 CUs and 191.69 GiB. The dtype figures are a single 8192³ shape, 30 iterations after 5 warm-up, all four dtypes in one process against the same source buffers, measured inside the serving container on torch 2.12.0+rocm10.0.0 while vLLM held the card — which depresses every absolute rate and is why the ratios are quoted rather than the TFLOP/s. The bandwidth figure is bare metal torch 2.9.1+rocm6.4, a different stack, and is not comparable to the dtype rows. Spec peaks are AMD's published numbers and are not measurements. The $1.99 rate comes from AMD Developer Cloud and is not in `GET /v2/sizes`, which lists the public `gpu-mi300x1-192gb` at $2.59 instead. Every host, GPU and memory figure was read through the MCP server on the live box; the archived output is in `evidence/`.

The strategy for using MCP for single card platform management was validated with a incremental step by step approach.
