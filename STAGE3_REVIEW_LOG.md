# Stage 3 independent review log

## Paused after review of `ad651fa` (2026-07-26)

Original status: **REJECT**. Both findings are now remediated in the working
tree and await a new independent review. Stage 3 step 2 remains blocked until
that review returns **APPROVE**.

### P1: late closure uses end-of-parent context

Location: `scripts/stage3_surface_audit.py:616`.

The closure inherits `context_at(parent)` for the end of the outer function.
A raw write performed by a closure before a later alias shadow is therefore
missed.

```python
before = """def outer(service):
    ga = getattr
    def inner():
        ga(service, 'store').future_write()
    inner()
    ga = lambda *args: None
"""
```

Actual: `calls_in_source(before, path="fixture.py") == ()`.
Required: a non-empty violation, because `inner()` executes before `ga` is
shadowed.

### P1: conditional binding is treated as unconditional

Locations: `scripts/stage3_surface_audit.py:132` and the prefix construction
around `scripts/stage3_surface_audit.py:587-599`.

Flattened binding events apply assignments in conditional branches as though
they always execute. This erases a possible builtin-reflection path.

```python
branch = """def route(service, cond):
    ga = getattr
    if cond:
        ga = lambda *args: None
    ga(service, 'store').future_write()
"""
```

Actual: `calls_in_source(branch, path="fixture.py") == ()`.
Required: a non-empty violation, because the raw write executes when
`cond == False`.

### Evidence already collected

- Review target: `ad651fa` (`Fix reflection binding order in stage 3 audit`).
- Audit tests: 11/11 PASS.
- Linked focused suite: 24/24 PASS.
- Raw-write inventory: exactly 16 entries.
- Stage 1 gates: PASS.
- The fixed order cases behave correctly: assignment then builtin import is
  detected; builtin import then foreign assignment/import is not a false
  positive.
- The reviewer made no edits or commits.

Next action after restart: fix both P1 findings together, add runnable
regression tests, run the required suites/gates/inventory, commit the fix, and
send the new commit to a fresh independent reviewer. Do not start Stage 3 step
2 until that reviewer returns `APPROVE`.

## Remediation pending review

- Alias and value bindings now advance together in source order.
- Conditional binding events conservatively merge pre-branch and branch
  states, so a possible unshadowed path cannot disappear.
- Conflicting possible reflection aliases and binding kinds are retained as
  sets of alternatives instead of selecting one representative path.
- Nested functions merge end-of-parent state with each direct call-site state;
  a call before later shadowing is visible while a function called only after
  shadowing remains a negative control.
- Conditional ancestry is cached once per lexical scope, and state merging is
  limited to binding events. The focused audit suite completes in about 23
  seconds rather than the initial 176-second unoptimised implementation.
- Regression tests contain both reviewer reproducers plus the negative
  call-after-shadow control.

Verification before commit:

- audit suite: 12/12 PASS;
- linked focused suite: 25/25 PASS;
- raw-write inventory: exactly 16 entries;
- Stage 1 gates: PASS;
- `git diff --check`: PASS.
