"""
sources.py -- fetch document text from external APIs referenced by the server.

Each work item carries a list of document refs like:
    {"source": "pmc",          "external_id": "PMC7239701"}
    {"source": "pubmed",       "external_id": "38123456"}
    {"source": "medline",      "external_id": "38123456"}
    {"source": "nih_reporter", "external_id": "5R01AI123456"}
    {"source": "biorxiv",      "external_id": "10.1101/2023.12.01.569666"}
    {"source": "medrxiv",      "external_id": "10.1101/2020.09.09.20191205"}
    {"source": "text",         "external_id": "...", "text": "inline text"}

Verified endpoints (checked 2026-07):
- E-utilities base: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/
    efetch.fcgi?db=pubmed&id=..&retmode=text&rettype=abstract   (PubMed abstract)
    efetch.fcgi?db=pubmed&id=..&retmode=text&rettype=medline    (MEDLINE record)
- PMC full text (BioC JSON):
    https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/<PMCID|PMID>/unicode
- NIH RePORTER (no auth): POST https://api.reporter.nih.gov/v2/projects/search
- bioRxiv/medRxiv (no auth): https://api.biorxiv.org/details/<server>/<DOI>/na/json

NCBI politeness: <=3 requests/sec without an API key, <=10/sec with one. Set
NCBI_API_KEY (and optionally NCBI_TOOL / NCBI_EMAIL) in the environment.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIOC_PMC = "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json"
REPORTER = "https://api.reporter.nih.gov/v2/projects/search"
BIORXIV = "https://api.biorxiv.org/details"   # /{server}/{doi}/na/json  (server: biorxiv|medrxiv)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": os.getenv("NCBI_TOOL", "paper-review-skill/1.0")})


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, per_second: float):
        self._min_interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self._min_interval


# NCBI rate: 10/s with a key, 3/s without.
_HAS_KEY = bool(os.getenv("NCBI_API_KEY"))
_NCBI = RateLimiter(9.0 if _HAS_KEY else 2.5)
# bioRxiv/medRxiv: no documented limit, but be polite.
_BIORXIV = RateLimiter(3.0)


def _ncbi_params(extra: dict) -> dict:
    p = dict(extra)
    if os.getenv("NCBI_API_KEY"):
        p["api_key"] = os.getenv("NCBI_API_KEY")
    if os.getenv("NCBI_EMAIL"):
        p["email"] = os.getenv("NCBI_EMAIL")
    p["tool"] = os.getenv("NCBI_TOOL", "paper-review-skill")
    return p


def _get(url, *, params=None, timeout=30):
    _NCBI.wait()
    r = _SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r


def fetch_pubmed_abstract(pmid: str) -> str:
    r = _get(f"{EUTILS}/efetch.fcgi",
             params=_ncbi_params({"db": "pubmed", "id": pmid,
                                  "retmode": "text", "rettype": "abstract"}))
    return r.text.strip()


def fetch_medline(pmid: str) -> str:
    r = _get(f"{EUTILS}/efetch.fcgi",
             params=_ncbi_params({"db": "pubmed", "id": pmid,
                                  "retmode": "text", "rettype": "medline"}))
    return r.text.strip()


def fetch_pmc_fulltext(pmc_id: str) -> str:
    """Full text via the BioC JSON service (PMC Open Access subset).
    Falls back to the PubMed abstract if the article isn't in the OA subset."""
    _NCBI.wait()
    r = _SESSION.get(f"{BIOC_PMC}/{pmc_id}/unicode", timeout=60)
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
        try:
            return _bioc_to_text(r.json())
        except Exception:
            pass
    # Fallback: strip the PMC prefix and pull the abstract from PubMed's PMC db.
    r = _get(f"{EUTILS}/efetch.fcgi",
             params=_ncbi_params({"db": "pmc", "id": pmc_id.replace("PMC", ""),
                                  "retmode": "xml"}))
    return r.text.strip()


def _bioc_to_text(doc: object) -> str:
    """Flatten a BioC JSON response into plain passage text."""
    collection = doc[0] if isinstance(doc, list) else doc
    parts: list[str] = []
    for document in collection.get("documents", []):
        for passage in document.get("passages", []):
            section = passage.get("infons", {}).get("section_type", "")
            text = passage.get("text", "").strip()
            if text:
                parts.append(f"[{section}] {text}" if section else text)
    return "\n\n".join(parts).strip()


def fetch_nih_reporter(core_project_num: str) -> str:
    """Fetch a grant's title, abstract, and terms from NIH RePORTER."""
    payload = {
        "criteria": {"project_nums": [core_project_num]},
        "include_fields": ["ProjectTitle", "AbstractText", "PhrText", "Terms",
                           "FiscalYear", "Organization", "PrincipalInvestigators"],
        "limit": 1,
    }
    r = _SESSION.post(REPORTER, json=payload, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return f"(no NIH RePORTER record for {core_project_num})"
    p = results[0]
    lines = [f"TITLE: {p.get('project_title', '')}"]
    if p.get("abstract_text"):
        lines.append(f"ABSTRACT: {p['abstract_text']}")
    if p.get("phr_text"):
        lines.append(f"PUBLIC HEALTH RELEVANCE: {p['phr_text']}")
    if p.get("terms"):
        lines.append(f"TERMS: {p['terms']}")
    return "\n\n".join(lines).strip()


def fetch_biorxiv(doi: str, server: str = "biorxiv") -> str:
    """Title + abstract for a bioRxiv/medRxiv preprint by DOI (e.g.
    ``10.1101/2023.12.01.569666``). Uses the latest posted version. The full
    JATS XML path is appended as a pointer for optional deeper retrieval."""
    server = "medrxiv" if server.lower() == "medrxiv" else "biorxiv"
    _BIORXIV.wait()
    r = _SESSION.get(f"{BIORXIV}/{server}/{doi}/na/json", timeout=30)
    r.raise_for_status()
    coll = r.json().get("collection", [])
    if not coll:
        return f"(no {server} record for {doi})"
    latest = coll[-1]  # collection is ordered by version
    lines = [f"TITLE: {latest.get('title', '')}",
             f"ABSTRACT: {latest.get('abstract', '')}"]
    if latest.get("category"):
        lines.append(f"CATEGORY: {latest['category']}")
    if latest.get("jatsxml"):
        lines.append(f"FULL_TEXT_JATS_XML: {latest['jatsxml']}")
    return "\n\n".join(lines).strip()


_FETCHERS = {
    "pubmed": fetch_pubmed_abstract,
    "medline": fetch_medline,
    "pmc": fetch_pmc_fulltext,
    "nih_reporter": fetch_nih_reporter,
    # biorxiv / medrxiv are handled in fetch_document (they need a server arg).
}


def fetch_document(ref: dict) -> dict:
    """Resolve one document ref to text. Never raises: failures are returned in
    the dict so a single bad id doesn't sink the whole item."""
    source = ref.get("source", "").lower()
    ext_id = ref.get("external_id", "")
    out = {"source": source, "external_id": ext_id, "text": "", "error": None}
    try:
        if source == "text":
            out["text"] = ref.get("text", "")
        elif source in ("biorxiv", "medrxiv"):
            out["text"] = fetch_biorxiv(ext_id, server=ref.get("server", source))
        elif source in _FETCHERS:
            out["text"] = _FETCHERS[source](ext_id)
        else:
            out["error"] = f"unknown source: {source}"
    except Exception as e:  # network / API errors surface here
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def fetch_documents(refs: list[dict]) -> list[dict]:
    return [fetch_document(r) for r in refs]
