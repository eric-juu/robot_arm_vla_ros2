"""
Blocking client for the k1_web server, for agents and scripts. No ROS needed.

    with K1Client("http://127.0.0.1:8080", token) as k1:
        k1.request({"type": "goal", "positions": [0.3, 0, 0, 0, 0, 0]})
        k1.wait_for(lambda s: s["goal"] == s["commanded"], timeout=5)

A background thread reads the WebSocket and keeps the latest `state`; another sends a ping every
`heartbeat_sec` so the server's deadman doesn't stop the arm while the agent is thinking.
When a human presses STOP in the browser, every later call raises StoppedByOperator.
"""
import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

DEFAULT_URL = "http://127.0.0.1:8080"


class K1Error(Exception):
    pass


class StoppedByOperator(Exception):
    """A human pressed STOP. The current task must end; start a new run to continue."""


def default_token() -> str:
    return os.environ.get("K1_WEB_TOKEN") or os.environ.get("K1_WEB_ADMIN_PASSWORD", "")


class K1Client:

    def __init__(self, url: str = DEFAULT_URL, token: str | None = None, role: str = "agent",
                 heartbeat_sec: float = 0.25, timeout: float = 10.0):
        self.http_url = url.rstrip("/")
        if not self.http_url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        self.ws_url = "ws" + self.http_url[len("http"):] + "/ws"
        self.token = token if token is not None else default_token()
        if not self.token:
            raise K1Error("no token: set K1_WEB_TOKEN (or K1_WEB_ADMIN_PASSWORD)")
        self.role = role
        self.heartbeat_sec = heartbeat_sec
        self.timeout = timeout

        self._ws = None
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._state: dict | None = None
        self._state_seq = 0         # counts state messages, so waits can ignore states sent before a command
        self._replies: dict = {}
        self._ids = itertools.count(1)
        self._closed = False
        self._stopped = False
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ----

    def connect(self) -> "K1Client":
        try:
            # entering the context is the supported way to open it on every websockets version
            self._ws = connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self.token}"},
                               open_timeout=self.timeout).__enter__()
        except Exception as e:
            raise K1Error(f"could not connect to {self.ws_url}: {e}") from e
        for target in (self._read_loop, self._heartbeat_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        try:
            self.request({"type": "hello", "role": self.role})
        except K1Error as e:
            self.close()
            raise K1Error("k1_web did not answer 'hello'. The server is probably running older code: on the "
                          "arm PC, git pull, colcon build, source install/setup.bash, then restart ros2 launch") from e
        self.wait_for(lambda s: True, timeout=self.timeout)
        return self

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._ws is not None:
            self._ws.close()
        for thread in self._threads:
            thread.join(timeout=1.0)

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _read_loop(self):
        try:
            for text in self._ws:
                msg = json.loads(text)
                with self._cond:
                    kind = msg.get("type")
                    if kind == "state":
                        self._state = msg
                        self._state_seq += 1
                    elif kind == "event" and msg.get("name") == "stop" and msg.get("by") != self.role:
                        self._stopped = True
                    if "id" in msg:
                        self._replies[msg["id"]] = msg
                    self._cond.notify_all()
        except (ConnectionClosed, OSError):
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _heartbeat_loop(self):
        while not self._closed:
            try:
                self.send({"type": "ping"})
            except K1Error:
                return
            time.sleep(self.heartbeat_sec)

    # ---- messages ----

    @property
    def stopped(self) -> bool:
        return self._stopped

    def check_stopped(self):
        if self._stopped:
            raise StoppedByOperator("STOP was pressed by the operator")

    def send(self, msg: dict):
        if self._closed or self._ws is None:
            raise K1Error("not connected to k1_web")
        try:
            with self._send_lock:
                self._ws.send(json.dumps(msg))
        except (ConnectionClosed, OSError) as e:
            raise K1Error(f"connection lost: {e}") from e

    def request(self, msg: dict, timeout: float | None = None) -> dict:
        """Send a message and wait for its reply (ack, error, enable_result or info)."""
        msg_id = next(self._ids)
        self.send({**msg, "id": msg_id})
        deadline = time.monotonic() + (timeout or self.timeout)
        with self._cond:
            while msg_id not in self._replies:
                remaining = deadline - time.monotonic()
                if self._closed:
                    raise K1Error("connection lost")
                if remaining <= 0:
                    raise K1Error(f"no reply to {msg['type']!r}")
                self._cond.wait(remaining)
            return self._replies.pop(msg_id)

    @property
    def state(self) -> dict:
        with self._cond:
            if self._state is None:
                raise K1Error("no state received yet")
            return dict(self._state)

    @property
    def state_seq(self) -> int:
        with self._cond:
            return self._state_seq

    def wait_for(self, predicate, timeout: float, after_seq: int = -1) -> dict | None:
        """Wait until predicate(state) is true for a state newer than `after_seq`.
        Returns that state, or None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                self.check_stopped()
                if self._closed:
                    raise K1Error("connection lost")
                if self._state is not None and self._state_seq > after_seq and predicate(self._state):
                    return dict(self._state)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.1))

    def sleep(self, seconds: float):
        """Sleep, but wake up and raise if STOP is pressed."""
        self.wait_for(lambda s: False, timeout=seconds)

    def note(self, text: str):
        """Show a line in the web page's agent log."""
        self.send({"type": "agent_note", "text": text})

    def snapshot(self) -> bytes | None:
        """Latest camera frame as JPEG, or None when the camera has no frame."""
        req = urllib.request.Request(self.http_url + "/snapshot.jpg",
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 503:
                return None
            raise K1Error(f"snapshot failed: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise K1Error(f"snapshot failed: {e.reason}") from e
