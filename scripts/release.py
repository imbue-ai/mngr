"""Selectively bump and publish changed packages to PyPI.

Only packages that changed since the last release (or their dependents) are bumped.
The tag is always based on the mngr version, so mngr is always bumped to ensure a
unique tag. This cascades to mngr's dependents (they must update their pin).

Bump levels cascade upward through the dependency DAG: if a package is bumped at
a given level, all its dependents must be bumped at at least that level (because
their pinned dependency changed). Use --minor/--major to override specific packages
above the base level.

Usage:
    uv run scripts/release.py patch                    # all get patch
    uv run scripts/release.py patch --minor mngr        # mngr+ get minor, rest get patch
    uv run scripts/release.py patch --dry-run          # preview without changes
    uv run scripts/release.py --watch                  # watch publish workflow
    uv run scripts/release.py --retry                  # rerun failed jobs and watch

The script refuses to cut a release while there are unconsolidated entries in
any project's ``<project_dir>/changelog/`` (those bullets would otherwise be
omitted from the version's release notes). When the gate fires it prints the
on-demand invocation of the
``changelog-consolidation`` schedule on stderr; run that, land the resulting
PR, then re-run this script. ``--dry-run`` downgrades the gate to a warning
so the preview still works.
"""

import argparse
import json
import subprocess
import sys
from collections import deque
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import TextIO

import httpx
import semver
import tomlkit
from changelog_release_utils import finalize_changelog_unreleased
from changelog_release_utils import today_pacific
from consolidate_changelog import pending_changelog_entries
from trigger_changelog_consolidation import MNGR_ROOT_NAME as CHANGELOG_MNGR_ROOT_NAME
from trigger_changelog_consolidation import PROVIDER as CHANGELOG_PROVIDER
from trigger_changelog_consolidation import TRIGGER_NAME as CHANGELOG_TRIGGER_NAME
from trigger_changelog_consolidation import disable_plugin_args as changelog_disable_plugin_args
from utils import PACKAGES
from utils import PACKAGE_BY_PYPI_NAME
from utils import REPO_ROOT
from utils import get_package_versions
from utils import parse_dep_name

from imbue.mngr.utils.polling import poll_for_value

BUMP_KINDS: Final[tuple[str, ...]] = ("major", "minor", "patch")
BUMP_LEVEL_ORDER: Final[dict[str, int]] = {"patch": 0, "minor": 1, "major": 2}

# Supply-chain cooldown window: the resolver only adopts registry releases that
# have been public at least this long. Enforced via the root `[tool.uv]
# exclude-newer` cutoff, which a release advances to (release date - this window).
DEPENDENCY_COOLDOWN: Final[timedelta] = timedelta(weeks=2)

PUBLISH_WORKFLOW: Final[str] = "publish.yml"
RELEASE_TESTS_WORKFLOW: Final[str] = "release-tests.yml"
ACTIONS_URL: Final[str] = "https://github.com/imbue-ai/mngr/actions/workflows/publish.yml"
POLL_INTERVAL_SECONDS: Final[int] = 10
MAX_WAIT_FOR_RUN_SECONDS: Final[int] = 300
SLOW_START_WARNING_SECONDS: Final[int] = 60


def run(*args: str) -> str:
    """Run a command in the repo root. Returns stripped stdout."""
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()


def get_mngr_version() -> str:
    """Read the current mngr package version (used for tag naming)."""
    versions = get_package_versions()
    return versions["imbue-mngr"]


def _find_last_release_tag() -> str:
    """Find the most recent v* tag reachable from HEAD. Fetches tags from origin first."""
    run("git", "fetch", "--tags", "origin")
    try:
        return run("git", "describe", "--tags", "--match", "v*", "--abbrev=0")
    except subprocess.CalledProcessError:
        print("ERROR: No v* tags found. Cannot determine what changed.", file=sys.stderr)
        sys.exit(1)


def _get_pypi_version() -> str | None:
    """Query PyPI for the latest published version of mngr. Returns None if the query fails."""
    try:
        response = httpx.get("https://pypi.org/pypi/imbue-mngr/json", timeout=10)
        response.raise_for_status()
        return response.json()["info"]["version"]
    except Exception:
        return None


def _detect_changed_packages(since_tag: str) -> set[str]:
    """Return the set of pypi names for packages whose source changed since the given tag."""
    changed: set[str] = set()
    for pkg in PACKAGES:
        # git diff --quiet exits 1 if there are differences
        result = subprocess.run(
            ["git", "diff", "--quiet", since_tag, "HEAD", "--", f"libs/{pkg.dir_name}/"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if result.returncode != 0:
            changed.add(pkg.pypi_name)
    return changed


def _is_published_on_pypi(pypi_name: str) -> bool:
    """Check whether a package has ever been published on PyPI."""
    try:
        response = httpx.head(f"https://pypi.org/pypi/{pypi_name}/json", timeout=10)
        return response.status_code == 200
    except Exception:
        # If we can't reach PyPI, assume published to avoid accidentally
        # treating existing packages as new.
        return True


def _detect_new_packages(since_tag: str) -> set[str]:
    """Return the set of pypi names for packages that have never been released.

    A package is considered new if either:
    - Its pyproject.toml didn't exist at the given tag, OR
    - It has never been published on PyPI
    """
    new: set[str] = set()
    for pkg in PACKAGES:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{since_tag}:libs/{pkg.dir_name}/pyproject.toml"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if result.returncode != 0:
            new.add(pkg.pypi_name)
        elif not _is_published_on_pypi(pkg.pypi_name):
            print(f"  {pkg.pypi_name}: exists in repo but not on PyPI, treating as new")
            new.add(pkg.pypi_name)
    return new


def _confirm_new_packages(new_packages: set[str], current_versions: dict[str, str]) -> set[str]:
    """Prompt the user to confirm first-time publication for each new package.

    Returns the set of confirmed package names.
    """
    confirmed: set[str] = set()
    for name in sorted(new_packages):
        version = current_versions[name]
        answer = input(f"\n{name} appears to be a new package. Publish it for the first time at {version}? [y/N] ")
        if answer.lower() == "y":
            confirmed.add(name)
        else:
            print(f"  Skipping {name}.")
    return confirmed


def _print_trusted_publisher_warning(confirmed_new: set[str]) -> None:
    """Print a reminder to register pending Trusted Publishers for each new package.

    No-op when `confirmed_new` is empty.
    """
    if not confirmed_new:
        return
    print()
    print("=" * 72)
    print("ACTION REQUIRED: register a pending Trusted Publisher on PyPI for each")
    print("new package before the publish workflow runs:")
    for name in sorted(confirmed_new):
        print(f"  - {name}")
    print()
    print("  https://pypi.org/manage/account/publishing/")
    print()
    print("WARNING: PyPI only allows ONE pending publisher per account at a time.")
    print("If multiple new packages are released in the same tag, the publish")
    print("workflow will fail on each unregistered package in turn. You will need")
    print("to register the next pending publisher and re-run the failed workflow")
    print("ONCE PER NEW PACKAGE until all are published.")
    print("=" * 72)


def _cascade_reverse_deps(
    seeds: deque[str],
    reverse_deps: dict[str, list[str]],
    to_bump: dict[str, str],
) -> None:
    """BFS through reverse deps, marking unvisited dependents as "cascade"."""
    while seeds:
        current = seeds.popleft()
        for dependent in reverse_deps.get(current, []):
            if dependent not in to_bump:
                to_bump[dependent] = "cascade"
                seeds.append(dependent)


def _compute_bump_set(directly_changed: set[str]) -> dict[str, str]:
    """Compute the full set of packages to bump and the reason for each.

    Returns {pypi_name: reason} where reason is "changed", "cascade", or "always".
    """
    # Build reverse dependency map
    reverse_deps: dict[str, list[str]] = {pkg.pypi_name: [] for pkg in PACKAGES}
    for pkg in PACKAGES:
        for dep in pkg.internal_deps:
            reverse_deps[dep].append(pkg.pypi_name)

    # BFS from directly changed packages through reverse deps
    to_bump: dict[str, str] = {}
    for name in directly_changed:
        to_bump[name] = "changed"
    _cascade_reverse_deps(deque(directly_changed), reverse_deps, to_bump)

    # mngr is always bumped (tag is v<mngr-version>)
    if "imbue-mngr" not in to_bump:
        to_bump["imbue-mngr"] = "always"
        _cascade_reverse_deps(deque(["imbue-mngr"]), reverse_deps, to_bump)

    return to_bump


def _max_bump_kind(a: str, b: str) -> str:
    """Return the higher of two bump kinds (major > minor > patch)."""
    if BUMP_LEVEL_ORDER[a] >= BUMP_LEVEL_ORDER[b]:
        return a
    else:
        return b


def _compute_bump_levels(
    to_bump: dict[str, str],
    base_kind: str,
    overrides: dict[str, str],
) -> dict[str, str]:
    """Compute per-package bump levels with upward cascade through the DAG.

    Each package starts at base_kind (or its override if specified). Then, in
    topological order, each package's level is raised to at least the max level
    of its bumped internal dependencies.
    """
    levels: dict[str, str] = {}
    for name in to_bump:
        levels[name] = overrides.get(name, base_kind)

    # PACKAGES is already in topological order (deps before dependents)
    for pkg in PACKAGES:
        if pkg.pypi_name not in levels:
            continue
        # Cascade: this package's level must be >= max level of its bumped deps
        for dep_name in pkg.internal_deps:
            if dep_name in levels:
                levels[pkg.pypi_name] = _max_bump_kind(levels[pkg.pypi_name], levels[dep_name])

    return levels


def bump_package_versions(
    bump_levels: dict[str, str],
    current_versions: dict[str, str],
) -> dict[str, str]:
    """Apply per-package bump levels. Returns {pypi_name: new_version}."""
    new_versions: dict[str, str] = {}
    for name, bump_kind in bump_levels.items():
        current = semver.Version.parse(current_versions[name])
        new_versions[name] = str(current.next_version(bump_kind))
    return new_versions


def _write_version(pkg_pypi_name: str, new_version: str) -> None:
    """Update the version field in a package's pyproject.toml."""
    pkg = PACKAGE_BY_PYPI_NAME[pkg_pypi_name]
    doc = tomlkit.loads(pkg.pyproject_path.read_text())
    project = doc["project"]
    project["version"] = new_version
    pkg.pyproject_path.write_text(tomlkit.dumps(doc))


def update_internal_dep_pins(all_versions: dict[str, str]) -> list[str]:
    """Rewrite internal dep entries to use == pins matching current versions.

    Returns list of packages whose pyproject.toml was modified.
    """
    modified: list[str] = []
    for pkg in PACKAGES:
        if not pkg.internal_deps:
            continue
        doc = tomlkit.loads(pkg.pyproject_path.read_text())
        project = doc["project"]
        # Modify the tomlkit array in-place to preserve formatting and comments
        deps = project["dependencies"]
        is_changed = False
        for idx in range(len(deps)):
            dep_str = str(deps[idx])
            dep_name = parse_dep_name(dep_str)
            if dep_name in all_versions:
                canonical_name = PACKAGE_BY_PYPI_NAME[dep_name].pypi_name
                new_dep = f"{canonical_name}=={all_versions[dep_name]}"
                if dep_str != new_dep:
                    deps[idx] = new_dep
                    is_changed = True
        if is_changed:
            pkg.pyproject_path.write_text(tomlkit.dumps(doc))
            modified.append(pkg.pypi_name)
    return modified


def update_exclude_newer(pyproject_path: Path, release_date: date) -> str | None:
    """Advance the root ``[tool.uv] exclude-newer`` cutoff, forward-only.

    The cutoff is the supply-chain cooldown boundary: uv refuses to consider any
    package uploaded after it when resolving, so we only adopt registry releases
    that have been public long enough for the community to flag malware. We move
    it to ``release_date`` minus the cooldown window, but never backward -- if the
    current cutoff is still younger than the window (e.g. it was set recently to
    admit a freshly-pinned, deliberately-trusted dep), pushing it back would
    re-exclude that dep and break resolution. So the new cutoff is the later
    of the current value and ``release_date - DEPENDENCY_COOLDOWN``.

    The cutoff is anchored at midnight UTC, matching the UTC upload-times uv
    compares it against. The committed value is therefore identical regardless of
    who cuts the release, and the time-of-day is immaterial for a two-week boundary.

    Returns the new cutoff string if it changed, or ``None`` if the current cutoff
    already wins (in which case no write is performed).
    """
    doc = tomlkit.loads(pyproject_path.read_text())
    uv_config = doc["tool"]["uv"]
    current = datetime.fromisoformat(str(uv_config["exclude-newer"]))
    candidate_date = release_date - DEPENDENCY_COOLDOWN
    candidate = datetime(candidate_date.year, candidate_date.month, candidate_date.day, tzinfo=timezone.utc)
    new_cutoff = max(current, candidate)
    if new_cutoff == current:
        return None
    new_value = new_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    uv_config["exclude-newer"] = new_value
    pyproject_path.write_text(tomlkit.dumps(doc))
    return new_value


def gh_is_available() -> bool:
    """Check whether the gh CLI is installed and authenticated."""
    try:
        run("gh", "auth", "status")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _try_find_run_id(tag: str) -> str | None:
    """Check if a publish workflow run exists for the given tag. Returns run ID or None."""
    result = run(
        "gh",
        "run",
        "list",
        "-w",
        PUBLISH_WORKFLOW,
        "-b",
        tag,
        "--json",
        "databaseId,status",
        "-L",
        "1",
    )
    if result:
        runs = json.loads(result)
        if runs:
            return str(runs[0]["databaseId"])
    return None


def _try_get_conclusion(run_id: str, after_workflow_attempt: int) -> str | None:
    """Check if a workflow run has completed after a given attempt.

    Returns the conclusion if the run is completed with attempt > after_workflow_attempt.
    Pass after_workflow_attempt=0 to match any attempt.
    """
    result = run("gh", "run", "view", run_id, "--json", "status,conclusion,attempt")
    data = json.loads(result)
    if data["status"] == "completed" and data["attempt"] > after_workflow_attempt:
        return data["conclusion"]
    return None


def find_publish_run_id(tag: str) -> str:
    """Find the workflow run ID for the publish workflow triggered by a tag push.

    Polls until the run appears (there may be a brief delay after pushing).
    """
    # Try for 60s, then warn and keep waiting
    run_id, _, _ = poll_for_value(lambda: _try_find_run_id(tag), timeout=SLOW_START_WARNING_SECONDS, poll_interval=2)
    if run_id is None:
        print("This is taking longer than expected, still waiting...")
        remaining_seconds = MAX_WAIT_FOR_RUN_SECONDS - SLOW_START_WARNING_SECONDS
        run_id, _, _ = poll_for_value(lambda: _try_find_run_id(tag), timeout=remaining_seconds, poll_interval=2)

    if run_id is not None:
        print(f"Tracking publish workflow (run {run_id})")
        return run_id

    print("ERROR: Could not find publish workflow run.", file=sys.stderr)
    print(f"Check manually: {ACTIONS_URL}", file=sys.stderr)
    sys.exit(1)


def wait_for_run_completion(run_id: str, after_workflow_attempt: int) -> str:
    """Poll until the workflow run completes. Returns the conclusion (e.g. 'success', 'failure')."""
    conclusion, _, _ = poll_for_value(
        lambda: _try_get_conclusion(run_id, after_workflow_attempt), timeout=1800, poll_interval=POLL_INTERVAL_SECONDS
    )
    if conclusion is not None:
        return conclusion
    print("ERROR: Workflow did not complete within 30 minutes.", file=sys.stderr)
    print(f"Check manually: https://github.com/imbue-ai/mngr/actions/runs/{run_id}", file=sys.stderr)
    sys.exit(1)


def print_run_failure(run_id: str) -> None:
    """Print the failure logs for a workflow run."""
    print("\n--- Workflow failure logs ---\n")
    try:
        logs = run("gh", "run", "view", run_id, "--log-failed")
        print(logs)
    except subprocess.CalledProcessError:
        print("(Could not retrieve failure logs)")
    print(f"\nFull details: https://github.com/imbue-ai/mngr/actions/runs/{run_id}")


def _get_workflow_attempt_number(run_id: str) -> int:
    """Get the current attempt number for a workflow run."""
    result = run("gh", "run", "view", run_id, "--json", "attempt")
    return json.loads(result)["attempt"]


def watch_publish_workflow(run_id: str, after_workflow_attempt: int = 0) -> None:
    """Watch a publish workflow run until it completes.

    On failure, prints the error logs and the commands to watch/retry.
    """
    conclusion = wait_for_run_completion(run_id, after_workflow_attempt)

    if conclusion == "success":
        print("Publish workflow succeeded!")
        return

    print_run_failure(run_id)
    print()
    print("To retry failed jobs and watch:")
    print("  uv run scripts/release.py --retry")
    sys.exit(1)


def _print_bump_summary(
    directly_changed: set[str],
    to_bump: dict[str, str],
    bump_levels: dict[str, str],
    current_versions: dict[str, str],
    new_versions: dict[str, str],
    confirmed_new: set[str],
) -> None:
    """Print a summary of what will be bumped and why."""
    print("Directly changed packages:")
    if directly_changed:
        for name in sorted(directly_changed):
            print(f"  {name}")
    else:
        print("  (none)")

    if confirmed_new:
        print()
        print("New packages (first publication):")
        for pkg in PACKAGES:
            if pkg.pypi_name in confirmed_new:
                print(f"  {pkg.pypi_name}: {current_versions[pkg.pypi_name]} (new)")

    print()
    print("Packages to bump:")
    bumped = [pkg for pkg in PACKAGES if pkg.pypi_name in to_bump]
    if bumped:
        for pkg in bumped:
            name = pkg.pypi_name
            reason = to_bump[name]
            level = bump_levels[name]
            old_v = current_versions[name]
            new_v = new_versions[name]
            print(f"  {name}: {old_v} -> {new_v} ({level}, {reason})")
    else:
        print("  (none)")

    print()
    print("Packages unchanged:")
    all_included = set(to_bump) | confirmed_new
    unchanged = [pkg.pypi_name for pkg in PACKAGES if pkg.pypi_name not in all_included]
    if unchanged:
        for name in unchanged:
            print(f"  {name} (stays at {current_versions[name]})")
    else:
        print("  (none)")


def _format_pending_changelog_list(entries: list[Path], repo_root: Path) -> str:
    return "\n".join(f"  - {entry.relative_to(repo_root)}" for entry in entries)


def _pluralize_entry(count: int) -> str:
    return "entry" if count == 1 else "entries"


def _print_on_demand_consolidation_command(file: TextIO) -> None:
    """Print the one-liner that triggers an on-demand consolidation run.

    Equivalent to the example invocation in ``setup_changelog_agent.sh``'s
    header so the user can copy-paste it directly. Uses the shared
    constants and helper so the disable-plugin list stays in sync with
    the deploy script. The deploy script's header inlines that list as
    ``$DISABLE_PLUGIN_ARGS``; this helper expands it onto its own line
    because the resolved value is long.

    The provider is the shared ``CHANGELOG_PROVIDER`` constant, so the
    printed command targets the same provider the schedule was deployed
    against; changing providers requires editing the constant and
    redeploying the schedule together.

    ``file`` is the stream to write to (forwarded to ``print``). The
    caller in the gate's error path passes ``sys.stderr`` so the
    on-demand command lands on the same stream as the surrounding error
    message.
    """
    disable_args = " ".join(changelog_disable_plugin_args())
    print(f"  env -u MNGR_HOST_DIR -u MNGR_PREFIX MNGR_ROOT_NAME={CHANGELOG_MNGR_ROOT_NAME} \\", file=file)
    # Only emit a continuation + third line when there are disable-plugin
    # args to print. Otherwise the command would end with a trailing
    # backslash followed by a whitespace-only line, which makes the
    # copy-paste form malformed (the empty line terminates the
    # continuation and the leading spaces become a stray empty command).
    if disable_args:
        print(f"    uv run mngr schedule run {CHANGELOG_TRIGGER_NAME} --provider {CHANGELOG_PROVIDER} \\", file=file)
        print(f"    {disable_args}", file=file)
    else:
        print(f"    uv run mngr schedule run {CHANGELOG_TRIGGER_NAME} --provider {CHANGELOG_PROVIDER}", file=file)


def _gate_release_on_pending_changelog_entries(repo_root: Path, dry_run: bool) -> bool:
    """Block a release until pending changelog entries are consolidated.

    Walks each known project's ``<project_dir>/changelog/`` directory
    under ``repo_root`` via ``pending_changelog_entries``. Taking the
    path as a parameter (rather than always reading the module-level
    ``REPO_ROOT``) is the production contract -- the gate's job is to
    inspect a particular repo -- and conveniently lets tests pass a
    ``tmp_path`` populated with synthetic entries.

    Returns ``True`` if the release may proceed (no pending entries, or
    ``dry_run`` is set), ``False`` if the caller must abort. After
    consolidating (waiting for the nightly cron or running the on-demand
    one-liner this prints), the user re-runs ``release.py``.

    ``dry_run`` swaps the error for a warning so ``release.py --dry-run``
    can still preview what would be released.
    """
    entries = pending_changelog_entries(repo_root)
    if not entries:
        return True

    entry_word = _pluralize_entry(len(entries))
    if dry_run:
        print()
        print(f"WARNING: {len(entries)} pending changelog {entry_word} would block a real release:")
        print(_format_pending_changelog_list(entries, repo_root))
        print(f"(consolidate via the '{CHANGELOG_TRIGGER_NAME}' schedule before cutting the release)")
        print()
        return True

    # Route the block-release path to stderr to match every other
    # 'ERROR:' message in this file (see _find_last_release_tag, the
    # workflow polling helpers, the branch check, etc.). The dry-run
    # WARNING above stays on stdout to match release.py's other
    # informational WARNINGs (e.g. the empty-[Unreleased] notice).
    print(file=sys.stderr)
    print(f"ERROR: cannot release with {len(entries)} pending changelog {entry_word}.", file=sys.stderr)
    print(file=sys.stderr)
    print("The following entries in per-project changelog/ dirs haven't been consolidated into", file=sys.stderr)
    print("their projects' CHANGELOG.md [Unreleased] sections yet:", file=sys.stderr)
    print(_format_pending_changelog_list(entries, repo_root), file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"The '{CHANGELOG_TRIGGER_NAME}' schedule runs nightly at 08:00 UTC (midnight or 1 AM Pacific, depending on DST). To",
        file=sys.stderr,
    )
    print("trigger it on demand instead (opens a PR you can merge before re-running", file=sys.stderr)
    print("this script), run:", file=sys.stderr)
    print(file=sys.stderr)
    _print_on_demand_consolidation_command(file=sys.stderr)
    print(file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Selectively bump and publish changed packages to PyPI.")
    parser.add_argument(
        "bump_kind",
        nargs="?",
        choices=BUMP_KINDS,
        help="Base bump kind: major, minor, or patch",
    )
    parser.add_argument(
        "--minor",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="Override a package to minor bump (repeatable)",
    )
    parser.add_argument(
        "--major",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="Override a package to major bump (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch the publish workflow for the current version (no version bump)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Rerun failed publish jobs for the current version, then watch",
    )
    args = parser.parse_args()

    # --watch / --retry mode
    if args.watch or args.retry:
        mngr_version = get_mngr_version()
        tag = f"v{mngr_version}"
        run_id = find_publish_run_id(tag)

        after_attempt = 0
        if args.retry:
            after_attempt = _get_workflow_attempt_number(run_id)
            print(f"Rerunning failed jobs for {tag}...")
            run("gh", "run", "rerun", run_id, "--failed")

        print(f"Watching publish workflow for {tag}...")
        watch_publish_workflow(run_id, after_workflow_attempt=after_attempt)
        return

    if args.bump_kind is None:
        parser.error("bump_kind is required: patch, minor, or major")

    # On a real run, enforce branch == main before doing anything else.
    # In --dry-run, surface a WARNING instead so the user sees they would
    # be blocked on a real run, but the preview still proceeds (matching
    # the dry-run-friendly behavior of the changelog gate below).
    branch = run("git", "branch", "--show-current")
    if branch != "main":
        if args.dry_run:
            print(f"WARNING: not on main branch (currently on {branch}); a real release would fail this check.")
        else:
            print(f"ERROR: Must be on main branch (currently on {branch})", file=sys.stderr)
            sys.exit(1)

    # Refuse to release while any project has unconsolidated entries in
    # its <project_dir>/changelog/ directory. Otherwise the per-package
    # [Unreleased] sections we're about to finalize would be missing
    # those entries' bullets. In --dry-run we warn rather than block so
    # the user can still preview what would be released.
    if not _gate_release_on_pending_changelog_entries(REPO_ROOT, dry_run=args.dry_run):
        sys.exit(1)

    base_kind: str = args.bump_kind

    # Build overrides from --minor and --major flags
    overrides: dict[str, str] = {}
    for pkg_name in args.minor:
        if pkg_name not in PACKAGE_BY_PYPI_NAME:
            parser.error(f"Unknown package: {pkg_name}")
        overrides[pkg_name] = "minor"
    for pkg_name in args.major:
        if pkg_name not in PACKAGE_BY_PYPI_NAME:
            parser.error(f"Unknown package: {pkg_name}")
        overrides[pkg_name] = "major"

    # Validate overrides are >= base level
    for pkg_name, override_kind in overrides.items():
        if BUMP_LEVEL_ORDER[override_kind] < BUMP_LEVEL_ORDER[base_kind]:
            parser.error(
                f"Override for {pkg_name} ({override_kind}) is lower than base level ({base_kind}). "
                f"Use a lower base level instead."
            )

    # Check release state: last git tag and latest version on PyPI
    last_tag = _find_last_release_tag()
    tag_version = last_tag.lstrip("v")
    pypi_version = _get_pypi_version()

    print(f"Last release tag: {last_tag}")
    if pypi_version is not None:
        print(f"Latest on PyPI:   v{pypi_version}")
    else:
        print("Latest on PyPI:   (could not check)")

    is_unpublished = (
        pypi_version is not None
        and tag_version != pypi_version
        and semver.Version.parse(tag_version) > semver.Version.parse(pypi_version)
    )
    if is_unpublished:
        print(f"\nWARNING: {last_tag} appears unpublished (PyPI is at v{pypi_version}).")
        print("To publish the existing release:")
        print("  uv run scripts/release.py --retry")

    # Detect what changed since the last release
    directly_changed = _detect_changed_packages(last_tag)

    if not directly_changed:
        if is_unpublished:
            print("\nNo packages changed since the last release, and it was not published.")
            print("Use --retry to publish it, or fix the issue and try again.")
        else:
            print("\nNo packages changed since the last release. Nothing to do.")
        return

    # Detect new packages (not present at last tag) and confirm with user
    new_packages = _detect_new_packages(last_tag) & directly_changed
    current_versions = get_package_versions()
    if new_packages and not args.dry_run:
        confirmed_new = _confirm_new_packages(new_packages, current_versions)
    elif new_packages:
        # In dry-run mode, assume all new packages are confirmed for the preview
        confirmed_new = new_packages
    else:
        confirmed_new = set()
    _print_trusted_publisher_warning(confirmed_new)

    # Remove new packages (confirmed or not) from the changed set before computing bumps.
    # Confirmed new packages are published at their current version, not bumped.
    # Declined new packages are excluded entirely.
    directly_changed_for_bump = directly_changed - new_packages

    if not directly_changed_for_bump and not confirmed_new:
        print("\nNo packages to release (new packages were declined). Nothing to do.")
        return

    # Compute the full bump set (includes cascades and mngr-always rule)
    to_bump = _compute_bump_set(directly_changed_for_bump)

    # Remove confirmed new packages from bump set -- they publish at current version,
    # not a bumped version. They may have entered to_bump via cascade (e.g. mngr is
    # always bumped, and most packages depend on mngr).
    for name in confirmed_new:
        to_bump.pop(name, None)

    # Warn if any overrides target packages not in the bump set
    for pkg_name in overrides:
        if pkg_name not in to_bump:
            print(f"WARNING: --{overrides[pkg_name]} {pkg_name} ignored (package is not being bumped)")
    overrides = {k: v for k, v in overrides.items() if k in to_bump}

    # Compute per-package bump levels with DAG cascade
    bump_levels = _compute_bump_levels(to_bump, base_kind, overrides)
    new_versions = bump_package_versions(bump_levels, current_versions)

    # Compute what the full version map will look like after bumping
    all_versions_after = dict(current_versions)
    all_versions_after.update(new_versions)

    new_mngr_version = all_versions_after["imbue-mngr"]
    tag = f"v{new_mngr_version}"

    # Show summary
    _print_bump_summary(directly_changed, to_bump, bump_levels, current_versions, new_versions, confirmed_new)
    print()
    print(f"Tag: {tag}")

    if args.dry_run:
        print("\n(dry run -- no changes made)")
        return

    # Ensure the working tree is clean and up to date before prompting
    # for confirmation. (Branch == main is enforced above for non-dry-run
    # invocations, before the pending-changelog-entry gate.)
    if run("git", "status", "--porcelain"):
        print("ERROR: Working tree is not clean. Commit or stash changes first.", file=sys.stderr)
        sys.exit(1)

    run("git", "fetch", "origin", "main")
    local_sha = run("git", "rev-parse", "HEAD")
    remote_sha = run("git", "rev-parse", "origin/main")
    if local_sha != remote_sha:
        print(
            f"ERROR: Local main ({local_sha[:8]}) is not up to date with origin ({remote_sha[:8]}).", file=sys.stderr
        )
        print("Run 'git pull' first.", file=sys.stderr)
        sys.exit(1)

    # Advisory: surface whether the Release Tests workflow has passed on this
    # exact commit. Release tests are not a hard publish gate, so this only
    # warns -- the user decides at the confirmation prompt below.
    if gh_is_available():
        runs = json.loads(
            run(
                "gh",
                "run",
                "list",
                "-w",
                RELEASE_TESTS_WORKFLOW,
                "-b",
                "main",
                "-L",
                "20",
                "--json",
                "headSha,conclusion",
            )
        )
        match = next((r for r in runs if r["headSha"] == local_sha), None)
        if match is None:
            print(f"\nWARNING: no Release Tests run found for this commit ({local_sha[:8]}).")
            print(f"  Run them first: gh workflow run {RELEASE_TESTS_WORKFLOW} --ref main")
        elif match["conclusion"] != "success":
            print(f"\nWARNING: Release Tests for this commit concluded '{match['conclusion']}', not success.")

    confirm = input(f"\nProceed with release {tag}? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    # Bump versions for bumped packages (new packages keep their current version)
    for name, new_version in new_versions.items():
        _write_version(name, new_version)
    if new_versions:
        print(f"\nBumped versions for {len(new_versions)} package(s).")
    if confirmed_new:
        print(f"Publishing {len(confirmed_new)} new package(s) at current version.")

    # Update internal dependency pins to match new versions
    pin_modified = update_internal_dep_pins(all_versions_after)
    if pin_modified:
        print(f"Updated dependency pins in: {', '.join(pin_modified)}")

    # Advance the supply-chain cooldown cutoff before re-locking so the
    # regenerated uv.lock records the new `[options] exclude-newer`. Forward-only:
    # a release run while the cutoff is still younger than the window leaves it
    # untouched (see update_exclude_newer). Anchored to UTC (today's date) to match
    # the UTC upload-times uv compares it against -- deliberately independent of the
    # Pacific changelog date used below.
    new_cutoff = update_exclude_newer(REPO_ROOT / "pyproject.toml", datetime.now(timezone.utc).date())
    if new_cutoff is not None:
        print(f"Advanced exclude-newer cooldown cutoff to {new_cutoff}")

    print("Regenerating uv.lock...")
    run("uv", "lock")

    # Finalize each released package's per-project CHANGELOG.md: rename its
    # [Unreleased] section to [v<package-version>] - <date> and insert a
    # fresh empty [Unreleased] above it. Covers both bumped packages (use
    # the new version) and confirmed first-time publications (use the
    # current version, since these publish without a bump). apps/<name>/
    # and dev/ changelogs are not versioned and stay untouched -- their
    # entries accumulate in [Unreleased] indefinitely (the consolidator
    # keeps appending there).
    release_date = today_pacific()
    finalized_paths: list[Path] = []
    versions_to_finalize: dict[str, str] = {
        **{name: current_versions[name] for name in confirmed_new},
        **new_versions,
    }
    for pypi_name, version in versions_to_finalize.items():
        pkg = PACKAGE_BY_PYPI_NAME[pypi_name]
        pkg_changelog = REPO_ROOT / "libs" / pkg.dir_name / "CHANGELOG.md"
        if not pkg_changelog.exists():
            print(f"WARNING: {pkg.dir_name} has no CHANGELOG.md; skipping finalize.")
            continue
        had_content = finalize_changelog_unreleased(pkg_changelog, version, release_date)
        rel = pkg_changelog.relative_to(REPO_ROOT)
        if had_content:
            print(f"Finalized {rel}: [Unreleased] -> [v{version}] - {release_date}")
        else:
            print(f"WARNING: [Unreleased] empty in {rel}; emitted empty [v{version}] section.")
        finalized_paths.append(pkg_changelog)

    # Commit, tag, push
    all_released_names = sorted(set(new_versions.keys()) | confirmed_new)
    commit_msg = f"Release {tag} ({', '.join(all_released_names)})"

    files_to_add = [
        # Root pyproject.toml carries the `[tool.uv] exclude-newer` cutoff that
        # update_exclude_newer may have advanced above.
        "pyproject.toml",
        *[str(pkg.pyproject_path.relative_to(REPO_ROOT)) for pkg in PACKAGES],
        "uv.lock",
        *[str(p.relative_to(REPO_ROOT)) for p in finalized_paths],
    ]
    run("git", "add", *files_to_add)
    run("git", "commit", "-m", commit_msg)
    run("git", "tag", tag)
    run("git", "push", "origin", "main", tag)

    print(f"\nRelease {tag} pushed. Publish workflow: {ACTIONS_URL}")

    # Watch the publish workflow if gh is available
    if gh_is_available():
        run_id = find_publish_run_id(tag)
        watch_publish_workflow(run_id)
    else:
        print()
        print("To watch the publish (requires gh CLI):")
        print("  uv run scripts/release.py --watch")


if __name__ == "__main__":
    main()
