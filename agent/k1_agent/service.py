"""
The agent as a service: stays connected to k1_web and runs the tasks people type into the web
page's agent card, one at a time (`k1-agent serve`).

The page shows the service's status (idle / running, provider, model). k1_web refuses new prompts
while a task runs. STOP on the page ends the current task; the service then waits for the next prompt.
Questions from the agent appear on the page (WebOperator).
"""
from k1_agent.client import K1Client, K1Error
from k1_agent.runner import RunResult
from k1_agent.web_operator import WebOperator


class AgentService:

    def __init__(self, client: K1Client, run_task, info: dict, operator: WebOperator | None = None):
        """`run_task(text, mode, operator) -> RunResult` runs one task; `info` is shown on the page
        (provider, model, allow_motion)."""
        self.client = client
        self.run_task = run_task
        self.info = info
        self.operator = operator or WebOperator(client)

    def set_status(self, state: str, task: str = ""):
        self.client.request({"type": "agent_status", "status": {"state": state, "task": task, **self.info}})

    def handle(self, prompt: dict) -> RunResult | None:
        text, mode = prompt.get("text", ""), prompt.get("mode", "tools")
        self.client.clear_stopped()          # a STOP from an earlier task doesn't cancel this one
        self.set_status("running", text)
        try:
            return self.run_task(text, mode, self.operator)
        except K1Error:
            raise
        except Exception as e:               # report it and stay up for the next prompt
            self.client.note(f"agent error: {type(e).__name__}: {e}"[:300])
            return None
        finally:
            self.set_status("idle")

    def serve_forever(self, max_tasks: int | None = None):
        self.set_status("idle")
        self.client.note(f"agent service ready ({self.info.get('provider')}:{self.info.get('model') or 'default'})")
        done = 0
        while max_tasks is None or done < max_tasks:
            prompt = self.client.next_message(lambda m: m.get("type") == "agent_prompt", None, stop_aware=False)
            self.handle(prompt)
            done += 1
