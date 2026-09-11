# Dashboards

## Grafana Cloud (current)

`grafana-rekordo.json` — the Rekordo dashboard on `janne6565.grafana.net`, uid `rekordo`.
One dashboard, five rows: Overview, Traffic & latency, Sync & integrity, Runtime,
Collection & community. Variables: `DS` (datasource) and `env` (`prod` / `staging`,
from the `deployment_environment` label).

**Rekordo reaches Grafana Cloud over OTLP, so the metric names are NOT the ones the old
SigNoz dashboards used.** Micrometer's OTLP registry publishes durations in
**milliseconds** — `http_server_requests_milliseconds`, not
`http_server_requests_seconds` and not the dotted `http.server.request.duration`.
Latency is a summary with a `quantile` label (client-side percentiles), so
`histogram_quantile()` does not apply and the percentiles cannot be re-aggregated
across replicas; `max by (...)` is the honest reducer at one replica.

Regenerate with the script that built it rather than hand-editing, then apply:

    curl -s -X POST https://janne6565.grafana.net/api/dashboards/db \
      -H "Authorization: Bearer $GRAFANA_SA_TOKEN" -H 'Content-Type: application/json' \
      -d "$(python3 -c 'import json;print(json.dumps({"dashboard":json.load(open("monitoring/grafana-rekordo.json")),"overwrite":True}))')"

The token is a **Grafana service account token** (`glsa_…`, Administration → Users and
access → Service accounts), not the `glc_…` Cloud access-policy token used for
ingest/queries — that one 401s against the Grafana instance API.

## SigNoz (legacy)

> SigNoz is being decommissioned; these two are kept until the Grafana Cloud dashboard
> has been checked against them, then they go.

The version-controlled copy of Rekordo's two dashboards. SigNoz keeps dashboards in its
own database, not in Kubernetes manifests, so **ArgoCD does not apply these** — they are
here so a dashboard someone edits in the UI can be diffed, reviewed and restored.

| File | Dashboard | What it answers |
|---|---|---|
| `dashboard-rekordo-collection-community.json` | Rekordo — Collection & Community | Who signed up, what they collect, what they are hunting for |
| `dashboard-rekordo-service-health.json` | Rekordo — Service Health | Whether it works, where it is slow, and what it is quietly throwing away |

Both take an `env` variable (`prod` / `staging`), which selects on the
`deployment.environment` resource attribute set per overlay.

## Re-exporting after a UI edit

    curl -s -H "SIGNOZ-API-KEY: $KEY" \
      https://signoz.jannekeipert.de/api/v1/dashboards/<uuid> \
      | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(json.dumps({"uuid":d["id"],"data":d["data"]}, indent=2, sort_keys=True))' \
      > monitoring/dashboard-<slug>.json

The uuid is the `uuid` field in each file. The API key lives in the login keychain
(`security find-generic-password -s signoz.jannekeipert.de -a alert-rule-automation -w`);
never commit it.

## Why the panels are PromQL and not the query builder

Rekordo's metrics arrive over OTLP, so SigNoz stores them under their dotted names
(`rekordo.users.total`, `http.server.request.duration.count`). PromQL reaches those through
the `{__name__="..."}` form, and dotted *label* names need quoting the same way
(`{"deployment.environment"="prod"}`). It also matches how the other services' dashboards
here are written.

A panel counting bad things ends in `or vector(0)` on purpose: without it a healthy service
matches no series and the panel reads "no data", which looks identical to a panel that is
broken.

## Value panels need a unit, even when there isn't one

A value panel left with an empty `yAxisUnit` renders any number of 1000 or more as
**0**. Small numbers pass through, so the panel looks fine right up to the day it
doesn't: "Catalogue entries" read 0 against 6841 rows in the database, while the
"Catalogue growth" graph beside it drew the same series correctly.

Set `"yAxisUnit": "none"` on unitless value panels. It is not the same as `""` --
`none` selects the plain-number formatter, `""` selects nothing and the value is
formatted away.

## A value panel reads the *start* of the window, not the end

Every value panel here is a PromQL panel, and a PromQL value panel has no reduce
operator: SigNoz evaluates the query across the dashboard's whole time range and
the tile then shows the value at the **beginning** of it. For a gauge that grows
-- accounts, records, catalogue entries -- that means the tile silently reports
however far back the window reaches. "Accounts" read **2** on Last 7 days and
**5** on Last 30 minutes against five real accounts; nothing about the panel says
which one you are looking at.

The fix is per-panel, not per-query: no PromQL can make a past timestamp know the
present. Set **Panel Time Preference** (`timePreferance` in the JSON) to a short
fixed window and the tile stops following the dashboard:

    "timePreferance": "LAST_15_MIN"

`LAST_15_MIN` for everything that is read from the database on the five-minute
timer, `LAST_6_HR` for the two storage panels -- `StorageMetrics` walks the bucket
**hourly**, so a fifteen-minute window contains no sample at all and their
`or vector(0)` renders a confident 0 B.

The reading lags by at most the panel's own window, which is the trade: a number
that is fifteen minutes old on a screen, rather than a week old.

**After a bulk edit the grid lies for a minute.** Several tiles read 0 straight
after saving all eighteen, including ones that had been correct. Nothing was
wrong with them -- the queries and the config were identical to the panels that
worked, and a fresh load a few minutes later showed every one correctly. Do not
chase it; reload before believing a zero.

## `process.uptime` is milliseconds

Micrometer documents seconds and publishes milliseconds. With `"yAxisUnit": "s"`
the Uptime panel read **1.9 years** for a pod sixteen hours old -- and, before the
window fix hid it, a plausible-looking **6.85 days**. The query divides by 1000:

    max({__name__="process.uptime", ...}) / 1000

The alert rules already accounted for this; the panel did not.

## Alert rules

Not here — the rules live in `cluster-deployment/infrastructure/signoz-alerts/`, beside
every other service's, because they are managed as one set and delivered through one n8n
channel.
