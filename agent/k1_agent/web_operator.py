"""
The human operator, through the web page instead of the terminal.

Questions appear in the page's agent card (a text answer, Yes/No, or Done); the answer comes back
over the agent's WebSocket. Same interface as runner.ConsoleOperator.
"""
import itertools
import os

from k1_agent.client import K1Client

NO_ANSWER = "(no answer from the operator)"


class WebOperator:

    def __init__(self, client: K1Client, timeout: float = 600.0):
        self.client = client
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._prefix = f"{os.getpid()}-"      # the MCP server and the service may both ask questions

    def _ask(self, text: str, kind: str) -> str | None:
        qid = f"{self._prefix}{next(self._ids)}"
        self.client.send({"type": "agent_question", "qid": qid, "text": text, "kind": kind})
        answer = self.client.next_message(
            lambda m: m.get("type") == "agent_answer" and m.get("qid") == qid, self.timeout)
        return None if answer is None else answer.get("text", "")

    def ask(self, question: str) -> str:
        answer = self._ask(question, "ask")
        return NO_ANSWER if answer is None else answer

    def confirm(self, question: str) -> bool:
        return (self._ask(question, "confirm") or "").strip().lower() in ("y", "yes")

    def wait_for_reset(self, message: str):
        self._ask(message, "reset")
