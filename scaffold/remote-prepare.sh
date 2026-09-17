#!/bin/bash
# Remote half of the droplet scaffold. Runs ON the droplet, as root, over ssh.
#
# This file is the single source of truth for what "a prepared droplet" means.
# It is used two ways and must keep working both:
#
#   - `scaffold-droplet.sh` pipes it to the droplet from the workstation.
#   - `server.py` reads it and sends it through the MCP tool `prepare_droplet`.
#
# So it takes its configuration from the environment and never from arguments,
# writes its report in <<<marker>>> sections that both callers parse, and never
# exits early on a failed step: the interesting droplet is the one where half of
# this does not apply, and a partial report beats none.
#
# Everything here is idempotent. Running it twice is a supported operation and
# the second run should report "nothing changed".

set -u

APT_COMPONENTS="${APT_COMPONENTS:-main contrib non-free non-free-firmware}"
SETUP_PACKAGES="${SETUP_PACKAGES:-ca-certificates curl gnupg git tmux jq pciutils python3-pip docker.io docker-compose rocm-smi rocminfo}"
# Installed with -t <codename>-backports. firmware-amd-graphics is here rather
# than in SETUP_PACKAGES because trixie carries 20250410 and backports carries a
# build months newer, and the MI300X needs blobs (gc_9_4_3, sdma_4_4_2) that are
# recent enough to be a real risk in the older one.
BACKPORTS_PACKAGES="${BACKPORTS_PACKAGES:-firmware-amd-graphics}"

say() { printf '<<<%s>>>\n' "$1"; }

SRC=/etc/apt/sources.list.d/debian.sources
# shellcheck disable=SC1091  # present on every Debian droplet, not in this repo
. /etc/os-release
CODE="${VERSION_CODENAME:-trixie}"

say before
if [ -f "$SRC" ]; then grep -E '^(Suites|Components):' "$SRC"; else echo "MISSING $SRC"; fi

# The sources file is deb822 and its URIs are a DigitalOcean mirror
# indirection, mirror+file:///etc/apt/mirrors/debian.list. So it is EDITED IN
# PLACE and never appended to: dropping a classic one-line
# `deb http://deb.debian.org/debian trixie-backports main` into sources.list.d
# would bypass the mirror and duplicate a suite that is usually already there.
#
# MEASURED 2026-09-17 on droplet 601418522: the stock Suites line already read
# `trixie trixie-updates trixie-backports`, so BACKPORTS WAS ALREADY ON and only
# `Components: main` was short. The backports branch below is a fallback for an
# image that differs, not the normal path.
say sources
if [ -f "$SRC" ]; then
  [ -f "$SRC.amd-gputools.bak" ] || cp -a "$SRC" "$SRC.amd-gputools.bak"
  sed -i "s/^Components:.*/Components: ${APT_COMPONENTS}/" "$SRC"
  grep -q -- "-backports" "$SRC" || sed -i "/^Suites: $CODE /s/\$/ $CODE-backports/" "$SRC"
  grep -E '^(Suites|Components):' "$SRC"
else
  echo "deb822 sources file absent; apt was left alone"
fi

say update
apt-get update -qq 2>&1 | tail -6
echo "exit:$?"

say install
# shellcheck disable=SC2086  # the package list is deliberately word-split
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  -o Dpkg::Options::=--force-confold $SETUP_PACKAGES 2>&1 | tail -10
echo "exit:$?"

say backports
if [ -n "$BACKPORTS_PACKAGES" ]; then
  # shellcheck disable=SC2086  # deliberately word-split
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    -t "${CODE}-backports" -o Dpkg::Options::=--force-confold $BACKPORTS_PACKAGES 2>&1 | tail -8
  echo "exit:$?"
else
  echo "(none requested)"
fi

say docker
systemctl enable --now docker >/dev/null 2>&1
printf 'service:%s\n' "$(systemctl is-active docker 2>/dev/null || echo inactive)"
docker --version 2>/dev/null || echo "docker: absent"

# Resolved on the host and reported, because `--group-add render` fails outright
# against the ROCm container images: they have no render group of their own, and
# `video` exists in both with no guarantee the numbers agree.
say gids
printf 'video=%s\nrender=%s\n' \
  "$(getent group video | cut -d: -f3)" \
  "$(getent group render | cut -d: -f3)"

# The bind check, and the reason this section is not `test -e /dev/kfd`:
# /dev/kfd and /dev/dri/renderD128 are created before amdgpu finishes probing,
# so both survive a bind that failed. A gfx agent in rocminfo does not.
say gpu
if command -v rocminfo >/dev/null 2>&1; then
  AGENTS="$(rocminfo 2>/dev/null | grep -cE '^[[:space:]]*Name:[[:space:]]*gfx[0-9a-f]+[[:space:]]*$')"
else
  AGENTS="unknown"
fi
printf 'gfx_agents=%s\n' "$AGENTS"
printf 'kfd=%s\n' "$([ -e /dev/kfd ] && echo present || echo absent)"
printf 'pci=%s\n' "$(lspci -nn 2>/dev/null | grep -ciE 'processing accelerator' || echo 0)"
# An empty /lib/firmware/amdgpu is the other way this card fails to bind, and it
# looks nothing like the first: dmesg shows `failed to load amdgpu/gc_9_4_3_rlc.bin
# (-2)` and `early_init of IP block <gfx_v9_4_3> failed -19` rather than a PF
# handshake timeout. -2 is ENOENT. The blobs are in firmware-amd-graphics, which
# lives in non-free-firmware — so on a stock image the component has to be
# enabled before the fix is even installable. MEASURED 2026-09-17 on 601418522.
BLOBS="$(find /lib/firmware/amdgpu -type f 2>/dev/null | wc -l)"
printf 'firmware_blobs=%s\n' "$BLOBS"
if [ "$AGENTS" = "0" ] && [ "$BLOBS" = "0" ]; then
  echo "verdict=no GPU firmware installed — install firmware-amd-graphics from non-free-firmware, then reboot"
elif [ "$AGENTS" = "0" ]; then
  echo "verdict=amdgpu did not bind — reboot once; firmware is present, so nothing needs installing"
elif [ "$AGENTS" = "unknown" ]; then
  echo "verdict=rocminfo is not installed, so the bind cannot be checked"
else
  echo "verdict=ok"
fi

say disk
df -h --output=avail /var/lib/docker 2>/dev/null | tail -1 ||
  df -h --output=avail / | tail -1
