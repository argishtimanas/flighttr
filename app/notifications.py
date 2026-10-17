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


def send_alert(body, session=None):
    """Send a free Telegram bot message to the configured chat."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    response = (session or requests).post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": body}, timeout=15)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the alert")
    return str(result["result"]["message_id"])


def telegram_chat_ids(session=None):
    """List chat IDs for users who have already started the bot."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    response = (session or requests).get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram did not return updates")
    return sorted({str(update["message"]["chat"]["id"]) for update in result.get("result", []) if "message" in update})
