"""Release test for cloudflare_forwarding: exercises all routes against the real Cloudflare API.

Requires env vars: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID,
                    CLOUDFLARE_DOMAIN, USER_CREDENTIALS

Run with:
    cd apps/cloudflare_forwarding && PYTEST_MAX_DURATION_SECONDS=300 uv run pytest \
        imbue/cloudflare_forwarding/test_cloudflare_forwarding.py -v -s --no-cov --cov-fail-under=0
"""

import base64
import json
import os
import secrets

import pytest
from starlette.testclient import TestClient

import imbue.cloudflare_forwarding.app as app_module
from imbue.cloudflare_forwarding.app import ForwardingCtx
from imbue.cloudflare_forwarding.app import HttpCloudflareOps
from imbue.cloudflare_forwarding.app import web_app


def _skip_if_missing_env() -> None:
    required = ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_DOMAIN", "USER_CREDENTIALS"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        pytest.skip(f"Missing env vars: {', '.join(missing)}")


def _admin_headers() -> dict[str, str]:
    creds = json.loads(os.environ["USER_CREDENTIALS"])
    username = next(iter(creds))
    password = creds[username]
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _admin_username() -> str:
    creds = json.loads(os.environ["USER_CREDENTIALS"])
    return next(iter(creds))


@pytest.mark.release
@pytest.mark.timeout(120)
def test_full_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end test: create tunnel, manage services with admin and agent auth,
    set auth policies, verify cascading cleanup."""
    _skip_if_missing_env()

    suffix = secrets.token_hex(4)
    agent_id = f"release-test-{suffix}"

    ops = HttpCloudflareOps(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
    )
    ctx = ForwardingCtx(ops=ops, domain=os.environ["CLOUDFLARE_DOMAIN"])
    monkeypatch.setattr(app_module, "get_ctx", lambda: ctx)

    client = TestClient(web_app)
    admin = _admin_headers()
    username = _admin_username()
    tunnel_name = f"{username}--{agent_id}"

    resp = client.post("/tunnels", json={"agent_id": agent_id}, headers=admin)
    assert resp.status_code == 200, f"Create tunnel failed: {resp.text}"
    tunnel_data = resp.json()
    assert tunnel_data["tunnel_name"] == tunnel_name
    tunnel_token = tunnel_data["token"]
    assert tunnel_token is not None

    agent_headers = {"Authorization": f"Bearer {tunnel_token}"}

    try:
        policy = {"rules": [{"action": "allow", "include": [{"email": {"email": "test@example.com"}}]}]}
        resp = client.put(f"/tunnels/{tunnel_name}/auth", json=policy, headers=admin)
        assert resp.status_code == 200, f"Set tunnel auth failed: {resp.text}"

        resp = client.get(f"/tunnels/{tunnel_name}/auth", headers=admin)
        assert resp.status_code == 200
        assert len(resp.json()["rules"]) == 1

        resp = client.post(
            f"/tunnels/{tunnel_name}/services",
            json={"service_name": f"svc1-{suffix}", "service_url": "http://localhost:8080"},
            headers=admin,
        )
        assert resp.status_code == 200, f"Add service failed: {resp.text}"
        svc1_hostname = resp.json()["hostname"]

        access_app = ops.get_access_app_by_domain(svc1_hostname)
        assert access_app is not None, f"Access Application not created for {svc1_hostname}"
        policies = ops.list_access_policies(access_app["id"])
        assert len(policies) >= 1, "Access policy not applied"

        override_policy = {"rules": [{"action": "allow", "include": [{"email": {"email": "override@example.com"}}]}]}
        resp = client.put(
            f"/tunnels/{tunnel_name}/services/svc1-{suffix}/auth",
            json=override_policy,
            headers=admin,
        )
        assert resp.status_code == 200, f"Set service auth failed: {resp.text}"

        resp = client.post(
            f"/tunnels/{tunnel_name}/services",
            json={"service_name": f"svc2-{suffix}", "service_url": "http://localhost:3000"},
            headers=agent_headers,
        )
        assert resp.status_code == 200, f"Agent add service failed: {resp.text}"

        resp = client.get(f"/tunnels/{tunnel_name}/services", headers=agent_headers)
        assert resp.status_code == 200
        services = resp.json()
        assert len(services) == 2, f"Expected 2 services, got {len(services)}"

        resp = client.delete(f"/tunnels/{tunnel_name}/services/svc2-{suffix}", headers=agent_headers)
        assert resp.status_code == 200, f"Agent remove service failed: {resp.text}"

        resp = client.post("/tunnels", json={"agent_id": "forbidden"}, headers=agent_headers)
        assert resp.status_code == 403

        resp = client.delete(f"/tunnels/{tunnel_name}", headers=agent_headers)
        assert resp.status_code == 403

        resp = client.put(f"/tunnels/{tunnel_name}/auth", json={"rules": []}, headers=agent_headers)
        assert resp.status_code == 403

    finally:
        resp = client.delete(f"/tunnels/{tunnel_name}", headers=admin)
        assert resp.status_code == 200, f"Delete tunnel failed: {resp.text}"

        resp = client.get("/tunnels", headers=admin)
        tunnel_names = [t["tunnel_name"] for t in resp.json()]
        assert tunnel_name not in tunnel_names
