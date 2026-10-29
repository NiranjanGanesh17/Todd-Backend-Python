"""The routes the web app actually calls.

Shapes are asserted against `Todd-Frontend/contracts/api.ts`. The web app parses
every response through those Zod schemas, so a snake_case key or a stray `null`
is a hard client failure — these tests are what stop that reaching the browser.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from todd.api.main import create_app
from todd.config import Settings
from todd.db.session import dispose_engine, init_engine

WEB_ORIGIN = "http://localhost:3000"


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    test_settings = settings.model_copy(update={"auth_dev_bypass": True})
    app = create_app(test_settings)
    init_engine(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_engine()


async def test_healthz_is_liveness_only(client: AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "skipped", "redis": "skipped"}
    assert isinstance(body["uptimeSeconds"], int | float)


async def test_readyz_actually_probes_the_database(client: AsyncClient) -> None:
    body = (await client.get("/readyz")).json()
    assert body["checks"]["database"] == "ok"


async def test_every_response_key_is_camel_case(client: AsyncClient) -> None:
    """The contract is camelCase; a snake_case key fails Zod on the client."""
    for path in ("/v1/report", "/v1/profile", "/v1/goals", "/v1/gmail/status", "/healthz"):
        body = (await client.get(path)).json()
        assert isinstance(body, dict)
        offenders = [k for k in body if "_" in k]
        assert not offenders, f"{path} returned snake_case keys: {offenders}"


async def test_report_window_ends_last_month(client: AsyncClient) -> None:
    from todd.parsers.dates import today_ist

    window = (await client.get("/v1/report")).json()["window"]
    assert window["monthCount"] == 6
    assert len(window["months"]) == 6
    assert f"{today_ist():%Y-%m}" not in window["months"], "statements arrive in arrears"
    assert isinstance(window["label"], str) and window["label"]


@pytest.mark.parametrize("months", [1, 3, 6, 12])
async def test_report_honours_every_allowed_window(client: AsyncClient, months: int) -> None:
    window = (await client.get(f"/v1/report?months={months}")).json()["window"]
    assert window["monthCount"] == months
    assert len(window["months"]) == months


async def test_report_falls_back_to_the_saved_window(client: AsyncClient) -> None:
    """An out-of-range value must not 500 or echo itself back."""
    assert (await client.get("/v1/report?months=7")).json()["window"]["monthCount"] == 6


async def test_an_empty_system_says_so_rather_than_inventing_data(client: AsyncClient) -> None:
    report = (await client.get("/v1/report")).json()
    assert report["hasData"] is False
    assert report["totals"] == {"spentPaise": 0, "moneyInPaise": 0, "savedPctOfIncome": None}
    assert report["categories"] == []
    assert report["biggestMonth"] is None
    assert all(m["totalPaise"] == 0 for m in report["monthlySpend"])


async def test_profile_sends_the_display_label_not_the_slug(client: AsyncClient) -> None:
    """The web app renders `account.institution` straight into the UI (§6)."""
    from todd.api.routes.screens import institution_label

    assert institution_label("icici") == "ICICI Bank"
    assert institution_label("sbi") == "State Bank of India"
    assert institution_label(None) == "Unknown bank"
    body = (await client.get("/v1/profile")).json()
    assert body["gmail"] == {"connected": False, "email": None}


async def test_goals_reads_real_configuration(client: AsyncClient) -> None:
    body = (await client.get("/v1/goals")).json()
    assert body["locked"] is True
    assert body["mode"] in {"upgrade", "waitlist"}
    assert body["onWaitlist"] is False


async def test_unbuilt_routes_answer_with_the_error_envelope(client: AsyncClient) -> None:
    """Never a bare FastAPI `detail`, and never a stack trace."""
    response = await client.post("/v1/sources/gmail/connect")
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "not_implemented"
    assert error["message"] and error["requestId"]


async def test_connect_accepts_an_empty_post(client: AsyncClient) -> None:
    """§6 — the web app POSTs this with no body; it must not 422."""
    assert (await client.post("/v1/sources/gmail/connect")).status_code != 422


async def test_a_not_found_uses_the_same_envelope(client: AsyncClient) -> None:
    body = (await client.get("/v1/nope")).json()
    assert set(body["error"]) >= {"code", "message", "requestId"}


async def test_cors_allows_the_web_origin_with_credentials(client: AsyncClient) -> None:
    response = await client.get("/v1/report", headers={"Origin": WEB_ORIGIN})
    assert response.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_cors_refuses_an_unknown_origin(client: AsyncClient) -> None:
    response = await client.get("/v1/report", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


async def test_security_headers_are_present(client: AsyncClient) -> None:
    headers = (await client.get("/healthz")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "x-request-id" in headers


async def test_openapi_matches_the_node_operation_set(client: AsyncClient) -> None:
    """§6 — the generated spec must stay shape-compatible or the web app breaks."""
    import json
    import pathlib

    node_path = pathlib.Path("/Users/niranjang/Documents/Finance/Todd-Backend/openapi.json")
    if not node_path.exists():
        pytest.skip("the Node repo is not present on this machine")

    def operations(spec: dict) -> set[str]:
        verbs = {"get", "post", "put", "patch", "delete"}
        return {f"{m.upper()} {p}" for p, item in spec["paths"].items() for m in item if m in verbs}

    ours = operations((await client.get("/openapi.json")).json())
    theirs = operations(json.loads(node_path.read_text()))
    assert ours == theirs, f"drift: only-ours={ours - theirs} only-theirs={theirs - ours}"
