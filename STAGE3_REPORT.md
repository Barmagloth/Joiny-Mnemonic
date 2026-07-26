# Stage 3 acceptance report

Date: 2026-07-26  
Authority: `ROADMAP.md`, section 6

## Outcome

Stage 3 is implemented and its local acceptance is green. CLI, MCP, HTTP and
host hooks no longer call mutating `service.store` methods directly. They use
the named application boundary, and the same active-block operation through
CLI, MCP and HTTP receives the same stored-size validation.

## Reviewed implementation commits

| Commit | Independent review | Scope |
|---|---|---|
| `f9f0949` | APPROVE | Roadmap-bounded structural surface gate |
| `09bc07c` | APPROVE | CLI/MCP/HTTP application command path |
| `553afb5` | APPROVE | Hook application command path |
| `3aa75ed` | APPROVE | Storage split and lowered complexity baseline |

The final acceptance/documentation commit is reviewed separately after it is
created, following the same commit-review-fix barrier.

## Executable evidence

- `JM-INV-003`:
  `tests.test_stage3_application_commands.ApplicationCommandsTest.test_jm_inv_003_all_public_surfaces_share_application_path`
- structural gate: `python scripts/stage3_surface_audit.py --require-clean`
- storage split:
  `python -m unittest tests.test_stage3_storage_split -v`
- contract and complexity gates: `python scripts/stage1_gates.py all`
- full suite:
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 python -m unittest discover -s tests -q`

Final results:

| Check | Result |
|---|---:|
| `JM-INV-003` exact test | 1/1 PASS |
| Stage 3 focused suite | 8/8 PASS |
| Direct mutating store calls in four surfaces | 0 |
| Contract/complexity gates | PASS |
| Full suite | 325/325 OK in 427.761 s |

The first full run exposed a deterministic test-isolation defect: the HTTP
task integration used the shared, accumulated `tests/runtime` tree when
building a snapshot and exceeded its five-second client timeout. The test now
uses a temporary empty project root. It passed three consecutive isolated
runs (~1.3 s each), and the subsequent full suite passed.

## Complexity result

`storage.py` is strictly below the immutable Stage 1 baseline in every tracked
metric:

| Metric | Original | Enforced now |
|---|---:|---:|
| Physical lines | 4847 | 4702 |
| Functions/methods | 156 | 138 |
| Classes | 4 | 1 |

`ProjectionStorageMixin` is the single SQLite owner for rebuildable retrieval
health and file-hash projections. `storage_errors.py` owns storage exception
types, while `storage.py` preserves their old import path. Both modules use the
same database connection and transaction machinery and have exact complexity
caps.

## Public compatibility and working tree

No public CLI, MCP or HTTP response format changed. The implementation commits
did not include the pre-existing user changes in `TODO.md`,
`benchmarks/results/census-latest.json`, `src/joiny_mnemonic/reducers.py`,
`task7.md`, or the pre-existing untracked history/benchmark files.
