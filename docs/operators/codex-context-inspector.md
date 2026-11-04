# Inspect one Codex task locally

Start with an APR task, not a chat transcript. The task record holds the starting goal, criteria, delivery and human review. The optional inspector adds a scoped file inventory and read receipts for specific Codex CLI turns. Open **Open context inspector** in the task workspace, or call the read-only MCP tool `inspect_context` with the APR task ID.

**Off by default.** Loading the MCP, opening the workspace, reading an ASK index, or inspecting a task starts no observer. Nothing installs a hook, edits a skill, chooses context for Codex, or writes back to a source. The existing hook prototype is separate.

## Record two turns

Use the [existing task lifecycle](task-recording.md) with an explicit private `APR_DATA_DIR` shared by the MCP and collector. For a trial, use a new private temporary directory and isolate Codex from your normal configuration. A *resumed* run needs a stateful Codex session; do not use `--ephemeral` for this example.

1. Call `start_task` **before work**, using the request and criteria that Codex will receive. Keep the returned `task_id` and `task_url`. A model-written summary has source `agent-reported`; only literal user requirements are `user-stated`.
2. List the small set of text files eligible for this task. Use a single canonical source root and paths relative to it. No directory traversal or recursive inventory is performed. From this APR checkout, in Bash:

   ```bash
   set -o pipefail
   # APR_DATA_DIR, TASK_ID and SOURCE_ROOT refer to this trial's store, task and source root.
   export APR_DATA_DIR APR_CONTEXT_CAPTURE=1
   python3 codex_context.py inventory --task-id "$TASK_ID" --root "$SOURCE_ROOT" \
     guide.md reference.md
   ```

3. Pipe **one real invocation** directly to the observer for each turn. The following shows the pipe, using an already isolated, authenticated Codex CLI. Substitute the actual prompts and approved files. `-o` keeps just the selected final message for delivery; place it in private scratch, never inside a source repository. Initialize the store and supply the same per-process MCP config on both calls if using in-turn lifecycle tools. A local operator can also call the MCP before and after the run.

   ```bash
   umask 077
   scratch=$(mktemp -d)
   codex exec --json -o "$scratch/draft" "$FIRST_PROMPT" |
     python3 codex_context.py turn --task-id "$TASK_ID" --root "$SOURCE_ROOT" \
       --turn 1 --show-resume-id
   # Use the resume_id just printed; it is the Codex session, not the APR task ID.
   codex exec --json -o "$scratch/result" resume "$RESUME_ID" "$FOLLOWUP_PROMPT" |
     python3 codex_context.py turn --task-id "$TASK_ID" --root "$SOURCE_ROOT" --turn 2
   ```

4. Use `append_evidence` for useful **agent-reported** context or milestones. Use `finish_task` for the delivered result. If an in-turn tool is unavailable, preserve the gap and the provenance of any operator-recorded delivery. Never pass a transcript as evidence. Leave completion and stars empty for the human.
5. Open the task link → **Open context inspector** → **Review this task**. The human chooses completion and overall stars; other rating dimensions are optional. Later evidence stays separate from the rated snapshot. Inspector receipts are excluded even if all share fields are selected.

To stop capture, omit the pipe and unset `APR_CONTEXT_CAPTURE`. The local service can keep serving the same task link. To stop just the trial service, run `APR_DATA_DIR="$APR_DATA_DIR" python3 local_service.py stop`. Restart with `start` and that same store to inspect again.

## What an observation means

- **Inventory:** relative path, byte count and SHA-256 at snapshot time. This is the operator-selected boundary, not a census of everything the model could know. Snapshots accept up to 128 regular UTF-8 files, 256 KiB each; hidden/sensitive names, symlink entries and recognizable credentials are rejected. Select only authorized, non-secret material.
- **Observed read:** Codex reported a completed, successful single-file `cat` command for that selected path, and its complete output matched the inventory byte count and digest. Use an absolute path: Codex CLI 0.166.0 did not emit a working directory on its command events. Reads are unique per file per turn; this is not an invocation counter. Compound shell commands, other tools, truncated or changed content, and unselected paths cannot become matches.
- **Coverage:** a missing event is unknown. Prompt injections by the harness, implicit instructions, hosted tools, native model context, compaction and attention are outside this adapter. A matched read is not proof that the file influenced the answer. An independently supplied context claim stays a report even if its caller declared the source to be runtime-observed.
- **Identity and trust:** the operator selects the APR task and turn number; the CLI stream supplies the session identity. A different session, a changed replay at the same turn number, or identical bytes assigned a new number are rejected. The observer trusts a direct local pipe from Codex; handcrafted or edited JSONL is not an authenticated runtime trace.
- **Privacy:** the observer discards stdout, commands, unselected paths, model text and native session IDs. The store keeps relative paths, digests (including scoped identity/replay digests), sizes, gap codes and task/turn association. Codex's own session retention and the model provider's access are separate. Calling `inspect_context` in a cloud-hosted assistant sends the returned fields to that assistant; the loopback workspace suffices for local inspection.
