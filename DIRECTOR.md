# Bounded role/model director (opt-in)

This mode extends the existing CLI-worker execution primitives. It does **not**
replace `team.py`, change its defaults, or reinterpret any previous benchmark.
It is not a full OMP implementation and does not promise better performance.

## What changes

The Codex head can choose **role + configured model + task count**, then choose
its next action after receiving real results. There is no fixed mandatory
research/implement/review pipeline and no separate routing model.

```text
Codex head (same session throughout)
  -> finish a trivial known-location task directly, OR
  -> select 1..3 bounded tasks with operator-allowed profiles
       research / implement / review
       Codex OR OpenCode, including mixed profiles in one stage
  -> inspect returned evidence, then choose:
       another stage / one continuation per worker / final report
```

For example, the head may select OpenCode GLM for research, receive file/line
evidence, select DeepSeek for implementation, and later select a Codex worker
for a separate review. It can also choose just one implementer and do the final
review itself. No model-family capability ranking is hard-coded.

**Codex workers are separate CLI child sessions, not native Codex subagents.**
Native agent spawning remains disabled by the existing worker runner. This mode
adds the requested Sol/Codex worker choice without asserting a separate quota
pool, introducing untracked descendants, or requiring Claude.

## Configure and run

Python 3.11+, Git, the existing authenticated Codex CLI, and this repository's
`team.py`, `worker.py`, and `opencode_backend.py` are required. OpenCode is needed
only for enabled OpenCode profiles. No extra Python dependencies are added.

1. Copy `examples/director-profiles.json` to a local configuration file.
2. The example enables only the existing repository's Sol model. Check that this
   model is available to your account before spending tokens. It is an example,
   not an assertion that every account can use it.
3. For GLM/DeepSeek, inspect **your** `opencode models` output, replace the disabled
   example IDs with exact `provider/model` IDs, set the supported variant, and
   enable the desired profiles. An empty OpenCode `effort` omits the variant.
   An enabled `REPLACE` placeholder fails validation before any model call.
4. Save a self-contained UTF-8 request with constraints and acceptance criteria.
   Keep credentials out of the profile and request files.

```sh
python director.py request.txt \
  --cwd /path/to/target-repository \
  --profiles /private/director-profiles.json \
  --main-model gpt-6.1-sol --effort high \
  --max-stages 3 --max-workers 6 --concurrency 3 \
  --output /private/worker-runs/director-001
```

`--output` must be a new directory **outside** the target worktree. `--codex` and
`--opencode` select executable paths; the model is never allowed to supply them.
No API key is requested or copied by this mode. It uses the same CLI credentials
and environment as the existing runner. It never automatically commits/pushes.

The installed `cli-worker-team` skill and its launcher still run the original
`team.py` workflow. Run `director.py` explicitly from this checkout to opt in;
this PR does not silently change an already installed skill or install globals.

## Decisions and budgets

`profiles` is an operator-owned allowlist (up to eight entries). Only enabled
profiles are shown to the head. Each has an exact backend/model/effort, allowed
roles, and a short operator description. No learned capability claims are
inferred from GLM, DeepSeek, or Sol names.

The head returns exactly one action:

- `tasks`: a new stage with up to three task IDs, roles, profiles, scopes,
  self-contained prompts, and observable acceptance criteria;
- `followups`: existing task IDs and concrete correction/verification requests;
- `report`: final `completed`, `blocked`, or `failed` with evidence/limitations.

Default limits are three stages, six total worker-start **budget slots**, and
three simultaneous workers. A task's correction is allowed once, in the same
session/backend/model/role/scope. All proposed continuations are preflighted
before the first correction starts. The number of head decisions is also
bounded: `max_stages + max_workers + 1`. Each CLI call has a timeout (600 seconds
by default). These bound host-scheduled work, not a guaranteed dollar/token cap.

A valid, safely finished `blocked` task can be explicitly escalated to another
allowed profile. A new task must name the old ID in `replaces` and preserve its
role, scope, and acceptance criteria. It consumes a new stage and start slot.
A runtime/authentication/quota failure, missing usage, invalid session, or scope
violation instead stops the run. There is no automatic fallback/retry after such
failures. Replaced tasks and their original usage stay in the audit trail.

A final `completed` cannot hide unresolved non-replaced blocked tasks. The head
still has to inspect the relevant code and original acceptance criteria: worker
claims and passing command exit codes alone do not establish correctness.

## Execution and safety boundaries

The new adapter reuses `team.invoke`, `worker.execute_task`, `worker.resume_task`,
`worker.load_tasks`, the existing conflict predicate, root lock, scope audit,
structured report validation, and backend permission/event adapters.

All initial scopes are validated before launching any worker. Disjoint tasks
can run concurrently even across different profiles. A reader waits for an
overlapping writer; overlapping writes retain input order. Logical dependencies
between different files cannot be inferred from paths: the head must schedule
those in successive stages, after receiving upstream results.

Mixed OpenCode models still share a session store. The existing no-model
initialization command is run once before two or more OpenCode workers can start
concurrently. Initialization failure launches no workers.

There are no per-worker worktrees or automatic merges. The existing scope audit
is **not** a file-level OS security boundary. OpenCode permissions are tool
permissions, not a full OS sandbox; the inherited limitations for ignored files,
symlinks, writes outside the repository, and concurrent attribution remain.
The initial trivial/direct head turn has workspace-write permission. After
that, head invocations use the existing read-only review path. Head behavioral
limits such as not running tests or starting nested CLIs also remain prompt
policies, not new OS-level guarantees.

No Jev, always-on advisor, permanent reviewer, arbitrary model generation, native
subagent tree, autonomous background service, or automatic model benchmarking is
added. Larger teams and unrestricted stage loops are intentionally excluded.

## Evidence and usage

Read `summary.json` first. It records final status, unresolved errors, latest
head usage, per-profile worker usage, reservations, corrections, replacements,
and individual invocation entries. It preserves `null` for unknown usage, also
when a dispatch/correction is interrupted before returning. Reserved start
slots are not a claim that every corresponding model invocation actually began.

The head uses its last **session-cumulative** counter, never a sum of head turns.
Worker continuations use the existing runner's per-invocation usage delta
handling. Per-profile/model counters remain separate. The raw `worker_usage`
sum is diagnostic only; different tokenizers, cached inputs, and pricing are not
comparable units of useful work. **None of these counters measures ChatGPT's
five-hour allowance or proves cost savings.**

Each head turn has prompt/schema/command/execution artifacts. Each stage has
per-worker reports, events, patches, and session records. `history/summary.json`
holds the full accumulated report; the head handoff uses the existing 6,000-byte
limit and a disk-reference fallback, preferring newer results. Source code may
appear in these files: keep runtime output private and outside version control.

## Verification and rollout

```sh
python -m unittest discover -s tests -p test_director.py -v
python -m unittest discover -s tests -v
```

The added deterministic tests use fake model/CLI responses. They exercise
allowlists, roles, staged decisions, mixed routing, conflict ordering,
concurrency, store initialization, continuation limits, explicit escalation,
fail-closed behavior, and usage accounting. They do **not** measure live model
quality, actual provider availability, or subscription savings.

The previous single-model `team.py` remains the baseline and rollback path.
Use a separate, explicitly authorized paid smoke test before making this mode
the default. A later matched `solo / legacy team / director` evaluation should
include quality, head and Codex-child usage, external model cost, elapsed time,
and the controller overhead; do not reuse the old 44.7%/47.9% results as evidence
for this new routing policy.
