"""Offline tests for the Gitleaks secrets/PII gate (DESIGN.md §7, §11).

Runs the real gitleaks binary with the repo's .gitleaks.toml against two
checked-in corpora:

- corpora/planted_secrets.txt — fake secrets + PII; every planted line must
  be flagged.
- corpora/clean.txt — ordinary code/diff text salted with lookalikes
  (version strings, 0.0.0.0, decorators, git remotes); nothing may be
  flagged.

This is the one part of open-seer tested offline — everything else is
validated on live surfaces (DESIGN.md §11). Skips when gitleaks is not
installed, or is too old to have the `dir` subcommand (gitleaks >= 8.19;
older releases spelled it `detect --no-git`, which newer releases removed).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / ".gitleaks.toml"
CORPORA = Path(__file__).resolve().parent / "corpora"

# Every custom rule in .gitleaks.toml must fire at least once on the planted
# corpus, or the rule is dead weight.
CUSTOM_RULE_IDS = {
    "open-seer-pii-email",
    "open-seer-pii-ipv4",
    "open-seer-pii-ipv6",
    "open-seer-pii-bearer-token",
    "open-seer-pii-auth-header",
    "open-seer-pii-cookie-header",
    "open-seer-pii-user-id",
    "open-seer-url-credentials",
}

GITLEAKS = shutil.which("gitleaks")


def _gitleaks_has_dir_subcommand() -> bool:
    if GITLEAKS is None:
        return False
    proc = subprocess.run([GITLEAKS, "dir", "--help"], capture_output=True)
    return proc.returncode == 0

pytestmark = pytest.mark.skipif(
    not _gitleaks_has_dir_subcommand(),
    reason="gitleaks binary with the `dir` subcommand (>= 8.19) not available",
)


def run_gitleaks(corpus: Path) -> tuple[int, list[dict]]:
    """Scan a single corpus file; return (exit code, findings from the JSON report)."""
    proc = subprocess.run(
        [
            GITLEAKS,
            "dir",
            str(corpus),
            "--config",
            str(CONFIG),
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            "-",  # report on stdout; logs go to stderr
        ],
        capture_output=True,
        text=True,
    )
    # 0 = clean, 1 = leaks found; anything else is a config/usage error.
    assert proc.returncode in (0, 1), f"gitleaks failed unexpectedly:\n{proc.stderr}"
    return proc.returncode, json.loads(proc.stdout)


def planted_lines(corpus: Path) -> list[int]:
    """1-based numbers of every non-comment, non-blank line in a corpus."""
    return [
        lineno
        for lineno, line in enumerate(corpus.read_text().splitlines(), start=1)
        if line.strip() and not line.strip().startswith("#")
    ]


def test_every_planted_line_is_flagged() -> None:
    corpus = CORPORA / "planted_secrets.txt"
    exit_code, findings = run_gitleaks(corpus)
    assert exit_code == 1, "planted corpus scanned clean — the gate is not catching anything"

    flagged = set()
    for finding in findings:
        flagged.update(range(finding["StartLine"], finding["EndLine"] + 1))

    missed = [lineno for lineno in planted_lines(corpus) if lineno not in flagged]
    assert not missed, f"planted lines not flagged by any rule: {missed}"


def test_every_custom_rule_fires_on_planted_corpus() -> None:
    _, findings = run_gitleaks(CORPORA / "planted_secrets.txt")
    fired = {finding["RuleID"] for finding in findings}
    silent = CUSTOM_RULE_IDS - fired
    assert not silent, f"custom rules that never fired: {sorted(silent)}"


def test_clean_corpus_passes() -> None:
    exit_code, findings = run_gitleaks(CORPORA / "clean.txt")
    hits = [(f["RuleID"], f["StartLine"], f["Secret"]) for f in findings]
    assert exit_code == 0 and not hits, f"false positives on clean corpus: {hits}"
