"""Unit tests for design_tokens.py."""

import pytest

from imbue.minds.desktop_client.design_tokens import DEFAULT_WORKSPACE_PRESET
from imbue.minds.desktop_client.design_tokens import Theme
from imbue.minds.desktop_client.design_tokens import WORKSPACE_PRESETS
from imbue.minds.desktop_client.design_tokens import WorkspaceColor
from imbue.minds.desktop_client.design_tokens import WorkspacePreset
from imbue.minds.desktop_client.design_tokens import oklch_starting_color
from imbue.minds.desktop_client.design_tokens import theme_for


def test_palette_has_entry_for_every_preset() -> None:
    assert set(WORKSPACE_PRESETS.keys()) == set(WorkspacePreset)


def test_default_preset_is_confusion() -> None:
    assert DEFAULT_WORKSPACE_PRESET is WorkspacePreset.CONFUSION


def test_workspace_color_accepts_preset_slug() -> None:
    color = WorkspaceColor("confusion")
    assert color == "confusion"
    assert color.is_preset()
    assert color.is_preset(WorkspacePreset.CONFUSION)
    assert not color.is_preset(WorkspacePreset.PEACE)


def test_workspace_color_accepts_hex() -> None:
    color = WorkspaceColor("#9FBBD3")
    assert color == "#9FBBD3"
    assert not color.is_preset()


def test_workspace_color_accepts_hex_with_alpha() -> None:
    color = WorkspaceColor("#9FBBD3FF")
    assert color == "#9FBBD3FF"


def test_workspace_color_accepts_oklch() -> None:
    color = WorkspaceColor("oklch(75% 0.15 230)")
    assert color == "oklch(75% 0.15 230)"


def test_workspace_color_accepts_rgb() -> None:
    color = WorkspaceColor("rgb(159 187 211)")
    assert color == "rgb(159 187 211)"


def test_workspace_color_strips_whitespace() -> None:
    assert WorkspaceColor("  confusion  ") == "confusion"


def test_workspace_color_rejects_empty() -> None:
    with pytest.raises(ValueError):
        WorkspaceColor("")
    with pytest.raises(ValueError):
        WorkspaceColor("   ")


def test_workspace_color_rejects_unknown_slug() -> None:
    with pytest.raises(ValueError):
        WorkspaceColor("mauve")


def test_workspace_color_rejects_unparseable_literal() -> None:
    with pytest.raises(ValueError):
        WorkspaceColor("not a color")
    # 4-character hex is not supported; only 6 (RGB) or 8 (RGBA) forms.
    with pytest.raises(ValueError):
        WorkspaceColor("#1234")
    with pytest.raises(ValueError):
        WorkspaceColor("hsl(220 50% 50%)")


def test_resolve_hex_for_preset_returns_palette_value() -> None:
    assert WorkspaceColor("confusion").resolve_hex() == "#0B292B"
    assert WorkspaceColor("clarity").resolve_hex() == "#FCEFD4"


def test_resolve_hex_for_literal_passes_through() -> None:
    assert WorkspaceColor("#ABCDEF").resolve_hex() == "#ABCDEF"
    assert WorkspaceColor("oklch(75% 0.15 230)").resolve_hex() == "oklch(75% 0.15 230)"


# -- theme inference --
# Spot-check each preset against the Figma intent: pure-black + dark teals/maroons
# read as DARK (white text); pastels read as LIGHT (black text).


@pytest.mark.parametrize(
    "preset",
    [
        WorkspacePreset.INDIFFERENCE,
        WorkspacePreset.CONFUSION,
        WorkspacePreset.COURAGE,
        WorkspacePreset.ENVY,
    ],
)
def test_theme_for_dark_presets(preset: WorkspacePreset) -> None:
    assert theme_for(WorkspaceColor(preset.value)) is Theme.DARK


@pytest.mark.parametrize(
    "preset",
    [
        WorkspacePreset.PEACE,
        WorkspacePreset.BELONGING,
        WorkspacePreset.ENERGY,
        WorkspacePreset.STRENGTH,
        WorkspacePreset.COMFORT,
        WorkspacePreset.INSPIRATION,
        WorkspacePreset.CLARITY,
    ],
)
def test_theme_for_light_presets(preset: WorkspacePreset) -> None:
    assert theme_for(WorkspaceColor(preset.value)) is Theme.LIGHT


def test_theme_for_oklch_75_percent_resolves_light() -> None:
    # OKLCH L=75% is well above the 0.5 threshold; the migration-derived
    # starting colors should land on the light side regardless of hue.
    for hue in (0, 90, 180, 270, 359):
        color = WorkspaceColor(f"oklch(75% 0.15 {hue})")
        assert theme_for(color) is Theme.LIGHT, f"hue {hue} should resolve LIGHT"


def test_theme_for_oklch_20_percent_resolves_dark() -> None:
    for hue in (0, 90, 180, 270, 359):
        color = WorkspaceColor(f"oklch(20% 0.15 {hue})")
        assert theme_for(color) is Theme.DARK


def test_theme_for_rgb_literal() -> None:
    # Matches the hex path; rgb is just a different format for the same color.
    assert theme_for(WorkspaceColor("rgb(0 0 0)")) is Theme.DARK
    assert theme_for(WorkspaceColor("rgb(255 255 255)")) is Theme.LIGHT


# -- migration helper --


def test_oklch_starting_color_is_deterministic_per_agent_id() -> None:
    agent_id = "agent-abcdefghijklmnopqrstuvwxyz000001"
    first = oklch_starting_color(agent_id)
    second = oklch_starting_color(agent_id)
    assert first == second


def test_oklch_starting_color_uses_75_percent_lightness() -> None:
    color = oklch_starting_color("agent-test")
    # The lightness is the value that drives the bump-from-65% migration.
    assert color.startswith("oklch(75%")


def test_oklch_starting_color_differs_across_agent_ids() -> None:
    a = oklch_starting_color("agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    b = oklch_starting_color("agent-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert a != b


def test_oklch_starting_color_is_a_valid_workspace_color() -> None:
    # Round-trips through the WorkspaceColor validator so the OKLCH literal
    # we produce matches the regex used at the persistence boundary.
    color = oklch_starting_color("agent-test")
    reparsed = WorkspaceColor(str(color))
    assert reparsed == color
