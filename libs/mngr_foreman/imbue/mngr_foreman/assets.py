"""Pinned frontend assets, fetched on first run instead of vendored in git.

The web UI's third-party libraries -- marked, xterm (+fit +css), highlight.js,
KaTeX (+fonts), mermaid, and the Atkinson Hyperlegible fonts -- used to live in
the repo as ~4MB of minified blobs. Instead each file's exact URL and sha256 are
pinned in ``_RAW`` below, and the server fetches any that are missing into a
local cache dir on startup (``<host_dir>/plugin/foreman/assets``), verifying the
hash before writing. A present, hash-verified file is served straight from the
cache; the pin makes the fetch reproducible and tamper-evident.

Tiers control how a fetch failure is handled (see ``ensure_assets``):

* ``REQUIRED`` -- the terminal (xterm) and markdown (marked) core. If these are
  neither cached nor fetchable we log a loud error naming them, but still serve:
  the terminal page shows its own load error and markdown falls back to escaped
  text (``app.js`` ``renderMarkdown``), so the agent list and chat still work.
* ``OPTIONAL`` -- progressive enhancement (syntax highlighting, math, diagrams,
  typography). A failure is logged quietly and the feature simply stays off; the
  frontend loaders already ``.catch`` a missing asset.
"""

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext

_DEFAULT_TIMEOUT_SECONDS: float = 20.0
_USER_AGENT: str = "mngr-foreman-assets"


class AssetTier(Enum):
    """How a fetch failure degrades: hard (loud, but keep serving) vs quiet."""

    REQUIRED = "required"
    OPTIONAL = "optional"


class AssetSpec(FrozenModel):
    """One pinned asset: where it is served, where it comes from, and its hash."""

    path: str = Field(description="Relative path under /static/vendor/ and the cache dir")
    url: str = Field(description="Pinned https source (jsdelivr npm/gh or fontsource)")
    sha256: str = Field(description="Expected hex digest of the fetched bytes")
    tier: AssetTier = Field(description="REQUIRED (loud on failure) or OPTIONAL (quiet)")


class AssetFetchResult(FrozenModel):
    """Outcome of an ``ensure_assets`` pass, for logging and tests."""

    served: tuple[str, ...] = ()
    fetched: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    missing_optional: tuple[str, ...] = ()


# (path, url, sha256, tier) -- generated once from the pinned CDN releases; see
# ASSETS.md. Keep this list and ASSETS.md in sync when bumping a version.
_RAW: tuple[tuple[str, str, str, str], ...] = (
    (
        "marked.min.js",
        "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
        "15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894",
        "required",
    ),
    (
        "xterm.min.js",
        "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js",
        "fc1dd31b221e3e5f929486e07a80b477a8aaf9dce2b4f9c3ffe7dd25f370655d",
        "required",
    ),
    (
        "xterm.min.css",
        "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css",
        "64ee6c4db69b4224d3362aced0fd4cdd620e0e60b3d01566450ae2d4b9e81849",
        "required",
    ),
    (
        "xterm-addon-fit.min.js",
        "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js",
        "c78e5795c6487acdf24ab436798d4a9cec3848101b281bc65b061f39db714be1",
        "required",
    ),
    (
        "highlight.min.js",
        "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js",
        "837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846",
        "optional",
    ),
    (
        "highlight-atom-one-dark.min.css",
        "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/atom-one-dark.min.css",
        "4237ffca7ce6aadb438c457e0a675b125c534bbdda5b87f41f3a1495603bcc9b",
        "optional",
    ),
    (
        "katex/katex.min.js",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js",
        "dc84b296ec3e884de093158f760fd9d45b6c7abe58b5381557f4e138f46a58ae",
        "optional",
    ),
    (
        "katex/katex.min.css",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css",
        "505d5f829022bb7b4f24dfee0aa1141cd7bba67afe411d1240335f820960b5c3",
        "optional",
    ),
    (
        "katex/auto-render.min.js",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js",
        "9cb8dacfc086c2966c9ec4ba54f4a2dc43b7cbe2b33cec1a2743d886c7fb47a7",
        "optional",
    ),
    (
        "mermaid.min.js",
        "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js",
        "61b335a46df05a7ce1c98378f60e5f3e77a7fb608a1056997e8a649304a936d6",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_AMS-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_AMS-Regular.woff2",
        "0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Caligraphic-Bold.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Caligraphic-Bold.woff2",
        "de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Caligraphic-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Caligraphic-Regular.woff2",
        "5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Fraktur-Bold.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Fraktur-Bold.woff2",
        "74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Fraktur-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Fraktur-Regular.woff2",
        "51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Main-Bold.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Main-Bold.woff2",
        "0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Main-BoldItalic.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Main-BoldItalic.woff2",
        "99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Main-Italic.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Main-Italic.woff2",
        "97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Main-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Main-Regular.woff2",
        "c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Math-BoldItalic.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Math-BoldItalic.woff2",
        "dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Math-Italic.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Math-Italic.woff2",
        "7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_SansSerif-Bold.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_SansSerif-Bold.woff2",
        "e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_SansSerif-Italic.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_SansSerif-Italic.woff2",
        "00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_SansSerif-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_SansSerif-Regular.woff2",
        "68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Script-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Script-Regular.woff2",
        "036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Size1-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Size1-Regular.woff2",
        "6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Size2-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Size2-Regular.woff2",
        "d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Size3-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Size3-Regular.woff2",
        "73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Size4-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Size4-Regular.woff2",
        "a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0",
        "optional",
    ),
    (
        "katex/fonts/KaTeX_Typewriter-Regular.woff2",
        "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/fonts/KaTeX_Typewriter-Regular.woff2",
        "71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-400-normal.woff2",
        "d64ba838ef5472bba248620ec4fd8b5aa7cf0db2908e0bb230600caf279ba7bc",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-400-italic.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-400-italic.woff2",
        "bc8825fd435d4aa8e31449937826d583a7daaae15a83832c91b38375131ebf08",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-mono-latin-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible-mono@5.3.0/files/atkinson-hyperlegible-mono-latin-400-normal.woff2",
        "87b5b94c18025729fc6ed4e8c25221ade3746dc87213943fa4de49f1f0f26787",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-700-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-700-normal.woff2",
        "140e2bd25a7315c8a062508391426b0d8c3297400c947b8d847be28f73a199f0",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-700-italic.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-700-italic.woff2",
        "73e0c9e1e284128dda599558f7c15bd5f3c056442c31dfae950528a16b3835cf",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-mono-latin-700-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible-mono@5.3.0/files/atkinson-hyperlegible-mono-latin-700-normal.woff2",
        "f30319aa2ab224c5a19d177617e2141b002fcb95c1afe233214f9d342e47687d",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-ext-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-ext-400-normal.woff2",
        "61eeb0eb8b881a84d069e67be1723f5b353b0b9b866ad8e7ad9dba66cdc4dabd",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-ext-400-italic.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-ext-400-italic.woff2",
        "90d887d541fe6e912d15f826aae6ca1190efcf172b65401704472475ed771049",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-mono-latin-ext-400-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible-mono@5.3.0/files/atkinson-hyperlegible-mono-latin-ext-400-normal.woff2",
        "1960608ff6f1c6c450adb7c6ce4e3d05a7e05dde61449b9f88ae2d14de78e3a1",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-ext-700-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-ext-700-normal.woff2",
        "840ced16f975d8abc2594b436d0206649e0c2a7327e36750ff849d3d37c3c02e",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-latin-ext-700-italic.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible@5.3.0/files/atkinson-hyperlegible-latin-ext-700-italic.woff2",
        "43471ea6265ba9619c8c12d3f9f53c9e1741fa3af69f6b658100a841b09398c8",
        "optional",
    ),
    (
        "fonts/atkinson-hyperlegible-mono-latin-ext-700-normal.woff2",
        "https://cdn.jsdelivr.net/npm/@fontsource/atkinson-hyperlegible-mono@5.3.0/files/atkinson-hyperlegible-mono-latin-ext-700-normal.woff2",
        "46ba8678220358e306d918613782dd0f41194be0fd935da9c02d6326abd62fa8",
        "optional",
    ),
)

MANIFEST: tuple[AssetSpec, ...] = tuple(
    AssetSpec(path=path, url=url, sha256=sha256, tier=AssetTier(tier)) for (path, url, sha256, tier) in _RAW
)


def get_asset_dir(mngr_ctx: MngrContext) -> Path:
    """Local cache dir for fetched frontend assets (mirrors mngr_forward's layout)."""
    return mngr_ctx.config.default_host_dir.expanduser() / "plugin" / "foreman" / "assets"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str, timeout: float) -> bytes:
    """Fetch a pinned https URL. Caller verifies the sha256 before trusting bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _cached_and_valid(target: Path, expected_sha256: str) -> bool:
    """True when ``target`` exists and its bytes hash to ``expected_sha256``."""
    try:
        return target.is_file() and _sha256(target.read_bytes()) == expected_sha256
    except OSError:
        return False


def ensure_asset(
    spec: AssetSpec,
    asset_dir: Path,
    *,
    fetch: Callable[[str, float], bytes],
    timeout: float,
) -> bool:
    """Make ``spec`` present and hash-valid under ``asset_dir``. True on success.

    Returns immediately if a valid copy is cached; otherwise downloads, verifies
    the sha256, and writes atomically. Any fetch/verify failure returns False
    (the asset is left absent) -- callers decide how loudly that degrades.
    """
    target = asset_dir / spec.path
    if _cached_and_valid(target, spec.sha256):
        return True
    try:
        data = fetch(spec.url, timeout)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        logger.debug("Asset fetch failed for {} ({}): {}", spec.path, spec.url, exc)
        return False
    actual = _sha256(data)
    if actual != spec.sha256:
        logger.warning("Asset {} hash mismatch (expected {}, got {}); refusing.", spec.path, spec.sha256, actual)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return True


def ensure_assets(
    asset_dir: Path,
    *,
    fetch: Callable[[str, float], bytes] = _download,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    manifest: tuple[AssetSpec, ...] = MANIFEST,
) -> AssetFetchResult:
    """Fetch any missing pinned assets into ``asset_dir`` and report the outcome.

    Never raises: a REQUIRED asset that is neither cached nor fetchable is logged
    as an error (naming what's missing) but the server still starts and serves
    the core UI with graceful frontend degradation.
    """
    served: list[str] = []
    fetched: list[str] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []
    for spec in manifest:
        was_cached = _cached_and_valid(asset_dir / spec.path, spec.sha256)
        if ensure_asset(spec, asset_dir, fetch=fetch, timeout=timeout):
            served.append(spec.path)
            if not was_cached:
                fetched.append(spec.path)
        elif spec.tier is AssetTier.REQUIRED:
            missing_required.append(spec.path)
        else:
            missing_optional.append(spec.path)

    logger.info(
        "Foreman assets: {} present ({} fetched now), {} optional unavailable.",
        len(served),
        len(fetched),
        len(missing_optional),
    )
    if missing_required:
        logger.error(
            "Foreman: REQUIRED frontend assets could not be fetched or cached: {}. "
            "The box may be offline. Chat still works (markdown degrades to plain text); "
            "the web terminal needs xterm and will show a load error until these are reachable.",
            ", ".join(missing_required),
        )
    if missing_optional:
        logger.info(
            "Foreman: optional assets unavailable (feature stays off): {}.",
            ", ".join(missing_optional),
        )
    return AssetFetchResult(
        served=tuple(served),
        fetched=tuple(fetched),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
    )
