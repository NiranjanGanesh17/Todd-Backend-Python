"""The routes the web app actually calls.

Shapes are asserted against `Todd-Frontend/contracts/api.ts`. The web app parses
every response through those Zod schemas, so a snake_case key or a stray `null`
is a hard client failure — these tests are what stop that reaching the browser.
"""

from __future__ import annotations

import asyncio
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


async def _drain(client: AsyncClient, documents: list[dict[str, str]]) -> dict[str, object]:
    """Wait for the upload's background parse, the way the screen does.

    Reading happens in a task now, so a test that looks straight after the
    POST sees "queued" — exactly as the browser would. Polling the progress
    endpoint is the real client's behaviour, so it is the test's too.
    """
    ids = ",".join(document["id"] for document in documents)
    for _ in range(200):
        body = (await client.get(f"/v1/documents/progress?ids={ids}")).json()
        if not body["busy"]:
            return body  # type: ignore[no-any-return]
        await asyncio.sleep(0.05)
    raise AssertionError("the upload never finished reading")


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
    assert report["totals"] == {
        "spentPaise": 0,
        "moneyInPaise": 0,
        "savedPaise": None,
        "savedPctOfIncome": None,
    }
    assert report["categories"] == []
    assert report["biggestMonth"] is None
    assert all(m["totalPaise"] == 0 for m in report["monthlySpend"])


async def test_one_month_is_a_window_of_its_own(client: AsyncClient) -> None:
    """Tapping a month on the chart has to be able to mean that month.

    The presets can only ever say "the last N", so without this the only way
    to see April is to wait until April is six months ago.
    """
    body = (await client.get("/v1/report?month=2026-04")).json()
    assert body["window"]["months"] == ["2026-04"]
    assert body["window"]["monthCount"] == 1
    assert body["window"]["label"] == "Apr 2026"

    # And it beats a preset asked for at the same time.
    both = (await client.get("/v1/report?months=12&month=2026-04")).json()
    assert both["window"]["months"] == ["2026-04"]


async def test_a_month_that_is_not_a_month_is_refused(client: AsyncClient) -> None:
    for bad in ("2026-13", "april", "2026-4", "2026"):
        response = await client.get(f"/v1/report?month={bad}")
        assert response.status_code == 400, bad
        assert response.json()["error"]["code"] == "bad_request"


async def test_all_spans_everything_or_falls_back(client: AsyncClient) -> None:
    """With no statements there is nothing to span, so it answers with the
    saved window rather than an empty range."""
    body = (await client.get("/v1/report?months=all")).json()
    assert body["window"]["monthCount"] >= 1
    assert len(body["window"]["months"]) == body["window"]["monthCount"]


async def test_a_month_with_no_statement_is_not_a_month_of_zero_spend(
    client: AsyncClient,
) -> None:
    """The chart draws those as empty outlines, so the flag has to be real."""
    body = (await client.get("/v1/report?months=6")).json()
    assert body["monthlySpend"]
    assert all(row["covered"] is False for row in body["monthlySpend"])
    assert body["window"]["incompleteMonths"] == 0


async def test_the_chart_offers_more_months_than_the_window_shows(
    client: AsyncClient,
) -> None:
    """The strip is the picker, so it has to reach past what is being shown.

    A single month whose chart holds only that month is a dead end: there is
    nothing to tap to get to another one.
    """
    one = (await client.get("/v1/report?month=2026-04")).json()
    assert one["window"]["monthCount"] == 1
    assert len(one["monthlySpend"]) > 1
    assert [row["month"] for row in one["monthlySpend"] if row["inWindow"]] == ["2026-04"]

    # The totals stay about the window, never the whole strip.
    assert one["totals"]["spentPaise"] == sum(
        row["totalPaise"] for row in one["monthlySpend"] if row["inWindow"]
    )


async def test_no_trend_without_the_months_to_compare_against(client: AsyncClient) -> None:
    """ "Up 3913%" against a window we hold no statements for is not a fact
    about spending, it is a fact about where the statements stop."""
    body = (await client.get("/v1/report?months=6")).json()
    assert all(category["trendPct"] is None for category in body["categories"])


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
        "GET /v1/documents/progress",
        "DELETE /v1/account",
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
    assert body["accepted"] == 1
    assert body["duplicates"] == 0
    assert [d["filename"] for d in body["documents"]] == ["notes.pdf"]
    await _drain(client, body["documents"])

    listed = (await client.get("/v1/documents")).json()
    mine = next(d for d in listed if d["filename"] == "notes.pdf")
    assert mine["status"] == "failed"
    assert mine["error"]


async def test_upload_answers_before_it_has_read_anything(client: AsyncClient) -> None:
    """The POST returns as soon as the bytes are on disk.

    Parsing a statement takes seconds; holding the request open for it is how
    a sleeping phone loses an upload. The screen follows the files through
    `/v1/documents/progress` instead.
    """
    response = await client.post(
        "/v1/documents", files={"files": ("slow.pdf", b"still not a pdf", "application/pdf")}
    )
    body = response.json()
    assert body["accepted"] == 1
    [queued] = body["documents"]

    progress = (await client.get(f"/v1/documents/progress?ids={queued['id']}")).json()
    [row] = progress["documents"]
    assert row["filename"] == "slow.pdf"
    # Whichever side of the parse this lands on, the row carries a sentence
    # the screen can print without composing one of its own.
    assert row["statusLabel"]

    final = await _drain(client, body["documents"])
    assert final["busy"] is False
    [done] = final["documents"]
    assert done["status"] == "failed"
    assert done["statusLabel"] == "We could not read this one"


async def test_progress_rejects_an_id_that_is_not_one(client: AsyncClient) -> None:
    response = await client.get("/v1/documents/progress?ids=not-a-uuid")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


async def test_the_same_bytes_twice_are_one_document(client: AsyncClient) -> None:
    """Rule 3 of the design: every stage is idempotent."""
    payload = {"files": ("dupe.pdf", b"not a pdf either", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    second = (await client.post("/v1/documents", files=payload)).json()
    assert first["duplicates"] == 0
    assert second["accepted"] == 0
    assert second["duplicates"] == 1

    # One document, but it is still a file this person just chose, so it comes
    # back for the screen — pointing at the document already held, not a
    # second copy of it.
    assert [d["id"] for d in second["documents"]] == [d["id"] for d in first["documents"]]


async def test_a_duplicate_still_has_a_screen_to_land_on(client: AsyncClient) -> None:
    """Re-uploading statements we already hold must not look like nothing happened.

    Every file was a duplicate, the list came back empty, and the web app
    bounced to the report without a word — which reads as an upload that
    silently failed.
    """
    payload = {"files": ("already-here.pdf", b"not a pdf at all", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, first["documents"])

    again = (await client.post("/v1/documents", files=payload)).json()
    assert again["documents"], "a duplicate must still give the screen something to show"

    body = (await client.get(f"/v1/documents/progress?ids={again['documents'][0]['id']}")).json()
    assert body["busy"] is False
    assert body["documents"][0]["filename"] == "already-here.pdf"


async def test_a_restart_picks_up_a_document_left_mid_parse(
    client: AsyncClient, settings: Settings
) -> None:
    """Parsing lives in the API process, so a restart used to strand a file.

    A document stuck at `parsing` has no task behind it any more, and the
    upload screen watching it would say "Opening…" for ever.
    """
    from sqlalchemy import text
    from todd.api.routes.documents import resume_unfinished
    from todd.db.session import get_sessionmaker, with_user

    payload = {"files": ("stranded.pdf", b"not a pdf, stranded", "application/pdf")}
    body = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, body["documents"])
    document_id = body["documents"][0]["id"]

    # Put it back the way a killed process would leave it.
    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, settings.auth_dev_user_id) as scoped,
    ):
        await scoped.execute(
            text("UPDATE documents SET status = 'parsing' WHERE id = :id"),
            {"id": uuid.UUID(document_id)},
        )

    assert await resume_unfinished(settings) >= 1
    final = await _drain(client, body["documents"])
    assert final["documents"][0]["status"] != "parsing"


async def test_offering_a_statement_again_reads_it_again(client: AsyncClient) -> None:
    """Uploading a statement twice is how someone says "look at this again".

    The document is still deduplicated — one file, one row — but the reading
    is not: the parser moves on, and rows written by an older one could
    otherwise never be repaired.
    """
    payload = {"files": ("again.pdf", b"not a pdf, but stored", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, first["documents"])
    [document] = first["documents"]

    second = (await client.post("/v1/documents", files=payload)).json()
    assert second["duplicates"] == 1
    assert second["accepted"] == 0
    # The same document, not a second copy of it...
    assert [d["id"] for d in second["documents"]] == [document["id"]]

    # ...and it went back through the parser.
    listed = (await client.get("/v1/documents")).json()
    mine = [d for d in listed if d["filename"] == "again.pdf"]
    assert len(mine) == 1, "a re-upload must not create a second document"
    await _drain(client, second["documents"])


async def test_reading_a_statement_twice_does_not_double_it(client: AsyncClient) -> None:
    """The bug this guards: one statement read twice became two of everything.

    A re-read that finally saw the masked number resolved to a different
    account, and because a fingerprint is scoped to its account, every row was
    written again under the new one — while the first statement stayed
    reconciled and kept feeding the report.
    """
    from sqlalchemy import text
    from todd.db.session import get_sessionmaker, with_user

    payload = {"files": ("twice.pdf", b"not a pdf, read twice", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, first["documents"])
    document_id = uuid.UUID(first["documents"][0]["id"])

    async def _counts() -> tuple[int, int, int]:
        async with (
            get_sessionmaker()() as session,
            session.begin(),
            with_user(session, "00000000-0000-4000-8000-000000000001") as scoped,
        ):
            accounts = (await scoped.execute(text("SELECT count(*) FROM accounts"))).scalar_one()
            statements = (
                await scoped.execute(
                    text("SELECT count(*) FROM statements WHERE document_id = :id"),
                    {"id": document_id},
                )
            ).scalar_one()
            rows = (
                await scoped.execute(
                    text(
                        "SELECT count(*) FROM transactions t JOIN statements s "
                        "ON s.id = t.statement_id WHERE s.document_id = :id"
                    ),
                    {"id": document_id},
                )
            ).scalar_one()
            return int(accounts), int(statements), int(rows)

    before = await _counts()
    again = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, again["documents"])
    assert await _counts() == before, "a second read must not add an account, a statement or a row"


async def test_a_file_read_into_two_statements_is_collapsed_on_the_next_read(
    client: AsyncClient,
) -> None:
    """Ledgers written before statements were keyed on the document.

    Re-reading such a file used to fail outright — the update collided with
    the second statement's own (account, period) — which the web app reported
    as "we could not reach the server".
    """
    from sqlalchemy import text
    from todd.db.session import get_sessionmaker, with_user

    payload = {"files": ("legacy.pdf", b"not a pdf, from before", "application/pdf")}
    body = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, body["documents"])
    document_id = uuid.UUID(body["documents"][0]["id"])

    user = "00000000-0000-4000-8000-000000000001"
    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, user) as scoped,
    ):
        # The damage: a second account and a second statement for one file.
        account = (
            await scoped.execute(
                text(
                    "INSERT INTO accounts (user_id, institution, type, masked_number, source) "
                    "VALUES (:uid, 'bob', 'savings', '1421', 'detected') RETURNING id"
                ),
                {"uid": uuid.UUID(user)},
            )
        ).scalar_one()
        await scoped.execute(
            text(
                "INSERT INTO statements (user_id, account_id, document_id, period_start, "
                "  period_end, reconciled) "
                "VALUES (:uid, :aid, :did, '2026-08-01', '2026-08-31', true)"
            ),
            {"uid": uuid.UUID(user), "aid": account, "did": document_id},
        )

    again = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, again["documents"])

    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, user) as scoped,
    ):
        left = (
            await scoped.execute(
                text("SELECT count(*) FROM statements WHERE document_id = :id"),
                {"id": document_id},
            )
        ).scalar_one()
        assert left == 1, "one file, one statement"
        stranded = (
            await scoped.execute(
                text(
                    "SELECT count(*) FROM transactions t WHERE t.statement_id IS NULL "
                    "AND t.source = 'statement'"
                )
            )
        ).scalar_one()
        # ON DELETE SET NULL would leave the duplicates published for ever.
        assert stranded == 0


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


# ------------------------------------------------------------- the erasure


async def test_deleting_the_account_leaves_nothing_behind(
    client: AsyncClient, settings: Settings
) -> None:
    """The right the profile screen promises, and the only real start over.

    Re-uploading corrects the statements we still hold; it cannot repair an
    account an older parser split in two, or a row whose statement was lost.
    """
    from todd.worker.lib.storage import build_storage

    payload = {"files": ("erase-me.pdf", b"not a pdf, but stored", "application/pdf")}
    uploaded = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, uploaded["documents"])
    assert (await client.get("/v1/documents")).json()

    response = await client.delete("/v1/account")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["removed"]["documents"] >= 1
    assert body["removed"]["files"] >= 1

    # The bypass user is created on demand, so the next request has an
    # account again — an empty one. That is what makes this call a reset in
    # development and an erasure in production.
    assert (await client.get("/v1/documents")).json() == []
    assert (await client.get("/v1/profile")).json()["accounts"] == []
    assert (await client.get("/v1/report")).json()["totals"]["spentPaise"] == 0

    # And the PDFs themselves are gone, not just the rows pointing at them.
    # Through `build_storage`, so this reads the run's own temporary
    # directory: `LocalStorage()` with no root is the development one, and
    # the erasure above would have taken the developer's statements with it.
    user = "00000000-0000-4000-8000-000000000001"
    assert not build_storage(settings).exists(f"users/{user}/documents")


async def test_an_erasure_takes_only_the_asker_with_it(client: AsyncClient) -> None:
    """One tenant's deletion must stop at the policy boundary.

    `DELETE FROM users` carries every table with it through the cascade, so a
    clause that missed RLS here would empty the database.
    """
    from sqlalchemy import text
    from todd.db.session import get_sessionmaker, with_user

    stranger = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": stranger, "email": f"{stranger}@example.test"},
        )
    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, str(stranger)) as scoped,
    ):
        await scoped.execute(
            text(
                "INSERT INTO accounts (user_id, institution, type) "
                "VALUES (:uid, 'icici', 'savings')"
            ),
            {"uid": stranger},
        )

    assert (await client.delete("/v1/account")).status_code == 200

    async with get_sessionmaker()() as session, session.begin():
        kept = (
            await session.execute(
                text("SELECT count(*) FROM accounts WHERE user_id = :uid"), {"uid": stranger}
            )
        ).scalar_one()
        assert kept == 1, "the stranger's account went with it"
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": stranger})


# ------------------------------------------------------- an exact date range


async def _spend_on(
    settings: Settings,
    days: list[tuple[str, int]],
    *,
    direction: str = "debit",
    institution: str = "icici",
) -> None:
    """Rows on given days, publishable and uncategorised, for the dev user."""
    import datetime as dt

    from sqlalchemy import text
    from todd.db.session import get_sessionmaker, with_user

    user_id = settings.auth_dev_user_id
    async with get_sessionmaker()() as session, session.begin():
        # The bypass user is created on the first request, and this runs
        # before one.
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email) ON CONFLICT DO NOTHING"),
            {"id": uuid.UUID(user_id), "email": settings.auth_dev_user_email},
        )

    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, user_id) as scoped,
    ):
        account = (
            await scoped.execute(
                text(
                    "INSERT INTO accounts (user_id, institution, type) "
                    "VALUES (:uid, :inst, 'savings') RETURNING id"
                ),
                {"uid": uuid.UUID(user_id), "inst": institution},
            )
        ).scalar_one()
        for day, paise in days:
            await scoped.execute(
                text(
                    "INSERT INTO transactions (user_id, account_id, source, txn_date, "
                    "  amount_paise, direction, raw_description, fingerprint) "
                    "VALUES (:uid, :aid, 'statement', :d, :amt, :dir, :desc, :fp)"
                ),
                {
                    "uid": uuid.UUID(user_id),
                    "aid": account,
                    "d": dt.date.fromisoformat(day),
                    "amt": paise,
                    "dir": direction,
                    "desc": f"UPI/TEST/{day}",
                    "fp": uuid.uuid4().hex,
                },
            )


async def test_a_date_range_counts_only_the_days_inside_it(
    client: AsyncClient, settings: Settings
) -> None:
    """ "From the 30th of July to the 30th of September" is a question neither
    a preset nor a month can ask, and the answer has to stop at both ends."""
    await _spend_on(
        settings,
        [("2026-07-29", 100_00), ("2026-07-30", 200_00), ("2026-08-15", 300_00)],
    )

    body = (await client.get("/v1/report?from=2026-07-30&to=2026-09-30")).json()
    window = body["window"]
    assert window["exact"] is True
    assert window["start"] == "2026-07-30"
    assert window["end"] == "2026-09-30"
    assert window["label"] == "30 Jul – 30 Sep"
    assert window["months"] == ["2026-07", "2026-08", "2026-09"]
    assert window["dayCount"] == 63

    # The 29th is a day before the range: it is not in the total...
    assert body["totals"]["spentPaise"] == 500_00
    # ...nor in July's bar, which is clipped to the days asked for rather than
    # showing a month the figures above do not contain.
    july = next(row for row in body["monthlySpend"] if row["month"] == "2026-07")
    assert july["totalPaise"] == 200_00
    assert july["inWindow"] is True
    assert body["categories"][0]["totalPaise"] == 500_00

    # And the drill-down is drawn for the same days, or its total would differ
    # from the bar that opened it.
    rows = (
        await client.get("/v1/categories/uncategorized/transactions?from=2026-07-30&to=2026-09-30")
    ).json()
    assert rows["totalPaise"] == 500_00
    assert rows["count"] == 2
    assert rows["window"]["label"] == "30 Jul – 30 Sep"


async def test_a_range_of_one_day_is_a_range(client: AsyncClient, settings: Settings) -> None:
    await _spend_on(settings, [("2026-07-30", 200_00), ("2026-07-31", 900_00)])
    body = (await client.get("/v1/report?from=2026-07-30&to=2026-07-30")).json()
    assert body["totals"]["spentPaise"] == 200_00
    assert body["window"]["dayCount"] == 1
    assert body["window"]["label"] == "30 Jul 2026"


async def test_half_a_range_is_refused_rather_than_guessed(client: AsyncClient) -> None:
    """Filling in the missing end would answer a question nobody asked — and
    the report would look like the dates had been applied."""
    for query in ("from=2026-07-30", "to=2026-09-30"):
        response = await client.get(f"/v1/report?{query}")
        assert response.status_code == 400, query
        assert response.json()["error"]["code"] == "bad_request"


async def test_a_range_that_is_not_one_is_refused(client: AsyncClient) -> None:
    bad = [
        "from=2026-09-30&to=2026-07-30",  # the wrong way round
        "from=30-07-2026&to=30-09-2026",  # day first, which is not ISO
        "from=2026-02-30&to=2026-03-01",  # not a day that exists
        "from=2000-01-01&to=2026-01-01",  # longer than the cap
    ]
    for query in bad:
        response = await client.get(f"/v1/report?{query}")
        assert response.status_code == 400, query


async def test_dates_beat_a_month_and_a_preset(client: AsyncClient) -> None:
    body = (
        await client.get("/v1/report?months=12&month=2026-04&from=2026-07-30&to=2026-09-30")
    ).json()
    assert body["window"]["start"] == "2026-07-30"


async def test_a_preset_still_carries_the_days_it_covers(client: AsyncClient) -> None:
    """Every window has dates now, not only the ones the user drew."""
    body = (await client.get("/v1/report?month=2026-04")).json()
    assert body["window"]["exact"] is False
    assert body["window"]["start"] == "2026-04-01"
    assert body["window"]["end"] == "2026-04-30"
    assert body["window"]["dayCount"] == 30


# ------------------------------------------------------------ what was saved


async def test_what_was_saved_is_what_came_in_less_what_went_out(
    client: AsyncClient, settings: Settings
) -> None:
    """The screen says a rupee figure, so the rupee figure is the server's.

    Two clients subtracting it themselves is two chances to disagree with the
    percentage printed beside it.
    """
    await _spend_on(settings, [("2026-08-10", 300_00), ("2026-08-20", 200_00)])
    await _spend_on(settings, [("2026-08-01", 1_000_00)], direction="credit", institution="bob")

    totals = (await client.get("/v1/report?month=2026-08")).json()["totals"]
    assert totals["moneyInPaise"] == 1_000_00
    assert totals["spentPaise"] == 500_00
    assert totals["savedPaise"] == 500_00
    assert totals["savedPctOfIncome"] == 50.0


async def test_spending_more_than_came_in_is_a_negative_saving(
    client: AsyncClient, settings: Settings
) -> None:
    """Rounded up to zero it would read as a month that broke even."""
    await _spend_on(settings, [("2026-08-10", 1_500_00)])
    await _spend_on(settings, [("2026-08-01", 1_000_00)], direction="credit", institution="bob")

    totals = (await client.get("/v1/report?month=2026-08")).json()["totals"]
    assert totals["savedPaise"] == -500_00
    assert totals["savedPctOfIncome"] == -50.0


async def test_nothing_coming_in_says_nothing_about_saving(
    client: AsyncClient, settings: Settings
) -> None:
    """A card statement on its own is not a month that overspent everything.

    With no credits in the window the only honest answer is that we cannot
    tell, and the screen draws a dash for it.
    """
    await _spend_on(settings, [("2026-08-10", 1_500_00)])

    totals = (await client.get("/v1/report?month=2026-08")).json()["totals"]
    assert totals["spentPaise"] == 1_500_00
    assert totals["savedPaise"] is None
    assert totals["savedPctOfIncome"] is None


async def test_uploading_a_file_we_lost_puts_it_back(
    client: AsyncClient, settings: Settings
) -> None:
    """A document row can outlive its object, and the re-read needs the file.

    The row is deduplicated on content, so a second upload used to add
    nothing — and if the object was gone, reading it again failed on a key
    pointing at nothing. Uploading it again is the user handing us the file.
    """
    from todd.worker.lib.storage import build_storage

    payload = {"files": ("lost.pdf", b"not a pdf, but stored", "application/pdf")}
    first = (await client.post("/v1/documents", files=payload)).json()
    await _drain(client, first["documents"])

    storage = build_storage(settings)
    user = "00000000-0000-4000-8000-000000000001"
    key = f"users/{user}/documents/{first['documents'][0]['id']}/lost.pdf"
    assert storage.exists(key)
    storage.delete_prefix(f"users/{user}/documents/{first['documents'][0]['id']}")
    assert not storage.exists(key)

    again = (await client.post("/v1/documents", files=payload)).json()
    assert again["duplicates"] == 1
    assert again["documents"][0]["id"] == first["documents"][0]["id"], "still one document"
    assert storage.exists(key), "the file is back"
