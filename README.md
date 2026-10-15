# Agent Performance Review — Local Integrations

Public source for the local runtime integrations and review flow. The code is experimental; it is published for inspection and development, not as a supported or operational release.

## Contents

- `mcp_server.py` — local MCP stdio bridge (thin client of the loopback service).
- `local_service.py` — per-user loopback HTTP service: `start/stop/status/serve/import`.
- `review_schema.py` — shared validation (no service/store imports).
- `review_store.py` — SQLite persistence, tracked migrations, legacy JSONL import.
- `rating-popup/` — local rating form served by the service.
- `skill/agent-performance-review/` — Codex skill source.
- `capture_metadata_hook.py`, `runtime_hook.py`, `stop_hook.py`, `run_state.py`, and `hooks.json` — unsupported prototype hook adapters. They are not installed, not wired to any runtime, and not dependencies of the MCP bridge or service.
- `tests/` — maintained suite, run with `python3 -m unittest discover -s tests`.
- `docs/operators/local-mcp.md` — operator guide (commands, data location, import, recovery).

## Current status

The Codex review hooks were removed from the active user configuration. This checkout is not installed in any runtime. Repeated end-of-turn links, cumulative metadata capture, and cross-runtime setup have not passed real end-to-end validation. Do not treat the hook adapters as operational.

The local flow keeps reviews on the user's computer. It must not collect prompts, responses, tool arguments or outputs, file paths, or document contents. Runtime-reported token and cost data remain unknown when the host does not provide reliable measurements. Model and context values are caller-supplied, not automatically captured.

## Local MCP setup

The MCP server uses Python's standard library. Configure an MCP-capable host to launch `mcp_server.py` as a stdio server, using the absolute path to this checkout. On the first tool call the bridge starts the per-user loopback service on demand; the rating page is served while that service is running.

For Codex, the canonical install (bridge registration verified against `codex mcp add --help`, full skill copy, no `~/.codex` edits here) is documented once in `docs/operators/local-mcp.md` under "Manual Codex integration"; this README carries no second copy.

Service control and data locations are documented in `docs/operators/local-mcp.md`. The default data directory lives outside the checkout (`~/Library/Application Support/Agent Performance Review/` on macOS, XDG equivalent elsewhere); `APR_DATA_DIR` overrides it. `request_rating` returns the same `rating_url` for a repeated `review_id` and `expires_in_seconds: null`. Hook adapters require runtime-specific configuration and explicit validation before use. `hooks.json` is a prototype reference, not an active Codex registration.

## Privacy and local data

Submitted reviews are stored in the per-user SQLite database outside the source tree. Never commit databases, credentials, runtime state, prompts, responses, tool arguments or outputs, paths, or document contents. `reviews.jsonl` from older checkouts is legacy import input only. Sharing is not implemented by this local prototype.

## Release decisions

This project is distributed under the MIT License. Copyright (c) 2026 Andrey Zaynulin. See `LICENSE` for the full terms.
