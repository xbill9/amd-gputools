#!/bin/bash
# Serve a model with vLLM on the droplet's MI300X.
#
# Defaults to google/gemma-4-E2B-it on a vllm/vllm-openai-rocm nightly, and
# handles AMD's rocm/vllm images too — they differ in whether the image's
# ENTRYPOINT already runs `vllm serve`. See the SERVE_ARGV block below.
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

VLLM_MODEL="${VLLM_MODEL:-google/gemma-4-E2B-it}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_IMAGE="${VLLM_IMAGE:?VLLM_IMAGE is not set — see amd.env}"
HF_CACHE="${HF_CACHE:-/opt/hf-cache}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
LIMIT_MM_PER_PROMPT="${LIMIT_MM_PER_PROMPT:-}"
CHAT_TEMPLATE="${CHAT_TEMPLATE:-}"
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

# TWO ENTRYPOINT SHAPES, AND THE IMAGE DECIDES WHICH.
#
# vllm/vllm-openai-rocm sets ENTRYPOINT ["vllm","serve"], so the model id is the
# FIRST ARGUMENT and repeating the subcommand is an unrecognised-arguments
# error. AMD's rocm/vllm images have no entrypoint at all and need `vllm serve`
# spelled out, where passing the bare model id is "vllm: command not found".
SERVE_ARGV=()
case "$VLLM_IMAGE" in
  vllm/*) ;;                       # entrypoint already runs `vllm serve`
  *) SERVE_ARGV+=(vllm serve) ;;   # vendor image: spell the subcommand out
esac
SERVE_ARGV+=("$VLLM_MODEL" --host 0.0.0.0 --port 8000)
SERVE_ARGV+=(--max-model-len "$MAX_MODEL_LEN")
SERVE_ARGV+=(--gpu-memory-utilization "$GPU_MEMORY_UTILIZATION")

# Gemma 4 needs its own reasoning and tool-call parsers to expose thinking and
# function calling over the OpenAI API. Harmless on a model that has neither
# only because they are opt-in — drop them if you point this at something else.
case "$VLLM_MODEL" in
  *gemma-4*|*gemma4*)
    SERVE_ARGV+=(--enable-auto-tool-choice --reasoning-parser gemma4 --tool-call-parser gemma4)
    [ -n "$CHAT_TEMPLATE" ] && SERVE_ARGV+=(--chat-template "$CHAT_TEMPLATE")
    [ -n "$LIMIT_MM_PER_PROMPT" ] && SERVE_ARGV+=(--limit-mm-per-prompt "$LIMIT_MM_PER_PROMPT")
    ;;
esac
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
  "${SERVE_ARGV[@]}"

echo "📡 started. Weights download and load take minutes; poll with --status."
