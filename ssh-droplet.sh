#!/bin/bash
# Open a shell on the project's GPU droplet, or run one command on it.
#
#   ./ssh-droplet.sh                 # interactive shell
#   ./ssh-droplet.sh rocm-smi        # run a command, then exit
#   ./ssh-droplet.sh -- ls -la /opt  # everything after -- is the remote command
#
# The address is looked up from the DigitalOcean API every time rather than
# stored. A droplet that is rebuilt or moved comes back on a different IP, and a
# stale hardcoded address fails in a way that looks exactly like a machine that
# is still booting. Set DROPLET_IP in the environment to skip the lookup.
#
# Config comes from amd.env; a real environment variable always wins over it,
# which is the same precedence python-dotenv gives server.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$HERE/amd.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      '' | '#'*) continue ;;
    esac
    [ -z "${!key:-}" ] && export "$key=$value"
  done < "$HERE/amd.env"
fi

DROPLET_TAG="${DROPLET_TAG:-gemma}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
SSH_KEY="${SSH_KEY:-}"
# ssh does not expand a tilde inside an argument, only the shell does.
SSH_KEY="${SSH_KEY/#\~/$HOME}"

die() {
  echo "ssh-droplet: $*" >&2
  exit 1
}

token() {
  if [ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]; then
    printf '%s' "$DIGITALOCEAN_ACCESS_TOKEN"
    return
  fi
  # .env first — it is this project's own, gitignored, mode 0600.
  if [ -f "$HERE/.env" ]; then
    local from_env
    from_env="$(sed -n 's/^DIGITALOCEAN_ACCESS_TOKEN=//p' "$HERE/.env" | tr -d '"'\''' | head -1)"
    if [ -n "$from_env" ]; then
      printf '%s' "$from_env"
      return
    fi
  fi
  if [ -f "$HOME/ocean.txt" ]; then
    head -1 "$HOME/ocean.txt" | tr -d '[:space:]'
    return
  fi
  die "no API token. Export DIGITALOCEAN_ACCESS_TOKEN, or put it in .env or ~/ocean.txt."
}

lookup_ip() {
  command -v jq >/dev/null || die "jq is required to read the API response."
  local body status ip
  body="$(curl -sS --max-time 30 \
    -H "Authorization: Bearer $(token)" \
    "https://api.digitalocean.com/v2/droplets?tag_name=${DROPLET_TAG}&per_page=200")" ||
    die "could not reach the DigitalOcean API."

  if [ "$(jq -r '.droplets | length' <<<"$body")" = "0" ]; then
    die "no droplet tagged '${DROPLET_TAG}'. Set DROPLET_TAG in amd.env to the tag it carries."
  fi

  status="$(jq -r '.droplets[0].status' <<<"$body")"
  [ "$status" = "active" ] ||
    die "droplet is '${status}', not active. Power it on first (mcp__amd-gputools__start_droplet)."

  ip="$(jq -r '.droplets[0].networks.v4[] | select(.type=="public") | .ip_address' <<<"$body" | head -1)"
  if [ -z "$ip" ] || [ "$ip" = "null" ]; then
    die "droplet is active but has no public IPv4 yet. Networking is still coming up."
  fi

  echo "→ $(jq -r '.droplets[0].name' <<<"$body") at ${ip}" >&2
  printf '%s' "$ip"
}

[ "${1:-}" = "--" ] && shift

IP="${DROPLET_IP:-$(lookup_ip)}"

ssh_args=(
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=10
  -p "$SSH_PORT"
)
# BatchMode only when running unattended: it would block an interactive
# passphrase prompt, which is exactly what a human at a terminal wants.
[ "$#" -gt 0 ] && ssh_args+=(-o BatchMode=yes)
[ -n "$SSH_KEY" ] && ssh_args+=(-i "$SSH_KEY")

exec ssh "${ssh_args[@]}" "${SSH_USER}@${IP}" "$@"
