from __future__ import annotations

import unittest


from tests.coverage_gate import THRESHOLDS, evaluate_coverage


class CoverageGateTests(unittest.TestCase):
    def test_threshold_report_passes(self) -> None:
        files = {}
        for module, (line, branch) in THRESHOLDS.items():
            files[f"src/zevorinth/{module}"] = {
                "summary": {
                    "covered_lines": 100,
                    "num_statements": 100,
                    "covered_branches": 100,
                    "num_branches": 100,
                }
            }
        result = evaluate_coverage(
            {"files": files, "meta": {"branch_coverage": True}}
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["failures"], [])

    def test_low_branch_coverage_fails(self) -> None:
        files = {}
        for module in THRESHOLDS:
            files[f"src/zevorinth/{module}"] = {
                "summary": {
                    "covered_lines": 100,
                    "num_statements": 100,
                    "covered_branches": 0,
                    "num_branches": 100,
                }
            }
        result = evaluate_coverage(
            {"files": files, "meta": {"branch_coverage": True}}
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("service.py", result["failures"])

    def test_missing_module_fails(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_coverage({})
        result = evaluate_coverage(
            {"files": {}, "meta": {"branch_coverage": True}}
        )
        self.assertEqual(result["status"], "FAIL")

    def test_line_only_report_cannot_pass_branch_gate(self) -> None:
        files = {}
        for module in THRESHOLDS:
            files[f"src/zevorinth/{module}"] = {
                "summary": {
                    "covered_lines": 100,
                    "num_statements": 100,
                    "covered_branches": 0,
                    "num_branches": 0,
                }
            }
        with self.assertRaises(ValueError):
            evaluate_coverage(
                {"files": files, "meta": {"branch_coverage": False}}
            )

    def test_zero_branch_denominator_fails_closed(self) -> None:
        files = {}
        for module in THRESHOLDS:
            files[f"src/zevorinth/{module}"] = {
                "summary": {
                    "covered_lines": 100,
                    "num_statements": 100,
                    "covered_branches": 0,
                    "num_branches": 0,
                }
            }
        result = evaluate_coverage(
            {"files": files, "meta": {"branch_coverage": True}}
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("policy.py:no_branch_data", result["failures"])


if __name__ == "__main__":
    unittest.main()
