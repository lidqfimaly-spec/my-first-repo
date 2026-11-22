"""Guards for the static workbench assets.

These exist because a syntax error in ``app.js`` does not fail any Python
test: the server keeps serving bytes, every endpoint keeps working, and the
user sees a blank page. That is a whole-product failure with no red test,
so it needs a guard that actually parses the file.

The i18n check is the other half: a ``ui("...")`` call whose key is missing
from ``i18n.zh-CN.json`` renders as a raw key to the user. Static, cheap, and
it catches the "added a control, forgot its words" case.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "src" / "zevorinth" / "workbench_static"


class JavaScriptSyntaxTests(unittest.TestCase):
    def test_app_js_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; syntax check unavailable")
        completed = subprocess.run(
            [node, "--check", str(STATIC / "app.js")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "app.js does not parse, so the workbench UI cannot load:\n"
            + completed.stderr.strip(),
        )

    def test_static_assets_do_not_use_inner_html(self) -> None:
        source = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("outerHTML", source)
        self.assertNotIn("document.write", source)


class TranslationCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.i18n = json.loads(
            (STATIC / "i18n.zh-CN.json").read_text(encoding="utf-8")
        )

    def test_every_ui_key_used_by_the_app_exists(self) -> None:
        used = set(re.findall(r'ui\("([a-z0-9_]+)"', self.source))
        missing = sorted(key for key in used if key not in self.i18n["ui"])
        self.assertEqual(
            missing,
            [],
            "these ui() keys have no zh-CN text and would render raw: "
            f"{missing}",
        )

    def test_every_error_code_the_server_can_send_has_words(self) -> None:
        """Codes raised by the service layer must be displayable."""
        codes = set()
        for path in (PROJECT_ROOT / "src" / "zevorinth").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            codes.update(
                re.findall(
                    r'(?:HostAuthorizationError|WorkspaceError|'
                    r'TestExecutionError|ControlActionError|WorkbenchError)\('
                    r'\s*"([a-z0-9_]+)"',
                    text,
                )
            )
        known = set(self.i18n["errors"])
        missing = sorted(code for code in codes if code not in known)
        self.assertEqual(
            missing,
            [],
            "these error codes have no zh-CN text: " + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
