from imbue.skitwright.redaction import redact_secret_values


def test_redact_secret_values_replaces_every_occurrence_under_any_name() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghij",
        "MODAL_TOKEN_SECRET": "as-0123456789",
        "HOME": "/home/someone",
    }
    text = (
        "$ mngr exec my-task -- env | sort\n"
        "  ANTHROPIC_API_KEY=sk-ant-api03-abcdefghij\n"
        "  CLAUDE_KEY_COPY=sk-ant-api03-abcdefghij\n"
        "  HOME=/home/someone\n"
        "  MODAL_TOKEN_SECRET=as-0123456789\n"
        "? 0\n"
    )
    assert redact_secret_values(text, env, ["ANTHROPIC_API_KEY", "MODAL_TOKEN_SECRET"]) == (
        "$ mngr exec my-task -- env | sort\n"
        "  ANTHROPIC_API_KEY=<redacted ANTHROPIC_API_KEY>\n"
        "  CLAUDE_KEY_COPY=<redacted ANTHROPIC_API_KEY>\n"
        "  HOME=/home/someone\n"
        "  MODAL_TOKEN_SECRET=<redacted MODAL_TOKEN_SECRET>\n"
        "? 0\n"
    )


def test_redact_secret_values_only_touches_the_named_variables() -> None:
    env = {"GH_TOKEN": "ghp_abcdefghijklmnop", "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghij"}
    text = "GH_TOKEN=ghp_abcdefghijklmnop ANTHROPIC_API_KEY=sk-ant-api03-abcdefghij"
    assert redact_secret_values(text, env, ["GH_TOKEN"]) == (
        "GH_TOKEN=<redacted GH_TOKEN> ANTHROPIC_API_KEY=sk-ant-api03-abcdefghij"
    )


def test_redact_secret_values_skips_missing_and_empty_variables() -> None:
    text = "$ echo hello\n  hello\n? 0\n"
    assert redact_secret_values(text, {"EMPTY_TOKEN": ""}, ["EMPTY_TOKEN", "UNSET_TOKEN"]) == text


def test_redact_secret_values_labels_a_shared_value_once_and_deterministically() -> None:
    env = {"ZZ_TOKEN": "same-value-1234", "AA_TOKEN": "same-value-1234"}
    assert redact_secret_values("x=same-value-1234", env, ["ZZ_TOKEN", "AA_TOKEN"]) == "x=<redacted AA_TOKEN>"


def test_redact_secret_values_labels_a_value_containing_another_by_its_own_name() -> None:
    env = {"INNER_TOKEN": "abcdefghij", "OUTER_TOKEN": "xx-abcdefghij-yy"}
    assert redact_secret_values("inner=abcdefghij outer=xx-abcdefghij-yy", env, env) == (
        "inner=<redacted INNER_TOKEN> outer=<redacted OUTER_TOKEN>"
    )


def test_redact_secret_values_leaves_text_alone_without_names() -> None:
    text = "$ echo hello\n  hello\n? 0\n"
    assert redact_secret_values(text, {"GH_TOKEN": "ghp_abcdefghijklmnop"}, []) == text
