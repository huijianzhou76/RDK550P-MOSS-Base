from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app import app, runtime
from mcu_service import McuService

ROOT = Path(__file__).resolve().parents[1]
MODE = os.getenv("MOSS_MODE", "mock").strip().lower()
mcu = McuService(ROOT, MODE)


class HeadMoveRequest(BaseModel):
    yaw_deg: float = Field(ge=-90, le=90)
    pitch_deg: float = Field(ge=-90, le=90)
    speed: float = Field(default=0.5, ge=0.05, le=1.0)


class EstopClearRequest(BaseModel):
    operator_confirmed: bool


class LightRequest(BaseModel):
    brightness: float = Field(default=0.6, ge=0.0, le=1.0)


class DisplayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(default=3000, ge=100, le=60_000)


class IrLearnRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_.-]+$")
    timeout_ms: int = Field(default=4000, ge=500, le=4500)


class IrSendRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_.-]+$")
    repeat: int = Field(default=1, ge=1, le=5)


async def _hardware_event(name: str, result: dict[str, Any]) -> dict[str, Any]:
    await runtime.set_state(hardware="ready" if result.get("ok") else "error")
    await runtime.broadcast(name, result)
    return result


@app.get("/api/hardware/mcu")
async def mcu_info() -> dict[str, Any]:
    return {"ok": True, "mcu": mcu.info()}


@app.post("/api/hardware/mcu/ping")
async def mcu_ping() -> dict[str, Any]:
    await runtime.set_state(hardware="busy")
    return await _hardware_event("hardware.mcu.ping", await mcu.ping())


@app.get("/api/hardware/mcu/status")
async def mcu_status() -> dict[str, Any]:
    result = await mcu.status()
    return await _hardware_event("hardware.mcu.status", result)


@app.get("/api/hardware/mcu/capabilities")
async def mcu_capabilities() -> dict[str, Any]:
    result = await mcu.capabilities()
    return await _hardware_event("hardware.mcu.capabilities", result)


@app.post("/api/hardware/mcu/head/move")
async def mcu_head_move(req: HeadMoveRequest) -> dict[str, Any]:
    await runtime.set_state(hardware="busy")
    result = await mcu.move_head(req.yaw_deg, req.pitch_deg, req.speed)
    return await _hardware_event("hardware.mcu.head_moved", result)


@app.post("/api/hardware/mcu/head/center")
async def mcu_head_center() -> dict[str, Any]:
    await runtime.set_state(hardware="busy")
    result = await mcu.center_head()
    return await _hardware_event("hardware.mcu.head_centered", result)


@app.post("/api/hardware/mcu/estop")
async def mcu_estop() -> dict[str, Any]:
    # E-STOP bypasses normal mission scheduling and is sent immediately.
    result = await mcu.emergency_stop()
    await runtime.set_state(hardware="estop" if result.get("ok") else "error")
    await runtime.broadcast("hardware.mcu.estop", result)
    return result


@app.post("/api/hardware/mcu/estop/clear")
async def mcu_estop_clear(req: EstopClearRequest) -> dict[str, Any]:
    if not req.operator_confirmed:
        return {"ok": False, "error": "operator confirmation required"}
    result = await mcu.clear_emergency_stop(True)
    return await _hardware_event("hardware.mcu.estop_cleared", result)


@app.post("/api/hardware/mcu/light")
async def mcu_light(req: LightRequest) -> dict[str, Any]:
    result = await mcu.set_light(req.brightness)
    return await _hardware_event("hardware.mcu.light", result)


@app.post("/api/hardware/mcu/display")
async def mcu_display(req: DisplayRequest) -> dict[str, Any]:
    result = await mcu.display_text(req.text, req.duration_ms)
    return await _hardware_event("hardware.mcu.display", result)


@app.get("/api/hardware/mcu/sensors")
async def mcu_sensors() -> dict[str, Any]:
    result = await mcu.read_sensors()
    return await _hardware_event("hardware.mcu.sensors", result)


@app.get("/api/hardware/mcu/ir")
async def mcu_ir_slots() -> dict[str, Any]:
    result = await mcu.ir_list()
    return await _hardware_event("hardware.mcu.ir.slots", result)


@app.post("/api/hardware/mcu/ir/learn")
async def mcu_ir_learn(req: IrLearnRequest) -> dict[str, Any]:
    await runtime.set_state(hardware="busy")
    result = await mcu.ir_learn(req.slot, req.timeout_ms)
    return await _hardware_event("hardware.mcu.ir.learned", result)


@app.post("/api/hardware/mcu/ir/send")
async def mcu_ir_send(req: IrSendRequest) -> dict[str, Any]:
    result = await mcu.ir_send(req.slot, req.repeat)
    return await _hardware_event("hardware.mcu.ir.sent", result)


@app.on_event("shutdown")
async def shutdown_mcu_gateway() -> None:
    await mcu.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app_mcu:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5500")),
        reload=False,
    )
