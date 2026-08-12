from imbue.minds.desktop_client.host_names import make_unique_host_name
from imbue.minds.desktop_client.host_names import resolve_create_host_name


def test_resolve_create_host_name_uses_submitted_value() -> None:
    assert str(resolve_create_host_name("my-workspace")) == "my-workspace"


def test_resolve_create_host_name_generates_workspace_name_when_empty() -> None:
    # No submitted name and no existing workspaces -> the first ``workspace-N`` name.
    assert str(resolve_create_host_name("")) == "workspace-1"


def test_resolve_create_host_name_picks_next_free_workspace_name() -> None:
    # The fallback skips names already in use across providers.
    assert str(resolve_create_host_name("", {"workspace-1", "workspace-2"})) == "workspace-3"


def test_make_unique_host_name_numbered_empty_is_one() -> None:
    assert str(make_unique_host_name("mind", set(), always_number=True)) == "mind-1"


def test_make_unique_host_name_numbered_increments_past_used() -> None:
    assert str(make_unique_host_name("mind", {"mind-1", "mind-2", "mind-3"}, always_number=True)) == "mind-4"


def test_make_unique_host_name_numbered_reuses_lowest_gap() -> None:
    # A destroyed ``mind-2`` leaves a gap that is filled before climbing higher.
    assert str(make_unique_host_name("mind", {"mind-1", "mind-3"}, always_number=True)) == "mind-2"


def test_make_unique_host_name_numbered_ignores_non_canonical_suffixes() -> None:
    # Names that merely start with ``mind-`` but are not a canonical positive
    # integer (a coolname, a zero-padded number, ``mind-0``) do not take the
    # ``mind-1`` slot, and unrelated names are ignored entirely.
    existing = {"mind-foo", "mind-01", "mind-0", "brave-cool-otter", "mindful"}
    assert str(make_unique_host_name("mind", existing, always_number=True)) == "mind-1"


def test_make_unique_host_name_bare_when_free() -> None:
    assert str(make_unique_host_name("mindtest", set())) == "mindtest"
    assert str(make_unique_host_name("mindtest", {"other"})) == "mindtest"


def test_make_unique_host_name_bare_then_numbered_from_two() -> None:
    # When the bare base is taken, suffixes start at 2 (so the bare name reads
    # as the "first").
    assert str(make_unique_host_name("mindtest", {"mindtest"})) == "mindtest-2"
    assert str(make_unique_host_name("mindtest", {"mindtest", "mindtest-2"})) == "mindtest-3"
