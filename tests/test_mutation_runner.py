from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


from tests.mutation_runner import (  # noqa: E402
    _classify_result,
    _evaluate_mutation,
    run_mutations,
    run_self_test,
)


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

    def test_broken_test_selection_is_error_not_killed(self) -> None:
        result = _evaluate_mutation(
            {
                "id": "broken_selection",
                "path": "src/zevorinth/artifact_tool.py",
                "old": "from __future__ import annotations\n",
                "new": "from __future__ import annotations  # mutated\n",
                "tests": ["tests.test_module_that_does_not_exist"],
                "expected": "killed",
            }
        )
        self.assertEqual(result["result"], "error")
        self.assertNotEqual(result["result"], "killed")
        self.assertIsNotNone(result["reason"])

    def test_expected_kill_that_survives_fails_the_gate(self) -> None:
        cases = {
            "version": "test",
            "cases": [
                {
                    "id": "surviving_noop",
                    "path": "src/zevorinth/artifact_tool.py",
                    "old": "from __future__ import annotations\n",
                    "new": "from __future__ import annotations  # mutated\n",
                    "tests": [
                        "tests.test_artifact_tool.ArtifactToolTests.test_writer_refuses_overwrite"
                    ],
                    "expected": "killed",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            report = run_mutations(path)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["counts"]["survived"], 1)
        self.assertEqual(report["unexpected_results"], ["surviving_noop"])

    def test_classifier_requires_the_declared_test_in_the_output(self) -> None:
        result, reason = _classify_result(
            {
                "exit_code": 1,
                "stdout": "FAILED (errors=1)\n",
                "stderr": "",
            },
            ["tests.test_real"],
        )
        self.assertEqual(result, "error")
        self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()
