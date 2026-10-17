# Flight Deal Tracker

Tracks Google Flights prices through SerpApi, stores observed price history in SQLite, and sends alerts through a Telegram bot. A Streamlit dashboard shows routes, trends and alert history. The included GitHub Actions workflow checks every 12 hours and commits its SQLite file to a **private** repository so price history survives temporary runners. Sends Telegram app notifications, not carrier SMS.

## Local setup

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main demo
pytest -q
```

The demo uses `data/sample_flights.json` and makes no network call. Obtain a [free SerpApi key](https://serpapi.com/) and put it in `.env` as `SERPAPI_API_KEY`. Review the existing active route in `data/routes.yaml` and run `python -m app.main check`. Launch the dashboard with `streamlit run dashboard/dashboard.py`; it can add, edit, and remove routes and trigger checks. An airport pair may return no flights; the tracker records that as an empty check and keeps the route unchanged.

## Free Telegram notifications

1. In Telegram, message **@BotFather**, create a bot with `/newbot`, and copy the bot token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Open your new bot in Telegram and send `/start`.
3. Run `python -m app.main telegram-chat-id` and copy the printed chat ID into `.env` as `TELEGRAM_CHAT_ID`.
4. Run `python -m app.main test-alert`. This sends one real Telegram message at no cost under Telegram's normal bot limits.
5. Keep `alerts_enabled: true` on each route that should notify you.

The bot token is a secret. Never put it in `routes.yaml` or commit `.env`.

## Routes and deal rules

Each entry in `data/routes.yaml` needs a unique `id`, 3-letter IATA `origin` and `destination`, `trip_type` (`one_way` or `round_trip`), and a departure date or range (`departure_start`, `departure_end`). Round trips also need `return_start` and `return_end`. Dates use `YYYY-MM-DD`. Optional fields are `cabin`, `adults`, `currency`, `max_price`, `preferred_airlines` (2-character IATA codes), `max_stops` (0, 1, 2, or null), `max_duration_minutes`, `alerts_enabled`, and `enabled`.

The tracker checks each exact date combination. Round trips use SerpApi's documented `departure_token` follow-up request to obtain real return choices and complete prices. This costs extra search credits. It checks at most `MAX_OUTBOUND_OPTIONS` cheapest outbound options per date pair (default 2). It never treats a token as a booking URL; alerts include a Google Flights search link when no direct booking link is present.

An alert qualifies when the current cheapest fare for a date pair meets a target price, falls at least 15% from the previous comparable check, makes a new historical low, or is unusually below the historical median after enough collected observations. Comparison groups require matching airports, exact dates, trip type, cabin, adult count, and currency. No invented historical prices are used. Alerts have a cooldown, meaningful-change threshold, per-itinerary deduplication, and a daily limit.
