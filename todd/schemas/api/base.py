"""The wire convention: camelCase out, snake_case in Python.

The web app's Zod contracts (`Todd-Frontend/contracts/`) are camelCase, and it
parses every response through them — a snake_case key is a hard failure on the
client, not a cosmetic difference. Declaring the alias generator once here is
what keeps the generated `openapi.json` shape-compatible with the Node one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        ser_json_timedelta="float",
    )
