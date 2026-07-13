"""Offline tests for the secrets/PII gate (DESIGN.md §7, §11).

The gate is three scanners, all of which must pass before a fixer's PR goes
up: Betterleaks (stock rules + the custom PII rules in .betterleaks.toml),
TruffleHog, and Kingfisher (both with stock rules). Each scanner is tested
against two checked-in corpora:

- corpora/planted_secrets.txt — fake secrets + PII; every planted line must
  be flagged by Betterleaks (the scanner carrying the PII rules), and the
  real-credential lines must also be flagged by TruffleHog and Kingfisher.
- corpora/clean.txt — ordinary code/diff text salted with lookalikes
  (version strings, 0.0.0.0, decorators, git remotes); no scanner may flag
  anything, because a single hit from any scanner blocks the PR.

This is the one part of open-seer tested offline — everything else is
validated on live surfaces (DESIGN.md §11). Each scanner's tests skip when
its binary is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / ".betterleaks.toml"
CORPORA = Path(__file__).resolve().parent / "corpora"

# Every custom rule in .betterleaks.toml must fire at least once on the
# planted corpus, or the rule is dead weight.
CUSTOM_RULE_IDS = {
    "open-seer-aws-access-key-id",
    "open-seer-pii-email",
    "open-seer-pii-ipv4",
    "open-seer-pii-ipv6",
    "open-seer-pii-bearer-token",
    "open-seer-pii-auth-header",
    "open-seer-pii-cookie-header",
    "open-seer-pii-user-id",
    "open-seer-url-credentials",
}

# 1-based line of the planted Anthropic API key — the canonical real-secret
# line that every scanner's stock rules must catch.
ANTHROPIC_KEY_LINE = 13

BETTERLEAKS = shutil.which("betterleaks")
TRUFFLEHOG = shutil.which("trufflehog")
KINGFISHER = shutil.which("kingfisher")

requires_betterleaks = pytest.mark.skipif(BETTERLEAKS is None, reason="betterleaks binary not available")
requires_trufflehog = pytest.mark.skipif(TRUFFLEHOG is None, reason="trufflehog binary not available")
requires_kingfisher = pytest.mark.skipif(KINGFISHER is None, reason="kingfisher binary not available")


def planted_lines(corpus: Path) -> list[int]:
    """1-based numbers of every non-comment, non-blank line in a corpus."""
    return [
        lineno
        for lineno, line in enumerate(corpus.read_text().splitlines(), start=1)
        if line.strip() and not line.strip().startswith("#")
    ]


# --- Betterleaks (stock rules + custom PII rules) --------------------------


def run_betterleaks(corpus: Path) -> tuple[int, list[dict]]:
    """Scan a single corpus file; return (exit code, findings from the JSON report)."""
    proc = subprocess.run(
        [
            BETTERLEAKS,
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
    assert proc.returncode in (0, 1), f"betterleaks failed unexpectedly:\n{proc.stderr}"
    return proc.returncode, json.loads(proc.stdout) or []


@requires_betterleaks
def test_betterleaks_flags_every_planted_line() -> None:
    corpus = CORPORA / "planted_secrets.txt"
    exit_code, findings = run_betterleaks(corpus)
    assert exit_code == 1, "planted corpus scanned clean — the gate is not catching anything"

    flagged = set()
    for finding in findings:
        flagged.update(range(finding["StartLine"], finding["EndLine"] + 1))

    missed = [lineno for lineno in planted_lines(corpus) if lineno not in flagged]
    assert not missed, f"planted lines not flagged by any rule: {missed}"


@requires_betterleaks
def test_every_custom_rule_fires_on_planted_corpus() -> None:
    _, findings = run_betterleaks(CORPORA / "planted_secrets.txt")
    fired = {finding["RuleID"] for finding in findings}
    silent = CUSTOM_RULE_IDS - fired
    assert not silent, f"custom rules that never fired: {sorted(silent)}"


@requires_betterleaks
def test_betterleaks_passes_clean_corpus() -> None:
    exit_code, findings = run_betterleaks(CORPORA / "clean.txt")
    hits = [(f["RuleID"], f["StartLine"], f["Secret"]) for f in findings]
    assert exit_code == 0 and not hits, f"false positives on clean corpus: {hits}"


# --- TruffleHog (stock detectors) -------------------------------------------


def run_trufflehog(corpus: Path) -> tuple[int, list[dict]]:
    """Scan a single corpus file; return (exit code, findings from JSONL stdout)."""
    proc = subprocess.run(
        [
            TRUFFLEHOG,
            "filesystem",
            str(corpus),
            "--no-verification",  # offline: never call providers to validate
            "--fail",  # exit 183 when findings exist
            "--json",
            "--no-update",
        ],
        capture_output=True,
        text=True,
    )
    # 0 = clean, 183 = findings (--fail); anything else is a usage error.
    assert proc.returncode in (0, 183), f"trufflehog failed unexpectedly:\n{proc.stderr}"
    findings = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return proc.returncode, findings


def trufflehog_lines(findings: list[dict]) -> set[int]:
    return {
        finding["SourceMetadata"]["Data"]["Filesystem"]["line"]
        for finding in findings
        if "Filesystem" in finding.get("SourceMetadata", {}).get("Data", {})
    }


@requires_trufflehog
def test_trufflehog_flags_planted_credentials() -> None:
    exit_code, findings = run_trufflehog(CORPORA / "planted_secrets.txt")
    assert exit_code == 183, "planted corpus scanned clean — trufflehog is not catching anything"
    assert ANTHROPIC_KEY_LINE in trufflehog_lines(findings), (
        f"trufflehog did not flag the planted Anthropic key on line {ANTHROPIC_KEY_LINE}"
    )


@requires_trufflehog
def test_trufflehog_passes_clean_corpus() -> None:
    exit_code, findings = run_trufflehog(CORPORA / "clean.txt")
    hits = [(f.get("DetectorName"), sorted(trufflehog_lines([f]))) for f in findings]
    assert exit_code == 0 and not hits, f"false positives on clean corpus: {hits}"


# --- Kingfisher (stock rules) ------------------------------------------------


def run_kingfisher(corpus: Path) -> tuple[int, list[dict]]:
    """Scan a single corpus file; return (exit code, findings from the JSON report)."""
    proc = subprocess.run(
        [
            KINGFISHER,
            "scan",
            str(corpus),
            "--no-validate",  # offline: never call providers to validate
            "--format",
            "json",
            "--no-update-check",
        ],
        capture_output=True,
        text=True,
    )
    # 0 = clean, 200 = findings (205 = validated findings, impossible with
    # --no-validate); anything else is a usage error.
    assert proc.returncode in (0, 200), f"kingfisher failed unexpectedly:\n{proc.stderr}"
    # Stdout is the report object, possibly followed by a summary document —
    # parse only the first.
    report, _ = json.JSONDecoder().raw_decode(proc.stdout.lstrip())
    return proc.returncode, report.get("findings") or []


def kingfisher_lines(findings: list[dict]) -> set[int]:
    return {finding["finding"]["line"] for finding in findings}


@requires_kingfisher
def test_kingfisher_flags_planted_credentials() -> None:
    exit_code, findings = run_kingfisher(CORPORA / "planted_secrets.txt")
    assert exit_code == 200, "planted corpus scanned clean — kingfisher is not catching anything"
    assert ANTHROPIC_KEY_LINE in kingfisher_lines(findings), (
        f"kingfisher did not flag the planted Anthropic key on line {ANTHROPIC_KEY_LINE}"
    )


@requires_kingfisher
def test_kingfisher_passes_clean_corpus() -> None:
    exit_code, findings = run_kingfisher(CORPORA / "clean.txt")
    hits = [(f["rule"]["id"], f["finding"]["line"], f["finding"]["snippet"][:40]) for f in findings]
    assert exit_code == 0 and not hits, f"false positives on clean corpus: {hits}"
