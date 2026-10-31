"""Categorisation: a ladder, cheapest and most certain first (§9).

    1  own-account transfer                  conf 1.00
    2  bank charge pattern                   conf 1.00
    3  structural rule (ATM/salary/interest) conf 0.95
    4  user correction (user_rules)          conf 1.00
    5  merchant dictionary                   conf 0.90
    6  VPA merchant handle -> business       conf 0.60
    7  VPA plain name -> upi_to_people       conf 0.70
    8  LLM, distinct payees only, then cached — NOT WIRED

Which rung fired is recorded in `category_source`, so a wrong label can be
traced to the rule that produced it rather than guessed at.

Rung 8 is deliberately absent: it costs money per call and must not run
without the operator asking for it. Rung 6 therefore identifies a business
without naming its category — leaving it visibly uncategorised is honest,
and far better than the alternative of filing every shop under "UPI to
people", which is what happens with no rung 6 at all.

The labelling unit is the **payee, not the transaction**: 208 real
transactions were only 113 distinct payees, so a decision cached per payee
compounds across months.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from todd.config import Settings
from todd.db.seed.data import UNCATEGORIZED_SLUG
from todd.log import get_logger
from todd.parsers.payee import extract_payee, normalise_payee
from todd.parsers.vpa import Counterparty, Vpa, extract_vpa

logger = get_logger(__name__)

#: Rung 2 — what a bank charges you for having an account with it.
BANK_CHARGE = re.compile(
    r"\b(?:amb\s+charge|min(?:imum)?\s+bal|sms\s+(?:alert|charge)|annual\s+fee|"
    r"service\s+charge|penalty|chrg|chgs?|processing\s+fee|late\s+fee|"
    r"gst\s+on|cgst|sgst|igst|atm\s+decline|cheque\s+return)\b",
    re.IGNORECASE,
)

#: Rung 3 — structural movements every statement has.
#: `NFS` is the National Financial Switch — an ATM withdrawal at another
#: bank's machine, and it appears as `NFS/CASH` with no other clue.
CASH_WITHDRAWAL = re.compile(
    r"\b(?:atm|cash\s+wdl|cash\s+withdrawal|nwd|awb|nfs\s*/\s*cash|cash\s+at)\b",
    re.IGNORECASE,
)

#: An ACH or NACH debit is a standing mandate: an EMI, a SIP or a premium.
#: Never a shop. Which of the three it is comes from the merchant below, so
#: this only fires when nothing more specific did.
MANDATE = re.compile(
    r"\b(?:ach|nach|e-?mandate|si\s+debit|standing\s+instruction)\b", re.IGNORECASE
)
#: Payroll rarely spells out "salary". It arrives as an ACH reference whose
#: first segment is `SAL-` followed by the employer name, running straight on:
#: `ACH/SAL-ACCOLITEDIGIINDL/ADIPSALAPR268`. So `SAL` plus a separator is the
#: signal, with no closing word boundary — that boundary is what made the
#: first version of this miss two months of salary. `salon` still does not
#: match, because the separator is required.
SALARY = re.compile(
    r"(?:\bsalary\b|\bsal[-_/]|\bsal\s+cr\b|\bpayroll\b|\bwages\b|\bstipend\b)",
    re.IGNORECASE,
)
INTEREST = re.compile(r"\b(?:int\.?\s*(?:credit|pd)|interest\s+(?:credit|paid))\b", re.IGNORECASE)

#: Money going into an investment. Banks name the *rail* rather than the
#: fund — `UPI/Indian Cle/mutualfunds.el/…` is a mutual-fund SIP settling
#: through Indian Clearing Corporation, and the fund's own name never
#: appears. The handle fragment is the signal.
#:
#: Every token here is anchored: `\bsip\b` so "gossip" is not an investment,
#: `\bmf\b` so "comfort" is not, `\bnse\b`/`\bbse\b` so a reference number
#: containing those letters is not.
INVESTMENT = re.compile(
    r"(?:\bsip\b|\bmf\b|mutual\s*funds?|mutualfunds?|\bicclsip\b"
    r"|indian\s+clear|clearing\s+corp|\bnse\b|\bbse\b|\bdemat\b"
    r"|zerodha|groww|upstox|kuvera|smallcase|indmoney|paytm\s*money"
    r"|\bcams\b|kfintech|\bkarvy\b|\bamc\b|broking|securities)",
    re.IGNORECASE,
)

#: Rung 1 — moving money between your own accounts is not spending.
OWN_TRANSFER = re.compile(
    r"\b(?:credit\s+card\s+payment|card\s+payment|self\s+transfer|own\s+account|"
    r"transfer\s+to\s+self|to\s+own)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Label:
    category_slug: str | None
    source: str
    confidence: float
    merchant_id: uuid.UUID | None = None
    counterparty_vpa: str | None = None
    counterparty_name: str | None = None
    is_transfer: bool = False


@dataclass(slots=True)
class Context:
    """Everything the ladder needs, loaded once per run rather than per row."""

    category_ids: dict[str, uuid.UUID]
    #: alias -> (merchant_id, category_slug), longest alias first when matching.
    merchant_aliases: list[tuple[str, uuid.UUID, str]]
    #: (match_kind, match_value) -> category_slug
    user_rules: dict[tuple[str, str], str]
    #: Last-4s of the user's own accounts, for own-transfer detection.
    own_masked: set[str]
    #: normalised payee -> (category_slug, merchant_id, source, confidence).
    #: Resolved once per distinct payee, by trigram and then — if enabled —
    #: by the model. 399 unlabelled rows were only 118 distinct payees, so
    #: this is where the work actually compounds.
    payee_labels: dict[str, tuple[str | None, uuid.UUID | None, str, float]] = field(
        default_factory=dict
    )


async def load_context(session: AsyncSession) -> Context:
    categories = {
        row["slug"]: uuid.UUID(str(row["id"]))
        for row in (await session.execute(text("SELECT id, slug FROM categories"))).mappings().all()
    }

    aliases: list[tuple[str, uuid.UUID, str]] = []
    for row in (
        (
            await session.execute(
                text(
                    "SELECT m.id, m.aliases, c.slug FROM merchants m "
                    "JOIN categories c ON c.id = m.default_category_id"
                )
            )
        )
        .mappings()
        .all()
    ):
        for alias in row["aliases"] or []:
            aliases.append((alias.lower(), uuid.UUID(str(row["id"])), row["slug"]))
    # Longest alias first: "amazon prime" must beat "amazon".
    aliases.sort(key=lambda a: len(a[0]), reverse=True)

    rules = {
        (row["match_kind"], row["match_value"].lower()): row["slug"]
        for row in (
            await session.execute(
                text(
                    "SELECT r.match_kind::text AS match_kind, r.match_value, c.slug "
                    "FROM user_rules r JOIN categories c ON c.id = r.category_id"
                )
            )
        )
        .mappings()
        .all()
    }

    own = {
        row["masked_number"]
        for row in (
            await session.execute(
                text("SELECT masked_number FROM accounts WHERE masked_number IS NOT NULL")
            )
        )
        .mappings()
        .all()
    }

    return Context(
        category_ids=categories,
        merchant_aliases=aliases,
        user_rules=rules,
        own_masked=own,
        payee_labels={},
    )


def classify(
    *, description: str, direction: str, ctx: Context, existing_vpa: str | None = None
) -> Label:
    """Walk the ladder. The first rung that fires wins."""
    text_lower = (description or "").lower()
    vpa: Vpa | None = extract_vpa(description)
    address = (vpa.address if vpa else None) or existing_vpa

    # 1 — own-account transfer. Either the narration says so, or it names the
    #     last 4 of another account this user owns.
    mentions_own = any(
        re.search(rf"(?<!\d){re.escape(last4)}(?!\d)", description or "")
        for last4 in ctx.own_masked
    )
    if OWN_TRANSFER.search(text_lower) or mentions_own:
        return Label(
            "transfers_own", "own_transfer", 1.0, is_transfer=True, counterparty_vpa=address
        )

    # 2 — bank charges.
    if BANK_CHARGE.search(text_lower):
        return Label("bank_charges", "bank_charge", 1.0, counterparty_vpa=address)

    # 3 — structural.
    if CASH_WITHDRAWAL.search(text_lower):
        return Label("cash_withdrawal", "structural", 0.95, counterparty_vpa=address)
    if direction == "credit" and SALARY.search(text_lower):
        return Label("income_salary", "structural", 0.95, counterparty_vpa=address)
    if direction == "credit" and INTEREST.search(text_lower):
        return Label("income_other", "structural", 0.95, counterparty_vpa=address)

    # 4 — the user's own corrections outrank every automatic rule below.
    if address and ("vpa", address) in ctx.user_rules:
        return Label(ctx.user_rules[("vpa", address)], "user_rule", 1.0, counterparty_vpa=address)
    for (kind, value), slug in ctx.user_rules.items():
        if kind == "text" and value in text_lower:
            return Label(slug, "user_rule", 1.0, counterparty_vpa=address)

    # 5 — the merchant dictionary.
    for alias, merchant_id, slug in ctx.merchant_aliases:
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text_lower):
            return Label(
                slug, "merchant_dict", 0.9, merchant_id=merchant_id, counterparty_vpa=address
            )

    # 5.5 — the payee's printed name, resolved once per payee: by trigram
    #        against the dictionary, or by the model when that rung is on.
    payee = normalise_payee(extract_payee(description))
    if payee and payee in ctx.payee_labels:
        payee_slug, payee_merchant, payee_source, payee_conf = ctx.payee_labels[payee]
        if payee_slug:
            return Label(
                payee_slug,
                payee_source,
                payee_conf,
                merchant_id=payee_merchant,
                counterparty_vpa=address,
                counterparty_name=_titled(payee),
            )

    # 5.7 — money into an investment. Before the VPA rungs, because a SIP
    #       settles through a business handle and would otherwise be filed as
    #       an unnamed business and left uncategorised.
    if direction == "debit" and INVESTMENT.search(text_lower):
        return Label(
            "investments_sip",
            "investment",
            0.85,
            counterparty_vpa=address,
            counterparty_name=_titled(payee) if payee else None,
        )

    # 5.75 — a standing mandate is an EMI or a premium, never a shop.
    #
    #        Debits only. ACH carries payroll in the other direction, and
    #        without this guard `ACH/SAL-AC…` — an employer paying you — was
    #        booked as a loan repayment, which is wrong twice over: it loses
    #        the income and invents an expense.
    if direction == "debit" and MANDATE.search(text_lower):
        return Label("emis_loans", "mandate", 0.8, counterparty_vpa=address)

    # 6 — a business UPI handle. Identified, but not named: no category here
    #     is honest, and it stops a shop being filed under "UPI to people".
    if vpa and vpa.kind is Counterparty.BUSINESS:
        return Label(None, "vpa_business", 0.6, counterparty_vpa=address)

    # 7 — a person's UPI handle.
    if vpa and vpa.kind is Counterparty.PERSON:
        from todd.parsers.vpa import display_name_for

        return Label(
            "upi_to_people" if direction == "debit" else "income_other",
            "vpa_person",
            0.7,
            counterparty_vpa=address,
            counterparty_name=display_name_for(address),
        )

    # Money arriving that is not a transfer between your own accounts is
    # income by definition — the open question is only which kind. Saying
    # "uncategorised" here would leave the whole of "Came in" unexplained.
    if direction == "credit":
        return Label(
            "income_other",
            "income_fallback",
            0.4,
            counterparty_vpa=address,
            counterparty_name=_titled(payee) if payee else None,
        )

    # Nothing matched. The payee name still rides along: it is what the model
    #     rung (§9 rung 8) and the user-correction screen both work from.
    return Label(
        None,
        "unmatched",
        0.0,
        counterparty_vpa=address,
        counterparty_name=_titled(payee) if payee else None,
    )


def _titled(payee: str) -> str:
    """`bajaj finance` -> `Bajaj Finance`, for display."""
    return payee.title()


#: Trigram similarity above which a printed payee is the dictionary merchant.
#: Trigrams beat embeddings here: they catch `SWIGY` for `SWIGGY` and
#: `BAJAJ FINA` for `BAJAJ FINANCE`, which an embedding smooths over. The
#: threshold is deliberately high — a wrong merchant is worse than none.
TRIGRAM_THRESHOLD = 0.45


async def resolve_payees(
    session: AsyncSession, payees: set[str]
) -> dict[str, tuple[str | None, uuid.UUID | None, str, float]]:
    """Match each distinct payee against the merchant dictionary.

    One decision per payee, never per transaction. Uses `pg_trgm`, which the
    baseline migration already enables.
    """
    resolved: dict[str, tuple[str | None, uuid.UUID | None, str, float]] = {}

    for payee in payees:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT m.id, c.slug, MAX(similarity(alias, :payee)) AS score "
                        "FROM merchants m "
                        "JOIN categories c ON c.id = m.default_category_id, "
                        "     unnest(m.aliases) AS alias "
                        "WHERE similarity(alias, :payee) >= :threshold "
                        "GROUP BY m.id, c.slug "
                        "ORDER BY score DESC LIMIT 1"
                    ),
                    {"payee": payee, "threshold": TRIGRAM_THRESHOLD},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            resolved[payee] = (row["slug"], uuid.UUID(str(row["id"])), "payee_trigram", 0.85)

    logger.info("payees_resolved", distinct=len(payees), matched=len(resolved))
    return resolved


async def classify_with_model(
    session: AsyncSession,
    *,
    user_id: str,
    payees: set[str],
    settings: Settings,
    business_payees: set[str] | None = None,
) -> dict[str, tuple[str | None, uuid.UUID | None, str, float]]:
    """§9 rung 8 — the model, on distinct payees only, then cached.

    Three gates stand in front of the call, and all three must pass:

    1. `LLM_ENRICH_ENABLED`. Off by default; this rung is the only one that
       costs money.
    2. The day's spend against `LLM_DAILY_COST_CAP_MICROS`.
    3. The payee must not look like a private individual. §12 forbids sending
       names, and a model cannot categorise a stranger anyway — rung 7 has
       already labelled those `upi_to_people` locally.

    The answer is written to `merchants`, so a payee is decided once and every
    future month is free.
    """
    resolved: dict[str, tuple[str | None, uuid.UUID | None, str, float]] = {}
    if not settings.llm_enrich_enabled or not payees:
        return resolved

    spent = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(cost_micros), 0) FROM llm_usage "
                "WHERE user_id = :uid AND day = CURRENT_DATE"
            ),
            {"uid": uuid.UUID(user_id)},
        )
    ).scalar_one()
    cap = settings.llm_daily_cost_cap_micros
    if cap is not None and int(spent) >= cap:
        logger.warning("llm_daily_cap_reached", spent_micros=int(spent), cap_micros=cap)
        return resolved

    # Gate 3: never send a person.
    business = business_payees or set()
    sendable = sorted(p for p in payees if not _looks_personal(p, vpa_is_business=p in business))
    skipped = len(payees) - len(sendable)
    if skipped:
        logger.info("payees_withheld_as_personal", count=skipped)
    if not sendable:
        return resolved

    from todd.parsers.llm.model import AnthropicPayeeClassifier

    try:
        result = await AnthropicPayeeClassifier(settings).classify(sendable)
    except Exception as exc:
        logger.warning("llm_enrich_failed", error=str(exc)[:200])
        return resolved

    if result.cost is not None:
        await session.execute(
            text(
                "INSERT INTO llm_usage (user_id, day, model, prompt_id, input_tokens, "
                "  output_tokens, cost_micros) "
                "VALUES (:uid, CURRENT_DATE, :model, 'payee_classify', :inp, :out, :cost)"
            ),
            {
                "uid": uuid.UUID(user_id),
                "model": result.cost.model,
                "inp": result.cost.input_tokens,
                "out": result.cost.output_tokens,
                "cost": result.cost.micros,
            },
        )

    for name, (slug, confidence) in result.labels.items():
        resolved[name] = (slug, None, "llm", min(confidence, 0.5))

    # Record the ones it could not place, too. Without this a payee the model
    # has already called unreadable is re-sent, and re-paid for, on every run.
    for name in sendable:
        resolved.setdefault(name, (None, None, "llm_unknown", 0.0))

    return resolved


#: A payee that reads as a person's name rather than a trading name. Crude on
#: purpose: the cost of withholding a shop is one uncategorised row; the cost
#: of sending an individual is a privacy breach.
_BUSINESS_WORDS = frozenset(
    {
        # legal and structural
        "ltd", "limited", "pvt", "private", "llp", "inc", "corp", "company", "co",
        "and", "the",
        # retail
        "stores", "store", "mart", "supermarket", "bazaar", "traders", "trading",
        "enterprises", "enterprise", "agencies", "agency", "services", "solutions",
        "shop", "shoppe", "retail", "centre", "center", "point", "world", "hub",
        "collection", "collections", "emporium", "depot", "outlet",
        # eating and drinking
        "restaurant", "restaurants", "cafe", "coffee", "hotel", "hotels", "bakery",
        "bakers", "sweets", "tiffins", "tiffin", "mess", "darshini", "dhaba",
        "biryani", "shawarma", "kebab", "pizza", "burger", "juice", "chats",
        "foods", "food", "kitchen", "caterers", "catering", "chicken", "mutton",
        "meat", "fish", "bar", "brewery", "icecream",
        # health
        "medical", "medicals", "pharma", "pharmacy", "clinic", "hospital", "labs",
        "diagnostics", "dental", "opticals", "gym", "fitness",
        # money
        "finance", "finserv", "capital", "insurance", "bank", "nidhi", "chits",
        "investments", "securities", "broking",
        # other trades
        "technologies", "tech", "systems", "fuels", "petroleum", "filling",
        "motors", "automobiles", "auto", "tyres", "travels", "tours", "transport",
        "entertainment", "cinemas", "theatre", "studio", "salon", "spa",
        "academy", "institute", "school", "college", "classes", "tutorials",
        "builders", "constructions", "interiors", "furniture", "hardware",
        "electricals", "electronics", "mobiles", "communications", "telecom",
        "provision", "provisions", "kirana", "general", "fresh", "organic", "dairy",
    }
)  # fmt: skip


def _looks_personal(payee: str, *, vpa_is_business: bool = False) -> bool:
    """True unless there is positive evidence the payee is a business.

    **The default is to withhold.** An earlier version of this sent anything
    with three or more words, on the theory that a long name is a trading
    name. That is wrong for Indian names: `AKRAM MOHAMMED JAHANGEER` and
    `ELAVARASI CHINNASAMY` are people, and they were sent. §12 says no names,
    so the burden is on the evidence, not on the absence of it.

    Two things count as evidence, and nothing else does:

    * rung 6 read a merchant handle off the UPI address — a QR id or an
      aggregator prefix, which an individual's handle cannot carry;
    * the name contains a trade noun or a legal suffix (`BAKERY`, `TIFFINS`,
      `FINANCE`, `PVT LTD`).

    The cost of being wrong in this direction is one uncategorised row. The
    cost of being wrong in the other is sending a private person's name to a
    third party.
    """
    if vpa_is_business:
        return False
    return not any(word in _BUSINESS_WORDS for word in payee.split())


async def load_cached_payees(
    session: AsyncSession, payees: set[str]
) -> dict[str, tuple[str | None, uuid.UUID | None, str, float]]:
    """Payees this user has already had decided, from any earlier run."""
    if not payees:
        return {}
    rows = (
        (
            await session.execute(
                text(
                    "SELECT l.payee, c.slug, l.source, l.confidence "
                    "FROM user_payee_labels l "
                    "LEFT JOIN categories c ON c.id = l.category_id "
                    "WHERE l.payee = ANY(:payees)"
                ),
                {"payees": sorted(payees)},
            )
        )
        .mappings()
        .all()
    )
    cached: dict[str, tuple[str | None, uuid.UUID | None, str, float]] = {
        str(row["payee"]): (
            row["slug"],
            None,
            str(row["source"]),
            float(row["confidence"]),
        )
        for row in rows
    }
    if cached:
        logger.info("payees_from_cache", count=len(cached))
    return cached


async def save_payees(
    session: AsyncSession,
    *,
    user_id: str,
    labels: dict[str, tuple[str | None, uuid.UUID | None, str, float]],
    ctx: Context,
) -> None:
    """Persist one decision per payee. Idempotent."""
    for payee, (slug, _merchant, source, confidence) in labels.items():
        await session.execute(
            text(
                "INSERT INTO user_payee_labels (user_id, payee, category_id, source, "
                "  confidence) VALUES (:uid, :payee, :cid, :src, :conf) "
                "ON CONFLICT (user_id, payee) DO UPDATE SET "
                "  category_id = EXCLUDED.category_id, source = EXCLUDED.source, "
                "  confidence = EXCLUDED.confidence, updated_at = now()"
            ),
            {
                "uid": uuid.UUID(user_id),
                "payee": payee[:160],
                "cid": ctx.category_ids.get(slug or ""),
                "src": source,
                "conf": confidence,
            },
        )


async def enrich_user(
    session: AsyncSession,
    *,
    user_id: str,
    only_unlabelled: bool = True,
    settings: Settings | None = None,
) -> int:
    """Label this user's transactions. Returns how many changed.

    Idempotent: re-running produces the same labels, and a row the user has
    corrected is never overwritten by an automatic rung.
    """
    ctx = await load_context(session)
    # A correction the user made is never re-decided, by any rung or any
    # model run. Relying on the payee cache to preserve it is not enough: a
    # row with no extractable payee and no VPA would be silently overwritten
    # on the next full re-enrichment, which is the one thing that would make
    # sorting feel pointless.
    where = (
        "WHERE category_id IS NULL AND category_source IS NULL"
        if only_unlabelled
        else "WHERE category_source IS DISTINCT FROM 'user_rule'"
    )
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, raw_description, direction::text AS direction, "
                    f"counterparty_vpa FROM transactions {where}"
                )
            )
        )
        .mappings()
        .all()
    )

    # Pass one: every distinct payee, decided once. 399 unlabelled rows were
    # only 118 distinct payees, so this is the difference between 399 lookups
    # and 118 — and it is what makes the model rung affordable later.
    payees: set[str] = set()
    business_payees: set[str] = set()
    for row in rows:
        key = normalise_payee(extract_payee(row["raw_description"] or ""))
        if not key:
            continue
        payees.add(key)
        # Rung 6 already decided this from the UPI handle, which a merchant QR
        # cannot fake. It is the strongest signal the privacy gate has.
        found = extract_vpa(row["raw_description"] or "")
        if found is not None and found.kind is Counterparty.BUSINESS:
            business_payees.add(key)
    # The cache first: a payee decided in an earlier run is never re-decided,
    # and in particular is never re-sent to the model.
    ctx.payee_labels = await load_cached_payees(session, payees)
    ctx.payee_labels |= await resolve_payees(session, payees - set(ctx.payee_labels))

    # Rung 8 sees only what the free rungs could not place.
    if settings is not None:
        unresolved = payees - set(ctx.payee_labels)
        fresh = await classify_with_model(
            session,
            user_id=user_id,
            payees=unresolved,
            settings=settings,
            business_payees=business_payees,
        )
        ctx.payee_labels |= fresh

    # Write back whatever this run worked out, so the next one is free. The
    # model's own "this is a person" verdicts are cached too, which is what
    # stops those names being sent a second time.
    await save_payees(session, user_id=user_id, labels=ctx.payee_labels, ctx=ctx)
    updated = 0
    for row in rows:
        label = classify(
            description=row["raw_description"] or "",
            direction=row["direction"],
            ctx=ctx,
            existing_vpa=row["counterparty_vpa"],
        )
        category_id = ctx.category_ids.get(label.category_slug or "", None)
        await session.execute(
            text(
                # counterparty_name is derived here and nowhere else, so it is
                # assigned rather than coalesced: a re-run must be able to
                # clear a name a previous, worse rule invented.
                "UPDATE transactions SET category_id = :cid, category_confidence = :conf, "
                "  category_source = :src, merchant_id = COALESCE(:mid, merchant_id), "
                "  counterparty_vpa = COALESCE(:vpa, counterparty_vpa), "
                "  counterparty_name = :cname, "
                "  is_transfer = :transfer, updated_at = now() "
                "WHERE id = :id"
            ),
            {
                "id": row["id"],
                "cid": category_id,
                "conf": label.confidence,
                "src": label.source,
                "mid": label.merchant_id,
                "vpa": label.counterparty_vpa,
                "cname": label.counterparty_name,
                "transfer": label.is_transfer,
            },
        )
        updated += 1

    logger.info("enriched", user_id=user_id, transactions=updated)
    return updated


__all__ = ["UNCATEGORIZED_SLUG", "Context", "Label", "classify", "enrich_user", "load_context"]
