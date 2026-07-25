from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[dict], str]


def _get_current_time(args: dict) -> str:
    name = args.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"error: unknown timezone {name!r}; use an IANA name like 'Asia/Yangon'"
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


REGISTRY: dict[str, Tool] = {
    "get_current_time": Tool(
        name="get_current_time",
        description=(
            "Get the current date and time. Call this whenever the user asks "
            "what the time or date is — you have no other way to know it."
        ),
        schema={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "IANA timezone name, e.g. 'Asia/Yangon' or "
                        "'America/New_York'. Defaults to UTC."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        run=_get_current_time,
    ),
}
