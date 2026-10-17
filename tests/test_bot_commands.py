from pathlib import Path
from unittest.mock import Mock
from datetime import datetime, timezone

import yaml

from app.bot_commands import handle_command, process_updates
from app.config import load_routes
from app.database import Store


def test_add_list_remove_routes_without_target(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": []}))
    reply, requested = handle_command("/add trip SFO JFK 2027-02-12 2027-02-19", path)
    assert requested == ["trip"] and "Added" in reply
    saved = load_routes(path)
    assert len(saved) == 1 and saved[0].max_price is None
    assert saved[0].trip_type == "round_trip"
    assert "trip" in handle_command("/list", path)[0]
    assert handle_command("/check trip", path)[1] == ["trip"]
    assert "already exists" in handle_command("/add trip SFO LAX 2027-02-12", path)[0]
    assert "Removed" in handle_command("/remove trip", path)[0]
    assert load_routes(path) == []


def test_private_channel_link_is_owner_setup_only(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": []}))
    reply, requested = handle_command("/channel_id https://t.me/c/1234567890/42", path)
    assert "-1001234567890" in reply and requested == []
    assert "Send a private channel post link" in handle_command("/channel_id https://example.com/c/123/42", path)[0]


def test_only_configured_chat_can_change_routes(tmp_path, monkeypatch):
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": []}))
    store = Store(f"sqlite:///{tmp_path / 'state.db'}")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    updates = [
        {"update_id": 4, "message": {"chat": {"id": 456}, "text": "/add evil SFO JFK 2027-02-12"}},
        {"update_id": 5, "message": {"chat": {"id": 123}, "text": "/add trip SFO JFK 2027-02-12"}},
    ]
    monkeypatch.setattr("app.bot_commands.telegram_updates", lambda offset, session=None: updates)
    sent = Mock()
    assert process_updates(store, path, send=sent) == ["trip"]
    assert [r.id for r in load_routes(path)] == ["trip"]
    assert store.bot_offset() == 6
    sent.assert_called_once()


def test_guest_gets_only_channel_directions(tmp_path, monkeypatch):
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": []}))
    store = Store(f"sqlite:///{tmp_path / 'state.db'}")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 456, "type": "private"}, "text": "/start"}},
        {"update_id": 2, "message": {"chat": {"id": 456, "type": "private"}, "text": "/add evil SFO JFK 2027-02-12"}},
        {"update_id": 3, "message": {"chat": {"id": 123, "type": "private"},
                                     "forward_origin": {"chat": {"id": -1001234567890, "type": "channel"}}}},
    ]
    monkeypatch.setattr("app.bot_commands.telegram_updates", lambda offset, session=None: updates)
    owner = Mock()
    guest = Mock()
    assert process_updates(store, path, send=owner, send_guest=guest) == []
    assert load_routes(path) == []
    assert store.bot_offset() == 4
    guest.assert_called_once()
    assert guest.call_args.args[1] == 456
    assert "-1001234567890" in owner.call_args.args[0]


def test_hourly_job_waits_when_daily_pace_used(monkeypatch):
    from app import main
    store = Mock()
    store.usage_today.return_value = 6
    monkeypatch.setattr(main, "Store", lambda: store)
    monkeypatch.setattr(main, "process_updates", lambda _: [])
    check = Mock()
    monkeypatch.setattr(main, "check", check)
    main.scheduled()
    check.assert_not_called()


def test_hourly_job_checks_oldest_route_with_available_credits(monkeypatch):
    from app import main
    from app.config import route_from_dict
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 17, 21, tzinfo=tz)
    monkeypatch.setattr(main, "datetime", FixedDateTime)
    store = Mock()
    store.usage_today.return_value = 0
    store.last_checked.side_effect = lambda route_id: None if route_id == "old" else datetime(2026, 10, 16)
    monkeypatch.setattr(main, "Store", lambda: store)
    monkeypatch.setattr(main, "process_updates", lambda _: [])
    routes = [route_from_dict({"id": route_id, "origin": "SFO", "destination": "JFK",
                               "trip_type": "one_way", "departure_start": "2027-02-12",
                               "departure_end": "2027-02-12"}) for route_id in ("recent", "old")]
    monkeypatch.setattr(main, "load_routes", lambda: routes)
    check = Mock()
    monkeypatch.setattr(main, "check", check)
    main.scheduled()
    assert check.call_args.kwargs["routes"] == [routes[1]]


def test_bot_requested_check_reports_fare(monkeypatch):
    from app import main
    from app.config import route_from_dict
    route = route_from_dict({"id": "trip", "origin": "SFO", "destination": "JFK",
                             "trip_type": "one_way", "departure_start": "2027-02-12",
                             "departure_end": "2027-02-12"})
    store = Mock()
    store.usage_month.return_value = 0
    store.usage_today.side_effect = [0, 1]
    monkeypatch.setattr(main, "Store", lambda: store)
    monkeypatch.setattr(main, "process_updates", lambda _: ["trip"])
    monkeypatch.setattr(main, "load_routes", lambda: [route])
    check = Mock(return_value=[{"route": "trip", "results": 2, "cheapest": 199.0}])
    send = Mock()
    monkeypatch.setattr(main, "check", check)
    monkeypatch.setattr(main, "send_alert", send)
    main.scheduled()
    assert check.call_args.kwargs["routes"] == [route]
    assert "199 cheapest" in send.call_args.args[0]
