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

    async def test_every_tool_catches(self):
        source = (PROJECT_DIR / "server.py").read_text()
        decorated = source.count("@mcp.tool(")
        caught = source.count("except Exception as exc:")
        # get_help has no external call and needs no handler; everything else does.
        self.assertEqual(caught, decorated - 1, "a tool is missing its `except Exception` guard")


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

    async def test_present_kfd_points_at_the_tooling_instead(self):
        result, _ = await self._run([(0, "", "something odd"), (0, "ERROR: no devices", ""), (0, "kfd:present\n", "")])
        self.assertIn("driver is up", result)

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
