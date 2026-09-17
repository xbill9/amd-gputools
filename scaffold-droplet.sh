#!/bin/bash
# Re-scaffold the GPU droplet from bare image to ready-to-serve, in one command.
#
#   ./scaffold-droplet.sh              # full scaffold, rebooting if the card needs it
#   ./scaffold-droplet.sh --no-pull    # apt and docker only, skip the 35-62 GB image
#   ./scaffold-droplet.sh --no-reboot  # report a card that did not bind, do not fix it
#   ./scaffold-droplet.sh --probe      # also run the Gemma 4 capability check
#
# Everything is idempotent. Running it against an already-scaffolded droplet
# should change nothing and still print the same report, which is what makes it
# usable as a health check as well as a build step.
#
# Config comes from amd.env, and a real environment variable always wins — the
# same precedence ssh-droplet.sh and server.py use. The address is resolved from
# the API on every call by ssh-droplet.sh; nothing here knows an IP.
#
# The remote half is scaffold/remote-prepare.sh, which `server.py` also sends
# through the MCP tool `prepare_droplet`. Edit that file, not a copy of it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH="$HERE/ssh-droplet.sh"
REMOTE="$HERE/scaffold/remote-prepare.sh"

PULL=1
REBOOT=1
PROBE=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --no-reboot) REBOOT=0 ;;
    --probe) PROBE=1 ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "scaffold: unknown option '$arg'" >&2
      exit 2
      ;;
  esac
done

if [ -f "$HERE/amd.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      '' | '#'*) continue ;;
    esac
    [ -z "${!key:-}" ] && export "$key=$value"
  done < "$HERE/amd.env"
fi

VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai-rocm:nightly-rocm100}"
PULL_LOG="${PULL_LOG:-/var/log/amd-gputools-pull.log}"
export APT_COMPONENTS="${APT_COMPONENTS:-main contrib non-free non-free-firmware}"
export SETUP_PACKAGES="${SETUP_PACKAGES:-}"
export BACKPORTS_PACKAGES="${BACKPORTS_PACKAGES:-firmware-amd-graphics}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() {
  printf '\nscaffold: %s\n' "$*" >&2
  exit 1
}

# Run the remote script with the config passed as leading assignments, and turn
# its <<<marker>>> sections into headings a human can read. Both callers of
# remote-prepare.sh parse those markers; only this one prettifies them.
run_remote_prepare() {
  {
    printf 'APT_COMPONENTS=%q\n' "$APT_COMPONENTS"
    [ -n "$SETUP_PACKAGES" ] && printf 'SETUP_PACKAGES=%q\n' "$SETUP_PACKAGES"
    [ -n "$BACKPORTS_PACKAGES" ] && printf 'BACKPORTS_PACKAGES=%q\n' "$BACKPORTS_PACKAGES"
    cat "$REMOTE"
  } | "$SSH" 'bash -s' | sed 's/^<<<\(.*\)>>>$/   -- \1 --/'
}

# The only trustworthy bind check. /dev/kfd is not it: the node is created
# before amdgpu finishes probing, so it survives a bind that failed — measured
# 2026-09-17, when a card with /dev/kfd present and amdgpu in lsmod was reporting
# no GPU at all and dmesg showed the probe dying in amdgpu_pci_probe.
gfx_agents() {
  "$SSH" "rocminfo 2>/dev/null | grep -cE '^[[:space:]]*Name:[[:space:]]*gfx[0-9a-f]+[[:space:]]*$' || true" 2>/dev/null |
    tr -dc '0-9' || echo 0
}

wait_for_ssh() {
  local deadline=$((SECONDS + ${1:-300}))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$SSH" true > /dev/null 2>&1; then return 0; fi
    sleep 10
  done
  return 1
}

[ -x "$SSH" ] || die "ssh-droplet.sh is missing or not executable."
[ -f "$REMOTE" ] || die "scaffold/remote-prepare.sh is missing."

step "Preparing the droplet (apt, packages, docker)"
run_remote_prepare

step "Checking whether amdgpu bound to the card"
AGENTS="$(gfx_agents)"
AGENTS="${AGENTS:-0}"
note "rocminfo reports ${AGENTS} gfx agent(s)"
if [ "$AGENTS" = "0" ]; then
  if [ "$REBOOT" = "0" ]; then
    note "amdgpu did not bind. --no-reboot given, so leaving it. The card is unusable."
  else
    note "amdgpu did not bind. This is normal on a freshly provisioned droplet."
    note "Rebooting once — nothing needs installing."
    "$SSH" 'systemctl reboot' > /dev/null 2>&1 || true
    sleep 15
    wait_for_ssh 300 || die "the droplet did not come back within 5 minutes."
    AGENTS="$(gfx_agents)"
    AGENTS="${AGENTS:-0}"
    note "after reboot: ${AGENTS} gfx agent(s)"
    [ "$AGENTS" = "0" ] &&
      die "still no gfx agent after a reboot. Check dmesg for 'Doesn't get msg:1 from pf'."
  fi
else
  note "already bound; no reboot needed"
fi

if [ "$PULL" = "1" ]; then
  step "Pulling ${VLLM_IMAGE}"
  if "$SSH" "docker image inspect ${VLLM_IMAGE} >/dev/null 2>&1"; then
    note "already present; nothing to pull"
  else
    note "35-62 GB, so this takes minutes. Log: ${PULL_LOG}"
    "$SSH" "docker pull ${VLLM_IMAGE} 2>&1 | tee ${PULL_LOG} | grep -E 'Pull complete|Status|Digest' || true"
  fi
  SIZE="$("$SSH" "docker image inspect --format '{{.Size}}' ${VLLM_IMAGE} 2>/dev/null || echo 0" 2>/dev/null | tr -dc '0-9')"
  # The arithmetic belongs here, not in whoever reads eleven digits of bytes.
  [ -n "${SIZE:-}" ] && [ "$SIZE" != "0" ] &&
    note "image size: $((SIZE / 1000000000)) GB"
fi

if [ "$PROBE" = "1" ]; then
  step "Checking the image for Gemma 4 support"
  "$SSH" "cat > /tmp/gemma4_probe.py" < "$HERE/scaffold/gemma4-probe.py"
  "$SSH" "VID=\$(getent group video | cut -d: -f3); REN=\$(getent group render | cut -d: -f3); \
    docker run --rm -v /tmp/gemma4_probe.py:/probe.py --device /dev/kfd --device /dev/dri \
    --group-add \$VID --group-add \$REN --entrypoint python3 ${VLLM_IMAGE} /probe.py 2>&1 | tail -40"
fi

step "Done"
note "Serve with: vllm/docker-serve.sh"
note "Re-run this script any time; it changes nothing on an already-scaffolded box."
