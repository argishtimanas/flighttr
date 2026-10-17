"""SQLite locally; PostgreSQL when DATABASE_URL points to a persistent server."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, Integer, MetaData,
                        String, Table, Text, create_engine, func, select, text)

metadata = MetaData()
observations = Table("observations", metadata,
    Column("id", Integer, primary_key=True), Column("route_id", String(64), nullable=False),
    Column("origin", String(3)), Column("destination", String(3)),
    Column("departure_date", Date), Column("return_date", Date), Column("trip_type", String(16)),
    Column("cabin", String(24)), Column("adults", Integer), Column("currency", String(3)),
    Column("airline", Text), Column("flight_numbers", Text), Column("stops", Integer),
    Column("duration_minutes", Integer), Column("price", Float),
    Column("departure_time", Text), Column("arrival_time", Text),
    Column("itinerary_key", String(32)), Column("booking_link", Text), Column("search_link", Text),
    Column("checked_at", DateTime(timezone=True), nullable=False), Column("check_id", String(40), nullable=False))
alerts = Table("alerts", metadata,
    Column("id", Integer, primary_key=True), Column("route_id", String(64)),
    Column("itinerary_key", String(32)), Column("price", Float), Column("currency", String(3)),
    Column("reasons", Text), Column("status", String(16)), Column("message_id", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False))
api_usage = Table("api_usage", metadata,
    Column("day", Date, primary_key=True), Column("count", Integer, nullable=False))
monthly_usage = Table("monthly_usage", metadata,
    Column("month", String(7), primary_key=True), Column("count", Integer, nullable=False))
checks = Table("checks", metadata,
    Column("id", Integer, primary_key=True), Column("route_id", String(64)),
    Column("checked_at", DateTime(timezone=True)), Column("status", String(16)),
    Column("result_count", Integer), Column("error", Text))


class Store:
    def __init__(self, url=None):
        url = url or os.getenv("DATABASE_URL", "sqlite:///data/tracker.db")
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, future=True)
        metadata.create_all(self.engine)

    def reserve_api(self, limit, monthly_limit=200):
        today = datetime.now(timezone.utc).date()
        month = today.strftime("%Y-%m")
        # Atomic upsert; safe across concurrent PostgreSQL workers.
        dialect = self.engine.dialect.name
        class BudgetExhausted(Exception):
            pass
        try:
            with self.engine.begin() as conn:
                if dialect == "postgresql":
                    monthly = conn.execute(text("INSERT INTO monthly_usage(month,count) VALUES (:month,1) ON CONFLICT(month) DO UPDATE SET count=monthly_usage.count+1 WHERE monthly_usage.count < :limit RETURNING count"), {"month": month, "limit": monthly_limit}).first()
                    if monthly is None:
                        raise BudgetExhausted
                    daily = conn.execute(text("INSERT INTO api_usage(day,count) VALUES (:day,1) ON CONFLICT(day) DO UPDATE SET count=api_usage.count+1 WHERE api_usage.count < :limit RETURNING count"), {"day": today, "limit": limit}).first()
                    if daily is None:
                        raise BudgetExhausted
                else:
                    conn.exec_driver_sql("INSERT OR IGNORE INTO monthly_usage(month,count) VALUES (?,0)", (month,))
                    monthly = conn.exec_driver_sql("UPDATE monthly_usage SET count=count+1 WHERE month=? AND count<?", (month, monthly_limit))
                    if monthly.rowcount != 1:
                        raise BudgetExhausted
                    conn.exec_driver_sql("INSERT OR IGNORE INTO api_usage(day,count) VALUES (?,0)", (today.isoformat(),))
                    daily = conn.exec_driver_sql("UPDATE api_usage SET count=count+1 WHERE day=? AND count<?", (today.isoformat(), limit))
                    if daily.rowcount != 1:
                        raise BudgetExhausted
        except BudgetExhausted:
            return False
        return True

    def usage_today(self):
        with self.engine.connect() as conn:
            return conn.execute(select(api_usage.c.count).where(api_usage.c.day == datetime.now(timezone.utc).date())).scalar() or 0

    def usage_month(self):
        with self.engine.connect() as conn:
            return conn.execute(select(monthly_usage.c.count).where(monthly_usage.c.month == datetime.now(timezone.utc).strftime("%Y-%m"))).scalar() or 0

    def save_fares(self, fares, check_id):
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(observations.insert(), [{**f.__dict__, "checked_at": now, "check_id": check_id} for f in fares])

    def record_check(self, route_id, status, count=0, error=None):
        with self.engine.begin() as conn:
            conn.execute(checks.insert().values(route_id=route_id, checked_at=datetime.now(timezone.utc), status=status,
                                               result_count=count, error=(error or "")[:300]))

    def historical(self, fare):
        """Lowest fare per previous check, only for exactly comparable searches."""
        q = (select(observations.c.check_id, func.min(observations.c.price).label("price"),
                    func.min(observations.c.checked_at).label("checked_at"))
             .where(observations.c.origin == fare.origin, observations.c.destination == fare.destination,
                    observations.c.departure_date == fare.departure_date,
                    observations.c.return_date.is_(None) if fare.return_date is None else observations.c.return_date == fare.return_date,
                    observations.c.trip_type == fare.trip_type, observations.c.cabin == fare.cabin,
                    observations.c.adults == fare.adults, observations.c.currency == fare.currency,
                    observations.c.check_id != getattr(fare, "check_id", ""))
             .group_by(observations.c.check_id).order_by(func.min(observations.c.checked_at)))
        with self.engine.connect() as conn:
            return [dict(x._mapping) for x in conn.execute(q)]

    def last_itinerary_price(self, fare):
        with self.engine.connect() as conn:
            return conn.execute(select(observations.c.price).where(
                observations.c.itinerary_key == fare.itinerary_key, observations.c.currency == fare.currency)
                .order_by(observations.c.checked_at.desc(), observations.c.id.desc()).limit(1)).scalar()

    def recent_alert(self, fare):
        with self.engine.connect() as conn:
            row = conn.execute(select(alerts).where(alerts.c.itinerary_key == fare.itinerary_key,
                alerts.c.currency == fare.currency, alerts.c.status == "sent")
                .order_by(alerts.c.created_at.desc()).limit(1)).first()
            return dict(row._mapping) if row else None

    def alert_count_today(self):
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(alerts).where(
                alerts.c.status == "sent", alerts.c.created_at >= datetime.combine(datetime.now(timezone.utc).date(), datetime.min.time(), timezone.utc))).scalar_one()

    def record_alert(self, fare, reasons, status, message_id=None):
        with self.engine.begin() as conn:
            conn.execute(alerts.insert().values(route_id=fare.route_id, itinerary_key=fare.itinerary_key,
                price=fare.price, currency=fare.currency, reasons=", ".join(reasons), status=status,
                message_id=message_id, created_at=datetime.now(timezone.utc)))

    def dashboard_rows(self, table, limit=1000):
        with self.engine.connect() as conn:
            return [dict(x._mapping) for x in conn.execute(select(table).order_by(table.c.id.desc()).limit(limit))]
