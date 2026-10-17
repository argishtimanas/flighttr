"""Deal rules compare only exact route, date, cabin, passenger, trip and currency peers."""
from datetime import datetime, timedelta, timezone
from statistics import mean, median


def percent_drop(previous, current):
    return max(0.0, (previous - current) / previous * 100) if previous and previous > 0 else 0.0


def metrics(history, current):
    prices = [float(x["price"]) for x in history]
    now = datetime.now(timezone.utc)
    def recent(days):
        vals = [float(x["price"]) for x in history if
                (x["checked_at"].replace(tzinfo=timezone.utc) if x["checked_at"].tzinfo is None else x["checked_at"].astimezone(timezone.utc)) >= now - timedelta(days=days)]
        return mean(vals) if vals else None
    return {"current_lowest": current, "observations": len(prices), "historical_low": min(prices) if prices else None,
            "average": mean(prices) if prices else None, "median": median(prices) if prices else None,
            "last_price": prices[-1] if prices else None, "change_since_last": current - prices[-1] if prices else None,
            "drop_percent": percent_drop(prices[-1], current) if prices else 0,
            "rolling_7d": recent(7), "rolling_30d": recent(30)}


def detect(fare, route, history, *, min_observations=5, unusual_discount=25, drop_threshold=15):
    m = metrics(history, fare.price)
    reasons = []
    if route.max_price is not None and fare.price <= route.max_price:
        reasons.append("price threshold")
    if m["last_price"] is not None and m["drop_percent"] >= drop_threshold:
        reasons.append("price drop")
    if m["historical_low"] is not None and fare.price < m["historical_low"]:
        reasons.append("historical low")
    if m["observations"] >= min_observations and percent_drop(m["median"], fare.price) >= unusual_discount:
        reasons.append("exceptional deal")
    return reasons, m


def should_notify(last_alert, price, *, cooldown_hours=24, meaningful_percent=5, now=None):
    if not last_alert:
        return True
    if price >= last_alert["price"]:
        return False
    if percent_drop(last_alert["price"], price) < meaningful_percent:
        return False
    now = now or datetime.now(timezone.utc)
    previous = last_alert["created_at"]
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return now - previous >= timedelta(hours=cooldown_hours)
