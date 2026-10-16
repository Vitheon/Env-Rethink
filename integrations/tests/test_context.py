from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from env_rethink_context.cli import client_config, make_runtime, prepare, probe

spec = importlib.util.spec_from_file_location("demo", Path(__file__).parents[1] / "create_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "中文 demo with spaces"
    demo.create_demo(root)
    return argparse.Namespace(
        workspace=str(root / "workspace"), collection_map=str(root / "public/map.json"),
        member_index=str(root / "public/members.sqlite"), events=str(root / "public/events.public.jsonl"),
        bundle=str(root / "bundle"),
    )


def test_prepare_and_real_stdio(inputs):
    result = prepare(inputs)
    assert result["files"] == 2 and result["history_methods"] == ["snapshot_observation"]
    checked = asyncio.run(asyncio.wait_for(probe(Path(inputs.bundle)), timeout=45))
    assert checked["status"] == "passed" and checked["model_calls"] == 0
    assert len(checked["tools"]) == 3


def test_private_event_rejected_without_publishing(inputs):
    path = Path(inputs.events)
    row = json.loads(path.read_text())
    row["rubric"] = "PRIVATE CANARY"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="PUBLIC event"):
        prepare(inputs)
    assert not Path(inputs.bundle).exists()


def test_historical_event_path_can_be_missing_but_not_escape(inputs):
    path = Path(inputs.events)
    row = json.loads(path.read_text())
    row["object"]["path_at_event"] = "archive/old-report.txt"
    path.write_text(json.dumps(row))
    assert prepare(inputs)["events"] == 1
    inputs.bundle += "-bad"
    row["object"]["path_at_event"] = "metadata.json"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="Private/evaluation"):
        prepare(inputs)


def test_empty_history_is_explicit(inputs):
    Path(inputs.events).write_text("")
    result = prepare(inputs)
    assert result["events"] == 0 and result["history_methods"] == []


@pytest.mark.parametrize("path", ["../secret.txt", "/tmp/secret.txt", "metadata.json", "x/.env", "audit.private.json"])
def test_forbidden_member(inputs, path):
    with sqlite3.connect(inputs.member_index) as db:
        db.execute("UPDATE members SET path=? WHERE path=?", (path, "报表.txt"))
    with pytest.raises(ValueError):
        prepare(inputs)


def test_symlink_escape(inputs, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("PRIVATE CANARY")
    member = Path(inputs.workspace) / "报表.txt"
    member.unlink()
    member.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        prepare(inputs)


def test_missing_member(inputs):
    (Path(inputs.workspace) / "报表.txt").unlink()
    with pytest.raises(ValueError, match="missing"):
        prepare(inputs)


def test_symlink_to_private_file_inside_workspace(inputs):
    workspace = Path(inputs.workspace)
    (workspace / "metadata.json").write_text('{"rubric": "PRIVATE CANARY"}')
    member = workspace / "报表.txt"
    member.unlink()
    member.symlink_to("metadata.json")
    with pytest.raises(ValueError, match="Private/evaluation symlink"):
        prepare(inputs)


def test_map_index_mismatch(inputs):
    with sqlite3.connect(inputs.member_index) as db:
        db.execute("UPDATE metadata SET value='wrong' WHERE key='workspace_collection_set_sha256'")
    with pytest.raises(ValueError, match="different generations"):
        prepare(inputs)


def test_counts_rejected_and_import_atomic(inputs):
    with sqlite3.connect(inputs.member_index) as db:
        db.execute("DELETE FROM members WHERE path='报表.txt'")
    with pytest.raises(ValueError):
        prepare(inputs)
    assert not Path(inputs.bundle).exists()


def test_state_must_be_outside_workspace_and_no_overwrite(inputs):
    original = inputs.bundle
    inputs.bundle = str(Path(inputs.workspace) / "bundle")
    with pytest.raises(ValueError, match="outside"):
        prepare(inputs)
    inputs.bundle = original
    prepare(inputs)
    with pytest.raises(ValueError, match="already exists"):
        prepare(inputs)


def test_rebuilt_index_excludes_private_tables(inputs):
    with sqlite3.connect(inputs.member_index) as db:
        db.execute("CREATE TABLE private_notes (secret TEXT)")
        db.execute("INSERT INTO private_notes VALUES ('PRIVATE CANARY')")
    prepare(inputs)
    index = Path(inputs.bundle) / "blobs/workspace-collection-map.members.sqlite"
    assert b"PRIVATE CANARY" not in index.read_bytes()


def test_sessions_independent_and_query_errors(inputs, tmp_path):
    prepare(inputs)
    one = make_runtime(Path(inputs.bundle), tmp_path / "state-one")
    two = make_runtime(Path(inputs.bundle), tmp_path / "state-two")
    try:
        assert one.resolved.manifest.workspace.artifact_root != two.resolved.manifest.workspace.artifact_root
        assert one.workspace_search_text(path="../secret.txt").isError
        assert one.workspace_search_text(path="报表.txt", card_id="demo").isError
        assert "报表.txt" in one.workspace_search_text(card_id="demo")
        assert "报表.txt" in two.event_search_text(path="报表.txt")
    finally:
        one.close()
        two.close()


def test_tampered_bundle_refused(inputs, tmp_path):
    prepare(inputs)
    events = Path(inputs.bundle) / "blobs/visible_events.public.jsonl"
    events.write_text("")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        make_runtime(Path(inputs.bundle), tmp_path / "state")


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_client_config_command_can_launch_with_unicode_spaces(inputs, client):
    prepare(inputs)
    config = client_config(Path(inputs.bundle), client)
    import shlex
    command = shlex.split(config["command"])
    start = command.index("--") + 1
    actual = command[start:]
    actual[actual.index("serve")] = "doctor"
    result = subprocess.run(actual, capture_output=True, text=True, timeout=65)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "passed"
