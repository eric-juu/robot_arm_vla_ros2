"""
The agent as a service: stays connected to k1_web and runs the tasks people type into the web
page's agent card, one at a time (`k1-agent serve`).

The page shows the service's status (idle / running, provider, model). k1_web refuses new prompts
while a task runs. STOP on the page ends the current task; the service then waits for the next prompt.
Questions from the agent appear on the page (WebOperator).

With several models (`k1-agent serve --models ...`) the page gets a model menu, and each prompt names
the model to run it with. The safety flags are the same for every model.
"""
from k1_agent.client import K1Client, K1Error
from k1_agent.runner import RunResult
from k1_agent.web_operator import WebOperator


class AgentService:

    def __init__(self, client: K1Client, run_task, info: dict, operator: WebOperator | None = None,
                 choices: dict | None = None):
        """`run_task(text, mode, operator) -> RunResult` runs one task; `info` is shown on the page
        (provider, model, allow_motion). `choices` maps model labels the page may pick, default first,
        to their own (run_task, info); without it every task uses `run_task`."""
        self.client = client
        self.run_task = run_task
        self.info = info
        self.operator = operator or WebOperator(client)
        self.choices = choices or {}

    def set_status(self, state: str, task: str = "", info: dict | None = None):
        status = {"state": state, "task": task, **(info or self.info)}
        if self.choices:
            status["models"] = list(self.choices)
        self.client.request({"type": "agent_status", "status": status})

    def pick(self, label: str | None):
        """(run_task, info) for the model the prompt names; the default when it names none."""
        if not self.choices:
            return self.run_task, self.info
        if label is None:
            label = next(iter(self.choices))
        if label not in self.choices:
            raise ValueError(f"model {label!r} isn't offered by this service; choose one of {list(self.choices)}")
        return self.choices[label]

    def handle(self, prompt: dict) -> RunResult | None:
        text, mode = prompt.get("text", ""), prompt.get("mode", "tools")
        self.client.clear_stopped()          # a STOP from an earlier task doesn't cancel this one
        try:
            run_task, info = self.pick(prompt.get("model"))
        except ValueError as e:
            self.client.note(f"agent error: {e}"[:300])
            return None
        self.set_status("running", text, info)
        try:
            return run_task(text, mode, self.operator)
        except K1Error:
            raise
        except Exception as e:               # report it and stay up for the next prompt
            self.client.note(f"agent error: {type(e).__name__}: {e}"[:300])
            return None
        finally:
            self.set_status("idle")

    def serve_forever(self, max_tasks: int | None = None):
        self.set_status("idle")
        offered = ", ".join(self.choices) or f"{self.info.get('provider')}:{self.info.get('model') or 'default'}"
        self.client.note(f"agent service ready ({offered})")
        done = 0
        while max_tasks is None or done < max_tasks:
            prompt = self.client.next_message(lambda m: m.get("type") == "agent_prompt", None, stop_aware=False)
            self.handle(prompt)
            done += 1
