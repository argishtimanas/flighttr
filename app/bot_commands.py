"""Process route commands from the one configured Telegram chat."""
from __future__ import annotations

import os
import shlex
from datetime import date
from pathlib import Path

import yaml

from app.config import load_routes, route_from_dict
from app.notifications import send_alert, telegram_updates

HELP = ("Commands:\n"
        "/add ID ORIGIN DEST YYYY-MM-DD [RETURN-YYYY-MM-DD]\n"
        "/list\n/remove ID\n/check [ID|all]\n/help\n"
        "A route added without a return date is one-way. Alerts use price drops and history; no target price is required."
        " Commands are read on the next scheduled run.")


def _save_routes(path: Path, routes):
    data = {"routes": []}
    for route in routes:
        item = dict(route.__dict__)
        for key in ("departure_start", "departure_end", "return_start", "return_end"):
            item[key] = item[key].isoformat() if item[key] else None
        item["preferred_airlines"] = list(item["preferred_airlines"])
        data["routes"].append(item)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(yaml.safe_dump(data, sort_keys=False))
    temp.replace(path)


def handle_command(message: str, path: Path):
    """Return (reply, route IDs to check). Unknown text gets help."""
    try:
        args = shlex.split(message)
    except ValueError:
        return "Could not parse command.\n" + HELP, []
    if not args:
        return HELP, []
    command = args[0].lower().split("@", 1)[0]
    routes = load_routes(path)
    if command in ("/start", "/help"):
        return HELP, []
    if command == "/list":
        if len(args) != 1:
            return "Usage: /list", []
        if not routes:
            return "No routes yet.\n" + HELP, []
        return "Routes:\n" + "\n".join(
            f"{r.id}: {r.origin}→{r.destination} {r.departure_start}"
            + (f"–{r.return_start}" if r.return_start else " one-way")
            + (" (paused)" if not r.enabled else "") for r in routes), []
    if command == "/add":
        if len(args) not in (5, 6):
            return "Usage: /add ID ORIGIN DEST YYYY-MM-DD [RETURN-YYYY-MM-DD]", []
        _, route_id, origin, destination, departure, *return_dates = args
        try:
            if date.fromisoformat(departure) < date.today():
                raise ValueError("departure date must be today or later")
            raw = {"id": route_id, "origin": origin.upper(), "destination": destination.upper(),
                   "trip_type": "round_trip" if return_dates else "one_way",
                   "departure_start": departure, "departure_end": departure,
                   "return_start": return_dates[0] if return_dates else None,
                   "return_end": return_dates[0] if return_dates else None,
                   "max_price": None, "alerts_enabled": True, "enabled": True}
            new_route = route_from_dict(raw)
        except (ValueError, TypeError) as exc:
            return f"Invalid route: {exc}", []
        if any(r.id == new_route.id for r in routes):
            return "That ID already exists. Use another ID or /remove it first.", []
        _save_routes(path, routes + [new_route])
        return f"Added {new_route.id}. Checking it now if API credits remain.", [new_route.id]
    if command == "/remove":
        if len(args) != 2:
            return "Usage: /remove ID", []
        remaining = [r for r in routes if r.id != args[1]]
        if len(remaining) == len(routes):
            return "Route ID not found.", []
        _save_routes(path, remaining)
        return f"Removed {args[1]}.", []
    if command == "/check":
        if len(args) > 2:
            return "Usage: /check [ID|all]", []
        target = args[1] if len(args) == 2 else "all"
        ids = [r.id for r in routes if r.enabled and (target == "all" or r.id == target)]
        if not ids:
            return "No matching active route.", []
        return "Checking " + ", ".join(ids) + " if API credits remain.", ids
    return HELP, []


def process_updates(store, path: str | Path | None = None, *, session=None, send=send_alert):
    path = Path(path or os.getenv("ROUTES_FILE", "data/routes.yaml"))
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    requested = []
    for update in telegram_updates(store.bot_offset(), session=session):
        update_id = update["update_id"]
        message = update.get("message") or {}
        if str(message.get("chat", {}).get("id")) == chat_id and isinstance(message.get("text"), str):
            reply, ids = handle_command(message["text"], path)
            send(reply)
            requested.extend(ids)
        store.save_bot_offset(update_id + 1)
    return list(dict.fromkeys(requested))
