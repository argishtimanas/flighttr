"""CLI: python -m app.main check|test-alert|telegram-chat-id|demo."""
from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.config import load_routes, positive_env
from app.database import Store
from app.deal_detector import detect, should_notify
from app.flight_api import FlightClient, FlightAPIError, normalize_results
from app.notifications import format_alert, send_alert, send_channel_alert, telegram_chat_ids
from app.bot_commands import process_updates

logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)


def event(name, **fields):
    log.info(json.dumps({"event": name, **fields}, default=str))


@contextmanager
def run_lock(store):
    if store.engine.dialect.name == "postgresql":
        conn = store.engine.connect()
        acquired = conn.execute(text("SELECT pg_try_advisory_lock(412765182)")).scalar()
        try:
            if not acquired:
                raise RuntimeError("a check is already running")
            yield
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(412765182)"))
            conn.close()
    else:
        import fcntl
        lock_path = Path("data/tracker.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("a check is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def check(routes=None, store=None, client=None, request_limit=None):
    routes = routes if routes is not None else load_routes()
    store = store or Store()
    if not any(route.enabled for route in routes):
        event("check_complete", requests=0, routes=[], message="no active routes")
        return []
    client = client or FlightClient(os.getenv("SERPAPI_API_KEY", ""))
    daily_limit = positive_env("DAILY_API_LIMIT", 100)
    monthly_limit = positive_env("MONTHLY_API_LIMIT", 200)
    run_limit = positive_env("MAX_REQUESTS_PER_RUN", 20)
    if request_limit is not None:
        run_limit = min(run_limit, request_limit)
    alert_limit = positive_env("DAILY_ALERT_LIMIT", 5)
    cooldown = positive_env("ALERT_COOLDOWN_HOURS", 24)
    min_history = positive_env("MIN_HISTORY_OBSERVATIONS", 5)
    unusual = positive_env("UNUSUAL_DISCOUNT_PERCENT", 25)
    meaningful = positive_env("MIN_ALERT_PRICE_CHANGE_PERCENT", 5)
    used = 0
    summary = []

    def reserve():
        nonlocal used
        if used >= run_limit or not store.reserve_api(daily_limit, monthly_limit):
            return False
        used += 1
        return True

    with run_lock(store):
        for route in routes:
            if not route.enabled:
                continue
            fares = []
            failed = 0
            for outbound, inbound in route.combinations():
                if used >= run_limit:
                    break
                if outbound < date.today() or (inbound and inbound <= outbound):
                    event("invalid_dates", route=route.id, departure=outbound)
                    continue
                try:
                    fares.extend(client.search(route, outbound, inbound, reserve))
                except FlightAPIError as exc:
                    failed += 1
                    event("api_error", route=route.id, error=str(exc))
                    if "budget exhausted" in str(exc):
                        break
                    continue
            if not fares:
                store.record_check(route.id, "error" if failed else "empty", error=f"{failed} failed queries" if failed else None)
                summary.append({"route": route.id, "results": 0, "failed_queries": failed})
                continue
            check_id = str(uuid.uuid4())
            # Evaluate against prior checks before inserting this batch.
            candidates = sorted(fares, key=lambda f: f.price)
            cheapest_by_dates = {}
            for fare in candidates:
                cheapest_by_dates.setdefault((fare.departure_date, fare.return_date), fare)
            alerts_to_send = []
            for fare in cheapest_by_dates.values():
                history = store.historical(fare)
                reasons, stats = detect(fare, route, history, min_observations=min_history, unusual_discount=unusual)
                if not reasons or not route.alerts_enabled:
                    continue
                last = store.recent_alert(fare)
                if should_notify(last, fare.price, cooldown_hours=cooldown, meaningful_percent=meaningful):
                    alerts_to_send.append((fare, reasons, stats))
            store.save_fares(fares, check_id)
            store.record_check(route.id, "partial" if failed else "success", len(fares), f"{failed} failed queries" if failed else None)
            for fare, reasons, stats in alerts_to_send:
                if store.alert_count_today() >= alert_limit:
                    event("alert_limit", route=route.id)
                    break
                try:
                    body = format_alert(fare, reasons, stats)
                    message_id = send_alert(body)
                    store.record_alert(fare, reasons, "sent", message_id)
                    event("alert_sent", route=route.id, price=fare.price, reasons=reasons)
                    if os.getenv("TELEGRAM_DEALS_CHANNEL_ID"):
                        try:
                            send_channel_alert(body)
                            event("channel_alert_sent", route=route.id)
                        except Exception as exc:
                            event("channel_alert_error", route=route.id, error=type(exc).__name__)
                except Exception as exc:
                    store.record_alert(fare, reasons, "failed")
                    event("alert_error", route=route.id, error=type(exc).__name__)
            summary.append({"route": route.id, "results": len(fares), "failed_queries": failed,
                            "cheapest": candidates[0].price, "top_5_dates": [
                                {"departure": f.departure_date.isoformat(), "return": f.return_date.isoformat() if f.return_date else None,
                                 "price": f.price} for f in sorted({(f.departure_date, f.return_date): f for f in reversed(candidates)}.values(), key=lambda f: f.price)[:5]]})
    event("check_complete", requests=used, routes=summary)
    return summary


def scheduled():
    """Poll commands hourly and spread automatic searches across the free month."""
    store = Store()
    requested = process_updates(store)
    routes = load_routes()
    if requested:
        selected = [r for r in routes if r.enabled and r.id in requested]
        if store.usage_month() >= positive_env("MONTHLY_API_LIMIT", 200):
            send_alert("Monthly flight-search cap reached. Checks resume next UTC month.")
            return
        before = store.usage_today()
        summary = check(routes=selected, store=store)
        lines = [f"{item['route']}: " + (f"{item['cheapest']:.0f} cheapest" if item['results'] else "no fares found")
                 for item in summary]
        send_alert("Check complete (" + str(store.usage_today() - before) + " API requests).\n" + "\n".join(lines))
        return

    # Six requests per UTC day is at most 186 in a 31-day month. The monthly
    # app cap remains 200, leaving some room for manual bot checks.
    now = datetime.now(timezone.utc)
    allowance = 6 * (now.hour + 1) // 24
    remaining = max(0, allowance - store.usage_today())
    if remaining == 0:
        event("scheduled_wait", reason="daily pacing")
        return
    max_outbound = positive_env("MAX_OUTBOUND_OPTIONS", 1)
    eligible = []
    for route in routes:
        if not route.enabled:
            continue
        dates = [(outbound, inbound) for outbound, inbound in route.combinations() if outbound >= now.date()]
        estimated = len(dates) * (1 + (max_outbound if route.trip_type == "round_trip" else 0))
        if dates and estimated <= remaining:
            eligible.append(route)
    if not eligible:
        event("scheduled_wait", reason="insufficient paced credits or no current routes")
        return
    selected = min(eligible, key=lambda route: store.last_checked(route.id) or datetime.min)
    check(routes=[selected], store=store, request_limit=remaining)


def demo():
    from app.config import route_from_dict
    route = route_from_dict({"id": "demo-sfo-jfk", "origin": "SFO", "destination": "JFK", "trip_type": "one_way",
                             "departure_start": "2027-02-12", "departure_end": "2027-02-12"})
    payload = json.loads(Path("data/sample_flights.json").read_text())
    fares = normalize_results(payload, route, route.departure_start, None)
    print(json.dumps([f.__dict__ for f in fares], default=str, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Flight Deal Tracker")
    parser.add_argument("command", choices=["check", "scheduled", "test-alert", "test-channel", "telegram-chat-id", "demo"])
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "scheduled":
        scheduled()
    elif args.command == "test-alert":
        message_id = send_alert("Flight Deal Tracker test alert. Notifications are working.")
        print(f"Test alert accepted by Telegram: {message_id}")
    elif args.command == "test-channel":
        message_id = send_channel_alert("Flight Deal Tracker channel test. Future deal alerts will appear here.")
        print(f"Channel test accepted by Telegram: {message_id}")
    elif args.command == "telegram-chat-id":
        print("Chat IDs:", ", ".join(telegram_chat_ids()) or "none; send /start to your bot first")
    else:
        demo()


if __name__ == "__main__":
    main()
