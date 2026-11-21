---
name: agent-performance-review
description: "Record a task with APR when the user invokes $agent-performance-review, asks to record or track it, or says review this task; continue recording the same job until delivery."
---

# Agent Performance Review

## First run

Explicit invocation (for example `$agent-performance-review`, or "record this task with APR") turns recording on for the task the user names in that message. Before doing the task: call `get_setup`, then `start_task`, then do the work. If the invocation names no task, start recording the next task the user gives. On every later turn of the same task, reuse the stored `task_id` and `task_url`; do not call `start_task` again with a new request ID. If the APR tools (`start_task`, `append_evidence`, `finish_task`) are not available in this session, say the MCP server is not registered or the host needs a reload, then do the task without claiming it was recorded.

APR records a job aimed at a result. A single automation or a conversation spanning fifty messages can each be one task. A chat can contain several tasks. The recording harness complements an existing execution harness; newcomer mode uses the same record format without requiring an existing harness.

## One local link from a desktop chat

Treat “review this task” as opt-in for the current job only. A quoted example, a question about APR, or an ordinary request for code review does not enable it. If the chat has no identifiable task, ask what result the user wants reviewed before creating a record. A new job needs its own opt-in.

Use the connected APR MCP tools directly in this chat. Record only the goal, criteria and a short delivered result for an ordinary review. Use the existing harness, empty lists for unrecorded components and “unknown” for an unknown version. No terminal commands, CLI observer, background watcher or second Codex session are needed. File observations are a separate explicit opt-in. When selected desktop resource scope is supplied, the local service reads the corresponding session file for numeric counters as described below.

If work is already underway when the user opts in, set `baseline_missing: true` and use the original request visible in this chat. If the result is already available, start and finish in the same turn. Reuse the task ID you received for follow-ups to that job. Never select a task by “most recent” across chats. Use a fresh, collision-resistant request ID for each job, retained for exact retries.

## Start before working

If the recording mode is unclear, read `get_setup` once. Call `start_task` within the normal turn, before substantive work. Setup preferences do not prove runtime installation or capabilities. Supply a stable `request_id`, title, category (`coding`, `writing`, `research`, or `other`), goal, success criteria, runtime/model when known, and the harness mode, name, version, roles, loops, rules, tools, and context categories actually available. Keep the returned `task_id` and `task_url` for subsequent turns. A retry uses the same request ID and identical payload.

Record the user's stated criteria faithfully. Set `baseline_source` to `user-stated` only when the goal and criteria faithfully reflect explicit user requirements; otherwise use `agent-reported`. Label additional inferred criteria in the evidence; do not present them as user-stated. Unknown model/version/resource values stay unknown. Record explicit `parent_task_id` for delegated work, without guessing relationships from directories or recent activity.

For a retrospective review, use `baseline_missing: true`. Do not reconstruct an earlier baseline as if it had been recorded at the start. A new independent job gets a new task ID; continuation and rework toward the same result keep the existing task.

## Preserve useful evidence

Use `append_evidence` at meaningful milestones or changes, not after every token or tool call. Supply a stable `event_id`, kind (`milestone`, `setup`, `context`, `artifact`, `criteria`, or `resource`), source, and structured data. Retrying an event must not change its content. Sources are `user-stated`, `agent-reported`, `runtime-observed`, and `imported`.

Keep the starting goal, criteria, and setup intact. Changed criteria or setup belong in new evidence entries. Record accessible task briefs, instructions, reusable definitions, selected context, work logs, outputs, and artifacts within the user's task and access authorization. Do not sweep unrelated files or claim to preserve hidden reasoning. Do not send credentials, secrets, session tokens, or private keys to APR. If a source contains them, omit or redact those values before calling a tool. Rejection of a sensitive payload is a reason to remove the sensitive material, not bypass validation.

Skill summaries are `agent-reported`. Only use `runtime-observed` for evidence actually supplied by the runtime; imported logs retain `imported`. Do not invent token totals, cost, compute time, corrections, or causal explanations. Keep parent and child resource measurements separate so totals are not doubled.

## Finish and present the record

When delivering the result, call `finish_task` with the task ID, a stable event ID, result, and available verification details as an object. If interrupted, mark it interrupted. Finishing records the agent's delivery; it does not supply a human review or positive outcome. Continue useful work if APR is temporarily unavailable, and disclose the recording gap without inventing a successful receipt.

For an opted-in local Codex Desktop review, include `verification.codex_resource_scope` with `thread_id` and a list of exact `turn_ids` for the work being rated. Use current runtime IDs. If needed, read just the current process's `CODEX_THREAD_ID` and use the desktop `read_thread` tool on that exact chat to identify its work turns. Keep this lookup inside the normal task; the human has no setup steps. Include the active delivery turn if it did the work. For retrospective reviews select the original work turns and exclude the later rating request. Omit the scope if a turn covers unrelated jobs, the IDs are uncertain, or the user declines usage collection. Never select a chat by recency. The local review page reads completed turn counters on demand. It stores numeric usage with the saved review. Billing and Mac process memory are separate from these counters.

After `finish_task` succeeds, include one APR link with the delivered result: `[⭐ Rate this task](REVIEW_URL)`. Use the `review_url` returned by `start_task`. An already-open bridge may return only `task_url`: for its local `/app/#task=<task_id>` link, change just the fragment to `#review=<same task_id>`. Preserve the returned host, port and task ID. Do not guess a port or link. Keep the link for this task across turns; there is no need for a footer on progress messages.

Do not create an extra model continuation just to record or print the link. Modest MCP calls within normal turns are permitted. Runtime hooks remain optional and their coverage must be stated separately.

Only the human submits a review. The form requires outcome and overall stars; quality, speed, and resource-efficiency stars are visible but optional. Never turn an unreviewed task into success, infer missing ratings, or call a write-rating tool on the user's behalf.

## History and sharing

Use `list_tasks` and `get_task` for explicitly requested local history analysis. Explain that supplying selected records to a cloud assistant crosses that provider's boundary even though APR stores them locally.

`prepare_share` creates a local preview only, after a saved review. Sharing is a separate human action: choose fields, redact individual values, inspect the exact snapshot, then explicitly publish through the community website. Private content starts unselected. Do not upload local history, publish a task, or update a previously shared snapshot merely because recording or review is enabled.

If the connected MCP still exposes only `request_rating`, report that it is the legacy end-only integration and needs the updated bridge/reload. Do not claim a starting baseline or lifecycle coverage it cannot provide. Existing legacy review URLs remain valid.
