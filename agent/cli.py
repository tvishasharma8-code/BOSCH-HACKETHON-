"""`iz` command line entry point.

Skeleton only — owner C fills in `tools`, `investigate` and `watch` in later
blocks, and other owners land their own subcommands here. The one thing that is
real from the start is the execution mode: dry run is the default everywhere and
`--live` must be passed explicitly (R8).
"""

from __future__ import annotations

import typer
from rich.console import Console

from agent.config import load_settings

app = typer.Typer(add_completion=False, help="Incident Zero — AI on-call engineer.")
console = Console()

# Set by the root callback, read by subcommands as they are added.
STATE: dict[str, bool] = {"live": False}


@app.callback()
def main(
    live: bool = typer.Option(
        False,
        "--live",
        help="Allow state-changing actions. Omit for dry run, which is the default (R8).",
    ),
) -> None:
    STATE["live"] = live


@app.command()
def config() -> None:
    """Load and print settings — fails loudly if an endpoint is not local (R6)."""
    s = load_settings()
    mode = "LIVE" if STATE["live"] else "DRY RUN"
    console.print(f"[bold]mode:[/bold] {mode}")
    console.print(f"[bold]endpoints:[/bold] {s.endpoints.model_dump()}")
    console.print(f"[bold]llm chain:[/bold] {' -> '.join(s.llm.model_chain)}")


if __name__ == "__main__":
    app()
