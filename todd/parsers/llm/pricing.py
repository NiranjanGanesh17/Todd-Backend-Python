"""Per-model rates, and what one call cost.

Cost is recorded per call in `llm_usage` so a retry shows up as the extra
spend it is (§7.1 step 8).

§8.9: thinking tokens are billed as **output**. On a 153-row statement they
were ~44% of output — the largest single line item, and invisible unless
recorded. `output_tokens` from the API already includes them; the separate
thinking count is kept when the response reports one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: US dollars per million tokens. Keep in step with the published price list.
RATES: dict[str, tuple[float, float]] = {
    # model id: (input $/MTok, output $/MTok)
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

#: Haiku 5.5 is priced for prompts up to this size; beyond it the rate rises.
HAIKU_LONG_PROMPT_TOKENS = 100_000
HAIKU_LONG_PROMPT_RATES = (0.50, 2.50)

MICROS_PER_DOLLAR = 1_000_000


@dataclass(frozen=True, slots=True)
class Cost:
    model: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    micros: int

    @property
    def dollars(self) -> float:
        return self.micros / MICROS_PER_DOLLAR


def rates_for(model: str, input_tokens: int = 0) -> tuple[float, float]:
    if model.startswith("claude-haiku-5-5") and input_tokens > HAIKU_LONG_PROMPT_TOKENS:
        return HAIKU_LONG_PROMPT_RATES
    try:
        return RATES[model]
    except KeyError as exc:
        raise ValueError(f"no published rate for {model!r}; add it to RATES") from exc


def cost_of(*, model: str, input_tokens: int, output_tokens: int, thinking_tokens: int = 0) -> Cost:
    """Cost in integer micros — money stays an int here too (§8.2)."""
    input_rate, output_rate = rates_for(model, input_tokens)
    micros = round(
        (input_tokens * input_rate + output_tokens * output_rate) * MICROS_PER_DOLLAR / 1_000_000
    )
    return Cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        micros=micros,
    )
