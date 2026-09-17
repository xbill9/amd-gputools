"""Offline unit tests for the amd-gputools MCP server.

unittest, never pytest. The whole `mcp` package is mocked before `server` is
imported, so nothing here reaches the DigitalOcean API, opens an SSH
connection, or needs a token.

A bare MagicMock is NOT enough as the fake: `@mcp.tool()` would then return a
MagicMock instead of the decorated coroutine, and every tool test fails with
"'MagicMock' object can't be awaited" — which reads as a broken server and is
really a broken fake. So `tool()` is a pass-through decorator and the real
functions survive.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class _FakeMCPServer:
    def __init__(self, name):
        self.name = name

    def tool(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    async def list_tools(self):
        return []

    def run(self):
        raise AssertionError("mcp.run() must never be called from a test")


_mcpserver_module = MagicMock()
_mcpserver_module.MCPServer = _FakeMCPServer
sys.modules["mcp"] = MagicMock()
sys.modules["mcp.server"] = MagicMock()
sys.modules["mcp.server.mcpserver"] = _mcpserver_module
sys.modules["mcp.types"] = MagicMock()

import server  # noqa: E402

ACTIVE = {
    "id": 123456789,
    "name": "mi300-1",
    "status": "active",
    "size_slug": "gpu-mi300x1-192gb",
    "region": {"slug": "atl1"},
    "networks": {
        "v4": [{"type": "private", "ip_address": "10.0.0.2"}, {"type": "public", "ip_address": "203.0.113.7"}]
    },
    "tags": ["amd-gputools"],
}
OFF = dict(ACTIVE, id=987654321, name="mi300-2", status="off", networks={"v4": []})


class TokenTests(unittest.TestCase):
    """The token is read from the environment, never from the committed amd.env."""

    def test_missing_token_names_the_right_file(self):
        with patch.dict("os.environ", {}, clear=True), self._no_ocean_txt():
            with self.assertRaises(RuntimeError) as ctx:
                server._token()
        message = str(ctx.exception)
        self.assertIn("DIGITALOCEAN_ACCESS_TOKEN", message)
        # The remediation must not send anyone to the committed file.
        self.assertIn(".env", message)
        self.assertIn("ocean.txt", message)
        self.assertIn("never in `amd.env`", message)

    def test_either_env_var_works(self):
        for name in ("DIGITALOCEAN_ACCESS_TOKEN", "DIGITALOCEAN_TOKEN"):
            with patch.dict("os.environ", {name: "dop_v1_x"}, clear=True):
                self.assertEqual(server._token(), "dop_v1_x")

    def test_ocean_txt_is_the_last_resort(self):
        """With nothing in the environment, ~/ocean.txt still yields a token.

        ssh-droplet.sh has always read it. Before this fallback existed the
        shell script worked and every MCP tool returned "token is unset".
        """
        with tempfile.TemporaryDirectory() as tmp:
            ocean = Path(tmp) / "ocean.txt"
            ocean.write_text("dop_v1_from_file\n")
            with patch.dict("os.environ", {}, clear=True):
                with patch.object(server, "OCEAN_TXT", ocean):
                    self.assertEqual(server._token(), "dop_v1_from_file")

    def test_environment_beats_ocean_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ocean = Path(tmp) / "ocean.txt"
            ocean.write_text("dop_v1_stale\n")
            with patch.dict("os.environ", {"DIGITALOCEAN_ACCESS_TOKEN": "dop_v1_live"}, clear=True):
                with patch.object(server, "OCEAN_TXT", ocean):
                    self.assertEqual(server._token(), "dop_v1_live")

    def test_empty_ocean_txt_is_not_a_token(self):
        """An empty or blank file must raise, not return "" as a valid token."""
        with tempfile.TemporaryDirectory() as tmp:
            ocean = Path(tmp) / "ocean.txt"
            ocean.write_text("\n")
            with patch.dict("os.environ", {}, clear=True):
                with patch.object(server, "OCEAN_TXT", ocean):
                    with self.assertRaises(RuntimeError):
                        server._token()

    def test_dotenv_is_loaded_before_amd_env(self):
        """.env must be loaded too, or its token never reaches the process.

        The error message told people to put the token in .env while the server
        only ever loaded amd.env, so following the instructions changed nothing.
        """
        source = (PROJECT_DIR / "server.py").read_text()
        self.assertIn('load_dotenv(PROJECT_DIR / ".env")', source)
        self.assertLess(
            source.index('load_dotenv(PROJECT_DIR / ".env")'),
            source.index('load_dotenv(PROJECT_DIR / "amd.env")'),
        )

    def test_dotenv_is_gitignored(self):
        """.env holds the live token; git must never be able to see it."""
        ignored = (PROJECT_DIR / ".gitignore").read_text().splitlines()
        self.assertIn(".env", [line.strip() for line in ignored])

    @staticmethod
    def _no_ocean_txt():
        return patch.object(server, "OCEAN_TXT", Path("/nonexistent/ocean.txt"))

    def test_amd_env_carries_no_secret(self):
        """amd.env is committed, so a token appearing in it is a leak."""
        text = (PROJECT_DIR / "amd.env").read_text()
        self.assertNotIn("dop_v1_", text)
        for line in text.splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            self.assertNotIn("TOKEN", key.upper(), f"secret-shaped key in committed amd.env: {line}")


class PublicIPTests(unittest.TestCase):
    """The address is resolved per call; a rebuilt droplet gets a new one."""

    def test_picks_public_not_private(self):
        self.assertEqual(server._public_ip(ACTIVE), "203.0.113.7")

    def test_none_when_networking_is_not_up(self):
        self.assertIsNone(server._public_ip(OFF))


class SSHArgvTests(unittest.TestCase):
    """ssh must fail instead of prompting: a prompt hangs the tool with no output."""

    def test_never_prompts(self):
        argv = server._ssh_argv("203.0.113.7", "rocm-smi")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("ConnectTimeout=10", argv)

    def test_command_is_one_argument(self):
        """The remote command is a single argv entry — no local shell splits it."""
        argv = server._ssh_argv("203.0.113.7", "echo one two; echo three")
        self.assertEqual(argv[-1], "echo one two; echo three")

    def test_key_is_only_passed_when_configured(self):
        with patch.object(server, "SSH_KEY", ""):
            self.assertNotIn("-i", server._ssh_argv("203.0.113.7"))
        with patch.object(server, "SSH_KEY", "~/.ssh/amd"):
            argv = server._ssh_argv("203.0.113.7")
            self.assertIn("-i", argv)
            self.assertNotIn("~", argv[argv.index("-i") + 1], "~ must be expanded; ssh does not expand it")


class ReachableTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_with_ip(self):
        ip, why_not = await server._reachable(ACTIVE)
        self.assertEqual(ip, "203.0.113.7")
        self.assertIsNone(why_not)

    async def test_off_droplet_is_refused_with_the_fix(self):
        ip, why_not = await server._reachable(OFF)
        self.assertIsNone(ip)
        self.assertIn("start_droplet", why_not)


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    """Resolution is tag-scoped: an untagged droplet cannot be addressed at all."""

    async def test_by_id_and_by_name(self):
        with patch.object(server, "_droplets", AsyncMock(return_value=[ACTIVE, OFF])):
            self.assertEqual((await server._resolve("123456789"))["name"], "mi300-1")
            self.assertEqual((await server._resolve("mi300-2"))["id"], 987654321)

    async def test_unknown_droplet_lists_what_is_available(self):
        with patch.object(server, "_droplets", AsyncMock(return_value=[ACTIVE])):
            with self.assertRaises(RuntimeError) as ctx:
                await server._resolve("999")
        self.assertIn("mi300-1", str(ctx.exception))

    async def test_empty_tag_scope_says_how_to_fix_it(self):
        with patch.object(server, "_droplets", AsyncMock(return_value=[])):
            with self.assertRaises(RuntimeError) as ctx:
                await server._resolve("anything")
        self.assertIn("DROPLET_TAG", str(ctx.exception))


class ToolErrorTests(unittest.IsolatedAsyncioTestCase):
    """Tools return '❌ ...' — an exception escaping one kills the server process."""

    async def test_api_failure_becomes_a_string(self):
        with patch.object(server, "_droplets", AsyncMock(side_effect=RuntimeError("boom"))):
            result = await server.list_droplets()
        self.assertTrue(result.startswith("❌"), result)

    def test_every_tool_catches(self):
        """Each @mcp.tool body must contain `except Exception`, checked by parsing.

        This counted the two substrings and compared the totals until the
        remote Gemma 4 probe — Python source embedded in a string constant —
        contributed three `except Exception as exc:` of its own and the count
        passed while meaning nothing. Substring matching on source finds things
        that are not there; the tree does not have that problem.
        """
        import ast

        tree = ast.parse((PROJECT_DIR / "server.py").read_text())

        def is_tool(node):
            return any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "tool"
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "mcp"
                for d in node.decorator_list
            )

        def catches(node):
            return any(
                isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
                for inner in ast.walk(node)
                if isinstance(inner, ast.Try)
                for handler in inner.handlers
            )

        tools = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_tool(n)]
        self.assertGreater(len(tools), 10, "the tools were not found; the decorator shape changed")
        unguarded = sorted(n.name for n in tools if not catches(n))
        # get_help has no external call and needs no handler; everything else does.
        self.assertEqual(unguarded, ["get_help"], "a tool is missing its `except Exception` guard")


class StopDropletTests(unittest.IsolatedAsyncioTestCase):
    async def test_graceful_sends_shutdown_and_hard_sends_power_off(self):
        sent = []

        async def fake_api(method, path, payload=None, timeout=30):
            sent.append(payload["type"])
            return {"action": {"id": 1, "status": "in-progress"}}

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "_api", fake_api):
                await server.stop_droplet("mi300-1")
                await server.stop_droplet("mi300-1", graceful=False)
        self.assertEqual(sent, ["shutdown", "power_off"])

    async def test_says_billing_continues(self):
        """The costly misconception this server exists to correct."""
        with patch.object(server, "_resolve", AsyncMock(return_value=OFF)):
            result = await server.stop_droplet("mi300-2")
        self.assertIn("billed", result.lower())


class RocmSmiSummaryTests(unittest.TestCase):
    """Counting and totalling happen here, not in the model reading the output."""

    def test_counts_cards(self):
        raw = """{"card0": {"Card Series": "MI300X", "GPU use (%)": "97", "GPU Memory Use (%)": "81"},
                  "card1": {"Card Series": "MI300X", "GPU use (%)": "0", "GPU Memory Use (%)": "2"}}"""
        table = server._summarize_rocm_smi(raw)
        self.assertIn("2 GPU(s)", table)
        self.assertIn("MI300X", table)

    def test_reads_the_vram_key_rocm_smi_actually_emits(self):
        """Verbatim from ROCM-SMI 2.2.0 while vLLM held 168.3 GiB of 191.7 GiB."""
        raw = """{"card0": {"Device Name": "Aqua Vanjaram [Instinct MI300X VF]",
                            "GPU use (%)": "0", "GPU Memory Allocated (VRAM%)": "87",
                            "Card Series": "Aqua Vanjaram [Instinct MI300X VF]"}}"""
        table = server._summarize_rocm_smi(raw)
        self.assertIn("| 87 |", table)
        self.assertNotIn("| - |", table)

    def test_non_json_is_rejected_rather_than_guessed(self):
        self.assertIsNone(server._summarize_rocm_smi("GPU[0] : something human readable"))
        self.assertIsNone(server._summarize_rocm_smi("[]"))


class NoLocalGPUAssumptionTests(unittest.TestCase):
    """This workstation has no AMD GPU; every ROCm call must go through ssh."""

    def test_rocm_is_only_ever_invoked_remotely(self):
        source = (PROJECT_DIR / "server.py").read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if "rocm-smi" in stripped or "amd-smi" in stripped:
                if stripped.startswith("#") or stripped.startswith('"') or "_ssh_argv" in stripped:
                    continue
                self.assertNotIn(
                    "run_command(",
                    stripped,
                    f"ROCm invoked without _ssh_argv — there is no local GPU: {stripped[:120]}",
                )

    def test_no_shell_true_anywhere(self):
        """The only permitted mention of shell=True is the rule forbidding it."""
        source = (PROJECT_DIR / "server.py").read_text()
        offenders = [
            line.strip() for line in source.splitlines() if "shell=True" in line and "Never shell=True" not in line
        ]
        self.assertEqual(offenders, [], f"shell=True used in server.py: {offenders}")


class GPUStatusExitCodeTests(unittest.IsolatedAsyncioTestCase):
    """rocm-smi and amd-smi exit 0 while failing, so the exit code decides nothing.

    Measured 2026-09-16 on the live MI300X droplet: with the driver
    uninitialised, rocm-smi wrote its complaint to stderr, wrote nothing to
    stdout and exited 0. The first version of gpu_status trusted that 0 and
    reported "✅" with an empty table, which is the worst possible answer — a
    broken GPU that looks healthy.
    """

    async def _run(self, responses):
        calls = []

        async def fake_run_command(cmd, timeout=120):
            calls.append(cmd[-1])
            return responses.pop(0)

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.gpu_status("mi300-1"), calls

    async def test_zero_exit_with_empty_stdout_is_not_success(self):
        result, _ = await self._run(
            [
                (0, "", "ERROR:root:Driver not initialized (amdgpu not found in modules)"),
                (0, "ERROR: Unable to get devices, driver not initialized", ""),
                (0, "kfd:absent\n", ""),
            ]
        )
        self.assertTrue(result.startswith("❌"), result)
        self.assertIn("exited 0 while failing", result)

    async def test_absent_kfd_is_named_as_the_cause(self):
        result, _ = await self._run(
            [(0, "", "Driver not initialized"), (0, "ERROR: no devices", ""), (0, "kfd:absent\n", "")]
        )
        self.assertIn("/dev/kfd", result)
        self.assertIn("ROCm compute is unavailable", result)

    async def test_present_kfd_does_not_clear_the_driver(self):
        """/dev/kfd surviving a failed bind is the whole point of this wording.

        It used to assert "driver is up". Measured 2026-09-17 on 601418522:
        /dev/kfd existed, amdgpu was in lsmod, and rocminfo listed only the
        CPU because the probe had died inside amdgpu_pci_probe. The node is
        created before that happens, so it proves nothing.
        """
        result, _ = await self._run([(0, "", "something odd"), (0, "ERROR: no devices", ""), (0, "kfd:present\n", "")])
        self.assertIn("weaker evidence", result)
        self.assertIn("hardware_scan", result)
        self.assertNotIn("the driver is up and", result)

    async def test_healthy_json_still_reports_success(self):
        raw = '{"card0": {"Card Series": "MI300X", "GPU use (%)": "0", "GPU Memory Use (%)": "1"}}'
        result, calls = await self._run([(0, raw, "")])
        self.assertTrue(result.startswith("✅"), result)
        self.assertIn("1 GPU(s)", result)
        self.assertEqual(len(calls), 1, "a healthy rocm-smi must not trigger the amd-smi fallback")

    async def test_amd_smi_output_containing_error_is_not_success(self):
        result, _ = await self._run(
            [(0, "not json", ""), (0, "ERROR: Unable to detect any GPU devices", ""), (0, "kfd:absent\n", "")]
        )
        self.assertTrue(result.startswith("❌"), result)


class HardwareScanTests(unittest.IsolatedAsyncioTestCase):
    """Counting GPUs is code's job, and the obvious match double-counts."""

    SCAN = (
        "<<<host>>>\nbox\n6.12\nDebian 13\nup 3 minutes\nXeon\n20\n236 GB\n679G\n"
        "<<<kfd>>>\npresent\nloaded\n-\n"
        "<<<pci>>>\n83:00.0 Instinct MI300X VF\n"
        "<<<agents>>>\n  Name:   INTEL XEON\n  Marketing Name:  INTEL XEON\n"
        "  Name:   gfx942\n  Marketing Name:  AMD Instinct MI300X VF\n"
        "  Name:   amdgcn-amd-amdhsa--gfx942:sramecc+:xnack-\n"
        "<<<vram>>>\ncard0,205822885888,299687936\n"
        "<<<firmware>>>\nGPU[0] : VBIOS version: 113-M3000108-103\n"
        "<<<tools>>>\nrocm-smi\t/usr/bin/rocm-smi\ndocker\t-\nhipcc\t-\n"
        "<<<versions>>>\nPython 3.13.5\n"
        "<<<packages>>>\nrocm-smi\t6.1.2-1\n"
        "<<<python>>>\nModuleNotFoundError: No module named 'torch'\n"
    )

    async def _scan(self, raw):
        async def fake_run_command(cmd, timeout=120):
            return (0, raw, "")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.hardware_scan("mi300-1")

    async def test_one_gpu_is_counted_once(self):
        """rocminfo's ISA line reuses the `Name:` key and must not count as an agent."""
        result = await self._scan(self.SCAN)
        self.assertIn("1 GPU agent(s)", result)
        self.assertNotIn("2 GPU agent(s)", result)
        self.assertIn("`gfx942`", result)
        self.assertNotIn("`amdgcn-amd-amdhsa", result)

    async def test_missing_tools_are_counted_and_named(self):
        result = await self._scan(self.SCAN)
        self.assertIn("missing (2)", result)
        self.assertIn("`docker`", result)

    async def test_absent_kfd_is_flagged_as_fatal(self):
        result = await self._scan(self.SCAN.replace("<<<kfd>>>\npresent", "<<<kfd>>>\nABSENT"))
        self.assertIn("ROCm cannot work", result)

    async def test_empty_output_is_an_error_not_an_empty_report(self):
        result = await self._scan("")
        self.assertTrue(result.startswith("❌"), result)


class CostLineTests(unittest.TestCase):
    """The hourly-to-monthly multiplication happens in code, not in the reader."""

    def test_hourly_is_converted(self):
        line = server._cost_line(1.99)
        self.assertIn("$1.990/hour", line)
        self.assertIn("$48/day", line)
        self.assertIn("$1,433", line)


class SSHKeyResolutionTests(unittest.IsolatedAsyncioTestCase):
    """A droplet created with no key costs money and cannot be reached."""

    KEYS = [
        {"id": 59210687, "name": "amd", "fingerprint": "92:17:77:ae"},
        {"id": 11111111, "name": "laptop", "fingerprint": "aa:bb:cc:dd"},
    ]

    def _keys(self, keys=None):
        return patch.object(server, "_paged", AsyncMock(return_value=self.KEYS if keys is None else keys))

    async def test_by_name_id_and_fingerprint(self):
        with self._keys():
            for wanted in ("amd", "59210687", "92:17:77:ae"):
                ids, _ = await server._resolve_ssh_keys(wanted)
                self.assertEqual(ids, [59210687], wanted)

    async def test_naming_nothing_means_every_key_not_none(self):
        """An empty setting must not silently produce a password-only droplet."""
        with self._keys():
            ids, labels = await server._resolve_ssh_keys("")
            self.assertEqual(ids, [59210687, 11111111])
            self.assertEqual(len(labels), 2)

    async def test_unknown_key_names_what_is_available(self):
        with self._keys():
            with self.assertRaises(RuntimeError) as ctx:
                await server._resolve_ssh_keys("typo")
        self.assertIn("`amd`", str(ctx.exception))

    async def test_account_with_no_keys_is_refused(self):
        with self._keys([]):
            with self.assertRaises(RuntimeError) as ctx:
                await server._resolve_ssh_keys("")
        self.assertIn("root password by email", str(ctx.exception))


class CreateDropletTests(unittest.IsolatedAsyncioTestCase):
    """Creating starts a meter that powering off does not stop, so it is two-step."""

    SIZES = [
        {"slug": "gpu-mi300x1-192gb", "price_hourly": 2.59, "price_monthly": 1891.0, "regions": ["tor1"]},
        {"slug": "gpu-mi325x1-256gb", "price_hourly": 3.8, "price_monthly": 2774.0, "regions": ["nyc2"]},
    ]
    REGIONS = [{"slug": "atl1", "available": True, "sizes": ["gpu-mi325x1-256gb", "s-1vcpu-1gb"]}]
    KEYS = [{"id": 59210687, "name": "amd", "fingerprint": "92:17:77:ae"}]

    def _api_fixtures(self, droplets=(), posts=None):
        """Patch the three list endpoints and record every POST."""

        async def fake_paged(path, key):
            if path.startswith("/sizes"):
                return list(self.SIZES)
            if path.startswith("/regions"):
                return list(self.REGIONS)
            if path.startswith("/account/keys"):
                return list(self.KEYS)
            raise AssertionError(f"unexpected list endpoint: {path}")

        async def fake_api(method, path, payload=None, timeout=30):
            posts.append((method, path, payload))
            return {"droplet": {"id": 601142019, "name": payload["name"], "status": "new"}}

        return (
            patch.object(server, "_paged", fake_paged),
            patch.object(server, "_droplets", AsyncMock(return_value=list(droplets))),
            patch.object(server, "_api", fake_api),
        )

    async def _create(self, droplets=(), **kwargs):
        posts: list = []
        paged, drops, api = self._api_fixtures(droplets, posts)
        with paged, drops, api:
            result = await server.create_droplet(**kwargs)
        return result, posts

    async def test_preflight_orders_nothing(self):
        """The default call must not reach POST /droplets at all."""
        result, posts = await self._create(name="mi300-new")
        self.assertEqual(posts, [], "the preflight placed an order")
        self.assertIn("nothing has been ordered", result.lower())
        self.assertIn("confirm=true", result)

    async def test_preflight_prices_a_listed_size(self):
        result, _ = await self._create(name="mi300-new", size="gpu-mi300x1-192gb")
        self.assertIn("$2.590/hour", result)
        self.assertIn("/day", result)

    async def test_unlisted_devcloud_slug_is_reported_not_refused(self):
        """gpu-mi300x1-192gb-devcloud is absent from GET /v2/sizes on purpose."""
        result, posts = await self._create(name="mi300-new", confirm=True)
        self.assertIn("not in `GET /v2/sizes`", result)
        self.assertTrue(result.startswith("✅"), result)
        self.assertEqual(len(posts), 1)

    async def test_confirm_posts_and_forces_the_tag(self):
        result, posts = await self._create(name="mi300-new", confirm=True)
        method, path, payload = posts[0]
        self.assertEqual((method, path), ("POST", "/droplets"))
        self.assertEqual(payload["tags"], [server.DROPLET_TAG])
        self.assertEqual(payload["ssh_keys"], [59210687])
        self.assertEqual(payload["size"], server.DROPLET_SIZE)
        self.assertIn("601142019", result)

    async def test_created_droplet_is_told_to_reboot_once(self):
        """A fresh MI300 droplet has no /dev/kfd until it is rebooted."""
        result, _ = await self._create(name="mi300-new", confirm=True)
        self.assertIn("/dev/kfd", result)
        self.assertIn("reboot_droplet", result)

    async def test_an_existing_tagged_droplet_blocks_a_second(self):
        result, posts = await self._create(droplets=[ACTIVE], name="mi300-new", confirm=True)
        self.assertTrue(result.startswith("❌"), result)
        self.assertEqual(posts, [], "a second droplet was ordered while one already exists")
        self.assertIn("allow_duplicate", result)

    async def test_allow_duplicate_is_the_way_past_it(self):
        result, posts = await self._create(droplets=[ACTIVE], name="mi300-new", confirm=True, allow_duplicate=True)
        self.assertEqual(len(posts), 1)
        self.assertTrue(result.startswith("✅"), result)

    async def test_reusing_an_existing_name_is_refused(self):
        result, posts = await self._create(droplets=[ACTIVE], name="mi300-1", confirm=True, allow_duplicate=True)
        self.assertTrue(result.startswith("❌"), result)
        self.assertEqual(posts, [])
        self.assertIn("start_droplet", result)

    async def test_unusable_names_are_rejected_before_the_api_sees_them(self):
        for bad in ("my droplet", "-leading", "trailing-", "under_score", ""):
            result, posts = await self._create(name=bad, confirm=True)
            self.assertTrue(result.startswith("❌"), f"{bad!r} was accepted")
            self.assertEqual(posts, [], f"{bad!r} reached the API")

    async def test_region_not_offering_the_size_is_flagged(self):
        result, _ = await self._create(name="mi300-new", size="gpu-mi300x1-192gb", region="atl1")
        self.assertIn("⚠️", result)
        self.assertIn("tor1", result)

    async def test_api_refusal_is_returned_not_raised(self):
        async def boom(method, path, payload=None, timeout=30):
            raise RuntimeError("DigitalOcean 422: size is not available in this region")

        paged, drops, _ = self._api_fixtures([], [])
        with paged, drops, patch.object(server, "_api", boom):
            result = await server.create_droplet("mi300-new", confirm=True)
        self.assertTrue(result.startswith("❌"), result)
        self.assertIn("422", result)


class NoDestroyToolTests(unittest.TestCase):
    """Create was added deliberately; destroy stays out, also deliberately."""

    def test_nothing_deletes_a_droplet(self):
        source = (PROJECT_DIR / "server.py").read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            self.assertNotIn('_api("DELETE"', stripped, f"a destroy path appeared in server.py: {stripped[:120]}")
            self.assertNotIn('"destroy"', stripped, f"a destroy action appeared in server.py: {stripped[:120]}")


class SSHTransportErrorTests(unittest.TestCase):
    """ssh failing to connect is not a statement about the GPU.

    Measured 2026-09-17 on droplet 601418522, seconds after it was created:
    gpu_status reported "`/dev/kfd` exists, so the driver is up" about a box
    that was refusing connections on port 22. The droplet was `active` in the
    API, none of the probes had run, and "not absent" was being read as
    "present" — a confident diagnosis of a machine nothing had spoken to.
    """

    def test_connection_refused_is_recognised(self):
        err = "ssh: connect to host 129.212.178.87 port 22: Connection refused"
        self.assertEqual(server._ssh_transport_error(255, err), err)

    def test_timeout_is_recognised(self):
        self.assertIn("timed out", server._ssh_transport_error(124, "timed out after 90s"))

    def test_a_remote_command_failing_is_not_a_transport_error(self):
        """rocm-smi exiting non-zero must still reach the GPU diagnosis."""
        self.assertIsNone(server._ssh_transport_error(127, "bash: line 1: rocm-smi: command not found"))
        self.assertIsNone(server._ssh_transport_error(0, ""))
        self.assertIsNone(server._ssh_transport_error(1, "Driver not initialized"))


class GPUStatusUnreachableTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, responses):
        async def fake_run_command(cmd, timeout=120):
            return responses.pop(0)

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.gpu_status("mi300-1")

    async def test_refused_ssh_does_not_become_a_gpu_diagnosis(self):
        result = await self._run([(255, "", "ssh: connect to host 203.0.113.7 port 22: Connection refused")])
        self.assertTrue(result.startswith("❌"), result)
        self.assertIn("Cannot reach", result)
        self.assertIn("says nothing about the GPU", result)
        self.assertNotIn("driver is up", result)

    async def test_a_silent_kfd_probe_is_unknown_not_present(self):
        """Neither marker came back, so the driver state is not known."""
        result = await self._run([(0, "not json", ""), (0, "ERROR: no devices", ""), (0, "", "")])
        self.assertIn("neither answer", result)
        self.assertNotIn("driver is up", result)

    async def test_absent_kfd_names_the_reboot_fix(self):
        result = await self._run([(0, "not json", ""), (0, "ERROR: no devices", ""), (0, "kfd:absent\n", "")])
        self.assertIn("reboot_droplet", result)


class ScanPackageFilterTests(unittest.IsolatedAsyncioTestCase):
    """`whiptail` contains "hip"; it is not a ROCm package."""

    async def test_whiptail_is_not_a_rocm_package(self):
        scan = HardwareScanTests.SCAN.replace("<<<packages>>>\nrocm-smi\t6.1.2-1\n", "<<<packages>>>\n")

        async def fake_run_command(cmd, timeout=120):
            return (0, scan, "")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                result = await server.hardware_scan("mi300-1")
        self.assertIn("ROCm packages (0)", result)

    def test_the_awk_filter_matches_names_not_the_whole_line(self):
        source = (PROJECT_DIR / "server.py").read_text()
        self.assertIn("$2 ~ /amdgpu|rocm", source, "the package filter must match the name field")
        self.assertNotIn("awk '/amdgpu|rocm|hsa|hip/", source, "a bare `hip` substring matches whiptail")


class BindFailureTests(unittest.IsolatedAsyncioTestCase):
    """A card on the PCI bus with no gfx agent did not bind. Say so, don't omit it."""

    NO_AGENTS = (
        "<<<host>>>\nbox\n6.12\nDebian 13\nup 3 minutes\nXeon\n20\n236 GB\n679G\n"
        "<<<kfd>>>\npresent\nloaded\n-\n"
        "<<<pci>>>\n83:00.0 Processing accelerators [1200]: Advanced Micro Devices, Inc. Instinct MI300X VF\n"
        "<<<agents>>>\n  Name:   INTEL XEON\n  Marketing Name:  INTEL XEON\n"
        "<<<vram>>>\n-\n<<<firmware>>>\n-\n<<<tools>>>\nrocm-smi\t/usr/bin/rocm-smi\n"
        "<<<versions>>>\nPython 3.13.5\n<<<packages>>>\n<<<python>>>\n-\n"
    )

    async def _scan(self, raw):
        async def fake_run_command(cmd, timeout=120):
            return (0, raw, "")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.hardware_scan("mi300-1")

    async def test_card_present_with_no_agent_is_named_a_bind_failure(self):
        result = await self._scan(self.NO_AGENTS)
        self.assertIn("did not bind", result)
        self.assertIn("reboot_droplet", result)

    async def test_kfd_present_does_not_suppress_the_warning(self):
        """The whole trap: /dev/kfd says present on a card that never bound."""
        result = await self._scan(self.NO_AGENTS)
        self.assertIn("`/dev/kfd`: **present**", result)
        self.assertIn("did not bind", result)

    async def test_a_working_card_is_not_accused_of_not_binding(self):
        result = await self._scan(HardwareScanTests.SCAN)
        self.assertNotIn("did not bind", result)
        self.assertIn("1 GPU agent(s)", result)

    async def test_no_card_on_the_bus_is_not_a_bind_failure_either(self):
        no_card = self.NO_AGENTS.replace(
            "83:00.0 Processing accelerators [1200]: Advanced Micro Devices, Inc. Instinct MI300X VF",
            "00:01.0 VGA compatible controller [0300]: Red Hat, Inc. Virtio 1.0 GPU",
        )
        result = await self._scan(no_card)
        self.assertNotIn("did not bind", result)


class PrepareDropletTests(unittest.IsolatedAsyncioTestCase):
    """The sources file is deb822 behind a mirror indirection; edit, never append."""

    PREPARED = (
        "<<<before>>>\nSuites: trixie trixie-updates trixie-backports\nComponents: main\n"
        "<<<sources>>>\nSuites: trixie trixie-updates trixie-backports\n"
        "Components: main contrib non-free non-free-firmware\n"
        "<<<update>>>\nexit:0\n"
        "<<<install>>>\nSetting up rocminfo (6.1.2-2) ...\nexit:0\n"
        "<<<docker>>>\nservice:active\nDocker version 26.1.5+dfsg1, build a72d7cd\n"
        "<<<gids>>>\nvideo=44\nrender=991\n"
        "<<<rocm>>>\n1\n"
        "<<<disk>>>\n678G\n"
    )

    async def _prepare(self, raw=None, pull="STARTED pid=7088", **kwargs):
        calls = []

        async def fake_run_command(cmd, timeout=120):
            calls.append(cmd[-1])
            if "docker pull" in cmd[-1]:
                return (0, pull, "")
            return (0, self.PREPARED if raw is None else raw, "")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.prepare_droplet("mi300-1", **kwargs), calls

    async def test_it_reports_that_backports_was_already_on(self):
        """The obvious assumption — that backports needed enabling — was wrong."""
        result, _ = await self._prepare()
        self.assertIn("already enabled", result)
        self.assertIn("Changed: components", result)

    async def test_the_remote_script_edits_in_place_and_never_appends(self):
        """A hand-written `deb` line would bypass the DigitalOcean mirror.

        Comments are stripped before asserting. This check has now been fooled
        twice by prose quoting the very line it warns against — first in
        server.py, then in the script that replaced it — which is the same
        substring-matching mistake as `whiptail` under "ROCm packages". Assert
        against what runs, not against what the file says.
        """
        script = server._read_script("remote-prepare.sh")
        code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn("sed -i", code)
        self.assertNotIn(">> /etc/apt/sources.list", code)
        self.assertNotIn("deb http://deb.debian.org", code)
        self.assertNotIn("tee /etc/apt/sources.list", code)

    async def test_it_backs_the_sources_file_up_once(self):
        source = (PROJECT_DIR / "server.py").read_text()
        self.assertIn(".amd-gputools.bak", source)

    async def test_gpu_gids_are_reported_for_the_container_flags(self):
        result, _ = await self._prepare()
        self.assertIn("`video:44`", result)
        self.assertIn("`render:991`", result)

    async def test_the_pull_is_detached(self):
        """A foreground pull of a 35-62 GB image outlives any call timeout."""
        source = (PROJECT_DIR / "server.py").read_text()
        self.assertIn("setsid nohup docker pull", source)
        self.assertIn("</dev/null", source)

    async def test_pull_image_false_skips_the_pull(self):
        result, calls = await self._prepare(pull_image=False)
        self.assertNotIn("docker pull", " ".join(calls))
        self.assertIn("Not pulled", result)

    async def test_an_already_present_image_is_not_repulled(self):
        result, _ = await self._prepare(pull="ALREADY_PRESENT")
        self.assertIn("already on the droplet", result)

    async def test_a_running_pull_is_not_duplicated(self):
        result, _ = await self._prepare(pull="ALREADY_RUNNING")
        self.assertIn("already running", result)

    async def test_missing_docker_is_reported_not_ignored(self):
        result, _ = await self._prepare(pull="NO_DOCKER")
        self.assertIn("docker is not installed", result)

    async def test_an_unreachable_droplet_does_not_produce_a_setup_report(self):
        async def fake_run_command(cmd, timeout=120):
            return (255, "", "ssh: connect to host 203.0.113.7 port 22: Connection refused")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                result = await server.prepare_droplet("mi300-1")
        self.assertTrue(result.startswith("❌"), result)
        self.assertIn("Connection refused", result)


class VLLMImageStatusTests(unittest.IsolatedAsyncioTestCase):
    async def _status(self, image_line, running="no", **kwargs):
        raw = f"<<<image>>>\n{image_line}\n<<<running>>>\n{running}\n<<<log>>>\nef4cb6142c43: Download complete\n"

        async def fake_run_command(cmd, timeout=120):
            return (0, raw, "")

        with patch.object(server, "_resolve", AsyncMock(return_value=ACTIVE)):
            with patch.object(server, "run_command", fake_run_command):
                return await server.vllm_image_status("mi300-1", **kwargs)

    async def test_absent_and_pulling_is_progress_not_failure(self):
        result = await self._status("absent", running="yes")
        self.assertTrue(result.startswith("📡"), result)
        self.assertIn("still pulling", result)

    async def test_absent_and_idle_is_a_failure(self):
        result = await self._status("absent", running="no")
        self.assertTrue(result.startswith("❌"), result)

    async def test_size_is_converted_here_not_left_in_bytes(self):
        result = await self._status("sha256:abc123 62000000000")
        self.assertIn("62.0 GB", result)
        self.assertNotIn("62000000000", result)


class SharedScaffoldScriptTests(unittest.TestCase):
    """The MCP tool and scaffold-droplet.sh must send the same remote script.

    They were two copies of the same shell for about an hour. One copy on disk,
    read by both, is the only version of this that stays true.
    """

    def test_the_remote_script_exists_and_is_executable(self):
        path = PROJECT_DIR / "scaffold" / "remote-prepare.sh"
        self.assertTrue(path.is_file(), "scaffold/remote-prepare.sh is missing")
        self.assertTrue(os.access(path, os.X_OK), "scaffold/remote-prepare.sh is not executable")

    def test_the_driver_pipes_the_same_file_rather_than_a_copy(self):
        driver = (PROJECT_DIR / "scaffold-droplet.sh").read_text()
        self.assertIn("scaffold/remote-prepare.sh", driver)
        # A second copy of the apt edit in the driver is the drift this prevents.
        self.assertNotIn("Components:", driver, "the driver has its own copy of the sources edit")

    def test_the_script_takes_its_config_from_the_environment(self):
        script = server._read_script("remote-prepare.sh")
        self.assertIn("${APT_COMPONENTS:-", script)
        self.assertIn("${SETUP_PACKAGES:-", script)
        # Placeholders would mean the shell driver could not run it directly.
        self.assertNotIn("__COMPONENTS__", script)
        self.assertNotIn("__PACKAGES__", script)

    def test_env_values_are_quoted_so_they_cannot_inject(self):
        composed = server._with_env("echo hi\n", APT_COMPONENTS="main; rm -rf /")
        self.assertIn("'main; rm -rf /'", composed)
        self.assertTrue(composed.endswith("echo hi\n"))

    def test_empty_values_are_omitted_not_passed_as_blank(self):
        """A blank assignment would override the script's own default with ''."""
        composed = server._with_env("body\n", APT_COMPONENTS="", SETUP_PACKAGES="git")
        self.assertNotIn("APT_COMPONENTS=", composed)
        self.assertIn("SETUP_PACKAGES=git", composed)

    def test_the_bind_check_is_rocminfo_not_dev_kfd(self):
        """/dev/kfd survives a failed bind, so it cannot be the gate."""
        for text in (
            server._read_script("remote-prepare.sh"),
            (PROJECT_DIR / "scaffold-droplet.sh").read_text(),
        ):
            self.assertIn("gfx[0-9a-f]", text, "the gfx-agent check is missing")
        driver = (PROJECT_DIR / "scaffold-droplet.sh").read_text()
        self.assertNotIn("test -e /dev/kfd", driver)


class RegistrationTests(unittest.TestCase):
    """The server key prefixes every tool name, so the four places must agree."""

    def test_mcp_json_matches_the_directory_name(self):
        import json

        config = json.loads((PROJECT_DIR / ".mcp.json").read_text())
        servers = config["mcpServers"]
        self.assertEqual(list(servers), [PROJECT_DIR.name])
        entry = servers[PROJECT_DIR.name]
        self.assertEqual(entry["command"], "python3", "no virtualenv path — the repo uses the system python3")
        self.assertEqual(entry["args"], ["server.py"])
        self.assertEqual(entry["env"]["MCP_SERVER_NAME"], PROJECT_DIR.name)


if __name__ == "__main__":
    unittest.main()
