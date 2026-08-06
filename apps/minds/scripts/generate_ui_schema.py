#!/usr/bin/env python3
"""Emit the consolidated JSON Schema for the /ui wire contract.

The frontend build runs this (via ``pnpm generate``) and feeds the output to a
JSON-Schema-to-TypeScript generator, so the SPA's channel/bootstrap types are
derived from the pydantic models in ``ui_models.py`` rather than hand-written.
Deterministic output (sorted keys, stable ordering) keeps regeneration diffs
meaningful.

Run from the repo root:

    uv run --package minds python apps/minds/scripts/generate_ui_schema.py [--out PATH]
"""

import json
from pathlib import Path

import click

from imbue.minds.desktop_client.ui_models import UiWireSchema
from imbue.minds.utils.output import write_stdout_line


def render_ui_schema_json() -> str:
    """The consolidated wire schema as deterministic, pretty-printed JSON."""
    schema = UiWireSchema.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


@click.command()
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the schema to this file instead of stdout",
)
def main(out_path: Path | None) -> None:
    rendered = render_ui_schema_json()
    if out_path is None:
        write_stdout_line(rendered.rstrip("\n"))
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)


if __name__ == "__main__":
    main()
