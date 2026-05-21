"""
Regression test for the test-collection pollution bug we hit once.

Background: several test files have a `def _install_hw_mocks(): ...`
helper that swaps `sys.modules['gcode_control']` for a MagicMock so the
test can import web_server without a connected machine. Convention is
to call that helper inside setUpClass or setUp.

The bug class: if a file calls `_install_hw_mocks()` at MODULE TOP
LEVEL, the mock fires during pytest's collection phase. Other test
modules collected AFTER that file then bind `gcode_control` to the
MagicMock instead of the real module — silently breaking unrelated
tests that don't realise their gcode_control reference is fake (e.g.
test_z_bounce.py).

This test catches the bug at collection time. It scans every test_*.py
under tests/ and fails if any of them invokes _install_hw_mocks() at
module scope.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


_TESTS_DIR = Path(__file__).resolve().parent
# The conftest's central helper is part of the test infrastructure and
# must NOT be flagged — it's a function definition, not a call.
_HELPER_NAMES = {
    '_install_hw_mocks',
    'install_gcode_control_mock',
}


def _module_level_calls_to(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    """Return every top-level `Call` whose target identifier is in
    `names`. Calls inside function defs, class defs, or comprehensions
    are NOT module-level and are excluded."""
    hits = []
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        target = call.func
        # Accept both bare name and attribute access (e.g.
        # `conftest._install_hw_mocks()`).
        if isinstance(target, ast.Name) and target.id in names:
            hits.append(call)
        elif isinstance(target, ast.Attribute) and target.attr in names:
            hits.append(call)
    return hits


class HardwareMockHygieneTests(unittest.TestCase):

    def test_no_module_level_hw_mock_installs(self):
        """Walk every tests/test_*.py and fail if _install_hw_mocks()
        (or install_gcode_control_mock()) is called at module scope.

        Module-level calls fire during pytest collection, which
        installs a MagicMock into sys.modules['gcode_control']
        BEFORE other test files are imported. Tests that bind
        gcode_control during their own import then get the mock by
        accident.
        """
        offenders = []
        for path in sorted(_TESTS_DIR.glob('test_*.py')):
            if path.name == Path(__file__).name:
                continue
            try:
                source = path.read_text(encoding='utf-8')
            except Exception as exc:
                self.fail(f"Could not read {path}: {exc}")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                self.fail(f"Could not parse {path}: {exc}")
            for call in _module_level_calls_to(tree, _HELPER_NAMES):
                offenders.append(
                    f"  {path.name}:{call.lineno} — "
                    f"module-level mock install (must be inside "
                    f"setUpClass / setUp instead)"
                )

        if offenders:
            self.fail(
                "Found module-level hardware-mock installs. These "
                "fire during pytest collection and silently break "
                "test files collected later (they bind gcode_control "
                "to the MagicMock by accident):\n\n"
                + "\n".join(offenders)
                + "\n\nFix: move the call inside setUpClass or setUp."
            )


if __name__ == '__main__':
    unittest.main()
