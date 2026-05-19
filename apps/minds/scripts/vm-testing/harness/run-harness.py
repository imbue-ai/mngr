#!/usr/bin/env python3
"""In-VM end-to-end test harness for the packaged minds.app.

Runs INSIDE a fresh macOS VM. Inputs are wired through env vars set by the
host orchestrator (run-test.sh):

    MINDS_APP_PATH      Path to the unzipped minds.app bundle inside the VM
                        (typically under /Volumes/My Shared Files/share/).
    RESULTS_DIR         Directory where structured results are written.
                        Lives on the shared volume so the host can read them
                        after the harness exits.
    TEMPLATE_GIT_URL    Git URL passed to /api/create-agent. Defaults to
                        forever-claude-template on GitHub.
    LAUNCH_MODE         /api/create-agent launch_mode (LOCAL, LIMA, DOCKER).
                        Defaults to LOCAL.
    HOST_NAME           host_name to assign to the created agent. Defaults
                        to a timestamped value.
    TEST_PROMPT         Message body to send via `mngr message`.
    EXPECTED_RESPONSE   Substring that must appear in a minds-events.jsonl
                        message before the message step is considered to
                        have succeeded.
    APPLY_QUARANTINE    "1" to xattr-tag the app with com.apple.quarantine
                        before launch (exercises Gatekeeper). Default off.
    BACKEND_READY_TIMEOUT, CREATE_TIMEOUT, MESSAGE_TIMEOUT (seconds, ints):
                        per-step timeouts.

The harness writes ``junit.xml``, ``summary.json``, ``minds.log``,
``minds-events.jsonl``, and ``launcher.log`` to ``RESULTS_DIR``.

Exit code: 0 on full success, 1 on any failed step. The failed step's name
is printed to stderr.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

HOME = Path.home()
MINDS_DATA_DIR = HOME / ".minds"
MINDS_LOG = MINDS_DATA_DIR / "logs" / "minds.log"
MINDS_EVENTS = MINDS_DATA_DIR / "logs" / "minds-events.jsonl"


@dataclass
class StepResult:
    name: str
    duration_s: float
    passed: bool
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    sys.stderr.write(f"[harness {ts}] {msg}\n")
    sys.stderr.flush()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"required env var {name} is not set")
    return val


def run_step(name: str, fn: Any) -> StepResult:
    log(f"=== {name} ===")
    start = time.monotonic()
    try:
        extra = fn() or {}
    except StepFailure as exc:
        duration = time.monotonic() - start
        log(f"FAIL {name}: {exc}")
        return StepResult(name=name, duration_s=duration, passed=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 -- harness must report any failure
        duration = time.monotonic() - start
        tb = traceback.format_exc()
        log(f"FAIL {name}: unhandled exception\n{tb}")
        return StepResult(name=name, duration_s=duration, passed=False, error=f"{exc}\n{tb}")
    duration = time.monotonic() - start
    log(f"PASS {name} ({duration:.1f}s)")
    return StepResult(name=name, duration_s=duration, passed=True, extra=extra)


class StepFailure(RuntimeError):
    pass


def wipe_minds_state() -> dict[str, Any]:
    if MINDS_DATA_DIR.exists():
        shutil.rmtree(MINDS_DATA_DIR)
    # Also nuke any leftover minds.app from a prior run so we are testing the
    # exact bundle the orchestrator copied in.
    target = Path("/Applications/minds.app")
    if target.exists():
        shutil.rmtree(target)
    return {"data_dir": str(MINDS_DATA_DIR)}


def install_app(source: Path) -> dict[str, Any]:
    """Install minds.app into /Applications/.

    ``source`` may be either an unzipped ``.app`` bundle or a tarball of one
    (suffix ``.tar`` or ``.tar.gz``). The tarball path exists because the
    Tart shared-volume virtiofs implementation mishandles the framework
    symlinks inside an Electron bundle (they show up as cyclic), so the host
    orchestrator tars the bundle before staging it.
    """
    target = Path("/Applications/minds.app")
    if not source.exists():
        raise StepFailure(f"source bundle does not exist: {source}")
    if source.is_dir():
        log(f"copying {source} -> {target} (ditto)")
        subprocess.check_call(["/usr/bin/ditto", str(source), str(target)])
    else:
        log(f"extracting {source} -> /Applications/ (tar)")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["/usr/bin/tar", "-xf", str(source), "-C", str(target.parent)])
        if not target.exists():
            raise StepFailure(f"tarball {source} did not contain minds.app at /Applications/minds.app")
    if os.environ.get("APPLY_QUARANTINE") == "1":
        log("applying com.apple.quarantine xattr (Gatekeeper exercise)")
        subprocess.check_call(
            [
                "xattr",
                "-w",
                "com.apple.quarantine",
                "0181;00000000;Safari;",
                str(target),
            ]
        )
    return {"installed_at": str(target)}


def launch_app() -> dict[str, Any]:
    binary = Path("/Applications/minds.app/Contents/MacOS/minds")
    if not binary.exists():
        raise StepFailure(f"minds binary missing at {binary}")
    log(f"exec'ing {binary} with SKIP_AUTH=1")
    env = os.environ.copy()
    env["SKIP_AUTH"] = "1"
    # The cirruslabs vanilla VM has no unlocked keychain, so latchkey cannot
    # derive its credential-store encryption key. Provide a deterministic
    # one so the gateway initializes cleanly; the value is irrelevant for a
    # throwaway VM but its absence aborts agent creation.
    env.setdefault(
        "LATCHKEY_ENCRYPTION_KEY",
        "vmtest-deterministic-latchkey-key-do-not-reuse-outside-throwaway-vms",
    )
    # start_new_session detaches the child from the SSH session's controlling
    # terminal so it survives after this harness invocation exits. stdout/
    # stderr are redirected to launcher_log so we have something to grab if
    # the backend never comes up.
    launcher_log = MINDS_DATA_DIR / "logs" / "launcher.log"
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = launcher_log.open("ab")
    proc = subprocess.Popen(  # noqa: S603 -- absolute path, fixed args, trusted env
        [str(binary)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"minds pid={proc.pid}")
    # Persist the pid so the cleanup step can find it again even if the
    # harness process gets a fresh shell.
    (MINDS_DATA_DIR / "harness.pid").write_text(str(proc.pid))
    return {"pid": proc.pid}


def _read_backend_port_from_log() -> int | None:
    if not MINDS_LOG.exists():
        return None
    text = MINDS_LOG.read_text(errors="replace")
    # Extract the dynamic backend port from the ``Bare-origin`` line the
    # forward supervisor writes once it starts listening.
    m = re.search(r"Bare-origin:\s*http://[^:]+:(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def _read_backend_port_from_ps() -> int | None:
    out = subprocess.run(
        ["ps", "-axo", "command"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        if "minds" not in line:
            continue
        if not any(kw in line for kw in ("forward", "run")):
            continue
        m = re.search(r"--port\s+(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def wait_for_backend(timeout_s: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    port: int | None = None
    while time.monotonic() < deadline:
        port = _read_backend_port_from_log() or _read_backend_port_from_ps()
        if port is not None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    log(f"backend listening on 127.0.0.1:{port}")
                    break
            except OSError:
                pass
        time.sleep(2)
    else:
        raise StepFailure(f"backend did not start within {timeout_s}s")

    # Then poll HTTP until the server actually responds (uv sync can take a
    # while on cold cache; the TCP socket binds well before the app is ready).
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url)
            req.add_header("Cookie", "minds_session=skip")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- localhost only
                if resp.status == 200:
                    log(f"backend HTTP 200 on {url}")
                    return {"port": port}
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(3)
    raise StepFailure(f"backend HTTP did not return 200 within {timeout_s}s (port={port})")


def _http_json(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Cookie", "minds_session=skip")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- localhost only
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return exc.code, payload


def create_agent(
    port: int,
    git_url: str,
    host_name: str,
    launch_mode: str,
    ai_provider: str,
    anthropic_api_key: str,
    timeout_s: int,
) -> dict[str, Any]:
    base = f"http://127.0.0.1:{port}"
    body: dict[str, Any] = {
        "git_url": git_url,
        "host_name": host_name,
        "launch_mode": launch_mode,
        "ai_provider": ai_provider,
    }
    if ai_provider == "API_KEY":
        if not anthropic_api_key:
            raise StepFailure("AI_PROVIDER=API_KEY but ANTHROPIC_API_KEY is empty")
        body["anthropic_api_key"] = anthropic_api_key
    status_code, payload = _http_json(
        "POST",
        f"{base}/api/create-agent",
        body=body,
    )
    if status_code != 200:
        raise StepFailure(f"POST /api/create-agent returned {status_code}: {payload}")
    creation_id = payload.get("agent_id")
    if not creation_id:
        raise StepFailure(f"POST /api/create-agent did not return agent_id: {payload}")
    log(f"creation started, creation_id={creation_id}")

    deadline = time.monotonic() + timeout_s
    last_status: str | None = None
    while time.monotonic() < deadline:
        code, info = _http_json("GET", f"{base}/api/create-agent/{creation_id}/status")
        if code != 200:
            raise StepFailure(f"status endpoint returned {code}: {info}")
        status = info.get("status")
        if status != last_status:
            log(f"creation status: {status}")
            last_status = status
        if status == "DONE":
            return {
                "creation_id": creation_id,
                "agent_id": info.get("agent_id"),
                "host_name": host_name,
            }
        if status == "FAILED":
            raise StepFailure(f"agent creation failed: {info.get('error', 'unknown')}")
        time.sleep(5)
    raise StepFailure(f"agent creation did not finish within {timeout_s}s (last status={last_status})")


def _resource_path(name: str) -> Path:
    return Path("/Applications/minds.app/Contents/Resources") / name


def _mngr_env() -> dict[str, str]:
    env = os.environ.copy()
    uv_bin_dir = _resource_path("uv")
    git_bin_dir = _resource_path("git/bin")
    env["PATH"] = f"{uv_bin_dir}:{git_bin_dir}:" + env.get("PATH", "")
    env["UV_CACHE_DIR"] = str(MINDS_DATA_DIR / ".uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(MINDS_DATA_DIR / ".uv-python")
    env["MNGR_HOST_DIR"] = str(MINDS_DATA_DIR / "mngr")
    env["MNGR_PREFIX"] = "minds-"
    env["MINDS_ROOT_NAME"] = "minds"
    return env


def send_message(host_name: str, prompt: str, expected_response: str, timeout_s: int) -> dict[str, Any]:
    """Send a message via `mngr message` and wait for the expected reply.

    mngr's TUI submission watchdog can report failure at 90 s even when the
    keystroke did land; we treat the CLI's exit code as advisory and confirm
    delivery by tailing the events log for the expected response substring.
    """
    uv = _resource_path("uv/uv")
    pyproject = _resource_path("pyproject")
    if not uv.exists():
        raise StepFailure(f"bundled uv binary missing at {uv}")
    if not pyproject.exists():
        raise StepFailure(f"bundled pyproject missing at {pyproject}")

    # Snapshot the events log offset so we only consider events emitted from
    # this point forward when watching for the expected response.
    initial_offset = MINDS_EVENTS.stat().st_size if MINDS_EVENTS.exists() else 0

    cmd = [
        str(uv),
        "run",
        "--project",
        str(pyproject),
        "mngr",
        "message",
        host_name,
        "-m",
        prompt,
    ]
    log(f"sending message via mngr (timeout 180s): {' '.join(cmd)}")
    try:
        proc = subprocess.run(  # noqa: S603 -- absolute paths, controlled args
            cmd,
            env=_mngr_env(),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log(f"mngr message timed out at 180s (advisory): {exc}")
        proc = None

    if proc is not None and proc.returncode != 0:
        # Advisory: mngr's submit watchdog times out at 90s even when keystroke
        # made it through. Continue and verify via events log.
        log(f"mngr message exited nonzero (rc={proc.returncode}); stderr tail:\n{proc.stderr[-500:]}")

    log(f"watching {MINDS_EVENTS} for '{expected_response}' (timeout {timeout_s}s)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if MINDS_EVENTS.exists():
            with MINDS_EVENTS.open("rb") as fh:
                fh.seek(initial_offset)
                for raw_line in fh:
                    line = raw_line.decode(errors="replace")
                    if expected_response in line:
                        log("expected response found in events log")
                        return {"matched_line_excerpt": line.strip()[:200]}
        time.sleep(2)
    raise StepFailure(f"expected response '{expected_response}' not seen in {MINDS_EVENTS} within {timeout_s}s")


def capture_artifacts(results_dir: Path) -> dict[str, Any]:
    results_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}
    if MINDS_LOG.exists():
        shutil.copy2(MINDS_LOG, results_dir / "minds.log")
        out["minds_log"] = "minds.log"
    if MINDS_EVENTS.exists():
        shutil.copy2(MINDS_EVENTS, results_dir / "minds-events.jsonl")
        out["minds_events"] = "minds-events.jsonl"
    launcher_log = MINDS_DATA_DIR / "logs" / "launcher.log"
    if launcher_log.exists():
        shutil.copy2(launcher_log, results_dir / "launcher.log")
        out["launcher_log"] = "launcher.log"
    return out


def write_junit(results: list[StepResult], path: Path, suite_name: str) -> None:
    suite = ET.Element(
        "testsuite",
        name=suite_name,
        tests=str(len(results)),
        failures=str(sum(1 for r in results if not r.passed)),
        time=f"{sum(r.duration_s for r in results):.3f}",
    )
    for r in results:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=suite_name,
            name=r.name,
            time=f"{r.duration_s:.3f}",
        )
        if not r.passed:
            failure = ET.SubElement(case, "failure", message=(r.error or "")[:200])
            failure.text = r.error or ""
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    minds_app_path = Path(env_required("MINDS_APP_PATH"))
    results_dir = Path(env_required("RESULTS_DIR"))
    template_url = os.environ.get("TEMPLATE_GIT_URL", "https://github.com/imbue-ai/forever-claude-template.git")
    launch_mode = os.environ.get("LAUNCH_MODE", "LOCAL")
    ai_provider = os.environ.get("AI_PROVIDER", "SUBSCRIPTION")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if ai_provider == "API_KEY" and not anthropic_api_key:
        log("AI_PROVIDER=API_KEY requires ANTHROPIC_API_KEY; aborting")
        return 1
    host_name = os.environ.get("HOST_NAME") or f"vmtest-{int(time.time())}"
    test_prompt = os.environ.get(
        "TEST_PROMPT",
        "Print exactly the literal string PINGPONG-OK on a line by itself, with no other output.",
    )
    expected_response = os.environ.get("EXPECTED_RESPONSE", "PINGPONG-OK")
    backend_timeout = env_int("BACKEND_READY_TIMEOUT", 300)
    create_timeout = env_int("CREATE_TIMEOUT", 600)
    message_timeout = env_int("MESSAGE_TIMEOUT", 300)

    results: list[StepResult] = []
    creation_info: dict[str, Any] = {}

    results.append(run_step("wipe_minds_state", wipe_minds_state))
    if not results[-1].passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)

    results.append(run_step("install_app", lambda: install_app(minds_app_path)))
    if not results[-1].passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)

    results.append(run_step("launch_app", launch_app))
    if not results[-1].passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)

    backend_step = run_step("wait_for_backend", lambda: wait_for_backend(backend_timeout))
    results.append(backend_step)
    if not backend_step.passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)
    backend_port = int(backend_step.extra["port"])

    create_step = run_step(
        "create_agent",
        lambda: create_agent(
            backend_port,
            template_url,
            host_name,
            launch_mode,
            ai_provider,
            anthropic_api_key,
            create_timeout,
        ),
    )
    results.append(create_step)
    if not create_step.passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)
    creation_info = create_step.extra

    message_step = run_step(
        "send_message",
        lambda: send_message(host_name, test_prompt, expected_response, message_timeout),
    )
    results.append(message_step)
    if not message_step.passed:
        return _finalize(results, results_dir, creation_info, exit_code=1)

    return _finalize(results, results_dir, creation_info, exit_code=0)


def _finalize(
    results: list[StepResult],
    results_dir: Path,
    creation_info: dict[str, Any],
    exit_code: int,
) -> int:
    capture_artifacts(results_dir)
    write_junit(results, results_dir / "junit.xml", "minds_vm_harness")
    summary = {
        "passed": all(r.passed for r in results),
        "exit_code": exit_code,
        "host_name": creation_info.get("host_name"),
        "agent_id": creation_info.get("agent_id"),
        "creation_id": creation_info.get("creation_id"),
        "steps": [
            {
                "name": r.name,
                "passed": r.passed,
                "duration_s": round(r.duration_s, 3),
                "error": r.error,
            }
            for r in results
        ],
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    if exit_code != 0:
        failed = next(r for r in results if not r.passed)
        sys.stderr.write(f"FAILED at step: {failed.name}\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
