import webbrowser

from imbue.minds.desktop_client.sign_in_browser import InstalledBrowser
from imbue.minds.desktop_client.sign_in_browser import _first_per_label
from imbue.minds.desktop_client.sign_in_browser import list_installed_browsers


def _browser(browser_id: str, label: str) -> InstalledBrowser:
    return InstalledBrowser(browser_id=browser_id, label=label, command_template=f"{browser_id} %s &")


def test_first_per_label_keeps_the_first_browser_found_under_each_name_in_order() -> None:
    stable = _browser("/usr/bin/google-chrome-stable", "Google Chrome")
    firefox = _browser("/usr/bin/firefox", "Firefox")

    unique = _first_per_label([stable, firefox, _browser("/usr/bin/google-chrome", "Google Chrome")])

    assert unique == (stable, firefox)


def test_every_browser_found_is_named_once_and_opens_the_url_in_that_browser() -> None:
    browsers = list_installed_browsers()

    assert len({browser.label for browser in browsers}) == len(browsers)
    for browser in browsers:
        # Parsing the template launches nothing; it has to name the browser as one argument (an
        # app bundle path can hold a space) and end with the URL.
        controller = webbrowser.get(browser.command_template)
        assert isinstance(controller, webbrowser.GenericBrowser)
        assert browser.browser_id in controller.args
        assert controller.args[-1] == "%s"
