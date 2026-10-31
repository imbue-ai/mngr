from pathlib import Path

from imbue.minds.smoke_tests.browser import default_playwright_browsers_dir


def test_playwright_keeps_its_browsers_in_the_platform_cache_under_home() -> None:
    home = Path("/Users/someone")
    assert default_playwright_browsers_dir(home, "darwin") == home / "Library" / "Caches" / "ms-playwright"
    assert default_playwright_browsers_dir(Path("/home/runner"), "linux") == Path("/home/runner/.cache/ms-playwright")
