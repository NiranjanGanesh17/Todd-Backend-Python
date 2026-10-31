"""Re-label a user's transactions through the categorisation ladder (§9).

Rungs 1-7 are free. Rung 8 calls the model, and only runs when
`LLM_ENRICH_ENABLED` is true and the day's spend is under the cap.

    uv run python scripts/enrich.py            # only unlabelled rows
    uv run python scripts/enrich.py --all      # re-decide every row
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from todd.config import get_settings
from todd.db.session import (
    dispose_engine,
    get_sessionmaker,
    init_engine,
    with_user,
)
from todd.worker.agents.enrich import enrich_user


async def main() -> None:
    settings = get_settings()
    only_unlabelled = "--all" not in sys.argv
    init_engine(settings)

    async with get_sessionmaker()() as session:
        async with session.begin(), with_user(session, settings.auth_dev_user_id) as scoped:
            await enrich_user(
                scoped,
                user_id=settings.auth_dev_user_id,
                only_unlabelled=only_unlabelled,
                settings=settings,
            )

        async with session.begin(), with_user(session, settings.auth_dev_user_id) as scoped:
            spend = (
                (
                    await scoped.execute(
                        text(
                            "SELECT model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out, "
                            "SUM(cost_micros) AS micros, count(*) AS calls FROM llm_usage "
                            "WHERE day = CURRENT_DATE GROUP BY model"
                        )
                    )
                )
                .mappings()
                .all()
            )

    for row in spend:
        print(
            f"  {row['model']}: {row['calls']} call(s), "
            f"{row['inp']} in / {row['out']} out tokens, "
            f"{row['micros']} micros (${row['micros'] / 1_000_000:.5f})"
        )
    if not spend:
        print("  no model calls today")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
