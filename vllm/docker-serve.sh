#!/bin/bash
# Serve a model with vLLM on the droplet's MI300X, using AMD's rocm/vllm image.
#
#   vllm/docker-serve.sh              # start, using VLLM_MODEL from amd.env
#   vllm/docker-serve.sh --stop       # stop and remove the container
#   vllm/docker-serve.sh --logs       # follow the container log
#   vllm/docker-serve.sh --status     # is it up, and does it answer
#
# Runs ON the droplet. Use ../ssh-droplet.sh to get there, or let the MCP
# server's run_on_droplet call it.
#
# The container brings its own complete ROCm userspace. The only things it needs
# from the host are the kernel amdgpu driver and the two device nodes, which is
# why this works on a box with no /opt/rocm and ROCm 6.1.2 userspace while the
# image inside is ROCm 7.13.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for envfile in "$HERE/../amd.env" "$HERE/../.env"; do
  if [ -f "$envfile" ]; then
    while IFS='=' read -r key value; do
      case "$key" in '' | '#'*) continue ;; esac
      [ -z "${!key:-}" ] && export "$key=$value"
    done < "$envfile"
  fi
done

VLLM_MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_IMAGE="${VLLM_IMAGE:?VLLM_IMAGE is not set — see amd.env}"
HF_CACHE="${HF_CACHE:-/opt/hf-cache}"
NAME="vllm"

die() { echo "docker-serve: $*" >&2; exit 1; }

case "${1:-}" in
  --stop)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "🛑 stopped $NAME" || echo "not running"
    exit 0
    ;;
  --logs)
    exec docker logs -f "$NAME"
    ;;
  --status)
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "❌ container '$NAME' is not running"
      exit 1
    fi
    # /health decides, the container state only annotates: vLLM takes minutes to
    # load weights and compile, and is up but not serving for all of it.
    if curl -sf --max-time 5 "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null; then
      echo "✅ serving on :${VLLM_PORT}"
      curl -sS --max-time 10 "http://127.0.0.1:${VLLM_PORT}/v1/models"
    else
      echo "📡 container is up but /health is not answering yet — still loading. docker logs -f $NAME"
      exit 1
    fi
    exit 0
    ;;
  "") ;;
  *) die "unknown option: $1" ;;
esac

[ -e /dev/kfd ] || die "/dev/kfd is missing. Reboot the droplet — see CLAUDE.md."
command -v docker >/dev/null || die "docker is not installed."

docker rm -f "$NAME" >/dev/null 2>&1 || true
mkdir -p "$HF_CACHE"

echo "→ $VLLM_IMAGE"
echo "→ model $VLLM_MODEL on :$VLLM_PORT"

# --device for the two GPU nodes, and the groups that own them, because without
# --group-add the container opens /dev/kfd without the right group and HSA
# reports no agents. The GIDs are resolved on the HOST and passed numerically:
# `--group-add render` fails outright with "Unable to find group render" since
# the Ubuntu 24.04 image has no render group of its own, and video exists in
# both with no guarantee the numbers agree. --ipc=host because vLLM's workers
# need shared memory larger than docker's default 64 MB.
VIDEO_GID="$(getent group video | cut -d: -f3)"
RENDER_GID="$(getent group render | cut -d: -f3)"
docker run -d --name "$NAME" \
  --device=/dev/kfd --device=/dev/dri \
  ${VIDEO_GID:+--group-add "$VIDEO_GID"} ${RENDER_GID:+--group-add "$RENDER_GID"} \
  --ipc=host --shm-size=16g \
  --security-opt seccomp=unconfined \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  ${HF_TOKEN:+-e HF_TOKEN="$HF_TOKEN"} \
  -p "${VLLM_PORT}:8000" \
  --restart unless-stopped \
  "$VLLM_IMAGE" \
  vllm serve "$VLLM_MODEL" --host 0.0.0.0 --port 8000

echo "📡 started. Weights download and load take minutes; poll with --status."
