"""Native DSH adapter: no nested Docker, no evaluator mounts or auth discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from provider_auth import first_provider_value, resolve_provider_api_key

RUNTIME = Path(__file__).with_name("dsh_runtime")


def parse_stream(text: str) -> tuple[list[dict], dict | None]:
    events, result = [], None
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "dsh.event" and isinstance(item.get("event"), dict):
            events.append(item["event"])
        elif item.get("type") == "dsh.result":
            result = item
    return events, result


def normalize(events: list[dict], prompt: str) -> tuple[list[dict], dict]:
    trace: list[dict] = [{"type": "text", "role": "user", "text": prompt}]
    calls: dict[str, dict] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    have_usage = False
    for event in events:
        kind, data = event.get("type"), event.get("data") or {}
        if not isinstance(data, dict):
            continue
        if kind == "assistant/message":
            message = data.get("message") or data
            content = message.get("content") or []
            text = "".join(
                str(x.get("text", ""))
                for x in content
                if isinstance(x, dict) and x.get("type") == "text"
            )
            trace.append({"type": "text", "role": "assistant", "text": text})
            raw = message.get("usage") or data.get("usage") or {}
            if raw:
                have_usage = True
                pt = (
                    raw.get(
                        "inputTokens",
                        raw.get("prompt_tokens", raw.get("input_tokens", 0)),
                    )
                    or 0
                )
                ct = (
                    raw.get(
                        "outputTokens",
                        raw.get("completion_tokens", raw.get("output_tokens", 0)),
                    )
                    or 0
                )
                usage["prompt_tokens"] += pt
                usage["completion_tokens"] += ct
                usage["total_tokens"] += (
                    raw.get("totalTokens", raw.get("total_tokens", pt + ct)) or pt + ct
                )
        elif kind in {"tool/call", "tool/result"}:
            message = data.get("message") or {}
            source = message.get("source") or {}
            call_id = str(
                data.get("toolCallId")
                or data.get("callId")
                or data.get("callID")
                or source.get("callId")
                or data.get("id")
                or ""
            )
            item = calls.get(call_id) if kind == "tool/result" else None
            if item is None:
                item = {
                    "type": "tool",
                    "role": "tool",
                    "callID": call_id,
                    "tool": data.get("toolName") or data.get("name") or "tool",
                    "input": data.get("input", data.get("arguments")),
                    "output": None,
                    "status": "started",
                }
                trace.append(item)
                calls[call_id] = item
            if kind == "tool/result":
                item["output"] = data.get(
                    "message", data.get("result", data.get("output"))
                )
                failed = (
                    data.get("isError")
                    or data.get("error")
                    or any(
                        x.get("isError")
                        for x in message.get("content", [])
                        if isinstance(x, dict)
                    )
                )
                item["status"] = "failed" if failed else "completed"
    return trace, usage if have_usage else {k: None for k in usage}


def run(
    *,
    prompt: str,
    work_dir: str,
    sandbox_dir: str,
    timeout_s: float,
    api_provider: dict[str, Any],
    agent_id: str | None = None,
) -> dict:
    started = time.monotonic()
    raw = Path(sandbox_dir).resolve() / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    out_path, err_path = raw / "dsh-events.jsonl", raw / "stderr.txt"
    # Retries must not inherit a previous run's apparent completion or trace.
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    status, error, key, process = "error", None, "", None
    result = None
    env = dict(os.environ)
    try:
        if not timeout_s or timeout_s <= 0:
            raise ValueError("DSH requires a positive wall-clock timeout")
        wire = api_provider.get("wireApi", "chat")
        if wire not in {"chat", "openai-chat", "chat_completions"}:
            raise ValueError("DSH requires wireApi=chat (DeepSeek Chat Completions)")
        if env.get("WORKSPACE_BENCH_MCP_URL"):
            raise ValueError("DSH minimal profile does not support workspace_env MCP")
        model = first_provider_value(api_provider.get("model"), env.get("DSH_MODEL"))
        base = first_provider_value(
            api_provider.get("baseUrl"),
            api_provider.get("base_url"),
            env.get("DEEPSEEK_BASE_URL"),
        )
        key = resolve_provider_api_key(api_provider, model=model) or env.get(
            "DEEPSEEK_API_KEY", ""
        )
        if not all((key, model, base)):
            raise ValueError("DSH requires explicit model, baseUrl and API credentials")
        env.update(
            DEEPSEEK_API_KEY=key,
            DEEPSEEK_BASE_URL=base,
            DSH_MODEL=model,
            DSH_REASONING_EFFORT=str(api_provider.get("reasoningEffort") or "high"),
            DSH_MAX_TOKENS=str(api_provider.get("maxOutputTokens") or 32768),
            DSH_CONTEXT_WINDOW=str(api_provider.get("dshContextTokens") or 1000000),
        )
        prompt_path = raw / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            sys.executable,
            str(RUNTIME / "runner.py"),
            "--prompt-file",
            str(prompt_path),
            "--workdir",
            str(Path(work_dir).resolve()),
            "--state-dir",
            str(raw),
            "--timeout",
            str(timeout_s),
        ]
        with (
            tempfile.TemporaryDirectory(prefix="dsh-state-", dir=raw) as state,
            out_path.open("w", encoding="utf-8") as stdout,
            err_path.open("w", encoding="utf-8") as stderr,
        ):
            command[command.index("--state-dir") + 1] = state
            process = subprocess.Popen(
                command,
                cwd=work_dir,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                rc = process.wait(timeout=timeout_s)
                status = "ok" if rc == 0 else "error"
            except subprocess.TimeoutExpired:
                status, error = (
                    "timeout",
                    f"DSH exceeded wall-clock limit of {timeout_s:g}s",
                )
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            finally:
                # Also reap descendants left behind on early SDK exit.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    # Scrub SDK stderr as well as structured stdout before returning/persisting.
    for path in (out_path, err_path):
        text = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        if key:
            text = text.replace(key, "<redacted>")
        path.write_text(text, encoding="utf-8")
    events, result = parse_stream(out_path.read_text(encoding="utf-8"))
    if status == "ok" and (
        not result
        or result.get("status") != "ok"
        or result.get("finishReason") != "completed"
    ):
        status = "error"
    if status == "error" and error is None:
        error = (
            (result or {}).get("error")
            or err_path.read_text(encoding="utf-8")[-2000:]
            or "Missing successful DSH completion"
        )
    if error and key:
        error = error.replace(key, "<redacted>")
    execution, usage = normalize(events, prompt)
    return {
        "status": status,
        "paths": [],
        "errorMessage": error,
        "trace": {
            "runner": "deepseekharness",
            "agentId": agent_id,
            "rawDir": str(raw),
            "lastText": (result or {}).get("lastText", ""),
            "executionTrace": execution,
            "usageTotal": usage,
            "finishReason": (result or {}).get("finishReason"),
            "sdkVersion": (result or {}).get("sdkVersion"),
        },
        "metrics": {
            "turns": sum(x.get("role") == "assistant" for x in execution),
            "promptTokens": usage["prompt_tokens"],
            "completionTokens": usage["completion_tokens"],
            "totalTokens": usage["total_tokens"],
        },
        "durationMs": int((time.monotonic() - started) * 1000),
    }
