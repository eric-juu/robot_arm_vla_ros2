"""
k1-agent command line.

    k1-agent observe [--out frame.jpg]                 check the connection, print the state, save a frame
    k1-agent run "TASK" --allow-motion [--mode code]   run the agent on a task
    k1-agent skills [list|pending|show NAME|approve NAME|reject NAME]
    k1-agent mcp --allow-motion                        MCP server for Claude Code / Codex
    k1-agent login [--status]                          sign in to Claude Code (for --provider claude-code)
    k1-agent serve --allow-motion                      take tasks from the web page's prompt box
    k1-agent eval run|perception|label|report ...      compare models: success rate against cost

The k1_web token comes from K1_WEB_TOKEN (or K1_WEB_ADMIN_PASSWORD); API keys from ANTHROPIC_API_KEY,
OPENAI_API_KEY or GEMINI_API_KEY. The claude-code provider needs no key: it uses the Claude account
Claude Code is signed in with.
"""
import argparse
import json
import os
import sys
import time

import yaml

from k1_agent.claude_code import ClaudeCodeError
from k1_agent.client import K1Client, K1Error
from k1_agent.evaluate import EvalError
from k1_agent.primitives import Primitives
from k1_agent.skills import SkillLibrary
from k1_agent.trace import Trace

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(AGENT_DIR, "config.yaml")


def load_config(path: str | None) -> dict:
    path = path or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    config["config_path"] = os.path.abspath(path)
    base = os.path.dirname(config["config_path"])
    for key in ("runs_dir", "skills_dir"):     # relative to the config file
        config[key] = os.path.join(base, os.path.expanduser(config.get(key, key.split("_")[0])))
    return config


def add_safety_args(p: argparse.ArgumentParser):
    p.add_argument("--allow-motion", action="store_true", help="let the agent move the arm (off by default)")
    p.add_argument("--allow-enable", action="store_true", help="let the agent enable/disable the motors")
    p.add_argument("--max-steps", type=int, help="motion calls allowed in this run")


def add_provider_args(p: argparse.ArgumentParser):
    p.add_argument("--provider", choices=["anthropic", "openai", "gemini", "claude-code"])
    p.add_argument("--model")


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


def mcp_safety_args(args) -> list[str]:
    """The run's safety flags, passed on to the `k1-agent mcp` server that Claude Code starts."""
    flags = (["--allow-motion"] if args.allow_motion else []) + (["--allow-enable"] if args.allow_enable else [])
    return flags + (["--max-steps", str(args.max_steps)] if args.max_steps else [])


def build_task_runner(config, args, client, log=print):
    """Returns (run_task(text, mode, operator) -> RunResult, info) for the chosen provider."""
    provider_name = args.provider or config["provider"]
    skills = SkillLibrary(config["skills_dir"])

    if provider_name == "claude-code":
        from k1_agent.claude_code import ClaudeCodeRunner, login_status

        cc = config.get("claude_code") or {}
        command = cc.get("command") or "claude"
        if not login_status(command).get("loggedIn"):
            raise ClaudeCodeError("Claude Code is not signed in: run `k1-agent login` first")
        model = args.model or cc.get("model")

        def run_task(text, mode, operator):
            trace = Trace(config["runs_dir"], text)
            log(f"running with Claude Code ({model or 'default model'}); trace in {trace.dir}")
            runner = ClaudeCodeRunner(client, trace, args.url or config["url"], config["config_path"],
                                      mcp_safety_args(args), model=model, command=command,
                                      use_api_key=bool(cc.get("use_api_key")),
                                      max_budget_usd=cc.get("max_budget_usd"), timeout=cc.get("timeout", 900))
            return runner.run(text, mode)

        return run_task, {"provider": "claude-code", "model": model or "default", "allow_motion": args.allow_motion}

    from k1_agent.pricing import Pricing
    from k1_agent.providers.base import make_provider
    from k1_agent.runner import AgentRunner

    model = args.model or config["models"][provider_name]
    kwargs = {}
    if provider_name == "openai" and config.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = config["openai_reasoning_effort"]
    provider = make_provider(provider_name, model, **kwargs)
    pricing = Pricing.load()

    def run_task(text, mode, operator):
        trace = Trace(config["runs_dir"], text)
        prims = make_prims(config, args, client, trace)
        runner = AgentRunner(provider, prims, skills, trace, operator, max_turns=config["max_turns"],
                             image_history=config["image_history"], max_tokens=config["max_tokens"],
                             program_timeout=config["program_timeout"], pricing=pricing)
        log(f"running {mode} mode with {provider_name}:{model}; trace in {trace.dir}")
        if mode == "code":
            result = runner.run_code(text, attempts=args.attempts or config["attempts"])
        else:
            result = runner.run_tools(text)
        if result.success and operator.confirm("Did it really succeed? Save a skill draft from this run?"):
            path = runner.draft_skill(text)
            log(f"skill draft: {path}  (review it, then `k1-agent skills approve <name>`)" if path
                else "could not save a skill draft")
        return result

    return run_task, {"provider": provider_name, "model": model, "allow_motion": args.allow_motion}


def cmd_run(config, args):
    from k1_agent.runner import ConsoleOperator

    with K1Client(args.url or config["url"]) as client:
        run_task, info = build_task_runner(config, args, client)
        if not args.allow_motion:
            print("note: --allow-motion not given, so every motion will be refused (dry run)")
        if info["provider"] == "claude-code":
            print("note: Claude Code's questions appear on the web page")
        result = run_task(args.task, args.mode or config["mode"], ConsoleOperator())
        print(f"\n{'SUCCESS' if result.success else 'FAILED'} ({result.reason}): {result.summary}")
        if result.cost_usd is not None:
            print(f"cost: ${result.cost_usd:.4f} over {result.llm_calls} model calls")
        print(f"trace: {result.trace_dir}")
    return 0 if result.success else 1


def cmd_serve(config, args):
    from k1_agent.service import AgentService

    with K1Client(args.url or config["url"]) as client:
        def log(text):
            print(text)
            client.note(text[:300])

        run_task, info = build_task_runner(config, args, client, log=log)
        if not args.allow_motion:
            print("note: --allow-motion not given, so every motion will be refused (dry run)")
        print(f"agent service ready ({info['provider']}:{info['model']}). Type tasks into the web page's "
              "agent card; Ctrl+C here quits.")
        AgentService(client, run_task, info).serve_forever()


def cmd_login(config, args):
    from k1_agent.claude_code import login, login_status

    command = (config.get("claude_code") or {}).get("command") or "claude"
    if not args.status:
        login(command)
    status = login_status(command)
    if status.get("loggedIn"):
        print(f"Claude Code is signed in ({status.get('authMethod', 'unknown method')}). "
              "Use --provider claude-code, or set provider: claude-code in config.yaml.")
        return 0
    print("Claude Code is not signed in. Run `k1-agent login`, or on a PC without a browser run "
          "`claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN.")
    return 1


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
    operator = None
    if args.web_operator:
        from k1_agent.web_operator import WebOperator
        operator = WebOperator(client)
    server = build_server(make_prims(config, args, client, trace), SkillLibrary(config["skills_dir"]), operator)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http")
    finally:
        client.close()


def cmd_eval(config, args):
    from k1_agent import evaluate as ev

    if args.eval_command == "run":
        tasks = [t for t in ev.load_suite(args.suite) if t["tier"] == args.tier and
                 (not args.tasks or t["id"] in args.tasks.split(","))]
        if not tasks:
            raise ev.EvalError(f"no tier {args.tier} tasks matched in {args.suite}")
        name = f"{os.path.splitext(os.path.basename(args.suite))[0]}-tier{args.tier}-{time.strftime('%Y%m%d-%H%M%S')}"
        out = args.resume or args.out or os.path.join(AGENT_DIR, "evals", "results", name)
        ev.run_suite(config, tasks, ev.parse_models(args.models), out, trials=args.trials,
                     modes=args.modes.split(",") if args.modes else None, parallel=args.parallel,
                     skills_on=not args.no_skills, max_cost_usd=args.max_cost_usd, url=args.url, seed=args.seed)
        print(f"results in {out}; summary: k1-agent eval report {out}")
    elif args.eval_command == "perception":
        name = f"perception-{time.strftime('%Y%m%d-%H%M%S')}"
        out = args.out or os.path.join(AGENT_DIR, "evals", "results", name)
        ev.run_perception(config, args.dataset, ev.parse_models(args.models), out, repeats=args.repeats,
                          parallel=args.parallel, max_cost_usd=args.max_cost_usd)
        print(f"results in {out}; summary: k1-agent eval report {out}")
    elif args.eval_command == "label":
        ev.label_frames(args.runs, args.out, rotate=args.rotate_180, question=args.question,
                        choices=args.choices.split(",") if args.choices else None, category=args.category,
                        limit=args.limit, auto=not args.no_auto)
    elif args.eval_command == "report":
        rows = ev.load_rows(args.results)
        trials, perception = ev.summarize_trials(rows), ev.summarize_perception(rows)
        print(ev.report_markdown(trials, perception))
        if args.html:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(ev.report_html(trials, perception))
            print(f"\nwrote {args.html}")
    return 0


def add_eval_args(sub):
    p = sub.add_parser("eval", help="compare models: success rate against cost (ASPIRE-style)")
    ev_sub = p.add_subparsers(dest="eval_command", required=True)

    q = ev_sub.add_parser("run", help="run a task suite: tier 1 in the simulator, tier 2 on the real arm (--url)")
    q.add_argument("suite", nargs="?", default=os.path.join(AGENT_DIR, "evals", "tasks.yaml"))
    q.add_argument("--models", required=True,
                   help="comma-separated, e.g. claude-haiku-5-5,openai:gpt-5.5@high,gemini-4-argon,claude-code:sonnet")
    q.add_argument("--tier", type=int, choices=[1, 2], default=1)
    q.add_argument("--tasks", help="only these task ids (comma-separated)")
    q.add_argument("--modes", help="tools,code (default: each task's first mode)")
    q.add_argument("--trials", type=int, default=1)
    q.add_argument("--parallel", type=int, default=1, help="simulated arms run side by side (tier 1)")
    q.add_argument("--max-cost-usd", type=float, help="stop starting trials once this much is spent")
    q.add_argument("--no-skills", action="store_true", help="run without the skill library (an ablation)")
    q.add_argument("--out", help="results folder (default evals/results/<suite>-tier<N>-<time>)")
    q.add_argument("--resume", help="a results folder to continue; finished trials are skipped")
    q.add_argument("--seed", type=int, default=0, help="seed for the model order")

    q = ev_sub.add_parser("perception", help="tier 0: ask models questions about labelled camera frames")
    q.add_argument("dataset", nargs="?", default=os.path.join(AGENT_DIR, "evals", "perception.jsonl"))
    q.add_argument("--models", required=True)
    q.add_argument("--repeats", type=int, default=3)
    q.add_argument("--parallel", type=int, default=4)
    q.add_argument("--max-cost-usd", type=float)
    q.add_argument("--out", help="results folder; reuse one to continue")

    q = ev_sub.add_parser("label", help="build the perception set from traced camera frames")
    q.add_argument("runs", nargs="+", help="folders with run traces (for example a copy of the arm PC's runs/)")
    q.add_argument("--out", default=os.path.join(AGENT_DIR, "evals", "perception.jsonl"))
    q.add_argument("--rotate-180", action="store_true", help="turn frames upside down (recorded before the fix)")
    q.add_argument("--question", help="ask this about each frame, e.g. 'Is the block between the fingers?'")
    q.add_argument("--choices", help="allowed answers, comma-separated (default yes,no)")
    q.add_argument("--category", default="custom")
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--no-auto", action="store_true", help="skip the automatic gripper open/closed items")

    q = ev_sub.add_parser("report", help="success rate vs cost tables, and an HTML page with the chart")
    q.add_argument("results", nargs="+", help="results folders or results.jsonl files")
    q.add_argument("--html", help="also write an HTML report here")


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
    add_provider_args(p)
    p.add_argument("--attempts", type=int)
    add_safety_args(p)

    p = sub.add_parser("serve", help="run tasks typed into the web page's agent card")
    add_provider_args(p)
    p.add_argument("--attempts", type=int)
    add_safety_args(p)

    p = sub.add_parser("login", help="sign in to Claude Code, for --provider claude-code")
    p.add_argument("--status", action="store_true", help="only show whether it is signed in")

    p = sub.add_parser("skills", help="manage the skill library")
    p.add_argument("action", nargs="?", default="list", choices=["list", "pending", "show", "approve", "reject"])
    p.add_argument("name", nargs="?")

    p = sub.add_parser("mcp", help="serve the primitives over MCP")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--web-operator", action="store_true", help="add an ask_operator tool answered on the web page")
    add_safety_args(p)

    add_eval_args(sub)

    args = parser.parse_args(argv)
    if args.command == "skills" and args.action in ("show", "approve", "reject") and not args.name:
        parser.error(f"skills {args.action} needs a NAME")
    config = load_config(args.config)
    try:
        commands = {"observe": cmd_observe, "run": cmd_run, "serve": cmd_serve, "login": cmd_login,
                    "skills": cmd_skills, "mcp": cmd_mcp, "eval": cmd_eval}
        return commands[args.command](config, args) or 0
    except (K1Error, ValueError, ClaudeCodeError, EvalError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
