---
title: "Driving an MI300X Over MCP: Twelve Tools, One $1.99/Hour Card, and the Two Times the Control Plane Lied"
published: false
description: "An MCP control plane for a single AMD Instinct MI300X on AMD Developer Cloud: twelve tag-scoped tools for inventory, reboot, hardware scan and GPU state, with worked examples — and what the card measures at when you ask which numeric formats it actually executes. fp8 is 1.77x bf16; int8, which the spec rates identically, is 0.69x."
tags: amd, mcp, rocm, machinelearning
cover_image: https://raw.githubusercontent.com/xbill9/amd-gputools/main/devto-cover.fe2cfa4c.jpg
---

There are two halves to running a GPU box you do not own: **managing the platform**, and **running the workload**. This article is mostly about the first half. The workstation writing this has no AMD GPU and never will — the hardware is one MI300X droplet on AMD Developer Cloud, billed at $1.99 an hour, and everything that reaches it goes through an MCP server: twelve tools for inventory, power, reboot, hardware inventory, GPU state and remote execution.

Two things came out of it. The control plane is where the interesting engineering was, because that is where the measurements come from, and twice in one day it reported a healthy box that was not the box that existed. And once you can ask the card questions cheaply, the question worth asking is **which numeric formats it will actually execute** — where the answer contradicts the spec sheet, the library and the vendor's own capability list, one each.

The repository is at [github.com/xbill9/amd-gputools](https://github.com/xbill9/amd-gputools).

## The Box, In Detail

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

## The Control Plane: Twelve Tools, Tag-Scoped

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

## Worked Example 1: Inventory

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

## Worked Example 2: One Round Trip For The Whole Box

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

## Worked Example 3: The Reboot That Is The Fix

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

## Worked Example 4: The Two Times The Control Plane Lied

This is the section that justifies writing a control plane instead of piping `ssh rocm-smi` into a model.

**Lie one: the exit code.** With the driver uninitialised, `rocm-smi` printed `Driver not initialized (amdgpu not found in modules)` to **stderr**, printed nothing to stdout, and **exited 0**. `amd-smi list` printed three ERROR lines and also exited 0. An early version of `gpu_status` trusted the exit status and reported a cheerful `✅` above an empty table. Neither tool sets a useful exit code, so the current version does not consult the exit code at all: it parses `rocm-smi --json`, and if the parse does not yield at least one card it falls through to `amd-smi list`, and if that fails too it says so and explains why.

**Lie two: a key name I guessed.** On 2026-09-16 `gpu_status` returned this, and I read it aloud as an idle card with nothing running:

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

`GPU Memory Allocated (VRAM%)`. The unit test that should have caught it used the invented key too, so it passed against a payload no machine produces. The fix is three words in a lookup list; the lesson is that **a fixture you wrote from memory tests your memory, not the tool**. The regression test now carries the verbatim payload above, and the fallback spellings stay for other rocm-smi builds.

The shape of both bugs is identical: a plausible-looking success from a tool that had failed. A model handed raw `rocm-smi` output reproduces both. The control plane is where you pay once to get it right.

## Worked Example 5: Driving The Workload Over The Same Server

`run_on_droplet` hands one command to the remote shell as a single argument — the local side never invokes a shell, and `BatchMode=yes` means a wrong key fails instead of hanging on a password prompt. It is how the workload gets checked:

```
run_on_droplet("601142018", "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'; curl -s localhost:8000/v1/models")

✅ exited 0.

vllm	vllm/vllm-openai-rocm:nightly-rocm100	Up 3 hours
{"object":"list","data":[{"id":"google/gemma-4-E2B-it","max_model_len":32768,...}]}
```

And `list_gpu_sizes("mi300")` reads the catalogue rather than trusting a slug — which surfaces a genuine oddity: **the devcloud size this droplet runs is not in `GET /v2/sizes` at all.** The public catalogue lists `gpu-mi300x1-192gb` at $2.59/hr; the droplet's own `size_slug` is `gpu-mi300x1-192gb-devcloud` at $1.99/hr, and it appears only on the droplet object. The tool shows the public catalogue, and says so, rather than pretending the two are the same list.

## Designing Tools A Model Will Not Misread

The rules this server follows, all of them earned:

- **Return markdown with an emoji prefix** — `✅` success, `❌` error, `📡` in progress, `🛑` stopping. Never a dict, never raw JSON for the model to interpret.
- **Errors are returned, not raised.** Every tool ends in `except Exception as exc: return _error(exc)`. An exception escaping a tool kills the server process for every later call in the session. A test walks the module and fails if any tool lacks the handler.
- **Never `shell=True`.** Every subprocess goes through one `run_command(cmd: list[str])` using `asyncio.create_subprocess_exec`. A test greps the source to keep it that way.
- **Push the arithmetic into the tool.** Counts, totals, sorts and the cheapest-size pick happen in Python. The model quotes a number it did not compute.
- **Put the measurement in the docstring.** `reboot_droplet` explains the `/dev/kfd` symptom; `gpu_status` explains that the exit code is worthless and names the date it was measured. The docstring is the model's context window, and it is the cheapest place to prevent an hour of misdiagnosis.
- **Annotate write and destructive tools.** `READ_ONLY`, `WRITE`, `DESTRUCTIVE` — so a client can gate the ones that cost money or kill a running job.
- **Tests run offline.** The whole `mcp` package is mocked before `server` is imported, so no test needs a token or a network.

## What The Silicon Will Actually Compute

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

**fp4 is not there at all**, and this is the one the tooling actively misleads you about. `supported_quantization` on this platform lists `mxfp4` and `mxfp8`; torch 2.12 defines `torch.float4_e2m1fn_x2`. Neither is a statement about this GPU. Ask the hardware and it is unambiguous:

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

It widens the 4-bit weights back to bf16 **on every forward pass**, round-trips the activations through quantize-dequantize to reproduce fp4's error, and runs an ordinary bf16 `F.linear`. You get bf16 speed, no resident saving while computing, plus dequantization overhead, plus the full quantization error. That kernel is an instrument for answering *"would this model survive fp4"* before buying hardware that runs it. It is not a deployment path, and its own log line says so.

### The trap inside fp8: `fnuz` is not `fn`

fp8 being native makes it tempting to pull a ready-made fp8 checkpoint off the Hub. Do not. **CDNA 3 implements a different fp8 than Hopper and Blackwell do.** From `torch.finfo` on this box:

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

In the other direction the hardware at least fails loudly rather than quietly:

```
float8_e4m3fn:   FAILED -> RuntimeError: HIPBLAS_STATUS_NOT_SUPPORTED
float8_e4m3fnuz: _scaled_mm OK  out=(4096, 4096) torch.bfloat16
```

vLLM agrees from its own side — `is_fp8_fnuz()` keys on the string `"gfx94"`, and `fp8_dtype()` returns `torch.float8_e4m3fnuz`. So the safe route is to quantize **online, from the bf16 weights**, and never to go looking for a checkpoint: the scales are then derived on the machine that will run them.

### GGUF is not compiled in at all

Worth stating because it fails differently from everything above — not gated, absent:

| Check | Result |
| --- | --- |
| `'gguf' in supported_quantization` | False |
| `'gguf' in QUANTIZATION_METHODS` (the global registry) | **False** |
| `vllm.model_executor.layers.quantization.gguf` | `ModuleNotFoundError` |
| ggml/gguf symbols in `_custom_ops` | **none** |

Older builds accepted `--quantization gguf`; this one has no such module. GGUF here means changing inference engine rather than passing a flag — and since its k-quants are weight-only anyway, dequantized in-kernel with the arithmetic still at bf16, it would not have unlocked a format the matrix cores lack.

**The whole answer for this card is one line: fp8 `e4m3fnuz`, quantized online, and nothing else.** That is a surprising amount of ground to cover for a one-line conclusion, which is the point — the spec sheet, the library and the capability list each pointed somewhere else.

## The Workload, Briefly

The card is not idle hardware. It serves `google/gemma-4-E2B-it` through vLLM in a container, and that workload is what the memory figures below are measured against. The engine reports `dtype=torch.bfloat16, quantization=None`, matching the checkpoint's own `dtype: bfloat16` — so everything in this article is measured against an **unquantized** baseline, and the 1.77x above is available and unclaimed.

The deployment itself is the subject of a companion article and is not repeated here. One finding from it is worth carrying across, because it is the same shape as the two lies in Worked Example 4: **the newest ROCm vLLM image AMD publishes cannot load Gemma 4 at all.** It lacks `Gemma4ModelArchConfigConvertor`, so a *global* read of `head_dim` raises during config parsing, before the GPU is touched — Gemma 4 runs 256-wide heads on its sliding-attention layers and 512 on its full-attention ones, and transformers 5.15+ reports that as a per-layer attribute. The oldest image works because its transformers predates per-layer attributes; the nightly works because it has the convertor. **The broken build sits between the two that work**, which is not a thing version intuition predicts.

## Where The 191.69 GiB Goes

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

Text, thinking, tool calling and vision were each exercised against the live endpoint — riddle with `reasoning` populated at 610 reasoning tokens, a `get_weather` call returning `finish_reason: tool_calls`, and a 158-byte synthetic red/blue checkerboard described correctly. **Audio is unreachable on every image tried**: E2B carries a conformer audio encoder, but none of these images ships the `vllm[audio]` extras (`librosa` and `soundfile` are both missing), so `--limit-mm-per-prompt '{"audio": 0}'` is the honest setting and it also skips reserving encoder memory for a path that cannot be used.

## What Was Controlled

| | How it was held |
| --- | --- |
| Checkpoint | one `/opt/hf-cache` snapshot, mounted into every arm, never re-downloaded |
| Flags | identical across images apart from the entrypoint form |
| Card | one MI300X, one arm at a time |
| Arch check | `get_arch_list()` read with devices mapped in, plus a live bf16 GEMM |
| Hardware figures | read through the MCP server on the live box, not from a spec sheet |
| Failure mode | read from the traceback's own frame, not inferred from the symptom |
| dtype comparison | one shape (8192³), one process, 30 iterations, 5 warm-up, same buffers cast per dtype |

## What Was Not Controlled

- **Serving throughput was not measured.** Nothing here is a serving benchmark. The KV figure is vLLM's own allocation report, and no tokens/sec number is claimed.
- **The dtype table is a GEMM ratio, not an end-to-end result.** One matmul shape, on a card that was concurrently serving — which is why every absolute rate sits near half of peak and why the ratios, not the TFLOP/s, are what is claimed. A 1.77x on `torch._scaled_mm` does **not** imply 1.77x tokens/sec: attention and kernel-launch overhead do not shrink with the weights, and for a 2B model they are a large share of decode.
- **The int8 result is about this software stack, not about CDNA 3.** `torch._int_mm` at 17.4% of peak says a tuned kernel was not reached. A different library, or a hand-written MFMA path, could plausibly close it; that was not attempted.
- **fp8 was measured, not deployed.** The serving arm ran bf16 throughout. No quality evaluation of fp8 on this checkpoint was run, and small models have less redundancy to spend on quantization than large ones.
- **The 8 GiB accounting gap was not explained**, only reported from both sides.
- **One nightly build.** `af1c0149` on 2026-09-16. Nightlies rebuild daily and this conclusion has a shelf life.
- **VF, not bare metal.** 191.69 GiB exposed on a virtual function; partitioning on a bare card was not examined.
- **One droplet, one tag.** The tag scoping is tested against a mocked API, not against a second real droplet.

## Summary

- **The control plane is the product.** Twelve tag-scoped MCP tools — inventory, power, reboot, scan, GPU state, remote exec — with no create and no destroy, because both are dollar-per-hour decisions.
- **`rocm-smi` and `amd-smi` exit 0 when they fail.** Parse the output; never branch on the status.
- **A fixture written from memory tests your memory.** The VRAM column read `-` for hours because the summariser and its unit test both used a key name no machine emits; the real one is `GPU Memory Allocated (VRAM%)`.
- **One reboot fixes a freshly provisioned GPU droplet.** `/dev/kfd` is missing until you reboot once, and nothing needs installing.
- **Powering off does not stop the billing.** Only destroying the droplet does.
- **fp8 `e4m3fnuz` is the only format on this card faster than bf16** — 1.77x measured. bf16 and fp16 are one pipeline at one peak, so there is no choice between them.
- **int8 is rated identically to fp8 and measured 0.69x bf16.** An equal number in a spec table is not an equal number on the machine.
- **fp4 does not exist on `gfx942`** — `supports_mx()` gates it to `gfx950`/`gfx1250`, and the fallback emulation kernel dequantizes to bf16 every forward pass. `mxfp4` in a capability list is not a hardware claim.
- **`e4m3fnuz` is not `e4m3fn`.** One bit pattern means 2.0 on an H100 and 1.0 here, 448 becomes `NaN`, and an fp8 checkpoint built for NVIDIA is not drop-in. Quantize online from bf16.
- **191.69 GiB, 304 CUs, PCIe Gen5 x16**, and 9,026,017 tokens of KV at 275x concurrency on one card — all of it at bf16, with the 1.77x still unclaimed.
