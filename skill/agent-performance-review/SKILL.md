---
name: agent-performance-review
description: "At the end of every turn, refresh a one-click local rating link for the active task/run. The user opens the compact rating page when ready."
---

# Agent Performance Review

Use when the user requests a review link, or has authorized an assistant-generated link at the end of each turn. Reuse one review ID for the full active task/run, including earlier turns and linked sub-agent work where visible. This is a manual MCP workflow; prototype hooks stay off.

## 1. Build a short evidence-based run card

Use visible conversation evidence for short, privacy-safe **Task**, **Action**, and **Result** summaries of the full active run. Keep each under 500 characters. Generalize names, customer details, URLs, file paths, and document-specific content. Do not read the transcript file, tool arguments or outputs, filenames, or document contents to fill gaps. If a detail is unavailable, use `unknown`; do not infer it.

Use only explicitly available, content-free evidence for context:

- `model`: an explicitly supplied runtime model slug, or `unknown`.
- `connections`: explicitly supplied broad tool-category counts, or `unknown`. Never include raw tool or connection names.
- `tool_calls`: an explicitly supplied observed count, or `unknown`.
- `elapsed_time`: an explicitly supplied measurement, or `unknown`; identify approximate measurements as approximate.
- `tokens` and `cost`: use runtime-reported usage only when available; otherwise `unknown`. Do not tokenize a summary and present it as total run usage.
- `loops` and `roles`: `unknown` unless they are explicitly visible in the conversation.
- `review_id`: one stable caller-selected ID reused across turns in the active run. Pending links show the latest caller-supplied snapshot; saved reviews retain the snapshot that was rated.

Do not inspect a transcript or collect user prompts, tool data, paths, or document contents. This manual workflow does not observe runtime events automatically. No metadata or review is uploaded.

## 2. Create the one-click rating link

Call local MCP `request_rating` with `review_id`, `job_type`, the brief `task`, `action`, and `result`, `verification` (`verified`, `partial`, `unverified`, or `unknown`), and the known `context` and `resources` fields. Keep `job_type` to `writing`, `research`, `coding`, or `other`. Use cumulative metadata across turns and label elapsed time approximate. The tool starts the per-user loopback service on demand if needed, then returns the SAME loopback-only URL for a repeated `review_id` (earlier links show the newest snapshot while pending); it does not open the browser or save a review. The tool returns `expires_in_seconds: null`: links persist until the review data is explicitly removed, and need the local service running to open.

End the assistant response with exactly one Markdown link, using the returned URL:

`[⭐ Review this task](http://127.0.0.1:PORT/review/...)`

Do not show the run card or ask for scores in chat. The user clicks the link to open the rating page, which already contains the run summary and available context. The first question is whether the agent finished the job; No reveals optional reason chips. Then the page collects four 1–5 star ratings: overall, quality, speed, and resource efficiency. There are no text fields. The page buttons are Save locally and Skip review; skipping discards the selections without saving.

If link creation fails, give a short error instead of fabricating a link. A saved review freezes the snapshot that was rated; rating again needs an explicit new `review_id`.

## 3. Save from the page only after submission

The rating page posts a submitted review to the local service, which stores it in the per-user SQLite database. Skip saves nothing. Do not call `record_review` after creating a link; no rating exists until the user submits in the page.

Reusing a review ID with identical content is a safe retry; use a new ID for a different review.

## 4. Local summary only on request

Call `review_summary` only when the user asks about saved reviews. Describe results as a local-only count and averages from this computer. Do not present them as community ratings or infer why a setup scored better.

This prototype stores reviews locally only. No Clerk, network service, public feed, or user account is part of it. Runtime hooks and automatic capture are not enabled: model and context values are caller-supplied, and missing token/cost numbers stay `unknown`.
