"""Create a tiny, explicitly fictional integration fixture; never an experiment.

Usage: python integrations/create_demo.py --output /new/demo-directory
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from workspace_env.collection_map import WorkspaceCollectionSetV3, build_workspace_collection_v3_index
from workspace_env.manifest import sha256_text


def create_demo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "交付说明.txt").write_text("演示项目：交付前核对报表。此文件仅用于接入测试。\n", encoding="utf-8")
    (workspace / "报表.txt").write_text("演示数据：收入 100，成本 60，利润 40。\n", encoding="utf-8")
    artifacts = root / "public"
    artifacts.mkdir()
    collection = WorkspaceCollectionSetV3.model_validate({
        "workspace_snapshot_hash": sha256_text("fictional-integration-demo"),
        "distinct_file_count": 2, "membership_count": 2,
        "cards": [{"card_id": "demo", "title": "演示项目", "description": "接入测试使用的说明与报表，不是真实工作历史。", "file_count": 2}],
    })
    (artifacts / "map.json").write_text(collection.model_dump_json(indent=2), encoding="utf-8")
    build_workspace_collection_v3_index(collection, memberships={"demo": ["交付说明.txt", "报表.txt"]}, index_path=artifacts / "members.sqlite")
    event = {
        "schema_version": 2, "event_id": "evt_aaaaaaaaaaaaaaaa",
        "occurred_at": "2026-01-01T00:00:00Z", "workspace_id": "wrk_aaaaaaaaaaaaaaaa",
        "session_id": "ses_aaaaaaaaaaaaaaaa", "actor": "workspace_automation", "action": "file.open",
        "object": {"object_id": "obj_aaaaaaaaaaaaaaaa", "path_at_event": "报表.txt"},
        "payload": {"application": "integration-demo", "open_mode": "read_only", "observation": "metadata_only"},
        "provenance": {"synthetic": True, "generation_method": "snapshot_observation",
                       "content_basis": "workspace_metadata", "transition_basis": "not_applicable",
                       "temporal_basis": "synthetic_timestamp"},
    }
    (artifacts / "events.public.jsonl").write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    create_demo(Path(parser.parse_args().output).expanduser().resolve())
