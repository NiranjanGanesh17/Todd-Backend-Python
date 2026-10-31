"""The ONLY module that imports an LLM SDK.

Keeping this seam is what made swapping providers a 40-line job in the
TypeScript version. Everything above talks to `PayeeClassifier`.

What this asks the model is deliberately small: given a list of merchant
names, return a category slug for each. It never sees an amount, a date, an
account, or a person's name — rung 8 runs only on payees the VPA classifier
did not identify as individuals, and §12 forbids sending names regardless.

The model never calculates money (§7.4). It returns a slug and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import anthropic
from pydantic import BaseModel, Field

from todd.config import Settings
from todd.log import get_logger
from todd.parsers.llm.pricing import Cost, cost_of
from todd.parsers.llm.redact import redact_payee

logger = get_logger(__name__)

#: Batched so one call decides many payees. 118 distinct names fit comfortably;
#: this caps a runaway list rather than the usual case.
MAX_PAYEES_PER_CALL = 150

SYSTEM = """You categorise Indian bank-statement payees.

You will be given merchant or payee names exactly as a bank printed them. They \
are often truncated, abbreviated, or missing spaces.

For each name, reply with the single best category slug from this list:

  rent_housing      rent, maintenance, brokerage
  groceries         supermarkets, kirana, meat, milk, produce, quick-commerce
  food_delivery     Swiggy, Zomato and similar delivery platforms
  dining_out        restaurants, cafes, bakeries, street food, bars
  shopping          retail, e-commerce, clothing, electronics, entertainment
  travel_fuel       fuel, cabs, trains, flights, hotels, tolls, parking
  utilities_bills   electricity, water, gas, mobile, broadband, DTH
  subscriptions     streaming, software, memberships
  emis_loans        loan repayments, EMIs, credit-card bills, lenders
  investments_sip   broking, mutual funds, SIPs
  insurance         life, health, motor premiums
  health            pharmacy, clinics, hospitals, diagnostics, gyms
  education         schools, colleges, tuition, courses
  bank_charges      bank fees and penalties
  cash_withdrawal   ATM withdrawals
  upi_to_people     an individual person, not a business

Rules:
- Use `unknown` when the name is a meaningless machine identifier, or when you \
genuinely cannot tell. A wrong category is worse than `unknown`.
- Indian context matters: "tiffins", "darshini", "mess" and "chats" are eating \
places; "kirana" and "provision" are groceries; "finserv" and "finance" are \
lenders.
- Judge only the name. You have no other information and must not invent any."""


class PayeeLabel(BaseModel):
    name: str = Field(description="The payee name exactly as given")
    category: str = Field(description="A slug from the list, or 'unknown'")
    confidence: float = Field(ge=0.0, le=1.0, description="0 to 1")


class PayeeLabels(BaseModel):
    labels: list[PayeeLabel]


@dataclass(slots=True)
class ClassificationResult:
    labels: dict[str, tuple[str, float]] = field(default_factory=dict)
    cost: Cost | None = None
    truncated: bool = False


class PayeeClassifier(Protocol):
    async def classify(self, payees: list[str]) -> ClassificationResult: ...


class AnthropicPayeeClassifier:
    """Rung 8. One call for many payees; the caller caches the answers."""

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model_fast or "claude-haiku-5-5"

    async def classify(self, payees: list[str]) -> ClassificationResult:
        batch = [redact_payee(p) for p in payees[:MAX_PAYEES_PER_CALL]]
        batch = [p for p in batch if p]
        if not batch:
            return ClassificationResult()

        listing = "\n".join(f"- {name}" for name in batch)

        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=8000,
            system=SYSTEM,
            # A classification needs no deliberation, and thinking bills as
            # output (§8.9). Low effort is the whole point of this rung.
            output_config={"effort": "low"},
            output_format=PayeeLabels,
            messages=[
                {
                    "role": "user",
                    "content": f"Categorise each of these payees:\n\n{listing}",
                }
            ],
        )

        # §8.8 — a truncated reply surfaces as a schema error about a missing
        # field and sends you hunting for a bug that does not exist. Check it.
        truncated = response.stop_reason == "max_tokens"
        if truncated:
            logger.warning("payee_classify_truncated", model=self._model, payees=len(batch))

        usage = response.usage
        cost = cost_of(
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            thinking_tokens=getattr(
                getattr(usage, "output_tokens_details", None), "thinking_tokens", 0
            )
            or 0,
        )

        parsed = response.parsed_output
        labels: dict[str, tuple[str, float]] = {}
        if parsed is not None:
            for item in parsed.labels:
                if item.category and item.category != "unknown":
                    labels[item.name.strip().lower()] = (item.category, item.confidence)

        logger.info(
            "payees_classified",
            model=self._model,
            asked=len(batch),
            labelled=len(labels),
            cost_micros=cost.micros,
        )
        return ClassificationResult(labels=labels, cost=cost, truncated=truncated)
