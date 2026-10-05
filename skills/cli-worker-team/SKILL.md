---
name: cli-worker-team
description: Run a Codex CLI coordinator with Codex or OpenCode workers for a Git project when the user asks to use this worker team. It assigns work, reviews results, and resumes workers once if needed. Use for requests such as "use the CLI worker team" or "$cli-worker-team"; not for every coding task or unrelated OpenCode delegation.
---

# CLI worker team

This skill launches a Python coordinator with a separate Codex CLI main session and CLI workers. Workers default to Codex; the user can select OpenCode. The current chat prepares the request and reports the outcome; it does not become that main session. The runner edits the target checkout directly and has no per-worker worktrees.

## Prepare the request

- Identify the target Git checkout from the user's request or current project. Ask only if the target is ambiguous. Check its applicable instructions and existing Git changes; preserve the user's work.
- Carry the user's goal, constraints, acceptance criteria, and relevant prior decisions into a self-contained UTF-8 request file. Include important applicable instructions that the CLI would not otherwise receive. Avoid investigating or solving the implementation before delegation.
- Use the user's model and effort choices. If unspecified, use the tested baseline: main `gpt-6.1-sol`, worker `gpt-6.1-sol`, both `high`. This baseline does not claim cheaper worker pricing. Never silently substitute a model after an access or quota failure.
- Require Python 3.11+, Git and an authenticated `codex` executable. Report missing prerequisites; do not print or copy credentials or install a different CLI without a task-related reason.
- For OpenCode workers, also require an authenticated OpenCode CLI and an explicit `provider/model` worker model. Use `--worker-backend opencode --worker-model <provider/model>`. Do not infer an OpenCode model from a Codex model name. The main always remains Codex. `--worker-effort` maps to the provider-specific OpenCode variant; an empty value omits it. `--opencode` selects its executable. Windows npm shims are resolved to the native executable when available.
- When the user names an OpenCode provider/model in natural language, query the selected executable with `opencode models` (or `opencode models <provider>` after identifying the provider). Resolve a unique match to its exact listed ID and state it before running; ask only when matches remain ambiguous or unavailable. Do not require the user to find the ID. Check configured variants when mapping an effort choice, without exposing resolved configuration or credentials. The listing confirms configuration, not working authentication; preserve and report any actual invocation failure.

## Run

Use the installed skill's `scripts/run_team.py`. It also works in the source repository. Resolve its absolute path from this SKILL.md, not the target project's current directory.

Create a unique job directory under this skill's `runs/` (or the calling task's private artifact directory), outside the target checkout. Write `request.txt` there. Pass an unused `result` subdirectory so the runner can create it itself:

```text
python <skill-dir>/scripts/run_team.py <job-dir>/request.txt --cwd <target-git-checkout> --main-model gpt-6.1-sol --worker-model gpt-6.1-sol --effort high --worker-effort high --output <job-dir>/result
```

Quote paths according to the current shell. Use the shell tool's running-session handle to wait for completion; do not run a second team against the same checkout. Each CLI call defaults to a 600-second timeout, adjustable with `--timeout` when the task needs it.

The main normally assigns one end-to-end worker, up to three for independent scopes. It reviews read-only and can resume each selected worker once, within the original scope. Do not launch native subagents or another AI CLI to recreate these steps. When already executing inside this coordinator as its main or worker, do not launch another team.

OpenCode workers receive role-specific tool permissions through runtime inline config without editing the project's config: implementation can edit and run commands; research/review cannot edit or run shell commands. These are tool permissions, not an OS sandbox. OpenCode returns a prompted JSON report that the runner validates locally; its JSON event output alone does not enforce the report schema.

Before a parallel OpenCode batch, the runner initializes the shared session store once without a model call, then starts workers in parallel. This avoids the observed fresh-store startup race in CLI 1.18.34. If initialization fails, inspect the batch's `initialization/` artifacts; no workers have started. This does not guarantee coordination with other programs independently opening the same store.

## Verify and report

- Read `<job-dir>/result/summary.json` first. Report the actual `completed`, `blocked`, or `failed` state. Exit 0 alone and passing worker tests do not prove the requested behavior is correct.
- Summarize delivered changes, observed checks, unresolved issues, and artifact locations. Inspect the relevant diff if needed; avoid repeating the worker's broad investigation or automatically rerunning its entire test suite.
- Distinguish worker claims from `observed_commands` and changed-file evidence. If reporting tokens, use summary `main_usage` and `worker_usage`; do not sum main phases or worker cumulative totals. Missing usage remains unknown. These are CLI tokens, not billing or subscription quota measurements.
- OpenCode step tokens are per invocation; the runner normalizes cache and reasoning fields and sums steps. Saved session artifacts retain the backend, model, variant, executable, and profile fingerprint for continuation. Preserve the OpenCode profile environment when resuming. Resume uses the saved backend, including old artifacts without a backend field (Codex).
- On quota, authentication, timeout, scope, or validation failures, preserve artifacts and explain what completed and what did not. Do not silently retry, expand scope, or treat a partial result as successful. A user-authorized retry uses a new job directory.
- This runner does not automatically commit or publish. Perform those actions separately only when authorized by the user's task. A new user task starts a new coordinator session; automatic conversation continuation across tasks is not implemented.
