"""The smoke run report, written for real by a pytest session over a small suite shaped like the smoke suite's.

The suite lives in a git repository of its own and has one scenario of each
kind a report shows: one that passes, one that fails in its second step, one
that skips, one with a failed subtest, one whose fixture errors, one that
launches an "app" with a Sentry install id, and one the session's ``-k``
deselects. Its conftest hands the finished session to the same reader the smoke
suite's conftest uses, and keeps the report's data beside the page for the
assertions below.
"""

import os
import subprocess
import sys
from pathlib import Path

from imbue.minds.smoke_tests.data_types import SmokeArtifactKind
from imbue.minds.smoke_tests.data_types import SmokeRunReport
from imbue.minds.smoke_tests.data_types import SmokeScenarioOutcome
from imbue.minds.smoke_tests.data_types import SmokeScenarioResult
from imbue.minds.smoke_tests.data_types import SmokeStepOutcome
from imbue.minds.smoke_tests.data_types import SmokeTestPhase

_SESSION_TIMEOUT_SECONDS = 120.0
_APP_INSTALL_ID = "5f1e2d3c4b5a69788796a5b4c3d2e1f0"

_PYTEST_INI = """[pytest]
addopts = -p no:cacheprovider
"""

_CONFTEST = """import os
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests.backend import resolve_smoke_backend
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.data_types import SmokeRunVerdict
from imbue.minds.smoke_tests.report import write_smoke_run_report
from imbue.minds.smoke_tests.testing import SMOKE_APP_STATE_DIR_KEY
from imbue.minds.smoke_tests.testing import SMOKE_BACKEND_KEY
from imbue.minds.smoke_tests.testing import smoke_run_report_of_session

OUTPUTS_DIR = Path(__file__).parent / ".test_output" / "smoke"


@pytest.fixture
def smoke_output_dir(request: pytest.FixtureRequest) -> Path:
    output_dir = OUTPUTS_DIR / request.node.name
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


@pytest.fixture
def broken_account() -> None:
    raise RuntimeError("the persistent account could not sign in 40402")


@pytest.fixture
def launched_app(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    request.config.stash[SMOKE_BACKEND_KEY] = resolve_smoke_backend(
        DevEnvName("staging"), resolve_smoke_client_config_path(DevEnvName("staging"))
    )
    state_dir = tmp_path / "studio-data-home" / "staging" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "anonymous_user_id").write_text("{install_id}\\n")
    request.node.stash[SMOKE_APP_STATE_DIR_KEY] = state_dir


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    report = smoke_run_report_of_session(
        session,
        terminal_reporter=terminal_reporter,
        exit_status=int(exitstatus),
        outputs_dir=OUTPUTS_DIR,
        verdict=SmokeRunVerdict(dwt_failure_count=NonNegativeInt(1), other_failure_count=NonNegativeInt(2)),
        environ=os.environ,
        finished_at=datetime.now(timezone.utc),
    )
    write_smoke_run_report(OUTPUTS_DIR / "report.html", report)
    (OUTPUTS_DIR / "report-data.json").write_text(report.model_dump_json())
""".replace("{install_id}", _APP_INSTALL_ID)

_SUITE = '''import pytest

from imbue.minds.smoke_tests.steps import recorded_step


def test_passes() -> None:
    """The connector answers its liveness probe.

    A second paragraph the report leaves out.
    """


def test_fails_in_its_second_step(smoke_output_dir) -> None:
    """The journey signs in."""
    with recorded_step(smoke_output_dir / "steps.jsonl", "Step 1: launch the app"):
        (smoke_output_dir / "01-landing-0.png").write_bytes(b"png")
    with recorded_step(smoke_output_dir / "steps.jsonl", "Step 2: sign in"):
        assert False, "the hosted login never answered 31337"


def test_skips() -> None:
    pytest.skip("staging publishes no release feed 16180")


def test_reports_a_failed_subtest(subtests) -> None:
    with subtests.test(msg="dwt:root::test_a.py::test_ok"):
        pass
    with subtests.test(msg="dwt:root::test_b.py::test_broken"):
        pytest.fail("Failed in the workspace on its only attempt:\\nboom 27182", pytrace=False)


def test_errors_in_setup(broken_account) -> None:
    pass


def test_launches_an_app(launched_app) -> None:
    pass


def test_never_selected() -> None:
    pass
'''


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=Smoke Report Test", "-c", "user.email=smoke-report@example.test", *arguments),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _run_session_in_a_committed_suite(tmp_path: Path) -> tuple[Path, str, subprocess.CompletedProcess[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pytest.ini").write_text(_PYTEST_INI)
    (repo / "conftest.py").write_text(_CONFTEST)
    (repo / "test_fake_smoke.py").write_text(_SUITE)
    _git(repo, "init", "--quiet", "--initial-branch=smoke-report-branch")
    _git(repo, "add", "pytest.ini", "conftest.py", "test_fake_smoke.py")
    _git(repo, "commit", "--quiet", "--message", "the suite")
    home = tmp_path / "home"
    home.mkdir()
    session = subprocess.run(
        (sys.executable, "-m", "pytest", "-k", "not never_selected", "test_fake_smoke.py"),
        cwd=repo,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "MINDS_SMOKE_TIER": "staging",
            "SENTRY_ORG": "example-org",
        },
        capture_output=True,
        text=True,
        timeout=_SESSION_TIMEOUT_SECONDS,
    )
    return repo, _git(repo, "rev-parse", "HEAD"), session


def _written_report(tmp_path: Path) -> tuple[Path, str, SmokeRunReport]:
    repo, git_sha, session = _run_session_in_a_committed_suite(tmp_path)
    report_data = repo / ".test_output" / "smoke" / "report-data.json"
    assert report_data.is_file(), session.stdout + session.stderr
    return repo, git_sha, SmokeRunReport.model_validate_json(report_data.read_text())


def _scenario_named(report: SmokeRunReport, name: str) -> SmokeScenarioResult:
    (scenario,) = (scenario for scenario in report.scenarios if scenario.name == name)
    return scenario


def test_the_report_has_every_selected_scenario_in_order_with_its_outcome(tmp_path: Path) -> None:
    _, _, report = _written_report(tmp_path)

    assert [(str(scenario.name), scenario.outcome) for scenario in report.scenarios] == [
        ("test_passes", SmokeScenarioOutcome.PASSED),
        ("test_fails_in_its_second_step", SmokeScenarioOutcome.FAILED),
        ("test_skips", SmokeScenarioOutcome.SKIPPED),
        ("test_reports_a_failed_subtest", SmokeScenarioOutcome.FAILED),
        ("test_errors_in_setup", SmokeScenarioOutcome.ERROR),
        ("test_launches_an_app", SmokeScenarioOutcome.PASSED),
    ]
    assert report.deselected_count == 1
    assert report.exit_status == 1
    assert _scenario_named(report, "test_passes").description == "The connector answers its liveness probe."


def test_a_failed_scenario_says_why_how_far_it_got_and_what_it_left(tmp_path: Path) -> None:
    repo, git_sha, report = _written_report(tmp_path)

    failed = _scenario_named(report, "test_fails_in_its_second_step")

    assert failed.failed_phase == SmokeTestPhase.CALL
    assert "the hosted login never answered 31337" in failed.summary
    assert [(str(step.label), step.outcome) for step in failed.steps] == [
        ("Step 1: launch the app", SmokeStepOutcome.COMPLETED),
        ("Step 2: sign in", SmokeStepOutcome.UNFINISHED),
    ]
    assert (
        "test_fails_in_its_second_step/01-landing-0.png",
        SmokeArtifactKind.IMAGE,
    ) in [(str(artifact.relative_path), artifact.kind) for artifact in failed.artifacts]
    assert str(failed.source_url) == (
        f"https://github.com/imbue-ai/mngr-internal/blob/{git_sha}/test_fake_smoke.py#L{failed.source_line}"
    )
    source_lines = (repo / "test_fake_smoke.py").read_text().splitlines()
    assert source_lines[failed.source_line - 1].startswith("def test_fails_in_its_second_step")


def test_skips_subtests_and_fixture_errors_carry_their_own_reasons(tmp_path: Path) -> None:
    _, _, report = _written_report(tmp_path)

    assert _scenario_named(report, "test_skips").summary == "staging publishes no release feed 16180"
    with_subtests = _scenario_named(report, "test_reports_a_failed_subtest")
    assert [(str(subtest.label), subtest.outcome) for subtest in with_subtests.subtests] == [
        ("dwt:root::test_a.py::test_ok", SmokeScenarioOutcome.PASSED),
        ("dwt:root::test_b.py::test_broken", SmokeScenarioOutcome.FAILED),
    ]
    assert "boom 27182" in with_subtests.subtests[1].summary
    errored = _scenario_named(report, "test_errors_in_setup")
    assert errored.failed_phase == SmokeTestPhase.SETUP
    assert "RuntimeError: the persistent account could not sign in 40402" in errored.summary


def test_a_scenario_that_launched_the_app_links_its_install_into_sentry(tmp_path: Path) -> None:
    _, _, report = _written_report(tmp_path)

    launched = _scenario_named(report, "test_launches_an_app")

    assert launched.app_install_id == _APP_INSTALL_ID
    assert launched.sentry_url is not None
    assert f"user.id%3A{_APP_INSTALL_ID}" in str(launched.sentry_url)
    assert _scenario_named(report, "test_passes").app_install_id is None


def test_the_run_records_its_commit_its_target_and_its_inputs(tmp_path: Path) -> None:
    repo, git_sha, report = _written_report(tmp_path)

    checkout = report.environment.checkout
    assert checkout is not None
    assert (str(checkout.git_sha), checkout.branch_name, checkout.is_dirty, checkout.is_pushed) == (
        git_sha,
        "smoke-report-branch",
        False,
        False,
    )
    assert report.backend is not None and str(report.backend.env_name) == "staging"
    assert report.environment.pytest_arguments == ("-k", "not never_selected", "test_fake_smoke.py")
    set_parameters = {
        str(parameter.spec.env_var): parameter.shown_value for parameter in report.parameters if parameter.is_set
    }
    assert set_parameters == {"MINDS_SMOKE_TIER": "staging", "SENTRY_ORG": "example-org"}
    page = (repo / ".test_output" / "smoke" / "report.html").read_text()
    assert "1 of 6 scenarios passed" not in page
    assert "3 of 6 scenarios failed, 1 skipped" in page
