"""
FastAPI app for the K1 web interface. No ROS imports; the ROS node hands it an ArmController,
a PoseStore and an async `enable_fn(bool) -> (success, message)`.

    GET  /              control page (login page when not authenticated)
    POST /login         {"token": "..."} sets the session cookie
    POST /logout
    GET  /static/<f>    UI assets
    GET  /video.mjpeg   camera as an MJPEG stream
    GET  /snapshot.jpg  latest camera frame
    WS   /ws            JSON protocol, see handle_message()

Everything except the login page and the UI assets needs the session cookie or an
`Authorization: Bearer <token>` header (for scripts and agents).
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


class WebConfig:
    def __init__(self, token: str, static_dir: str, state_rate_hz: float = 20.0,
                 video_max_fps: float = 15.0, secure_cookie: bool = False):
        self.token = token
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

    def is_authenticated(conn) -> bool:
        cookie = conn.cookies.get(COOKIE_NAME, "")
        if cookie and secrets.compare_digest(cookie, session_value):
            return True
        auth = conn.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return secrets.compare_digest(auth[7:].strip(), config.token)
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
        if not secrets.compare_digest(token, config.token):
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

    def state_message() -> dict:
        return {"type": "state", **controller.snapshot(), "poses": poses.names()}

    async def handle_message(msg) -> dict | None:
        """Apply one client message. Returns the reply to send, if any."""
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise CommandError("messages must be JSON objects with a 'type'")
        kind = msg["type"]
        if kind == "ping":
            return None
        if kind == "goal":
            controller.set_goal(msg.get("positions"))
            return None
        if kind == "gripper":
            controller.set_gripper(msg.get("value"))
            return None
        if kind == "stop":
            controller.stop()
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

        controller.client_connected()
        sender = asyncio.create_task(send_state())
        try:
            while True:
                text = await websocket.receive_text()
                controller.heartbeat()
                try:
                    reply = await handle_message(json.loads(text))
                except json.JSONDecodeError:
                    reply = {"type": "error", "message": "invalid JSON"}
                except CommandError as e:
                    reply = {"type": "error", "message": str(e)}
                if reply is not None:
                    await send(reply)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            controller.client_disconnected()

    return app
