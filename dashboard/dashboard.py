"""Local dashboard: streamlit run dashboard/dashboard.py."""
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import load_routes, route_from_dict
from app.database import Store, observations, alerts, checks
from app.main import check

st.set_page_config(page_title="Flight Deal Tracker", layout="wide")
st.title("Flight Deal Tracker")
route_path = Path(os.getenv("ROUTES_FILE", "data/routes.yaml"))
store = Store()


def save_routes(routes):
    serial = []
    for route in routes:
        item = asdict(route)
        for key in ("departure_start", "departure_end", "return_start", "return_end"):
            item[key] = item[key].isoformat() if item[key] else None
        item["preferred_airlines"] = list(item["preferred_airlines"])
        serial.append(item)
    route_path.parent.mkdir(parents=True, exist_ok=True)
    route_path.write_text(yaml.safe_dump({"routes": serial}, sort_keys=False))


try:
    routes = load_routes(route_path)
except (ValueError, FileNotFoundError) as exc:
    st.error(f"Route configuration: {exc}")
    st.stop()

st.caption(f"API requests today: {store.usage_today()} / {os.getenv('DAILY_API_LIMIT', '100')} · this month: {store.usage_month()} / {os.getenv('MONTHLY_API_LIMIT', '200')}")
col1, col2 = st.columns([3, 1])
with col2:
    if st.button("Check prices now", type="primary"):
        try:
            result = check(routes=routes, store=store)
            st.success(f"Checked {len(result)} active routes")
            st.json(result)
        except Exception as exc:
            st.error(f"Check failed: {exc}")

st.subheader("Monitored routes")
st.dataframe(pd.DataFrame([{"id": r.id, "route": f"{r.origin} → {r.destination}", "trip": r.trip_type,
    "depart": str(r.departure_start), "return": str(r.return_start or ""), "target": r.max_price,
    "alerts": r.alerts_enabled, "active": r.enabled} for r in routes]), use_container_width=True)

with st.expander("Add or edit a route"):
    choices = ["New route"] + [r.id for r in routes]
    selected = st.selectbox("Route", choices)
    existing = next((r for r in routes if r.id == selected), None)
    with st.form("route_form"):
        rid = st.text_input("Route ID", existing.id if existing else "")
        a, b = st.columns(2)
        origin = a.text_input("Departure airport", existing.origin if existing else "SFO")
        destination = b.text_input("Destination airport", existing.destination if existing else "JFK")
        trip_type = st.selectbox("Trip type", ["round_trip", "one_way"], index=0 if not existing or existing.trip_type == "round_trip" else 1)
        departure_start = st.date_input("Departure from", existing.departure_start if existing else date.today())
        departure_end = st.date_input("Departure through", existing.departure_end if existing else date.today())
        return_start = st.date_input("Return from", existing.return_start if existing and existing.return_start else date.today())
        return_end = st.date_input("Return through", existing.return_end if existing and existing.return_end else date.today())
        cabin = st.selectbox("Cabin", ["economy", "premium_economy", "business", "first"], index=["economy", "premium_economy", "business", "first"].index(existing.cabin) if existing else 0)
        adults = st.number_input("Adults", 1, 9, existing.adults if existing else 1)
        currency = st.text_input("Currency", existing.currency if existing else "USD")
        target = st.number_input("Target price (0 disables threshold)", min_value=0.0, value=float(existing.max_price or 0) if existing else 0.0)
        airlines = st.text_input("Preferred airline codes, comma-separated", ",".join(existing.preferred_airlines) if existing else "")
        max_stops = st.selectbox("Maximum stops", ["Any", "0", "1", "2"], index=0 if not existing or existing.max_stops is None else existing.max_stops + 1)
        duration = st.number_input("Maximum duration in minutes (0 means any)", min_value=0, value=existing.max_duration_minutes or 0 if existing else 0)
        alert_enabled = st.checkbox("Telegram alerts", existing.alerts_enabled if existing else True)
        enabled = st.checkbox("Active", existing.enabled if existing else True)
        submit = st.form_submit_button("Save route")
    if submit:
        try:
            raw = {"id": rid, "origin": origin.upper(), "destination": destination.upper(), "trip_type": trip_type,
                   "departure_start": departure_start, "departure_end": departure_end,
                   "return_start": return_start if trip_type == "round_trip" else None,
                   "return_end": return_end if trip_type == "round_trip" else None,
                   "cabin": cabin, "adults": adults, "currency": currency.upper(), "max_price": target or None,
                   "preferred_airlines": [x.strip().upper() for x in airlines.split(",") if x.strip()],
                   "max_stops": None if max_stops == "Any" else int(max_stops),
                   "max_duration_minutes": duration or None, "alerts_enabled": alert_enabled, "enabled": enabled}
            new_route = route_from_dict(raw)
            save_routes([r for r in routes if r.id != selected and r.id != new_route.id] + [new_route])
            st.success("Route saved")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if existing and st.button("Remove selected route"):
        save_routes([r for r in routes if r.id != selected])
        st.rerun()

obs = pd.DataFrame(store.dashboard_rows(observations, 5000))
ck = pd.DataFrame(store.dashboard_rows(checks, 100))
al = pd.DataFrame(store.dashboard_rows(alerts, 100))
if not ck.empty:
    successful = ck[ck.status.isin(["success", "partial"])]
    st.caption("Last successful API check: " + (str(successful.iloc[0].checked_at) if not successful.empty else "none"))
if not obs.empty:
    st.subheader("Current cheapest by route and travel dates")
    last_checks = obs.sort_values("checked_at").groupby(["route_id", "departure_date", "return_date"], dropna=False).tail(1)[["route_id", "departure_date", "return_date", "check_id"]]
    latest = obs.merge(last_checks, on=["route_id", "departure_date", "return_date", "check_id"])
    latest = latest.sort_values("price").groupby(["route_id", "departure_date", "return_date"], dropna=False).head(1)
    st.dataframe(latest[["route_id", "departure_date", "return_date", "airline", "price", "currency", "stops"]].sort_values("price").head(50), use_container_width=True)
    st.subheader("Price history")
    selected_route = st.selectbox("View route", sorted(obs.route_id.unique()))
    part = obs[obs.route_id == selected_route].copy()
    lowest = part.groupby("check_id", as_index=False).agg(price=("price", "min"), checked_at=("checked_at", "min"))
    st.plotly_chart(px.line(lowest.sort_values("checked_at"), x="checked_at", y="price", markers=True), use_container_width=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Historical minimum", f"{part.currency.iloc[0]} {part.price.min():,.0f}")
    c2.metric("Average check minimum", f"{lowest.price.mean():,.0f}")
    c3.metric("Median check minimum", f"{lowest.price.median():,.0f}")
    st.subheader("Cheapest travel dates")
    st.dataframe(part.sort_values("price")[["departure_date", "return_date", "airline", "price"]].drop_duplicates(["departure_date", "return_date"]).head(5), use_container_width=True)
    lowest = lowest.sort_values("checked_at")
    lowest["price_drop"] = lowest.price.shift(1) - lowest.price
    st.subheader("Recent price drops")
    st.dataframe(lowest[lowest.price_drop > 0].sort_values("checked_at", ascending=False).head(10), use_container_width=True)
else:
    st.info("No observations yet. Run a check or inspect the included demo data.")
st.subheader("Alert history")
st.dataframe(al if not al.empty else pd.DataFrame(columns=["route_id", "price", "reasons", "status", "created_at"]), use_container_width=True)
st.subheader("Recent checks")
st.dataframe(ck if not ck.empty else pd.DataFrame(columns=["route_id", "checked_at", "status", "result_count"]), use_container_width=True)
