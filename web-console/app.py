from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SCRIPTS = ROOT / "scripts"
MODE = os.getenv("MOSS_MODE", "mock").lower()  # mock | rdk

app = FastAPI(title="MOSS 550W Web Console", version="0.1.0")
clients: set[WebSocket] = set()
state: dict[str, Any] = {
    "mode": MODE,
    "agent": "standby",
    "camera": "ready",
    "voice": "ready",
    "hardware": "ready",
    "last_event": "system_initialized",
    "updated_at": int(time.time()),
}


class AgentRequest(BaseModel):
    message: str
    session_id: str | None = None


class ServoRequest(BaseModel):
    action: str


async def broadcast(event: str, payload: dict[str, Any] | None = None) -> None:
    message = {"event": event, "payload": payload or {}, "ts": int(time.time() * 1000)}
    state["last_event"] = event
    state["updated_at"] = int(time.time())
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def run_process(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "state": state}


@app.post("/api/agent/chat")
async def agent_chat(req: AgentRequest):
    message = req.message.strip()
    if not message:
        return {"ok": False, "error": "message is required"}

    session_id = req.session_id or f"web-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    state["agent"] = "thinking"
    await broadcast("agent.thinking", {"session_id": session_id, "message": message})

    if MODE == "mock":
        await asyncio.sleep(0.4)
        reply = f"[MOCK MOSS] 已收到任务：{message}。当前运行于 mock 模式，接入 RDK X5 后会调用真实 OpenClaw Agent。"
        state["agent"] = "standby"
        await broadcast("agent.reply", {"session_id": session_id, "reply": reply})
        return {"ok": True, "session_id": session_id, "reply": reply, "mode": MODE}

    code, stdout, stderr = await run_process([
        "openclaw", "agent",
        "--session-id", session_id,
        "-m", message,
        "--timeout", "600",
        "--thinking", "off",
    ], timeout=610)
    state["agent"] = "standby"
    if code != 0:
        await broadcast("agent.error", {"session_id": session_id, "stderr": stderr[-1200:]})
        return {"ok": False, "session_id": session_id, "error": stderr or f"exit code {code}"}
    reply = stdout.strip()
    await broadcast("agent.reply", {"session_id": session_id, "reply": reply})
    return {"ok": True, "session_id": session_id, "reply": reply, "mode": MODE}


@app.post("/api/camera/snapshot")
async def camera_snapshot():
    await broadcast("camera.capture.started")
    if MODE == "mock":
        await asyncio.sleep(0.25)
        await broadcast("camera.capture.completed", {"mock": True})
        return {"ok": True, "mock": True, "message": "mock snapshot completed"}

    output = ROOT / "media" / "web-snapshot.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr = await run_process(["python3", str(SCRIPTS / "snap.py"), str(output)], timeout=20)
    if code != 0:
        await broadcast("camera.capture.error", {"stderr": stderr[-800:]})
        return {"ok": False, "error": stderr or stdout}
    await broadcast("camera.capture.completed", {"path": str(output)})
    return {"ok": True, "path": str(output), "message": stdout.strip()}


@app.post("/api/hardware/servo")
async def servo(req: ServoRequest):
    action = req.action.strip().lower()
    allowed = {"dance", "idle"}
    if action not in allowed:
        return {"ok": False, "error": f"unsupported action: {action}"}
    await broadcast("hardware.servo.started", {"action": action})
    if MODE == "mock":
        await asyncio.sleep(0.25)
        await broadcast("hardware.servo.completed", {"action": action, "mock": True})
        return {"ok": True, "mock": True, "action": action}
    script = SCRIPTS / ("dance.py" if action == "dance" else "idle_motion.py")
    code, stdout, stderr = await run_process(["python3", str(script)], timeout=120)
    if code != 0:
        await broadcast("hardware.servo.error", {"action": action, "stderr": stderr[-800:]})
        return {"ok": False, "error": stderr or stdout}
    await broadcast("hardware.servo.completed", {"action": action})
    return {"ok": True, "action": action, "message": stdout.strip()}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json({"event": "system.state", "payload": state, "ts": int(time.time() * 1000)})
    try:
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_text("pong")
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"raw": raw}
                await ws.send_json({"event": "client.echo", "payload": data, "ts": int(time.time() * 1000)})
    except WebSocketDisconnect:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "5500")), reload=False)
