# Incident Zero — AI On-Call Engineer

> **Build plan, architecture and phase-wise execution guide.**
> This file is written so it can be handed directly to **Claude Code** (or any coding agent) to continue the work.
> Don't copy this whole file into `CLAUDE.md`. It's too long to load every session, and its status tracker changes
> constantly. Keep `CLAUDE.md` short and point it here.
> In Phase 5 this plan moves to `PLAN.md`, and `README.md` becomes the submission README (setup, running, approval
> gate, kill switch, limitations).
> **Team of 5:** Hemanth, Aditi, Tvisha, Mohit, Pavan. **Organizers told us to build `faultctl`, the Action API and
> the JSON schema ourselves.** `faultctl` is done (`harness/faultctl.py`); we're building the other two.

**Theme:** Agentic AI for IT Operations — *from alert to safe, approved fix.*
**Target system:** OpenTelemetry Demo ("Astronomy Shop"), running locally with Docker Compose.
**Core principle:** a good agent does not always act. It acts when evidence supports the action, and says
"I'm not sure" and escalates when it does not.

---

## 0. Instructions for Claude Code (read this first, every session)

1. **Read Section 1 (Hard Rules) before writing any code.** Violating them costs points or disqualifies the team.
2. **Check Section 13 (Status Tracker)** to see which phase we are in and what is done. Update it at the end of every task.
3. **We define `faultctl`, the Action API and the JSON schema ourselves** (organizer instruction). They live in
   `harness/faultctl.py`, `action_api/` and `schema/incident.schema.json`. The hidden test may still use the
   organizers' own faults, so the agent must **never depend on our `faultctl` incident names**. Record
   facts about the environment in `docs/DISCOVERY.md`. If something is unknown, ask the user rather than guess.
4. **Work phase by phase.** Do not start a later phase's features until the current phase's checkpoint criteria pass.
   After 12:00 on Day 2, **no new features** — bug fixes, tests and docs only.
5. **Keep changes small and committed often.** Each of the 5 team members must commit their own work. Do not squash
   other members' commits. Use commit messages like `feat(tools): add prometheus query tool`.
   **Git flow:** branch from `dev` (`<name>/<area>`), open PRs into `dev`, and Hemanth merges `dev` → `main` only
   after a checkpoint run passes. Stick to your own files to avoid conflicts.
6. **Every module gets a test.** Run `pytest -q` before declaring a task done.
7. **When in doubt about safety, choose the safer option** (dry run, escalate, refuse) and leave a `# SAFETY:` comment.
8. **Be honest in docs.** If something does not work, write it in `EVALUATION.md` / Known Limitations. Hiding failures loses points.

---

## 1. Hard Rules (official — non-negotiable)

These come directly from the challenge brief. Code must enforce them, not just document them.

| # | Rule | How we enforce it in code |
|---|------|---------------------------|
| R1 | Never edit demo source code, demo configuration, `faultctl`, or the scoring harness. | Agent has no write tools. No file-write or shell tools exposed to the LLM. Docker client wrapper is read-only. `harness/` (faultctl) is test tooling: `agent/` must never import, call or read it. |
| R2 | The system may only be changed through the **Action API**. | `execution/action_client.py` is the *only* module allowed to make state-changing calls. Test asserts no other module imports it. |
| R3 | No state-changing action without **explicit human approval** (dry runs excepted). | Executor requires an `ApprovalRecord` whose hash matches the exact action payload. State machine forbids `EXECUTING` without `APPROVED`. |
| R4 | Diagnosis must come **from telemetry only** — never from feature-flag settings, flag files, the flag UI or the flag API. | No tool talks to flagd / flag UI / flag API. `flagd` and flag-UI containers are excluded from log/container tools. `feature_flag.*` span attributes and flag-evaluation log lines are redacted. A test greps `agent/` for forbidden strings. |
| R5 | **No hard-coding** answers to practice incidents. | Ground truth lives only in `eval/ground_truth.yaml`, imported only by `eval/scorer.py`. Test asserts `agent/` never reads it. Prompts contain generic reasoning, not incident→answer maps. |
| R6 | Nothing outside our own laptop; nothing that affects other teams or the venue network; no attacks on real systems. | All endpoints configured as `localhost` in `config/settings.yaml`; config loader rejects non-local hosts for telemetry and Action API. |
| R7 | Proposed fixes must be **allowlisted** Action API operations. | `planning/runbook.py` holds a fixed allowlist mirrored from the Action API. Planner output is validated against it; anything else → escalate. |
| R8 | Provide **dry-run mode** and a **kill switch**. | `--dry-run` is the default; `--live` required to execute. Kill switch = sentinel file + CLI command + SIGINT handler, checked before every step. |
| R9 | Every action call is logged. | Append-only JSONL audit log in `runs/<incident_id>/audit.jsonl`. |
| R10 | Every incident output must follow the **provided JSON schema**. | `reporting/schema_adapter.py` maps our internal model to the schema; output is validated with `jsonschema` before being written. |
| R11 | All code written on the day, from **09:15**; all 5 members commit; AI coding assistants declared in the team form; every member can explain the code. | Process rule — see Section 8 (team split) and Phase 5 (team form). |
| R12 | Code freeze / hard cutoff: **14:00 Day 2** (one slide says 16:30 — treat 14:00 as binding until organizers confirm). | Phase 6 finishes the final push by 13:50. |

### Forbidden-strings guard (implemented as `tests/test_forbidden_access.py`)

The test scans every file under `agent/` and fails if it finds any of:
`flagd`, `demo.flagd.json`, `/feature`, `/flagservice`, `flagd-ui`, `openfeature` (case-insensitive), ports `8013`,
`8016`, `4000`, `faultctl`, any import of `harness`, the ground-truth file name, or any import of `eval.`.
(Keep the list in the test file, not in agent code.) Exception: `agent/tools/common.py` may name `flagd`/`flagd-ui`
in `EXCLUDED_SERVICES`. Allowlist that one line in the test.

---

## 2. Project Summary

When an alert fires (error-rate or latency threshold breach), the agent must:

1. **Detect** — receive the alert and start investigating automatically.
2. **Investigate** — adaptively call telemetry tools (traces, metrics, logs, container health, deployment history),
   choosing the next tool based on what it has found. No giant data-dump prompts.
3. **Diagnose** — name the failing component and root cause, state confidence, cite exact evidence
   (trace IDs, log lines, metric values). Invented evidence is penalized.
4. **Propose** — choose one allowlisted Action API remedy with a short reason, or escalate.
5. **Approve** — show diagnosis + plan to a human and wait for an explicit yes. Dry run + kill switch available.
6. **Act & verify** — execute only the approved action, re-check telemetry, roll back where appropriate and escalate if not recovered.
7. **Report** — schema-compliant JSON + readable report: timeline, root cause, evidence, action, prevention.

### Incident classes the agent must reason about (generically)

| Class | Examples | Distinguishing telemetry |
|-------|----------|--------------------------|
| Application fault | payment fails on charge; cart fails intermittently; catalog fails for one product | Error spans + exception logs in one service; container healthy; error concentrated on a specific span/attribute |
| Resource fault | memory leak (runaway cache); high CPU / heavy GC | Rising memory/CPU metrics, GC metrics, latency growth, possible OOMKilled / restarts |
| Load fault | traffic flood on homepage | Request rate spike at the frontend; latency/errors follow volume; downstream services healthy per request |
| Infra: container | stopped or restart-looping | Container state ≠ running, RestartCount rising, connection-refused errors in callers, missing spans |
| Infra: dependency | cache or DB unavailable | Callers' spans fail on DB/cache client spans; connection errors in logs; dependency container unhealthy |
| Infra: network | latency or packet loss between two services | Latency on one service→service hop only; callee's own server span is fast; timeouts/retries |
| Infra: starvation | CPU/memory starved | Throttling / near-limit usage with no code-level errors; slowness across that service's endpoints |
| Combined / unseen | any mix | Multiple independent signals; agent must say when evidence is insufficient |

**Key reasoning rule:** the first service showing errors is often *not* the root cause. Follow the failure downstream
to the deepest failing span, then check that component's logs and container health to separate app vs infra.

---

## 3. Discovery — environment facts (verified against the cloned demo)

Organizers told us to **define `faultctl`, the Action API and the JSON schema ourselves**. Record any new findings
in `docs/DISCOVERY.md` and `config/settings.yaml`.

| Item | Value |
|------|-------|
| JSON schema | **Ours:** `schema/incident.schema.json`. Hemanth builds it by Checkpoint 2, from the fields in Section 9 (`IncidentRecord`) |
| Action API | **Ours:** `action_api/`, a small local HTTP service. Endpoints: `restart_service`, `toggle_feature_flag` (disabled by default, see 5.6), `throttle_load_generator`. `scale_service` is Kubernetes-only, so disabled on Compose. It logs every call to an append-only JSONL. Built Day 2 morning |
| Action API dry-run | Ours: `dry_run=true` on every endpoint returns the exact call without executing |
| `faultctl` | **Ours, done:** `python harness/faultctl.py list / inject <name> / status / reset`. App faults switch the demo's built-in flags; infra faults use Docker (stop, CPU limit, netem latency). Test tooling only |
| Alert source | We detect it ourselves: an alert watcher polling Prometheus. Also accept `--alert alert.json` in case organizers feed alerts |
| Hidden-test entrypoint | **Unknown. Ask organizers** (CLI? HTTP? file drop?). Critical for 30 pts |
| Deployment history | No deploy system in the demo. Fallback: container image tags + `StartedAt` / `RestartCount` from read-only `docker inspect` |
| Offline bundles | None provided. Optional: record our own with `faultctl` + tool outputs |
| Prometheus | `http://localhost:9090` (exposed on host). Span metrics: `traces_span_metrics_calls_total` (labels `service_name`, `span_name`, `status_code`), `traces_span_metrics_duration_milliseconds_bucket`. Container CPU/mem from the collector's `docker_stats` receiver. Confirm names with `GET /api/v1/label/__name__/values` |
| Jaeger | **Not on host :16686** (container port not mapped). Use the frontend proxy: `http://localhost:8080/jaeger/ui/api/traces`, `/api/services`, `/api/dependencies` |
| Logs backend | Collector exports logs to **OpenSearch** (container port 9200, no fixed host port, not proxied). Options: read-only `docker port opensearch 9200`, Grafana datasource proxy at `localhost:8080/grafana/`, or `docker logs`. **Never edit compose files to expose it** (R1) |
| Docker read access as "container health" | Assumed yes, read-only (`list`, `inspect`, `stats`, `logs`) |
| Final deadline | 14:00 Day 2 (assume binding) |

Note: the frontend proxy on `http://localhost:8080` also serves `/feature` and `/flagservice/`, which are the flag UI
and flag API. **The agent must never call either.** The flag UI's own port is `4000`, flagd's are `8013`/`8016`.

---

## 4. Architecture

### 4.1 Design principles

- **Separate the brain from the hands.** The LLM only ever gets *read-only* tools. Planning produces a proposal
  (data). Execution is deterministic Python code that the LLM cannot call.
- **Evidence-first.** Every tool result is stored with an evidence ID (`E1`, `E2`, …). The diagnosis must cite
  evidence IDs, and a validator checks that every cited ID exists and every quoted value appears in the raw result.
- **Bounded and adaptive.** The investigation loop has a step budget (default 12 tool calls) and a token budget per
  tool output. Tools return compact summaries; raw data is saved to disk.
- **Explicit state machine.** Guarded transitions make unsafe paths impossible, not just unlikely.
- **Live and offline backends.** Every telemetry tool has a `live` backend and a `bundle` backend (recorded telemetry),
  so development and evaluation work even when the full demo can't run.
- **Model-agnostic.** A thin LLM provider interface supports the organizer API key and local/open-source models.

### 4.2 Component diagram

```
                    ┌──────────────────────────────────────────────────────────┐
  Prometheus ──────▶│ Alert Listener (rules: error rate, p95 latency, up==0)   │
  (or organizer     └───────────────┬──────────────────────────────────────────┘
   alert source)                    │ Alert
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Orchestrator / State Machine  (ALERTED → INVESTIGATING → … → REPORTED)       │
│  checks Kill Switch before every step · writes Audit Log · owns IncidentState │
└──────┬─────────────────────────┬──────────────────────┬──────────────────────┘
       │                         │                      │
       ▼                         ▼                      ▼
┌──────────────────┐   ┌───────────────────┐   ┌────────────────────────────┐
│ Investigator     │   │ Diagnoser         │   │ Planner (runbook mapping)  │
│ (LLM tool loop)  │──▶│ (LLM, structured) │──▶│ diagnosis → allowlisted    │
│ READ-ONLY tools  │   │ + Evidence        │   │ action or ESCALATE         │
└──────┬───────────┘   │   Validator       │   └─────────────┬──────────────┘
       │               │ + (opt) Reviewer  │                 │ ActionProposal
       ▼               └───────────────────┘                 ▼
┌──────────────────┐                               ┌────────────────────────┐
│ Telemetry Tools  │                               │ Approval Gate (human)  │
│ metrics  traces  │                               │ approve / reject /     │
│ logs  containers │                               │ dry-run · hash-bound   │
│ deploys topology │                               └───────────┬────────────┘
│  └ redaction ────│ (strips feature_flag.* etc.)              │ ApprovalRecord
│ live | bundle    │                                           ▼
└──────┬───────────┘                               ┌────────────────────────┐
       ▼                                           │ Executor → ActionClient│──▶ Action API
┌──────────────────┐                               │ (ONLY state changer)   │
│ Evidence Store   │◀──────────────────────────────┴───────────┬────────────┘
│ E1..En + raw     │                                           ▼
└──────────────────┘                               ┌────────────────────────┐
                                                   │ Verifier (telemetry)   │
                                                   │ recovered? → RESOLVED  │
                                                   │ else rollback+ESCALATE │
                                                   └───────────┬────────────┘
                                                               ▼
                                                   ┌────────────────────────┐
                                                   │ Reporter: schema JSON  │
                                                   │ + Markdown report      │
                                                   └────────────────────────┘
```

### 4.3 State machine

```
ALERTED → INVESTIGATING → DIAGNOSED → PROPOSED → AWAITING_APPROVAL → EXECUTING → VERIFYING → RESOLVED   → REPORTED
                │              │           │              │               │            ├──→ ROLLED_BACK → ESCALATED → REPORTED
                │              │           │              │               │            └──→ ESCALATED   → REPORTED
                │              │           │              ├─ rejected ───────────────────→ ESCALATED   → REPORTED
                │              │           │              └─ dry-run ────────────────────→ DRY_RUN_DONE → REPORTED
                │              │           └─ no safe allowlisted action ───────────────→ ESCALATED   → REPORTED
                │              └─ low confidence / validator fails / conflicting evidence → ESCALATED   → REPORTED
                └─ budget exhausted ────────────────────────────────────────────────────→ ESCALATED   → REPORTED

Any state ── kill switch ──→ HALTED → REPORTED
```

**Guards (enforced in `state_machine.py`, tested):**

- `→ EXECUTING` only if an `ApprovalRecord(decision="approved")` exists, its `action_hash` equals the hash of the
  proposal being executed, kill switch is off, and mode is `--live`.
- `→ RESOLVED` only if the Verifier returned `recovered=True` from fresh telemetry (not from the Action API response).
- `→ PROPOSED` only if diagnosis confidence ≥ `policy.min_confidence_to_act` (default 0.7) **and** the evidence validator passed.
- Every transition is appended to the audit log with timestamp and reason.

---

## 5. Component Specifications

### 5.1 Alert Listener — `agent/alerting/`

- Polls Prometheus every 15 s (configurable) with **generic, per-service** rules — never incident-specific rules:
  - error ratio over 1 min > `alert.error_rate_threshold` (default 5%) for any service;
  - p95 latency over 1 min > `alert.p95_latency_ms` (default 1500 ms) or > 3× its rolling baseline;
  - scrape target / container down.
- Requires the condition for 2 consecutive polls (debounce) to avoid single-sample alerts.
- Also accepts an alert from a file or HTTP POST (`iz investigate --alert alert.json`) so organizer alerts or the
  hidden-test harness can trigger it. **Adapt to the harness entrypoint recorded in DISCOVERY.md.**
- Output: `Alert{id, fired_at, rule, service, metric, value, threshold, window}`. Never changes anything.
- Records a **baseline snapshot** (key metrics for the alerting service) for later verification.

### 5.2 Telemetry Tools — `agent/tools/` (all read-only)

Every tool: typed input (pydantic), returns `ToolResult{evidence_id, tool, query, summary, key_facts[], raw_ref}`.
`summary` is short (≤ ~1,500 tokens); full raw output is written to `runs/<id>/raw/<evidence_id>.json`.
Every tool passes its output through `tools/redaction.py` before the LLM sees it.

| Tool name (LLM-facing) | Backend (live) | Purpose |
|------------------------|----------------|---------|
| `get_service_overview` | Prometheus | Error rate, request rate, p50/p95/p99 latency for all services over a window — the usual first call |
| `query_metric` | Prometheus `query_range` | Arbitrary PromQL restricted to a safe template set (rate, histogram_quantile, CPU, memory, GC) |
| `list_metric_names` | Prometheus label API | Discover available metric names (span-metrics prefix differs between demo versions) |
| `search_traces` | Jaeger `/api/traces` | Traces for a service/operation with `error=true` or `minDuration`, limited count |
| `get_trace` | Jaeger `/api/traces/{id}` | One trace summarized as a span tree: service, operation, duration, status, error message; marks the deepest failing span |
| `get_service_dependencies` | Jaeger `/api/dependencies` | Caller→callee graph (topology) to reason about propagation |
| `compare_hop_latency` | Jaeger | For a caller→callee pair, compare client-span vs server-span duration (network latency detector) |
| `search_logs` | logs backend or Docker logs | Error/warn lines for a service in a time window, deduplicated with counts |
| `get_container_health` | Docker Engine (read-only) | Status, health, RestartCount, OOMKilled, started_at, exit code for one or all services |
| `get_container_resources` | Docker stats / Prometheus | CPU %, memory usage vs limit, throttling |
| `get_deployment_history` | per DISCOVERY.md (fallback: image tags + container start times) | Recent changes correlated with incident start |

**Safety details for tools:**

- The Docker wrapper (`tools/docker_ro.py`) exposes only `list`, `inspect`, `stats`, `logs`. It must never expose
  `restart`, `stop`, `exec`, `kill`, `update`. Excluded containers: flagd, flag UI, anything flag-related (config list).
- `inspect` output is filtered: drop environment variables and mounts (they may reveal configuration).
- `redaction.py` removes span attributes / log fields whose key or message matches `feature_flag`, `flagd`,
  `openfeature` and drops spans from excluded services. This keeps diagnosis honestly telemetry-based (R4).
  *Ask organizers if they consider flag-evaluation span attributes off-limits; we redact them to be safe.*
- `query_metric` only accepts PromQL built from templates (no free-form strings into URLs) and enforces max range/step.
- All HTTP clients have timeouts (5 s) and retry once; failures return a `ToolResult` with `error` set (the agent
  should notice "telemetry unavailable" as a signal, not crash).

**Offline backend:** `tools/backends/bundle.py` serves the same tool interface from an offline incident bundle.
Select with `--backend bundle --bundle path/`. All tests and the eval harness use this backend.

### 5.3 Evidence Store & Validator — `agent/evidence/`

- `EvidenceStore` assigns IDs sequentially (`E1`…), stores tool, query, timestamp, summary, key_facts, raw path.
- `key_facts` are machine-checkable atoms, e.g.
  `{"type":"metric","service":"checkout","name":"error_ratio","value":0.42,"window":"5m"}`,
  `{"type":"trace","trace_id":"ab12…","span":"PaymentService/Charge","status":"ERROR","message":"…"}`,
  `{"type":"log","service":"cart","ts":"…","line":"…","count":37}`,
  `{"type":"container","service":"redis","status":"exited","restart_count":5}`.
- `EvidenceValidator.validate(diagnosis)` checks:
  1. every cited evidence ID exists;
  2. every claim uses a **structured citation** `{evidence_id, fact_index}` that points at a real `key_facts` entry.
     Don't parse numbers out of free text ("42%" vs `0.42` is brittle); generate the prose from verified facts;
  3. at least one cited evidence item references the claimed root-cause component;
  4. confidence ≥ 0.8 requires ≥ 2 *different* evidence types (e.g. trace + log, metric + container).
  Failure → diagnosis is downgraded (confidence capped) or the agent is asked once to fix it; second failure → ESCALATE.

### 5.4 Investigator — `agent/investigation/investigator.py`

LLM tool-calling loop:

```
context = alert + baseline + service list + tool specs + investigation policy (generic)
loop up to max_steps:
    check kill switch
    LLM → either a tool call (with a one-line "why this tool" rationale) or FINISH
    run tool → store evidence → append compact summary to context
    keep a running "hypotheses" list (LLM updates it each step)
stop when LLM says FINISH, or budget exhausted (→ escalate with partial findings)
```

The system prompt (`llm/prompts/investigator.md`) contains **generic SRE reasoning only**:

- Start broad (service overview), then narrow to the alerting service and its dependencies.
- Prefer the tool that best *discriminates* between current hypotheses.
- Follow errors downstream in traces to the deepest failing span; the first erroring service is often a victim.
- Separate app vs infra: container down/restarting → infra; exceptions in a healthy container → app;
  slow client span but fast server span on one hop → network; high CPU/memory near limit → resource.
- Intermittent failures: look at rates over a window and multiple traces; never conclude from one sample.
- Targeted failures: check whether errors concentrate on one operation or one attribute value (e.g. one product ID).
- Load: check request rate against baseline before blaming a service.
- It's fine to conclude "insufficient evidence".

Each step's rationale is logged — this is what we show judges ("visible, explainable tool use").

### 5.5 Diagnoser — `agent/diagnosis/`

- Separate LLM call with structured output (pydantic / JSON mode):
  `Diagnosis{component, category, root_cause, causal_chain[], confidence (0–1), evidence_ids[], uncertainty, alternatives_considered[], insufficient_evidence: bool}`.
- `category` ∈ `{app_error, resource_exhaustion, load_spike, container_down, dependency_unavailable, network_degradation, resource_starvation, multiple, unknown}`.
- Run the Evidence Validator. Apply the confidence policy.
- **Stretch — Reviewer** (`reviewer.py`): a second LLM pass that tries to *disprove* the diagnosis using the same
  evidence (contradictions, missing checks, victim-vs-cause). If it finds a strong objection, either run up to 3
  extra investigation steps or lower confidence.

### 5.6 Planner / Runbook — `agent/planning/`

- `runbook.py` = **static allowlist** mirroring the Action API (fill exact names from DISCOVERY.md):

| Action | Typical fit (category) | Reversible? | Rollback |
|--------|------------------------|-------------|----------|
| `restart_service(service)` | container_down, resource_exhaustion (leak), stuck process | No | Escalate if not recovered |
| `toggle_feature_flag(flag, state)` | **Disabled by default.** Choosing which flag to toggle without reading flag state is close to breaking R4. Enable only if the team decides how to map a telemetry diagnosis to a flag without reading flag config | Yes | Toggle back |
| `throttle_load_generator(level)` | load_spike | Yes | Restore previous level |
| `scale_service(service, replicas)` | resource_starvation / load (Kubernetes only, so **disabled on Docker Compose**) | Yes | Scale back |

- Planner: deterministic mapping category → candidate actions, then a short LLM call (or rules) to fill params and
  write a 1–2 sentence reason + expected impact + risks. Output validated against the allowlist and against known
  service names. Categories like `network_degradation` or `dependency_unavailable` with no fitting action → **ESCALATE**
  with findings (that is a correct outcome).
- Output: `ActionProposal{action_id, operation, params, reason, expected_impact, risks, reversible, rollback_plan, action_hash}`.
  `action_hash = sha256(canonical_json(incident_id, action_id, operation, target_service, params))`, so an
  approval can never be reused for a different incident or service.

### 5.7 Approval Gate — `agent/approval/`

- CLI (rich) screen shows: (1) diagnosis + evidence table with IDs, (2) exact action + params + reason,
  (3) expected impact / risks / rollback plan, (4) mode (DRY RUN or LIVE), (5) prompt.
- The human must type `approve <action_id>` (not just Enter) or `reject [reason]`. Timeout (default 10 min) = reject.
- Writes `ApprovalRecord{action_id, action_hash, decision, approver, ts, reason}` to the audit log.
- If approved in dry-run mode → executor prints the exact call it *would* make and stops (`DRY_RUN_DONE`).
- **Stretch:** a small Streamlit/FastAPI chat UI that writes the same `ApprovalRecord` — same validation path.

### 5.8 Kill Switch — `agent/execution/killswitch.py`

- Engaged by any of: `iz kill` (creates `.iz_kill` file), env `IZ_KILL=1`, SIGINT/SIGTERM.
- `check()` is called before every LLM call, every tool call, and immediately before the Action API call.
- Engaged → transition to `HALTED`, no further actions, report generated with status halted. `iz unkill` clears it.

### 5.9 Executor & Action Client — `agent/execution/`

- `ActionClient` is the **only** code that calls the Action API. Interface:
  `execute(operation, params) -> ActionResult`, `dry_run(operation, params) -> str`.
  Until the real API is known, implement `FakeActionClient` for tests.
- `Executor.run(proposal, approval)` re-checks: hash match, allowlist, kill switch, live mode. Executes **exactly once**.
  On API error: record failure, inspect current state via read-only tools, do **not** auto-retry a different action,
  do **not** claim recovery → escalate.
- Every call (request, response, latency, error) is logged to the audit log.

### 5.10 Verifier — `agent/verification/`

- Waits a settle period (default 30 s), then polls for up to `verify.timeout_s` (default 180 s).
- Re-runs the **same signals that triggered the alert** plus root-cause signals: error ratio / p95 back under
  threshold (and near baseline) for 3 consecutive polls, container running & healthy, no new error-log burst.
- Returns `VerificationResult{recovered, checks[], evidence_ids[]}` — verification evidence goes into the evidence store too.
- Not recovered → if action reversible, propose rollback (rollback of an approved action is pre-authorized only if the
  approval screen stated it; otherwise ask again) → `ROLLED_BACK` → `ESCALATED`.

### 5.11 Reporter — `agent/reporting/`

- Internal `IncidentRecord` (our model) → `schema_adapter.to_schema()` → validated with `jsonschema` against the
  our schema (`schema/incident.schema.json`) → `reports/<incident_id>.json`.
- Readable `reports/<incident_id>.md`: summary, timeline (from audit log), root cause, evidence table (IDs, quotes),
  investigation steps with rationale, action + approval, verification result, rollback/escalation, prevention suggestions,
  limitations/uncertainty.
- Prevention section = suggestions only (alerting, config, code) — never implemented by the agent.

### 5.12 Audit Log — `agent/audit/`

Append-only JSONL per incident: `{ts, incident_id, event, state_from, state_to, detail}`. Events: alert, tool_call,
llm_call (tokens/cost), diagnosis, validation, proposal, approval, action_request, action_result, verification,
rollback, escalation, kill, report.

---

## 6. Repository Layout (target)

```
incident-zero/
├── CLAUDE.md                  # this plan (for Claude Code)
├── README.md                  # submission README (from template, Phase 5)
├── ARCHITECTURE.md            # one-page architecture overview (deliverable 2)
├── EVALUATION.md              # evaluation note (deliverable 4)
├── TEAM_FORM.md               # team form (deliverable 5) — or the template's form
├── pyproject.toml             # Python 3.11, deps, `iz` console script
├── Makefile                   # setup, demo-up, agent, test, eval, kill, validate
├── config/
│   └── settings.yaml          # endpoints, thresholds, budgets, policy, excluded services
├── docs/
│   ├── DISCOVERY.md           # environment facts (ports, metric names)
│   └── architecture.png       # whiteboard photo acceptable
├── agent/
│   ├── cli.py                 # `iz` commands
│   ├── config.py
│   ├── models.py              # Alert, ToolResult, Diagnosis, ActionProposal, ApprovalRecord, IncidentRecord …
│   ├── state_machine.py
│   ├── orchestrator.py
│   ├── llm/ {provider.py, prompts/*.md}
│   ├── alerting/ {listener.py, rules.py}
│   ├── tools/ {common.py (shared result format, DONE), base.py, registry.py, redaction.py, docker_ro.py, metrics.py, traces.py, logs.py,
│   │           containers.py, deployments.py, topology.py, backends/{live.py, bundle.py}}
│   ├── evidence/ {store.py, validator.py}
│   ├── investigation/ investigator.py
│   ├── diagnosis/ {diagnoser.py, reviewer.py}
│   ├── planning/ {runbook.py, planner.py}
│   ├── approval/ {gate.py, cli_ui.py}
│   ├── execution/ {action_client.py, executor.py, killswitch.py}
│   ├── verification/ verifier.py
│   ├── reporting/ {schema_adapter.py, report.py, templates/report.md.j2}
│   └── audit/ logger.py
├── harness/                   # test tooling, NEVER imported or read by agent/
│   └── faultctl.py            # inject / reset practice incidents (DONE)
├── action_api/                # our Action API: the ONLY way to change the system (Day 2)
├── eval/                      # NEVER imported by agent/
│   ├── ground_truth.yaml      # practice incident → expected component/category (from published list)
│   ├── scorer.py
│   └── run_eval.py
├── schema/                    # our JSON schema: incident.schema.json
├── tests/
├── reports/                   # generated incident reports (deliverable 3)
└── runs/                      # per-run raw evidence + audit logs (gitignored except demo runs)
```

### CLI (`iz`)

| Command | Purpose |
|---------|---------|
| `iz watch [--live]` | Run alert listener; on alert, run the full pipeline |
| `iz investigate --alert alert.json [--backend bundle --bundle DIR]` | Pipeline from a given alert |
| `iz run ... --dry-run` (default) / `--live` | Dry run is default; `--live` required to execute actions |
| `iz kill` / `iz unkill` | Kill switch |
| `iz validate reports/` | Validate all reports against the schema |
| `iz eval --set practice` | Run practice incidents (bundles), score vs ground truth, write `eval/results.md` |
| `iz tools <name> <args>` | Call one telemetry tool manually (debug + Checkpoint 1 demo) |

### Key config (`config/settings.yaml`)

```yaml
endpoints: {prometheus: http://localhost:9090, jaeger: http://localhost:8080/jaeger/ui, logs: docker, action_api: http://localhost:8090}
excluded_services: [flagd, flagd-ui]          # never queried
llm: {provider: anthropic|openai|ollama, model: TBD, temperature: 0, max_cost_usd: 5}
investigation: {max_steps: 12, max_tool_output_tokens: 1500, lookback_minutes: 15}
policy: {min_confidence_to_act: 0.7, high_confidence_requires_types: 2}
alert: {poll_s: 15, error_rate_threshold: 0.05, p95_latency_ms: 1500, debounce_polls: 2}
verify: {settle_s: 30, timeout_s: 180, consecutive_ok: 3}
approval: {timeout_s: 600}
mode: {dry_run_default: true}
```

### Tech stack

Python 3.11 · `httpx` · `pydantic` v2 · `docker` SDK (wrapped read-only) · `jsonschema` · `rich` + `typer` (CLI) ·
`jinja2` (reports) · `pytest` · LLM SDK of choice (or `litellm` for model-agnostic) · optional `streamlit`.
Avoid heavy agent frameworks unless the team already knows one — a ~150-line custom tool loop is easier to explain to judges.

---

## 7. Phase-wise Plan (mapped to checkpoints and scoring)

Scoring recap: CP1 Eyes 10 · CP2 Brain 20 · CP3 Hands 20 · CP4 Hidden test 30 · Judges 20 = 100.
The hidden test is the largest single item, so **generalization beats polish**. Every phase ends with a
"Definition of Done" (DoD) that must pass before moving on.

### Phase 0 — Prep (before the event · NO code, NO commits)

- Install Docker + Compose v2, Python 3.11. Clone the OTel demo and `docker compose pull` (≈14 GB disk, ≈6 GB RAM;
  on 8 GB laptops use minimal mode or offline bundles).
- Run the demo once; open the flag page *only* to understand a failure end-to-end (familiarization only).
- Read the LLM tool-calling primer. Each member reads this plan and their role in Section 8.
- Decide LLM provider/model and confirm a spend limit. Decide who owns which modules.
- **DoD:** every laptop can start the demo (or has bundles); everyone knows their role. *Nothing committed.*

### Phase 1 — Setup & Discovery · Day 1 12:00–13:00 → **Checkpoint 0 (13:00)**

| Task | Owner |
|------|-------|
| Repo skeleton, shared tool result format (`agent/tools/common.py`), `faultctl`, architecture sketch | Hemanth (DONE) |
| Start demo with load; confirm Jaeger, Prometheus, logs reachable; confirm metric names | Aditi |
| Run `faultctl` inject → observe → reset on one practice incident | Hemanth |
| LLM provider smoke test; cost tracking stub | Pavan |
| `tests/test_forbidden_access.py` + `config/settings.yaml` with localhost guard | Mohit |
| **Every member makes at least one commit** | all |

**DoD:** demo + load running; faultctl inject/reset works; LLM key works; 5 committers.

### Phase 2 — "The agent can see" · Day 1 13:00–15:30 → **Checkpoint 1: Eyes (15:30, 10 pts)**

**Must have by 15:30** (in parallel, each person in their own file):
1. Aditi: `agent/tools/metrics.py` (`get_service_overview`, error rate, p95 latency) + minimal `alerting/rules.py`.
2. Tvisha: `agent/tools/traces.py` (`search_traces`, `get_trace` with deepest failing span).
3. Mohit: `agent/tools/containers.py` + `logs.py` (`get_container_health`, `search_logs`) through read-only `docker_ro.py`.
4. Pavan: investigator v0: alert → overview → 2–4 adaptive LLM tool calls → **symptom summary** citing evidence.
5. Hemanth: architecture sketch (`docs/ARCHITECTURE.md`, DONE as a draft), integration, live run with `faultctl`.

**Can slip to Phase 3:** `models.py`/evidence store (use the dict format in `common.py` until then), `redaction.py`,
`get_deployment_history`, `get_service_dependencies`, `query_metric`, `iz tools` CLI, bundle backend.

**DoD / demo for CP1:** inject one practice incident live → alert fires (or is fed) → agent calls tools with visible
rationale → prints a symptom summary with evidence IDs → show architecture sketch.

### Phase 3 — "The agent can diagnose" · Day 1 15:30–18:30 → **Checkpoint 2: Brain (18:30, 20 pts)**

1. Full investigator loop with hypotheses tracking, step/token budgets, kill-switch checks.
2. Diagnoser with structured output + `evidence/validator.py` + confidence policy + `unknown`/escalate path.
3. Define **our JSON schema** `schema/incident.schema.json` (from Section 9 `IncidentRecord`), then
   `reporting/schema_adapter.py` + `jsonschema` validation + markdown report v0 (diagnosis part only).
4. Specialized tools for hard classes: `compare_hop_latency` (network), resource metrics (CPU/memory/GC),
   per-attribute error grouping in traces (single-product failures), rate-vs-baseline (load).
5. `eval/` harness: run each practice incident (live via faultctl, or bundles), score component + category, write `eval/results.md`.
6. Iterate on **generic** prompt guidance using eval failures. Never add incident-specific rules (R5).
7. Save state, push, write a short "Day 1 status" in Section 13.

**DoD for CP2:** correct, evidence-cited, schema-valid diagnosis on **≥ 3 mentor-selected practice incidents**;
`iz eval` runs over all available practice incidents; validator rejects a fabricated-evidence test case.

> Overnight (Day 1 18:45 → Day 2 09:00): confirm with organizers whether coding/commits are allowed. If not, only
> think/plan; don't commit.

### Phase 4 — "The agent can act safely" · Day 2 09:30–12:00 → **Checkpoint 3: Hands (12:00, 20 pts)**

Build in this order (safety first, then capability):

1. `killswitch.py` + tests (09:30–09:50).
2. `state_machine.py` with guards + tests for every forbidden transition (09:50–10:20).
3. `runbook.py` allowlist + `planner.py` → `ActionProposal` with hash (10:20–10:50).
4. `approval/gate.py` CLI approve/reject/timeout + dry-run path (10:50–11:15).
5. **Our Action API** service in `action_api/` (restart / throttle loadgen, dry-run, JSONL call log) +
   `action_client.py` + `executor.py` + audit logging (11:15–11:35). Build the Action API service in parallel from 09:30.
6. `verifier.py` + rollback/escalation path; full report (timeline, action, verification, prevention) (11:35–11:55).
7. One full live rehearsal (11:55–12:00).

**DoD for CP3:** one full run alert → diagnosis → proposal → explicit approval → Action API call (logged) →
**verified recovery from telemetry** → report. Also demonstrable: rejection (nothing executes), dry run (nothing
changes), kill switch (halts), and a failed-recovery path (escalates).

### Phase 5 — Harden, document, prepare · Day 2 12:00–13:30 · **NO NEW FEATURES**

- Run `iz eval` over all 10 practice incidents + at least 2 combinations (e.g. two faults at once) + a "no fault" control.
- Fix bugs that cause wrong/unsafe outcomes first; then crashes; then cosmetics.
- Clean startup from a fresh clone: `make setup && make demo-up && make agent` works.
- Write deliverables:
  - **README.md** (submission): prerequisites, exact setup commands, start demo + agent, inject/reset incident,
    how telemetry tools are called, approval/dry run/kill switch, verification & rollback/escalation, JSON example +
    readable report, known limitations, incidents tested.
  - **ARCHITECTURE.md**: one page — components, tools, decision process, safety controls.
  - **reports/**: agent-generated JSON + markdown for each practice incident run.
  - **EVALUATION.md**: table of practice incidents → right/wrong/escalated, why, what we'd change. Be honest.
  - **TEAM_FORM.md**: team name, members, contribution split, tools & AI models used (**including Claude Code /
    other coding assistants**), third-party libraries.
- `iz validate reports/` passes.

### Phase 6 — Final verification & submission · Day 2 13:30–14:00 → **Code freeze 14:00 (hard cutoff)**

- 13:30 fresh-clone test on a second laptop. 13:40 schema validation. 13:45 deliverables checklist (Section 11).
- **13:50 final push.** Verify on the remote that the last commit is visible. Nothing after 14:00 counts.

### Phase 7 — Hidden test & finalist demo · Day 2 14:00–18:45

- 14:00–15:30 organizers run unseen incidents (automated). Be available; don't change code.
- Rehearse the 5-minute demo (Section 12) and 3-minute Q&A; each member prepares to explain their own modules and one honest limitation.

---

## 8. Team Split (5 members — all must commit and explain their code)

| Member | Day 1 (Eyes + Brain) | Day 2 (Hands) | Explains to judges |
|--------|---------------------|---------------|--------------------|
| **Aditi** | `tools/metrics.py`, `alerting/*` | `verification/*` (reuses her metric queries) | Alert rules + debounce; how recovery is checked from telemetry |
| **Tvisha** | `tools/traces.py`, `tools/topology.py`, `compare_hop_latency` | `planning/*` (runbook allowlist + planner) | Following errors downstream; network vs app; why each action fits |
| **Mohit** | `tools/containers.py`, `tools/logs.py`, `docker_ro.py`, `redaction.py`, `tests/test_forbidden_access.py` | `action_api/` service + `execution/action_client.py` | Read-only Docker; flag-data avoidance; the Action API allowlist + call log |
| **Pavan** | `llm/*`, `investigation/*`, `diagnosis/*`, `evidence/*`, prompts, `eval/*` | `reporting/*` (schema adapter + markdown) | Adaptive tool choice; evidence validation; confidence policy; generalization |
| **Hemanth** | Integration, `faultctl`, `ARCHITECTURE.md`, `schema/incident.schema.json`, live runs | `state_machine.py`, `approval/*`, `execution/{executor,killswitch}.py`, `audit/*` | Approval gate + hash binding; dry run; kill switch; state guards |

Shared: `models.py` (agree on it in the first hour — it's the contract between members), README, demo.
Pair-review each other's PRs so everyone can answer questions about the whole system.

---

## 9. Data Contracts (`agent/models.py`, pydantic v2 — sketch)

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
    component: str | None; category: str; root_cause: str
    causal_chain: list[str]; confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]; uncertainty: str
    alternatives_considered: list[str]; insufficient_evidence: bool

class ActionProposal(BaseModel):
    action_id: str; operation: str; params: dict; reason: str
    expected_impact: str; risks: str; reversible: bool
    rollback_plan: str | None; action_hash: str

class ApprovalRecord(BaseModel):
    action_id: str; action_hash: str
    decision: Literal["approved","rejected","timeout"]
    approver: str; ts: datetime; reason: str | None = None

class VerificationResult(BaseModel):
    recovered: bool; checks: list[dict]; evidence_ids: list[str]

class IncidentRecord(BaseModel):   # mapped to our JSON schema by schema_adapter
    incident_id: str; alert: Alert; steps: list[dict]
    diagnosis: Diagnosis | None; proposal: ActionProposal | None
    approval: ApprovalRecord | None; execution: dict | None
    verification: VerificationResult | None
    final_state: str; escalation_reason: str | None
    timeline: list[dict]; prevention: list[str]; limitations: list[str]
```

---

## 10. Testing Strategy

### Unit / safety tests (must all pass before each checkpoint)

| Test | Asserts |
|------|---------|
| `test_forbidden_access.py` | No forbidden strings in `agent/`; `agent/` never imports `eval`; only `action_client.py` calls the Action API |
| `test_state_machine.py` | Cannot reach EXECUTING without matching approval; RESOLVED requires verification; kill → HALTED from every state |
| `test_approval.py` | Reject → no execution; timeout → no execution; hash mismatch (params changed after approval) → refused |
| `test_dry_run.py` | Dry run never calls `ActionClient.execute` (use a spy) |
| `test_killswitch.py` | Sentinel/env/SIGINT all halt before the next tool or action call |
| `test_validator.py` | Fabricated evidence ID / fabricated number / uncited component → rejected or confidence capped |
| `test_redaction.py` | `feature_flag.*` attributes and excluded services never reach the LLM context |
| `test_schema.py` | Every generated report validates against our JSON schema |
| `test_executor_failure.py` | Action API error → no recovery claimed → escalation |
| `test_verifier.py` | Action "succeeds" but telemetry still bad → not recovered → rollback/escalate |
| `test_config_localhost.py` | Non-localhost endpoints rejected (R6) |

### Scenario matrix (eval harness + live runs)

| Scenario | Expected agent behavior |
|----------|-------------------------|
| Known app fault | Correct service via telemetry, evidence cited, allowlisted remedy |
| Intermittent fault | Uses window/multiple traces; doesn't over-claim from one sample |
| Single-product failure | Identifies targeted failure, not broad outage |
| CPU / memory problem | Correlates resource metrics with symptoms |
| Container stopped / restarting | Uses container health; doesn't blame app code |
| Cache / DB unavailable | Traces downstream dependency; corroborates with logs/health |
| Network latency / loss | Compares hop latency; doesn't blame first slow service |
| Combined / unseen | Reasons from evidence; reports multiple causes or insufficiency |
| No fault (control) | No action; reports "no incident / false alarm" |
| Low confidence / conflicting evidence | Escalates without acting |
| Approval rejected | No execution; rejection recorded |
| Action API failure | Failure reported, state inspected, no recovery claimed |
| Action didn't recover | Rollback where possible, escalate |
| Kill switch / dry run | Halts / previews only |

Track results in `eval/results.md`; this feeds `EVALUATION.md` and the stretch "calibrated confidence" analysis
(compare stated confidence vs actual accuracy).

---

## 11. Final Readiness Checklist (before 13:50 Day 2 push)

- [ ] Demo (or bundle) + telemetry + model access work from a fresh clone
- [ ] Agent adaptively queries traces, metrics, logs, container health, deployment history (rationale logged)
- [ ] Diagnosis: component, cause, confidence, exact evidence; validator on; no invented evidence
- [ ] Only allowlisted Action API operations, each with a short reason
- [ ] Explicit approval, dry run and kill switch all demonstrably work; no change before approval
- [ ] Every action logged; health re-checked from telemetry; failure → rollback/escalate
- [ ] All outputs validate against the exact organizer JSON schema; readable reports present
- [ ] Practice incidents + combinations tested; no hard-coding (forbidden-access test green)
- [ ] Deliverables in repo: source + README, ARCHITECTURE.md, reports/, EVALUATION.md, TEAM_FORM.md
- [ ] AI coding assistants (e.g. Claude Code) declared in team form
- [ ] All 5 members have commits and can explain their modules
- [ ] Hidden-test harness entrypoint works exactly as the organizers specified

---

## 12. Five-minute Live Demo Script

| Time | Show |
|------|------|
| 0:00–0:30 | Clean environment; inject an incident with `faultctl`; alert fires — state the symptom |
| 0:30–1:30 | Agent choosing tools live, with one-line rationale per step; real trace/log/metric output |
| 1:30–2:15 | Diagnosis: component, cause, confidence, evidence IDs → point at the exact trace/log line/metric |
| 2:15–2:45 | Proposed allowlisted action + why it fits; dry run preview |
| 2:45–3:30 | Explicit approval (`approve <id>`); Action API call in the audit log |
| 3:30–4:15 | Verifier: post-action metrics back to baseline → RESOLVED (or show escalation path) |
| 4:15–5:00 | JSON report (schema-valid) + readable report with timeline & prevention; one honest limitation |

Backup: a recorded offline bundle run if the live demo misbehaves (say so openly).

---

## 13. Status Tracker (Claude Code: update this at the end of every task)

**Current phase:** Phase 2 — Eyes (CP1 at 15:30)
**Last updated:** Day 1 ~14:45, Hemanth

| Phase | Status | Notes |
|-------|--------|-------|
| 0 Prep | ☑ | Docker, Python 3.11, demo cloned |
| 1 Setup & Discovery (CP0) | ◐ | Skeleton + faultctl + architecture draft on `dev`; demo images still downloading; Discovery filled in Section 3 |
| 2 Eyes (CP1) | ☐ | |
| 3 Brain (CP2) | ☐ | Practice incidents correct: _/10 |
| 4 Hands (CP3) | ☐ | |
| 5 Harden & docs | ☐ | |
| 6 Submission (14:00) | ☐ | Final commit SHA: |
| 7 Hidden test & demo | ☐ | |

**Open questions for organizers:** hidden-test entrypoint (how will they trigger our agent, and with which faults?) ·
deadline (14:00 vs 16:30) · overnight coding allowed? · flag-evaluation span attributes allowed?
(JSON schema, Action API and faultctl: organizers said define them ourselves.)

**Known limitations (keep honest, copy into README at Phase 5):**
- _(fill in as discovered)_

---

## 14. Common Pitfalls (avoid)

- Dumping all logs/metrics into one prompt instead of adaptive tool calls.
- Naming a cause without exact, checkable evidence.
- Blaming the first erroring service (it's often a victim of a downstream failure).
- Reading feature-flag settings / flag UI / flag API — or leaking flag info via spans/logs/env vars.
- Letting the LLM invent fixes or run arbitrary commands.
- Treating "Action API returned 200" as proof of recovery.
- Executing without explicit approval, or executing something different from what was approved.
- Hard-coding practice answers or tuning prompts with incident-specific rules.
- Overstating confidence or hiding failed tests.
- Starting stretch goals (incident memory, reviewer agent, chat UI, calibration) before the full loop + safety controls work.
