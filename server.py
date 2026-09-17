"""MCP server for the DigitalOcean droplets that carry this project's AMD GPUs.

amd-gputools is a Python + shell toolkit for AMD Instinct MI300-class
accelerators. There is no AMD GPU on the workstation this server runs on: the
hardware lives on a DigitalOcean GPU droplet, so every ROCm command in here is
executed over SSH and every lifecycle operation goes through the DigitalOcean
v2 API. Nothing in this file touches a local GPU, and nothing should be added
that assumes one.

STATUS 2026-09-16: exercised against the live droplet
debian-gpu-mi300x1-192gb-devcloud-atl1 (id 601142018, atl1, $1.99/hr). Every
tool works and the GPU reports: gfx942, MI300X VF, 304 CUs, 191.7 GiB VRAM.

A freshly provisioned droplet has no /dev/kfd until it is rebooted once —
amdgpu fails to bind during provisioning and unloads, leaving a card that lspci
can see and nothing can use. Nothing needs installing. gpu_status names /dev/kfd
when it is missing precisely so the answer is "reboot it", not "debug ROCm".

The droplet is reached through AMD Developer Cloud (devcloud.amd.com), which is
DigitalOcean underneath — same v2 API, same droplet ids, token issued from the
"My AMD Team" account. Its size slug, gpu-mi300x1-192gb-devcloud, does not
appear in GET /v2/sizes: that endpoint lists the public gpu-mi300x1-192gb at
$2.59/hr instead, so list_gpu_sizes shows the public catalogue, not what
devcloud actually sells.

Deliberately absent: destroy. This server can create, find, start, stop, reach
and interrogate droplets, and every lookup is scoped by tag so it cannot act on
a droplet it was not pointed at. create_droplet is two-step on purpose — the
first call orders nothing and reports what it would order, what the catalogue
says that costs and whether the API even offers that size in that region — and
it applies the tag unconditionally, because an untagged droplet would be a
machine this server is paying for and cannot reach. Destroying one is the
operation whose cost of being wrong is measured in lost local state rather than
dollars, and it stays a deliberate human step in the DigitalOcean console.

BILLING DOES NOT STOP WHEN A DROPLET IS POWERED OFF. DigitalOcean bills a
powered-off droplet at the full hourly rate, because the resources stay
reserved for it. `stop_droplet` saves nothing. Only destroying the droplet
stops the meter, and this server cannot destroy one. Treat the hourly rate
from `list_gpu_sizes` as what an idle droplet costs.
"""

import asyncio
import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

PROJECT_DIR = Path(__file__).resolve().parent
# .env before amd.env, and both after the real environment: load_dotenv never
# overwrites a name that is already set, so an exported variable still wins.
# amd.env is committed and holds no secrets; .env is gitignored and mode 0600.
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(PROJECT_DIR / "amd.env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# The registered key prefixes every tool name (mcp__amd-gputools__list_droplets),
# so it must match the directory or two loaded servers are indistinguishable at
# the call site.
PROJECT_NAME = PROJECT_DIR.name
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", PROJECT_NAME)

DO_API_BASE = os.environ.get("DO_API_BASE", "https://api.digitalocean.com/v2")

# Every lookup is filtered by this tag, so the server can only ever see the
# droplets somebody deliberately tagged for it. An untagged droplet in the same
# account is invisible here, which is the point: a typo in a droplet id cannot
# power-cycle an unrelated machine.
DROPLET_TAG = os.environ.get("DROPLET_TAG", "amd-gputools")

SSH_USER = os.environ.get("SSH_USER", "root")
SSH_KEY = os.environ.get("SSH_KEY", "")
SSH_PORT = os.environ.get("SSH_PORT", "22")
REMOTE_WORKDIR = os.environ.get("REMOTE_WORKDIR", "/opt/amd-gputools")

# Defaults for create_droplet: identifiers, not secrets, so they belong in
# amd.env. They describe the droplet this project actually ran, so creating one
# with no arguments rebuilds that box rather than inventing a new shape.
#
# The size slug is an AMD Developer Cloud one and is NOT in GET /v2/sizes — the
# public catalogue lists gpu-mi300x1-192gb at $2.59/hr where devcloud sells this
# at $1.99/hr — so "unknown slug" is the normal case here and create_droplet
# reports it rather than refusing on it.
DROPLET_SIZE = os.environ.get("DROPLET_SIZE", "gpu-mi300x1-192gb-devcloud")
DROPLET_REGION = os.environ.get("DROPLET_REGION", "atl1")
DROPLET_IMAGE = os.environ.get("DROPLET_IMAGE", "debian-13-x64")
# Comma-separated key names, ids or fingerprints. Empty means every key on the
# account, never none: see _resolve_ssh_keys.
DROPLET_SSH_KEYS = os.environ.get("DROPLET_SSH_KEYS", "")

# prepare_droplet. The stock devcloud image is bare — of eleven tools probed on
# a fresh droplet only python3 existed — so serving anything means installing
# the userspace first. See amd.env for what each of these is and why.
APT_COMPONENTS = os.environ.get("APT_COMPONENTS", "main contrib non-free non-free-firmware")
SETUP_PACKAGES = os.environ.get(
    "SETUP_PACKAGES",
    "ca-certificates curl gnupg git tmux jq pciutils python3-pip docker.io docker-compose rocm-smi rocminfo",
)
# Installed from backports. The card does not bind without it: a stock droplet
# has an empty /lib/firmware/amdgpu. See amd.env.
BACKPORTS_PACKAGES = os.environ.get("BACKPORTS_PACKAGES", "firmware-amd-graphics")
VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "vllm/vllm-openai-rocm:nightly-rocm100")
PULL_LOG = os.environ.get("PULL_LOG", "/var/log/amd-gputools-pull.log")

# Last-resort token file, matching ssh-droplet.sh so the shell script and the
# server never disagree about where the token lives. A module constant rather
# than an inline path so tests can point it somewhere that does not exist.
OCEAN_TXT = Path.home() / "ocean.txt"

mcp = MCPServer(MCP_SERVER_NAME)
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
WRITE = ToolAnnotations(destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)
# Creating a droplet destroys nothing, but destructiveHint is what makes a client
# stop and ask, and starting a $2/hour meter that only a console visit can stop
# is exactly the class of call that should be confirmed rather than inferred.
COSTLY = ToolAnnotations(destructiveHint=True, idempotentHint=False)


def _error(exc: Exception) -> str:
    """Render an exception as the markdown a tool returns instead of raising."""
    if isinstance(exc, httpx.HTTPError):
        return f"❌ DigitalOcean API unreachable: {exc}"
    return f"❌ {exc}"


def _token() -> str:
    """Read the API token, preferring the name doctl and Terraform use.

    The token is never read from amd.env — that file is committed. It comes
    from the environment, from .env (gitignored, mode 0600, loaded at import),
    or from ~/ocean.txt, which is the same order ssh-droplet.sh uses.
    """
    token = os.environ.get("DIGITALOCEAN_ACCESS_TOKEN") or os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        token = _token_from_ocean_txt()
    if not token:
        raise RuntimeError(
            "DIGITALOCEAN_ACCESS_TOKEN is unset. Put it in `.env` (gitignored, mode 0600) "
            f"or {OCEAN_TXT}, or export it — never in `amd.env`, which is committed."
        )
    return token


def _token_from_ocean_txt() -> str:
    """Return the first line of ~/ocean.txt, or "" if it is unreadable.

    Unreadable is not an error: the file is a fallback, and the caller already
    raises a message naming every place the token is allowed to live.
    """
    try:
        first = OCEAN_TXT.read_text().splitlines()[0]
    except (OSError, IndexError):
        return ""
    return first.strip()


async def _api(method: str, path: str, payload: Optional[dict] = None, timeout: int = 30) -> dict:
    """Call the DigitalOcean v2 API and return the decoded body.

    Raises RuntimeError carrying the API's own error message, which is far more
    useful than a bare status code: DigitalOcean puts the reason in `message`.
    """
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    url = path if path.startswith("http") else f"{DO_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, json=payload)

    if resp.status_code == 401:
        raise RuntimeError("DigitalOcean rejected the token (401). Is DIGITALOCEAN_ACCESS_TOKEN current?")
    if resp.status_code == 429:
        remaining = resp.headers.get("ratelimit-remaining", "?")
        reset = resp.headers.get("ratelimit-reset", "?")
        raise RuntimeError(f"DigitalOcean rate limit hit (429). remaining={remaining} reset={reset}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text[:300])
        except ValueError:
            detail = resp.text[:300]
        raise RuntimeError(f"DigitalOcean {resp.status_code}: {detail}")
    if not resp.content:
        return {}
    return resp.json()


async def _paged(path: str, key: str) -> list[dict]:
    """Collect every page of a list endpoint.

    per_page=200 is the API maximum. The loop is not decoration: a GPU account
    with more than 200 tagged droplets would otherwise be silently truncated,
    and a truncated list is a wrong answer that looks like a right one.
    """
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}per_page=200"
    items: list[dict] = []
    while url:
        body = await _api("GET", url)
        items.extend(body.get(key, []))
        url = body.get("links", {}).get("pages", {}).get("next", "")
    return items


async def _droplets() -> list[dict]:
    """Every droplet carrying DROPLET_TAG."""
    return await _paged(f"/droplets?tag_name={DROPLET_TAG}", "droplets")


async def _resolve(droplet: str) -> dict:
    """Find one tagged droplet by numeric id or by name.

    Accepting the name matters because the id is a nine-digit number nobody
    remembers, and mistyping one is exactly the class of error the tag scope
    exists to contain.
    """
    found = await _droplets()
    if not found:
        raise RuntimeError(
            f"No droplets tagged `{DROPLET_TAG}`. Tag the droplet in the DigitalOcean "
            f"console, or set DROPLET_TAG in `amd.env` to the tag it already has."
        )
    wanted = droplet.strip()
    for item in found:
        if str(item.get("id")) == wanted or item.get("name") == wanted:
            return item
    names = ", ".join(f"`{d.get('name')}` ({d.get('id')})" for d in found)
    raise RuntimeError(f"No droplet `{wanted}` tagged `{DROPLET_TAG}`. Tagged droplets: {names}")


def _public_ip(droplet: dict) -> Optional[str]:
    """Pull the public IPv4 out of a droplet record.

    Resolved per call rather than pinned in amd.env: a droplet that is rebuilt
    or moved comes back with a different address, and a stale hardcoded IP is
    indistinguishable from a machine that is merely still booting.
    """
    for iface in droplet.get("networks", {}).get("v4", []):
        if iface.get("type") == "public":
            return iface.get("ip_address")
    return None


def _ssh_argv(ip: str, command: Optional[str] = None) -> list[str]:
    """Build an ssh argv that fails fast instead of asking a question.

    BatchMode=yes turns a missing key into an error rather than a password
    prompt, and accept-new adopts an unknown host key rather than an
    interactive yes/no. Either prompt would hang this tool until it timed out,
    with no output to explain why.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-p",
        SSH_PORT,
    ]
    if SSH_KEY:
        argv += ["-i", os.path.expanduser(SSH_KEY)]
    argv.append(f"{SSH_USER}@{ip}")
    if command is not None:
        argv.append(command)
    return argv


async def run_command(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a command with no shell. Never shell=True — see CLAUDE.md."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    except asyncio.TimeoutError:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"


def _ssh_transport_error(code: int, err: str) -> Optional[str]:
    """Return ssh's own complaint, or None if ssh actually ran the command.

    ssh exits 255 when it could not run anything at all — connection refused,
    timed out, host key rejected, no usable key — and writes a line beginning
    "ssh:" to stderr. Telling that apart from the remote command's own exit
    status matters because `_reachable` only proves the API calls the droplet
    `active`, and a freshly created GPU droplet refuses connections for a
    minute or more after that while sshd comes up. MEASURED 2026-09-17 on
    601418522, seconds after creation: "ssh: connect to host 129.212.178.87
    port 22: Connection refused". Without this, gpu_status read an unreachable
    droplet as a reachable one whose probes happened to say nothing.
    """
    text = (err or "").strip()
    if code == 124:
        return text or "ssh timed out"
    if code == 255 and text:
        return text.splitlines()[0]
    return None


async def _reachable(droplet: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (ip, reason_it_is_not_usable). Exactly one is None."""
    status = droplet.get("status")
    if status != "active":
        return None, f"droplet is `{status}`, not `active`. Run `start_droplet` first."
    ip = _public_ip(droplet)
    if not ip:
        return (
            None,
            "droplet is active but has no public IPv4 yet. Networking is still coming up.",
        )
    return ip, None


def _fmt_price(size: dict) -> str:
    hourly = size.get("price_hourly")
    monthly = size.get("price_monthly")
    if hourly is None:
        return "-"
    return f"${hourly:.3f}/hr (${monthly:,.0f}/mo)" if monthly else f"${hourly:.3f}/hr"


# DigitalOcean accepts letters, digits, dots and dashes in a droplet name and
# rejects anything else with a 422 that does not say which character offended.
_NAME_OK = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?")


def _cost_line(hourly: float) -> str:
    """Spell an hourly rate out in days and months, computed here rather than quoted.

    An hourly figure is the number nobody converts: $1.99/hr reads as small and
    is $1,433 a month. The multiplication belongs in code — see CLAUDE.md.
    """
    return f"${hourly:.3f}/hour — ${hourly * 24:,.0f}/day, ${hourly * 24 * 30:,.0f} for 30 days if it is left up."


async def _resolve_ssh_keys(wanted: str) -> tuple[list[int], list[str]]:
    """Turn a comma-separated list of key names, ids or fingerprints into key ids.

    Naming nothing means every key on the account, never none. A GPU droplet
    created with no key is not a cheap mistake: DigitalOcean falls back to
    emailing a root password, `ssh_command` and `run_on_droplet` cannot use one,
    and the meter runs at full rate while somebody goes looking for the mail.
    """
    keys = await _paged("/account/keys", "ssh_keys")
    if not keys:
        raise RuntimeError(
            "No SSH keys on this account. Add one in the DigitalOcean console before creating a "
            "droplet — one created without a key gets a root password by email, which none of the "
            "SSH tools here can use, while billing at the full hourly rate."
        )
    names = [part.strip() for part in wanted.split(",") if part.strip()]
    if not names:
        return [int(k["id"]) for k in keys], [str(k.get("name")) for k in keys]
    chosen: list[int] = []
    labels: list[str] = []
    for want in names:
        for key in keys:
            if want in (str(key.get("id")), key.get("name"), key.get("fingerprint")):
                chosen.append(int(key["id"]))
                labels.append(f"`{key.get('name')}` ({key.get('fingerprint')})")
                break
        else:
            have = ", ".join(f"`{k.get('name')}`" for k in keys) or "(none)"
            raise RuntimeError(f"No SSH key `{want}` on this account. Keys on the account: {have}.")
    return chosen, labels


async def _create_preflight(size: str, region: str, image: str) -> tuple[list[str], Optional[float]]:
    """Describe a size/region/image order before anything is placed, with its price.

    Every finding here is one the API would otherwise deliver as a 422 after the
    droplet request, and the size check cannot be fatal: the devcloud slug this
    project uses is absent from the public catalogue by design, so "not listed"
    is the normal case and is reported rather than refused.
    """
    notes: list[str] = []
    hourly: Optional[float] = None

    sizes = {s.get("slug"): s for s in await _paged("/sizes", "sizes")}
    listed = sizes.get(size)
    if listed:
        hourly = listed.get("price_hourly")
        notes.append(f"- size `{size}`: in the public catalogue at {_fmt_price(listed)}.")
        offered = listed.get("regions") or []
        if not offered:
            notes.append("  ⚠️ it lists no regions at all, so ordering it through the API may be refused.")
        elif region not in offered:
            notes.append(
                f"  ⚠️ it lists {', '.join(f'`{r}`' for r in offered)} — not `{region}`. "
                f"Expect a 422 unless this is a devcloud allocation."
            )
    else:
        notes.append(
            f"- ⚠️ size `{size}` is not in `GET /v2/sizes`. Expected for an AMD Developer Cloud slug — "
            f"the public catalogue carries `gpu-mi300x1-192gb` at $2.59/hr instead of the $1.99/hr "
            f"devcloud card — but it does mean the price cannot be quoted from the API here, and the "
            f"order may still be refused. The devcloud console is the fallback."
        )

    regions = {r.get("slug"): r for r in await _paged("/regions", "regions")}
    here = regions.get(region)
    if not here:
        notes.append(f"- ⚠️ region `{region}` is not in `GET /v2/regions`.")
    elif not here.get("available"):
        notes.append(f"- ⚠️ region `{region}` reports `available: false`.")
    else:
        gpu_here = [s for s in here.get("sizes", []) if str(s).startswith("gpu-")]
        notes.append(f"- region `{region}`: available, {len(gpu_here)} GPU size(s) offered through the public API.")
        if gpu_here and size not in gpu_here:
            notes.append(f"  GPU sizes it does offer: {', '.join(f'`{s}`' for s in gpu_here)}.")

    notes.append(f"- image `{image}`.")
    return notes, hourly


def _read_script(name: str) -> str:
    """Read a remote script from scaffold/, the single source of truth for it.

    These scripts are sent to the droplet by this server AND piped there by
    `scaffold-droplet.sh`. One copy on disk rather than a constant here and a
    duplicate in the shell script is the point: the two callers cannot drift,
    and shellcheck lints the exact text this server sends.
    """
    return (PROJECT_DIR / "scaffold" / name).read_text()


def _with_env(script: str, **values: str) -> str:
    """Prepend shell assignments to a script, quoted so a value cannot inject.

    The scripts read configuration from the environment so that the shell
    driver and this server can hand them the same settings under the same
    names, rather than one of them templating placeholders the other does not
    know about.
    """
    prefix = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items() if value)
    return prefix + script


# The pull is detached on purpose. The ROCm vLLM images are 35-62 GB and a
# foreground `docker pull` outlives any sane MCP call timeout, which would leave
# the client believing a pull that is running fine has failed. setsid + nohup
# with stdin closed also stops ssh waiting on the child's stdout, which is what
# makes a backgrounded remote command hang until the timeout anyway.
_PULL_START_SCRIPT = r"""
IMAGE='__IMAGE__'
LOG='__LOG__'
command -v docker >/dev/null 2>&1 || { echo "NO_DOCKER"; exit 0; }
if docker image inspect "$IMAGE" >/dev/null 2>&1; then echo "ALREADY_PRESENT"; exit 0; fi
pgrep -f "docker pull $IMAGE" >/dev/null 2>&1 && { echo "ALREADY_RUNNING"; exit 0; }
: > "$LOG"
setsid nohup docker pull "$IMAGE" </dev/null >>"$LOG" 2>&1 &
echo "STARTED pid=$!"
"""

_PULL_STATUS_SCRIPT = r"""
say() { printf '<<<%s>>>\n' "$1"; }
IMAGE='__IMAGE__'
LOG='__LOG__'

say image
docker image inspect --format '{{.Id}} {{.Size}}' "$IMAGE" 2>/dev/null || echo absent

say running
pgrep -f "docker pull $IMAGE" >/dev/null 2>&1 && echo yes || echo no

say log
tail -4 "$LOG" 2>/dev/null | tr '\r' '\n' | tail -4 || echo "(no log)"
"""


@mcp.tool(title="List managed droplets", annotations=READ_ONLY)
async def list_droplets() -> str:
    """List every droplet tagged for this project, with state and address."""
    try:
        found = await _droplets()
        if not found:
            return (
                f"No droplets tagged `{DROPLET_TAG}`.\n\n"
                f"Create the MI300 droplet in the DigitalOcean console and give it that tag, "
                f"or point `DROPLET_TAG` in `amd.env` at the tag it already carries."
            )
        lines = [
            "| Droplet | ID | Size | Status | Public IP | Region |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        by_status: dict[str, int] = {}
        for item in found:
            status = str(item.get("status", "?"))
            by_status[status] = by_status.get(status, 0) + 1
            lines.append(
                f"| `{item.get('name')}` | `{item.get('id')}` | {item.get('size_slug', '-')} "
                f"| {status} | {_public_ip(item) or '-'} | {item.get('region', {}).get('slug', '-')} |"
            )
        tally = ", ".join(f"{count} {status}" for status, count in sorted(by_status.items()))
        lines.append("")
        lines.append(f"📡 {len(found)} droplet(s) tagged `{DROPLET_TAG}`: {tally}.")
        return "\n".join(lines)
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Show droplet status", annotations=READ_ONLY)
async def droplet_status(droplet: str) -> str:
    """Show one tagged droplet in detail, by numeric id or by name."""
    try:
        item = await _resolve(droplet)
        ip = _public_ip(item)
        lines = [
            f"**{item.get('name')}** (`{item.get('id')}`)",
            "",
            f"- status: `{item.get('status')}`",
            (
                f"- size: `{item.get('size_slug')}` — {item.get('vcpus', '?')} vCPU, "
                f"{item.get('memory', 0) // 1024 if item.get('memory') else '?'} GB RAM, "
                f"{item.get('disk', '?')} GB disk"
            ),
            f"- region: {item.get('region', {}).get('slug', '-')}",
            f"- image: {item.get('image', {}).get('slug') or item.get('image', {}).get('name', '-')}",
            f"- public IPv4: {ip or '- (none yet)'}",
            f"- tags: {', '.join(item.get('tags', [])) or '-'}",
            f"- created: {item.get('created_at', '-')}",
        ]
        if item.get("status") == "active" and ip:
            lines += ["", f"✅ Reachable as `{SSH_USER}@{ip}`. Billing is running."]
        elif item.get("status") == "off":
            lines += [
                "",
                "🛑 Powered off — and still billed at the full hourly rate. Only destroying it stops that.",
            ]
        else:
            lines += ["", f"📡 State is `{item.get('status')}`; not usable yet."]
        return "\n".join(lines)
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Create a GPU droplet", annotations=COSTLY)
async def create_droplet(
    name: str,
    confirm: bool = False,
    size: Optional[str] = None,
    region: Optional[str] = None,
    image: Optional[str] = None,
    ssh_keys: Optional[str] = None,
    allow_duplicate: bool = False,
) -> str:
    """Create a GPU droplet, tagged so this server can see it. Two steps on purpose.

    THE FIRST CALL ORDERS NOTHING. With confirm=False, which is the default,
    this reports the size, region, image and keys it would use, what the public
    catalogue says they cost per hour and per month, and whether the API even
    offers that size in that region — and then stops. Call it again with
    confirm=True to place the order. Provisioning an MI300 starts a meter that
    powering the droplet off does not stop, so the decision gets its own round
    trip rather than riding along with a typo.

    The tag from DROPLET_TAG is applied unconditionally and is not a parameter.
    Every other tool here is scoped to that tag, so an untagged droplet would be
    a machine this server is paying for and cannot reach, start or stop.

    Refuses by default if anything is already tagged for this project: a second
    GPU droplet doubles the hourly bill and the first one is billed in full even
    while powered off. Pass allow_duplicate=true when that really is the intent.

    Defaults come from amd.env. A droplet created here is NOT ready for ROCm
    until it has been rebooted once — see reboot_droplet.
    """
    try:
        wanted = name.strip()
        if not _NAME_OK.fullmatch(wanted):
            return (
                f"❌ `{wanted}` is not a usable droplet name. DigitalOcean allows letters, digits, dots "
                f"and dashes, and the name can neither start nor end with a dash."
            )
        size = (size or DROPLET_SIZE).strip()
        region = (region or DROPLET_REGION).strip()
        image = (image or DROPLET_IMAGE).strip()
        key_ids, key_labels = await _resolve_ssh_keys(DROPLET_SSH_KEYS if ssh_keys is None else ssh_keys)

        existing = await _droplets()
        clash = [d for d in existing if d.get("name") == wanted]
        if clash:
            return (
                f"❌ `{wanted}` already exists (`{clash[0].get('id')}`, status `{clash[0].get('status')}`) "
                f"and is already tagged `{DROPLET_TAG}`. Use `start_droplet` on it, or pick another name."
            )
        if existing and not allow_duplicate:
            listed = ", ".join(f"`{d.get('name')}` ({d.get('status')})" for d in existing)
            return (
                f"❌ {len(existing)} droplet(s) already tagged `{DROPLET_TAG}`: {listed}.\n\n"
                f"A second GPU droplet doubles the hourly bill, and the existing one is billed at the "
                f"full rate even while powered off. Refused by default; pass allow_duplicate=true if a "
                f"second machine is genuinely wanted."
            )

        notes, hourly = await _create_preflight(size, region, image)
        plan = "\n".join(
            [
                f"**{wanted}**",
                "",
                *notes,
                f"- ssh keys ({len(key_ids)}): {', '.join(key_labels)}",
                f"- tag: `{DROPLET_TAG}` — forced, because an untagged droplet is invisible to every tool here",
            ]
        )

        if not confirm:
            cost = (
                _cost_line(hourly)
                if hourly
                else (
                    "The API cannot price this slug, so nothing here is a quote. The devcloud MI300X "
                    "billed at $1.99/hour, which is $48/day and $1,433 for 30 days."
                )
            )
            return (
                f"📡 **Preflight only — nothing has been ordered.**\n\n{plan}\n\n"
                f"Cost if it is created: {cost}\n\n"
                f"Billing starts the moment the droplet exists and only destroying it stops the meter; "
                f"powering it off does not. Call `create_droplet` again with `confirm=true` to order it."
            )

        body = await _api(
            "POST",
            "/droplets",
            {
                "name": wanted,
                "region": region,
                "size": size,
                "image": image,
                "ssh_keys": key_ids,
                "tags": [DROPLET_TAG],
                "monitoring": True,
                "backups": False,
            },
            timeout=60,
        )
        created = body.get("droplet", {})
        return (
            f"✅ Created `{created.get('name')}` (`{created.get('id')}`), status `{created.get('status')}`.\n\n"
            f"{plan}\n\n"
            f"📡 There is no public IP until provisioning finishes — poll `droplet_status` and read the "
            f"address from there rather than reusing an old one.\n\n"
            f"**Then reboot it once before touching ROCm.** A freshly provisioned MI300 droplet has no "
            f"`/dev/kfd`: amdgpu fails to bind during provisioning and unloads itself, leaving a card "
            f"`lspci` can see and nothing can use. `reboot_droplet` is the fix and nothing needs "
            f"installing.\n\n"
            f"The meter is running now, and powering the droplet off will not stop it."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Start droplet", annotations=WRITE)
async def start_droplet(droplet: str) -> str:
    """Power on a tagged droplet. No-op if it is already active."""
    try:
        item = await _resolve(droplet)
        if item.get("status") == "active":
            ip = _public_ip(item)
            return f"✅ `{item.get('name')}` is already active at {ip or 'no public IP yet'}."
        body = await _api("POST", f"/droplets/{item['id']}/actions", {"type": "power_on"})
        action = body.get("action", {})
        return (
            f"📡 Powering on `{item.get('name')}` (`{item['id']}`). Action `{action.get('id')}` is "
            f"`{action.get('status')}`.\n\nBoot is not instant — poll `action_status` or `droplet_status`. "
            f"The public IP is assigned as it comes up, so re-read it rather than reusing the last one."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Stop droplet", annotations=DESTRUCTIVE)
async def stop_droplet(droplet: str, graceful: bool = True) -> str:
    """Power off a tagged droplet.

    graceful=True sends `shutdown`, which asks the OS to halt and can fail
    silently if the guest ignores it — always confirm with `droplet_status`.
    graceful=False sends `power_off`, the equivalent of pulling the cord; it is
    reliable and can corrupt an in-flight write.

    THIS DOES NOT STOP BILLING. DigitalOcean charges the full hourly rate for a
    powered-off droplet because the resources stay reserved. Powering an MI300
    droplet off overnight to save money does not save money.
    """
    try:
        item = await _resolve(droplet)
        if item.get("status") == "off":
            return f"🛑 `{item.get('name')}` is already off — and still being billed."
        action_type = "shutdown" if graceful else "power_off"
        body = await _api("POST", f"/droplets/{item['id']}/actions", {"type": action_type})
        action = body.get("action", {})
        note = (
            "A guest that ignores ACPI stays up; check `droplet_status` and retry with graceful=False."
            if graceful
            else "Hard power cut — any in-flight write is lost."
        )
        return (
            f"🛑 Sent `{action_type}` to `{item.get('name')}` (`{item['id']}`). Action `{action.get('id')}` is "
            f"`{action.get('status')}`.\n\n{note}\n\nBilling continues while it is off."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Reboot droplet", annotations=DESTRUCTIVE)
async def reboot_droplet(droplet: str) -> str:
    """Reboot a tagged droplet.

    THIS IS THE FIX FOR A FRESHLY PROVISIONED GPU DROPLET. MEASURED 2026-09-16
    on debian-gpu-mi300x1-192gb-devcloud-atl1: straight after provisioning
    /dev/kfd did not exist, amdgpu had failed to bind the MI300X VF and
    unloaded itself, and the card was visible to lspci while being unusable by
    anything. One reboot brought up /dev/kfd and the GPU reported normally. No
    driver installation was involved. Reach for this before debugging ROCm.

    Sends the API `reboot` action, which is a graceful restart. Anything
    running on the droplet dies with it, and the public IP is unchanged.
    """
    try:
        item = await _resolve(droplet)
        if item.get("status") != "active":
            return f"❌ `{item.get('name')}` is `{item.get('status')}`, not active. Use `start_droplet` instead."
        body = await _api("POST", f"/droplets/{item['id']}/actions", {"type": "reboot"})
        action = body.get("action", {})
        return (
            f"📡 Rebooting `{item.get('name')}` (`{item['id']}`). Action `{action.get('id')}` is "
            f"`{action.get('status')}`.\n\nPoll `action_status`; SSH came back about 20s after the "
            f"action completed when this was measured. Then check `gpu_status`."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Check action status", annotations=READ_ONLY)
async def action_status(action_id: str) -> str:
    """Poll a droplet action returned by start_droplet or stop_droplet."""
    try:
        action = (await _api("GET", f"/actions/{action_id}")).get("action", {})
        status = action.get("status", "?")
        icon = {"completed": "✅", "in-progress": "📡", "errored": "❌"}.get(status, "📡")
        return (
            f"{icon} Action `{action_id}` (`{action.get('type')}`) is `{status}`. "
            f"started {action.get('started_at', '-')}, completed {action.get('completed_at') or '-'}."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Get SSH command", annotations=READ_ONLY)
async def ssh_command(droplet: str) -> str:
    """Print the ssh command for a tagged droplet, with its current address."""
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot connect to `{item.get('name')}`: {why_not}"
        key = f" -i {SSH_KEY}" if SSH_KEY else ""
        port = f" -p {SSH_PORT}" if SSH_PORT != "22" else ""
        return (
            f"✅ `{item.get('name')}` is at {ip}.\n\n"
            f"```\nssh{key}{port} {SSH_USER}@{ip}\n```\n\n"
            f"Project checkout on the droplet: `{REMOTE_WORKDIR}`."
        )
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Run a command on the droplet", annotations=WRITE)
async def run_on_droplet(droplet: str, command: str, timeout: int = 300) -> str:
    """Run one shell command on a tagged droplet over SSH and return its output.

    The command is handed to the remote shell as a single argument; the local
    side never invokes a shell at all. Fails rather than prompts if the key is wrong
    (BatchMode=yes), so a hang here means the command itself is slow, not that
    something is waiting on a password.
    """
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot reach `{item.get('name')}`: {why_not}"
        code, out, err = await run_command(_ssh_argv(ip, command), timeout=timeout)
        icon = "✅" if code == 0 else "❌"
        body = (out or err or "(no output)").strip()
        if len(body) > 6000:
            body = body[:6000] + "\n… truncated at 6000 chars."
        return f"{icon} `{command}` on `{item.get('name')}` exited {code}.\n\n```\n{body}\n```"
    except Exception as exc:
        return _error(exc)


def _summarize_rocm_smi(raw: str) -> Optional[str]:
    """Turn `rocm-smi --json` output into a table, or None if it isn't that.

    The per-GPU figures are parsed and totalled here rather than handed to the
    caller as raw JSON to add up: counting and summing belong in code.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data:
        return None

    def pick(card: dict, *names: str) -> str:
        for name in names:
            for key, value in card.items():
                if key.lower().replace(" ", "") == name:
                    return str(value)
        return "-"

    # The VRAM key is "GPU Memory Allocated (VRAM%)" on ROCM-SMI 2.2.0 here, not
    # any of the plausible "GPU Memory Use (%)" spellings. Measured 2026-09-16
    # while vLLM held 168.3 GiB: this table printed "-" and read as an idle card.
    # The older spellings stay as fallbacks for other rocm-smi builds.
    rows = ["| Card | Product | GPU use % | VRAM used % |", "| --- | --- | --- | --- |"]
    count = 0
    for card, values in sorted(data.items()):
        if not isinstance(values, dict):
            continue
        count += 1
        rows.append(
            f"| {card} | {pick(values, 'cardseries', 'devicename', 'productname')} "
            f"| {pick(values, 'gpuuse(%)', 'gpuuse')} | {pick(values, 'gpumemoryallocated(vram%)', 'gpumemoryuse(%)', 'memoryuse(%)')} |"
        )
    if not count:
        return None
    rows.append("")
    rows.append(f"📡 {count} GPU(s) reported by rocm-smi.")
    return "\n".join(rows)


@mcp.tool(title="Show GPU status on the droplet", annotations=READ_ONLY)
async def gpu_status(droplet: str) -> str:
    """Report the AMD GPUs on a tagged droplet, and say why if there are none.

    This is the only way this project ever sees a GPU: the workstation has
    none. A failure here is a statement about the droplet, not about the local
    machine.

    NEITHER rocm-smi NOR amd-smi SETS A USEFUL EXIT CODE. MEASURED 2026-09-16
    on debian-gpu-mi300x1-192gb-devcloud-atl1: with the driver uninitialised,
    `rocm-smi` printed "Driver not initialized (amdgpu not found in modules)"
    to stderr, printed nothing to stdout, and exited 0; `amd-smi list` printed
    three ERROR lines and exited 0 as well. An earlier version of this tool
    trusted the exit code and reported a healthy "✅" with an empty table. So
    the parsed output decides here, and the exit code is not consulted at all.
    """
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot reach `{item.get('name')}`: {why_not}"

        code, out, err = await run_command(
            _ssh_argv(ip, "rocm-smi --showid --showproductname --showuse --showmemuse --json"),
            timeout=90,
        )
        # Ask whether ssh ran at all before saying anything about the GPU: an
        # unreachable droplet produces no rocm-smi output and no kfd marker, and
        # every "no GPU" branch below would then be a guess about a box nothing
        # has spoken to.
        transport = _ssh_transport_error(code, err)
        if transport:
            return (
                f"❌ Cannot reach `{item.get('name')}` over SSH at {ip}.\n\n```\n{transport}\n```\n\n"
                f"This says nothing about the GPU. The API calls a droplet `active` a minute or more "
                f"before sshd accepts connections, so retry if it was just created or rebooted; if it "
                f"persists, check the key with `ssh_command`."
            )
        table = _summarize_rocm_smi(out)
        if table:
            return f"✅ `{item.get('name')}`\n\n{table}"

        _, out2, err2 = await run_command(_ssh_argv(ip, "amd-smi list"), timeout=90)
        if out2.strip() and "ERROR" not in out2 and "ERROR" not in err2:
            return f"✅ `{item.get('name')}` — via amd-smi:\n\n```\n{out2.strip()[:4000]}\n```"

        # Both tools declined to report a GPU. /dev/kfd is what separates "ROCm
        # is broken" from "ROCm is fine but these two tools are unhappy": it is
        # the compute node every ROCm process opens, and without it nothing on
        # this droplet can use the card no matter what is installed.
        _, probe, _ = await run_command(
            _ssh_argv(ip, "test -e /dev/kfd && echo kfd:present || echo kfd:absent"),
            timeout=60,
        )
        complaint = (err or out or "").strip() or (err2 or out2 or "").strip() or "(no output)"
        # Three answers, not two. Treating "not absent" as "present" turned a
        # probe that never ran into a confident "the driver is up" — which is
        # the wrong answer to act on, and the expensive one.
        if "kfd:absent" in probe:
            diagnosis = (
                "`/dev/kfd` does not exist, so ROCm compute is unavailable on this droplet — the card may "
                "be visible to `lspci` and still be unusable. On a freshly provisioned droplet this is "
                "normal and the fix is `reboot_droplet`, not installing anything: amdgpu fails to bind the "
                "MI300X VF during provisioning and unloads itself."
            )
        elif "kfd:present" in probe:
            diagnosis = (
                "`/dev/kfd` exists, but that is weaker evidence than it looks: the node is created "
                "before `amdgpu` finishes probing, so it survives a bind that failed. Run "
                "`hardware_scan` — if it reports the card on the PCI bus with no gfx agent, the driver "
                "did not bind and `reboot_droplet` is the fix. Otherwise the fault is in the tooling "
                "or its permissions."
            )
        else:
            diagnosis = (
                "The `/dev/kfd` probe returned neither answer, so the state of the driver is unknown — "
                "treat this as an unreachable droplet rather than a broken GPU."
            )
        return (
            f"❌ No GPU reported on `{item.get('name')}` — and note both tools exited 0 while failing.\n\n"
            f"```\n{complaint[:800]}\n```\n\n{diagnosis}"
        )
    except Exception as exc:
        return _error(exc)


# One remote script, one round trip. Each section is fenced by a marker so the
# parsing below never has to guess where an unfamiliar tool's output ended. Every
# probe is guarded: a missing tool prints "-" rather than failing the whole scan,
# because the interesting case is exactly the droplet where half of this is absent.
_SCAN_SCRIPT = r"""
say() { printf '<<<%s>>>\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

say host
hostname; uname -r; (. /etc/os-release && echo "$PRETTY_NAME"); uptime -p
awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo
nproc
awk '/MemTotal/{printf "%.0f GB\n", $2/1048576}' /proc/meminfo
df -h --output=avail / | tail -1

say kfd
test -e /dev/kfd && echo present || echo ABSENT
lsmod | awk '$1=="amdgpu"{print "loaded"; found=1} END{if(!found) print "NOT loaded"}'
cat /sys/module/amdgpu/version 2>/dev/null || echo -

say pci
lspci -nn 2>/dev/null | grep -iE 'processing accelerator|vga|display' || echo -

say agents
have rocminfo && rocminfo 2>/dev/null | grep -E '^\s*(Name|Marketing Name|Compute Unit|Uuid):' || echo -

say vram
have rocm-smi && rocm-smi --showmeminfo vram --csv 2>/dev/null | grep -i '^card' || echo -

say firmware
have rocm-smi && rocm-smi --showfw 2>/dev/null | grep -iE '^GPU|FW version|VBIOS' | head -40 || echo -
have rocm-smi && rocm-smi --showvbios 2>/dev/null | grep -i vbios | head -4 || echo -

say tools
for t in rocm-smi rocminfo amd-smi hipcc clinfo docker podman python3 pip3 git tmux; do
  if have "$t"; then printf '%s\t%s\n' "$t" "$(command -v $t)"; else printf '%s\t-\n' "$t"; fi
done

say versions
have rocm-smi && rocm-smi --version 2>/dev/null | head -2 || echo -
have hipcc && hipcc --version 2>/dev/null | head -2 || echo -
have docker && docker --version 2>/dev/null || echo -
have python3 && python3 --version || echo -
ls -d /opt/rocm* 2>/dev/null || echo "no /opt/rocm"

say packages
# Matched against the package NAME only, and against names that really exist,
# because a bare "hip" anywhere in the line put `whiptail` under "ROCm packages"
# on a box with no ROCm at all — measured 2026-09-17 on 601418522. "hsa" and
# "hip" cannot be used as loose substrings for the same reason.
dpkg -l 2>/dev/null | awk '$2 ~ /amdgpu|rocm|hsakmt|libhsa|hipcc|libamdhip|hip-runtime|hipblas|hipfft|hiprand|hipsparse|hipsolver|miopen|rccl|comgr/ {print $2"\t"$3}' | head -25

say python
have python3 && python3 -c "import torch;print('torch',torch.__version__,'hip',getattr(torch.version,'hip',None),'avail',torch.cuda.is_available())" 2>&1 | tail -1 || echo -
have python3 && python3 -c "import vllm;print('vllm',vllm.__version__)" 2>&1 | tail -1 || echo -
"""


def _sections(raw: str) -> dict:
    """Split the scan output on its <<<marker>>> fences."""
    out: dict = {}
    current = None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("<<<") and stripped.endswith(">>>"):
            current = stripped[3:-3]
            out[current] = []
        elif current:
            out[current].append(line.rstrip())
    return {k: [ln for ln in v if ln.strip()] for k, v in out.items()}


@mcp.tool(title="Scan hardware, firmware and tooling", annotations=READ_ONLY)
async def hardware_scan(droplet: str) -> str:
    """Inventory a droplet: host, GPU, firmware, ROCm packages and installed tools.

    One SSH round trip. Answers the questions that decide what can be deployed
    on a box — is /dev/kfd there, which gfx target, how much VRAM, is docker
    installed, is there a /opt/rocm, is torch built for HIP — without needing a
    separate call per question.
    """
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot reach `{item.get('name')}`: {why_not}"

        code, out, err = await run_command(_ssh_argv(ip, _SCAN_SCRIPT), timeout=180)
        sec = _sections(out)
        if not sec:
            return f"❌ Scan returned nothing from `{item.get('name')}` (exit {code}).\n\n```\n{err[:600]}\n```"

        def block(name: str) -> list[str]:
            return sec.get(name, [])

        host = block("host")
        kfd = block("kfd")
        lines = [f"# `{item.get('name')}`", ""]

        lines += ["## Host", ""]
        labels = ["hostname", "kernel", "os", "uptime", "cpu", "cores", "ram", "disk free /"]
        # strict=False on purpose: a probe that failed leaves `host` short, and a
        # partial scan is worth printing.
        for label, value in zip(labels, host, strict=False):
            lines.append(f"- {label}: {value.strip()}")

        lines += ["", "## GPU", ""]
        if kfd:
            lines.append(f"- `/dev/kfd`: **{kfd[0]}**" + ("" if kfd[0] == "present" else "  ← ROCm cannot work"))
        if len(kfd) > 1:
            lines.append(f"- amdgpu module: {kfd[1]}")
        if len(kfd) > 2:
            lines.append(f"- amdgpu version: {kfd[2]}")
        for line in block("pci"):
            lines.append(f"- pci: `{line.strip()}`")

        # rocminfo lists the CPU agent first and the GPU agents after it, so the
        # gfx targets are counted rather than assumed to be one. The ISA line
        # ("Name: amdgcn-amd-amdhsa--gfx942:sramecc+:xnack-") uses the same
        # "Name:" key as the agent line ("Name: gfx942"), so matching on "gfx"
        # alone double-counts every GPU — measured 2026-09-16, one MI300X
        # reported as "2 GPU agent(s)". Only a bare gfx<digits> target counts.
        agents = block("agents")
        gfx = [
            value
            for ln in agents
            if ln.strip().startswith("Name:")
            for value in [ln.split(":", 1)[1].strip()]
            if re.fullmatch(r"gfx[0-9a-f]+", value)
        ]
        marketing = [ln.split(":", 1)[1].strip() for ln in agents if "Marketing Name" in ln and "CPU" not in ln]
        if gfx:
            lines.append(f"- gfx targets: {', '.join(f'`{g}`' for g in gfx)}  ({len(gfx)} GPU agent(s))")
        elif any("Processing accelerator" in ln or "Instinct" in ln for ln in block("pci")):
            # The card is on the PCI bus and ROCm sees no GPU agent, which is
            # the bind failure and not a missing device. /dev/kfd is NOT the
            # test for this: it and /dev/dri/renderD128 are created before the
            # probe fails, so both can be present on a card nothing can use.
            # Measured 2026-09-17 on 601418522 — `amdgpu` was in lsmod, /dev/kfd
            # existed, rocminfo listed only the CPU, and dmesg showed
            # "Doesn't get msg:1 from pf, error=-62" inside amdgpu_pci_probe.
            lines.append(
                "- **gfx targets: none — the card is on the PCI bus and `amdgpu` did not bind to it.**"
                "  `reboot_droplet` is the fix; nothing needs installing. Neither `/dev/kfd` above nor"
                " `amdgpu` in `lsmod` contradicts this."
            )
        for name in marketing:
            if "AMD" in name or "Instinct" in name or "Radeon" in name:
                lines.append(f"- device: {name}")
                break
        for line in block("vram"):
            lines.append(f"- vram: `{line.strip()}`")

        fw = block("firmware")
        lines += ["", "## Firmware", ""]
        lines.append(f"```\n{chr(10).join(fw[:30]) if fw else '(none reported)'}\n```")

        lines += ["", "## Tools", ""]
        present, missing = [], []
        for line in block("tools"):
            parts = line.split("\t")
            if len(parts) == 2:
                (present if parts[1] != "-" else missing).append(parts[0])
        lines.append(f"- present ({len(present)}): {', '.join(f'`{t}`' for t in present) or '-'}")
        lines.append(f"- **missing ({len(missing)})**: {', '.join(f'`{t}`' for t in missing) or '-'}")
        for line in block("versions"):
            lines.append(f"- {line.strip()}")

        pkgs = block("packages")
        lines += ["", f"## ROCm packages ({len(pkgs)})", "", f"```\n{chr(10).join(pkgs) or '(none)'}\n```"]

        lines += ["", "## Python", ""]
        for line in block("python"):
            lines.append(f"- {line.strip()}")

        return "\n".join(lines)
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="Prepare a droplet for serving", annotations=WRITE)
async def prepare_droplet(droplet: str, pull_image: bool = True, image: Optional[str] = None) -> str:
    """Bring a bare droplet up to the point where vLLM can be served.

    The stock AMD Developer Cloud image ships a working kernel and an empty
    userspace: measured on a fresh droplet, of eleven tools probed only python3
    existed — no rocm-smi, no rocminfo, no docker, no git, no pip3, no
    /opt/rocm. Nothing here can serve a model until that is fixed, and
    gpu_status reports "command not found" in the meantime.

    Three steps, each reported separately and none of them fatal to the others:

    1. apt sources. Every `Components:` line in the deb822 sources file is
       rewritten to APT_COMPONENTS — contrib and non-free are not optional
       extras on this box, because firmware lives in non-free-firmware — and
       `-backports` is added to the release suite only if it is not there
       already. It usually is. The file is edited in place and backed up once
       to `.amd-gputools.bak`, because its URIs are a DigitalOcean mirror
       indirection that a hand-written `deb` line would bypass.
    2. SETUP_PACKAGES, which includes docker and Debian's ROCm tools, then
       enables the docker service.
    3. With pull_image=True, starts a DETACHED `docker pull` of VLLM_IMAGE and
       returns immediately. The ROCm vLLM images are 35-62 GB, so a foreground
       pull would outlive the call and look like a failure. Poll it with
       `vllm_image_status`.

    Safe to run twice: the sources rewrite is idempotent, apt-get install is a
    no-op on packages already present, and the pull declines if the image is
    already there or already being fetched.
    """
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot reach `{item.get('name')}`: {why_not}"

        script = _with_env(
            _read_script("remote-prepare.sh"),
            APT_COMPONENTS=APT_COMPONENTS,
            SETUP_PACKAGES=SETUP_PACKAGES,
            BACKPORTS_PACKAGES=BACKPORTS_PACKAGES,
        )
        code, out, err = await run_command(_ssh_argv(ip, script), timeout=900)
        transport = _ssh_transport_error(code, err)
        if transport:
            return f"❌ Cannot reach `{item.get('name')}` over SSH at {ip}.\n\n```\n{transport}\n```"
        sec = _sections(out)
        if not sec:
            return f"❌ Setup returned nothing from `{item.get('name')}` (exit {code}).\n\n```\n{err[:600]}\n```"

        lines = [f"# Prepared `{item.get('name')}`", ""]

        before = sec.get("before", [])
        after = sec.get("sources", [])
        lines += ["## apt sources", ""]
        lines.append(f"```\nbefore: {' | '.join(before) or '(none)'}\nafter:  {' | '.join(after) or '(none)'}\n```")
        # Saying which of the two things actually changed matters, because the
        # obvious assumption — that backports needed enabling — was wrong here.
        joined_before, joined_after = " ".join(before), " ".join(after)
        changed = []
        if "contrib" not in joined_before and "contrib" in joined_after:
            changed.append("components")
        if "-backports" not in joined_before and "-backports" in joined_after:
            changed.append("backports suite")
        if "-backports" in joined_before:
            lines.append("")
            lines.append("📡 backports was **already enabled**; only the components were short.")
        lines.append("")
        lines.append(f"Changed: {', '.join(changed) or 'nothing — already configured'}.")

        lines += ["", "## apt", ""]
        for name in ("update", "install"):
            body = "\n".join(sec.get(name, [])) or "(no output)"
            lines.append(f"**{name}**\n\n```\n{body[:1200]}\n```")

        lines += ["", "## docker", ""]
        for line in sec.get("docker", []):
            lines.append(f"- {line.strip()}")
        gids = {}
        for line in sec.get("gids", []):
            if "=" in line:
                key, _, value = line.strip().partition("=")
                gids[key] = value
        if gids:
            # --group-add render fails outright on the container images: they
            # have no render group of their own. The numeric GIDs are resolved
            # here so vllm/docker-serve.sh never has to guess. See CLAUDE.md.
            lines.append(f"- GPU group GIDs on this host: {', '.join(f'`{k}:{v}`' for k, v in gids.items())}")

        rocm = [ln for ln in sec.get("rocm", []) if ln.strip() and ln.strip() != "-"]
        lines += ["", "## ROCm userspace", ""]
        lines.append(f"```\n{chr(10).join(rocm)[:600] if rocm else '(rocm-smi still reports nothing)'}\n```")

        for line in sec.get("disk", []):
            lines.append(f"- disk free for images: {line.strip()}")

        wanted = (image or VLLM_IMAGE).strip()
        lines += ["", "## vLLM image", ""]
        if not pull_image:
            lines.append(f"Not pulled (pull_image=false). `{wanted}` when you want it.")
        else:
            started = await _start_pull(ip, wanted)
            lines.append(started)
        return "\n".join(lines)
    except Exception as exc:
        return _error(exc)


async def _start_pull(ip: str, image: str) -> str:
    """Kick off the detached pull and say what the droplet made of the request."""
    script = _PULL_START_SCRIPT.replace("__IMAGE__", image).replace("__LOG__", PULL_LOG)
    _, out, _ = await run_command(_ssh_argv(ip, script), timeout=120)
    answer = (out or "").strip()
    if "NO_DOCKER" in answer:
        return "❌ docker is not installed, so nothing was pulled. The install step above is why."
    if "ALREADY_PRESENT" in answer:
        return f"✅ `{image}` is already on the droplet. Nothing to pull."
    if "ALREADY_RUNNING" in answer:
        return f"📡 A pull of `{image}` is already running. Poll `vllm_image_status`."
    return (
        f"📡 Started a detached pull of `{image}` ({answer}).\n\n"
        f"These images are 35-62 GB, so this runs for minutes with nothing to see. "
        f"Poll `vllm_image_status`; the log is at `{PULL_LOG}` on the droplet."
    )


@mcp.tool(title="Check the vLLM image pull", annotations=READ_ONLY)
async def vllm_image_status(droplet: str, image: Optional[str] = None, probe: bool = False) -> str:
    """Report whether the vLLM image is down yet, and optionally test it for Gemma 4.

    probe=True runs the image's own python3 against /dev/kfd and prints the
    vLLM, torch and transformers versions, the gfx targets the build actually
    carries, and whether the Gemma 4 config convertor is present. That last one
    is the check worth doing before a serving attempt rather than after: an
    image missing `Gemma4ModelArchConfigConvertor` fails on `head_dim` while
    parsing config, long before the GPU is touched, and the error names neither
    Gemma nor the image.
    """
    try:
        item = await _resolve(droplet)
        ip, why_not = await _reachable(item)
        if why_not:
            return f"❌ Cannot reach `{item.get('name')}`: {why_not}"
        wanted = (image or VLLM_IMAGE).strip()

        script = _PULL_STATUS_SCRIPT.replace("__IMAGE__", wanted).replace("__LOG__", PULL_LOG)
        code, out, err = await run_command(_ssh_argv(ip, script), timeout=120)
        transport = _ssh_transport_error(code, err)
        if transport:
            return f"❌ Cannot reach `{item.get('name')}` over SSH at {ip}.\n\n```\n{transport}\n```"
        sec = _sections(out)
        present = sec.get("image", ["absent"])[0].strip()
        running = (sec.get("running", ["no"])[0]).strip() == "yes"
        log = "\n".join(sec.get("log", [])) or "(no log yet)"

        if present == "absent":
            icon = "📡" if running else "❌"
            state = "still pulling" if running else "not present and no pull is running"
            return f"{icon} `{wanted}` is {state} on `{item.get('name')}`.\n\n```\n{log[:800]}\n```" + (
                "" if running else f"\n\nStart one with `prepare_droplet` or check `{PULL_LOG}`."
            )

        # `docker image inspect` prints the size in bytes; convert it here
        # rather than handing a reader eleven digits to parse.
        parts = present.split()
        size = ""
        if len(parts) > 1 and parts[1].isdigit():
            size = f", {int(parts[1]) / 1_000_000_000:.1f} GB on disk"
        head = f"✅ `{wanted}` is present on `{item.get('name')}`{size}."
        if not probe:
            return f"{head}\n\nRun again with probe=true to check it for Gemma 4 support."
        return f"{head}\n\n{await _probe_image(ip, wanted)}"
    except Exception as exc:
        return _error(exc)


async def _probe_image(ip: str, image: str) -> str:
    """Run CLAUDE.md's Gemma 4 capability check inside the image."""
    # The probe is written to a file and mounted rather than passed as -c, so
    # nothing has to survive two levels of shell quoting. The GIDs are resolved
    # on the host: --group-add render fails outright against these images,
    # which have no render group of their own.
    runner = (
        "cat > /tmp/gemma4_probe.py <<'PROBE'\n"
        + _read_script("gemma4-probe.py")
        + "\nPROBE\n"
        + "VID=$(getent group video | cut -d: -f3); REN=$(getent group render | cut -d: -f3)\n"
        + "docker run --rm -v /tmp/gemma4_probe.py:/probe.py --device /dev/kfd --device /dev/dri "
        + f'--group-add "$VID" --group-add "$REN" --entrypoint python3 {image} /probe.py 2>&1 | tail -40\n'
    )
    _, out, err = await run_command(_ssh_argv(ip, runner), timeout=600)
    body = (out or err or "(no output)").strip()
    verdict = ""
    if "gemma4_convertor" in body:
        missing = '"gemma4_convertor": "not found"' in body or "None" in body.split("gemma4_convertor")[1][:40]
        verdict = (
            "\n\n❌ **No Gemma 4 convertor in this build.** It will raise on `head_dim` while parsing "
            "config, before the GPU is touched. A different image is needed, not a different flag."
            if missing
            else "\n\n✅ **Gemma 4 convertor present.**"
        )
    return f"```json\n{body[:3000]}\n```{verdict}"


@mcp.tool(title="List GPU droplet sizes", annotations=READ_ONLY)
async def list_gpu_sizes(contains: Optional[str] = None) -> str:
    """List DigitalOcean GPU droplet sizes and their prices, cheapest first.

    `contains` filters the slug — pass "mi300" for the AMD Instinct sizes. The
    filtering, sorting and totals happen here so the answer is exact; the size
    catalogue changes on DigitalOcean's schedule, so read it rather than
    assuming a slug.
    """
    try:
        sizes = await _paged("/sizes", "sizes")
        gpus = [s for s in sizes if str(s.get("slug", "")).startswith("gpu-") and s.get("available", True)]
        if contains:
            needle = contains.lower()
            gpus = [s for s in gpus if needle in str(s.get("slug", "")).lower()]
        if not gpus:
            scope = f" matching `{contains}`" if contains else ""
            return f"No available GPU sizes{scope}. GPU capacity is region-limited and changes."
        gpus.sort(key=lambda s: s.get("price_hourly") or 0.0)
        lines = [
            "| Slug | vCPU | RAM GB | Disk GB | Price | Regions |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for size in gpus:
            regions = ", ".join(size.get("regions", [])) or "-"
            lines.append(
                f"| `{size.get('slug')}` | {size.get('vcpus', '-')} | {(size.get('memory') or 0) // 1024} "
                f"| {size.get('disk', '-')} | {_fmt_price(size)} | {regions} |"
            )
        cheapest = gpus[0]
        lines.append("")
        lines.append(
            f"📡 {len(gpus)} available GPU size(s). Cheapest: `{cheapest.get('slug')}` at "
            f"{_fmt_price(cheapest)}. A powered-off droplet of any of these still bills at that rate."
        )
        return "\n".join(lines)
    except Exception as exc:
        return _error(exc)


@mcp.tool(title="List this server's tools", annotations=READ_ONLY)
async def get_help() -> str:
    """List the tools this server exposes."""
    tools = await mcp.list_tools()
    lines = [
        f"📡 **{MCP_SERVER_NAME}** — DigitalOcean control plane for this project's AMD MI300 droplets.",
        "",
        f"Scoped to droplets tagged `{DROPLET_TAG}`; SSH as `{SSH_USER}`; checkout at `{REMOTE_WORKDIR}`.",
        "",
    ]
    for tool in tools:
        lines.append(f"- **{tool.name}** — {(tool.description or '').splitlines()[0]}")
    lines += [
        "",
        (
            "`create_droplet` orders nothing until it is called a second time with confirm=true, and it "
            "always applies the tag. There is no destroy tool by design: that one stays a deliberate "
            "step in the DigitalOcean console."
        ),
        "",
        "Powering a droplet off does not stop DigitalOcean billing it.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
