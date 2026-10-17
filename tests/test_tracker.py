from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.config import route_from_dict
from app.database import Store
from app.deal_detector import detect, percent_drop, should_notify
from app.flight_api import FlightClient, FlightAPIError, normalize_results, search_params
from app.notifications import send_alert, send_channel_alert, telegram_chat_ids


def route(**changes):
    raw = {"id": "test-route", "origin": "SFO", "destination": "JFK", "trip_type": "one_way",
           "departure_start": "2027-02-12", "departure_end": "2027-02-14", "max_price": 250}
    raw.update(changes)
    return route_from_dict(raw)


def payload(price=218):
    return {"best_flights": [{"price": price, "total_duration": 330, "flights": [
        {"departure_airport": {"time": "2027-02-12 08:00"}, "arrival_airport": {"time": "2027-02-12 16:00"},
         "airline": "Delta", "flight_number": "DL 123", "duration": 330}]}]}


def test_normalize_filter_and_dates():
    r = route(max_stops=0)
    fares = normalize_results(payload(), r, date(2027, 2, 12), None)
    assert fares[0].price == 218 and fares[0].stops == 0 and fares[0].flight_numbers == "DL 123"
    assert len(list(r.combinations())) == 3
    assert normalize_results({}, r, date(2027, 2, 12), None) == []
    assert search_params(r, date(2027, 2, 12), None)["stops"] == 1
    with pytest.raises(ValueError):
        route(origin="S1O")


def test_roundtrip_combinations():
    r = route(trip_type="round_trip", return_start="2027-02-13", return_end="2027-02-14")
    assert len(list(r.combinations())) == 3


def test_history_and_rules(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'test.db'}")
    r = route()
    fare = normalize_results(payload(400), r, date(2027, 2, 12), None)[0]
    store.save_fares([fare], "first")
    current = normalize_results(payload(218), r, date(2027, 2, 12), None)[0]
    history = store.historical(current)
    reasons, metrics = detect(current, r, history)
    assert {"price threshold", "price drop", "historical low"} <= set(reasons)
    assert metrics["historical_low"] == 400 and percent_drop(400, 320) == 20
    assert "exceptional deal" not in reasons
    store.save_fares([current], "second")
    assert len(store.historical(current)) == 2
    assert store.last_itinerary_price(current) == 218
    assert store.reserve_api(1) and not store.reserve_api(1)
    assert store.usage_month() == 1
    assert store.usage_today() == 1


def test_deal_without_target_uses_history():
    r = route(max_price=None)
    fare = normalize_results(payload(280), r, date(2027, 2, 12), None)[0]
    now = datetime.now(timezone.utc)
    history = [{"price": 350, "checked_at": now - timedelta(days=2)},
               {"price": 340, "checked_at": now - timedelta(days=1)}]
    reasons, _ = detect(fare, r, history)
    assert "price drop" in reasons and "historical low" in reasons
    assert detect(fare, r, [])[0] == []


def test_deduplication():
    previous = {"price": 220, "created_at": datetime.now(timezone.utc) - timedelta(days=2)}
    assert not should_notify(previous, 220)
    assert not should_notify(previous, 215, meaningful_percent=5)
    assert should_notify(previous, 200, meaningful_percent=5)
    previous["created_at"] = datetime.now(timezone.utc)
    assert not should_notify(previous, 200, cooldown_hours=24)


def test_api_retry_and_missing():
    r = route()
    session = Mock()
    failure = Mock(status_code=429)
    success = Mock(status_code=200)
    success.json.return_value = payload()
    session.get.side_effect = [failure, success]
    client = FlightClient("fake", session=session, sleep=lambda _: None)
    assert len(client.search(r, date(2027, 2, 12), None, lambda: True)) == 1
    assert session.get.call_count == 2
    success.json.return_value = {}
    session.get.side_effect = None
    session.get.return_value = success
    assert client.search(r, date(2027, 2, 12), None, lambda: True) == []
    with pytest.raises(FlightAPIError):
        client.search(r, date(2027, 2, 12), None, lambda: False)


def test_serpapi_no_results_is_empty():
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {
        "error": "Google Flights hasn't returned any results for this query."
    }
    client = FlightClient("fake", session=session)
    assert client.search(route(), date(2027, 2, 12), None, lambda: True) == []


def test_telegram_mock(monkeypatch):
    for key, value in {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "12345"}.items():
        monkeypatch.setenv(key, value)
    session = Mock()
    session.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 777}}
    assert send_alert("test", session=session) == "777"
    session.post.assert_called_once()
    assert session.post.call_args.kwargs["data"]["chat_id"] == "12345"
    session.get.return_value.json.return_value = {"ok": True, "result": [{"message": {"chat": {"id": 12345}}}]}
    assert telegram_chat_ids(session=session) == ["12345"]
    monkeypatch.setenv("TELEGRAM_DEALS_CHANNEL_ID", "-1001234567890")
    assert send_channel_alert("deal", session=session) == "777"
    assert session.post.call_args.kwargs["data"]["chat_id"] == "-1001234567890"


def test_complete_check_with_mocked_services(tmp_path, monkeypatch):
    from app.main import check
    store = Store(f"sqlite:///{tmp_path / 'check.db'}")
    r = route(departure_start="2027-02-12", departure_end="2027-02-12")
    client = Mock()
    fare = normalize_results(payload(), r, date(2027, 2, 12), None)[0]
    client.search.return_value = [fare]
    sent = Mock(return_value="SMfake")
    channel = Mock(return_value="CHfake")
    monkeypatch.setattr("app.main.send_alert", sent)
    monkeypatch.setattr("app.main.send_channel_alert", channel)
    monkeypatch.setenv("TELEGRAM_DEALS_CHANNEL_ID", "-1001234567890")
    monkeypatch.chdir(tmp_path)
    result = check(routes=[r], store=store, client=client)
    assert result[0]["cheapest"] == 218
    assert store.usage_today() == 0  # mock client made no real API call
    assert store.alert_count_today() == 1
    assert sent.call_count == 1
    assert channel.call_count == 1
    check(routes=[r], store=store, client=client)
    assert sent.call_count == 1  # same itinerary and price is deduplicated
    assert channel.call_count == 1


def test_roundtrip_uses_departure_token():
    r = route(trip_type="round_trip", departure_end="2027-02-12", return_start="2027-02-19", return_end="2027-02-19")
    outward = payload(350)["best_flights"][0]
    outward["departure_token"] = "token"
    returning = payload(400)["best_flights"][0]
    returning["flights"][0]["flight_number"] = "DL 456"
    session = Mock()
    first = Mock(status_code=200)
    first.json.return_value = {"best_flights": [outward]}
    second = Mock(status_code=200)
    second.json.return_value = {"best_flights": [returning]}
    session.get.side_effect = [first, second]
    fare = FlightClient("fake", session=session).search(r, date(2027, 2, 12), date(2027, 2, 19), lambda: True)[0]
    assert fare.price == 400 and "DL 123" in fare.flight_numbers and "DL 456" in fare.flight_numbers
    assert session.get.call_args.kwargs["params"]["departure_token"] == "token"
