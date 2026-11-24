# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Serves dsy_state_* gauges on 9464 through the Temporal runtime, refreshed from the app database every 60 s."""

import os
import time

import psycopg
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

runtime = Runtime(telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address="0.0.0.0:9464")))
meter = runtime.metric_meter
tables = meter.create_gauge("dsy_state_table_live_rows", "Live rows per table.")
sessions = meter.create_gauge("dsy_state_sessions", "Sessions of the app database.")
dsn = os.environ["DSY_ADMISSION_DSN"]

while True:
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            sessions.set(conn.execute("select count(*) from pg_stat_activity").fetchone()[0])
            for name, rows in conn.execute(
                "select relname, n_live_tup from pg_stat_user_tables"
            ).fetchall():
                tables.set(int(rows), {"table": name})
    except Exception as error:  # noqa: BLE001
        print("pass failed:", repr(error), flush=True)
    time.sleep(60)
