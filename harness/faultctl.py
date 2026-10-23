#!/usr/bin/env python3
"""faultctl: inject and reset practice incidents in the OpenTelemetry Demo.

This is the TEST HARNESS, not the agent. The agent must never import, call or
read this file, or the flag file it edits: diagnosis must come from telemetry.

Usage:
    python harness/faultctl.py list
    python harness/faultctl.py inject <incident>
    python harness/faultctl.py status
    python harness/faultctl.py reset
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEMO_DIR = Path(os.getenv("FAULTCTL_DEMO_DIR", Path(__file__).resolve().parents[2] / "opentelemetry-demo"))
FLAG_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"
STATE_FILE = Path(__file__).resolve().parent / ".faultctl_state.json"
NETSHOOT_IMAGE = "nicolaka/netshoot:v0.13"

# Each incident is a list of steps. Step kinds:
#   ("flag", flag_name, variant)          app fault via the demo's built-in flags
#   ("stop", container)                   container stopped
#   ("cpu", container, cpus)              container starved of CPU
#   ("netem", container, netem_args)      network latency / loss on a container
INCIDENTS = {
    # Application faults (demo's built-in failure flags)
    "payment-failure":      [("flag", "paymentFailure", "50%")],
    "cart-intermittent":    [("flag", "cartFailure", "25%")],
    "catalog-one-product":  [("flag", "productCatalogFailure", "on")],
    "recommendation-cache": [("flag", "recommendationCacheFailure", "on")],
    "ad-high-cpu":          [("flag", "adHighCpu", "on")],
    "ad-gc":                [("flag", "adManualGc", "on")],
    "homepage-flood":       [("flag", "loadGeneratorFloodHomepage", "on")],
    # Infrastructure faults
    "payment-stopped":      [("stop", "payment")],
    "cache-down":           [("stop", "valkey-cart")],
    "checkout-latency":     [("netem", "checkout", "delay 800ms 100ms")],
    "currency-cpu-starved": [("cpu", "currency", "0.05")],
    # Combination
    "combo-cart-and-latency": [("flag", "cartFailure", "50%"), ("netem", "checkout", "delay 500ms")],
}


def run(cmd, check=True):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"steps": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def set_flag(flag, variant):
    data = json.loads(FLAG_FILE.read_text())
    if flag not in data["flags"]:
        sys.exit(f"unknown flag {flag}")
    previous = data["flags"][flag]["defaultVariant"]
    data["flags"][flag]["defaultVariant"] = variant
    FLAG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    return previous


def current_cpus(container):
    out = run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", container]).stdout.strip()
    nano = int(out or 0)
    return str(nano / 1e9) if nano else "0"


def netem(container, args):
    tc = f"tc qdisc replace dev eth0 root netem {args}" if args else "tc qdisc del dev eth0 root"
    run(["docker", "run", "--rm", "--net", f"container:{container}", "--cap-add", "NET_ADMIN",
         NETSHOOT_IMAGE, "sh", "-c", tc], check=bool(args))


def inject(name):
    if name not in INCIDENTS:
        sys.exit(f"unknown incident '{name}'. Run: faultctl list")
    state = load_state()
    if state["steps"]:
        sys.exit("an incident is already active. Run: faultctl reset")
    for step in INCIDENTS[name]:
        kind = step[0]
        if kind == "flag":
            previous = set_flag(step[1], step[2])
            state["steps"].append({"kind": "flag", "flag": step[1], "previous": previous})
        elif kind == "stop":
            run(["docker", "stop", step[1]])
            state["steps"].append({"kind": "stop", "container": step[1]})
        elif kind == "cpu":
            previous = current_cpus(step[1])
            run(["docker", "update", "--cpus", step[2], step[1]])
            state["steps"].append({"kind": "cpu", "container": step[1], "previous": previous})
        elif kind == "netem":
            netem(step[1], step[2])
            state["steps"].append({"kind": "netem", "container": step[1]})
        save_state(state)
    state["incident"] = name
    save_state(state)
    print(f"injected: {name}")


def reset():
    state = load_state()
    for step in reversed(state["steps"]):
        kind = step["kind"]
        if kind == "flag":
            set_flag(step["flag"], step["previous"])
        elif kind == "stop":
            run(["docker", "start", step["container"]], check=False)
        elif kind == "cpu":
            run(["docker", "update", "--cpus", step["previous"], step["container"]], check=False)
        elif kind == "netem":
            netem(step["container"], None)
    STATE_FILE.unlink(missing_ok=True)
    print("reset complete" if state["steps"] else "nothing to reset")


def main():
    parser = argparse.ArgumentParser(prog="faultctl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("reset")
    inj = sub.add_parser("inject")
    inj.add_argument("incident")
    args = parser.parse_args()

    if args.cmd == "list":
        for name, steps in INCIDENTS.items():
            print(f"{name:26} {steps}")
    elif args.cmd == "status":
        state = load_state()
        print(f"active: {state.get('incident')}" if state["steps"] else "no active incident")
    elif args.cmd == "inject":
        inject(args.incident)
    elif args.cmd == "reset":
        reset()


if __name__ == "__main__":
    main()
