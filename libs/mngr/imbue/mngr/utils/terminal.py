from typing import Final

ANSI_ERASE_LINE: Final[str] = "\r\x1b[K"
ANSI_DIM_GRAY: Final[str] = "\x1b[38;5;245m"
ANSI_RESET: Final[str] = "\x1b[0m"
