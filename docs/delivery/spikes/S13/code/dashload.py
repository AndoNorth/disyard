# ruff: noqa: E501  (throwaway spike code with long prose lines)
"""Loads Grafana the way a viewer does: every minute, every panel query of every provisioned dashboard as a one hour
range query through Grafana's datasource proxy, for SECONDS seconds.
Usage: dashload.py SECONDS   (Grafana forwarded on 127.0.0.1:19300)
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:19300"


def get(path: str):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


end = time.time() + int(sys.argv[1])
ok = bad = 0
while time.time() < end:
    for item in get("/api/search?type=dash-db"):
        dash = get(f"/api/dashboards/uid/{item['uid']}")["dashboard"]
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                expr = target.get("expr")
                if not expr or "$" in expr:
                    continue
                now = time.time()
                q = urllib.parse.urlencode(
                    {"query": expr, "start": now - 3600, "end": now, "step": 30}
                )
                try:
                    get(f"/api/datasources/proxy/uid/prometheus/api/v1/query_range?{q}")
                    ok += 1
                except Exception:
                    bad += 1
    print(f"queries ok {ok} failed {bad}", flush=True)
    time.sleep(60)
