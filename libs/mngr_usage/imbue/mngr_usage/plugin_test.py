"""Unit tests for the mngr_usage plugin hooks."""

from imbue.mngr_usage.plugin import register_help_topics


def test_register_help_topics_exposes_cron_recipes() -> None:
    """The plugin contributes the cron_recipes doc as a help topic.

    Exercises the register_help_topics hook end to end: it reads the plugin's
    docs directory and builds a TopicHelpPage per markdown file, keyed by the
    filename stem with the first heading as the description.
    """
    topics = register_help_topics()
    by_key = {topic.key: topic for topic in topics}
    assert "cron_recipes" in by_key
    cron_recipes = by_key["cron_recipes"]
    assert cron_recipes.one_line_description == "Cron automation recipes"
    assert "cron" in cron_recipes.content.lower()
