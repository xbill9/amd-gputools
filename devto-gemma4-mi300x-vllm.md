---
title: "Driving an MI300X Over MCP: Twelve Tools, One $1.99/Hour Card, and the Two Times the Control Plane Lied"
published: false
description: "An MCP control plane for a single AMD Instinct MI300X on AMD Developer Cloud: twelve tag-scoped tools for inventory, reboot, hardware scan and GPU state, with worked examples. The workload it manages is Gemma 4 on vLLM — where the newest vendor image cannot load the model at all."
tags: amd, mcp, vllm, rocm
---

There are two halves to running a GPU box you do not own: **managing the platform**, and **running the workload**. This article is mostly about the first half. The workstation writing this has no AMD GPU and never will — the hardware is one MI300X droplet on AMD Developer Cloud, billed at $1.99 an hour, and everything that reaches it goes through an MCP server: twelve tools for inventory, power, reboot, hardware inventory, GPU state and remote execution.

The workload is `google/gemma-4-E2B-it` served by vLLM, and it earns its section — three ROCm images were tried and **the newest vendor image cannot load the model at all**. But the interesting engineering was in the control plane, because that is where the measurements come from, and twice in one day the control plane reported a healthy box that was not the box that existed.

The repository is at https://github.com/xbill9/amd-gputools.

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
| VRAM | 205,822,885,888 B = **191.69 GiB** | 180,717,051,904 B = 168.31 GiB (87%) |
| VIS_VRAM (host-visible) | 205,822,885,888 B = 191.69 GiB | 180,717,051,904 B = 168.31 GiB |
| GTT (system memory aperture) | 126,676,250,624 B = 117.98 GiB | 21,327,872 B = 0.02 GiB |

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

The action id in that reply is what `action_status` then polls, until the reboot action
reports `completed` with its start and finish timestamps. The sequence is three calls and
no guessing: reboot, poll, `gpu_status`.

An hour went into diagnosing a driver stack that was fine, which is why that instruction now lives in the tool's own docstring where a model will read it before it starts debugging. These dmesg lines are benign on a VF and are *not* the problem: `failed to load amdgpu/psp_13_0_6_cap.bin (-2)`, `Unsupported TA type: 8`, `TMZ feature not supported`.

The tool refuses to reboot anything that is not `active`, pointing at `start_droplet` instead, and it is annotated `DESTRUCTIVE` in its MCP annotations — whatever is running on the card dies with the reboot.

## Worked Example 4: The Two Times The Control Plane Lied

This is the section that justifies writing a control plane instead of piping `ssh rocm-smi` into a model.

**Lie one: the exit code.** With the driver uninitialised, `rocm-smi` printed `Driver not initialized (amdgpu not found in modules)` to **stderr**, printed nothing to stdout, and **exited 0**. `amd-smi list` printed three ERROR lines and also exited 0. An early version of `gpu_status` trusted the exit status and reported a cheerful `✅` above an empty table. Neither tool sets a useful exit code, so the current version does not consult the exit code at all: it parses `rocm-smi --json`, and if the parse does not yield at least one card it falls through to `amd-smi list`, and if that fails too it says so and explains why.

**Lie two: a key name I guessed.** Earlier today `gpu_status` returned this, and I read it aloud as an idle card with nothing running:

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

## The Workload: Three vLLM Images, One Card

With the platform managed, the workload. `google/gemma-4-E2B-it` — Apache-2.0 and **ungated**, which matters more than it sounds like, since a gated repo turns a first run into a Hugging Face login problem instead of a vLLM problem. There is no `gemma-4-2B`: the lineup is E2B, E4B, 12B, 26B-A4B and 31B, and E2B is the effective-2B MatFormer member at 10.25 GB of bf16 safetensors.

| | `rocm/vllm` 0.19.1 | `rocm/vllm` 0.27.0 | `vllm-openai-rocm:nightly-rocm100` |
| --- | --- | --- | --- |
| Built | 2026-05-19 | 2026-08-27 | 2026-09-16 |
| vLLM | 0.19.1 | 0.27.1.dev5 | 0.29.1rc1.dev187+gaf1c014 |
| torch | 2.10.0+rocm7.13.0 | 2.12.0+rocm10.0.0 | 2.12.0+rocm10.0.0 |
| transformers | 5.8.1 | 5.16.1 | 5.17.0 |
| Size | 44.3 GB | 61.7 GB | 35.1 GB |
| `Gemma4ModelArchConfigConvertor` | n/a | **no** | yes |
| Serves E2B | yes | **no** | yes |

The middle column is the newest image AMD publishes, the largest of the three, passes a `gfx942` bf16 matmul, and registers the Gemma 4 model class. It still cannot serve the model. It dies in config parsing, before the GPU is touched:

```
AmbiguousGlobalPerLayerAttributeError: 'head_dim' is a per-layer attribute and may vary
across layers. Access it via the individual layer configs instead.

vllm/transformers_utils/model_arch_config_convertor.py:608 in get_head_size
    head_dim = getattr(self.hf_text_config, "head_dim", 0)
```

Gemma 4 uses dual attention: in E2B's `config.json`, `text_config.head_dim` is 256 and `global_head_dim` is 512, with `layer_types` alternating 31 sliding-attention layers against 4 full-attention ones. Transformers 5.15+ models that honestly — `head_dim` becomes a per-layer attribute and a *global* read raises instead of silently returning one of the two. vLLM's generic `get_head_size()` asks with a default, and **the default never applies**: `getattr`'s third argument only catches `AttributeError`, and this is a subclass raised deliberately by the accessor. The line that looks like it covers the case is the line that fails. Upstream fixes it with a `Gemma4ModelArchConfigConvertor` that builds per-layer configs and never asks globally; that class is absent from the 0.27.1.dev5 build.

So **the broken build sits between the two that work** — 0.19.1 escapes from the other side, because transformers 5.8.1 predates per-layer attributes and reads a flat 256. This is a version-pairing bug, not a ROCm one.

Two checks cost nothing and rule out a 35–62 GB pull. Read the convertor registry — `MODEL_ARCH_CONFIG_CONVERTORS.get("gemma4")`, importing `vllm.config` first or a circular import makes a good image look broken. And read `torch.cuda.get_arch_list()` **with `/dev/kfd` and `/dev/dri` mapped in**, or it returns `[]`, which reads exactly like "no kernels for you" and is nothing of the sort.

One more trap worth stating: the two image families take different argv. `vllm/vllm-openai-rocm` sets `ENTRYPOINT ["vllm","serve"]`, so the model id is the first argument; AMD's images have no entrypoint and need `vllm serve` spelled out. And pass the GPU groups as **numeric GIDs resolved on the host** — `--group-add render` fails outright, because the Ubuntu-based image has no `render` group of its own. Here they are `video:44`, `render:991`.

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

## What Was Not Controlled

- **Throughput was not measured.** Nothing here is a benchmark. The KV figure is vLLM's own allocation report; the only other rate quoted is a 557.7 TFLOP/s bare-metal fp16 matmul at 8192³, which is a smoke test, not a benchmark.
- **The 8 GiB accounting gap was not explained**, only reported from both sides.
- **The 0.19.1 arm was not run to completion.** It reached `Application startup complete` and was torn down to free the card.
- **One nightly build.** `af1c0149` on 2026-09-16. Nightlies rebuild daily and this conclusion has a shelf life.
- **VF, not bare metal.** 191.69 GiB exposed on a virtual function; partitioning on a bare card was not examined.
- **One droplet, one tag.** The tag scoping is tested against a mocked API, not against a second real droplet.

## Summary

- **The control plane is the product.** Twelve tag-scoped MCP tools — inventory, power, reboot, scan, GPU state, remote exec — with no create and no destroy, because both are dollar-per-hour decisions.
- **`rocm-smi` and `amd-smi` exit 0 when they fail.** Parse the output; never branch on the status.
- **A fixture written from memory tests your memory.** The VRAM column read `-` for hours because the summariser and its unit test both used a key name no machine emits; the real one is `GPU Memory Allocated (VRAM%)`.
- **One reboot fixes a freshly provisioned GPU droplet.** `/dev/kfd` is missing until you reboot once, and nothing needs installing.
- **Powering off does not stop the billing.** Only destroying the droplet does.
- **The newest vendor vLLM image cannot load Gemma 4** — no `Gemma4ModelArchConfigConvertor`, so `head_dim` raises during config parsing. The container nightly can, and is 26 GB smaller.
- **191.69 GiB, 304 CUs, PCIe Gen5 x16**, and 9,026,017 tokens of KV at 275x concurrency on one card.
