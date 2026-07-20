"""Guard the live behavioral-spec corpus shipped at ``apps/minds/specs/``.

This corpus is a minds artifact: it travels with minds (and any future
spin-out), so this guard lives in the minds app rather than in the
corpus-generic ``mngr_specs`` tool. It fails if the corpus ever drifts out of
conformance with the behavioral-spec language that ``mngr specs validate``
enforces.
"""

from pathlib import Path

from imbue.mngr_specs.corpus import scan_corpus

# The live corpus shipped in this repo (this test sits at
# apps/minds/imbue/minds/, so parents[2] is apps/minds).
_LIVE_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "specs"


def test_live_corpus_has_no_violations() -> None:
    """The corpus at ``apps/minds/specs/`` always satisfies the spec-language rules."""
    scan = scan_corpus(_LIVE_CORPUS_ROOT)

    assert scan.violations == ()
    # Guard against the root silently pointing at an empty or wrong directory.
    assert len(scan.units) > 0
