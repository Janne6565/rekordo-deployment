#!/usr/bin/env python3
"""Generate the Rekordo Grafana Cloud dashboard.

Queries here were each run against the live Prometheus datasource before being
written in; none are guessed. Two things differ from the old SigNoz dashboards
and are the reason this is a rewrite rather than a translation:

  * Rekordo reaches Grafana Cloud over OTLP, and Micrometer's OTLP registry
    publishes DURATIONS IN MILLISECONDS with different names than the Prometheus
    scrape path: http_server_requests_milliseconds, not
    http_server_requests_seconds / http.server.request.duration.
  * Latency is client-side percentiles (a summary with a `quantile` label), not
    buckets, so histogram_quantile() does not apply. These are computed per
    instance and cannot be re-aggregated; `max by (...)` is the honest reducer
    at one replica.
"""
import json

DS = {"type": "prometheus", "uid": "${DS}"}
ENV = 'deployment_environment="$env", job="rekordo-backend"'

panels = []
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def target(expr, legend=None, instant=False):
    t = {"datasource": DS, "editorMode": "code", "expr": expr, "range": not instant,
         "instant": instant, "refId": chr(65 + target.n)}
    target.n += 1
    if legend:
        t["legendFormat"] = legend
    return t


target.n = 0


def row(title, y):
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})
    return y + 1


def stat(title, expr, x, y, w=4, h=4, unit="short", decimals=None, desc=None):
    target.n = 0
    p = {"id": nid(), "type": "stat", "title": title, "datasource": DS,
         "gridPos": {"h": h, "w": w, "x": x, "y": y},
         "targets": [target(expr, instant=True)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "orientation": "auto", "textMode": "auto", "colorMode": "value",
                     "graphMode": "area", "justifyMode": "auto"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute",
                                                     "steps": [{"color": "text", "value": None}]}},
                         "overrides": []}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if desc:
        p["description"] = desc
    panels.append(p)


def ts(title, targets, x, y, w=12, h=8, unit="short", desc=None, stack=False, fill=0):
    target.n = 0
    tg = [target(e, l) for e, l in targets]
    p = {"id": nid(), "type": "timeseries", "title": title, "datasource": DS,
         "gridPos": {"h": h, "w": w, "x": x, "y": y}, "targets": tg,
         "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
         "fieldConfig": {"defaults": {
             "unit": unit,
             "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
                        "showPoints": "never", "spanNulls": True,
                        "stacking": {"mode": "normal" if stack else "none", "group": "A"}}},
             "overrides": []}}
    if desc:
        p["description"] = desc
    panels.append(p)


y = 0

# ---------------------------------------------------------------- Overview
y = row("Overview", y)
stat("Uptime", f"process_uptime_milliseconds{{{ENV}}} / 1000", 0, y, unit="s",
     desc="Seconds since the JVM started. A reset means the pod rolled.")
stat("Requests / sec", f"sum(rate(http_server_requests_milliseconds_count{{{ENV}}}[5m]))",
     4, y, unit="reqps", decimals=2)
stat("5xx / sec", f'sum(rate(http_server_requests_milliseconds_count{{{ENV}, status=~"5.."}}[5m])) or vector(0)',
     8, y, unit="reqps", decimals=3,
     desc="`or vector(0)` on purpose: with no 5xx the query returns no series, and the tile would read 'No data' rather than zero.")
stat("p95 latency", f'max(http_server_requests_milliseconds{{{ENV}, quantile="0.95"}})',
     12, y, unit="ms", decimals=1,
     desc="Client-side percentile computed inside the JVM, not from buckets. Cannot be re-aggregated across replicas.")
stat("Accounts", f"rekordo_users_total{{{ENV}}}", 16, y)
stat("Records", f"rekordo_copies_total{{{ENV}}}", 20, y)
y += 4

# ------------------------------------------------------- Traffic & latency
y = row("Traffic & latency", y)
ts("Request rate by status",
   [(f"sum by (status) (rate(http_server_requests_milliseconds_count{{{ENV}}}[5m]))", "{{status}}")],
   0, y, unit="reqps", stack=True, fill=30)
ts("Latency percentiles",
   [(f'max(http_server_requests_milliseconds{{{ENV}, quantile="{q}"}})', f"p{int(float(q)*100)}")
    for q in ("0.5", "0.95", "0.99")],
   12, y, unit="ms")
y += 8
ts("p95 by route",
   [(f'max by (uri) (http_server_requests_milliseconds{{{ENV}, quantile="0.95"}})', "{{uri}}")],
   0, y, unit="ms")
ts("Errors by route",
   [(f'sum by (uri) (rate(http_server_requests_milliseconds_count{{{ENV}, outcome=~"SERVER_ERROR|CLIENT_ERROR"}}[5m]))', "{{uri}}")],
   12, y, unit="reqps",
   desc="Both 4xx and 5xx: a spike of CLIENT_ERROR on one route is usually a client shipping a bad payload, which is worth seeing next to the 5xx.")
y += 8

# --------------------------------------------------------- Sync & integrity
y = row("Sync & integrity", y)
ts("Sync records dropped / min, by kind and reason",
   [(f"sum by (kind, reason) (rate(rekordo_sync_push_dropped_total{{{ENV}}}[30m]) * 60)", "{{kind}} / {{reason}}")],
   0, y, unit="short", stack=True, fill=30,
   desc="A dropped push is a record a phone tried to sync and the backend refused. Sustained non-zero means a client is stuck: it will retry the same poison record forever and that device silently stops syncing.")
ts("Turnstile verifications / min",
   [(f"sum by (outcome) (rate(rekordo_turnstile_verifications_total{{{ENV}}}[30m]) * 60)", "{{outcome}}")],
   12, y, unit="short",
   desc="Bot check on the open auth endpoints. Currently observing rather than refusing.")
y += 8

# ------------------------------------------------------------------ Runtime
y = row("Runtime", y)
ts("Heap used", [(f'sum(jvm_memory_used_bytes{{{ENV}, area="heap"}})', "heap")],
   0, y, w=8, unit="bytes")
ts("GC pause", [(f"rate(jvm_gc_pause_milliseconds_sum{{{ENV}}}[5m])", "pause / sec")],
   8, y, w=8, unit="ms")
ts("Database connections",
   [(f"max(hikaricp_connections_active{{{ENV}}})", "active"),
    (f"max(hikaricp_connections_idle{{{ENV}}})", "idle"),
    (f"max(hikaricp_connections_max{{{ENV}}})", "max")],
   16, y, w=8, unit="short")
y += 8

# ------------------------------------------------- Collection & community
y = row("Collection & community", y)
stat("Collectors", f"rekordo_users_collecting{{{ENV}}}", 0, y,
     desc="Accounts with at least one record, as opposed to registered accounts.")
stat("Verified", f"rekordo_users_verified{{{ENV}}}", 4, y)
stat("Public profiles", f"rekordo_users_with_handle{{{ENV}}}", 8, y)
stat("Catalogue entries", f"rekordo_catalogue_releases{{{ENV}}}", 12, y)
stat("Wishlist entries", f"rekordo_wishlist_total{{{ENV}}}", 16, y)
stat("Push devices", f"rekordo_push_devices{{{ENV}}}", 20, y)
y += 4
ts("Accounts over time",
   [(f"rekordo_users_total{{{ENV}}}", "accounts"),
    (f"rekordo_users_collecting{{{ENV}}}", "collectors"),
    (f"rekordo_users_verified{{{ENV}}}", "verified")],
   0, y, unit="short")
ts("Collection size over time",
   [(f"rekordo_copies_total{{{ENV}}}", "records"),
    (f"rekordo_wishlist_total{{{ENV}}}", "wishes"),
    (f"rekordo_copies_rated{{{ENV}}}", "rated")],
   12, y, unit="short")
y += 8
ts("Sign-ups and records added",
   [(f"rekordo_users_signups_1d{{{ENV}}}", "sign-ups, 24h"),
    (f"rekordo_users_signups_7d{{{ENV}}}", "sign-ups, 7d"),
    (f"rekordo_copies_added_1d{{{ENV}}}", "records added, 24h")],
   0, y, unit="short")
ts("Image storage by kind",
   [(f"sum by (kind) (rekordo_storage_bytes{{{ENV}}})", "{{kind}}"),
    (f"rekordo_storage_orphan_bytes{{{ENV}}}", "orphaned")],
   12, y, unit="bytes", stack=False,
   desc="Orphaned bytes are objects in the bucket no row points at any more. A steadily climbing orphan line means deletes are not cleaning up.")
y += 8

dashboard = {
    "uid": "rekordo",
    "title": "Rekordo",
    "description": "Rekordo backend: service health and collection/community metrics. "
                   "Telemetry arrives over OTLP via Grafana Alloy, so durations are in "
                   "milliseconds and latency is client-side percentiles rather than histogram buckets.",
    "tags": ["rekordo", "music-collector"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "templating": {"list": [
        {"name": "DS", "label": "Datasource", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "refresh": 1},
        {"name": "env", "label": "Environment", "type": "query", "datasource": DS,
         "query": {"qryType": 1, "query": "label_values(rekordo_users_total, deployment_environment)",
                   "refId": "env"},
         "definition": "label_values(rekordo_users_total, deployment_environment)",
         "current": {"text": "prod", "value": "prod"},
         "includeAll": False, "multi": False, "refresh": 1, "sort": 1},
    ]},
    "panels": panels,
}

out = "/Users/jannekeipert/projects/music-collector/music-collector-deployment/monitoring/grafana-rekordo.json"
with open(out, "w") as f:
    json.dump(dashboard, f, indent=2, sort_keys=False)
    f.write("\n")
print(f"wrote {out}: {len([p for p in panels if p['type'] != 'row'])} panels, "
      f"{len([p for p in panels if p['type'] == 'row'])} rows")
