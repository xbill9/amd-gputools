---
title: "Three vLLM Images, One MI300X: What Loads Gemma 4, and What Doesn't"
published: false
description: "Porting Gemma 4 E2B to a single MI300X on Debian 13. Three ROCm vLLM images, same GPU, same checkpoint, same flags. The newest vendor image cannot parse the config; the nightly can. The deciding line is one getattr."
tags: amd, vllm, gemma, rocm
---

This article ports `google/gemma-4-E2B-it` onto one AMD Instinct MI300X and serves it through vLLM with thinking, tool calling and vision live. Three ROCm images were tried against the same checkpoint on the same card. **Two serve it and one cannot load it at all**, and the thing that separates them is a single attribute read during config parsing, months before any kernel runs.

The repository is at https://github.com/xbill9/amd-gputools — the MI300 toolkit this rig was ported on. The rig itself, with an MCP server driving it, is [`gpu-vllm-mi300x-2b`](https://github.com/xbill9/gemma4-dev/tree/main/gpu-vllm-mi300x-2b) in the gemma4-dev monorepo.

## What Is The Rig?

One GPU droplet, reached through AMD Developer Cloud, which is DigitalOcean underneath.

| | |
| --- | --- |
| GPU | AMD Instinct MI300X VF, `gfx942:sramecc+:xnack-` |
| VRAM | 191.69 GiB usable, 205822885888 B total |
| OS | Debian 13 (trixie), kernel `6.12.94+deb13-amd64` |
| Disk | 709 G, 13% used after cleanup |
| Model | `google/gemma-4-E2B-it`, 10.28 GB of bf16 safetensors |
| Endpoint | `:8000`, OpenAI-compatible |

The model slot is worth a sentence, because the obvious name for it does not exist. **There is no `gemma-4-2B`.** The Gemma 4 lineup is E2B, E4B, 12B, 26B-A4B and 31B; E2B is the effective-2B member, MatFormer-style, and the checkpoint on disk is 10.25 GB in one `model.safetensors`. It is Apache-2.0 and ungated, so no HF token is needed — which matters more than it sounds like, since a gated repo turns a first run into a login problem instead of a vLLM problem.

vLLM's own Gemma 4 recipe lists E2B as `1x MI300X/MI325X/MI350X/MI355X` at BF16, so this rig is the smallest supported AMD configuration rather than a stunt.

## Step 1: Cache The Weights Before Choosing An Image

The weights are image-independent and every arm needs them, so they go first, into a host directory that gets mounted into whichever container wins:

```shell
docker run -d --name hf-dl -v /opt/hf-cache:/root/.cache/huggingface \
  --entrypoint python3 <any-vllm-image> \
  -c "from huggingface_hub import snapshot_download; \
      snapshot_download('google/gemma-4-E2B-it', max_workers=8)"
```

Nine files, 11 seconds, 9.6 G resident. Mount it at `/root/.cache/huggingface` in the serving container and an image swap costs nothing in re-download — which is the only reason trying three images was cheap.

## Step 2: Check The Image Before Pulling It

A ROCm vLLM image is 35-62 GB. Two checks cost nothing and rule out most of them.

**Does the build carry your architecture?** `torch.cuda.get_arch_list()` answers this, but only with devices mapped in:

```shell
docker run --rm --device=/dev/kfd --device=/dev/dri \
  --group-add 44 --group-add 991 \
  --security-opt seccomp=unconfined --security-opt label=disable \
  --entrypoint python3 <image> -c "
import torch
print(torch.cuda.get_arch_list())
p = torch.cuda.get_device_properties(0); print(p.name, p.gcnArchName)
a = torch.randn(4096,4096,device='cuda',dtype=torch.bfloat16); b = a@a
torch.cuda.synchronize(); print('matmul ok', float(b.float().abs().mean()))
"
```

**Without the devices the same call returns `[]`**, which reads exactly like "this build has no kernels for you" and is nothing of the sort. Map `/dev/kfd` and `/dev/dri` in for the check or don't believe the answer.

**Does the vLLM in it know your model?** The registry is authoritative and free to read:

```shell
docker run --rm --entrypoint python3 <image> -c "
import vllm.config
from vllm.transformers_utils.model_arch_config_convertor import MODEL_ARCH_CONFIG_CONVERTORS as M
print(M.get('gemma4'))
"
```

Import `vllm.config` first. Reaching into `vllm.transformers_utils.*` directly raises a circular-import `ImportError` that looks like a broken image and isn't.

## Step 3: The Three Images

| | `rocm/vllm` 0.19.1 | `rocm/vllm` 0.27.0 | `vllm-openai-rocm:nightly-rocm100` |
| --- | --- | --- | --- |
| Built | 2026-05-19 | 2026-08-27 | 2026-09-16 |
| vLLM | 0.19.1 | 0.27.1.dev5 | 0.29.1rc1.dev187+gaf1c014 |
| torch | 2.10.0+rocm7.13.0 | 2.12.0+rocm10.0.0 | 2.12.0+rocm10.0.0 |
| transformers | 5.8.1 | 5.16.1 | 5.17.0 |
| Size | 44.3 GB | 61.7 GB | 35.1 GB |
| `Gemma4ForConditionalGeneration` | yes | yes | yes |
| `Gemma4ModelArchConfigConvertor` | n/a | **no** | yes |
| Serves E2B | yes | **no** | yes |

The middle column is the interesting one. It is the newest image AMD publishes, it is the largest of the three, its `gfx942` bf16 matmul passes, and it registers the Gemma 4 model class. It still cannot serve the model.

## Why Does The Newest Vendor Image Fail?

It dies in config parsing, before the GPU is touched:

```
AmbiguousGlobalPerLayerAttributeError: 'head_dim' is a per-layer attribute
and may vary across layers. Access it via the individual layer configs
instead (e.g. config.per_layer_config[i].head_dim).
```

```
vllm/transformers_utils/model_arch_config_convertor.py:608 in get_head_size
    head_dim = getattr(self.hf_text_config, "head_dim", 0)
```

Gemma 4 uses dual attention. In `config.json` for E2B, `text_config.head_dim` is 256 and `text_config.global_head_dim` is 512, and `layer_types` alternates 31 sliding-attention layers against 4 full-attention ones. Transformers 5.15 and later model that honestly: `head_dim` becomes a per-layer attribute and a *global* read of it raises rather than silently returning one of the two values.

vLLM's generic `get_head_size()` asks for it with a default. The default never applies, because the attribute does not resolve to a missing value — it raises on access. `getattr`'s third argument only catches `AttributeError`, and this is a subclass of it that the accessor raises deliberately, so the fallback that looks like it covers this case is the exact line that fails.

Upstream fixes it by never asking globally:

```python
class Gemma4ModelArchConfigConvertor(ModelArchConfigConvertorBase):
    def get_per_layer_hf_configs(self):
        # Gemma4 uses a larger head dimension, and sometimes more KV heads, on
        # its full attention layers than on its sliding ones. Transformers
        # >= 5.15.0 says so in the config; before that the values are flat
        # attributes picked apart by `layer_types`, so build the per-layer
        # configs here instead.
```

That class is registered for `gemma4`, `gemma4_text`, `gemma4_unified` and the MoE variants. It is absent from the 0.27.1.dev5 build, so that build falls through to the base convertor and raises. **This is a version-pairing bug, not a ROCm one** — the same image runs bf16 GEMMs on `gfx942` correctly, and would serve a model whose head dimensions are uniform.

The 0.19.1 image escapes it from the other side: transformers 5.8.1 predates per-layer attributes entirely, so `head_dim` is a flat 256 and the old code path reads it without complaint.

## Is The Nightly Channel The Same As The Nightly Image?

No, and the documented one is the stale one.

vLLM's Gemma 4 recipe gives a ROCm pip install against `wheels.vllm.ai/rocm/nightly/rocm721`. That index currently tops out at:

```
vllm-0.20.2rc1.dev15+g321fa2d6d.rocm721-cp312-cp312-manylinux_2_34_x86_64.whl
```

which is **older than the 0.27.1.dev5 that just failed**, and far older than the container nightly. The `vllm/vllm-openai-rocm` image repo, by contrast, publishes `nightly` and `nightly-rocm100` rebuilt daily, each also tagged by commit SHA. For a model this recent the container nightlies are the only current route, and the pip channel named in the docs will quietly hand you something a generation behind.

## Step 4: Serve It

```shell
docker run -d --name vllm \
  --restart unless-stopped \
  --ipc host --shm-size 16g \
  --device /dev/kfd --device /dev/dri \
  --group-add 44 --group-add 991 \
  --security-opt seccomp=unconfined --security-opt label=disable \
  -v /opt/hf-cache:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai-rocm:nightly-rocm100 \
    google/gemma-4-E2B-it \
    --host 0.0.0.0 --port 8000 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.90 \
    --enable-auto-tool-choice \
    --reasoning-parser gemma4 \
    --tool-call-parser gemma4 \
    --chat-template /app/vllm/examples/tool_chat_template_gemma4.jinja \
    --limit-mm-per-prompt '{"image": 4, "audio": 0}' \
    --async-scheduling
```

Two details that differ from the vendor images. The official image's `ENTRYPOINT` is `["vllm","serve"]`, so **the model id is the first argument, not `vllm serve <model>`** — repeating the subcommand gets you an unrecognised-arguments error. And the tool chat template ships inside the image at `/app/vllm/examples/tool_chat_template_gemma4.jinja`, so it needs no mount; the copy in the vLLM repo is the same file if you would rather pin it.

The resulting KV budget on one card:

```
GPU KV cache size: 9,026,017 tokens
Maximum concurrency for 32,768 tokens per request: 275.45x
```

E2B shares KV across 20 of its 35 layers and runs one KV head at 256, so 32k of context costs almost nothing here. `--max-model-len 32768` is a workload choice, not a capacity one — the checkpoint supports 131072 and this card would hold it.

## Does It Actually Do The Four Things?

A model that starts is not a model that works. Each capability was exercised against the live endpoint rather than inferred from a flag being accepted.

| Capability | Probe | Result |
| --- | --- | --- |
| Text | "what GPU architecture is an MI300X?" | "based on the CDNA 3 architecture" |
| Thinking | snail-in-a-well riddle, `enable_thinking: true` | `reasoning` populated, 610 reasoning tokens, correct |
| Tool calling | one `get_weather` tool, `tool_choice: auto` | `finish_reason: tool_calls`, `{"city": "Reykjavik"}` |
| Vision | 64x64 synthetic red/blue checkerboard, base64 | "alternating bright red and royal blue squares" |

The vision probe is a generated PNG rather than a fetched one on purpose: 158 bytes, no network, and the expected answer is known before the model answers it.

Thinking shows up in `usage.completion_tokens_details.reasoning_tokens`, and it is on by default for this template — the plain text probe above spent 32 reasoning tokens without being asked. Budget `max_tokens` accordingly.

## What Does Not Work

**Audio.** E2B carries a conformer audio encoder, and vLLM will not use it on any of these three images, because none of them ships the `vllm[audio]` extras:

```
librosa    MISSING
soundfile  MISSING
torchaudio OK
PIL        OK
```

This is not a version problem and does not improve on the nightly. It needs a derived image adding the two packages. Until then `--limit-mm-per-prompt '{"audio": 0}'` is the honest setting — it also skips the audio-encoder memory allocation during profiling rather than reserving it for a path that cannot be reached.

## What Was Controlled

| | How it was held |
| --- | --- |
| Checkpoint | one `/opt/hf-cache` snapshot, mounted into every arm, never re-downloaded |
| Flags | identical across images apart from the entrypoint form |
| Card | one MI300X, one arm at a time |
| Arch check | `get_arch_list()` read with devices mapped in, plus a live bf16 GEMM |
| Failure mode | read from the traceback's own frame, not inferred from the symptom |

## What Was Not Controlled

- **Throughput was not measured.** Nothing here is a benchmark. The KV figure is vLLM's own allocation report, and the only rate quoted is an idle-log artifact.
- **The 0.19.1 arm was not run to completion.** It reached `Application startup complete` on this checkpoint and was then torn down to free the card. It is a working image, not a measured one.
- **One nightly build.** `af1c0149` on 2026-09-16. Nightlies rebuild daily and this conclusion has a shelf life; the SHA-pinned tag exists for exactly that reason.
- **VF, not bare metal.** This is an MI300X VF with 191.69 GiB exposed. Partitioning behaviour on a bare card was not examined.

## Summary

The goal was to get Gemma 4 serving on one MI300X with every capability the checkpoint claims. The key to the solution was reading the model-architecture convertor registry out of a candidate image before pulling it, rather than diagnosing a 62 GB image after it failed. The results were:

- **The newest vendor image cannot load Gemma 4.** `rocm/vllm` at vLLM 0.27.1.dev5 lacks `Gemma4ModelArchConfigConvertor` and raises on `head_dim` during config parsing.
- **The container nightly can**, at vLLM 0.29.1rc1.dev187 with transformers 5.17.0, and is 26 GB smaller than the image that fails.
- **The documented pip nightly channel is stale** at 0.20.2rc1, older than the build that fails.
- Text, thinking, tool calling and vision all verified live; **audio is unreachable on every image tried**, for want of two Python packages.
- 9,026,017 tokens of KV on one card, 275x concurrency at 32k context.
