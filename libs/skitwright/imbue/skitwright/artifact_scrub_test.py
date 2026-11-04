from pathlib import Path

from imbue.skitwright.artifact_scrub import format_pattern_redactions
from imbue.skitwright.artifact_scrub import is_placeholder_credential
from imbue.skitwright.artifact_scrub import redact_secret_like_strings
from imbue.skitwright.artifact_scrub import scrub_paths
from imbue.skitwright.artifact_scrub import secret_names_from_env
from imbue.skitwright.testing import random_looking_credential_body

REAL_SHAPED_ANTHROPIC_KEY = "sk-ant-api03-" + random_looking_credential_body(90)
# The shape LiteLLM mints: ``sk-`` plus ``secrets.token_urlsafe(16)``.
REAL_SHAPED_LITELLM_KEY = "sk-" + random_looking_credential_body(22)
REAL_SHAPED_MODAL_SECRET = "as-" + random_looking_credential_body(30)
REAL_SHAPED_GITHUB_TOKEN = "ghs_" + random_looking_credential_body(40)
REAL_SHAPED_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + random_looking_credential_body(70)
    + "\n"
    + random_looking_credential_body(70)
    + "\n-----END OPENSSH PRIVATE KEY-----"
)


def test_redact_secret_like_strings_replaces_each_credential_shape() -> None:
    text = (
        f"ANTHROPIC_API_KEY={REAL_SHAPED_ANTHROPIC_KEY}\n"
        f"LITELLM_KEY={REAL_SHAPED_LITELLM_KEY}\n"
        f"MODAL_TOKEN_SECRET={REAL_SHAPED_MODAL_SECRET}\n"
        f"GH_TOKEN={REAL_SHAPED_GITHUB_TOKEN}\n"
        f"{REAL_SHAPED_PRIVATE_KEY}\n"
        "the end\n"
    )

    redacted, matches = redact_secret_like_strings(text)

    assert redacted == (
        "ANTHROPIC_API_KEY=<redacted anthropic-key>\n"
        "LITELLM_KEY=<redacted sk-key>\n"
        "MODAL_TOKEN_SECRET=<redacted modal-token>\n"
        "GH_TOKEN=<redacted github-token>\n"
        "<redacted private-key>\n"
        "the end\n"
    )
    assert [(match.pattern_name, match.value, match.line_number) for match in matches] == [
        ("anthropic-key", REAL_SHAPED_ANTHROPIC_KEY, 1),
        ("sk-key", REAL_SHAPED_LITELLM_KEY, 2),
        ("modal-token", REAL_SHAPED_MODAL_SECRET, 3),
        ("github-token", REAL_SHAPED_GITHUB_TOKEN, 4),
        ("private-key", REAL_SHAPED_PRIVATE_KEY, 5),
    ]


def test_redact_secret_like_strings_redacts_a_private_key_header_without_a_footer() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\n" + random_looking_credential_body(70) + "\n"

    redacted, matches = redact_secret_like_strings(text)

    assert redacted == "<redacted private-key>\n" + random_looking_credential_body(70) + "\n"
    assert [(match.pattern_name, match.value) for match in matches] == [
        ("private-key", "-----BEGIN RSA PRIVATE KEY-----")
    ]


def test_redact_secret_like_strings_redacts_a_private_key_whose_body_looks_like_a_placeholder() -> None:
    """A base64 body can spell a telltale word by chance and encodes zero bytes as runs of A; only the header is judged."""
    text = (
        "-----BEGIN EC PRIVATE KEY-----\nMIItestFAKE"
        + random_looking_credential_body(40)
        + "AAAAAAAAAAAAB\n-----END EC PRIVATE KEY-----"
    )

    redacted, matches = redact_secret_like_strings(text)

    assert redacted == "<redacted private-key>"
    assert [match.pattern_name for match in matches] == ["private-key"]


def test_redact_secret_like_strings_leaves_placeholders_and_short_strings_alone() -> None:
    text = (
        "sk-ant-test\n"
        "sk-ant-api03-" + "a" * 80 + "\n"
        "sk-ant-api03-PLACEHOLDER0000000000000000000000000000\n"
        "export ANTHROPIC_API_KEY=sk-ant-api03-...\n"
        "<redacted ANTHROPIC_API_KEY>\n"
        "as-is and ak-47 are not tokens\n"
    )

    assert redact_secret_like_strings(text) == (text, [])


def test_is_placeholder_credential_rejects_repeated_runs_and_telltale_words() -> None:
    assert is_placeholder_credential("sk-ant-api03-" + "b" * 80)
    assert is_placeholder_credential("sk-ant-api03-ThisIsAnExampleKey12345678901234")
    assert not is_placeholder_credential(REAL_SHAPED_ANTHROPIC_KEY)


def test_secret_names_from_env_merges_both_list_variables() -> None:
    env = {
        "MNGR_SECRET_ENV_NAMES": "GH_TOKEN,ANTHROPIC_API_KEY",
        "VAULT_SECRET_ENV_NAMES": "ANTHROPIC_API_KEY,MODAL_TOKEN_SECRET,",
    }
    assert secret_names_from_env(env) == ["ANTHROPIC_API_KEY", "GH_TOKEN", "MODAL_TOKEN_SECRET"]
    assert secret_names_from_env({}) == []


def test_scrub_paths_redacts_known_values_by_name_and_the_rest_by_pattern(tmp_path: Path) -> None:
    known_key = "sk-ant-api03-" + random_looking_credential_body(88)
    unknown_token = REAL_SHAPED_GITHUB_TOKEN
    report_dir = tmp_path / "report"
    nested_dir = report_dir / "nested"
    nested_dir.mkdir(parents=True)
    known_only = report_dir / "transcript.txt"
    known_only.write_text(f"ANTHROPIC_API_KEY={known_key}\nHOME=/home/runner\n")
    with_unknown = nested_dir / "log.txt"
    with_unknown.write_text(f"first line\nGH_TOKEN={unknown_token}\n")
    clean = nested_dir / "clean.txt"
    clean.write_text("nothing to see\n")
    binary = report_dir / "branch.bundle"
    binary.write_bytes(b"\xff\xfe\x00bundle" + known_key.encode())

    report = scrub_paths((report_dir,), {"ANTHROPIC_API_KEY": known_key}, ["ANTHROPIC_API_KEY"])

    assert known_only.read_text() == "ANTHROPIC_API_KEY=<redacted ANTHROPIC_API_KEY>\nHOME=/home/runner\n"
    assert with_unknown.read_text() == "first line\nGH_TOKEN=<redacted github-token>\n"
    assert clean.read_text() == "nothing to see\n"
    assert binary.read_bytes() == b"\xff\xfe\x00bundle" + known_key.encode()
    assert report.scanned_files == (clean, with_unknown, known_only)
    assert report.redacted_files == (with_unknown, known_only)
    assert report.skipped_files == (binary,)
    assert [(r.path, r.line_number, r.pattern_name) for r in report.pattern_redactions] == [
        (with_unknown, 2, "github-token")
    ]
    assert report.pattern_redactions[0].preview == unknown_token[:8] + "..."
    assert format_pattern_redactions(report.pattern_redactions) == (
        f"{with_unknown}:2: github-token {unknown_token[:8]}..."
    )


def test_scrub_paths_keeps_line_endings_when_it_redacts(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    log.write_bytes(f"a\r\nGH_TOKEN={REAL_SHAPED_GITHUB_TOKEN}\r\nc".encode())

    scrub_paths((log,), {}, [])

    assert log.read_bytes() == b"a\r\nGH_TOKEN=<redacted github-token>\r\nc"


def test_scrub_paths_accepts_single_files_and_ignores_missing_paths(tmp_path: Path) -> None:
    log = tmp_path / "claude.log"
    log.write_text("fine\n")

    report = scrub_paths((log, tmp_path / "absent.txt"), {}, [])

    assert report.scanned_files == (log,)
    assert report.redacted_files == ()
    assert report.pattern_redactions == ()


def test_scrub_paths_on_an_empty_directory_reports_nothing(tmp_path: Path) -> None:
    report = scrub_paths((tmp_path,), {"X": "secret"}, ["X"])

    assert report.scanned_files == ()
    assert report.pattern_redactions == ()
