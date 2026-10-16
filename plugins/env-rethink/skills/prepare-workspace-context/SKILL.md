---
name: prepare-workspace-context
description: Prepare or connect Env-Rethink collection maps and event logs for Codex or Claude Code, validate an existing context bundle, or diagnose missing workspace_env tools.
---

# Prepare workspace context

Use the installed `env-rethink-context` command. The repository's
`integrations/README.md` is the copy-and-paste operator guide. If needed, locate
the Env-Rethink checkout from the user or existing workspace context; do not
guess machine-specific paths or scan unrelated home directories.

## Identify the inputs

Determine the actual agent-visible workspace, target client, and whether the
user already has a reviewed map and public history. Keep all generated state
outside that workspace. Reuse the user's chosen model/provider for generation;
never silently change providers or print credentials.

For existing artifacts, locate exactly:

- a `workspace-bench.workspace-collection-set.v3` public map JSON;
- its corresponding `workspace-collection-map.members.sqlite`;
- a public events JSONL (`events.public.jsonl` or `visible_events.public.jsonl`).

An existing `services/workspace-env/fixture.json` identifies its three files
under `blobs/`; read its `artifacts` mapping. Do not send private fixture/audit
contents to the task-solving model or copy the whole generation directory.

## When generation is needed

Follow `envgen/README.md` and `envgen/docs/RUNNER_CONTRACT.md` from the checkout.
Run envgen in a separate Python environment: envgen-kit and the context adapter
both provide a `workspace_env` package and must not be installed together.

- Semantic maps: use `run_workspace_collection_synthesis.py` with the v2
  full-coverage configuration. Its final public map uses the **v3 artifact**
  schema; configuration version and artifact version are different.
- Synthetic history: use `run_context_event_log_synthesis.py` in task-independent
  `workspace_inference` mode, with the existing runner contract and reviews.
- Metadata-only snapshot log: `generate_workspace_event_log.py` requires no
  model. Explain that it does not recover real activity or approval history.

Validate configuration paths and the configured Codex version against the
selected runner image before a model call. Existing generation configs pin
0.144.5 while the shared image defaults may differ. Do not bypass version/trace
checks or silently substitute a directory listing for a semantic map.
Read the producer's success manifest to find final accepted artifacts; failed
or incomplete intermediate files are not eligible for import.

For benchmark use, review public artifacts with the existing artifact gate;
never condition ordinary preparation on rubrics or reference answers. Runtime
schema validation is not proof of absence of semantic answer leakage.

## Import and verify

Run the following with actual paths (these placeholders are not commands to
execute verbatim):

```bash
env-rethink-context prepare \
  --workspace /absolute/agent-workspace \
  --collection-map /absolute/generation/final/workspace-collection-set.public.json \
  --member-index /absolute/generation/final/workspace-collection-map.members.sqlite \
  --events /absolute/history/final/events.public.jsonl \
  --bundle /absolute/context-bundles/new-run
env-rethink-context doctor --bundle /absolute/context-bundles/new-run
```

`prepare` must report `status: ready`; `doctor` must report `status: passed`
and all three tools. A new bundle directory is required on refresh. Never
overwrite a working bundle during an active session.

## Connect the client

Use the plugin installation steps in `integrations/README.md`. The plugin
starts `env-rethink-context serve` and reads `ENV_RETHINK_BUNDLE` from its
parent process. Activate the context venv and export that variable **before**
launching the client. Restart an existing client to pick up changes.

For clients without plugin support, run `client-config --client codex|claude
--bundle ...`. It prints a shell-quoted installation command with an absolute
Python path. It does not execute it or replace client settings. Preserve any
existing server with that name, and use only the user's intended config scope.
Install this plugin's `workspace-context` skill in the project's skill directory
as described in the guide when using that fallback.

Report the workspace, bundle path, client, provenance and verification outcome.
Distinguish protocol checks from a real model run. No API call is needed to
import artifacts, configure MCP or run `doctor`.
