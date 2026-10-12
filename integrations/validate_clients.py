"""Check native plugin installation in disposable profiles; never call a model.

Usage: python integrations/validate_clients.py --codex codex --claude /path/to/claude
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def codex_tools(codex: str, env: dict[str, str]) -> dict:
    """List live plugin tools through app-server; no thread/model is started."""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([codex, "app-server"], env=env, cwd=REPO,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        pending = b""
        try:
            def request(number, method, params):
                nonlocal pending
                process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
                process.stdin.flush()
                deadline = time.monotonic() + 45
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while time.monotonic() < deadline:
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            reply = json.loads(line)
                            if reply.get("id") == number:
                                if "error" in reply:
                                    raise RuntimeError(str(reply["error"]))
                                return reply["result"]
                        if selector.select(timeout=max(0, deadline - time.monotonic())):
                            block = os.read(process.stdout.fileno(), 65536)
                            if not block:
                                raise RuntimeError("app-server exited before tool discovery")
                            pending += block
                raise TimeoutError("app-server MCP discovery timed out")

            request(1, "initialize", {"clientInfo": {"name": "env-rethink-check", "version": "0.1.0"}})
            result = request(2, "mcpServerStatus/list", {})
            servers = [entry for entry in result["data"] if "workspace_env" in entry["name"]]
            if not servers or len(servers[0].get("tools", {})) != 3:
                errors.seek(0)
                detail = errors.read().decode(errors="replace")[-4000:]
                raise RuntimeError(f"Plugin MCP tools not ready: {servers}\n{detail}")
            return {"server": servers[0]["name"], "tools": sorted(servers[0]["tools"])}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()


def validate(codex: str | None, claude: str | None, bundle: str | None = None) -> dict:
    checks = []
    with tempfile.TemporaryDirectory(prefix="env-rethink-client-check-") as temporary:
        profile = Path(temporary)
        # These are the clients' intended profile selectors, not shell HOME
        # replacements. The server owner's real config/auth are untouched.
        env = dict(os.environ, CODEX_HOME=str(profile / "codex"),
                   CLAUDE_CONFIG_DIR=str(profile / "claude"),
                   DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1")
        if bundle:
            env["ENV_RETHINK_BUNDLE"] = str(Path(bundle).resolve(strict=True))
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        (profile / "codex").mkdir()
        (profile / "claude").mkdir()

        def run(command):
            result = subprocess.run(command, env=env, cwd=REPO, text=True,
                                    capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError(f"{command[:3]} failed: {result.stderr or result.stdout}")
            checks.append({"command": command, "stdout": result.stdout.strip()})
            return result.stdout

        if codex:
            run([codex, "--version"])
            run([codex, "plugin", "marketplace", "add", str(REPO), "--json"])
            run([codex, "plugin", "add", "env-rethink@env-rethink-local", "--json"])
            run([codex, "plugin", "list", "--json"])
            if bundle:
                checks.append({"codex_live_mcp": codex_tools(codex, env)})
        if claude:
            run([claude, "--version"])
            run([claude, "plugin", "validate", str(REPO / "plugins/env-rethink")])
            run([claude, "plugin", "validate", str(REPO)])
            run([claude, "plugin", "marketplace", "add", str(REPO)])
            run([claude, "plugin", "install", "env-rethink@env-rethink-local", "--scope", "user"])
            run([claude, "plugin", "list", "--json"])
            if bundle:
                status = run([claude, "mcp", "list"])
                if "workspace_env" not in status or "Connected" not in status:
                    raise RuntimeError(f"Claude plugin MCP not connected: {status}")
    return {"status": "passed", "model_calls": 0, "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex")
    parser.add_argument("--claude")
    parser.add_argument("--bundle", help="Also check Codex's live plugin MCP discovery without calling a model")
    args = parser.parse_args()
    if not args.codex and not args.claude:
        parser.error("Select at least one client")
    print(json.dumps(validate(args.codex, args.claude, args.bundle), ensure_ascii=False, indent=2))
