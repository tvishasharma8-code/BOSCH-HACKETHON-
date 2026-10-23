# Architecture: Incident Zero AI On-Call Engineer

## Flow

```
 Prometheus ──▶ Alert watcher ──▶ Agent loop (LLM) ──▶ Diagnosis ──▶ Action planner ──▶ Human approval ──▶ Action API ──▶ Verifier ──▶ Report
 (thresholds)      (detect)       picks next tool       (cause +      (allowlist only)    (approve /        (only state    (re-check     (JSON +
                                   ▲        │            evidence +                        reject,           change)        telemetry)    readable)
                                   │        ▼            confidence)                       dry run)
                              READ-ONLY telemetry tools
                 metrics (Prometheus) · traces (Jaeger) · logs · container health (Docker, read-only) · deploy history
```

## Components

| Component | What it does | Owner |
|---|---|---|
| Alert watcher | Polls Prometheus error rate / p95 latency per service; fires after 2+ consecutive breaches | Aditi |
| Metrics tools | Error rate, latency, request rate, CPU/memory per service | Aditi |
| Trace tools | Find failing/slow traces, follow the request to the deepest failing span | Tvisha |
| Logs + container tools | Error logs per service; container status, restarts, OOM (read-only Docker) | Mohit |
| Agent loop | LLM chooses the next tool from findings so far; logs why each tool was chosen; bounded steps | Pavan |
| Safety + action (Day 2) | Approval gate, dry run, kill switch, Action API client, verifier, report | Hemanth |

## How the agent decides
- It starts from the alert, takes a broad metrics view, then narrows to the alerting service and its dependencies.
- Each step, it picks the tool that best separates the current hypotheses (app bug vs. dependency vs. container vs. network vs. load).
- It follows errors downstream: the first service showing errors is often a victim, not the cause.
- It stops when the evidence supports a diagnosis, or escalates when the step budget runs out or evidence conflicts.

## Safety controls
- The LLM only gets **read-only** tools. It cannot change anything.
- No feature-flag access: no flagd, flag UI or flag API. Diagnosis comes from telemetry only.
- Every claim cites evidence: the exact query, the value and the timestamp from a real tool result.
- Fixes are limited to the Action API allowlist and need an **explicit human approve**. Dry run is the default, and a kill switch stops everything.
- Recovery is checked from telemetry, not from the Action API's response. If recovery fails, the agent rolls back or escalates.
