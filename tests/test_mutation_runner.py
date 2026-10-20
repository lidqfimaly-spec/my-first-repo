from __future__ import annotations

import unittest


from tests.mutation_runner import run_self_test


class MutationRunnerTests(unittest.TestCase):
    def test_runner_distinguishes_all_four_results(self) -> None:
        report = run_self_test()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["counts"],
            {
                "killed": 1,
                "survived": 1,
                "invalid": 1,
                "error": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
