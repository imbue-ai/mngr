"""Unit tests for the mngr_usage plugin hooks."""

from imbue.mngr_usage.plugin import register_help_topics


def test_register_help_topics_exposes_cron_recipes() -> None:
    """The plugin contributes the cron_recipes doc as a namespaced help topic.

    The key and description are namespaced ('usage_' / 'mngr usage:') so they are
    unambiguous in the global help topic list, and the body loads from the
    plugin's markdown doc file.
    """
    topics = register_help_topics()
    by_key = {topic.key: topic for topic in topics}
    assert "usage_cron_recipes" in by_key
    cron_recipes = by_key["usage_cron_recipes"]
    assert cron_recipes.one_line_description == "mngr usage: Cron automation recipes"
    assert "cron" in cron_recipes.load_body().lower()
    # The topic carries a GitHub source_url so its relative links render as
    # clickable absolute URLs in the terminal.
    link_base = cron_recipes.link_base_url()
    assert link_base is not None
    assert link_base.startswith("https://github.com/imbue-ai/mngr/blob/")
    assert link_base.endswith("/libs/mngr_usage/docs/cron_recipes.md")
