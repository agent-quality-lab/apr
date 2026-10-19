---
name: agent-performance-review
description: "Use when APR recording is enabled for a task, automation, or ongoing job, or when the user asks to review an agent's work."
---

# Agent Performance Review

APR records a job aimed at a result. A single automation or a conversation spanning fifty messages can each be one task. A chat can contain several tasks. The recording harness complements an existing execution harness; newcomer mode uses the same record format without requiring an existing harness.

## Start before working

When APR recording is enabled, call `start_task` within the normal turn, before substantive work. Supply a stable `request_id`, title, category (`coding`, `writing`, `research`, or `other`), goal, success criteria, runtime/model when known, and the harness mode, name, version, roles, loops, rules, tools, and context categories actually available. Keep the returned `task_id` and `task_url` for subsequent turns. A retry uses the same request ID and identical payload.

Record the user's stated criteria faithfully. Label additional inferred criteria in the evidence; do not present them as user-stated. Unknown model/version/resource values stay unknown. Record explicit `parent_task_id` for delegated work, without guessing relationships from directories or recent activity.

For a retrospective review, use `baseline_missing: true`. Do not reconstruct an earlier baseline as if it had been recorded at the start. A new independent job gets a new task ID; continuation and rework toward the same result keep the existing task.

## Preserve useful evidence

Use `append_evidence` at meaningful milestones or changes, not after every token or tool call. Supply a stable `event_id`, kind (`milestone`, `setup`, `context`, `artifact`, `criteria`, or `resource`), source, and structured data. Retrying an event must not change its content. Sources are `user-stated`, `agent-reported`, `runtime-observed`, and `imported`.

Keep the starting goal, criteria, and setup intact. Changed criteria or setup belong in new evidence entries. Record accessible task briefs, instructions, reusable definitions, selected context, work logs, outputs, and artifacts within the user's task and access authorization. Do not sweep unrelated files or claim to preserve hidden reasoning. Do not send credentials, secrets, session tokens, or private keys to APR. If a source contains them, omit or redact those values before calling a tool. Rejection of a sensitive payload is a reason to remove the sensitive material, not bypass validation.

Skill summaries are `agent-reported`. Only use `runtime-observed` for evidence actually supplied by the runtime; imported logs retain `imported`. Do not invent token totals, cost, compute time, corrections, or causal explanations. Keep parent and child resource measurements separate so totals are not doubled.

## Finish and present the record

When delivering the result, call `finish_task` with the task ID, a stable event ID, result, and verification state. If interrupted, mark it interrupted. Finishing records the agent's delivery; it does not supply a human review or positive outcome. Continue useful work if APR is temporarily unavailable, and disclose the recording gap without inventing a successful receipt.

End each authorized turn with the same returned task link:

`[⭐ Review this task](RETURNED_TASK_URL)`

Do not create an extra model continuation just to record or print the link. Modest MCP calls within normal turns are permitted. Runtime hooks remain optional and their coverage must be stated separately.

Only the human submits a review. The form requires outcome and overall stars; quality, speed, and resource-efficiency stars are visible but optional. Never turn an unreviewed task into success, infer missing ratings, or call a write-rating tool on the user's behalf.

## History and sharing

Use `list_tasks` and `get_task` for explicitly requested local history analysis. Explain that supplying selected records to a cloud assistant crosses that provider's boundary even though APR stores them locally.

`prepare_share` creates a local preview only, after a saved review. Sharing is a separate human action: choose fields, redact individual values, inspect the exact snapshot, then explicitly publish through the community website. Private content starts unselected. Do not upload local history, publish a task, or update a previously shared snapshot merely because recording or review is enabled.

If the connected MCP still exposes only `request_rating`, report that it is the legacy end-only integration and needs the updated bridge/reload. Do not claim a starting baseline or lifecycle coverage it cannot provide. Existing legacy review URLs remain valid.
