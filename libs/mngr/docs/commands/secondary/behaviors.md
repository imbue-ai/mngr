<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr behaviors
**Usage:**

```text
mngr behaviors [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr behaviors validate

**Usage:**

```text
mngr behaviors validate [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--root` | directory | Corpus root directory, conventionally <project>/behaviors (e.g. apps/minds/behaviors). Record file paths are formed from the root as given, so run from the repo root for repo-relative paths. | None |

## mngr behaviors list

**Usage:**

```text
mngr behaviors list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--root` | directory | Corpus root directory, conventionally <project>/behaviors (e.g. apps/minds/behaviors). Record file paths are formed from the root as given, so run from the repo root for repo-relative paths. | None |
| `--unit` | choice (`rule` &#x7C; `scenario` &#x7C; `scenario-outline`) | Only emit units of this kind. | None |
| `--area` | text | Keep units in this folder subtree, named as a dot-joined folder path from the corpus root (e.g. 'browser-authorization' or 'networking.tunnels'). Matched whole folder segment by segment, so 'browser' does not match the folder 'browser-authorization', and (unlike --tag) it never matches on a unit's identity tag. | None |
| `--tag` | text | Keep units with this exact raw tag (identity or auxiliary; a leading '@' is tolerated) or this exact coordinate. Auxiliary tags may be shared, so several units can match. | None |
| `--name` | text | Keep units whose name contains this substring (case-insensitive). | None |
| `--step` | text | Keep units where any step text contains this substring (case-insensitive). | None |

## mngr behaviors matrix

**Usage:**

```text
mngr behaviors matrix [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--root` | directory | Corpus root directory, conventionally <project>/behaviors (e.g. apps/minds/behaviors). Record file paths are formed from the root as given, so run from the repo root for repo-relative paths. | None |
| `--tests` | path | Test root to collect `witnesses` markers from; repeatable. Passed to an inner pytest --collect-only run, so paths resolve from the current directory. When omitted, defaults to the corpus root's parent directory (a corpus at <project>/behaviors is witnessed by <project>'s tests), so run from the repo root (or pass --tests). | `()` |
