"""Process route commands from the one configured Telegram chat."""
from __future__ import annotations

import os
import re
import shlex
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from app.config import load_routes, route_from_dict
from app.notifications import send_alert, send_message, telegram_updates

HELP = ("Commands:\n"
        "/add ID ORIGIN DEST YYYY-MM-DD [RETURN-YYYY-MM-DD]\n"
        "/list\n/remove ID\n/check [ID|all]\n/channel_id PRIVATE-POST-LINK\n/help\n"
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
    if command in ("/channel_id", "/channel-id"):
        if len(args) != 2:
            return "Usage: /channel_id https://t.me/c/CHANNEL/POST", []
        url = urlparse(args[1])
        match = re.fullmatch(r"/c/(\d+)/(\d+)", url.path)
        if url.scheme != "https" or url.netloc not in {"t.me", "telegram.me"} or not match:
            return "Send a private channel post link like https://t.me/c/CHANNEL/POST.", []
        channel_id = -(1_000_000_000_000 + int(match.group(1)))
        return f"Channel ID: {channel_id}\nSet it as the TELEGRAM_DEALS_CHANNEL_ID Actions secret.", []
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


def process_updates(store, path: str | Path | None = None, *, session=None, send=send_alert,
                    send_guest=send_message):
    path = Path(path or os.getenv("ROUTES_FILE", "data/routes.yaml"))
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    requested = []
    for update in telegram_updates(store.bot_offset(), session=session):
        update_id = update["update_id"]
        message = update.get("message") or {}
        source_chat = message.get("chat", {})
        if str(source_chat.get("id")) == chat_id:
            forwarded = (message.get("forward_origin") or {}).get("chat") or message.get("forward_from_chat") or {}
            if forwarded.get("type") == "channel" and forwarded.get("id"):
                send(f"Channel ID: {forwarded['id']}\nSet it as the TELEGRAM_DEALS_CHANNEL_ID Actions secret.")
            elif isinstance(message.get("text"), str):
                reply, ids = handle_command(message["text"], path)
                send(reply)
                requested.extend(ids)
        elif source_chat.get("type") == "private" and str(message.get("text", "")).split("@", 1)[0] == "/start":
            send_guest("Deal alerts are in a private channel. Ask the owner for an invite. Route commands are owner-only.",
                       source_chat["id"])
        store.save_bot_offset(update_id + 1)
    return list(dict.fromkeys(requested))
