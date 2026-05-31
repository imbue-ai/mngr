"""Background prefetch of slow first-create assets at app launch.

When a brand-new user opens minds.app, the first ``Create Agent`` click
spends 10+ minutes mostly waiting on two big downloads neither of
which depends on the form values: the Ubuntu cloud image Lima boots
into, and the forever-claude-template git clone. This module kicks
both off as soon as the desktop client backend is up, so by the time
the user signs in + fills the form they're already cached.

Idempotent: a check for the artifact's existence + minimum-size sanity
check is the gate. No sentinel file. A user who manually nukes
``~/Library/Caches/lima`` or the template cache will see a fresh
prefetch on the next launch, which is the right behaviour.

Runs on a background thread off the root concurrency group; failures
are logged at WARNING and never propagate. The fallback path (lazy
download at create-agent time) keeps working unchanged.
"""

import hashlib
import platform
import shutil
import subprocess
import time
import urllib.request
from email.utils import formatdate
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

# Inlined from libs/mngr_lima/imbue/mngr_lima/constants.py to avoid adding
# imbue-mngr-lima as an apps/minds dependency. If those constants ever
# change, this file needs to track them. They've been stable for a year+.
_DEFAULT_IMAGE_URL_AARCH64: str = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img"
)
_DEFAULT_IMAGE_URL_X86_64: str = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
)

_FCT_URL: str = "https://github.com/imbue-ai/forever-claude-template.git"
_FCT_CACHE_SUBDIR: str = "template-cache/forever-claude-template"

_MIN_BYTES_VALID_CLOUDIMG: int = 100 * 1024 * 1024


def _lima_cache_dir_for_url(url: str) -> Path:
    """Lima 1.x: ``~/Library/Caches/lima/download/by-url-sha256/<sha256(url)>/``"""
    url_sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return Path.home() / "Library" / "Caches" / "lima" / "download" / "by-url-sha256" / url_sha


def _is_lima_image_cached(url: str) -> bool:
    data = _lima_cache_dir_for_url(url) / "data"
    return data.exists() and data.stat().st_size >= _MIN_BYTES_VALID_CLOUDIMG


def _prefetch_lima_image(url: str) -> None:
    cache_dir = _lima_cache_dir_for_url(url)
    if _is_lima_image_cached(url):
        logger.info("[prefetch] lima image already cached: {}", url)
        return
    logger.info("[prefetch] downloading lima image: {}", url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / "data.tmp"
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f, length=4 * 1024 * 1024)
    bytes_written = tmp.stat().st_size
    if bytes_written < _MIN_BYTES_VALID_CLOUDIMG:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"prefetch wrote only {bytes_written} bytes from {url}")
    (cache_dir / "url").write_text(url)
    (cache_dir / "type").write_text("application/octet-stream")
    (cache_dir / "time").write_text(formatdate(usegmt=True))
    tmp.rename(cache_dir / "data")
    logger.info(
        "[prefetch] lima image done: {} bytes in {:.1f}s -> {}",
        bytes_written,
        time.monotonic() - started,
        cache_dir,
    )


def _lima_image_url_for_host() -> str:
    """Match what lima_yaml.py picks for this architecture."""
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return _DEFAULT_IMAGE_URL_AARCH64
    return _DEFAULT_IMAGE_URL_X86_64


def _fct_cache_dir(data_dir: Path) -> Path:
    return data_dir / _FCT_CACHE_SUBDIR


def _is_fct_cached(data_dir: Path) -> bool:
    cache = _fct_cache_dir(data_dir)
    return (cache / ".git").is_dir()


def _prefetch_fct_clone(data_dir: Path) -> None:
    cache = _fct_cache_dir(data_dir)
    if _is_fct_cached(data_dir):
        logger.info("[prefetch] FCT clone already cached: {}", cache)
        # Best-effort fetch to keep it warm; failures are non-fatal.
        try:
            subprocess.run(
                ["git", "-C", str(cache), "fetch", "--prune", "--all"],
                check=False,
                capture_output=True,
                timeout=120,
            )
        except Exception as exc:
            logger.debug("[prefetch] FCT background fetch failed: {}", exc)
        return
    logger.info("[prefetch] cloning FCT: {} -> {}", _FCT_URL, cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = subprocess.run(
        ["git", "clone", "--no-checkout", _FCT_URL, str(cache)],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone FCT failed: {result.stderr.decode(errors='replace')}")
    logger.info("[prefetch] FCT clone done in {:.1f}s -> {}", time.monotonic() - started, cache)


def _run_all(data_dir: Path) -> None:
    """Run every prefetch task in series. Errors are logged, not raised."""
    try:
        _prefetch_lima_image(_lima_image_url_for_host())
    except Exception as exc:
        logger.warning("[prefetch] lima image prefetch failed (lazy download will still work): {}", exc)
    try:
        _prefetch_fct_clone(data_dir)
    except Exception as exc:
        logger.warning("[prefetch] FCT clone prefetch failed: {}", exc)


def start_first_launch_prefetch(data_dir: Path, concurrency_group: ConcurrencyGroup) -> None:
    """Spawn the prefetch on ``concurrency_group``; returns immediately."""
    concurrency_group.start_new_thread(target=lambda: _run_all(data_dir))
