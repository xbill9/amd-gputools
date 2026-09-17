Category: Artificial Intelligence (AI) → Cloud & Edge AI → AMD Developer Cloud
Tags: rocm, gpu, instinct, performance, developer-cloud

---

# Which numeric formats an MI300X actually executes — measured on a Developer Cloud droplet

I spent a few days driving a single MI300X droplet on AMD Developer Cloud entirely
through an MCP server, and the part worth sharing here isn't the tooling — it's what
the card said when I stopped reading spec tables and measured it.

8192³ matmul, 30 iterations after 5 warm-up, torch 2.12.0+rocm10.0.0, `gfx942`
(MI300X VF, 304 CUs, 191.69 GiB). The card was concurrently serving, so every
absolute rate sits near half of peak — **the ratios are the result, not the
TFLOP/s**:

| dtype | TFLOP/s | vs bf16 |
| --- | ---: | ---: |
| bf16 | 664.3 | 1.00x |
| fp16 | 662.5 | 1.00x |
| **fp8 `e4m3fnuz`** | **1172.5** | **1.77x** |
| int8 | 455.6 | **0.69x** |

Three of those four contradict something plausible.

**int8 measured slower than bf16**, and the spec says it shouldn't. AMD rates int8
at 2614.9 TOPS — identical to fp8, double bf16. On that basis int8 looks like the
better-supported of the two. Measured, it reached 17.4% of its own peak. I read this
as a kernel gap rather than a silicon one: `torch._int_mm` isn't finding a tuned path
on this stack. If someone has hit a library or hand-written MFMA route that closes
it, I'd like to hear about it — I didn't attempt one.

**fp8 here is `e4m3fnuz`, not `e4m3fn`, and that bites harder than a format note
suggests.** Same byte, different value:

```
bit pattern 0b01000000 as float8_e4m3fn   -> 2.0
bit pattern 0b01000000 as float8_e4m3fnuz -> 1.0

float8_e4m3fn   round-trip [1.0, 2.0, 240.0, 448.0] -> [1.0, 2.0, 240.0, 448.0]
float8_e4m3fnuz round-trip [1.0, 2.0, 240.0, 448.0] -> [1.0, 2.0, 240.0, nan]
```

448 is an ordinary weight on an H100 and NaN here. Asking for the NVIDIA flavour at
least fails loudly rather than silently — `RuntimeError: CUDA error: HIPBLAS_STATUS_NOT_SUPPORTED`
— and vLLM agrees from its own side (`is_fp8_fnuz()` keys on `"gfx94"`). So an fp8
checkpoint from the Hub is not drop-in; quantize online from the bf16 weights and the
scales get derived on the machine that will run them.

**fp4 isn't on gfx942 at all, and the capability list says otherwise.**
`supported_quantization` includes `mxfp4`, but that's a generic registry, not a
hardware claim — `supports_mx()` gates on `gfx95`/`gfx1250`. Rather than refusing,
the stack falls back to an emulation kernel that dequantizes the weights back to bf16
on *every forward pass* and runs an ordinary bf16 `F.linear`. Bf16 speed, no resident
saving during compute, dequant overhead on top, plus the full fp4 error. Useful for
answering "would this model survive fp4" before buying MI355X; not a serving path.

Net: on this card it's fp8 `e4m3fnuz`, quantized online, and nothing else. Three
separate sources pointed elsewhere to get there.

Two operational notes from the same box, in case they save anyone the hour they cost
me:

- **`rocm-smi` and `amd-smi` exit 0 when they fail.** With the driver uninitialised,
  `rocm-smi` printed "Driver not initialized" to stderr, nothing to stdout, and exited
  0. Parse the output; never branch on the exit status.
- **A freshly provisioned droplet needs one reboot.** `amdgpu` fails to bind the VF
  and unloads, so `lspci` sees the card and `rocminfo` reports no GPU agent. The
  reliable test is whether `rocminfo` reports a `gfx` target — `/dev/kfd` can be
  present on a card that never bound.

Full write-up with the inventory tables, the vLLM memory accounting and the scope
caveats: https://xbill999.medium.com/managing-amd-mi300x-deployments-with-mcp-318d0ab116c8

Tooling is Apache-2.0 here, if the MCP side is of interest:
https://github.com/xbill9/amd-gputools

Happy to run a follow-up measurement on this droplet if there's a shape or dtype
people want checked.
