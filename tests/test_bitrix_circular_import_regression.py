"""Regression test for a real production defect: a circular import between
``integrations.bitrix.fixture_adapter`` and ``integrations.activation.service``.

Production symptom (Railway container, LIVE config present)::

    python -c "from integrations.bitrix.config import load_bitrix_config; \
print(load_bitrix_config().safe_metadata())"

    ImportError: cannot import name 'AsproFixtureAdapter' from partially
    initialized module 'integrations.bitrix.fixture_adapter' (most likely
    due to a circular import)

Root cause: ``integrations/bitrix/__init__.py`` imports
``integrations.bitrix.fixture_adapter``, which imports
``integrations.activation.adapters`` -- forcing the whole
``integrations.activation`` package (and therefore
``integrations/activation/service.py``) to finish loading before
``integrations.bitrix.fixture_adapter`` itself has finished. Because
``integrations/activation/service.py`` used to import
``AsproFixtureAdapter``/``BitrixFixtureAdapter``/``LiveBitrixAdapter`` from
``integrations.bitrix`` at *module scope*, it reached back into the
still-partially-initialized ``integrations.bitrix.fixture_adapter`` module,
which raised ``ImportError``.

These import-order bugs are invisible to a plain ``import`` inside an
already-running pytest process (other test modules have usually already
warmed ``sys.modules`` in a safe order), so this regression test spawns a
fresh interpreter for each entrypoint -- exactly like a first-touch
production process boot -- to actually exercise the cold-import path.
"""

from __future__ import annotations

import subprocess
import sys
import unittest


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=__import__("os").path.dirname(__import__("os").path.dirname(__file__)),
        capture_output=True,
        text=True,
        timeout=30,
    )


class BitrixCircularImportRegressionTests(unittest.TestCase):
    def test_exact_production_repro_command_succeeds(self):
        """The exact command executed in the failing Railway container."""
        proc = _run(
            "from integrations.bitrix.config import load_bitrix_config; "
            "print(load_bitrix_config().safe_metadata())"
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)
        self.assertIn("mode", proc.stdout)

    def test_bitrix_fixture_adapter_as_cold_first_import(self):
        proc = _run("from integrations.bitrix.fixture_adapter import AsproFixtureAdapter, BitrixFixtureAdapter")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)

    def test_bitrix_live_adapter_as_cold_first_import(self):
        proc = _run("from integrations.bitrix.live_adapter import LiveBitrixAdapter")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)

    def test_bitrix_package_as_cold_first_import(self):
        proc = _run("import integrations.bitrix")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)

    def test_activation_service_as_cold_first_import(self):
        proc = _run(
            "from integrations.activation.service import IntegrationActivationService; "
            "svc = IntegrationActivationService(); "
            "assert 'bitrix' in svc._adapters and 'aspro' in svc._adapters"
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)

    def test_activation_package_as_cold_first_import(self):
        proc = _run("import integrations.activation")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNotIn("circular import", proc.stderr)


if __name__ == "__main__":
    unittest.main()
