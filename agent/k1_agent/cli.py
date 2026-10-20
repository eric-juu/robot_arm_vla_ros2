"""
k1-agent command line.

    k1-agent observe [--out frame.jpg]                 check the connection, print the state, save a frame
    k1-agent run "TASK" --allow-motion [--mode code]   run the agent on a task
    k1-agent skills [list|pending|show NAME|approve NAME|reject NAME]
    k1-agent mcp --allow-motion                        MCP server for Claude Code / Codex

The k1_web token comes from K1_WEB_TOKEN (or K1_WEB_ADMIN_PASSWORD); API keys from ANTHROPIC_API_KEY
or OPENAI_API_KEY.
"""
import argparse
import json
import os
import sys

import yaml

from k1_agent.client import K1Client, K1Error
from k1_agent.primitives import Primitives
from k1_agent.skills import SkillLibrary
from k1_agent.trace import Trace

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(AGENT_DIR, "config.yaml")


def load_config(path: str | None) -> dict:
    path = path or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    base = os.path.dirname(os.path.abspath(path))
    for key in ("runs_dir", "skills_dir"):     # relative to the config file
        config[key] = os.path.join(base, os.path.expanduser(config.get(key, key.split("_")[0])))
    return config


def add_safety_args(p: argparse.ArgumentParser):
    p.add_argument("--allow-motion", action="store_true", help="let the agent move the arm (off by default)")
    p.add_argument("--allow-enable", action="store_true", help="let the agent enable/disable the motors")
    p.add_argument("--max-steps", type=int, help="motion calls allowed in this run")


def make_prims(config, args, client, trace=None) -> Primitives:
    return Primitives(client, trace, allow_motion=args.allow_motion, allow_enable=args.allow_enable,
                      max_steps=args.max_steps or config["max_steps"], max_step_rad=config["max_step_rad"],
                      tolerance=config["tolerance"], move_timeout=config["move_timeout"])


def cmd_observe(config, args):
    with K1Client(args.url or config["url"]) as client:
        obs = Primitives(client).observe()
        frame = obs.pop("image", None)
        print(json.dumps(obs, indent=2))
        if frame and args.out:
            with open(args.out, "wb") as f:
                f.write(frame)
            print(f"saved camera frame to {args.out}")
        elif not frame:
            print("no camera frame (is the camera node running?)")


def cmd_run(config, args):
    from k1_agent.providers.base import make_provider
    from k1_agent.runner import AgentRunner, ConsoleOperator

    provider_name = args.provider or config["provider"]
    model = args.model or config["models"][provider_name]
    kwargs = {}
    if provider_name == "openai" and config.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = config["openai_reasoning_effort"]
    provider = make_provider(provider_name, model, **kwargs)
    skills = SkillLibrary(config["skills_dir"])
    trace = Trace(config["runs_dir"], args.task)
    operator = ConsoleOperator()
    mode = args.mode or config["mode"]

    with K1Client(args.url or config["url"]) as client:
        prims = make_prims(config, args, client, trace)
        if not args.allow_motion:
            print("note: --allow-motion not given, so every motion will be refused (dry run)")
        runner = AgentRunner(provider, prims, skills, trace, operator, max_turns=config["max_turns"],
                             image_history=config["image_history"], max_tokens=config["max_tokens"],
                             program_timeout=config["program_timeout"])
        print(f"running {mode} mode with {provider_name}:{model}; trace in {trace.dir}")
        if mode == "code":
            result = runner.run_code(args.task, attempts=args.attempts or config["attempts"])
        else:
            result = runner.run_tools(args.task)
        print(f"\n{'SUCCESS' if result.success else 'FAILED'} ({result.reason}): {result.summary}")
        print(f"trace: {result.trace_dir}")
        if result.success and operator.confirm("Did it really succeed? Save a skill draft from this run?"):
            path = runner.draft_skill(args.task)
            print(f"skill draft: {path}  (review it, then `k1-agent skills approve <name>`)" if path
                  else "could not save a skill draft")
    return 0 if result.success else 1


def cmd_skills(config, args):
    skills = SkillLibrary(config["skills_dir"])
    if args.action == "list":
        print(skills.index_text())
    elif args.action == "pending":
        pending = skills.pending()
        print("\n".join(f"- {s['name']}: {s['description']}" for s in pending) or "(nothing pending)")
    elif args.action == "show":
        print(skills.read(args.name) or skills.read(args.name, pending=True) or f"no skill {args.name!r}")
    elif args.action == "approve":
        print(f"approved: {skills.approve(args.name)}")
    elif args.action == "reject":
        skills.reject(args.name)
        print(f"deleted pending skill {args.name}")


def cmd_mcp(config, args):
    from k1_agent.mcp_server import build_server

    client = K1Client(args.url or config["url"]).connect()
    trace = Trace(config["runs_dir"], "mcp")
    server = build_server(make_prims(config, args, client, trace), SkillLibrary(config["skills_dir"]))
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http")
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="k1-agent", description="AI agent for the K1 arm over k1_web")
    parser.add_argument("--config", help=f"settings file (default {DEFAULT_CONFIG})")
    parser.add_argument("--url", help="k1_web URL (default from the config)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("observe", help="print the arm state and save a camera frame")
    p.add_argument("--out", default="frame.jpg")

    p = sub.add_parser("run", help="run the agent on a task")
    p.add_argument("task")
    p.add_argument("--mode", choices=["tools", "code"])
    p.add_argument("--provider", choices=["anthropic", "openai"])
    p.add_argument("--model")
    p.add_argument("--attempts", type=int)
    add_safety_args(p)

    p = sub.add_parser("skills", help="manage the skill library")
    p.add_argument("action", nargs="?", default="list", choices=["list", "pending", "show", "approve", "reject"])
    p.add_argument("name", nargs="?")

    p = sub.add_parser("mcp", help="serve the primitives over MCP")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    add_safety_args(p)

    args = parser.parse_args(argv)
    if args.command == "skills" and args.action in ("show", "approve", "reject") and not args.name:
        parser.error(f"skills {args.action} needs a NAME")
    config = load_config(args.config)
    try:
        return {"observe": cmd_observe, "run": cmd_run, "skills": cmd_skills, "mcp": cmd_mcp}[args.command](
            config, args) or 0
    except (K1Error, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
