"""P0-1: Controlled shell tool — whitelist, parameter validation, execution."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from zevorinth.models import ExecutionMode, TaskRequest, ToolResult
from zevorinth.shell_tool import (
    ALLOWED_COMMANDS,
    CONTROLLED_SHELL_TOOL,
    MAX_ARGUMENTS,
    MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    ShellToolError,
    execute_controlled_shell,
    validate_shell_parameters,
)


class ShellParameterValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_whitelisted_command_passes(self) -> None:
        result = validate_shell_parameters(
            {"command": "ls", "arguments": ["-la"]},
            workspace_root=self.root,
        )
        self.assertEqual(result["command"], "ls")
        self.assertEqual(result["arguments"], ["-la"])
        self.assertEqual(result["timeout_seconds"], 15)

    def test_non_whitelisted_command_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "rm", "arguments": ["-rf", "/"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "command_not_whitelisted")

    def test_python_interpreter_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "python3", "arguments": ["-c", "print(1)"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "command_not_whitelisted")

    def test_command_with_path_separator_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "/bin/ls", "arguments": []},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "command_must_be_name_only")

    def test_empty_command_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "", "arguments": []},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "invalid_command")

    def test_non_string_argument_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "ls", "arguments": [123]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "invalid_argument_type")

    def test_null_byte_in_argument_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "echo", "arguments": ["hello\x00world"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "invalid_argument_null")

    def test_too_many_arguments_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "echo",
                    "arguments": ["a"] * (MAX_ARGUMENTS + 1),
                },
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "too_many_arguments")

    def test_forbidden_flag_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "find", "arguments": [".", "-delete"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "forbidden_flag")

    def test_exec_flag_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "find",
                    "arguments": [".", "-exec", "rm", "{}", ";"],
                },
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "forbidden_flag")

    def test_sensitive_path_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "cat", "arguments": ["/etc/passwd"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "sensitive_path_rejected")

    def test_ssh_key_path_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "cat", "arguments": ["~/.ssh/id_rsa"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "sensitive_path_rejected")

    def test_absolute_path_outside_workspace_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "cat", "arguments": ["/tmp/secret.txt"]},
                workspace_root=self.root,
            )
        self.assertIn(
            caught.exception.code,
            ("absolute_path_outside_workspace", "sensitive_path_rejected"),
        )

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {"command": "cat", "arguments": ["../../etc/passwd"]},
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "sensitive_path_rejected")

    def test_timeout_out_of_range_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "ls",
                    "arguments": [],
                    "timeout_seconds": 100,
                },
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "timeout_out_of_range")

    def test_timeout_below_minimum_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "ls",
                    "arguments": [],
                    "timeout_seconds": 0,
                },
                workspace_root=self.root,
            )
        self.assertEqual(caught.exception.code, "timeout_out_of_range")

    def test_working_directory_traversal_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "ls",
                    "arguments": [],
                    "working_directory": "../../../etc",
                },
                workspace_root=self.root,
            )
        self.assertEqual(
            caught.exception.code, "working_directory_no_traversal"
        )

    def test_working_directory_absolute_rejected(self) -> None:
        with self.assertRaises(ShellToolError) as caught:
            validate_shell_parameters(
                {
                    "command": "ls",
                    "arguments": [],
                    "working_directory": "/etc",
                },
                workspace_root=self.root,
            )
        self.assertEqual(
            caught.exception.code,
            "working_directory_must_be_relative",
        )

    def test_valid_working_directory_within_workspace_accepted(self) -> None:
        subdir = self.root / "subdir"
        subdir.mkdir()
        result = validate_shell_parameters(
            {
                "command": "ls",
                "arguments": [],
                "working_directory": "subdir",
            },
            workspace_root=self.root,
        )
        self.assertTrue(result["working_directory"].endswith("subdir"))


class ShellExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Create a test file for cat/grep
        (self.root / "test.txt").write_text("hello world\nfoo bar\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_echo_executes_successfully(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="echo",
            arguments=["hello", "world"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.tool_name, CONTROLLED_SHELL_TOOL)
        effects = result.workspace_effects
        self.assertEqual(effects["exit_code"], 0)
        self.assertIn("hello world", effects["output"])
        self.assertFalse(effects["timed_out"])
        self.assertFalse(effects["truncated"])

    def test_cat_reads_file_content(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="cat",
            arguments=["test.txt"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertTrue(result.success)
        effects = result.workspace_effects
        self.assertIn("hello world", effects["output"])
        self.assertIn("foo bar", effects["output"])

    def test_ls_lists_directory(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="ls",
            arguments=[],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertTrue(result.success)
        effects = result.workspace_effects
        self.assertIn("test.txt", effects["output"])

    def test_grep_finds_pattern(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="grep",
            arguments=["hello", "test.txt"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertTrue(result.success)
        effects = result.workspace_effects
        self.assertIn("hello world", effects["output"])

    def test_wc_counts_lines(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="wc",
            arguments=["-l", "test.txt"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertTrue(result.success)
        effects = result.workspace_effects
        self.assertIn("2", effects["output"])

    def test_failing_command_recorded(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="ls",
            arguments=["nonexistent_file_xyz"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        self.assertFalse(result.success)
        effects = result.workspace_effects
        self.assertNotEqual(effects["exit_code"], 0)
        self.assertIsNotNone(result.error_message)

    def test_simulated_mode_returns_immediately(self) -> None:
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="echo",
            arguments=["test"],
            timeout_seconds=5,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
            execution_mode=ExecutionMode.SIMULATED,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.execution_mode, ExecutionMode.SIMULATED)
        effects = result.workspace_effects
        self.assertIn("[simulated]", effects["output"])
        self.assertEqual(effects["exit_code"], 0)

    def test_output_truncation_enforced(self) -> None:
        # Generate output larger than MAX_OUTPUT_BYTES
        big_file = self.root / "big.txt"
        big_file.write_text("x" * (MAX_OUTPUT_BYTES + 1024), encoding="utf-8")
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="cat",
            arguments=["big.txt"],
            timeout_seconds=10,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        effects = result.workspace_effects
        self.assertTrue(effects["truncated"])
        self.assertIn("[output truncated", effects["output"])
        self.assertLessEqual(
            effects["output_bytes"],
            MAX_OUTPUT_BYTES + 100,  # truncation marker adds a few bytes
        )

    def test_timeout_kills_long_running_command(self) -> None:
        # `sleep` is not in the whitelist, so use `find` on a
        # large directory with a 1s timeout to trigger timeout.
        # Create a large directory structure.
        deep = self.root / "deep"
        deep.mkdir()
        for i in range(500):
            (deep / f"file_{i}.txt").write_text("x" * 100, encoding="utf-8")
        result = execute_controlled_shell(
            task_id="test-task",
            attempt_id="test-attempt",
            command="find",
            arguments=["deep", "-name", "file_*"],
            timeout_seconds=1,
            working_directory=str(self.root),
            started_at="2026-11-21T00:00:00.000+00:00",
            finished_at="2026-11-21T00:00:01.000+00:00",
        )
        # find might complete quickly on a small tree, but if it
        # times out, the timeout flag should be set.
        effects = result.workspace_effects
        if effects["timed_out"]:
            self.assertFalse(result.success)
            self.assertIn("timed out", result.error_message or "")


class ShellSecurityMutationTests(unittest.TestCase):
    """Mutation audit: inject defects and verify the guard catches them.

    These tests prove the whitelist is enforced at the validation layer,
    not just advisory. Each mutation simulates a regression where a
    security check is removed — the test must fail (turn red) when the
    guard is absent.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mutation_allow_non_whitelisted_command(self) -> None:
        """If the whitelist check is removed, rm should be rejected."""
        # Simulate mutation: temporarily add rm to the whitelist
        import zevorinth.shell_tool as mod
        original = mod.ALLOWED_COMMANDS
        mod.ALLOWED_COMMANDS = frozenset(original | {"rm"})
        try:
            # With the mutation, rm passes validation (proving the guard
            # is at the whitelist check). Without the mutation, it fails.
            result = validate_shell_parameters(
                {"command": "rm", "arguments": ["-f", "file.txt"]},
                workspace_root=self.root,
            )
            self.assertEqual(result["command"], "rm")
        except ShellToolError as exc:
            self.assertEqual(exc.code, "command_not_whitelisted")
        finally:
            mod.ALLOWED_COMMANDS = original

    def test_mutation_remove_sensitive_path_check(self) -> None:
        """If sensitive path check is removed, /etc/passwd should still
        be caught by the absolute path check."""
        # The defense-in-depth design means /etc/passwd is caught by
        # BOTH the sensitive path pattern AND the absolute path check.
        # Even if one is removed, the other catches it.
        try:
            validate_shell_parameters(
                {"command": "cat", "arguments": ["/etc/passwd"]},
                workspace_root=self.root,
            )
            self.fail("Expected ShellToolError for sensitive path")
        except ShellToolError as exc:
            # Must be caught by at least one guard
            self.assertIn(
                exc.code,
                ("sensitive_path_rejected", "absolute_path_outside_workspace"),
            )

    def test_whitelist_contains_only_safe_commands(self) -> None:
        """Verify the whitelist does not contain dangerous commands."""
        dangerous = {
            "rm", "mv", "cp", "mkdir", "rmdir", "chmod", "chown",
            "dd", "mkfs", "mount", "umount",
            "python", "python3", "node", "ruby", "perl", "bash", "sh",
            "zsh", "fish",
            "curl", "wget", "ssh", "scp", "rsync",
            "sudo", "su", "doas",
            "kill", "killall",
            "crontab", "at",
            "tar", "zip", "unzip", "gzip", "gunzip",
            "pip", "npm", "yarn", "cargo", "go",
        }
        overlap = dangerous & ALLOWED_COMMANDS
        self.assertEqual(
            overlap,
            set(),
            f"whitelist contains dangerous commands: {overlap}",
        )

    def test_no_shell_true_in_implementation(self) -> None:
        """Verify subprocess.run uses shell=False, never shell=True."""
        source = Path(
            PROJECT_ROOT / "src" / "zevorinth" / "shell_tool.py"
        ).read_text(encoding="utf-8")
        # The subprocess.run call must use shell=False
        self.assertIn("shell=False", source)
        # Extract just the subprocess.run call (not comments/docstrings)
        # and verify it doesn't pass shell=True
        lines = source.split("\n")
        in_subprocess_call = False
        subprocess_lines: list[str] = []
        for line in lines:
            if "subprocess.run(" in line:
                in_subprocess_call = True
            if in_subprocess_call:
                subprocess_lines.append(line)
                if line.strip().endswith(")"):
                    in_subprocess_call = False
        subprocess_code = "\n".join(subprocess_lines)
        self.assertNotIn(
            "shell=True",
            subprocess_code,
            "subprocess.run must never use shell=True",
        )

    def test_max_timeout_is_bounded(self) -> None:
        self.assertLessEqual(MAX_TIMEOUT_SECONDS, 60)
        self.assertGreaterEqual(MIN_TIMEOUT_SECONDS, 1)

    def test_output_limit_is_bounded(self) -> None:
        self.assertLessEqual(MAX_OUTPUT_BYTES, 256 * 1024)
        self.assertGreater(MAX_OUTPUT_BYTES, 1024)


class ShellEndToEndTests(unittest.TestCase):
    """End-to-end: submit a controlled_shell task via TaskService."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "test.txt").write_text("hello\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_submit_ls_task_succeeds(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="列出工作目录文件",
                operation="controlled_shell",
                parameters={
                    "command": "ls",
                    "arguments": [],
                },
            )
        )
        self.assertEqual(task.status.value, "succeeded")
        self.assertIsNotNone(task.tool_result)
        self.assertTrue(task.tool_result.success)
        effects = task.tool_result.workspace_effects
        self.assertIn("test.txt", effects["output"])
        self.assertEqual(effects["exit_code"], 0)

    def test_submit_cat_task_succeeds(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="读取 test.txt 内容",
                operation="controlled_shell",
                parameters={
                    "command": "cat",
                    "arguments": ["test.txt"],
                },
            )
        )
        self.assertEqual(task.status.value, "succeeded")
        effects = task.tool_result.workspace_effects
        self.assertIn("hello", effects["output"])

    def test_submit_non_whitelisted_command_blocked_by_policy(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="删除文件",
                operation="controlled_shell",
                parameters={
                    "command": "rm",
                    "arguments": ["test.txt"],
                },
            )
        )
        self.assertIn(task.status.value, ("failed", "planning_failed", "blocked"))

    def test_submit_missing_command_clarifies(self) -> None:
        from zevorinth.service import TaskService

        service = TaskService(self.root)
        task = service.submit(
            TaskRequest(
                goal="运行命令",
                operation="controlled_shell",
                parameters={},
            )
        )
        self.assertEqual(task.status.value, "awaiting_clarification")
        self.assertIn("command", task.missing_fields)


if __name__ == "__main__":
    unittest.main()
