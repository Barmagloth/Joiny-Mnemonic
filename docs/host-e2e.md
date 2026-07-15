# Host-level E2E checklist

Run this against a real host install (a project with `.joiny-mnemonic/`)
after every release-worthy change. Target: ~15 minutes per host. One-off
passes rot; this list is the repeatable form of the 2026-07-14 Claude pass.

Prereqs: the runtime venv carries the release under test
(`pip install <repo>` into `~/.joiny-mnemonic/runtime/venv`), and the live
project has hooks installed for the host being verified.

## Per-host steps

1. **Store opens on new code.** `jm capabilities` from the project root:
   no traceback; `schema_version` current; `state_maintenance` and
   `bitemporal_retrieval` blocks present. Derived-projection rebuilds (e.g.
   FTS schema changes) must be transparent — verify expected columns via a
   read-only sqlite query if the release changed them.
2. **Hooks fire.** Run one headless session in the project
   (`claude -p` / `codex exec` with a trivial prompt). Verify `MAX(seq)`
   grew and the new events carry `origin_channel=host_hook` with the right
   adapter.
3. **Injection delivered + recall.** Headless session asking a question
   whose answer lives in protected blocks (e.g. "какое решение по формату
   конфигов записано в ACTIVE MEMORY?"). Pass = the answer quotes the
   block content correctly.
4. **Authority probe (task7C).** Same session transcript: does the agent
   treat the packet as legitimate memory, or does it disclaim it as
   injected/untrusted text? Record verbatim any suspicion language — this
   is the metric for the task7 channel work, measured before/after each
   channel ships.
5. **Reconciler.** `jm reconcile`: detections consistent with the block
   state; flag-off → pending only, no block mutation; pending line present
   in `jm resume` output.
6. **Retrieval fusion.** `jm search` with a temporal cue ("что решили
   вчера про..."): top hit relevant; `fusion_ranks` present when a second
   arm is active.
7. **Reducers/telemetry.** `jm reduction-report`: families listed, no
   crash, promotion ratios sane.
8. **Host-specific silences.** Codex: PostCompact does its work with no
   stdout (schema-validated hosts reject noise). Claude Code: no
   settings-file validation warnings, hooks listed in the transcript's
   hook execution log without errors.
9. **Timing sanity (optional but cheap).**
   `joiny-mnemonic-hook-timing --project-root <repo> --assert-gates` on
   the dev machine — all gates green.

## Recording

Append a dated line per host to the table below. A failed step is a
release blocker or a documented known-issue with a TODO reference.

| Date | Release | Host | Result | Notes |
|---|---|---|---|---|
| 2026-07-14 | post-task5 (59fb5bb..e524616) | claude-code | PASS | FTS signal rebuild transparent; reconciler detected historical delme2 completion; verb-flip observed in nested paraphrase (see TODO#4) |
