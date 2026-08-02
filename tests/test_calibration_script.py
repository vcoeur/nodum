"""The calibration measurement script stays runnable and its docstring honest.

``scripts/measure_kasten_calibration.py`` is the reference method behind the
two measured cosine bars in :mod:`nodum.consolidate` — the comments cite it as
the authority and the release uses its numbers, so a break or a silent drift
would go unnoticed without a pin. What is asserted here is the checkable
contract: the script exists, its docstring still names the bars it measures
and the vault argument it takes, it imports cleanly, and its no-provider exit
path fails with a readable reason rather than a traceback. A real embedding
run needs the model in the local cache and takes about a minute, so the exit
path is the part a test can afford — the gate confirmed it works.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_kasten_calibration.py"

#: The measure script is a standalone tool: it must run without the package's
#: test fixtures, so the repo root is the working directory for every spawn.
REPO_ROOT = SCRIPT.parent.parent


def test_the_script_exists_and_its_docstring_names_the_bars_and_the_vault_arg():
    """The docstring is the release authority the comments cite, so it is pinned."""
    assert SCRIPT.is_file()
    docstring = SCRIPT.read_text(encoding="utf-8")

    assert "measure_kasten_calibration" in docstring
    assert "consolidate" in docstring
    assert "/path/to/kasten" in docstring


def test_the_script_imports_cleanly():
    """Importing must not execute main(): the module guard is the interface."""
    program = f"import runpy; runpy.run_path({str(SCRIPT)!r}, run_name='not_main')"
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


def test_the_no_provider_exit_path_is_a_readable_refusal(tmp_path):
    """With an empty model cache the script exits 1 and says why, never a traceback.

    The empty cache is the honest stand-in for an install without the model:
    resolution refuses there with the "rerun with NODUM_EMBED_DOWNLOAD=1"
    sentence (or "fastembed is not installed" on an install without the
    extra), which is the readable contract the docstring promises.
    """
    empty_cache = tmp_path / "empty-model-cache"
    empty_cache.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "NODUM_EMBED_CACHE": str(empty_cache), "NODUM_EMBED_DOWNLOAD": ""},
        timeout=120,
    )

    assert result.returncode == 1
    assert "no embedding provider:" in result.stderr
    assert "NODUM_EMBED_DOWNLOAD" in result.stderr or "fastembed is not installed" in result.stderr
