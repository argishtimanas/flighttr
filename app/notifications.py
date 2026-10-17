import os
import requests


def format_alert(fare, reasons, metrics):
    dates = fare.departure_date.isoformat()
    if fare.return_date:
        dates += "–" + fare.return_date.isoformat()
    text = (f"FLIGHT DEAL ALERT!\n{fare.origin} → {fare.destination} | {dates}\n"
            f"{fare.airline} | {fare.currency} {fare.price:,.0f} {fare.trip_type.replace('_', '-')}\n"
            f"Stops: {fare.stops} | {', '.join(reasons)}")
    if metrics["last_price"] is not None:
        text += f"\nPrevious check lowest: {fare.currency} {metrics['last_price']:,.0f} | Drop: {metrics['drop_percent']:.1f}%"
    return text + "\nSearch: " + (fare.booking_link or fare.search_link)


def send_message(body, chat_id, session=None):
    """Send a Telegram message to an explicit chat without logging credentials."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        response = (session or requests).post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": body}, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Telegram message request failed") from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the alert")
    return str(result["result"]["message_id"])


def send_alert(body, session=None):
    """Send an owner-only message, such as a command reply or test alert."""
    return send_message(body, os.environ["TELEGRAM_CHAT_ID"], session=session)


def send_channel_alert(body, session=None):
    """Post a deal to the private channel configured in Actions secrets."""
    channel_id = os.getenv("TELEGRAM_DEALS_CHANNEL_ID")
    if not channel_id:
        raise RuntimeError("TELEGRAM_DEALS_CHANNEL_ID is not configured")
    return send_message(body, channel_id, session=session)


def telegram_chat_ids(session=None):
    """List chat IDs for users who have already started the bot."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        response = (session or requests).get(
            f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Telegram update request failed") from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram did not return updates")
    return sorted({str(update["message"]["chat"]["id"]) for update in result.get("result", []) if "message" in update})


def telegram_updates(offset, session=None):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        response = (session or requests).get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "limit": 100, "timeout": 0, "allowed_updates": '["message"]'}, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Telegram update request failed") from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected update request")
    return result.get("result", [])
