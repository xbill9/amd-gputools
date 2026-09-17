"""Ask a vLLM image whether it can load Gemma 4, before trying to serve with it.

Run inside the candidate container with /dev/kfd and /dev/dri mapped in. Shared
by `server.py` (the MCP tool `vllm_image_status`) and `scaffold-droplet.sh`, so
there is one copy of the module path to get wrong rather than two.

Two things this has to get right, both learned the hard way:

  * `import vllm.config` comes FIRST. Importing the convertor module on its own
    raises a circular ImportError, which makes a working image look broken.
  * The registry is `vllm.transformers_utils.model_arch_config_convertor`.
    A first version guessed four other module paths, found nothing, and
    reported "not found" against an image that had ten gemma4 modules and a
    registered Gemma4ModelArchConfigConvertor. So the modules actually tried
    are reported alongside the answer: "not found" from a search that looked in
    the wrong place is not the same claim as "this build lacks it", and the
    difference decides whether you go hunting for another 35-62 GB image.

Why it matters: a build with no convertor does not fail at load time with a
clear message. Transformers >= 5.15 reports Gemma 4's head_dim as a per-layer
attribute (256 on sliding-attention layers, 512 on full-attention ones), vLLM
reads it globally, and config parsing raises AmbiguousGlobalPerLayerAttributeError
before the GPU is touched — naming neither Gemma nor the image.
"""

import json

MODULES = (
    "vllm.transformers_utils.model_arch_config_convertor",
    "vllm.config.model",
    "vllm.transformers_utils.config",
    "vllm.config",
)

out = {}

try:
    # This import, and only this one. `import vllm.config` binds the name `vllm`
    # as a side effect, so a second `import vllm` adds nothing — and an import
    # sorter would hoist it above this line, which is exactly the circular
    # ImportError this guard exists to avoid.
    import vllm.config

    out["vllm"] = vllm.__version__
except Exception as exc:  # noqa: BLE001
    out["vllm_error"] = f"{type(exc).__name__}: {exc}"

tried = []
convertor = None
for name in MODULES:
    tried.append(name)
    try:
        mod = __import__(name, fromlist=["MODEL_ARCH_CONFIG_CONVERTORS"])
    except Exception:  # noqa: BLE001
        continue
    registry = getattr(mod, "MODEL_ARCH_CONFIG_CONVERTORS", None)
    if registry is None:
        continue
    entry = registry.get("gemma4")
    out["convertor_registry"] = name
    out["convertor_count"] = len(registry)
    convertor = entry
    out["gemma4_convertor"] = None if entry is None else f"{entry.__module__}.{entry.__name__}"
    break

out["gemma4_supported"] = convertor is not None
if "convertor_registry" not in out:
    # No registry anywhere we looked. Say so as a search result, not a verdict.
    out["gemma4_convertor"] = "no MODEL_ARCH_CONFIG_CONVERTORS found"
    out["modules_tried"] = tried

for label in ("torch", "transformers"):
    try:
        out[label] = __import__(label).__version__
    except Exception as exc:  # noqa: BLE001
        out[f"{label}_error"] = f"{type(exc).__name__}: {exc}"

try:
    import torch

    # Empty means the devices were not mapped in, not a build without kernels.
    out["arch_list"] = torch.cuda.get_arch_list()
    out["gfx942_in_build"] = "gfx942" in out["arch_list"]
except Exception as exc:  # noqa: BLE001
    out["arch_list_error"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(out, indent=2))
