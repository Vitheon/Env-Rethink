"""Import reviewed public artifacts and serve the existing tools over stdio.

No model calls, directory-wide scans, client-global writes, or new history
inference happen here. envgen remains the producer of maps and event logs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import sqlite3
import sys
import tempfile
from typing import Any

from workspace_env.collection_map import (
    WorkspaceCollectionSetV3, build_workspace_collection_v3_index, sha256_file,
)
from workspace_env.event_log import PublicEvent
from workspace_env.manifest_staging import stage_environment_layer
from workspace_env.manifest import canonical_json, sha256_text
from workspace_env.runtime import WorkspaceRuntime
from workspace_env.server import TOOL_NAMES, build_server


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def outside_workspace(path: Path, workspace: Path) -> None:
    if path == workspace or workspace in path.parents:
        raise ValueError("Bundle/state must be outside the agent workspace")


def check_path(value: str, workspace: Path, *, must_exist: bool) -> None:
    relative = PurePosixPath(value)
    if (not value or relative.is_absolute() or "\\" in value or "\x00" in value
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError(f"Not a workspace-relative path: {value!r}")
    # Known evaluator/config surfaces must never become map/search results.
    forbidden = {".git", ".env", "metadata.json", "rubrics.json", "ground_truth.json",
                 "data_manifest.json", "input_gt", "canonical.private.jsonl"}
    if any(p.casefold() in forbidden or ".private." in p.casefold() for p in relative.parts):
        raise ValueError(f"Private/evaluation path in public artifacts: {value}")
    target = (workspace / value).resolve()
    if target == workspace or workspace not in target.parents:
        raise ValueError(f"Path escapes workspace (possibly a symlink): {value}")
    if any(p.casefold() in forbidden or ".private." in p.casefold()
           for p in target.relative_to(workspace).parts):
        raise ValueError(f"Private/evaluation symlink target: {value}")
    if must_exist and not target.is_file():
        raise ValueError(f"Map member is missing from this workspace: {value}")


def load_public_events(path: Path, workspace: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = PublicEvent.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"Invalid PUBLIC event at line {line_no}; do not use canonical/private logs") from exc
        if event.object:
            check_path(event.object.path_at_event, workspace, must_exist=False)
        for key, value in event.payload.items():
            if key in {"path", "path_before", "path_after", "destination_directory"} or key.endswith("_path"):
                check_path(value, workspace, must_exist=False)
        rows.append(event.model_dump(mode="json"))
        kinds.add(event.provenance.generation_method)
    return rows, sorted(kinds)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("--workspace must be a directory")
    bundle = Path(args.bundle).expanduser().resolve()
    outside_workspace(bundle, workspace)
    if bundle.exists():
        raise ValueError("Bundle already exists; use a new directory to refresh artifacts")
    collection = WorkspaceCollectionSetV3.model_validate(read_json(Path(args.collection_map)))
    index = Path(args.member_index).expanduser().resolve(strict=True)
    with sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        metadata = dict(db.execute("SELECT key, value FROM metadata"))
        if metadata.get("workspace_collection_set_sha256") != sha256_text(canonical_json(collection.model_dump(mode="json"))):
            raise ValueError("Collection map and member index belong to different generations")
        memberships: dict[str, list[str]] = {}
        for card_id, path in db.execute("SELECT card_id, path FROM members ORDER BY card_id, path"):
            check_path(path, workspace, must_exist=True)
            memberships.setdefault(card_id, []).append(path)
    events, kinds = load_public_events(Path(args.events), workspace)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    # Publish only after schema, membership, and runtime validation succeeds.
    with tempfile.TemporaryDirectory(prefix=".env-rethink-import-", dir=bundle.parent) as temporary:
        draft = Path(temporary) / "bundle"
        blobs = draft / "blobs"
        blobs.mkdir(parents=True, mode=0o700)
        draft.chmod(0o700)
        collection_path = blobs / "workspace-collection-map.public.json"
        index_path = blobs / "workspace-collection-map.members.sqlite"
        events_path = blobs / "visible_events.public.jsonl"
        write_json(collection_path, collection.model_dump(mode="json"))
        # Rebuild FTS/cards from the validated public map; never copy arbitrary
        # hidden tables, stale snippets, or executable schema from a source DB.
        build_workspace_collection_v3_index(collection, memberships=memberships, index_path=index_path)
        events_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events), encoding="utf-8")
        events_path.chmod(0o600)
        write_json(draft / "fixture.json", {
            "schema_version": 1, "generator": "env-rethink-context-public-import",
            "pool": {"workspace_snapshot_hash": collection.workspace_snapshot_hash},
            "run_config": {"event_delete_rate": 0.0, "deletion_seed": 0},
            "artifacts": {
                "collection_set": collection_path.name, "collection_set_sha256": sha256_file(collection_path),
                "member_index": index_path.name, "member_index_sha256": sha256_file(index_path),
                "events": events_path.name, "events_sha256": sha256_file(events_path),
            },
        })
        write_json(draft / "connection.json", {
            "schema_version": 1, "workspace": str(workspace),
            "history_methods": kinds, "event_count": len(events),
        })
        with tempfile.TemporaryDirectory(prefix="check-", dir=temporary) as state:
            runtime = make_runtime(draft, Path(state))
            try:
                result = runtime.workspace_map()
                if result.get("error"):
                    raise ValueError("Imported map cannot be queried")
            finally:
                runtime.close()
        draft.rename(bundle)
    return {"status": "ready", "bundle": str(bundle), "workspace": str(workspace),
            "files": collection.distinct_file_count, "cards": len(collection.cards),
            "events": len(events), "history_methods": kinds}


def bundle_path(value: str | None) -> Path:
    selected = value or os.environ.get("ENV_RETHINK_BUNDLE")
    if not selected:
        raise ValueError("Set ENV_RETHINK_BUNDLE or pass --bundle (see env-rethink-context prepare --help)")
    return Path(selected).expanduser().resolve(strict=True)


def make_runtime(bundle: Path, state: Path) -> WorkspaceRuntime:
    connection = read_json(bundle / "connection.json")
    if connection.get("schema_version") != 1:
        raise ValueError("Unsupported connection schema")
    workspace = Path(connection["workspace"]).resolve(strict=True)
    outside_workspace(bundle, workspace)
    outside_workspace(state.resolve(), workspace)
    resolved, _, _ = stage_environment_layer(
        fixture_path=bundle / "fixture.json", blobs_root=bundle / "blobs",
        workspace_root=workspace, state_dir=state,
        run_id="local-context", task_id="local-workspace",
    )
    return WorkspaceRuntime(resolved)


def serve(bundle: Path) -> None:
    # A fresh state per MCP process prevents two clients sharing cursors/logs.
    sessions = bundle / "sessions"
    sessions.mkdir(mode=0o700, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="session-", dir=sessions))
    runtime = make_runtime(bundle, state)
    try:
        # stdio does not open an HTTP listener or require a bearer credential.
        server = build_server(runtime, host="127.0.0.1", port=0, bearer_token="unused-stdio")
        server.run(transport="stdio")
    finally:
        runtime.close()


async def probe(bundle: Path) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "env_rethink_context", "serve", "--bundle", str(bundle),
    ])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            if names != sorted(TOOL_NAMES):
                raise ValueError(f"Unexpected tool surface: {names}")
            collection = read_json(bundle / "blobs/workspace-collection-map.public.json")
            card_id = collection["cards"][0]["card_id"]
            checks = [("workspace_map", {}), ("workspace_search", {"card_id": card_id}),
                      ("event_search", {})]
            for name, arguments in checks:
                response = await session.call_tool(name, arguments)
                if response.isError:
                    raise ValueError(f"MCP tool failed: {name}: {response.content}")
    return {"status": "passed", "transport": "stdio", "tools": names,
            "bundle": str(bundle), "model_calls": 0}


def client_config(bundle: Path, client: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "env_rethink_context", "serve", "--bundle", str(bundle)]
    if client == "codex":
        install = ["codex", "mcp", "add", "env_rethink", "--", *command]
    else:
        install = ["claude", "mcp", "add", "--transport", "stdio", "--scope", "local",
                   "env_rethink", "--", *command]
    return {"client": client, "command": shlex.join(install),
            "mcpServers": {"env_rethink": {"command": command[0], "args": command[1:]}}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare", help="Import a reviewed v3 map/index and public event log")
    for option in ("workspace", "collection-map", "member-index", "events", "bundle"):
        prep.add_argument("--" + option, required=True)
    for name in ("serve", "doctor", "client-config"):
        sub = commands.add_parser(name)
        sub.add_argument("--bundle", help="Defaults to ENV_RETHINK_BUNDLE")
        if name == "client-config":
            sub.add_argument("--client", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args)
        else:
            bundle = bundle_path(args.bundle)
            if args.action == "serve":
                serve(bundle)
                return 0
            if args.action == "doctor":
                result = asyncio.run(asyncio.wait_for(probe(bundle), timeout=60))
            else:
                result = client_config(bundle, args.client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, TimeoutError) as exc:
        message = str(exc) or "MCP doctor timed out after 60 seconds; check server startup and tokenizer cache"
        print(f"env-rethink-context: {message}", file=sys.stderr)
        return 2
    except ExceptionGroup:
        print("env-rethink-context: MCP connection failed; see server stderr above, or run serve --bundle PATH directly", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
