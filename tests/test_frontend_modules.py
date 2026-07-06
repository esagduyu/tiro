"""M2.0 pin: js/core.js exists and the node:test harness has content.

This is additive scaffolding only (see docs/plans/2026-07-05-m2-0-frontend-modules-plan.md
Task 1) — core.js is not yet loaded by any template. This pin guards against
the file being deleted/emptied or the node:test harness silently losing
coverage, same spirit as test_static_version.py's template pins.
"""

from pathlib import Path

import tiro

STATIC = Path(tiro.__file__).parent / "frontend" / "static"


def test_core_js_exists_and_exports_esc():
    core_js = STATIC / "js" / "core.js"
    assert core_js.exists(), "tiro/frontend/static/js/core.js is missing"
    text = core_js.read_text()
    assert "export function esc" in text, "core.js must export a pure esc() function"
    assert "export" in text


def test_core_js_test_harness_is_non_empty():
    tests_dir = STATIC / "js" / "tests"
    assert tests_dir.is_dir(), "tiro/frontend/static/js/tests/ directory is missing"
    test_files = list(tests_dir.glob("*.test.mjs"))
    assert test_files, "js/tests/ must contain at least one *.test.mjs file"
    for f in test_files:
        assert f.read_text().strip(), f"{f.name} is empty"


def test_ci_runs_node_test_harness():
    ci_yml = Path(tiro.__file__).parent.parent / ".github" / "workflows" / "ci.yml"
    text = ci_yml.read_text()
    assert "node --test" in text, "ci.yml must run the node:test harness"
