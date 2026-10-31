"""The routes the web app actually calls.

Shapes are asserted against `Todd-Frontend/contracts/api.ts`. The web app parses
every response through those Zod schemas, so a snake_case key or a stray `null`
is a hard client failure — these tests are what stop that reaching the browser.
"""

from __future__ import annotations

import uuid
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
    await _reset_dev_user(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_engine()


async def _reset_dev_user(settings: Settings) -> None:
    """Start every API test from an empty tenant.

    Uploads are content-addressed, so without this the second run of the suite
    sees its own first run's documents as duplicates.
    """
    from sqlalchemy import text
    from todd.db.session import get_sessionmaker

    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("DELETE FROM users WHERE id = :id"),
            {"id": uuid.UUID(settings.auth_dev_user_id)},
        )


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
    from todd.api.routes.screens import institution_label, short_institution

    assert institution_label("icici") == "ICICI Bank"
    assert institution_label("sbi") == "State Bank of India"
    assert institution_label(None) == "Unknown bank"
    # An account row appends the type and the last four digits, so it takes
    # the short form or the line wraps on a phone.
    assert short_institution("icici") == "ICICI"
    assert short_institution("sbi") == "SBI"
    assert short_institution(None) == "Unknown bank"
    body = (await client.get("/v1/profile")).json()
    assert body["gmail"] == {"connected": False, "email": None}


async def test_goals_reads_real_configuration(client: AsyncClient) -> None:
    body = (await client.get("/v1/goals")).json()
    assert body["locked"] is True
    assert body["mode"] in {"upgrade", "waitlist"}
    assert body["onWaitlist"] is False


async def test_connect_returns_a_google_consent_url(client: AsyncClient) -> None:
    """Read-only scope, offline access, and prompt=consent — without the
    prompt a returning user gets no refresh token and the connection dies in
    an hour (§7.5)."""
    response = await client.post("/v1/sources/gmail/connect")
    assert response.status_code == 200
    url = response.json()["authUrl"]
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "gmail.readonly" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=" in url
    # Nothing wider than read-only may ever be requested.
    assert "gmail.modify" not in url and "mail.google.com" not in url


async def test_an_unconfigured_server_says_so_in_the_envelope(
    settings: Settings,
) -> None:
    """Never a bare FastAPI `detail`, and never a stack trace."""
    blank = settings.model_copy(
        update={"auth_dev_bypass": True, "google_client_id": None, "google_client_secret": None}
    )
    app = create_app(blank)
    init_engine(blank)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post("/v1/sources/gmail/connect")
    await dispose_engine()

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "unavailable"
    assert error["message"] and error["requestId"]


async def test_connect_stores_the_pkce_verifier_with_the_state(
    client: AsyncClient, settings: Settings
) -> None:
    """Flow enables PKCE by default and puts a code_challenge in the consent
    URL. The exchange runs on a different Flow instance, so without the stored
    verifier Google answers `(invalid_grant) Missing code verifier`."""
    from sqlalchemy import text
    from todd.db.session import get_sessionmaker

    url = (await client.post("/v1/sources/gmail/connect")).json()["authUrl"]
    assert "code_challenge=" in url
    state = url.split("state=")[1].split("&")[0]

    async with get_sessionmaker()() as session:
        verifier = (
            await session.execute(
                text("SELECT code_verifier FROM oauth_states WHERE state = :s"),
                {"s": state},
            )
        ).scalar_one()
    assert verifier, "the consent URL carries a challenge but no verifier was stored"


async def test_the_oauth_state_is_single_use(client: AsyncClient) -> None:
    """A replayed callback must not mint a second connection."""
    url = (await client.post("/v1/sources/gmail/connect")).json()["authUrl"]
    state = url.split("state=")[1].split("&")[0]

    first = await client.get(f"/v1/gmail/callback?state={state}&code=bogus")
    second = await client.get(f"/v1/gmail/callback?state={state}&code=bogus")

    # Both redirect rather than rendering JSON at a human, but the second
    # fails on the state, not on the code.
    assert first.status_code == 303
    assert second.status_code == 303
    assert "error=expired" in second.headers["location"]


async def test_a_denied_consent_redirects_rather_than_erroring(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/gmail/callback?error=access_denied")
    assert response.status_code == 303
    assert "error=denied" in response.headers["location"]


async def test_sync_without_a_connection_is_a_clean_404(client: AsyncClient) -> None:
    response = await client.post("/v1/gmail/sync")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


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

    # Every Node operation must still exist, or the web app breaks on cutover.
    assert not theirs - ours, f"operations the web app relies on went missing: {theirs - ours}"

    # New operations are fine, but each has to be declared here, so that
    # accidental surface shows up as a failing test rather than a surprise.
    additions = {
        "GET /v1/categories/{slug}/transactions",
        "GET /v1/categories",
        "POST /v1/transactions/{transaction_id}/category",
    }
    assert ours - theirs == additions, f"undeclared new: {ours - theirs - additions}"


# ------------------------------------------------------------- the pipeline


async def test_upload_rejects_a_request_with_no_file(client: AsyncClient) -> None:
    assert (await client.post("/v1/documents")).status_code == 422


async def test_a_non_pdf_upload_fails_rather_than_being_accepted(
    client: AsyncClient,
) -> None:
    """Accepting bytes we cannot parse leaves the user watching a queue that
    never drains, so the document is recorded as failed, not queued."""
    response = await client.post(
        "/v1/documents", files={"files": ("notes.pdf", b"hello, not a pdf", "application/pdf")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"accepted": 1, "locked": 0, "duplicates": 0}

    listed = (await client.get("/v1/documents")).json()
    mine = next(d for d in listed if d["filename"] == "notes.pdf")
    assert mine["status"] == "failed"
    assert mine["error"]


async def test_the_same_bytes_twice_are_one_document(client: AsyncClient) -> None:
    """Rule 3 of the design: every stage is idempotent."""
    payload = {"files": ("dupe.pdf", b"not a pdf either", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    second = (await client.post("/v1/documents", files=payload)).json()
    assert first["duplicates"] == 0
    assert second == {"accepted": 0, "locked": 0, "duplicates": 1}


async def test_unlock_with_nothing_locked_is_not_an_error(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/unlock",
        json={
            "accountId": "00000000-0000-4000-8000-000000000001",
            "password": "whatever",
            "remember": False,
        },
    )
    assert response.status_code == 200
    assert set(response.json()) == {"unlocked", "stillLocked"}


async def test_unlock_rejects_an_empty_password(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/unlock",
        json={
            "accountId": "00000000-0000-4000-8000-000000000001",
            "password": "",
            "remember": False,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


# ------------------------------------------------- category drill-down


async def test_a_category_drill_down_totals_the_same_as_its_bar(
    client: AsyncClient,
) -> None:
    """A drill-down whose total differs from the bar it opened is worse than
    no drill-down at all, so both must apply the same filters."""
    report = (await client.get("/v1/report?months=12")).json()
    for bar in report["categories"]:
        detail = (
            await client.get(f"/v1/categories/{bar['slug']}/transactions?months=12&limit=1")
        ).json()
        assert detail["totalPaise"] == bar["totalPaise"], bar["slug"]


async def test_the_drill_down_reports_which_rung_labelled_each_row(
    client: AsyncClient,
) -> None:
    body = (await client.get("/v1/categories/uncategorized/transactions")).json()
    assert body["slug"] == "uncategorized"
    assert body["name"] == "Uncategorized"
    for row in body["transactions"]:
        assert row["categorySource"] is not None


async def test_an_unknown_category_is_a_clean_404(client: AsyncClient) -> None:
    response = await client.get("/v1/categories/not-a-category/transactions")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_the_uncategorized_bucket_is_not_a_404(client: AsyncClient) -> None:
    """It is synthetic — no row in `categories` — but it must still open."""
    assert (await client.get("/v1/categories/uncategorized/transactions")).status_code == 200


async def test_the_drill_down_paginates(client: AsyncClient) -> None:
    body = (await client.get("/v1/categories/uncategorized/transactions?limit=1")).json()
    assert len(body["transactions"]) <= 1
    assert body["count"] >= len(body["transactions"])


async def test_the_drill_down_rejects_an_absurd_limit(client: AsyncClient) -> None:
    assert (
        await client.get("/v1/categories/uncategorized/transactions?limit=99999")
    ).status_code == 422


async def test_income_drills_down_the_other_side_of_the_ledger(
    client: AsyncClient,
) -> None:
    """Every "Came in" source must open, and total what its bar said."""
    report = (await client.get("/v1/report?months=12")).json()
    for source in report["incomeSources"]:
        detail = (
            await client.get(
                f"/v1/categories/{source['slug']}/transactions?direction=credit&months=12&limit=1"
            )
        ).json()
        assert detail["totalPaise"] == source["totalPaise"], source["slug"]


async def test_the_income_sources_account_for_all_the_money_in(
    client: AsyncClient,
) -> None:
    """A breakdown that does not add up to its own headline is worse than
    no breakdown."""
    report = (await client.get("/v1/report?months=12")).json()
    assert (
        sum(s["totalPaise"] for s in report["incomeSources"]) == (report["totals"]["moneyInPaise"])
    )


async def test_direction_defaults_to_spend(client: AsyncClient) -> None:
    """An existing link with no `direction` still means what it used to."""
    without = (await client.get("/v1/categories/uncategorized/transactions")).json()
    explicit = (
        await client.get("/v1/categories/uncategorized/transactions?direction=debit")
    ).json()
    assert without["totalPaise"] == explicit["totalPaise"]


async def test_the_refunds_bucket_opens_although_it_is_synthetic(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/categories/refunds/transactions?direction=credit")
    assert response.status_code == 200
    assert response.json()["name"] == "Refunds"


async def test_an_invalid_direction_is_refused(client: AsyncClient) -> None:
    response = await client.get("/v1/categories/groceries/transactions?direction=sideways")
    assert response.status_code == 422


# --------------------------------------------------- filing a transaction


async def test_the_categories_list_offers_everything_seeded(client: AsyncClient) -> None:
    body = (await client.get("/v1/categories")).json()
    slugs = {c["slug"] for c in body["categories"]}
    assert {"groceries", "dining_out", "transfers_own", "income_salary"} <= slugs
    assert all(c["kind"] in {"spend", "income", "transfer"} for c in body["categories"])


async def test_filing_a_transaction_moves_it(client: AsyncClient) -> None:
    rows = (await client.get("/v1/categories/uncategorized/transactions?limit=1")).json()
    if not rows["transactions"]:
        pytest.skip("no unlabelled rows in the test tenant")
    txn = rows["transactions"][0]

    response = await client.post(
        f"/v1/transactions/{txn['id']}/category",
        json={"categorySlug": "groceries", "applyToFuture": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["categorySlug"] == "groceries"
    assert body["affected"] >= 1


async def test_filing_under_an_unknown_category_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/transactions/00000000-0000-4000-8000-000000000009/category",
        json={"categorySlug": "not-a-category", "applyToFuture": False},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_filing_a_transaction_that_does_not_exist_is_a_clean_404(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/transactions/00000000-0000-4000-8000-000000000009/category",
        json={"categorySlug": "groceries", "applyToFuture": False},
    )
    assert response.status_code == 404


async def test_a_malformed_transaction_id_is_a_400_not_a_500(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/transactions/not-a-uuid/category",
        json={"categorySlug": "groceries", "applyToFuture": False},
    )
    assert response.status_code == 400


async def test_rows_carry_a_server_written_title(client: AsyncClient) -> None:
    """§6 — the server writes display strings, so two screens cannot phrase
    the same row differently."""
    rows = (await client.get("/v1/categories/uncategorized/transactions?limit=5")).json()
    for txn in rows["transactions"]:
        assert txn["title"]
        assert "accountMasked" in txn
        assert "rail" in txn
