"""SerpApi Google Flights client. One query per exact date combination."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from urllib.parse import urlencode

import requests

from app.config import Route
from app.models import Fare

ENDPOINT = "https://serpapi.com/search.json"


class FlightAPIError(RuntimeError):
    pass


def search_link(route: Route, outbound, inbound):
    # Google Flights search URL uses only route and dates; it is not a booking link.
    query = f"Flights from {route.origin} to {route.destination} on {outbound.isoformat()}"
    if inbound:
        query += f" returning {inbound.isoformat()}"
    return f"https://www.google.com/travel/flights?{urlencode({'q': query, 'curr': route.currency})}"


def search_params(route: Route, outbound, inbound):
    params = {
        "engine": "google_flights", "departure_id": route.origin,
        "arrival_id": route.destination, "outbound_date": outbound.isoformat(),
        "type": 1 if inbound else 2,
        "travel_class": {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}[route.cabin],
        "adults": route.adults, "currency": route.currency, "sort_by": 2,
    }
    if inbound:
        params["return_date"] = inbound.isoformat()
    if route.max_stops is not None:
        params["stops"] = route.max_stops + 1
    if route.preferred_airlines:
        params["include_airlines"] = ",".join(route.preferred_airlines)
    if route.max_duration_minutes:
        params["max_duration"] = route.max_duration_minutes
    # Do not send max_price: a fare above the alert target is needed for history.
    return params


def normalize_results(payload: dict, route: Route, outbound, inbound) -> list[Fare]:
    result = []
    for item in (payload.get("best_flights") or []) + (payload.get("other_flights") or []):
        legs = item.get("flights") or []
        if not legs or not isinstance(item.get("price"), (int, float)) or item["price"] <= 0:
            continue
        stops = len(item.get("layovers") or []) if "layovers" in item else max(0, len(legs) - 1)
        duration = item.get("total_duration") or sum(int(x.get("duration") or 0) for x in legs)
        if route.max_stops is not None and stops > route.max_stops:
            continue
        if route.max_duration_minutes and duration > route.max_duration_minutes:
            continue
        numbers = ", ".join(x.get("flight_number", "") for x in legs if x.get("flight_number"))
        airline = ", ".join(dict.fromkeys(x.get("airline", "Unknown") for x in legs))
        if route.preferred_airlines and numbers:
            if any(n.replace(" ", "")[:2] not in route.preferred_airlines for n in numbers.split(", ")):
                continue
        first, last = legs[0], legs[-1]
        identity = [route.origin, route.destination, outbound.isoformat(), inbound.isoformat() if inbound else "", numbers,
                    first.get("departure_airport", {}).get("time", ""), last.get("arrival_airport", {}).get("time", "")]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
        result.append(Fare(route.id, route.origin, route.destination, outbound, inbound, route.trip_type,
                           route.cabin, route.adults, route.currency, airline, numbers, stops, int(duration),
                           float(item["price"]), first.get("departure_airport", {}).get("time", ""),
                           last.get("arrival_airport", {}).get("time", ""), key,
                           item.get("booking_link") if str(item.get("booking_link", "")).startswith("https://") else None,
                           search_link(route, outbound, inbound)))
    return sorted(result, key=lambda x: x.price)


class FlightClient:
    def __init__(self, api_key: str, session=None, sleep=time.sleep):
        if not api_key:
            raise ValueError("SERPAPI_API_KEY is required")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.sleep = sleep

    def _request(self, params, reserve_request):
        params = {**params, "api_key": self.api_key}
        for attempt in range(3):
            if not reserve_request():
                raise FlightAPIError("daily or run API request budget exhausted")
            try:
                response = self.session.get(ENDPOINT, params=params, timeout=30)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"SerpApi HTTP {response.status_code}")
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    raise FlightAPIError("SerpApi returned invalid JSON") from exc
                if data.get("error"):
                    raise FlightAPIError(str(data["error"]).replace(self.api_key, "[redacted]"))
                return data
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                if attempt == 2 or (isinstance(exc, requests.HTTPError) and response.status_code not in (429, 500, 502, 503, 504)):
                    raise FlightAPIError(str(exc).replace(self.api_key, "[redacted]")) from exc
                self.sleep(2 ** attempt)
        return {}

    def search(self, route: Route, outbound, inbound, reserve_request):
        params = search_params(route, outbound, inbound)
        data = self._request(params, reserve_request)
        if not inbound:
            return normalize_results(data, route, outbound, None)
        # Initial round-trip results show outbound legs. Select a departure_token
        # to get actual return choices and prices for a complete itinerary.
        outbound_items = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        outbound_limit = int(os.getenv("MAX_OUTBOUND_OPTIONS", "2"))
        if outbound_limit < 1:
            raise ValueError("MAX_OUTBOUND_OPTIONS must be positive")
        outbound_items = sorted(outbound_items, key=lambda x: x.get("price", float("inf")))[:outbound_limit]
        completed = []
        for item in outbound_items:
            token = item.get("departure_token")
            if not token:
                continue
            try:
                return_data = self._request({**params, "departure_token": token}, reserve_request)
            except FlightAPIError as exc:
                if "budget exhausted" in str(exc):
                    break
                continue
            outbound_fares = normalize_results({"best_flights": [item]}, route, outbound, inbound)
            if not outbound_fares:
                continue
            outward = outbound_fares[0]
            for coming_back in normalize_results(return_data, route, outbound, inbound):
                combined_key = hashlib.sha256(f"{outward.itinerary_key}:{coming_back.itinerary_key}".encode()).hexdigest()[:32]
                completed.append(replace(coming_back,
                    airline=outward.airline + " / " + coming_back.airline,
                    flight_numbers=outward.flight_numbers + " / " + coming_back.flight_numbers,
                    stops=max(outward.stops, coming_back.stops),
                    duration_minutes=outward.duration_minutes + coming_back.duration_minutes,
                    departure_time=outward.departure_time,
                    itinerary_key=combined_key))
        return sorted(completed, key=lambda x: x.price)
