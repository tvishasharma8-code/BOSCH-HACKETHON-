"""Checkpoint 2 demo: the agent can DIAGNOSE.

Rule-driven evidence gathering (metrics -> traces -> logs -> container health),
then one LLM call turns the evidence into a structured diagnosis: component,
category, cause, confidence and the evidence IDs that support it. A validator
rejects citations to evidence that doesn't exist. Interim version until the
LLM investigator (C) and diagnoser (D) land.

Run:  python -m agent.demo_brain
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from agent.llm.provider import GeminiProvider, LLMError, user_turn
from agent.tools import docker_ro
from agent.tools.logs import search_logs
from agent.tools.metrics import get_service_overview, query_metric
from agent.tools.traces import get_trace, search_traces

console = Console()
REPORTS = Path(__file__).resolve().parents[1] / "reports"
CATEGORIES = ["app_error", "resource_exhaustion", "load_spike", "container_down", "dependency_unavailable",
              "network_degradation", "resource_starvation", "multiple", "unknown"]

SYSTEM = f"""You are an SRE on call. Diagnose the incident using ONLY the evidence given.
Rules:
- The first service showing errors is often a victim. Prefer the deepest failing component in traces,
  and use container health and logs to separate an application fault from an infrastructure fault.
- A dependency (cache, database, another service) that is stopped or unreachable makes its callers fail.
- Cite evidence by ID (E1, E2, ...). Never invent values, IDs or services that are not in the evidence.
- If the evidence is weak or conflicting, lower confidence and say what is missing. "unknown" is allowed.
Reply with ONLY a JSON object, no prose, with these keys:
  component (string or null), category (one of {CATEGORIES}), root_cause (one or two sentences),
  causal_chain (list of short steps), confidence (0 to 1), evidence_ids (list of IDs),
  uncertainty (what you are unsure about or could not check), alternatives_considered (list of strings)"""


class Investigation:
    def __init__(self):
        self.evidence = []

    def step(self, why, result):
        eid = f"E{len(self.evidence) + 1}"
        self.evidence.append({"id": eid, "why": why, "result": result})
        console.print(f"[bold cyan]{eid}[/] [bold]{result['tool']}[/]  [dim]why: {why}[/]")
        text = result["error"] and f"error: {result['error']}" or result["summary"]
        console.print("\n".join(text.splitlines()[:12]) + "\n")
        return eid, result

    def for_llm(self):
        """Compact evidence for the model: summary and a few facts, never raw payloads."""
        return [{"id": e["id"], "tool": e["result"]["tool"], "why": e["why"],
                 "summary": (e["result"]["error"] and f"ERROR: {e['result']['error']}") or e["result"]["summary"][:1500],
                 "key_facts": e["result"]["key_facts"][:8]} for e in self.evidence]


def gather(inv):
    _, overview = inv.step("start broad: which service is worse than its own normal?", get_service_overview())
    flagged = [f for f in overview["key_facts"] if f["data"]["flagged"]]
    containers = docker_ro.list_containers()
    down = [r for r in containers.get("raw") or [] if r["status"] != "running"]
    if down:
        inv.step("check container health: is anything stopped?",
                 dict(containers, summary=containers["summary"] + " Not running: "
                      + ", ".join(f"{r['service']} ({r['status']})" for r in down),
                      key_facts=[{"type": "container", "service": r["service"], "data": r} for r in down]))
    if not flagged and not down:
        return None

    worst = max(flagged, key=lambda f: (f["data"]["error_ratio"], f["data"]["p95_ms"]))["service"] if flagged \
        else down[0]["service"]
    inv.step(f"{worst} is flagged: which of its operations is failing?", query_metric("errors_by_operation", worst))
    _, traces = inv.step(f"follow {worst}'s failing requests to the deepest failing span",
                         search_traces(worst, errors_only=True))
    culprits = Counter(f["data"]["deepest_error_service"] for f in traces["key_facts"]
                       if f["data"]["deepest_error_service"])
    cause = culprits.most_common(1)[0][0] if culprits else worst
    if traces["key_facts"]:
        inv.step("inspect one failing trace", get_trace(traces["key_facts"][0]["data"]["trace_id"], worst))
    inv.step(f"read {cause}'s error logs for the actual message", search_logs(cause))
    inv.step(f"check {cause}'s container: crashed, restarting, out of memory?", docker_ro.inspect(cause))
    return worst


def diagnose(inv):
    llm = GeminiProvider.from_settings()
    prompt = "Evidence:\n" + json.dumps(inv.for_llm(), indent=1, default=str)
    turn = llm.call(SYSTEM, [user_turn(prompt)])
    text = re.sub(r"^```(?:json)?|```$", "", (turn.text or "").strip(), flags=re.M).strip()
    return json.loads(text), turn.usage


def validate(diagnosis, inv):
    """Cited evidence must exist; otherwise confidence is capped. Returns a list of problems."""
    known = {e["id"] for e in inv.evidence}
    problems = [f"cites unknown evidence {i}" for i in diagnosis.get("evidence_ids", []) if i not in known]
    if not diagnosis.get("evidence_ids"):
        problems.append("cites no evidence")
    if diagnosis.get("category") not in CATEGORIES:
        problems.append(f"unknown category {diagnosis.get('category')!r}")
    if problems:
        diagnosis["confidence"] = min(float(diagnosis.get("confidence") or 0), 0.3)
    return problems


def main():
    console.print(Panel("Incident Zero: the agent can diagnose (Checkpoint 2)", style="bold"))
    inv = Investigation()
    alerting = gather(inv)
    if alerting is None:
        console.print(Panel("No service is abnormal and every container is running. No incident.", style="green"))
        return

    console.print("[bold]Asking the model for a diagnosis from the evidence above...[/]")
    try:
        diagnosis, usage = diagnose(inv)
    except (LLMError, json.JSONDecodeError) as exc:
        console.print(Panel(f"Diagnosis failed ({exc}). Escalating to a human with the evidence above.", style="red"))
        return
    problems = validate(diagnosis, inv)

    incident_id = f"INC-{datetime.now():%Y%m%d-%H%M%S}-{alerting}"
    status = "escalated" if problems or float(diagnosis.get("confidence") or 0) < 0.7 else "diagnosed"
    report = {"incident_id": incident_id, "generated_at": datetime.now(timezone.utc).isoformat(),
              "status": status, "alerting_service": alerting, "diagnosis": diagnosis,
              "validation_problems": problems,
              "investigation": [{"id": e["id"], "tool": e["result"]["tool"], "why": e["why"],
                                 "query": e["result"]["query"], "summary": e["result"]["summary"],
                                 "error": e["result"]["error"]} for e in inv.evidence]}
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{incident_id}.json"
    path.write_text(json.dumps(report, indent=2, default=str))

    d = diagnosis
    lines = [f"Component:   [bold]{d.get('component')}[/]",
             f"Category:    {d.get('category')}",
             f"Root cause:  {d.get('root_cause')}",
             f"Confidence:  [bold]{float(d.get('confidence') or 0):.0%}[/]",
             f"Evidence:    {', '.join(d.get('evidence_ids', []))}",
             f"Chain:       {' -> '.join(d.get('causal_chain', []))}",
             f"Uncertainty: {d.get('uncertainty')}"]
    if problems:
        lines.append(f"[red]Validator: {'; '.join(problems)} -> confidence capped[/]")
    lines.append(f"Decision:    {'ESCALATE to a human (low confidence or failed validation)' if status == 'escalated' else 'ready to propose a fix (Checkpoint 3)'}")
    lines.append(f"[dim]Report: {path.relative_to(path.parents[1])} · tokens: {getattr(usage, 'total_tokens', '?')}[/]")
    console.print(Panel("\n".join(lines), title="Diagnosis", style="yellow"))


if __name__ == "__main__":
    main()
