"""Phase 3.75: authoritative capability registry alignment."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.capabilities import (  # noqa: E402
    CAPABILITY_REGISTRY,
    CAPABILITY_REGISTRY_VERSION,
    CAPABILITY_STATUSES,
    SUPPORTED_TASK_OPERATIONS,
    capability_manifest,
    task_operation_names,
)
from zevorinth.cli import doctor_report  # noqa: E402
from zevorinth.llm_planner import CANDIDATE_JSON_SCHEMA  # noqa: E402
from zevorinth.policy import SUPPORTED_OPERATIONS  # noqa: E402


class CapabilityRegistryTests(unittest.TestCase):
    def test_policy_and_planner_share_the_registry_operations(self) -> None:
        schema_operations = set(
            CANDIDATE_JSON_SCHEMA["properties"]["operation"]["anyOf"][0][
                "enum"
            ]
        )
        self.assertEqual(SUPPORTED_OPERATIONS, SUPPORTED_TASK_OPERATIONS)
        self.assertEqual(schema_operations, set(SUPPORTED_TASK_OPERATIONS))
        self.assertEqual(
            set(task_operation_names()), set(SUPPORTED_TASK_OPERATIONS)
        )

    def test_registry_has_only_known_statuses(self) -> None:
        entries = [
            *CAPABILITY_REGISTRY["task_operations"].values(),
            *CAPABILITY_REGISTRY["capabilities"].values(),
        ]
        self.assertGreater(len(entries), 10)
        self.assertTrue(
            all(entry["status"] in CAPABILITY_STATUSES for entry in entries)
        )
        for operation, details in CAPABILITY_REGISTRY[
            "task_operations"
        ].items():
            with self.subTest(operation=operation):
                self.assertTrue(details["implemented"])
                self.assertTrue(details["verified"])
                self.assertTrue(details["policy_allowed"])

    def test_run_tests_declares_conditions_and_limitations(self) -> None:
        run_tests = CAPABILITY_REGISTRY["task_operations"]["run_tests"]
        self.assertEqual(run_tests["status"], "conditional")
        joined = " ".join(run_tests["requirements"]).lower()
        self.assertIn("workspace", joined)
        self.assertIn("isolation evidence", joined)
        self.assertIn("container", joined)
        self.assertTrue(run_tests["limitations"])

    def test_unimplemented_capabilities_are_not_task_operations(self) -> None:
        not_available = CAPABILITY_REGISTRY["not_available_task_types"]
        self.assertEqual(
            set(not_available).isdisjoint(SUPPORTED_TASK_OPERATIONS), True
        )
        for name in not_available:
            with self.subTest(name=name):
                self.assertEqual(
                    CAPABILITY_REGISTRY["capabilities"][name]["status"],
                    "not_implemented",
                )

    def test_doctor_matches_registry_and_is_json_safe(self) -> None:
        report = doctor_report(PROJECT_ROOT)
        self.assertEqual(
            report["capability_registry_version"],
            CAPABILITY_REGISTRY_VERSION,
        )
        self.assertEqual(
            report["task_types"], task_operation_names()
        )
        self.assertEqual(
            report["capability_manifest"]["task_operations"],
            CAPABILITY_REGISTRY["task_operations"],
        )
        self.assertEqual(
            CAPABILITY_REGISTRY_VERSION,
            report["capability_manifest"]["version"],
        )
        encoded = json.dumps(report, sort_keys=True)
        self.assertIn("run_tests", encoded)
        self.assertIn("write_text_artifact", encoded)

    def test_manifest_is_detached_from_source_registry(self) -> None:
        manifest = capability_manifest()
        manifest["task_operations"]["run_tests"]["status"] = "available"
        self.assertEqual(
            CAPABILITY_REGISTRY["task_operations"]["run_tests"]["status"],
            "conditional",
        )

    def test_readme_matches_authoritative_operations(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        for operation in SUPPORTED_TASK_OPERATIONS:
            with self.subTest(operation=operation):
                self.assertIn(f"`{operation}`", readme)
        self.assertNotIn("当前只支持一种真实操作", readme)
        self.assertIn("recover-prepublish", readme)
        for unavailable in (
            "浏览器",
            "任意 shell",
            "自动恢复",
            "自动清理",
            "多机执行",
        ):
            with self.subTest(unavailable=unavailable):
                self.assertIn(unavailable, readme)

    def test_prepublish_recovery_is_bounded_and_not_a_task_operation(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "prepublish_write_recovery"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertNotIn(
            "prepublish_write_recovery", SUPPORTED_TASK_OPERATIONS
        )
        self.assertTrue(
            any(
                "publish_lock_timeout" in item
                for item in details["requirements"]
            )
        )

    def test_live_planner_protocol_is_not_an_enabled_runner(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_live_baseline_protocol"
        ]
        self.assertEqual(details["status"], "conditional")
        self.assertTrue(details["implemented"])
        self.assertTrue(details["verified"])
        self.assertNotIn(
            "planner_live_baseline_protocol", SUPPORTED_TASK_OPERATIONS
        )
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("default-deny live planning runner", joined)
        self.assertIn("four deepseek baselines", joined)
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(report["capabilities"]["planner_live_baseline_runner"])
        self.assertFalse(report["capabilities"]["planner_live_baseline_enabled"])
        self.assertTrue(
            report["capabilities"]["planner_live_applicability_contract"]
        )

    def test_live_applicability_contract_is_not_retroactive(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_live_applicability_contract"
        ]
        self.assertEqual(details["status"], "available")
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("19 strict cases", joined)
        self.assertIn("not retroactively applicable", joined)

    def test_intent_fidelity_contract_is_registered_and_bounded(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_intent_fidelity_contract"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("not general natural-language entailment", joined)
        self.assertIn("not retroactively applicable", joined)
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["planner_intent_fidelity_contract"]
        )

    def test_intent_fidelity_gate_precedes_llm_plan_acceptance(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_intent_fidelity_gate"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn("llm_planner", details["access"])
        self.assertIn("service:submit", details["access"])
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["planner_intent_fidelity_gate"]
        )

    def test_text_precision_contract_and_gate_are_registered(self) -> None:
        contract = CAPABILITY_REGISTRY["capabilities"][
            "planner_text_precision_contract"
        ]
        self.assertEqual(contract["status"], "available")
        self.assertTrue(contract["verified"])
        joined = " ".join(contract["limitations"]).lower()
        self.assertIn("defaults to semantic", joined)
        self.assertIn("do not trim", joined)

        gate = CAPABILITY_REGISTRY["capabilities"][
            "planner_text_precision_gate"
        ]
        self.assertEqual(gate["status"], "available")
        self.assertIn("llm_planner", gate["access"])
        self.assertTrue(
            any("fail-closed" in item.lower() for item in gate["requirements"])
        )
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["planner_text_precision_contract"]
        )
        self.assertTrue(report["capabilities"]["planner_text_precision_gate"])

    def test_text_precision_execution_is_registered_and_bounded(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_text_precision_execution"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn("python_api", details["access"])
        self.assertTrue(
            any("readback" in item.lower() for item in details["requirements"])
        )
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("scripted candidates", joined)
        self.assertIn("temporary workspace", joined)
        self.assertNotIn(
            "planner_text_precision_execution",
            SUPPORTED_TASK_OPERATIONS,
        )
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["planner_text_precision_execution"]
        )

    def test_read_only_workbench_is_registered_without_execution_controls(
        self,
    ) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "local_read_only_workbench"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        self.assertIn("cli:workbench", details["access"])
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("no task submission", joined)
        self.assertIn("no remote hosting", joined)
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["local_read_only_workbench"]
        )
        self.assertTrue(report["capabilities"]["web_ui"])
        self.assertFalse(report["capabilities"]["interactive_web_ui"])
        web = CAPABILITY_REGISTRY["capabilities"]["web_ui"]
        self.assertEqual(web["status"], "conditional")
        self.assertIn("read-only operator surface", web["limitations"][0])

    def test_live_evaluation_metric_contract_is_registered(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "live_evaluation_integration_contract"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("does not authorize", joined)
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"][
                "live_evaluation_integration_contract"
            ]
        )

    def test_planner_precision_regression_is_registered(self) -> None:
        details = CAPABILITY_REGISTRY["capabilities"][
            "planner_precision_regression"
        ]
        self.assertEqual(details["status"], "available")
        self.assertTrue(details["verified"])
        joined = " ".join(details["limitations"]).lower()
        self.assertIn("offline scripted regression", joined)
        report = doctor_report(PROJECT_ROOT)
        self.assertTrue(
            report["capabilities"]["planner_precision_regression"]
        )


if __name__ == "__main__":
    unittest.main()
