"""A scripted stand-in for a vendor agent CLI, run as a real subprocess.

The runner tests spawn this file instead of ``claude``/``codex``. Each
invocation consumes the next step of a JSON scenario file, records the
prompt it was given, applies the step's file edits in its working directory,
optionally sleeps (to exercise cancellation), prints a JSON
result and exits with the step's exit code.

Usage: ``python fake_agent.py <scenario.json> <model> <prompt>``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    scenario_path = Path(sys.argv[1])
    model = sys.argv[2]
    prompt = sys.argv[3]

    calls_path = scenario_path.with_suffix(".calls.jsonl")
    previous_calls = calls_path.read_text().splitlines() if calls_path.exists() else []
    steps = json.loads(scenario_path.read_text())
    step = steps[min(len(previous_calls), len(steps) - 1)]

    with calls_path.open("a") as calls:
        calls.write(json.dumps({"model": model, "prompt": prompt}) + "\n")

    for relative_path, content in step.get("write", {}).items():
        target = Path.cwd() / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    if step.get("sleep"):
        time.sleep(step["sleep"])
        # Only reached when nobody killed us mid-sleep — lets a test prove
        # that cancellation really terminated the process.
        for relative_path, content in step.get("write_after_sleep", {}).items():
            (Path.cwd() / relative_path).write_text(content)

    print(
        json.dumps(
            {
                "result": step.get("result", "done"),
                "cost_usd": step.get("cost_usd", 0.0),
                "num_turns": step.get("num_turns", 1),
                "session_id": step.get("session_id"),
                "subtype": step.get("subtype", ""),
                "errors": step.get("errors", []),
            }
        )
    )
    return int(step.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
