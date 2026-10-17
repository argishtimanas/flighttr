"""Validated route and runtime configuration."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Route:
    id: str
    origin: str
    destination: str
    trip_type: str
    departure_start: date
    departure_end: date
    return_start: date | None = None
    return_end: date | None = None
    cabin: str = "economy"
    adults: int = 1
    currency: str = "USD"
    max_price: float | None = None
    preferred_airlines: tuple[str, ...] = ()
    max_stops: int | None = None
    max_duration_minutes: int | None = None
    alerts_enabled: bool = True
    enabled: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", self.id):
            raise ValueError("route id must be 2-64 alphanumeric/underscore/hyphen characters")
        if not all(re.fullmatch(r"[A-Z]{3}", x) for x in (self.origin, self.destination)) or self.origin == self.destination:
            raise ValueError("origin and destination must be distinct 3-letter IATA codes")
        if self.trip_type not in {"one_way", "round_trip"}:
            raise ValueError("trip_type must be one_way or round_trip")
        if self.cabin not in {"economy", "premium_economy", "business", "first"}:
            raise ValueError("invalid cabin")
        if not 1 <= self.adults <= 9 or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("adults must be 1-9 and currency must be a 3-letter code")
        if self.departure_end < self.departure_start or self.departure_end - self.departure_start > timedelta(days=31):
            raise ValueError("departure range must span 0-31 days")
        if self.trip_type == "round_trip":
            if not self.return_start or not self.return_end or self.return_end < self.return_start:
                raise ValueError("round trip requires a valid return range")
            if self.return_end - self.return_start > timedelta(days=31) or self.return_end <= self.departure_start:
                raise ValueError("return range must span 0-31 days and end after departure")
        elif self.return_start or self.return_end:
            raise ValueError("one-way trips cannot have return dates")
        if self.max_price is not None and self.max_price <= 0:
            raise ValueError("max_price must be positive")
        if self.max_stops not in (None, 0, 1, 2):
            raise ValueError("max_stops must be null, 0, 1, or 2")
        if self.max_duration_minutes is not None and self.max_duration_minutes <= 0:
            raise ValueError("max_duration_minutes must be positive")
        if any(not re.fullmatch(r"[A-Z0-9]{2}", x) for x in self.preferred_airlines):
            raise ValueError("preferred_airlines must contain 2-character IATA codes")

    def combinations(self):
        outbound = self.departure_start
        while outbound <= self.departure_end:
            if self.trip_type == "one_way":
                yield outbound, None
            else:
                inbound = self.return_start
                while inbound <= self.return_end:
                    if inbound > outbound:
                        yield outbound, inbound
                    inbound += timedelta(days=1)
            outbound += timedelta(days=1)


def route_from_dict(raw: dict) -> Route:
    allowed = set(Route.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown route fields: {sorted(unknown)}")
    data = dict(raw)
    for key in ("departure_start", "departure_end", "return_start", "return_end"):
        if data.get(key) is not None:
            data[key] = date.fromisoformat(str(data[key]))
    data["preferred_airlines"] = tuple(data.get("preferred_airlines") or ())
    return Route(**data)


def load_routes(path: str | Path | None = None) -> list[Route]:
    path = Path(path or os.getenv("ROUTES_FILE", "data/routes.yaml"))
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("routes"), list):
        raise ValueError("routes file must contain a routes list")
    routes = [route_from_dict(x) for x in raw["routes"]]
    if len({r.id for r in routes}) != len(routes):
        raise ValueError("route ids must be unique")
    return routes


def positive_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value
