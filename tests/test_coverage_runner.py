from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.coverage_runner import (  # noqa: E402
    CoverageEnvironmentError,
    build_shim,
    resolve_site_packages,
)


def _fake_package(root: Path) -> Path:
    package = root / "coverage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '__version__ = "0.0-test"\n', encoding="utf-8"
    )
    (package / "tracer.cpython-312-darwin.so").write_bytes(b"\x00binary")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"\x00pyc")
    return root


class CoverageRunnerTests(unittest.TestCase):
    def test_resolve_site_packages_accepts_venv_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            venv = Path(temp_dir)
            site = venv / "lib" / "python3.14" / "site-packages"
            _fake_package(site)
            self.assertEqual(resolve_site_packages(venv), site)

    def test_resolve_site_packages_rejects_missing_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CoverageEnvironmentError):
                resolve_site_packages(Path(temp_dir))

    def test_shim_drops_compiled_artifacts_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            site = root / "site-packages"
            _fake_package(site)
            shim = build_shim(site, root / "shim")
            copied = shim / "coverage"

            self.assertFalse(list(copied.glob("*.so")))
            self.assertFalse((copied / "__pycache__").exists())
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import coverage; print(coverage.__version__)",
                ],
                capture_output=True,
                text=True,
                env={
                    "PYTHONPATH": str(shim),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": "/usr/bin:/bin",
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "0.0-test")

    def test_build_shim_rejects_non_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "coverage").mkdir(parents=True)
            with self.assertRaises(CoverageEnvironmentError):
                build_shim(root, root / "shim")


if __name__ == "__main__":
    unittest.main()
