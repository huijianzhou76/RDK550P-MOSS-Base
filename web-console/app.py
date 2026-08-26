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

from moss_autonomy import AutonomyEngine, EvidenceStore, HeartbeatEngine, PolicyEngine
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
    description="Autonomous control plane for RDK550P-MOSS + OpenClaw",
    version="0.4.0",
)
app.mount("/media", StaticFiles(directory=MEDIA), name="media")

runtime = MossRuntime(MODE)
agent = OpenClawService(MODE)
missions = MissionStore(DATA)
memory = MemoryService(ROOT / "openclaw-templates")
hardware = HardwareService(ROOT, MODE)
voice = VoiceService(MODE)
policy = PolicyEngine()
evidence = EvidenceStore(DATA)
autonomy = AutonomyEngine(agent, policy, evidence, MODE)
mission_tasks: dict[str, asyncio.Task[Any]] = {}


async def emit(event: str, payload: dict[str, Any] | None = None) -> None:
    await runtime.broadcast(event, payload)


heartbeat = HeartbeatEngine(
    interval_seconds=int(os.getenv("MOSS_HEARTBEAT_INTERVAL", "30")),
    metrics_provider=lambda: system_metrics(ROOT),
    voice_status_provider=voice.status,
    emit=emit,
)


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = None


class MissionRequest(BaseModel):
    title: str = Field(default="", max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    priority: str = "normal"
    auto_run: bool = True
    timeout_seconds: int = Field(default=900, ge=30, le=3600)


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=1000)


class ServoRequest(BaseModel):
    action: str


class VoiceControlRequest(BaseModel):
    action: str


class MemoryWriteRequest(BaseModel):
    content: str


async def _mission_stage(
    mission_id: str,
    stage: str,
    progress: int,
    payload: dict[str, Any],
) -> None:
    updates: dict[str, Any] = {"progress": progress, "stage": stage}
    if "plan" in payload:
        updates["plan"] = payload["plan"]
    if "risk" in payload:
        updates["risk"] = payload["risk"]
    if "verification_passed" in payload:
        updates["verification_passed"] = payload["verification_passed"]
    await missions.update(mission_id, **updates)
    await runtime.broadcast(
        "mission.progress",
        {"id": mission_id, "stage": stage, "progress": progress, **payload},
    )


async def _run_mission(mission_id: str) -> None:
    mission = await missions.get(mission_id)
    if not mission:
        return

    timeout_seconds = max(30, min(int(mission.get("timeout_seconds") or 900), 3600))
    await runtime.set_state(mission="running", agent="thinking")
    await missions.update(
        mission_id,
        status="running",
        progress=5,
        stage="analysis",
        error=None,
        started_at=int(time.time()),
    )
    await runtime.broadcast(
        "mission.started",
        {
            "id": mission_id,
            "title": mission["title"],
            "priority": mission["priority"],
            "timeout_seconds": timeout_seconds,
        },
    )

    try:
        result = await asyncio.wait_for(
            autonomy.execute(
                mission,
                lambda stage, progress, payload: _mission_stage(
                    mission_id, stage, progress, payload
                ),
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        error = f"mission exceeded execution budget: {timeout_seconds}s"
        await missions.update(
            mission_id,
            status="timed_out",
            stage="timed_out",
            progress=100,
            error=error,
            finished_at=int(time.time()),
        )
        await evidence.append(
            mission_id,
            "mission.timed_out",
            {"timeout_seconds": timeout_seconds},
            actor="moss-core",
        )
        await runtime.broadcast(
            "mission.timed_out",
            {"id": mission_id, "timeout_seconds": timeout_seconds},
        )
        await runtime.set_state(mission="idle", agent="standby")
        return
    except asyncio.CancelledError:
        latest = await missions.get(mission_id) or {}
        status = latest.get("status")
        if status not in {"paused", "cancelled"}:
            status = "cancelled"
            await missions.update(
                mission_id,
                status=status,
                stage=status,
                finished_at=int(time.time()),
            )
        await evidence.append(
            mission_id,
            f"mission.{status}",
            {"stage": latest.get("stage"), "progress": latest.get("progress")},
            actor="human-operator",
        )
        await runtime.broadcast(
            f"mission.{status}",
            {"id": mission_id, "progress": latest.get("progress", 0)},
        )
        await runtime.set_state(mission="idle", agent="standby")
        return
    except Exception as exc:
        error = f"autonomy engine error: {exc}"
        await missions.update(
            mission_id,
            status="failed",
            stage="failed",
            progress=100,
            error=error,
            finished_at=int(time.time()),
        )
        await evidence.append(
            mission_id,
            "mission.exception",
            {"error": error},
            actor="moss-core",
        )
        await runtime.broadcast("mission.failed", {"id": mission_id, "error": error})
        await runtime.set_state(mission="idle", agent="standby")
        return

    if result.get("awaiting_approval"):
        risk = result["risk"]
        plan = result["plan"]
        await missions.update(
            mission_id,
            status="awaiting_approval",
            stage="approval",
            progress=25,
            risk=risk,
            plan=plan,
        )
        await runtime.set_state(
            mission="awaiting_approval",
            agent="standby",
            risk=risk["level"],
        )
        await runtime.broadcast(
            "mission.approval_required",
            {"id": mission_id, "risk": risk, "plan": plan},
        )
        return

    if not result.get("ok"):
        error = result.get("error", "unknown autonomous execution error")
        await missions.update(
            mission_id,
            status="failed",
            stage="failed",
            progress=100,
            error=error,
            risk=result.get("risk"),
            plan=result.get("plan"),
            finished_at=int(time.time()),
        )
        await runtime.set_state(mission="idle", agent="standby")
        await runtime.broadcast("mission.failed", {"id": mission_id, "error": error})
        return

    status = "completed" if result.get("verification_passed") else "review_required"
    updated = await missions.update(
        mission_id,
        status=status,
        stage="completed",
        progress=100,
        result=result.get("result", ""),
        plan=result.get("plan"),
        risk=result.get("risk"),
        verification=result.get("verification", ""),
        verification_passed=result.get("verification_passed", False),
        sessions=result.get("sessions", {}),
        evidence_id=result.get("evidence_id"),
        finished_at=int(time.time()),
    )
    await runtime.set_state(mission="idle", agent="standby", risk="low")
    await runtime.broadcast(
        "mission.completed" if status == "completed" else "mission.review_required",
        {"id": mission_id, "mission": updated},
    )


def _schedule_mission(mission_id: str) -> bool:
    task = mission_tasks.get(mission_id)
    if task and not task.done():
        return False
    task = asyncio.create_task(_run_mission(mission_id))
    mission_tasks[mission_id] = task
    task.add_done_callback(
        lambda _task, mid=mission_id: mission_tasks.pop(mid, None)
    )
    return True


async def _interrupt_mission(mission_id: str, status: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    terminal = {"completed", "review_required", "failed", "timed_out", "cancelled", "rejected"}
    if mission.get("status") in terminal:
        return {"ok": False, "error": f"mission already terminal: {mission.get('status')}"}

    updated = await missions.update(
        mission_id,
        status=status,
        stage=status,
        interrupted_at=int(time.time()),
    )
    task = mission_tasks.get(mission_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        await evidence.append(
            mission_id,
            f"mission.{status}",
            {"previous_status": mission.get("status")},
            actor="human-operator",
        )
        await runtime.broadcast(f"mission.{status}", {"id": mission_id})
    return {"ok": True, "mission": updated}


@app.on_event("startup")
async def startup() -> None:
    heartbeat.start()
    await runtime.broadcast(
        "system.autonomy_ready",
        {
            "version": app.version,
            "mode": MODE,
            "heartbeat_interval": heartbeat.interval_seconds,
        },
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    await heartbeat.stop()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/autonomy")
async def autonomy_console() -> FileResponse:
    return FileResponse(STATIC / "autonomy.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    voice_state = await voice.status()
    runtime.state["voice"] = "active" if voice_state.get("active") else "inactive"
    return {
        "ok": True,
        "version": app.version,
        "state": runtime.snapshot(),
        "voice": voice_state,
        "autonomy": {
            "enabled": True,
            "policy": "risk-gated",
            "heartbeat_interval": heartbeat.interval_seconds,
        },
    }


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    return {
        "mode": MODE,
        "capabilities": [
            {"id": "agent", "name": "OpenClaw Agent", "available": True, "risk": "variable"},
            {"id": "planner", "name": "Planner role", "available": True, "risk": "low"},
            {"id": "verifier", "name": "Independent verifier role", "available": True, "risk": "low"},
            {"id": "missions", "name": "Autonomous mission orchestration", "available": True, "risk": "variable"},
            {"id": "policy", "name": "Local risk / approval engine", "available": True, "risk": "low"},
            {"id": "evidence", "name": "Hashed execution evidence", "available": True, "risk": "low"},
            {"id": "heartbeat", "name": "Autonomous health observer", "available": True, "risk": "low"},
            {"id": "mission-control", "name": "Pause / resume / cancel / timeout budgets", "available": True, "risk": "low"},
            {"id": "memory", "name": "SOUL / Memory documents", "available": True, "risk": "medium"},
            {"id": "vision", "name": "IMX477 snapshot", "available": True, "risk": "medium"},
            {"id": "voice", "name": "Voice Assistant service", "available": True, "risk": "medium"},
            {"id": "servo", "name": "PWM servo actions", "available": True, "risk": "medium"},
            {"id": "realtime", "name": "WebSocket event bus", "available": True, "risk": "low"},
        ],
    }


@app.get("/api/system/metrics")
async def metrics() -> dict[str, Any]:
    return {"ok": True, "metrics": system_metrics(ROOT)}


@app.get("/api/events")
async def events(limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "events": runtime.recent_events(limit)}


@app.post("/api/policy/assess")
async def assess_policy(req: AgentRequest) -> dict[str, Any]:
    return {"ok": True, "risk": policy.assess(req.message)}


@app.get("/api/evidence")
async def list_evidence(mission_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    return {
        "ok": True,
        "evidence": await evidence.list(mission_id=mission_id, limit=limit),
    }


@app.post("/api/agent/chat")
async def agent_chat(req: AgentRequest) -> dict[str, Any]:
    message = req.message.strip()
    risk = policy.assess(message)
    if risk["approval_required"]:
        await runtime.broadcast(
            "agent.blocked_by_policy",
            {"message": message, "risk": risk},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": "高风险请求必须通过 Mission 创建并完成人工批准",
                "risk": risk,
            },
        )

    await runtime.set_state(agent="thinking", risk=risk["level"])
    await runtime.broadcast(
        "agent.thinking",
        {"message": message, "session_id": req.session_id, "risk": risk},
    )
    safe_message = (
        "[MOSS Interactive Chat]\n"
        "这是交互问答通道，不得执行会改变操作系统、删除数据或修改持久化安全配置的高风险动作。"
        "如用户要求此类动作，请建议通过 Mission 审批流程。\n"
        f"用户消息：{message}"
    )
    result = await agent.chat(safe_message, req.session_id)
    await runtime.set_state(agent="standby", risk="low")
    if result.get("ok"):
        await runtime.broadcast("agent.reply", {**result, "risk": risk})
    else:
        await runtime.broadcast("agent.error", {**result, "risk": risk})
    return {**result, "risk": risk}


@app.get("/api/missions")
async def list_missions() -> dict[str, Any]:
    return {"ok": True, "missions": await missions.list()}


@app.post("/api/missions")
async def create_mission(req: MissionRequest) -> dict[str, Any]:
    mission = await missions.create(req.title, req.prompt, req.priority)
    analysis = await autonomy.analyze(mission)
    mission = await missions.update(
        mission["id"],
        plan=analysis["plan"],
        risk=analysis["risk"],
        stage="queued",
        timeout_seconds=req.timeout_seconds,
    ) or mission
    await runtime.broadcast("mission.created", mission)
    if req.auto_run:
        _schedule_mission(mission["id"])
    return {"ok": True, "mission": mission}


@app.get("/api/missions/{mission_id}")
async def get_mission(mission_id: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    return {
        "ok": True,
        "mission": mission,
        "evidence": await evidence.list(mission_id=mission_id, limit=200),
    }


@app.post("/api/missions/{mission_id}/run")
async def run_mission(mission_id: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    if mission.get("approval_status") == "rejected":
        return {"ok": False, "error": "mission was rejected"}
    if mission.get("status") == "cancelled":
        return {"ok": False, "error": "cancelled mission must be recreated"}
    if not _schedule_mission(mission_id):
        return {"ok": False, "error": "mission already running"}
    await missions.update(mission_id, status="queued", progress=0, error=None)
    return {"ok": True, "mission_id": mission_id}


@app.post("/api/missions/{mission_id}/pause")
async def pause_mission(mission_id: str) -> dict[str, Any]:
    return await _interrupt_mission(mission_id, "paused")


@app.post("/api/missions/{mission_id}/resume")
async def resume_mission(mission_id: str) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    if mission.get("status") != "paused":
        return {"ok": False, "error": "only paused missions can be resumed"}
    resume_count = int(mission.get("resume_count") or 0) + 1
    await missions.update(
        mission_id,
        status="queued",
        stage="queued",
        error=None,
        resume_count=resume_count,
        resumed_at=int(time.time()),
    )
    await evidence.append(
        mission_id,
        "mission.resumed",
        {"resume_count": resume_count},
        actor="human-operator",
    )
    await runtime.broadcast(
        "mission.resumed",
        {"id": mission_id, "resume_count": resume_count},
    )
    if not _schedule_mission(mission_id):
        return {"ok": False, "error": "mission already running"}
    return {"ok": True, "mission_id": mission_id, "resume_count": resume_count}


@app.post("/api/missions/{mission_id}/cancel")
async def cancel_mission(mission_id: str) -> dict[str, Any]:
    return await _interrupt_mission(mission_id, "cancelled")


@app.post("/api/missions/{mission_id}/approval")
async def mission_approval(mission_id: str, req: ApprovalRequest) -> dict[str, Any]:
    mission = await missions.get(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="mission not found")
    risk = mission.get("risk") or policy.assess(
        mission.get("prompt", ""), mission.get("priority", "normal")
    )
    if not risk.get("approval_required"):
        return {"ok": False, "error": "mission does not require approval"}

    now = int(time.time())
    if req.decision == "reject":
        updated = await missions.update(
            mission_id,
            status="rejected",
            stage="rejected",
            approval_status="rejected",
            approval_note=req.note,
            rejected_at=now,
        )
        await evidence.append(
            mission_id,
            "mission.rejected",
            {"note": req.note, "risk": risk},
            actor="human-operator",
        )
        await runtime.broadcast("mission.rejected", {"id": mission_id, "note": req.note})
        return {"ok": True, "mission": updated}

    updated = await missions.update(
        mission_id,
        status="approved",
        stage="approved",
        approval_status="approved",
        approval_note=req.note,
        approved_at=now,
    )
    await evidence.append(
        mission_id,
        "mission.approved",
        {"note": req.note, "risk": risk},
        actor="human-operator",
    )
    await runtime.broadcast("mission.approved", {"id": mission_id, "note": req.note})
    _schedule_mission(mission_id)
    return {"ok": True, "mission": updated}


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
    await runtime.broadcast(
        "memory.updated",
        {"key": key.upper(), "size": len(req.content.encode("utf-8"))},
    )
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
