from typing import Final

PROFILES_DIRNAME: Final[str] = "profiles"
ROOT_CONFIG_FILENAME: Final[str] = "config.toml"
# Filenames of the per-scope settings files, relative to their containing
# directory (the user profile dir, or the resolved project config dir). Kept
# here as the single source of truth so the user / project / local path
# helpers can't drift apart.
SETTINGS_FILENAME: Final[str] = "settings.toml"
LOCAL_SETTINGS_FILENAME: Final[str] = "settings.local.toml"
