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

Deliberately absent: create and destroy. This server can find, start, stop,
reach and interrogate droplets that already exist, and it is scoped by tag so
it cannot act on a droplet it was not pointed at. Provisioning an MI300 droplet
and destroying one are the two operations whose cost of being wrong is measured
in dollars per hour and in lost local state, so they stay a deliberate human
step in the DigitalOcean console or a future tool added on purpose.

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
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

PROJECT_DIR = Path(__file__).resolve().parent
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

mcp = MCPServer(MCP_SERVER_NAME)
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
WRITE = ToolAnnotations(destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


def _error(exc: Exception) -> str:
    """Render an exception as the markdown a tool returns instead of raising."""
    if isinstance(exc, httpx.HTTPError):
        return f"❌ DigitalOcean API unreachable: {exc}"
    return f"❌ {exc}"


def _token() -> str:
    """Read the API token, preferring the name doctl and Terraform use.

    The token is never read from amd.env — that file is committed. It comes
    from the environment or from .env, which is gitignored and mode 0600.
    """
    token = os.environ.get("DIGITALOCEAN_ACCESS_TOKEN") or os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        raise RuntimeError(
            "DIGITALOCEAN_ACCESS_TOKEN is unset. Put it in `.env` (gitignored, mode 0600) "
            "or export it — never in `amd.env`, which is committed."
        )
    return token


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

    rows = ["| Card | Product | GPU use % | VRAM used % |", "| --- | --- | --- | --- |"]
    count = 0
    for card, values in sorted(data.items()):
        if not isinstance(values, dict):
            continue
        count += 1
        rows.append(
            f"| {card} | {pick(values, 'cardseries', 'devicename', 'productname')} "
            f"| {pick(values, 'gpuuse(%)', 'gpuuse')} | {pick(values, 'gpumemoryuse(%)', 'memoryuse(%)')} |"
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

        _, out, err = await run_command(
            _ssh_argv(ip, "rocm-smi --showid --showproductname --showuse --showmemuse --json"),
            timeout=90,
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
        kfd_absent = "kfd:absent" in probe
        complaint = (err or out or "").strip() or (err2 or out2 or "").strip() or "(no output)"
        diagnosis = (
            "`/dev/kfd` does not exist, so ROCm compute is unavailable on this droplet — the card may be "
            "visible to `lspci` and still be unusable. The stock amdgpu module does not bring up the "
            "compute node for an MI300X VF; that needs the ROCm driver stack installed on the droplet."
            if kfd_absent
            else "`/dev/kfd` exists, so the driver is up and the fault is in the tooling or its permissions."
        )
        return (
            f"❌ No GPU reported on `{item.get('name')}` — and note both tools exited 0 while failing.\n\n"
            f"```\n{complaint[:800]}\n```\n\n{diagnosis}"
        )
    except Exception as exc:
        return _error(exc)


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
            "No create or destroy tools, by design: both are dollar-per-hour decisions, so they stay "
            "a deliberate step in the DigitalOcean console."
        ),
        "",
        "Powering a droplet off does not stop DigitalOcean billing it.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
