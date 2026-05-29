"""Unit tests for the mngr_usage plugin hooks."""

from imbue.mngr_usage.plugin import register_help_topics


def test_register_help_topics_exposes_cron_recipes() -> None:
    """The plugin contributes the cron_recipes doc as a namespaced help topic.

    Exercises the register_help_topics hook end to end: it reads the plugin's
    docs directory, builds a TopicHelpPage per markdown file, and namespaces the
    key and description so they are unambiguous in the global help topic list.
    """
    topics = register_help_topics()
    by_key = {topic.key: topic for topic in topics}
    assert "usage_cron_recipes" in by_key
    cron_recipes = by_key["usage_cron_recipes"]
    assert cron_recipes.one_line_description == "mngr usage: Cron automation recipes"
    assert "cron" in cron_recipes.content.lower()
