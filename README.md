# Flight Deal Tracker — free setup

Tracks Google Flights prices through SerpApi, stores observed price history in SQLite, and sends alerts through a Telegram bot. A Streamlit dashboard shows routes, trends and alert history. The included GitHub Actions workflow checks every 12 hours and commits its SQLite file to a **private** repository so price history survives temporary runners. Review the route configuration before the first live check.

This setup uses no paid service when kept within the providers' free allowances. **It sends Telegram app notifications, not carrier SMS.** Twilio's SMS trial expires and cannot provide ongoing free custom flight alerts.

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

## Free scheduled deployment

1. Create a **private GitHub repository** containing this project's files. Keep `.github/workflows/flight_tracker.yml` at the repository root. The workflow stops before any API search if GitHub does not identify the repository as private.
2. Add repository Actions secrets `SERPAPI_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.
3. Configure your route in `data/routes.yaml`, set `enabled: true`, and push it.
4. Run the **Flight deal check** workflow manually once, then inspect its log and the committed `data/tracker.db` file. The workflow requires all three secrets before it searches. It runs at `0 */12 * * *` UTC; edit the cron line to change frequency.

The workflow requests `contents: write` so it can commit the SQLite database back to the private repository after each run. This is the persistent store for scheduled checks. If branch protection blocks the workflow's push, allow the workflow to write to that branch or use a separate writable private branch. SQLite files contain route and fare history, so keep the repository private. The local `.gitignore` excludes the database by default; the workflow deliberately force-adds only `data/tracker.db`. Scheduled runs are serialized through GitHub Actions concurrency and the app's file lock. For local cron, schedule `cd /path/to/flight-deal-tracker && /path/to/.venv/bin/python -m app.main check`.

The free tier is finite. [SerpApi currently lists 250 searches per month](https://serpapi.com/pricing); this app defaults to `MONTHLY_API_LIMIT=200` to leave a buffer, with `DAILY_API_LIMIT=100` and `MAX_REQUESTS_PER_RUN=20`. Every retry and round-trip follow-up consumes a request. One exact one-way route checked twice a day uses roughly 60 searches in a 30-day month. One exact round trip with two return follow-ups can use up to 180 searches. A flexible range or multiple routes can hit the cap sooner and then pauses until the next UTC month. The app's cap only counts its own requests; other use of the same SerpApi account is separate. Check your SerpApi account usage too. [Telegram bots can message users for free within normal rate limits](https://core.telegram.org/bots/faq). [GitHub Free includes 2,000 Actions minutes per month for private repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions); stay within that allowance and set a GitHub spending budget that stops usage at $0 if you want a hard billing safeguard.

## Troubleshooting

- No fares: verify future dates and airport codes; broaden filters.
- No Telegram chat ID: send `/start` to the bot first, then run `telegram-chat-id` again.
- No alert: a fare may not meet a deal rule, the cooldown may still apply, or `DAILY_ALERT_LIMIT` may have been reached.
- API stops early: inspect today's and this month's request counts in the dashboard. Reduce date combinations or check frequency.
- Scheduled history disappears: verify the workflow can commit `data/tracker.db` and that it runs from the same branch on the next check.
- Scheduled job fails: inspect the GitHub Actions log and confirm the three secrets and route file are present.
