"""Pinned, noninteractive DSH worker. Runs INSIDE the caller's task container.

The parent enforces a process-group wall deadline (SDK rc7's notification wait
is not covered by request_timeout_seconds). stdout is a redacted JSONL stream.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import tempfile

SDK_VERSION = "0.1.0rc7"


def emit(value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        # Replace in the encoded representation to preserve valid JSON.
        text = text.replace(json.dumps(key, ensure_ascii=False)[1:-1], "<redacted>")
    print(text, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()

    def terminate(_signum, _frame):
        # Unwind the SDK context to shut down its PTY/tool subprocesses too.
        raise TimeoutError("DSH worker terminated by wall-clock supervisor")

    signal.signal(signal.SIGTERM, terminate)
    try:
        installed = importlib.metadata.version("deepseek-harness-sdk")
        if installed != SDK_VERSION:
            raise ValueError(f"DSH SDK must be {SDK_VERSION}, found {installed}")
        runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
        if runtime_version != SDK_VERSION:
            raise ValueError(
                f"DSH runtime must be {SDK_VERSION}, found {runtime_version}"
            )
        if os.environ.get("WORKSPACE_BENCH_MCP_URL"):
            raise ValueError(
                "DSH minimal profile does not support workspace_env MCP; refusing to silently ignore it"
            )
        for name in ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "DSH_MODEL"):
            if not os.environ.get(name):
                raise ValueError(f"Missing {name}")
        effort = os.environ.get("DSH_REASONING_EFFORT") or "high"
        if effort not in {"off", "low", "high", "max"}:
            raise ValueError(f"Unsupported DSH reasoning effort: {effort}")
        max_tokens = int(os.environ.get("DSH_MAX_TOKENS") or 32768)
        context = int(os.environ.get("DSH_CONTEXT_WINDOW") or 1000000)
        if args.timeout <= 0 or not 0 < max_tokens <= context:
            raise ValueError("Require timeout > 0 and 0 < max_tokens <= context_window")
        from deepseek_harness import DeepSeekHarness
        from deepseek_harness_runtime import resolve_bundled_launch_args

        def notification(item):
            value = (
                dataclasses.asdict(item)
                if dataclasses.is_dataclass(item)
                else item.model_dump(mode="json")
            )
            if value.get("method") == "session.event":
                event = value.get("payload", {}).get("event")
                if isinstance(event, dict):
                    emit({"type": "dsh.event", "event": event})

        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        # No personal DSH home and no prior session reuse. Keep only the
        # redacted event stream, not SDK persistence that may contain secrets.
        with tempfile.TemporaryDirectory(prefix="dsh-", dir=args.state_dir) as state:
            with DeepSeekHarness(
                provider="deepseek-official",
                launch_args_override=resolve_bundled_launch_args("exe"),
                model=os.environ["DSH_MODEL"],
                cwd=str(Path(args.workdir).resolve()),
                runtime_cwd=state,
                session_root=str(Path(state) / "sessions"),
                cordis=str(Path(__file__).with_name("minimal.cordis.yml")),
                base_url=os.environ["DEEPSEEK_BASE_URL"],
                api_key=os.environ["DEEPSEEK_API_KEY"],
                max_tokens=max_tokens,
                request_timeout_seconds=args.timeout,
                env={
                    "DSH_HOME": state,
                    "DSH_REASONING_EFFORT": effort,
                    "DSH_MODEL": os.environ["DSH_MODEL"],
                    "DSH_CONTEXT_WINDOW": str(context),
                },
            ) as harness:
                result = harness.run(
                    Path(args.prompt_file).read_text(encoding="utf-8"),
                    on_notification=notification,
                )
            ok = result.finish_reason == "completed"
            emit(
                {
                    "type": "dsh.result",
                    "status": "ok" if ok else "error",
                    "finishReason": result.finish_reason,
                    "lastText": result.final_response,
                    "sdkVersion": installed,
                    "error": None
                    if ok
                    else f"DSH finish reason: {result.finish_reason}",
                }
            )
            return 0 if ok else 1
    except Exception as exc:
        emit(
            {
                "type": "dsh.result",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
