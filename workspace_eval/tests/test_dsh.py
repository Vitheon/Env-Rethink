"""DSH contract tests. Opt-in real SDK smoke uses ONLY a localhost fake API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "workspace_eval/src"))
sys.path.insert(0, str(ROOT / "workspace_eval/scripts"))
from agents import deepseekharness as adapter
from agentkit import (
    DeepSeekHarness,
    agent_protocol,
    build_harness,
    canonical_agent,
    resolve_gateway,
)
from agentkit import DockerRuntime
import run_experiment


class DSHContracts(unittest.TestCase):
    def test_aliases_and_unknown_agents(self):
        for alias in ("dsh", "DeepSeekHarness", "deepseek-harness", "deepseek_harness"):
            self.assertEqual(canonical_agent(alias), "dsh")
            self.assertEqual(agent_protocol(alias), "openai-chat")
            self.assertEqual(
                run_experiment._agent_harness({"agent": {"harness": alias}}),
                "DeepSeekHarness",
            )
        with self.assertRaises(ValueError):
            canonical_agent("typo")

    def test_shared_harness_command_and_failure(self):
        gateway = resolve_gateway(
            base_url="https://example.invalid/v1",
            api_key="secret-fixture",
            model="test",
            protocol="openai-chat",
        )
        harness = build_harness("dsh", gateway=gateway, workdir="/work/中文 space")
        self.assertIsInstance(harness, DeepSeekHarness)
        self.assertIn("'/work/中文 space'", harness.command("/prompt"))
        self.assertNotIn("secret-fixture", harness.command("/prompt"))
        event = json.dumps(
            {"type": "dsh.result", "status": "ok", "finishReason": "completed"}
        )
        for rc, expected in ((0, "ok"), (1, "error")):
            self.assertEqual(
                harness.parse(
                    runtime_out=event, rc=rc, duration=1, log_path="test"
                ).status,
                expected,
            )
        self.assertEqual(
            harness.parse(runtime_out="", rc=0, duration=1, log_path="test").status,
            "error",
        )

    def test_bad_config_and_mcp_are_errors_not_empty_success(self):
        with tempfile.TemporaryDirectory() as root:
            for provider, extra in (
                ({}, {}),
                ({"wireApi": "responses"}, {}),
                (
                    {"wireApi": "chat"},
                    {"WORKSPACE_BENCH_MCP_URL": "http://localhost:9"},
                ),
            ):
                with (
                    patch.dict(os.environ, extra),
                    patch.object(adapter.subprocess, "Popen") as popen,
                ):
                    result = adapter.run(
                        prompt="test",
                        work_dir=root,
                        sandbox_dir=root,
                        timeout_s=1,
                        api_provider=provider,
                    )
                self.assertEqual(result["status"], "error")
                popen.assert_not_called()

    def test_stream_and_usage_missing(self):
        events, result = adapter.parse_stream(
            "log line\n[]\n" + json.dumps({"type": "dsh.result", "status": "error"})
        )
        self.assertEqual(events, [])
        self.assertEqual(result["status"], "error")
        _, usage = adapter.normalize([], "hi")
        self.assertIsNone(usage["total_tokens"])

    def test_config_carries_context_window(self):
        model = run_experiment._model_config(
            {"model": "deepseek-v4-pro", "dsh_context_tokens": 65536},
            default_effort="high",
        )
        self.assertEqual(run_experiment._api_provider(model)["dshContextTokens"], 65536)


@unittest.skipUnless(
    os.environ.get("DSH_TEST_RUNTIME") == "1",
    "set DSH_TEST_RUNTIME=1 (real SDK, local fake API only)",
)
class DSHRuntimeSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="env-rethink-dsh-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work 中文"
        self.work.mkdir()
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(delta, finish=None, usage=None):
                    event = {"choices": [{"delta": delta, "finish_reason": finish}]}
                    if usage:
                        event["usage"] = usage
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()

                try:
                    send(
                        {"role": "assistant", "content": None, "reasoning_content": ""}
                    )
                    if len(owner.requests) == 1:
                        command = "mkdir -p model_output && printf smoke-ok > model_output/smoke.txt"
                        if getattr(owner, "hang", False):
                            command = "sleep 8; touch late-write"
                        send(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-smoke",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": json.dumps(
                                                {"command": command}
                                            ),
                                        },
                                    }
                                ]
                            }
                        )
                        send(
                            {"content": ""},
                            "tool_calls",
                            {
                                "prompt_tokens": 7,
                                "completion_tokens": 9,
                                "total_tokens": 16,
                            },
                        )
                    else:
                        send({"content": "DSH smoke passed. local-test-secret"})
                        send(
                            {"content": ""},
                            "stop",
                            {
                                "prompt_tokens": 11,
                                "completion_tokens": 5,
                                "total_tokens": 16,
                            },
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_adapter(self, timeout=30):
        return adapter.run(
            prompt="Write the requested file.",
            work_dir=str(self.work),
            sandbox_dir=str(self.root / "case"),
            timeout_s=timeout,
            api_provider={
                "model": "deepseek-fixture",
                "wireApi": "chat",
                "baseUrl": f"http://127.0.0.1:{self.server.server_port}/v1",
                "apiKey": "local-test-secret",
                "reasoningEffort": "high",
            },
        )

    def test_real_sdk_tool_roundtrip_and_secret_redaction(self):
        result = self.run_adapter()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((self.work / "model_output/smoke.txt").read_text(), "smoke-ok")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][0], "/v1/chat/completions")
        self.assertEqual(result["metrics"]["totalTokens"], 32, result)
        tools = [x for x in result["trace"]["executionTrace"] if x["type"] == "tool"]
        self.assertEqual(len(tools), 1, tools)
        self.assertEqual(tools[0]["tool"], "bash")
        self.assertEqual(tools[0]["status"], "completed")
        self.assertNotIn("local-test-secret", json.dumps(result))
        for path in (self.root / "case/raw").rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    "local-test-secret", path.read_text(errors="replace"), str(path)
                )

    def test_real_sdk_hung_tool_is_wall_timeout(self):
        self.hang = True
        result = self.run_adapter(timeout=6)
        self.assertEqual(result["status"], "timeout", result)
        self.assertGreaterEqual(len(self.requests), 1)
        self.assertLess(result["durationMs"], 15000)
        time.sleep(5)
        self.assertFalse(
            (self.work / "late-write").exists(), "orphaned tool outlived the timeout"
        )
        self.assertFalse(list((self.root / "case/raw").glob("dsh-state-*")))


@unittest.skipUnless(
    os.environ.get("DSH_TEST_IMAGE"),
    "set DSH_TEST_IMAGE to an existing SDK-enabled agentkit image",
)
class DSHDockerSmoke(unittest.TestCase):
    setUp = DSHRuntimeSmoke.setUp

    def test_shared_harness_in_real_container(self):
        async def check():
            runtime = await DockerRuntime.start(
                image=os.environ["DSH_TEST_IMAGE"],
                name="env-rethink-dsh-" + uuid.uuid4().hex[:12],
                workdir="/work",
                network="host",
            )
            try:
                await runtime.mkdirs("/opt/tb-agent")
                await runtime.put_text(
                    "/opt/tb-agent/tb_agent_env.sh",
                    (ROOT / "agentkit/tb_agent_env.sh").read_text(),
                )
                gateway = resolve_gateway(
                    protocol="openai-chat",
                    api_key="local-test-secret",
                    base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
                    model="deepseek-fixture",
                    reasoning_effort="high",
                )
                result = await build_harness(
                    "dsh", gateway=gateway, workdir="/work", timeout=30
                ).run(runtime, "Write the requested file.")
                self.assertEqual(result.status, "ok", result)
                self.assertNotIn("local-test-secret", result.last_message)
                output = await runtime.run_command(
                    "cat /work/model_output/smoke.txt", timeout=10
                )
                self.assertEqual(output.stdout, "smoke-ok")
                # Exercise the native Workspace adapter in the same Python
                # 3.11 image too, without mounting the host checkout/metadata.
                self.requests.clear()
                source = ROOT / "workspace_eval/src"
                for relative in (
                    "provider_auth.py",
                    "agents/deepseekharness.py",
                    "agents/dsh_runtime/runner.py",
                    "agents/dsh_runtime/minimal.cordis.yml",
                ):
                    await runtime.put_text(
                        "/opt/native-check/" + relative,
                        (source / relative).read_text(encoding="utf-8"),
                    )
                await runtime.mkdirs("/work-native")
                config = dict(
                    prompt="Write the requested file.",
                    work_dir="/work-native",
                    sandbox_dir="/tmp/native-case",
                    timeout_s=30,
                    api_provider={
                        "wireApi": "chat",
                        "model": gateway.model,
                        "baseUrl": gateway.base_url,
                        "apiKey": gateway.api_key,
                    },
                )
                await runtime.put_text(
                    "/opt/native-check/check.py",
                    "import json\nfrom agents.deepseekharness import run\n"
                    + f"result = run(**{config!r})\n"
                    + "assert result['status'] == 'ok', result\n"
                    + "assert result['metrics']['totalTokens'] == 32, result\n"
                    + "print('NATIVE_DSH_PASSED')\n",
                )
                native = await runtime.run_command(
                    "PYTHONPATH=/opt/tb-agent/dsh:/opt/native-check python3 /opt/native-check/check.py",
                    timeout=40,
                )
                self.assertEqual(native.return_code, 0, native.stdout + native.stderr)
                self.assertIn("NATIVE_DSH_PASSED", native.stdout)
                native_output = await runtime.run_command(
                    "cat /work-native/model_output/smoke.txt",
                    timeout=10,
                )
                self.assertEqual(native_output.stdout, "smoke-ok")
            finally:
                await runtime.stop()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
