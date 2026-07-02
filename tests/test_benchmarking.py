from __future__ import annotations

import sys
import unittest
from pathlib import Path

from joiny_mnemonic.benchmarking import BenchmarkWorkload, run_benchmark


class BenchmarkHarnessTest(unittest.TestCase):
    def test_real_subprocess_benchmark_enforces_profit_and_recovery_gates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (
            "print('\\n'.join([f'test_case_{i:04d} PASSED' for i in range(250)] + "
            "['250 passed in 1.00s']))"
        )
        report = run_benchmark(
            root,
            reduction_repetitions=5,
            prompt_exposures=2,
            workloads=[
                BenchmarkWorkload(
                    "harness-real-process",
                    (sys.executable, "-c", script),
                    "test",
                )
            ],
        )
        self.assertTrue(report["passed"], report["gates"])
        self.assertGreater(report["aggregate"]["tokens_saved_per_exposure"], 0)
        self.assertEqual(report["aggregate"]["critical_signal_recall"], 1.0)
        self.assertEqual(report["aggregate"]["exact_source_recovery_rate"], 1.0)
        self.assertGreaterEqual(report["aggregate"]["storage_overhead_bytes"], 0)
        self.assertLess(report["aggregate"]["hook_counter_append_ms_p95"], 25.0)
        self.assertTrue(report["aggregate"]["hook_counter_cumulative_exact"])
        self.assertTrue(report["aggregate"]["hook_counter_replay_idempotent"])


if __name__ == "__main__":
    unittest.main()