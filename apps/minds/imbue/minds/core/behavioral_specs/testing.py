from collections.abc import Mapping
from pathlib import Path


def write_spec_corpus(corpus_root: Path, content_by_relative_path: Mapping[str, str]) -> Path:
    """Materialize a synthetic behavioral-spec corpus for tests and return its root."""
    for relative_path, content in content_by_relative_path.items():
        target = corpus_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    corpus_root.mkdir(parents=True, exist_ok=True)
    return corpus_root
