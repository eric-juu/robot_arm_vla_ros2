"""
FastAPI app for the K1 web interface. No ROS imports; the ROS node hands it an ArmController,
a PoseStore and an async `enable_fn(bool) -> (success, message)`.

    GET  /              control page (login page when not authenticated)
    POST /login         {"token": "..."} sets the session cookie
    POST /logout
    GET  /static/<f>    UI assets
    GET  /video.mjpeg   camera as an MJPEG stream
    GET  /snapshot.jpg  latest camera frame
    WS   /ws            JSON protocol, see handle_message(). A message with an "id" gets exactly
                        one reply (ack, error, enable_result or info) echoing that id.

Everything except the login page and the UI assets needs the session cookie or an
`Authorization: Bearer <token>` header (for scripts and agents). The optional admin password
(K1_WEB_ADMIN_PASSWORD) works anywhere the token does.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from k1_web.control import ArmController, CommandError
from k1_web.poses import PoseStore

COOKIE_NAME = "k1_session"
STATIC_FILES = {"app.js", "style.css"}
MJPEG_BOUNDARY = "k1frame"
CLIENT_ROLES = ("human", "agent")
MAX_NOTE_CHARS = 500
MAX_PROMPT_CHARS = 2000
AGENT_MODES = ("tools", "code")
MAX_AGENT_MODELS = 20  # models an agent service may offer the page
QUESTION_KINDS = ("ask", "confirm", "reset")


class WebConfig:
    def __init__(self, token: str, static_dir: str, state_rate_hz: float = 20.0,
                 video_max_fps: float = 15.0, secure_cookie: bool = False, admin_password: str = ""):
        self.token = token
        self.admin_password = admin_password     # optional second password (K1_WEB_ADMIN_PASSWORD), "" = off
        self.static_dir = static_dir
        self.state_rate_hz = state_rate_hz
        self.video_max_fps = video_max_fps
        self.secure_cookie = secure_cookie


def create_app(controller: ArmController, poses: PoseStore, enable_fn, config: WebConfig) -> FastAPI:
    if not config.token:
        raise ValueError("an auth token is required")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # the cookie holds a value derived from the token, not the token itself
    session_value = hmac.new(config.token.encode(), b"k1_web_session", hashlib.sha256).hexdigest()

    def is_valid_secret(value: str) -> bool:
        valid = secrets.compare_digest(value, config.token)
        if config.admin_password:
            valid |= secrets.compare_digest(value, config.admin_password)
        return valid

    def is_authenticated(conn) -> bool:
        cookie = conn.cookies.get(COOKIE_NAME, "")
        if cookie and secrets.compare_digest(cookie, session_value):
            return True
        auth = conn.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return is_valid_secret(auth[7:].strip())
        return False

    def require_auth(request: Request):
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="not authenticated")

    # ---- pages ----

    @app.get("/")
    async def index(request: Request):
        page = "index.html" if is_authenticated(request) else "login.html"
        return FileResponse(os.path.join(config.static_dir, page), headers={"Cache-Control": "no-store"})

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in STATIC_FILES:
            raise HTTPException(status_code=404)
        return FileResponse(os.path.join(config.static_dir, name), headers={"Cache-Control": "no-cache"})

    @app.post("/login")
    async def login(request: Request):
        try:
            token = str((await request.json()).get("token", "")).strip()   # forgive copy-paste whitespace
        except (ValueError, AttributeError):
            token = ""
        if not is_valid_secret(token):
            await asyncio.sleep(1.0)    # slow down guessing
            return JSONResponse({"ok": False, "message": "wrong token"}, status_code=401)
        response = JSONResponse({"ok": True})
        # behind a tunnel the scheme is https (uvicorn applies X-Forwarded-Proto from 127.0.0.1),
        # so the cookie is https-only there while plain http://127.0.0.1 still works on the PC
        secure = config.secure_cookie or request.url.scheme == "https"
        response.set_cookie(COOKIE_NAME, session_value, httponly=True, samesite="strict",
                            secure=secure, max_age=30 * 24 * 3600)
        return response

    @app.post("/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME)
        return response

    # ---- camera ----

    @app.get("/snapshot.jpg")
    async def snapshot(request: Request):
        require_auth(request)
        _, jpeg = controller.latest_image()
        if jpeg is None:
            raise HTTPException(status_code=503, detail="no camera frame yet")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/video.mjpeg")
    async def video(request: Request):
        require_auth(request)
        period = 1.0 / config.video_max_fps

        async def frames():
            last_seq = -1
            while not await request.is_disconnected():
                seq, jpeg = controller.latest_image()
                if jpeg is not None and seq != last_seq:
                    last_seq = seq
                    yield (f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                           f"Content-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"
                await asyncio.sleep(period)

        return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
                                 headers={"Cache-Control": "no-store"})

    # ---- control ----

    clients: dict[int, dict] = {}       # connection id -> {"role", "send", "status"}

    def agent_status() -> dict | None:
        """Status of the agent service (`k1-agent serve`) that takes prompts, if one is connected."""
        for c in clients.values():
            if c["role"] == "agent" and c.get("status"):
                return c["status"]
        return None

    def state_message() -> dict:
        roles = [c["role"] for c in clients.values()]
        return {"type": "state", **controller.snapshot(), "poses": poses.names(),
                "clients": {role: roles.count(role) for role in CLIENT_ROLES}, "agent": agent_status()}

    async def send_to(role: str, msg: dict):
        for client in list(clients.values()):
            if client["role"] == role:
                try:
                    await client["send"](msg)
                except Exception:
                    pass

    def text_field(msg, name: str, limit: int) -> str:
        text = msg.get(name)
        if not isinstance(text, str) or not text.strip():
            raise CommandError(f"'{name}' must be a non-empty string")
        return text.strip()[:limit]

    def require_role(conn: dict, role: str, kind: str):
        if conn["role"] != role:
            raise CommandError(f"{kind!r} is only for {role} clients")

    async def broadcast(msg: dict):
        for client in list(clients.values()):
            try:
                await client["send"](msg)
            except Exception:       # that connection is closing, its own handler cleans up
                pass

    async def handle_message(msg, conn: dict) -> dict | None:
        """Apply one client message. Returns the reply to send, if any."""
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise CommandError("messages must be JSON objects with a 'type'")
        kind = msg["type"]
        if kind == "ping":
            return None
        if kind == "hello":
            role = msg.get("role")
            if role not in CLIENT_ROLES:
                raise CommandError(f"'role' must be one of {list(CLIENT_ROLES)}")
            conn["role"] = role
            return None
        if kind == "goal":
            controller.set_goal(msg.get("positions"))
            return None
        if kind == "gripper":
            controller.set_gripper(msg.get("value"))
            return None
        if kind == "stop":
            controller.stop()
            # everyone hears about it, so an agent aborts its task when a human presses STOP
            await broadcast({"type": "event", "name": "stop", "by": conn["role"]})
            return None
        if kind == "pose":
            pose = poses.get(msg.get("name"))
            controller.set_goal(pose["joints"])
            if pose["gripper"] is not None:
                controller.set_gripper(pose["gripper"])
            return None
        if kind == "enable":
            value = msg.get("value")
            if not isinstance(value, bool):
                raise CommandError("'value' must be true or false")
            success, message = await enable_fn(value)
            return {"type": "enable_result", "success": success, "message": message}
        if kind == "save_pose":
            name = msg.get("name")
            poses.save(name, controller.measured_arm(), controller.gripper)
            return {"type": "info", "message": f"saved pose {name!r}"}
        if kind == "agent_note":
            await broadcast({"type": "agent_note", "text": text_field(msg, "text", MAX_NOTE_CHARS), "by": conn["role"]})
            return None
        # ---- prompting the agent service from the page ----
        if kind == "agent_prompt":         # human -> agent service
            require_role(conn, "human", kind)
            text = text_field(msg, "text", MAX_PROMPT_CHARS)
            mode = msg.get("mode", "tools")
            if mode not in AGENT_MODES:
                raise CommandError(f"'mode' must be one of {list(AGENT_MODES)}")
            status = agent_status()
            if status is None:
                raise CommandError("no agent service is running (start `k1-agent serve` on the PC)")
            if status.get("state") != "idle":
                raise CommandError("the agent is busy; wait for it to finish, or press STOP")
            prompt = {"type": "agent_prompt", "text": text, "mode": mode}
            if msg.get("model") is not None:     # one of the models the service offers (`serve --models`)
                if msg["model"] not in status.get("models", []):
                    raise CommandError(f"'model' must be one of {status.get('models', [])}")
                prompt["model"] = msg["model"]
            await send_to("agent", prompt)
            await broadcast({"type": "agent_note", "text": f"prompt: {text[:MAX_NOTE_CHARS]}", "by": "human"})
            return None
        if kind == "agent_status":         # agent service -> everyone, through state["agent"]
            require_role(conn, "agent", kind)
            status = msg.get("status")
            if not isinstance(status, dict) or status.get("state") not in ("idle", "running"):
                raise CommandError("'status' must be an object with state 'idle' or 'running'")
            models = status.get("models")
            if models is not None and (not isinstance(models, list) or len(models) > MAX_AGENT_MODELS):
                raise CommandError(f"'models' must be a list of at most {MAX_AGENT_MODELS} names")
            conn["status"] = {k: (bool(v) if k == "allow_motion" else [str(m)[:200] for m in v] if k == "models"
                                  else str(v)[:200]) for k, v in status.items()
                              if k in ("state", "task", "provider", "model", "models", "allow_motion")}
            return None
        if kind == "agent_question":       # agent -> humans
            require_role(conn, "agent", kind)
            question_kind = msg.get("kind", "ask")
            if question_kind not in QUESTION_KINDS:
                raise CommandError(f"'kind' must be one of {list(QUESTION_KINDS)}")
            await send_to("human", {"type": "agent_question", "qid": str(msg.get("qid", ""))[:64],
                                    "text": text_field(msg, "text", MAX_NOTE_CHARS), "kind": question_kind})
            return None
        if kind == "agent_answer":         # human -> agents, and to other pages so they close the question
            require_role(conn, "human", kind)
            answer = msg.get("text", "")
            if not isinstance(answer, str):
                raise CommandError("'text' must be a string")
            await broadcast({"type": "agent_answer", "qid": str(msg.get("qid", ""))[:64],
                             "text": answer.strip()[:MAX_NOTE_CHARS]})
            return None
        raise CommandError(f"unknown message type {kind!r}")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not is_authenticated(websocket):
            await websocket.close(code=1008)    # policy violation
            return
        await websocket.accept()
        send_lock = asyncio.Lock()

        async def send(msg: dict):
            async with send_lock:
                await websocket.send_text(json.dumps(msg))

        async def send_state():
            while True:
                await send(state_message())
                await asyncio.sleep(1.0 / config.state_rate_hz)

        conn = {"role": "human", "send": send}
        clients[id(conn)] = conn
        controller.client_connected()
        sender = asyncio.create_task(send_state())
        try:
            while True:
                text = await websocket.receive_text()
                controller.heartbeat()
                msg_id = None
                try:
                    msg = json.loads(text)
                    msg_id = msg.get("id") if isinstance(msg, dict) else None
                    reply = await handle_message(msg, conn)
                except json.JSONDecodeError:
                    reply = {"type": "error", "message": "invalid JSON"}
                except CommandError as e:
                    reply = {"type": "error", "message": str(e)}
                # a message with an "id" always gets exactly one reply carrying that id
                if msg_id is not None:
                    reply = {**(reply or {"type": "ack"}), "id": msg_id}
                if reply is not None:
                    await send(reply)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            clients.pop(id(conn), None)
            controller.client_disconnected()

    return app
