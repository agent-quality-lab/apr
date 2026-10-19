# Task recording and deliberate sharing

APR's recording harness is separate from the harness that directs execution. Choose **Existing harness** to keep that setup, or **Newcomer** for the same record format without an existing harness. Neither mode installs roles, grants runtime access, or starts agents automatically.

## Lifecycle

Use `start_task` before work, `append_evidence` for meaningful milestones or changes, and `finish_task` when delivering a result. Keep the returned task ID and link across turns. One automation or a long conversation can each be one task; new independent work gets another record. `list_tasks` and `get_task` expose selected private history to a connected assistant.

The baseline records the goal, success criteria, runtime/model and harness version. Later changes append evidence rather than replacing it. Retrospective tasks and migrated reviews disclose missing baseline information. Stable request/event IDs make exact retries safe; changed content under the same ID is rejected.

An agent's delivery is not a human review. The user supplies outcome and overall stars, with optional quality, speed and resource-efficiency stars. Only the browser review action writes that judgment. Skipping or leaving a task unrated never implies success.

## Evidence

Evidence has a kind (`milestone`, `setup`, `context`, `artifact`, `criteria`, or `resource`) and source (`user-stated`, `agent-reported`, `runtime-observed`, or `imported`). The task log may include authorized briefs, definitions, selected context, work logs and outputs. Credentials must be removed before submission and are rejected when recognizable. Do not sweep unrelated files or claim access to hidden reasoning.

Record actual resource evidence; unknown values remain unknown. A skill's statement is agent-reported, not native measurement. Parent and child totals may overlap, so do not add them automatically. Optional native hooks retain their documented limited coverage; they are not enabled by installation of this source.

## Workspace

The local service serves `/app/`: Setup, My work, task detail, review, and sharing. It uses the established loopback service and per-user store. Start it using `python3 local_service.py start`, or let the MCP bridge start it on demand. Opening a link cannot revive a stopped service.

The task link stays stable. The review shows starting goal and delivered result, sourced process/context and artifacts, and measured resources separately from stars. Existing `/review/` URLs remain available for historical records.

## Sharing

Sharing requires a saved human review. `prepare_share` creates a local preview; it never publishes or uploads. Select only the additional fields to share and redact individual values. The structural core contains category, human outcome/ratings, generic harness structure/version, and coverage. Private content starts unselected.

The workspace downloads a frozen approved JSON snapshot. Open the community publishing page, sign in with GitHub, select that snapshot, inspect the exact public preview, and explicitly publish. Downloading, loading a file, creating a preview or signing in alone does not publish. No private log contents are put into the destination URL.

The website validates the selected payload again. A public page is an immutable publication; later local activity cannot update it. Owner removal deletes the publication independently of the private source. A new public version requires a new deliberate publication.

This file-import handoff avoids handing a local API credential to a remote website or exposing the entire local store through cross-origin browser access. Direct authenticated transfer can be added separately without changing the selected-snapshot contract.

## Support and rollout

Small lifecycle calls within normal turns are permitted. Do not create another model continuation just for APR. Runtime availability, bridge reachability, lifecycle identity, native measurements and link presentation must each be verified; a synthetic test is not native-runtime acceptance.

The maintained skill is in `skill/agent-performance-review/SKILL.md`. Updating source does not update an already-running MCP process or install the skill into every runtime. Keep those deployment steps separate and reload the host after a reviewed installation. Community configuration, tracked hosted migrations and real GitHub OAuth acceptance are also separate from local source verification. The existing hosting budget remains $0.
