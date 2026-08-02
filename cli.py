from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict
from pathlib import Path

from config import ConfigError, load_settings
from core import RepositoryTools
from graph import build_graph, initial_state
from llm_planner import LLMPlanner


def main() -> None:
    parser = argparse.ArgumentParser(description="Approval-gated repository maintenance Agent (online LLM)")
    parser.add_argument("task")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--approve", action="store_true", help="approve the proposed mutation")
    parser.add_argument(
        "--allow-command",
        action="append",
        default=[],
        metavar="ARGV",
        help="test command allow-list entry, e.g. 'python -m pytest' (repeatable)",
    )
    parser.add_argument("--env", type=Path, default=None, help="path to .env file (default: ./ .env)")
    args = parser.parse_args()

    try:
        settings = load_settings(args.env)
    except ConfigError as exc:
        parser.error(str(exc))

    allowed = [tuple(shlex.split(command)) for command in args.allow_command]
    tools = RepositoryTools(args.repo, allowed_commands=allowed)
    planner = LLMPlanner(settings, tools)
    graph = build_graph(tools, planner, max_repairs=settings.max_repairs)

    final_state = graph.invoke(initial_state(args.task, approved=args.approve))
    report = final_state["report"]
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
