# Independent Codex rejudge, 2026-07-17

Separate GPT-5.4 review of the 28 cases in Claude's manual census audit. The
review used a frozen snapshot of the deep-census evidence so concurrent v3
regeneration could not change inputs during judging.

## Result

| Independent verdict | n |
|---|---:|
| reader | 15 |
| passage absent | 8 |
| uncertain | 5 |

Agreement with Claude: 21/28. Disagreement: 7/28.

The claimed `11/11 passage:yes are reader failures` does not survive the
independent review:

- 7 reader;
- 4 uncertain because the packed evidence conflicts with, or does not cleanly
  support, the reference answer.

The original 17 `passage:no` cases split into:

- 8 reader;
- 8 passage absent;
- 1 uncertain.

## Disputed cases

- `dd2973ad`: the two relative dates refer to different weeks; reader failure
  is not established.
- `370a8ff4`: packed dates imply about 11.5 weeks, not the reference 15.
- `gpt4_fe651585`: packed chronology appears to support Rachel rather than the
  reference Alex.
- `852ce960`: both 350k and 400k are packed without enough status information
  to select one authoritatively.
- `0a995998`: two required items are supported; the third is absent.
- `2ce6a0f2`: three events are supported; the fourth is absent.
- `gpt4_93159ced`: the packed 9 years and 4 years 3 months are sufficient for
  the subtraction; this is reader-side.

## Safe conclusion

Both answer-time and passage-selection failures are real. Temporal
ordering/arithmetic and conflicting-version handling are the strongest visible
reader-side mechanisms in this 28-case slice, but some apparent reader failures
are actually reference/evidence inconsistencies.

No global selector ceiling follows from this audit because the other 26
indeterminate census cases remain unreviewed.

Full per-case rationales are in `audit.json`.
