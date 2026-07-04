"""Guard against 3.12-only syntax creeping into the codebase.

pyproject.toml declares `requires-python = ">=3.11"`, but PEP 701 relaxed
f-string grammar (nested quotes reusing the outer quote char) is only valid
starting Python 3.12.

Note: `ast.parse(source, feature_version=(3, 11))` does NOT catch this —
verified empirically. CPython's tokenizer applies PEP 701's relaxed f-string
rules unconditionally from 3.12 onward; `feature_version` only gates a
handful of grammar-level checks (match statement, except*, type params,
etc.), not the f-string tokenizer itself. A file with reused f-string quotes
parses "successfully" under `ast.parse(..., feature_version=(3, 11))` even
when actually running on a 3.12/3.13 interpreter (this repo's dev venv is
3.13), which would make that approach a silent no-op guard.

So this test does the only thing that's actually reliable: it hands every
`tiro/**/*.py` file to a real Python 3.11 interpreter (via `uv run --python
3.11`) and asks IT to `ast.parse` the source. That interpreter's own
tokenizer enforces pre-3.12 grammar natively — no flag needed.
"""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
TIRO_SRC = REPO_ROOT / "tiro"

_CHECK_SCRIPT = textwrap.dedent(
    """
    import ast, json
    from pathlib import Path

    errors = []
    for path in sorted(Path("tiro").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: {exc}")
    print(json.dumps(errors))
    """
)


def _python311_available() -> bool:
    try:
        result = subprocess.run(
            ["uv", "python", "find", "3.11"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.skipif(
    not _python311_available(),
    reason="No Python 3.11 interpreter available via `uv python find 3.11`",
)
def test_all_tiro_source_parses_under_python_3_11():
    result = subprocess.run(
        ["uv", "run", "--python", "3.11", "--no-project", "python", "-c", _CHECK_SCRIPT],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    errors = json.loads(result.stdout)
    assert not errors, "Files with Python 3.11-incompatible syntax:\n" + "\n".join(errors)
