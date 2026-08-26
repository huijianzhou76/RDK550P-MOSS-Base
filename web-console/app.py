from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from moss_runtime import MossRuntime, system_metrics
from moss_services import (
    HardwareService,
    MemoryService,
    MissionStore,
    OpenClawService,
    VoiceService,
)

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = Path(__file__).resolve().parent
STATIC = WEB_ROOT / "static"
DATA = WEB_ROOT / "data"
MEDIA = ROOT / "media"
MODE = os.getenv("MOSS_MODE", "mock").strip().lower()
if MODE not in {"mock", "rdk"}:
    MODE = "mock"

MEDIA.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="MOSS 550W Control API",
    description="Web control plane for RDK550P-MOSS + OpenClaw",
    version="0.2.0",
)
app.mount("/media", StaticFiles(directory=MEDIA), name="media")

runtime = MossRuntime(MODE)
agent = OpenClawService(MODE)
missions = MissionStore(DATA)
memory = MemoryService(ROOT / "openclaw-templates")
hardware = HardwareService(ROOT, MODE)
voice = VoiceService(MODE)
mission_tasks: dict[str, asyncio.Task[Any]] = {}


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None


class MissionRequest(BaseModel):
    title: str = Field(default="", max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    priority: str = "normal"
    auto_run: bool = True


class ServoRequest(BaseModel):
    action: str


class VoiceControlRequest(BaseModel):
    action: str


class MemoryWriteRequest(BaseModel):
    content: str


async def _run_mission(mission_id: str) -> None:
    mission = await missions.get(mission_id)
    if not mission:
        return

    await runtime.set_state(mission="running", agent="thinking")
    await missions.update(mission_id, status="running", progress=10)
    await runtime.broadcast(
        "mission.started",
        {"id": mission_id, "title": mission["title"], "priority": mission["priority"]},
    )
    await runtime.broadcast("mission.progress", {"id": mission_id, "progress": 25, "stage": "planning"})
    await missions.update(mission_id, progress=25)

    prompt = (
        "[MOSS Web Mission]\n"
        f"任务名称：{mission['title']}\n"
        f"优先级：{mission['priority']}\n"
        f"任务要求：{mission['prompt']}\n\n"
        "请作为 MOSS 执行任务。需要调用现有工具时先执行工具，再给出最终可交付结果。"
    )

    await runtime.broadcast("mission.progress", {"id": mission_id, "progress": 45, "stage": "agent_execution"})
    await missions.update(mission_id, progress=45)
    result = await agent.chat(prompt, mission.get("session_id"))

    if not result.get("ok"):
        error = result.get("error", "unknown agent error")
        await missions.update(
            mission_id,
            status="failed",
            progress=100,
            error=error,
            session_id=result.get("session_id"),
        )
        await runtime.set_state(mission="idle", agent="standby")
        await runtime.broadcast("mission.failed", {"id": mission_id, "error": error})
        return

    await runtime.broadcast("mission.progress", {"id": mission_id, "progress": 85, "stage": "verification"})
    await missions.update(mission_id, progress=85, session_id=result.get("session_id"))
    await asyncio.sleep(0.15 if MODE == "mock" else 0)
    reply = result.get("reply", "")
    updated = await missions.update(
        mission_id,
        status="completed",
        progress=100,
        result=reply,
        session_id=result.get("session_id"),
    )
    await runtime.set_state(mission="idle", agent="standby")
    await runtime.broadcast(
        "mission.completed",
        {"id": mission_id, "result": reply, "mission": updated},
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    voice_state = await voice.status()
    runtime.state["voice"] = "active" if voice_state.get("active") else "inactive"
    return {
        "ok": True,
        "version": app.version,
        "state": runtime.snapshot(),
        "voice": voice_state,
    }


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    return {
        "mode": MODE,
        "capabilities": [
            {"id": "agent", "name": "OpenClaw Agent", "available": True},
            {"id": "missions", "name": "Mission orchestration", "available": True},
            {"id": "memory", "name": "SOUL / Memory documents", "available": True},
            {"id": "vision", "name": "IMX477 snapshot", "available": True},
            {"id": "voice", "name": "Voice Assistant service", "available": True},
            {"id": "servo", "name": "PWM servo actions", "available": True},
            {"id": "realtime", "name": "WebSocket event bus", "available": True},
        ],
    }


@app.get("/api/system/metrics")
async def metrics() -> dict[str, Any]:
    return {"ok": True, "metrics": system_metrics(ROOT)}


@app.get("/api/events")
async def events(limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "events": runtime.recent_events(limit)}


@app.post("/api/agent/chat")
async def agent_chat(req: AgentRequest) -> dict[str, Any]:
    message = req.message.strip()
    await runtime.set_state(agent="thinking")
    await runtime.broadcast("agent.thinking", {"message": message, "session_id": req.session_id})
    result = await agent.chat(message, req.session_id)
    await runtime.set_state(agent="standby")
    if result.get("ok"):
        await runtime.broadcast("agent.reply", result)
    else:
        await runtime.broadcast("agent.error", result)
    return result


@app.get("/api/missions")
async def list_missions() -> dict[str, Any]:
    return {"ok": True, "missions": await missions.list()}


@app.post("/api/missions")
async def create_mission(req: MissionRequest) -> dict[str, Any]:
    mission = await missions.create(req.title, req.prompt, req.priority)
    await runtime.broadcast("mission.created", mission)
    if req.auto_run:
        task = asyncio.create_task(_run_mission(mission["id"]))
        mission_tasks[mission["id"]] = task
        task.add_done_callback(lambda _task, mid=mission["id"]: mission_tasks.pop(mid, None))
    return {"ok": True, "mission": mission}


@app.get("/api/missions/{mission_id}")
async def get_mission(mission_id: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    return {"ok": True, "mission": mission}


@app.post("/api/missions/{mission_id}/run")
async def run_mission(mission_id: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    task = mission_tasks.get(mission_id)
    if task and not task.done():
        return {"ok": False, "error": "mission already running"}
    await missions.update(mission_id, status="queued", progress=0, error=None)
    task = asyncio.create_task(_run_mission(mission_id))
    mission_tasks[mission_id] = task
    task.add_done_callback(lambda _task, mid=mission_id: mission_tasks.pop(mid, None))
    return {"ok": True, "mission_id": mission_id}


@app.get("/api/memory")
async def list_memory_documents() -> dict[str, Any]:
    return {"ok": True, "documents": memory.list()}


@app.get("/api/memory/{key}")
async def read_memory_document(key: str) -> dict[str, Any]:
    try:
        document = memory.read(key)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown memory document")
    return {"ok": True, "document": document}


@app.put("/api/memory/{key}")
async def write_memory_document(key: str, req: MemoryWriteRequest) -> dict[str, Any]:
    try:
        document = memory.write(key, req.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown memory document")
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    await runtime.broadcast("memory.updated", {"key": key.upper(), "size": len(req.content.encode("utf-8"))})
    return {"ok": True, "document": document}


@app.post("/api/camera/snapshot")
async def camera_snapshot() -> dict[str, Any]:
    await runtime.set_state(camera="capturing")
    await runtime.broadcast("camera.capture.started")
    result = await hardware.snapshot()
    await runtime.set_state(camera="ready" if result.get("ok") else "error")
    await runtime.broadcast(
        "camera.capture.completed" if result.get("ok") else "camera.capture.error",
        result,
    )
    return result


@app.get("/api/voice/status")
async def voice_status() -> dict[str, Any]:
    result = await voice.status()
    await runtime.set_state(voice="active" if result.get("active") else "inactive")
    return result


@app.post("/api/voice/control")
async def voice_control(req: VoiceControlRequest) -> dict[str, Any]:
    await runtime.broadcast("voice.control.started", {"action": req.action})
    result = await voice.control(req.action.strip().lower())
    await runtime.set_state(voice="active" if result.get("active") else "inactive")
    await runtime.broadcast(
        "voice.control.completed" if result.get("ok") else "voice.control.error",
        {"action": req.action, **result},
    )
    return result


@app.post("/api/hardware/servo")
async def servo(req: ServoRequest) -> dict[str, Any]:
    action = req.action.strip().lower()
    await runtime.set_state(hardware="busy")
    await runtime.broadcast("hardware.servo.started", {"action": action})
    result = await hardware.servo(action)
    await runtime.set_state(hardware="ready" if result.get("ok") else "error")
    await runtime.broadcast(
        "hardware.servo.completed" if result.get("ok") else "hardware.servo.error",
        result,
    )
    return result


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await runtime.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_text("pong")
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw}
            await ws.send_json(
                {"event": "client.echo", "payload": data, "ts": int(time.time() * 1000)}
            )
    except WebSocketDisconnect:
        runtime.disconnect(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5500")),
        reload=False,
    )
