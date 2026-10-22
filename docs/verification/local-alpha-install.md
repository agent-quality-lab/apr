# Local alpha install verification (Codex on macOS)

Date: 2026-10-21. Base commit `a9902a3`, branch `codex/local-alpha-package`.
Canonical guide: [Codex on macOS quickstart](../operators/local-mcp.md#codex-on-macos-quickstart).

## Environment

- macOS (Darwin 25.6.0)
- Python 3.14.2 (`python3 --version`); no other Python version was run
- Codex CLI `codex-cli 0.152.1` (`codex --version`); `codex mcp add --help`
  shows `codex mcp add [OPTIONS] <NAME> (--url <URL> | -- <COMMAND>...)` and `--env`

## Source-level protocol evidence (synthetic)

`python3 scripts/smoke_local_alpha.py` launches `mcp_server.py` as a stdio
subprocess with a script-owned temporary `APR_DATA_DIR` and checks:

- `initialize` returns `agent-performance-review` 0.6.0, protocol `2025-06-18`
- `tools/list` exposes `append_evidence`, `finish_task`, `get_setup`,
  `get_task`, `list_tasks`, `prepare_share`, `record_review`,
  `request_rating`, `review_summary`, `start_task`
- the `start_task` payload covers the `required` list read from `tools/list`
- `get_setup` → `start_task` → identical `start_task` retry (same task and URL)
  → `append_evidence` plus identical retry (`already_recorded`) → `finish_task`
  → `get_task` shows `awaiting_review`, `review: null`, and exactly one copy of
  the retried event followed by the finish event in `evidence`
- the returned `task_url` (`http://127.0.0.1:PORT/app/#task=...`) serves HTTP 200
- after `local_service.py stop`, a new bridge process restarts the service, and
  the task and its `task_url` are unchanged

Result: all checks passed; the service was stopped and the temporary data
directory removed.

## Not verified

- A fresh user-level install following all quickstart steps, desktop reload,
  and genuine human review. The real CLI test below
  validates only a project-local skill with per-process MCP configuration.
- `codex mcp add` was not executed here; only its help output was checked.
- Skill location follows the official Codex local-skills documentation
  (user `$HOME/.agents/skills`, repository `.agents/skills`). The controller
  reported that an isolated real host read the project-local skill from
  a disposable project’s `.agents/skills`; the user-level
  `~/.agents/skills` install was not observed here. Older `~/.codex/skills`
  copies may still be discovered by existing installations.
- Full real-host acceptance has not passed. The isolated CLI lifecycle run
  described below succeeded after earlier attempts failed, but a user-level
  global install, Codex desktop reload, genuine human review submission, are still pending. User configuration was not changed.
- Genuine human review submission. The controller rendered the workspace and review form, as recorded below.
- Linux, Windows (unsupported: `fcntl`), other MCP hosts, other Python versions.
- Upgrade from a previously installed older skill/bridge in a real host.

## Controller real-host check (isolated Codex CLI)

A separate Codex CLI 0.152.1 session loaded the updated project-local skill
from `.agents/skills/agent-performance-review` and connected to this bridge
with per-process configuration overrides and a disposable `APR_DATA_DIR`.
No installed user configuration or global skill was changed.

The first Astra model attempt was rejected because that installed CLI requires
a newer version for Astra. A default-model run loaded the skill but denied
MCP calls under print mode's default approval policy `never`. The successful
run used the CLI's supported default `gpt-5.6-sol` and its normal
`--approve-for-me` review mode; no permission controls were bypassed.

Observed: `get_setup`, `start_task` (one invalid argument retry was corrected),
`append_evidence`, and `finish_task` were called, and the final answer displayed
the returned task URL. The isolated database independently contained one task
in `awaiting_review`, two milestone events (including finish), a nonempty
result, and zero human reviews. After the controller stopped and restarted
only this synthetic service, its persisted port and task URL were unchanged.

The controller rendered the task record and human review form at desktop and
390px width, confirming the outcome/overall requirement and optional dimension
ratings. It did not submit a human rating. This is real CLI integration evidence,
not proof of a newcomer user-level installation, Codex desktop reload, or a
completed owner acceptance walkthrough. Other models/hosts remain unverified.

## Resumed two-turn task

The controller also ran a persistent synthetic Codex session across two actual
turns. The first turn started one task, appended its first milestone, and left
it unfinished. The exact same session was resumed for the second item; it
appended another milestone and called `finish_task` without a second baseline.
Both final answers displayed the identical task URL. Independent store checks
confirmed one baseline for this job, three evidence events including finish,
`awaiting_review`, and no human review. A separate single-turn job in the same
disposable store remained distinct.

The final synthetic smoke was re-run after cleanup repair and passed all 15
checks. The cleanup failure regression first failed against the old behavior,
then both cleanup tests passed. The skill frontmatter and UI metadata parsed
as YAML with the system Ruby YAML parser; the optional Python skill validator
could not run because PyYAML is not installed. No dependency was added for it.

Final controller repository run: `python3 -m unittest discover -s tests` passed all **152 tests** in 52.282 seconds, including both new smoke-cleanup regressions.
