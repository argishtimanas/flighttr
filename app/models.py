from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Fare:
    route_id: str
    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    trip_type: str
    cabin: str
    adults: int
    currency: str
    airline: str
    flight_numbers: str
    stops: int
    duration_minutes: int
    price: float
    departure_time: str
    arrival_time: str
    itinerary_key: str
    booking_link: str | None
    search_link: str
