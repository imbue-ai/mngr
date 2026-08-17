"""Resolution of output-style names against style files.

An *output style* is a markdown file whose YAML frontmatter carries a `name:` --
the human-readable label a create template names in `output_style`. The label,
not the filename, is the identifier, so resolving one means reading the
frontmatter of every candidate file and matching on that field.

Agent types consume the result differently: claude only needs the name validated
(it re-resolves the file itself at launch, from its own output-style directory),
while an agent type with no output-style concept needs the *body* so it can fold
the style into its system prompt. Both halves come from the same lookup, so it
lives here rather than in either plugin.

Matching is pure and separately testable; ``read_output_style_files`` is the one
host-touching piece, shared so each agent type does not reimplement the same
directory walk. A style directory lives in the agent's work_dir, which may be on
a remote host, so the read goes through ``OnlineHostInterface``.
"""

from pathlib import Path

import frontmatter

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import OutputStyleName

# Suffix of a style file. Anything else in the directory is ignored.
STYLE_FILE_SUFFIX: str = ".md"

# The frontmatter field naming an output style. Claude Code matches on this same
# field, so the two resolvers agree on what a style is called.
STYLE_NAME_FIELD: str = "name"


@pure
def parse_output_style_name(file_contents: str) -> OutputStyleName | None:
    """Return the `name:` frontmatter of one style file, or None if it has none.

    None covers both "no frontmatter block at all" and "frontmatter without a
    usable `name`" -- neither is an error on its own, since a directory may hold
    files that are not styles. Only a failed *lookup* is an error, and that is
    ``resolve_output_style``'s call to make.
    """
    parsed = frontmatter.loads(file_contents)
    raw_name = parsed.metadata.get(STYLE_NAME_FIELD)
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    return OutputStyleName(raw_name.strip())


@pure
def resolve_output_style(
    requested_name: OutputStyleName,
    files_by_path: dict[str, str],
) -> str:
    """Return the body (frontmatter included) of the style named ``requested_name``.

    ``files_by_path`` maps a display path to that file's full contents -- the caller
    supplies it after reading the agent type's output-style directory through its host.
    Paths are used only to make errors concrete; matching is on frontmatter alone.

    The returned body is verbatim, frontmatter block and all. Agent types that fold a
    style into a system prompt pass it through unchanged, so a style reads the same
    whichever agent type runs it.

    Raises UserInputError when no file carries a matching `name:`, listing the names
    that are available. Failing the create is the point: a silent miss would launch an
    unstyled agent with nothing to indicate the style never applied.
    """
    available: list[OutputStyleName] = []
    for path in sorted(files_by_path):
        style_name = parse_output_style_name(files_by_path[path])
        if style_name is None:
            continue
        if style_name == requested_name:
            return files_by_path[path]
        available.append(style_name)

    if available:
        known = ", ".join(f"'{name}'" for name in sorted(available))
        raise UserInputError(f"No output style named '{requested_name}'. Available output styles: {known}")
    raise UserInputError(
        f"No output style named '{requested_name}': no output styles are defined. "
        f"An output style is a markdown file whose frontmatter sets `{STYLE_NAME_FIELD}:`."
    )


def read_output_style_files(host: OnlineHostInterface, styles_dir: Path) -> dict[str, str]:
    """Read every ``.md`` in ``styles_dir`` on ``host``, keyed by path.

    An absent directory yields an empty mapping rather than an error, so the caller's
    ``resolve_output_style`` produces the one "no such style" message either way instead
    of two different failures for what a user experiences as the same mistake.

    ``styles_dir`` should be the directory the *agent type itself* reads, not a shared
    source of truth: where those differ by a symlink, validating the agent's own path is
    what catches a broken link before launch instead of after.
    """
    if not host.path_exists(styles_dir):
        return {}
    files_by_path: dict[str, str] = {}
    for entry in host.list_directory(styles_dir):
        if entry.file_type == FileType.DIRECTORY or not entry.path.endswith(STYLE_FILE_SUFFIX):
            continue
        entry_path = styles_dir / Path(entry.path).name
        files_by_path[str(entry_path)] = host.read_text_file(entry_path)
    return files_by_path
