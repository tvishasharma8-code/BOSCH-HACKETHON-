"""Checkpoint 1 demo: the agent can SEE.

A rule-driven investigation using A's read-only tools: each step is chosen from
what the previous one found, and every finding gets an evidence ID. The LLM
investigator (C) will replace these rules with model-chosen steps.

Run:  python -m agent.demo_eyes
"""
from collections import Counter

from rich.console import Console
from rich.panel import Panel

from agent.tools.metrics import get_service_overview, query_metric
from agent.tools.traces import get_trace, search_traces

console = Console()
evidence = []


def step(why, result):
    eid = f"E{len(evidence) + 1}"
    evidence.append((eid, result))
    console.print(f"\n[bold cyan]{eid}[/] [bold]{result['tool']}[/]  [dim]why: {why}[/]")
    console.print(result["error"] and f"[red]error: {result['error']}[/]" or result["summary"])
    return eid, result


def main():
    console.print(Panel("Incident Zero: the agent can see (Checkpoint 1)", style="bold"))

    e_over, overview = step("start broad: which service is worse than its own normal?", get_service_overview())
    flagged = [f for f in overview["key_facts"] if f["data"]["flagged"]]
    if not flagged:
        console.print(Panel("No service is abnormal compared to its own baseline. No incident.", style="green"))
        return

    worst = max(flagged, key=lambda f: f["data"]["error_ratio"])["service"]
    e_ops, _ = step(f"{worst} is flagged: which of its operations is failing?",
                    query_metric("errors_by_operation", worst))

    e_tr, traces = step(f"follow {worst}'s failing requests downstream to find the deepest failing span",
                        search_traces(worst, errors_only=True))
    culprits = Counter(f["data"]["deepest_error_service"] for f in traces["key_facts"]
                       if f["data"]["deepest_error_service"])
    cause = culprits.most_common(1)[0][0] if culprits else worst

    cited = [e_over, e_ops, e_tr]
    if traces["key_facts"]:
        e_one, _ = step("inspect one failing trace as a span tree",
                        get_trace(traces["key_facts"][0]["data"]["trace_id"], worst))
        cited.append(e_one)
    if cause != worst:
        e_cause, _ = step(f"traces point below {worst} to {cause}: which {cause} operation fails?",
                          query_metric("errors_by_operation", cause))
        cited.append(e_cause)

    row = next((f["data"] for f in overview["key_facts"] if f["service"] == cause), None)
    victims = sorted(f["service"] for f in flagged if f["service"] != cause)
    lines = [f"Most likely failing component: [bold]{cause}[/]"]
    if row:
        lines.append(f"  {cause} errors {row['error_ratio']:.1%} vs normal {row['baseline_error_ratio']:.1%}  ({e_over})")
    if culprits:
        lines.append(f"  deepest failing span in {culprits[cause]}/{sum(culprits.values())} error traces through {worst}  ({e_tr})")
    if victims:
        lines.append(f"  also flagged, likely callers affected by it: {', '.join(victims)}  ({e_over})")
    lines.append(f"Evidence cited: {', '.join(cited)}")
    lines.append("[dim]Symptom summary only. Root-cause diagnosis + confidence is Checkpoint 2.[/]")
    console.print(Panel("\n".join(lines), title="Symptom summary", style="yellow"))


if __name__ == "__main__":
    main()
