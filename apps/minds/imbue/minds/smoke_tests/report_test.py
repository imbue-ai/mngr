import os
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from pydantic import AnyUrl

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests import primitives
from imbue.minds.smoke_tests.backend import resolve_smoke_backend
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.data_types import SmokeArtifact
from imbue.minds.smoke_tests.data_types import SmokeArtifactKind
from imbue.minds.smoke_tests.data_types import SmokeCollectionError
from imbue.minds.smoke_tests.data_types import SmokeResourceKind
from imbue.minds.smoke_tests.data_types import SmokeRunCheckout
from imbue.minds.smoke_tests.data_types import SmokeRunEnvironment
from imbue.minds.smoke_tests.data_types import SmokeRunManifestEntry
from imbue.minds.smoke_tests.data_types import SmokeRunReport
from imbue.minds.smoke_tests.data_types import SmokeRunVerdict
from imbue.minds.smoke_tests.data_types import SmokeScenarioOutcome
from imbue.minds.smoke_tests.data_types import SmokeScenarioResult
from imbue.minds.smoke_tests.data_types import SmokeStepOutcome
from imbue.minds.smoke_tests.data_types import SmokeStepRecord
from imbue.minds.smoke_tests.data_types import SmokeSubtestResult
from imbue.minds.smoke_tests.data_types import SmokeTestPhase
from imbue.minds.smoke_tests.data_types import StudioBinary
from imbue.minds.smoke_tests.data_types import StudioBinaryKind
from imbue.minds.smoke_tests.primitives import ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import MAILTM_ACCOUNT_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_USER_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.report import SMOKE_RUN_PARAMETER_SPECS
from imbue.minds.smoke_tests.report import ci_run_description_from_environ
from imbue.minds.smoke_tests.report import ci_run_url_from_environ
from imbue.minds.smoke_tests.report import failure_summary_of
from imbue.minds.smoke_tests.report import github_source_url
from imbue.minds.smoke_tests.report import list_scenario_artifacts
from imbue.minds.smoke_tests.report import render_smoke_run_report_html
from imbue.minds.smoke_tests.report import repository_url_from_environ
from imbue.minds.smoke_tests.report import sentry_issues_url_or_none
from imbue.minds.smoke_tests.report import smoke_run_parameters_from_environ
from imbue.minds.utils.sentry.core import SentryDeployEnvironment


def test_a_parameter_the_environment_sets_shows_its_value_and_an_unset_one_shows_none() -> None:
    parameters = smoke_run_parameters_from_environ({"MINDS_SMOKE_TIER": "staging", "UNRELATED": "x"})

    by_name = {str(parameter.spec.env_var): parameter for parameter in parameters}
    assert (by_name["MINDS_SMOKE_TIER"].is_set, by_name["MINDS_SMOKE_TIER"].shown_value) == (True, "staging")
    assert (by_name["MINDS_SMOKE_CHANNEL"].is_set, by_name["MINDS_SMOKE_CHANNEL"].shown_value) == (False, None)
    assert "UNRELATED" not in by_name


def test_a_hidden_parameter_is_reported_as_set_without_its_value() -> None:
    parameters = smoke_run_parameters_from_environ({"MINDS_SMOKE_ACCOUNT_PASSWORD": "hunter2-58301"})

    by_name = {str(parameter.spec.env_var): parameter for parameter in parameters}
    assert (by_name["MINDS_SMOKE_ACCOUNT_PASSWORD"].is_set, by_name["MINDS_SMOKE_ACCOUNT_PASSWORD"].shown_value) == (
        True,
        None,
    )
    assert "hunter2-58301" not in repr(parameters)


_LONG_STYLE_FAILURE = """    def test_share() -> None:
        response = visit()
>       assert response.status == 200, "The share link https://hello.example/ did not answer 200"
E       AssertionError: The share link https://hello.example/ did not answer 200
E       assert 503 == 200
E        +  where 503 = <Response 503>.status

apps/minds/smoke_tests/test_smoke_journey.py:200: AssertionError"""

_CHAINED_FAILURE = """    def wait() -> None:
>       raise TimeoutError("first 10331")
E       TimeoutError: first 10331

During handling of the above exception, another exception occurred:

    def test_wait() -> None:
>       raise SmokeAppError("the landing page never loaded 10332")
E       imbue.minds.smoke_tests.primitives.SmokeAppError: the landing page never loaded 10332

apps/minds/smoke_tests/test_smoke_journey.py:120: SmokeAppError"""


def test_the_summary_of_a_failure_is_the_last_exception_pytest_marked() -> None:
    assert failure_summary_of(_LONG_STYLE_FAILURE) == (
        "AssertionError: The share link https://hello.example/ did not answer 200\n"
        "assert 503 == 200\n"
        "+  where 503 = <Response 503>.status"
    )
    assert failure_summary_of(_CHAINED_FAILURE) == (
        "imbue.minds.smoke_tests.primitives.SmokeAppError: the landing page never loaded 10332"
    )


def test_the_summary_of_a_failure_without_a_traceback_is_its_text() -> None:
    assert failure_summary_of("contains 2 failed subtests\n") == "contains 2 failed subtests"
    assert failure_summary_of("Traceback (most recent call last):\n  File 'x.py'\nValueError: boom 7781\n") == (
        "ValueError: boom 7781"
    )


_GITHUB_ACTIONS_ENVIRON = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "imbue-ai/mngr-internal",
    "GITHUB_RUN_ID": "19283746",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_WORKFLOW": "minds smoke",
    "GITHUB_JOB": "test",
    "RUNNER_NAME": "GitHub Actions 41",
}


def test_source_url_points_at_the_test_s_line_at_the_run_s_commit() -> None:
    url = github_source_url(
        AnyUrl("https://github.com/imbue-ai/mngr-internal"),
        "c993c71e0f",
        "apps/minds/smoke_tests/test_smoke_journey.py",
        254,
    )

    assert str(url) == (
        "https://github.com/imbue-ai/mngr-internal/blob/c993c71e0f/apps/minds/smoke_tests/test_smoke_journey.py#L254"
    )


def test_the_repository_is_the_ci_run_s_in_ci_and_the_canonical_one_elsewhere() -> None:
    assert str(repository_url_from_environ({**_GITHUB_ACTIONS_ENVIRON, "GITHUB_REPOSITORY": "imbue-ai/fork"})) == (
        "https://github.com/imbue-ai/fork"
    )
    assert str(repository_url_from_environ({})) == "https://github.com/imbue-ai/mngr-internal"


def test_a_ci_run_is_linked_at_its_attempt_and_a_local_run_has_no_link() -> None:
    ci_run_url = ci_run_url_from_environ(_GITHUB_ACTIONS_ENVIRON)

    assert ci_run_url is not None
    assert str(ci_run_url) == "https://github.com/imbue-ai/mngr-internal/actions/runs/19283746/attempts/2"
    assert ci_run_description_from_environ(_GITHUB_ACTIONS_ENVIRON) == (
        "minds smoke run 19283746, attempt 2, job test, on runner GitHub Actions 41"
    )
    assert ci_run_url_from_environ({}) is None
    assert ci_run_description_from_environ({}) == ""


def test_the_sentry_link_searches_the_app_s_install_in_both_of_the_tier_s_projects() -> None:
    url = sentry_issues_url_or_none(
        sentry_org="example-org",
        sentry_environment=SentryDeployEnvironment.STAGING,
        app_install_id="0123456789abcdef0123456789abcdef",
        started_at=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc),
        finished_at=datetime(2026, 11, 16, 9, 45, tzinfo=timezone.utc),
    )

    assert url is not None
    assert str(url) == (
        "https://sentry.io/organizations/example-org/issues/"
        "?project=4511609241862145&project=4511620045144064"
        "&query=user.id%3A0123456789abcdef0123456789abcdef"
        "&start=2026-11-16T09%3A25%3A00&end=2026-11-16T10%3A45%3A00&utc=true"
    )


def test_there_is_no_sentry_link_without_an_org() -> None:
    assert (
        sentry_issues_url_or_none(
            sentry_org="",
            sentry_environment=SentryDeployEnvironment.PRODUCTION,
            app_install_id="0123456789abcdef0123456789abcdef",
            started_at=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc),
            finished_at=datetime(2026, 11, 16, 9, 45, tzinfo=timezone.utc),
        )
        is None
    )


def test_a_scenario_s_artifacts_are_the_files_it_wrote_during_the_run_two_levels_deep(tmp_path: Path) -> None:
    report_dir = tmp_path / "smoke"
    output_dir = report_dir / "test_the_build_signs_in"
    (output_dir / "app-logs").mkdir(parents=True)
    (output_dir / "dwt" / "root" / "all").mkdir(parents=True)
    (output_dir / "01-landing-0.png").write_bytes(b"png")
    (output_dir / "app-logs" / "minds.log").write_text("log")
    (output_dir / "dwt" / "root" / "all" / "attempt-1.xml").write_text("<testsuite/>")
    stale = output_dir / "07-unshared-0.png"
    stale.write_bytes(b"png from yesterday's run")
    run_started_at = datetime.now(timezone.utc)
    yesterday = (run_started_at - timedelta(days=1)).timestamp()
    os.utime(stale, (yesterday, yesterday))

    artifacts = list_scenario_artifacts(output_dir, report_dir=report_dir, written_since=run_started_at)

    assert [(str(artifact.relative_path), artifact.kind) for artifact in artifacts] == [
        ("test_the_build_signs_in/01-landing-0.png", SmokeArtifactKind.IMAGE),
        ("test_the_build_signs_in/app-logs/minds.log", SmokeArtifactKind.FILE),
    ]


def test_a_scenario_that_wrote_no_output_directory_has_no_artifacts(tmp_path: Path) -> None:
    assert (
        list_scenario_artifacts(tmp_path / "absent", report_dir=tmp_path, written_since=datetime.now(timezone.utc))
        == ()
    )


_SCENARIO_STARTED_AT = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
_REPOSITORY_URL = "https://github.com/imbue-ai/mngr-internal"


def _scenario(
    name: str,
    outcome: SmokeScenarioOutcome,
    summary: str,
    steps: tuple[SmokeStepRecord, ...],
    app_install_id: str | None,
    sentry_url: str | None,
) -> SmokeScenarioResult:
    return SmokeScenarioResult(
        node_id=NonEmptyStr(f"apps/minds/smoke_tests/test_smoke_probes.py::{name}"),
        name=NonEmptyStr(name),
        description=f"What {name} checks.",
        source_path=NonEmptyStr("apps/minds/smoke_tests/test_smoke_probes.py"),
        source_line=PositiveInt(61),
        source_url=AnyUrl(f"{_REPOSITORY_URL}/blob/c993c71e0f/apps/minds/smoke_tests/test_smoke_probes.py#L61"),
        outcome=outcome,
        failed_phase=SmokeTestPhase.CALL if outcome == SmokeScenarioOutcome.FAILED else None,
        started_at=_SCENARIO_STARTED_AT,
        setup_seconds=NonNegativeFloat(1.25),
        call_seconds=NonNegativeFloat(312.0),
        teardown_seconds=NonNegativeFloat(0.5),
        summary=summary,
        failure_text=summary,
        captured_output="",
        subtests=(),
        steps=steps,
        output_dir_relative_path=None,
        artifacts=(),
        app_install_id=app_install_id,
        sentry_url=AnyUrl(sentry_url) if sentry_url is not None else None,
    )


def _step(label: str, outcome: SmokeStepOutcome, offset_seconds: int, duration_seconds: int | None) -> SmokeStepRecord:
    started_at = _SCENARIO_STARTED_AT + timedelta(seconds=offset_seconds)
    return SmokeStepRecord(
        label=NonEmptyStr(label),
        outcome=outcome,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=duration_seconds) if duration_seconds is not None else None,
        detail="",
    )


def _report(scenarios: tuple[SmokeScenarioResult, ...], environ: dict[str, str]) -> SmokeRunReport:
    return SmokeRunReport(
        run_id=SmokeRunId("20261116t093000z"),
        backend=resolve_smoke_backend(DevEnvName("staging"), resolve_smoke_client_config_path(DevEnvName("staging"))),
        studio_binary=None,
        started_at=_SCENARIO_STARTED_AT,
        finished_at=_SCENARIO_STARTED_AT + timedelta(minutes=9),
        exit_status=1,
        environment=SmokeRunEnvironment(
            host_name=NonEmptyStr("smoke-host"),
            platform_description=NonEmptyStr("Linux-6.8-x86_64"),
            python_version=NonEmptyStr("3.12.9"),
            pytest_version=NonEmptyStr("9.1.1"),
            pytest_arguments=("-m", "smoke", "apps/minds/smoke_tests/"),
            ci_run_url=None,
            ci_run_description="",
            repository_url=AnyUrl(_REPOSITORY_URL),
            checkout=None,
        ),
        parameters=smoke_run_parameters_from_environ(environ),
        scenarios=scenarios,
        collection_errors=(),
        deselected_count=NonNegativeInt(0),
        verdict=SmokeRunVerdict(dwt_failure_count=NonNegativeInt(0), other_failure_count=NonNegativeInt(1)),
        created_resources=(),
    )


def test_the_report_lists_failed_scenarios_first_then_passed_then_skipped() -> None:
    report = _report(
        (
            _scenario("test_passes_37214", SmokeScenarioOutcome.PASSED, "", (), None, None),
            _scenario("test_skips_37215", SmokeScenarioOutcome.SKIPPED, "staging publishes no feed", (), None, None),
            _scenario("test_fails_37216", SmokeScenarioOutcome.FAILED, "AssertionError: 503", (), None, None),
        ),
        {},
    )

    html = render_smoke_run_report_html(report)

    assert html.index("test_fails_37216") < html.index("test_passes_37214") < html.index("test_skips_37215")
    assert "staging publishes no feed" in html
    assert f'href="{_REPOSITORY_URL}/blob/c993c71e0f/apps/minds/smoke_tests/test_smoke_probes.py#L61"' in html


def test_failure_text_is_shown_as_text_and_never_as_markup() -> None:
    summary = "AssertionError: body excerpt '<script>alert(4402)</script>'"
    report = _report((_scenario("test_fails", SmokeScenarioOutcome.FAILED, summary, (), None, None),), {})

    html = render_smoke_run_report_html(report)

    assert "<script>alert(4402)" not in html
    assert "&lt;script&gt;alert(4402)&lt;/script&gt;" in html


def test_a_failed_scenario_shows_the_steps_it_completed_and_the_one_it_did_not_finish() -> None:
    steps = (
        _step("Step 1: launch the app 51120", SmokeStepOutcome.COMPLETED, 0, 40),
        _step("Step 2: sign in 51121", SmokeStepOutcome.UNFINISHED, 40, None),
    )
    report = _report((_scenario("test_journey", SmokeScenarioOutcome.FAILED, "boom", steps, None, None),), {})

    html = render_smoke_run_report_html(report)

    step_rows = re.findall(r'<tr class="step (\w+)">.*?<td>(Step [^<]*)</td>', html, flags=re.DOTALL)
    assert step_rows == [("COMPLETED", "Step 1: launch the app 51120"), ("UNFINISHED", "Step 2: sign in 51121")]


def test_a_scenario_s_app_is_linked_into_sentry_and_without_an_org_its_install_id_is_named() -> None:
    linked = _scenario(
        "test_linked",
        SmokeScenarioOutcome.FAILED,
        "boom",
        (),
        "0123456789abcdef0123456789abcdef",
        "https://sentry.io/organizations/example-org/issues/?query=user.id%3A0123456789abcdef0123456789abcdef",
    )
    unlinked = _scenario(
        "test_unlinked", SmokeScenarioOutcome.PASSED, "", (), "fedcba9876543210fedcba9876543210", None
    )

    html = render_smoke_run_report_html(_report((linked, unlinked), {}))

    assert 'href="https://sentry.io/organizations/example-org/issues/?query=user.id%3A0123456789abcdef' in html
    assert "user.id:fedcba9876543210fedcba9876543210" in html
    assert "minds-staging" in html


def test_the_parameter_table_marks_what_the_environment_set_and_hides_a_password() -> None:
    report = _report((), {"MINDS_SMOKE_TIER": "staging", "MINDS_SMOKE_ACCOUNT_PASSWORD": "hunter2-90211"})

    html = render_smoke_run_report_html(report)

    set_rows = re.findall(r'<tr class="parameter is-set">\s*<td><code>(\w+)</code></td>', html)
    assert set_rows == ["MINDS_SMOKE_TIER", "MINDS_SMOKE_ACCOUNT_PASSWORD"]
    assert "hunter2-90211" not in html


def test_a_run_whose_every_scenario_skipped_is_not_called_passed() -> None:
    failed_run = _report(
        (
            _scenario("test_probe_a", SmokeScenarioOutcome.SKIPPED, "MINDS_SMOKE_TIER is not set", (), None, None),
            _scenario("test_probe_b", SmokeScenarioOutcome.SKIPPED, "MINDS_SMOKE_TIER is not set", (), None, None),
        ),
        {},
    )
    report = failed_run.model_copy_update(to_update(failed_run.field_ref().exit_status, 0))

    html = render_smoke_run_report_html(report)

    headline = html[html.index("<h1>") : html.index("</h1>")]
    assert headline == '<h1><span class="badge SKIPPED">SKIPPED</span> All 2 scenarios skipped'


def test_a_docstring_s_double_backticked_names_render_as_code_and_stay_escaped() -> None:
    undescribed = _scenario("test_feed", SmokeScenarioOutcome.FAILED, "boom", (), None, None)
    scenario = undescribed.model_copy_update(
        to_update(
            undescribed.field_ref().description,
            "Every entry of ``release-channels.toml`` is served, ``<b>`` included.",
        )
    )

    html = render_smoke_run_report_html(_report((scenario,), {}))

    assert "Every entry of <code>release-channels.toml</code> is served, <code>&lt;b&gt;</code> included." in html


def test_every_section_of_a_full_ci_run_renders() -> None:
    failed = _scenario("test_journey", SmokeScenarioOutcome.FAILED, "AssertionError: share 64401", (), None, None)
    failed_with_evidence = failed.model_copy_update(
        to_update(failed.field_ref().failure_text, "Traceback ... AssertionError: share 64401"),
        to_update(failed.field_ref().captured_output, "----- Captured stderr call -----\nStep 4 reply 64402"),
        to_update(failed.field_ref().output_dir_relative_path, "test_journey[stable]"),
        to_update(
            failed.field_ref().artifacts,
            (
                SmokeArtifact(
                    relative_path=NonEmptyStr("test_journey[stable]/01-landing-0.png"), kind=SmokeArtifactKind.IMAGE
                ),
                SmokeArtifact(
                    relative_path=NonEmptyStr("test_journey[stable]/app-logs/minds.log"), kind=SmokeArtifactKind.FILE
                ),
            ),
        ),
        to_update(
            failed.field_ref().steps,
            (
                SmokeStepRecord(
                    label=NonEmptyStr("Step 3b: run the template's CI suites"),
                    outcome=SmokeStepOutcome.SKIPPED,
                    started_at=_SCENARIO_STARTED_AT,
                    finished_at=_SCENARIO_STARTED_AT,
                    detail="skipped by request 64403",
                ),
            ),
        ),
        to_update(
            failed.field_ref().subtests,
            (
                SmokeSubtestResult(
                    label=NonEmptyStr("dwt:root::test_b.py::test_broken"),
                    outcome=SmokeScenarioOutcome.FAILED,
                    duration_seconds=NonNegativeFloat(3.0),
                    summary="boom 64404",
                    failure_text="Failed in the workspace on its only attempt:\nboom 64404",
                ),
            ),
        ),
    )
    base = _report((failed_with_evidence,), {"SENTRY_ORG": "example-org"})
    report = base.model_copy_update(
        to_update(
            base.field_ref().studio_binary,
            StudioBinary(
                kind=StudioBinaryKind.PACKAGED,
                executable=Path("/opt/Imbue Studio/imbue-studio"),
                main_js=None,
                label=NonEmptyStr("alpha 0.10.3 (linux .deb)"),
                root_name=NonEmptyStr("minds"),
            ),
        ),
        to_update(
            base.field_ref().environment,
            base.environment.model_copy_update(
                to_update(
                    base.environment.field_ref().ci_run_url,
                    AnyUrl("https://github.com/imbue-ai/mngr-internal/actions/runs/19283746/attempts/2"),
                ),
                to_update(base.environment.field_ref().ci_run_description, "minds smoke run 19283746, attempt 2"),
                to_update(
                    base.environment.field_ref().checkout,
                    SmokeRunCheckout(
                        root_dir=Path("/checkout"),
                        git_sha=NonEmptyStr("c993c71e0f6b2a1d"),
                        branch_name="main",
                        is_dirty=True,
                        is_pushed=False,
                        commit_url=AnyUrl(f"{_REPOSITORY_URL}/commit/c993c71e0f6b2a1d"),
                    ),
                ),
            ),
        ),
        to_update(
            base.field_ref().collection_errors,
            (
                SmokeCollectionError(
                    node_id=NonEmptyStr("apps/minds/smoke_tests/test_smoke_signup.py"),
                    summary="ImportError: cannot import name 64405",
                    failure_text="ImportError while importing test module: cannot import name 64405",
                ),
            ),
        ),
        to_update(
            base.field_ref().created_resources,
            (
                SmokeRunManifestEntry(
                    kind=SmokeResourceKind.WORKSPACE,
                    identifier=NonEmptyStr("host-db-id-64406"),
                    owner_email=NonEmptyStr("studio-smoke-persistent@example.test"),
                    created_at=_SCENARIO_STARTED_AT,
                ),
            ),
        ),
    )

    html = render_smoke_run_report_html(report)

    for expected in (
        "alpha 0.10.3 (linux .deb)",
        'href="https://github.com/imbue-ai/mngr-internal/actions/runs/19283746/attempts/2"',
        "uncommitted changes",
        "No remote branch contains this commit",
        "ImportError: cannot import name 64405",
        "host-db-id-64406",
        "Step 4 reply 64402",
        "Traceback ... AssertionError: share 64401",
        'src="test_journey%5Bstable%5D/01-landing-0.png"',
        'href="test_journey%5Bstable%5D/app-logs/minds.log"',
        "skipped by request 64403",
        "Failed in the workspace on its only attempt:\nboom 64404",
        f'href="{_REPOSITORY_URL}/blob/c993c71e0f6b2a1d/apps/minds/docs/testing-overview.md#110-smoke-tests-appsmindssmoke_tests"',
    ):
        assert expected in html, expected


def test_a_parameter_row_is_its_name_its_value_and_what_it_does() -> None:
    report = _report((), {"MINDS_SMOKE_TIER": "staging"})

    html = render_smoke_run_report_html(report)

    parameters_section = html[
        html.index('<section id="parameters">') : html.index("</section>", html.index('<section id="parameters">'))
    ]
    assert set(re.findall(r"<tr><th>[^\n]*</th></tr>", parameters_section)) == {
        "<tr><th>Parameter</th><th>Value</th><th>Explanation</th></tr>"
    }
    tier_row = re.search(r'<tr class="parameter is-set">(.*?)</tr>', parameters_section, flags=re.DOTALL)
    assert tier_row is not None
    assert re.findall(r"<td[^>]*>(.*?)</td>", tier_row.group(1), flags=re.DOTALL) == [
        "<code>MINDS_SMOKE_TIER</code>",
        "<code>staging</code>",
        "The deployed env under test.",
    ]


def test_each_outcome_is_its_own_fold_and_only_the_passed_and_skipped_ones_start_closed() -> None:
    report = _report(
        (
            _scenario("test_fails_51201", SmokeScenarioOutcome.FAILED, "AssertionError: 503", (), None, None),
            _scenario("test_errors_51202", SmokeScenarioOutcome.ERROR, "RuntimeError: no account", (), None, None),
            _scenario("test_passes_51203", SmokeScenarioOutcome.PASSED, "", (), None, None),
            _scenario("test_skips_51204", SmokeScenarioOutcome.SKIPPED, "no feed", (), None, None),
        ),
        {},
    )

    html = render_smoke_run_report_html(report)

    folds = re.findall(r'<details class="scenario-group (\w+)"( open)?>\s*<summary><h3>([^<]*)</h3></summary>', html)
    assert folds == [
        ("FAILED", " open", "Failed (1)"),
        ("ERROR", " open", "Errored in setup or teardown (1)"),
        ("PASSED", "", "Passed (1)"),
        ("SKIPPED", "", "Skipped (1)"),
    ]
    fold_starts = [
        (match.start(), match.group(1)) for match in re.finditer(r'<details class="scenario-group (\w+)"', html)
    ]
    fold_ends = [start for start, _ in fold_starts[1:]] + [html.index("</section>", fold_starts[-1][0])]
    fold_bounds_by_outcome = {
        outcome: (start, end) for (start, outcome), end in zip(fold_starts, fold_ends, strict=True)
    }
    for name, outcome in (("test_passes_51203", "PASSED"), ("test_skips_51204", "SKIPPED")):
        fold_start, fold_end = fold_bounds_by_outcome[outcome]
        assert fold_start < html.index(name) < fold_end, (name, outcome)


# Inputs the suite reads that a reader of the report has no use for: the throwaway account's password and
# id, its mailbox's address, token and id, and the client config path an env activation sets.
_UNREPORTED_ENV_VARS = frozenset(
    {
        SMOKE_THROWAWAY_PASSWORD_ENV_VAR,
        SMOKE_THROWAWAY_USER_ID_ENV_VAR,
        MAILTM_ADDRESS_ENV_VAR,
        MAILTM_JWT_ENV_VAR,
        MAILTM_ACCOUNT_ID_ENV_VAR,
        ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR,
    }
)


def test_every_input_the_suite_declares_is_described_in_the_report() -> None:
    declared = {value for name, value in vars(primitives).items() if name.endswith("_ENV_VAR")}
    described = {str(spec.env_var) for spec in SMOKE_RUN_PARAMETER_SPECS}

    assert declared - described - _UNREPORTED_ENV_VARS == set()
    assert described <= declared | {"PYTEST_MAX_DURATION_SECONDS"}
