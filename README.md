# Incident Zero — AI On-Call Engineer

> **Build plan, architecture and phase-wise execution guide for a 5-person team.**
> This file is written so it can be handed directly to **Claude Code** to continue the work.
> Tip: copy this file to `CLAUDE.md` at the repo root so Claude Code loads it automatically every session.
> The submission README (setup, running, approval gate, kill switch, limitations) is a separate file written in Phase 5.

**Theme:** Agentic AI for IT Operations — *from alert to safe, approved fix.*
**Target system:** OpenTelemetry Demo ("Astronomy Shop"), running locally with Docker Compose.
**Core principle:** a good agent does not always act. It acts when evidence supports the action, and says
"I'm not sure" and escalates when it does not.

**Important change from the brief:** the organizers are **not** providing the JSON schema, `faultctl`, the Action API
or offline incident bundles. **We build all of them ourselves** (Section 3 and Section 5). The team has
**5 members (A, B, C, D, E)**, not 3 as stated in the PDF.

---

## 0. Instructions for Claude Code (read this first, every session)

1. **Read Section 1 (Hard Rules) before writing any code.** Violating them costs points.
2. **Check Section 17 (Status Tracker)** to see the current block, then find the task in **Section 11 (Schedule)**.
   Ask the user which member (A–E) you are working for, and work only on that member's branch and folders.
3. **Two separate codebases live in this repo:**
   - `envkit/` = the **environment** we build in place of the organizers' kit (faultctl, Action API, bundle recorder, JSON schema tooling).
   - `agent/` = the **AI on-call agent**.
   The agent must treat `envkit/` exactly as if a stranger wrote it: it may only talk to the Action API over HTTP.
   It must never import `envkit`, read faultctl state, or read bundle answer files.
4. **Start every task from the latest `dev`** and use the exact branch name from Section 11. Follow Section 10 (Git Workflow).
5. **Never edit files owned by another member** (Section 9). If an interface must change, tell the user so the owner can change it.
6. **Every module gets a test.** Run `pytest -q` before merging into dev.
7. **When in doubt about safety, choose the safer option** (dry run, escalate, refuse) and leave a `# SAFETY:` comment.
8. **After 12:00 on Day 2: no new features** — only `fix/*` and `docs/*` branches.
9. **Be honest in docs.** If something does not work, write it in `EVALUATION.md` / Known Limitations.
10. **Update Section 17** at the end of every task.

---

## 1. Hard Rules (non-negotiable)

| # | Rule | How we enforce it in code |
|---|------|---------------------------|
| R1 | The agent never edits demo source code, demo configuration, `faultctl` or any scoring/eval code. | Agent has no write tools. No shell or file-write tools are exposed to the LLM. Docker access from `agent/` is through a read-only wrapper. |
| R2 | The system may only be changed through the **Action API**. | In `agent/`, only `agent/execution/action_client.py` may call Action API `POST` endpoints. Test enforces it. |
| R3 | No state-changing action without **explicit human approval** (dry runs excepted). | Executor requires an `ApprovalRecord` whose hash matches the exact action. The state machine forbids `EXECUTING` without approval. The Action API also requires an `X-Approval-Id` header on non-dry-run calls. |
| R4 | Diagnosis must come **from telemetry only** — never from feature-flag settings, flag files, flag UI or flag API. | No agent tool talks to flagd / flag UI / flag files. flagd containers and envkit helper containers are excluded from tools. `feature_flag.*` attributes and flag-evaluation log lines are redacted. |
| R5 | **No hard-coding** answers to practice incidents. | Ground truth lives only in `eval/ground_truth.yaml`. `agent/` never imports `eval` or `envkit`, never reads `.faultctl/` or bundle `answer.json`. Prompts contain generic reasoning only. |
| R6 | Nothing outside our own laptop; nothing that affects other teams or the venue network; no attacks on real systems. | All endpoints are `localhost`. Config loader rejects non-local hosts. Action API binds to `127.0.0.1` only. |
| R7 | Proposed fixes must be **allowlisted** Action API operations. | Agent planner reads the allowlist from `GET /v1/actions`; anything not in it → escalate. Action API itself rejects unknown operations/targets. |
| R8 | Provide **dry-run mode** and a **kill switch**. | `--dry-run` is the default; `--live` required to execute. Kill switch = sentinel file + CLI command + SIGINT, checked before every step. Action API also has `POST /v1/killswitch`. |
| R9 | Every action call is logged. | Action API writes append-only JSONL; agent writes its own audit log per incident. |
| R10 | Every incident output follows **our JSON schema** (`schema/incident_report.schema.json`). | Reports are validated with `jsonschema` before being written. |
| R11 | All code written on the day from **09:15**; all **5 members** commit; AI coding assistants (e.g. Claude Code) declared in the team form; every member can explain their code. | Process rule — Sections 9–11. **Do not commit this plan or any code before 09:15 on the event day.** |
| R12 | Code freeze / hard cutoff **14:00 Day 2** (one slide says 16:30 — treat 14:00 as binding). | Final merge to main at 13:45 (Section 11, Block 6). |
| R13 | **Strict separation between the fault injector and the agent.** Because we wrote faultctl ourselves, judges may suspect the agent "knows" the answer. | Forbidden-access test (below). Helper containers created by faultctl carry the label `envkit.helper=true` and are invisible to agent tools. We state this separation in the README and team form. |

### Forbidden-access guard (`tests/test_forbidden_access.py`)

Scans every file under `agent/` and fails if it finds:
- any import of `envkit` or `eval`;
- the strings `flagd`, `demo.flagd.json`, `/feature`, `openfeature`, `.faultctl`, `answer.json`, `ground_truth` (case-insensitive);
- any known feature-flag name (list kept in the test file, not in agent code);
- Action API `POST` usage anywhere except `agent/execution/action_client.py`.

---

## 2. Project Summary

When an alert fires (error-rate or latency threshold breach), the agent must:

1. **Detect** — receive the alert and start investigating automatically.
2. **Investigate** — adaptively call telemetry tools (traces, metrics, logs, container health, deployment history),
   choosing the next tool based on what it has found. No giant data-dump prompts.
3. **Diagnose** — name the failing component and root cause, state confidence, cite exact evidence. Invented evidence is penalized.
4. **Propose** — choose one allowlisted Action API remedy with a short reason, or escalate.
5. **Approve** — show diagnosis + plan to a human and wait for an explicit yes. Dry run + kill switch available.
6. **Act & verify** — execute only the approved action, re-check telemetry, roll back where appropriate and escalate if not recovered.
7. **Report** — schema-compliant JSON + readable report: timeline, root cause, evidence, action, prevention.

### Incident classes the agent must reason about (generically)

| Class | Examples | Distinguishing telemetry |
|-------|----------|--------------------------|
| Application fault | payment fails on charge; cart fails intermittently; catalog fails for one product | Error spans + exception logs in one service; container healthy; errors concentrated on one span/attribute |
| Resource fault | memory leak (runaway cache); high CPU / heavy GC | Rising memory/CPU/GC metrics, latency growth, possible OOMKilled / restarts |
| Load fault | traffic flood on homepage | Request-rate spike at the frontend; latency/errors follow volume |
| Infra: container | stopped or restart-looping | Container not running, RestartCount rising, connection-refused in callers, missing spans |
| Infra: dependency | cache or DB unavailable | Callers fail on DB/cache client spans; connection errors in logs; dependency container unhealthy |
| Infra: network | latency or packet loss between two services | Latency on one service→service hop only; callee's server span is fast; timeouts/retries |
| Infra: starvation | CPU/memory starved | Near-limit usage / throttling with no code-level errors; slowness across that service |
| Combined / unseen | any mix | Multiple independent signals; agent says when evidence is insufficient |

**Key reasoning rule:** the first service showing errors is often *not* the root cause. Follow the failure downstream
to the deepest failing span, then check that component's logs and container health to separate app vs infra.

---

## 3. What We Build Ourselves (organizers are not providing it)

| Item from the brief | Our replacement | Owner | Location |
|---------------------|-----------------|-------|----------|
| Required JSON schema | Our own `incident_report.schema.json`, generated from pydantic models | D | `schema/`, `agent/models.py` |
| `faultctl` (inject / reset practice incidents) | Our own CLI: app faults via flagd, infra faults via Docker, network faults via `tc netem` sidecar | B | `envkit/faultctl/` |
| Action API (restart / toggle flag / throttle loadgen / scale) | Our own FastAPI service on `127.0.0.1:8700`, allowlisted, logged, with dry-run and kill switch | E | `envkit/action_api/` |
| Offline incident bundles | Our own recorder (`faultctl record`) + agent bundle backend | B | `envkit/faultctl/record.py`, `agent/tools/backends/bundle.py` |
| Deployment history | Action API `GET /v1/deployments` built from Docker events (image/create/start) + Action API action log | E (API), A (agent tool) | `envkit/action_api/deployments.py` |
| Alert source | Our own alert listener polling Prometheus | A | `agent/alerting/` |
| Practice incident list | The 10 incident types listed in the brief (Section 5.3 table) | B | `envkit/faultctl/scenarios.yaml` |

### Still to verify in the demo (Phase 1, record in `docs/DISCOVERY.md`)

| Item | How to find it | Default to try |
|------|----------------|----------------|
| Compose service/container names | `docker compose ps` | — |
| Prometheus URL | `docker compose ps` | `http://localhost:9090` |
| Jaeger query URL | `docker compose ps` | `http://localhost:16686` |
| Logs backend | `docker compose ps` (OpenSearch?) | fallback: Docker logs |
| Span-metric names (prefix varies by demo version) | `GET /api/v1/label/__name__/values` | `traces_span_metrics_*` |
| flagd flag file path + exact flag names | demo repo `src/flagd/` (**envkit only — never in agent**) | `src/flagd/demo.flagd.json` |
| Locust load generator API | `docker compose ps`; Locust web UI | `/loadgen/` via `:8080`, `POST /swarm`, `GET /stats/requests` |
| Images needed for network faults | pull **before** the event (venue network may be slow) | `gaiaadm/pumba` or `nicolaka/netshoot` |

### Questions still worth asking the organizers

- How will the **hidden test** inject faults, and how should they **trigger our agent**? (We support `iz watch` and `iz investigate --alert file.json`.)
- Is the 14:00 or 16:30 cutoff binding? Is overnight coding allowed between Day 1 and Day 2?
- Is a 5-person team officially confirmed (PDF says 3)?

---

## 4. Architecture

### 4.1 Design principles

- **Two halves:** `envkit/` simulates the operations environment; `agent/` is the AI on-call engineer. They meet only at telemetry and the Action API HTTP interface.
- **Separate the brain from the hands.** The LLM only gets *read-only* tools. Execution is deterministic code the LLM cannot call.
- **Evidence-first.** Every tool result gets an evidence ID (`E1`, `E2`, …). Diagnoses must cite IDs; a validator checks every cited fact exists.
- **Bounded and adaptive.** Step budget (default 12 tool calls) and token budget per tool output.
- **Explicit state machine** with guarded transitions.
- **Live and offline backends** for every telemetry tool.
- **Model-agnostic** LLM provider (organizer key or local/open-source model).

### 4.2 Component diagram

```
┌─────────────────────────── envkit/ (our environment kit) ───────────────────────────┐
│                                                                                      │
│  faultctl CLI ──inject/reset──▶ flagd flag file · Docker (stop/kill/limits) · netem   │
│      └── record ──▶ bundles/<scenario>/  (telemetry snapshot + answer.json)           │
│                                                                                      │
│  Action API (127.0.0.1:8700) ──▶ restart · toggle flag · throttle loadgen · scale*    │
│      ├── GET /v1/actions (allowlist)   ├── GET /v1/deployments   ├── JSONL action log │
│      └── dry-run · approval header · kill switch                       *501 on Compose│
└──────────────────────────────────────────────────────────────────────────────────────┘
                 ▲ HTTP only                                   │ affects
                 │                                             ▼
                 │                      OpenTelemetry Demo (Docker Compose) ──▶ Prometheus · Jaeger · logs
                 │                                                                     │
┌────────────────┼──────────────────────── agent/ ─────────────────────────────────────┼───┐
│                │                                                                     ▼   │
│  Alert Listener (generic rules: error rate, p95 latency, target down) ◀── Prometheus     │
│        │ Alert                                                                           │
│        ▼                                                                                 │
│  Orchestrator / State Machine · Kill Switch check every step · Audit Log                 │
│        │                                                                                 │
│        ├─▶ Investigator (LLM tool loop) ──▶ READ-ONLY Telemetry Tools ──▶ Evidence Store │
│        │                                    metrics · traces · logs · containers ·       │
│        │                                    deployments · topology (+ redaction)         │
│        │                                    backends: live | bundle                      │
│        ├─▶ Diagnoser + Evidence Validator (+ optional Reviewer)                          │
│        ├─▶ Planner (diagnosis → allowlisted action or ESCALATE)                          │
│        ├─▶ Approval Gate (human: approve <id> / reject / timeout; dry-run)               │
│        ├─▶ Executor ──▶ ActionClient ─────────────────── HTTP ──▶ Action API              │
│        ├─▶ Verifier (fresh telemetry) → RESOLVED or rollback + ESCALATE                  │
│        └─▶ Reporter (schema JSON + Markdown)                                             │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 State machine

```
ALERTED → INVESTIGATING → DIAGNOSED → PROPOSED → AWAITING_APPROVAL → EXECUTING → VERIFYING → RESOLVED   → REPORTED
                │              │           │              │                            ├──→ ROLLED_BACK → ESCALATED → REPORTED
                │              │           │              │                            └──→ ESCALATED   → REPORTED
                │              │           │              ├─ rejected / timeout ───────────→ ESCALATED   → REPORTED
                │              │           │              └─ approved in dry-run ──────────→ DRY_RUN_DONE → REPORTED
                │              │           └─ no safe allowlisted action ──────────────────→ ESCALATED   → REPORTED
                │              └─ low confidence / validator fails / conflicting evidence ──→ ESCALATED   → REPORTED
                └─ budget exhausted / no anomaly found ──────────────────────────────────→ ESCALATED or NO_INCIDENT → REPORTED

Any state ── kill switch ──→ HALTED → REPORTED
```

**Guards (enforced in `agent/state_machine.py`, tested):**
- `→ EXECUTING` only with an approved `ApprovalRecord` whose `action_hash` matches, kill switch off, mode `--live`.
- `→ RESOLVED` only if the Verifier returned `recovered=True` from fresh telemetry (not from the Action API response).
- `→ PROPOSED` only if confidence ≥ `policy.min_confidence_to_act` (default 0.7) **and** the evidence validator passed.
- Every transition is appended to the audit log with timestamp and reason.

---

## 5. Environment Kit Specifications (`envkit/` — we build these)

### 5.1 Incident Report JSON Schema — owner **D**

- Source of truth: pydantic model `IncidentReport` in `agent/models.py`.
- `iz schema export` writes `schema/incident_report.schema.json` (JSON Schema draft 2020-12) from the model.
  A test checks that the committed schema file equals the exported one (so they never drift).
- `iz validate reports/` validates every report with `jsonschema`.
- `schema/example_report.json` = one complete valid example (used in README and tests).

**Top-level fields (all required; use `null` where a stage didn't happen):**

| Field | Type | Content |
|-------|------|---------|
| `schema_version` | string | `"1.0"` |
| `incident_id` | string | e.g. `INC-20261023-1532-checkout` |
| `generated_at` | datetime | report creation time |
| `status` | enum | `resolved`, `escalated`, `rolled_back`, `halted`, `dry_run`, `no_incident` |
| `alert` | object | `id, fired_at, rule, service, metric, value, threshold, window` |
| `investigation` | object | `steps[]` (`n, ts, tool, rationale, query, evidence_ids`), `tool_calls`, `budget_exhausted` |
| `evidence` | array | `id, type (metric/trace/log/container/deploy/topology), service, source, timestamp, summary, data` |
| `diagnosis` | object \| null | `component, category, root_cause, causal_chain[], confidence (0–1), evidence_ids[], uncertainty, alternatives_considered[], insufficient_evidence` |
| `proposed_action` | object \| null | `action_id, operation, target, params, reason, expected_impact, risks, reversible, rollback_plan, action_hash` |
| `approval` | object \| null | `decision (approved/rejected/timeout), approver, ts, reason, mode (dry_run/live)` |
| `execution` | object \| null | `executed, dry_run, request, response, success, error, ts` |
| `verification` | object \| null | `performed, recovered, checks[] (name, before, after, threshold, passed), evidence_ids[]` |
| `rollback` | object \| null | `performed, operation, result` |
| `escalation` | object \| null | `escalated, reason, findings_summary` |
| `timeline` | array | `ts, event, detail` (built from the audit log) |
| `prevention` | array of string | suggestions only — never implemented |
| `limitations` | array of string | what the agent was unsure about / couldn't check |

`category` enum: `app_error, resource_exhaustion, load_spike, container_down, dependency_unavailable,
network_degradation, resource_starvation, multiple, unknown`.

### 5.2 Action API — owner **E** · `envkit/action_api/` · FastAPI on `127.0.0.1:8700`

The **only** way anything (agent or human) changes the running system during an incident.

| Method & path | Body / params | Behavior |
|---------------|---------------|----------|
| `GET /v1/health` | — | `{status, killswitch}` |
| `GET /v1/actions` | — | Allowlist catalog: each operation, allowed targets, params, `reversible`, description. For `toggle_feature_flag`, lists allowed flags with a `component` tag (runbook mapping) — **never their current state** |
| `POST /v1/actions/{operation}` | `{target, params, dry_run, reason}` + headers `X-Approval-Id`, `X-Request-Id` | Validates against allowlist. `dry_run=true` → returns exact planned effect, changes nothing. Live → requires `X-Approval-Id`, refuses if kill switch is on, executes, returns `{action_id, success, before, after, rollback_token}` |
| `POST /v1/actions/{action_id}/rollback` | `{rollback_token}` + `X-Approval-Id` | Reverses a reversible action (restore previous flag / user count / replicas) |
| `GET /v1/actions/log` | `?since=` | Action log entries (JSONL-backed) |
| `GET /v1/deployments` | `?since=&service=` | Deployment history: container create/start events + image tags + Action API actions. Excludes `envkit.helper` containers and resource-limit `update` events |
| `POST /v1/killswitch` | `{engaged: true/false}` | Global kill switch: all live actions refused while engaged |

**Operations (allowlist in `envkit/action_api/allowlist.yaml`):**

| Operation | Implementation | Reversible | Notes |
|-----------|----------------|------------|-------|
| `restart_service` | Docker SDK `container.restart()` by compose service label | No | Targets limited to demo services (not flagd, collector, Prometheus, Jaeger) |
| `toggle_feature_flag` | Set `defaultVariant` of an allowlisted flag in the flagd flag file; flagd hot-reloads | Yes | Only flags listed in the allowlist; previous value stored for rollback |
| `throttle_load_generator` | Locust web API `POST /swarm` with lower `user_count` | Yes | Previous user count stored for rollback |
| `scale_service` | Kubernetes only | — | Returns `501 Not Implemented` on Docker Compose (document as limitation) |

**Every call** (including dry runs and refusals) appends to `envkit/action_api/data/actions.jsonl`:
`{ts, request_id, approval_id, operation, target, params, dry_run, result, error, caller}`.
Binds to `127.0.0.1` only. Simple shared token from `.env` (`X-API-Token`).

### 5.3 faultctl — owner **B** · `envkit/faultctl/` · CLI `faultctl`

| Command | Behavior |
|---------|----------|
| `faultctl list` | Show available scenarios from `scenarios.yaml` |
| `faultctl inject <scenario> [<scenario> …]` | Inject one or several (combined) faults; saves undo info to `.faultctl/state.json` |
| `faultctl status` | Show currently injected faults (**humans only — agent never reads this**) |
| `faultctl reset` | Undo everything: restore flag file, start stopped containers, stop restart loops, remove netem sidecars, restore CPU/memory limits |
| `faultctl record <scenario> --minutes 5 --out bundles/<id>/` | Inject → wait → capture telemetry for **all** services → write bundle → reset |
| `faultctl random [--n 2]` | Inject a random scenario or combination (used to test generalization without the tester knowing the answer) |

**Practice scenarios (`scenarios.yaml`) — the 10 incident types from the brief:**

| # | Scenario id | Mechanism | Category (eval ground truth only) |
|---|-------------|-----------|-----------------------------------|
| 1 | `payment-failure` | flag (payment fails on charge) | app_error |
| 2 | `cart-intermittent` | flag (cart fails intermittently) | app_error |
| 3 | `catalog-one-product` | flag (one product fails) | app_error |
| 4 | `recommendation-leak` | flag (runaway cache memory leak) | resource_exhaustion |
| 5 | `ad-high-cpu` | flag (high CPU / manual GC) | resource_exhaustion |
| 6 | `homepage-flood` | flag (load generator floods homepage) | load_spike |
| 7 | `container-stopped` / `container-restart-loop` | `docker stop` / background loop of `docker kill` | container_down |
| 8 | `cache-down` / `db-down` | stop cache (valkey/redis) or DB container | dependency_unavailable |
| 9 | `network-latency` / `network-loss` | `tc netem` via helper container sharing the target's network namespace | network_degradation |
| 10 | `cpu-starve` / `memory-starve` | `docker update --cpus 0.1` / low `--memory` | resource_starvation |

- Flag names and target containers are configured in `scenarios.yaml` (verify flag names against the demo's flag file in Phase 1).
- Helper containers get label `envkit.helper=true`. Background loops store their PID in `.faultctl/state.json`.
- `reset` must be safe to run twice and must work even if `state.json` is missing (best-effort full restore).
- The "Category" column is copied into `eval/ground_truth.yaml` by D — it is **not** read by the agent.

### 5.4 Offline bundles — owner **B**

```
bundles/<scenario-id>-<timestamp>/
├── manifest.json        # time window, services, what files exist (NO answer)
├── answer.json          # scenario id + category — read ONLY by eval/scorer.py
├── prometheus/*.json    # range-query results for a fixed generic query set, all services
├── jaeger/traces.json   # recent traces for every service (errors + slowest + sample)
├── jaeger/dependencies.json
├── logs/<service>.log   # last N minutes per service (flagd + helpers excluded)
├── containers.json      # inspect (filtered) + stats snapshots
└── deployments.json     # GET /v1/deployments snapshot
```

The recorder captures the **same generic data for every scenario** — it must not choose queries based on the scenario.

### 5.5 Deployment history — owner **E** (API) and **A** (agent tool)

The demo has no real CI/CD, so "deployment history" = container create/start events with image tags and timestamps
(from Docker events, collected by the Action API in the background) + Action API actions. This lets the agent check
"did anything change right before the incident?" without seeing faultctl's injections.

---

## 6. Agent Component Specifications (`agent/`)

### 6.1 Alert Listener — owner **A** · `agent/alerting/`
- Polls Prometheus every 15 s with **generic per-service rules** (never incident-specific): error ratio over 1 min > 5%;
  p95 latency over 1 min > 1500 ms or > 3× rolling baseline; scrape target / container down.
- Needs 2 consecutive breaches (debounce). Also accepts `iz investigate --alert alert.json` (for manual/hidden-test triggering).
- Output `Alert{id, fired_at, rule, service, metric, value, threshold, window}` + a **baseline snapshot** for verification. Never changes anything.

### 6.2 Telemetry Tools — owners **A** (metrics, traces, topology, extra) · **B** (logs, redaction, docker wrapper) · **D** (containers)
Every tool has typed input and returns `ToolResult{evidence_id, tool, query, summary, key_facts[], raw_ref, error}`.
`summary` ≤ ~1,500 tokens; raw output saved to `runs/<id>/raw/<evidence_id>.json`. All output passes through `redaction.py`.

| Tool (LLM-facing) | Backend | Owner | Purpose |
|-------------------|---------|-------|---------|
| `get_service_overview` | Prometheus | A | Error rate, request rate, p50/p95/p99 for all services — usual first call |
| `query_metric` | Prometheus | A | Templated PromQL only (rate, quantile, CPU, memory, GC); max range enforced |
| `list_metric_names` | Prometheus | A | Discover metric names |
| `search_traces` | Jaeger | A | Error/slow traces for a service/operation |
| `get_trace` | Jaeger | A | One trace as a span tree; marks deepest failing span; groups errors by attribute (e.g. product id) |
| `get_service_dependencies` | Jaeger | A | Caller→callee topology |
| `compare_hop_latency` | Jaeger | A | Client-span vs server-span duration per hop (network detector) |
| `get_container_resources` | Docker stats / Prometheus | A | CPU %, memory vs limit, throttling |
| `get_deployment_history` | Action API `GET /v1/deployments` | A | Recent changes vs incident start |
| `search_logs` | logs backend or Docker logs | B | Error/warn lines per service, deduplicated with counts |
| `get_container_health` | Docker (read-only wrapper) | D | Status, health, RestartCount, OOMKilled, started_at, exit code |

Safety: `agent/tools/docker_ro.py` exposes only `list/inspect/stats/logs`; filters env vars and mounts from inspect;
excludes flagd, flag UI and `envkit.helper=true` containers. `redaction.py` strips `feature_flag.*` attributes and
flag-evaluation log lines. HTTP timeouts 5 s, one retry; failure returns `ToolResult.error` (a signal, not a crash).
Bundle backend (`agent/tools/backends/bundle.py`, owner B) serves the same tools from a bundle and never opens `answer.json`.

### 6.3 Evidence Store & Validator — owner **D** · `agent/evidence/`
- Store assigns `E1…En`; keeps tool, query, timestamp, summary, `key_facts` (machine-checkable atoms), raw path.
- Validator: every cited ID exists; every quoted number / trace ID / log excerpt appears in cited evidence; at least one
  cited item references the claimed component; confidence ≥ 0.8 requires ≥ 2 different evidence types.
  Fail → agent asked once to fix; second fail → confidence capped / ESCALATE.

### 6.4 Investigator — owner **C** · `agent/investigation/`
LLM tool loop: check kill switch → LLM picks a tool (with a one-line rationale) or FINISH → run tool → store evidence →
append compact summary → update hypotheses. Stops at FINISH or step budget (→ escalate with partial findings).
Prompt (`agent/llm/prompts/investigator.md`) = **generic SRE reasoning only**: start broad then narrow; pick the tool that
best separates hypotheses; follow errors downstream; separate app vs infra; intermittent → use windows and several traces;
targeted → check attribute concentration; load → compare request rate to baseline; "insufficient evidence" is allowed.

### 6.5 Diagnoser — owner **D** · `agent/diagnosis/`
Structured-output LLM call → `Diagnosis` (fields in 5.1) → validator → confidence policy.
Stretch: `reviewer.py` tries to disprove the diagnosis; strong objection → up to 3 extra steps or lower confidence.

### 6.6 Planner — owner **C** · `agent/planning/`
Reads the allowlist from `GET /v1/actions`. Deterministic category → candidate operation mapping:

| Category | Candidate action | Otherwise |
|----------|------------------|-----------|
| container_down, resource_exhaustion | `restart_service(component)` | escalate |
| load_spike | `throttle_load_generator` | escalate |
| app_error | `toggle_feature_flag` **only if** the catalog lists a flag tagged with the diagnosed component (runbook mapping; flag state is never read) | escalate |
| resource_starvation | `scale_service` (Kubernetes only) | escalate on Compose |
| dependency_unavailable | `restart_service(dependency)` if the dependency container is down | escalate |
| network_degradation, multiple, unknown | — | escalate with findings |

Then a short LLM call (or template) writes reason, expected impact, risks, rollback plan.
Output `ActionProposal` with `action_hash = sha256(canonical_json(operation, target, params))`.

### 6.7 Approval Gate — owner **E** · `agent/approval/`
CLI screen: diagnosis + evidence table, exact action, impact/risks/rollback, mode (DRY RUN / LIVE).
Human types `approve <action_id>` or `reject [reason]`; timeout 10 min = reject. Writes `ApprovalRecord`.
Approved in dry-run → Action API `dry_run=true` → `DRY_RUN_DONE`. Stretch (B): small chat/web approval UI writing the same record.

### 6.8 Kill Switch — owner **E** · `agent/execution/killswitch.py`
`iz kill` (sentinel file `.iz_kill`), env `IZ_KILL=1`, SIGINT/SIGTERM; also calls Action API `POST /v1/killswitch`.
Checked before every LLM call, tool call and action call → `HALTED`. `iz unkill` clears it.

### 6.9 Executor & ActionClient — owner **E** · `agent/execution/`
`ActionClient` = the only agent code calling Action API `POST`. Executor re-checks hash, allowlist, kill switch, live mode;
executes **exactly once**. On API error: record it, inspect state with read-only tools, never claim recovery → escalate.
`FakeActionClient` for tests.

### 6.10 Verifier & Rollback — owner **A** · `agent/verification/`
Settle 30 s, poll up to 180 s: alert signals back under threshold and near baseline for 3 consecutive polls, container
running/healthy, no new error-log burst. Verification results are stored as evidence.
Not recovered → reversible action: rollback via Action API (pre-authorized only if stated on the approval screen,
otherwise ask again) → `ROLLED_BACK` → `ESCALATED`; irreversible → `ESCALATED`.

### 6.11 Reporter — owner **B** · `agent/reporting/`
`IncidentReport` → validate against schema → `reports/<incident_id>.json` + `reports/<incident_id>.md`
(summary, timeline, root cause, evidence table, investigation steps with rationale, action + approval, verification,
rollback/escalation, prevention suggestions, limitations).

### 6.12 Audit Log — owner **E** · `agent/audit/`
Append-only JSONL per incident `runs/<id>/audit.jsonl`: alert, tool_call, llm_call (tokens/cost), diagnosis, validation,
proposal, approval, action_request, action_result, verification, rollback, escalation, kill, report.

---

## 7. Repository Layout

```
incident-zero/
├── CLAUDE.md                     # this plan
├── README.md                     # submission README (Phase 5)
├── ARCHITECTURE.md · EVALUATION.md · TEAM_FORM.md
├── pyproject.toml · Makefile · .env.example
├── config/settings.yaml          # one labeled section per member
├── docs/DISCOVERY.md · docs/architecture.png
├── schema/incident_report.schema.json · schema/example_report.json
├── envkit/                       # OUR ENVIRONMENT KIT — agent never imports this
│   ├── action_api/  {app.py, allowlist.yaml, operations/{restart.py, flags.py, loadgen.py, scale.py},
│   │                 deployments.py, actionlog.py, killswitch.py, data/actions.jsonl}
│   └── faultctl/    {cli.py, scenarios.yaml, app_faults.py, infra_faults.py, network_faults.py,
│                     record.py, state.py}
├── agent/
│   ├── cli.py · config.py · models.py · state_machine.py · orchestrator.py
│   ├── llm/ {provider.py, fake.py, prompts/*.md}
│   ├── alerting/ {listener.py, rules.py}
│   ├── tools/ {base.py, registry.py, fakes.py, redaction.py, docker_ro.py, metrics.py, traces.py,
│   │           topology.py, logs.py, containers.py, resources.py, deployments.py,
│   │           backends/{live.py, bundle.py}}
│   ├── evidence/ {store.py, validator.py}
│   ├── investigation/investigator.py
│   ├── diagnosis/ {diagnoser.py, policy.py, reviewer.py}
│   ├── planning/ {runbook.py, planner.py}
│   ├── approval/ {gate.py, cli_ui.py}
│   ├── execution/ {action_client.py, executor.py, killswitch.py}
│   ├── verification/ {verifier.py, rollback.py}
│   ├── reporting/ {report.py, templates/report.md.j2}
│   └── audit/logger.py
├── eval/                         # NEVER imported by agent/
│   ├── ground_truth.yaml · scorer.py · run_eval.py · results.md
├── bundles/                      # recorded offline incidents
├── reports/                      # generated incident reports (deliverable)
├── runs/                         # raw evidence + audit logs (gitignored except demo runs)
└── tests/
```

### CLI

| Command | Purpose |
|---------|---------|
| `make demo-up` / `make demo-down` | Start/stop the OTel demo |
| `make action-api` | Start the Action API on `127.0.0.1:8700` |
| `faultctl list / inject / status / reset / record / random` | Fault injection (humans and eval only) |
| `iz watch [--live]` | Alert listener → full pipeline on alert |
| `iz investigate --alert alert.json [--backend bundle --bundle DIR] [--live]` | Pipeline from a given alert |
| `iz tools <name> <args>` | Call one telemetry tool manually |
| `iz kill` / `iz unkill` | Kill switch |
| `iz schema export` / `iz validate reports/` | Schema export / validation |
| `iz eval [--backend bundle]` | Run practice incidents, score vs ground truth, write `eval/results.md` |

Dry run is the default everywhere; `--live` is required to execute.

### Key config (`config/settings.yaml`)

```yaml
endpoints: {prometheus: http://localhost:9090, jaeger: http://localhost:16686, logs: auto, action_api: http://127.0.0.1:8700}
excluded_services: [flagd, flagd-ui]
excluded_labels: ["envkit.helper=true"]
llm: {provider: anthropic|openai|ollama, model: TBD, temperature: 0, max_cost_usd: 5}
investigation: {max_steps: 12, max_tool_output_tokens: 1500, lookback_minutes: 15}
policy: {min_confidence_to_act: 0.7, high_confidence_requires_types: 2}
alert: {poll_s: 15, error_rate_threshold: 0.05, p95_latency_ms: 1500, debounce_polls: 2}
verify: {settle_s: 30, timeout_s: 180, consecutive_ok: 3}
approval: {timeout_s: 600}
mode: {dry_run_default: true}
```

### Tech stack
Python 3.11 · `httpx` · `pydantic` v2 · `fastapi` + `uvicorn` (Action API) · `docker` SDK · `jsonschema` · `typer` + `rich` ·
`jinja2` · `pyyaml` · `pytest` · LLM SDK of choice (or `litellm`) · optional `streamlit`.
A ~150-line custom tool loop is easier to explain to judges than a heavy agent framework.

---

## 8. Data Contracts (`agent/models.py` — owner D, sketch)

```python
class Alert(BaseModel):
    id: str; fired_at: datetime; rule: str; service: str
    metric: str; value: float; threshold: float; window: str

class KeyFact(BaseModel):
    type: Literal["metric","trace","log","container","deploy","topology"]
    service: str | None = None
    data: dict[str, Any]

class ToolResult(BaseModel):
    evidence_id: str; tool: str; query: dict; ts: datetime
    summary: str; key_facts: list[KeyFact]; raw_ref: str; error: str | None = None

class Diagnosis(BaseModel):
    component: str | None; category: Category; root_cause: str
    causal_chain: list[str]; confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]; uncertainty: str
    alternatives_considered: list[str]; insufficient_evidence: bool

class ActionProposal(BaseModel):
    action_id: str; operation: str; target: str; params: dict; reason: str
    expected_impact: str; risks: str; reversible: bool
    rollback_plan: str | None; action_hash: str

class ApprovalRecord(BaseModel):
    action_id: str; action_hash: str
    decision: Literal["approved","rejected","timeout"]
    approver: str; ts: datetime; reason: str | None = None; mode: Literal["dry_run","live"]

class VerificationResult(BaseModel):
    recovered: bool; checks: list[dict]; evidence_ids: list[str]

class IncidentReport(BaseModel):   # = the JSON schema (Section 5.1)
    ...
```

Interfaces (also D, in Block 1): `Tool` protocol (`agent/tools/base.py`), `LLMProvider` protocol (`agent/llm/provider.py`),
`ActionClient` protocol (`agent/execution/action_client.py`). Each owner ships a **fake** next to their interface
(`FakeTool`, `FakeLLM`, `FakeActionClient`, `FakeVerifier`) so nobody waits for anybody.

---

## 9. Team & Ownership (5 members)

| Member | Role | Owns |
|--------|------|------|
| **A** | Eyes-1: metrics, traces & recovery | `agent/tools/{metrics,traces,topology,resources,deployments}.py`, `agent/alerting/`, `agent/verification/`, forbidden-access test, DISCOVERY.md |
| **B** | Eyes-2 & environment: faults, logs, bundles, reports | `envkit/faultctl/`, `agent/tools/{logs,redaction,docker_ro}.py`, `agent/tools/backends/bundle.py`, `agent/reporting/`, `bundles/` |
| **C** | Brain-1: LLM, investigator & planner | Project skeleton, `agent/llm/`, `agent/investigation/`, `agent/planning/`, `agent/cli.py` |
| **D** | Brain-2: data model, evidence, diagnosis & eval | `agent/models.py`, `schema/`, `agent/evidence/`, `agent/diagnosis/`, `agent/tools/containers.py`, `eval/` |
| **E** | Hands: Action API & safety | `envkit/action_api/`, `agent/state_machine.py`, `agent/execution/`, `agent/approval/`, `agent/audit/`; merges dev → main |

**Shared-file rules:** D owns `models.py` (others request changes). Each member edits only their own labeled section of
`config/settings.yaml`. Announce new dependencies before adding them to `pyproject.toml`. C owns `Makefile`/`pyproject.toml` structure.

**Review pairs** (quick look before each merge into dev): A↔B, C↔D, E↔C.

---

## 10. Git Workflow

### Branches

```
main      ← always working; only E merges dev → main, only at checkpoints; organizers evaluate this
 └── dev  ← integration branch; everyone merges finished tasks here
      ├── a/* b/* c/* d/* e/*   ← one short-lived branch per task, cut from dev
      ├── fix/*                 ← bug fixes (only code branches allowed after 12:00 Day 2)
      └── docs/*                ← deliverables
```

### Commit vs merge

- **Commit** = save work on *your own branch*. As often as you like (every 20–30 min). Nobody else is affected.
- **Merge into dev** = share a finished task. Once per task, by the time in Section 11.

```bash
# START a task (always from the latest dev)
git checkout dev && git pull
git checkout -b a/metrics-tool          # exact branch name from Section 11

# SAVE work (repeat)
git add . && git commit -m "feat(tools): add prometheus query"
git push -u origin a/metrics-tool

# FINISH the task (by the merge time)
git checkout dev && git pull
git merge a/metrics-tool
pytest -q                               # must pass
git push

# CHECKPOINT (E only)
git checkout main && git pull && git merge dev
git tag cp1-eyes && git push && git push --tags
```

### Rules
- Never push to `main` directly (except E at checkpoints). Never force-push `dev` or `main`.
- Commit under your own name; never squash teammates' commits (commit history proves individual contribution).
- Pull `dev` into your branch at least every hour (`git merge dev` while on your branch).
- Never edit files owned by another member.
- Commit message format: `type(scope): message` — e.g. `feat(action-api): add restart`, `fix(approval): reject on timeout`.
- Delete branches after merging.

**Tags on main:** `cp0-setup` · `cp1-eyes` · `cp2-brain` · `day1-end` · `cp3-hands` · `final-submission`.

---

## 11. Schedule — who does what, on which branch, merged by when

Scoring recap: CP1 Eyes 10 · CP2 Brain 20 · CP3 Hands 20 · CP4 Hidden test 30 · Judges 20 = 100.
All times are merge-into-dev deadlines. Commit on your branch as often as you like before that.

### Phase 0 · Before the event · NO code, NO commits
- Install Docker + Compose v2, Python 3.11. Clone the OTel demo, `docker compose pull`.
- **Pull network-fault images in advance:** `docker pull gaiaadm/pumba` (or `nicolaka/netshoot`).
- Run the demo once; look at the flag page only to understand failures (familiarization).
- Read the LLM tool-calling primer. Choose LLM provider/model and set a spend limit. Everyone reads this plan.

---

### DAY 1

#### Block 1 · Setup · 12:00–13:00 → Checkpoint 0
C merges first (folder structure), D second (data shapes). A, B and E start with work that needs no shared code.

| Person | Work | Branch | Merge into dev by |
|---|---|---|---|
| **C** | Project skeleton: `agent/` + `envkit/` folders, `pyproject.toml`, Makefile, `.env.example`, one passing test | `c/repo-skeleton` | **12:20 (first)** |
| **D** | `models.py` data shapes + `Tool`/`LLMProvider`/`ActionClient` protocols with fakes | `d/models` | **12:35 (second)** |
| **A** | Start demo with load; record URLs, metric names, compose service names, Locust API in `docs/DISCOVERY.md` | `a/telemetry-discovery` | 12:40 |
| **A** | Forbidden-access test (Section 1) | `a/forbidden-access-test` | 12:55 |
| **B** | faultctl CLI skeleton: `list / inject / status / reset` for flag-based app faults; `scenarios.yaml` | `b/faultctl-app-faults` | 12:55 |
| **C** | LLM provider: send one test message to the model; `FakeLLM` | `c/llm-provider` | 12:50 |
| **D** | Incident report JSON schema: `IncidentReport` model, `iz schema export`, `iz validate`, example report | `d/incident-schema` | 12:55 |
| **E** | Action API skeleton: FastAPI on 127.0.0.1:8700, `/v1/health`, `GET /v1/actions`, `restart_service`, dry-run, JSONL log, token | `e/action-api-skeleton` | 12:55 |

**12:55–13:00:** E merges dev → main, tag `cp0-setup`. All 5 members now have commits.

#### Block 2 · The agent can see · 13:00–15:30 → Checkpoint 1 (10 pts)

| Person | Work | Branch | Merge into dev by |
|---|---|---|---|
| **A** | Metrics tool: service overview + templated PromQL + list metric names | `a/metrics-tool` | 14:00 |
| **A** | Trace tool: search traces, trace span tree, deepest failing span, dependencies | `a/trace-tool` | 15:00 |
| **B** | faultctl infra faults: container stop, restart loop, cache/DB down, CPU/memory starve; full `reset` | `b/faultctl-infra-faults` | 14:15 |
| **B** | Redaction filter + read-only Docker wrapper (`docker_ro.py`) | `b/redaction` | 14:40 |
| **B** | Log tool: error/warn lines per service, deduplicated with counts | `b/log-tool` | 15:00 |
| **C** | `iz tools` command to call any tool by hand | `c/cli-tools` | 14:00 |
| **C** | First investigator: alert → 2–4 adaptive tool calls → symptom summary with evidence IDs | `c/investigator-v0` | **15:05 (after A, B, D tools)** |
| **D** | Evidence store (IDs E1, E2…, raw files on disk) | `d/evidence-store` | 14:00 |
| **D** | Container health tool (status, restarts, OOM, exit code) | `d/container-tool` | 14:50 |
| **D** | One-page architecture sketch (+ whiteboard photo) | `docs/architecture-sketch` | 15:15 |
| **E** | Action API operations: throttle load generator, toggle feature flag, scale → 501, rollback endpoint, kill switch endpoint | `e/action-api-actions` | 14:15 |
| **E** | Agent audit logger | `e/audit-logger` | 14:45 |
| **E** | State machine (states + transitions, no guards yet) | `e/state-machine` | 15:10 |

**15:05–15:30:** Live run: `faultctl inject` one scenario → agent calls tools → symptom summary. E merges dev → main, tag `cp1-eyes`.

#### Block 3 · The agent can diagnose · 15:30–18:30 → Checkpoint 2 (20 pts)

| Person | Work | Branch | Merge into dev by |
|---|---|---|---|
| **A** | Alert listener: generic rules, debounce, baseline snapshot, `iz watch` | `a/alert-listener` | 16:30 |
| **A** | Extra tools: hop latency (network), CPU/memory/GC resources, deployment history tool | `a/extra-tools` | 17:45 |
| **B** | faultctl network faults: latency + packet loss via netem helper container | `b/faultctl-network-faults` | 16:15 |
| **B** | `faultctl record` + bundle backend for agent tools | `b/bundles` | 17:15 |
| **B** | Report v0: schema JSON + Markdown (diagnosis part) | `b/report-v0` | 18:00 |
| **C** | Full investigator loop: hypotheses, step/token budget, kill-switch checks | `c/investigator-loop` | 17:30 |
| **C** | Improve prompts — generic reasoning only, no incident answers | `c/prompts` | 18:00 |
| **D** | Evidence validator (rejects invented evidence) | `d/validator` | 16:45 |
| **D** | Diagnoser: component, category, cause, confidence, evidence, uncertainty | `d/diagnoser` | 17:30 |
| **D** | Eval harness: `eval/ground_truth.yaml`, scorer, `iz eval`, `eval/results.md` | `d/eval-harness` | 18:00 |
| **E** | Kill switch (`iz kill`, sentinel, SIGINT, Action API endpoint) | `e/killswitch` | 16:15 |
| **E** | Deployment history API (`GET /v1/deployments` from Docker events) | `e/deploy-history-api` | 17:00 |
| **E** | State-machine guards + safety tests | `e/guards-and-safety-tests` | 18:00 |

**18:00–18:30:** Run 3 practice incidents end-to-end to diagnosis. E merges dev → main, tag `cp2-brain`.
**18:45:** E tags `day1-end`. No overnight commits unless organizers confirm it's allowed.

---

### DAY 2

**09:00–09:30:** Alignment — review Day 1 feedback, `eval/results.md`, and today's plan.

#### Block 4 · The agent can act safely · 09:30–12:00 → Checkpoint 3 (20 pts)

| Person | Work | Branch | Merge into dev by |
|---|---|---|---|
| **C** | Planner: diagnosis → one allowlisted action (from `GET /v1/actions`) or escalate | `c/planner` | 10:30 |
| **D** | Confidence policy: low confidence / failed validation / conflicts → escalate | `d/confidence-policy` | 10:30 |
| **B** | Record bundles for all 10 practice scenarios + 2 combinations | `b/practice-bundles` | 10:30 |
| **E** | Approval gate: approve `<id>` / reject / timeout, dry-run path | `e/approval` | 10:30 |
| **E** | Executor + Action API client (hash check, exactly-once, failure handling) | `e/executor` | 11:15 |
| **A** | Verifier: fresh telemetry checks against baseline | `a/verifier` | 11:00 |
| **A** | Rollback + escalation path | `a/rollback` | 11:30 |
| **B** | Full report: timeline, action, approval, verification, rollback, prevention | `b/full-report` | 11:15 |
| **D / B** | *Optional, only if everything above works:* reviewer agent / chat approval UI | `d/reviewer`, `b/chat-ui` | 11:30 or skip |

**11:30–12:00:** One full live run: alert → diagnosis → approval → Action API call (logged) → verified recovery → report.
Also show rejection, dry run and kill switch. E merges dev → main, tag `cp3-hands`.
(Good CP3 scenarios: `container-stopped` → restart, or `homepage-flood` → throttle.)

#### Block 5 · Fix and document · 12:00–13:30 · NO NEW FEATURES

| Person | Work | Branch | Merge into dev by |
|---|---|---|---|
| **Everyone** | Fix bugs from `iz eval` and live runs (wrong/unsafe first, then crashes, then cosmetics) | `fix/<what-you-fix>` | As soon as fixed |
| **A** | Final `ARCHITECTURE.md` (one page) | `docs/architecture` | 13:20 |
| **B** | Submission `README.md`: setup, demo, faultctl, Action API, agent, approval/dry run/kill switch, verification, schema example, limitations | `docs/readme` | 13:20 |
| **C** | Demo script + example reports in `reports/` | `docs/demo` | 13:20 |
| **D** | `EVALUATION.md`: each practice incident right/wrong/escalated and why | `docs/evaluation` | 13:20 |
| **E** | `TEAM_FORM.md`: team name, 5 members, contribution split, AI tools incl. Claude Code, libraries | `docs/team-form` | 13:20 |

#### Block 6 · Submit · 13:30–14:00

- **13:30** one person clones the repo fresh and runs it from scratch.
- **13:40** `iz validate reports/` and `pytest -q` pass.
- **13:45** E merges dev → main, tag `final-submission`.
- **13:50** everyone checks on GitHub that the last commit is visible.
- **14:00** code freeze. Nothing after this counts.

#### Block 7 · Hidden test & finalist demo · 14:00–18:45
Don't change code. Keep `make demo-up`, `make action-api`, `iz watch` ready for organizers. Rehearse the demo (Section 15).

---

## 12. If you need someone's module before they've merged it

1. **Don't wait.** Code against the data shapes in `models.py` and the protocol, and test with the owner's fake
   (`FakeTool`, `FakeLLM`, `FakeActionClient`, `FakeVerifier`) or sample data in `tests/fixtures/`.
2. When the real module lands in dev: `git checkout dev && git pull`, then on your branch `git merge dev`, and switch from the fake to the real one.
3. Stuck more than 15 minutes → tell the owner and ask them to merge a small working version early.
4. Never copy or edit their files on your branch.

**Dependency chain** (provider merges before consumer at integration time):
```
c/repo-skeleton → d/models
   ├─ tools (A, B, D) → d/evidence-store → c/investigator → d/validator → d/diagnoser → d/confidence-policy
   ├─ c/llm-provider ─────────────────────────────┘
   ├─ e/action-api-* → e/deploy-history-api → a/extra-tools (deployment tool)
   └─ e/state-machine → e/killswitch → e/guards → c/planner → e/approval → e/executor → a/verifier → a/rollback → b/full-report
```

**2-minute standup every 60–90 minutes:** what I merged · what I'm merging next · what blocks me.

---

## 13. Testing Strategy

### Unit / safety tests (all must pass before every merge to main)

| Test | Owner | Asserts |
|------|-------|---------|
| `test_forbidden_access.py` | A | No forbidden strings/imports in `agent/`; only `action_client.py` uses Action API POST |
| `test_state_machine.py` | E | No EXECUTING without matching approval; RESOLVED requires verification; kill → HALTED from every state |
| `test_approval.py` | E | Reject / timeout → no execution; hash mismatch → refused |
| `test_dry_run.py` | E | Dry run never performs a live Action API call |
| `test_killswitch.py` | E | Sentinel / env / SIGINT / API switch all halt before next step |
| `test_action_api.py` | E | Unknown operation/target rejected; live call without `X-Approval-Id` rejected; every call logged; binds to localhost |
| `test_faultctl.py` | B | Inject → status → reset restores state; reset is idempotent; helper containers labeled |
| `test_redaction.py` | B | `feature_flag.*`, flagd and helper containers never reach the LLM |
| `test_bundle_backend.py` | B | Tools work from a bundle; `answer.json` is never opened |
| `test_validator.py` | D | Fabricated ID / number / uncited component → rejected or confidence capped |
| `test_schema.py` | D | Committed schema == exported schema; every report validates |
| `test_executor_failure.py` | E | API error → no recovery claimed → escalation |
| `test_verifier.py` | A | Action "succeeds" but telemetry still bad → not recovered → rollback/escalate |
| `test_config_localhost.py` | C | Non-localhost endpoints rejected |

### Scenario matrix (`iz eval` + live runs)

| Scenario | Expected agent behavior |
|----------|-------------------------|
| Known app fault | Correct service via telemetry, evidence cited, allowlisted remedy or justified escalation |
| Intermittent fault | Uses windows / several traces; no over-claiming from one sample |
| Single-product failure | Targeted failure, not broad outage |
| CPU / memory problem | Resource metrics correlated with symptoms |
| Container stopped / restarting | Container health used; app code not blamed |
| Cache / DB unavailable | Downstream dependency traced; logs/health corroborate |
| Network latency / loss | Hop latency compared; first slow service not blamed |
| Combined (`faultctl inject a b`) / `faultctl random` | Reasons from evidence; reports multiple causes or insufficiency |
| No fault (control) | No action; `no_incident` |
| Low confidence / conflicting evidence | Escalates without acting |
| Approval rejected | No execution; rejection recorded |
| Action API failure | Failure reported; no recovery claimed |
| Action didn't recover | Rollback where possible; escalate |
| Kill switch / dry run | Halts / previews only |

**Generalization test:** one member runs `faultctl random` without telling the others which fault it is; the agent's
answer is compared afterwards. This is the closest thing to the hidden test (30 pts).
Track everything in `eval/results.md` → feeds `EVALUATION.md` and the optional confidence-calibration analysis.

---

## 14. Final Readiness Checklist (before 13:45 Day 2)

- [ ] Fresh clone works: `make demo-up`, `make action-api`, `iz watch` / `iz investigate`
- [ ] faultctl inject/reset works for all 10 scenarios; bundles recorded
- [ ] Agent adaptively queries traces, metrics, logs, container health, deployment history (rationale logged)
- [ ] Diagnosis: component, cause, confidence, exact evidence; validator on; no invented evidence
- [ ] Only allowlisted Action API operations, each with a short reason
- [ ] Explicit approval, dry run, kill switch all demonstrably work; no change before approval
- [ ] Every action logged (agent audit log + Action API log); recovery verified from telemetry; failure → rollback/escalate
- [ ] All reports validate against `schema/incident_report.schema.json`; Markdown reports present
- [ ] Practice incidents + combinations tested; forbidden-access test green; envkit/agent separation documented
- [ ] Deliverables: source + README, ARCHITECTURE.md, reports/, EVALUATION.md, TEAM_FORM.md
- [ ] README explains that **we built** the schema, faultctl, Action API and bundles because they weren't provided
- [ ] AI coding assistants (Claude Code etc.) declared in team form
- [ ] All 5 members have commits and can explain their modules

---

## 15. Five-minute Live Demo Script

| Time | Show |
|------|------|
| 0:00–0:30 | Clean environment; `faultctl inject <scenario>`; alert fires — state the symptom |
| 0:30–1:30 | Agent choosing tools live with one-line rationale per step; real trace/log/metric output |
| 1:30–2:15 | Diagnosis: component, cause, confidence, evidence IDs → point at the exact trace/log line/metric |
| 2:15–2:45 | Proposed allowlisted action + why it fits; dry-run preview |
| 2:45–3:30 | Explicit approval (`approve <id>`); call visible in the Action API log |
| 3:30–4:15 | Verifier: metrics back to baseline → RESOLVED (or show escalation path) |
| 4:15–5:00 | JSON report (schema-valid) + readable report; mention envkit/agent separation and one honest limitation |

Backup: an offline bundle run if the live demo misbehaves (say so openly).

---

## 16. Common Pitfalls (avoid)

- Dumping all logs/metrics into one prompt instead of adaptive tool calls.
- Naming a cause without exact, checkable evidence.
- Blaming the first erroring service.
- Reading flag settings, faultctl state, bundle answers — or leaking them via spans, logs, env vars or helper containers.
- Letting the LLM invent fixes or run arbitrary commands.
- Treating "Action API returned 200" as proof of recovery.
- Executing without approval, or executing something different from what was approved.
- Hard-coding practice answers or tuning prompts with incident-specific rules.
- Spending too long polishing envkit — it only needs to be reliable, not fancy. The agent is what gets scored.
- Starting stretch goals before the full loop and safety controls work.

---

## 17. Status Tracker (Claude Code: update at the end of every task)

**Current block:** Phase 0 — Prep
**Last updated:** _(date / time / member)_

| Block | Status | Notes |
|-------|--------|-------|
| 0 Prep | ☐ | Images pulled incl. pumba/netshoot? |
| 1 Setup (cp0) | ☐ | DISCOVERY.md filled? All 5 committed? |
| 2 Eyes (cp1) | ☐ | |
| 3 Brain (cp2) | ☐ | Practice incidents correct: _/10 |
| 4 Hands (cp3) | ☐ | |
| 5 Fix & docs | ☐ | |
| 6 Submit (14:00) | ☐ | Final commit SHA: |
| 7 Hidden test & demo | ☐ | |

**Open questions for organizers:** hidden-test injection + agent trigger method · 14:00 vs 16:30 cutoff ·
overnight coding allowed? · 5-person team confirmed?

**Known limitations (keep honest; copy into submission README in Block 5):**
- `scale_service` is not available on Docker Compose (Action API returns 501).
- _(add as discovered)_
