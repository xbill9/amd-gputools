#!/bin/bash
# Install PyTorch for ROCm into the droplet's SYSTEM python3 — no virtualenv.
#
#   vllm/baremetal-install.sh          # install torch, then verify the GPU
#   vllm/baremetal-install.sh --check  # verify only, install nothing
#
# Runs ON the droplet.
#
# WHY THIS DOES NOT INSTALL VLLM. There is no prebuilt vLLM wheel for ROCm:
# PyPI's vllm wheels are CUDA builds, AMD's manylinux index at repo.radeon.com
# carries torch and triton but no vllm, and the per-architecture nightly index
# is not published (checked 2026-09-16). Building vLLM from source needs a full
# ROCm dev toolchain — hipcc plus rocblas, hipblaslt, miopen and rccl — and
# Debian 13 ships hipcc 5.7.1 and none of those libraries, while AMD's own repo
# does not support trixie. So bare metal here means PyTorch on the host python,
# and vLLM itself comes from the container. See vllm/docker-serve.sh.
#
# The torch ROCm wheel bundles its own copies of the ROCm runtime libraries in
# torch/lib, which is why this works on a box with no /opt/rocm.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/../amd.env" ] && while IFS='=' read -r key value; do
  case "$key" in '' | '#'*) continue ;; esac
  [ -z "${!key:-}" ] && export "$key=$value"
done < "$HERE/../amd.env"

# rocm6.4 rather than the newest index: the host runs Debian's in-tree amdgpu
# with ROCm 6.1.2 userspace, and the wheel's runtime talks to that kernel's KFD.
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.4}"

verify() {
  python3 - <<'PY'
import sys
try:
    import torch
except ModuleNotFoundError:
    print("❌ torch is not installed")
    sys.exit(1)
print(f"torch   {torch.__version__}")
print(f"hip     {getattr(torch.version, 'hip', None)}")
ok = torch.cuda.is_available()
print(f"gpu     {'available' if ok else 'NOT available'}")
if not ok:
    print("❌ torch cannot see the GPU. Is /dev/kfd present? Is this user in the render group?")
    sys.exit(1)
n = torch.cuda.device_count()
print(f"devices {n}")
for i in range(n):
    p = torch.cuda.get_device_properties(i)
    print(f"  [{i}] {p.name}  {p.total_memory / 1024**3:.1f} GiB  {p.multi_processor_count} CUs")
# A real kernel launch, not just a capability query: HSA can enumerate an agent
# that then fails to execute anything.
a = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
b = torch.randn(4096, 4096, device="cuda", dtype=torch.float16)
c = (a @ b).float()
torch.cuda.synchronize()
print(f"matmul  ok, mean {c.mean().item():.4f}")
PY
}

if [ "${1:-}" = "--check" ]; then
  verify
  exit $?
fi

[ -e /dev/kfd ] || { echo "baremetal-install: /dev/kfd is missing. Reboot the droplet." >&2; exit 1; }

echo "→ installing torch from $TORCH_INDEX into the system python3"
# --break-system-packages because Debian 13 marks the system python
# externally-managed (PEP 668). That flag is the supported way to say "yes, the
# system interpreter is the target" — the alternative it is steering toward is a
# virtualenv, and this project does not use one.
python3 -m pip install --break-system-packages --index-url "$TORCH_INDEX" torch

echo
verify
