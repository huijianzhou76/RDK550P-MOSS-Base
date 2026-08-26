from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


class McuService:
    """FastAPI-facing adapter for the RDK↔MCU hardware gateway."""

    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.port = os.getenv("MOSS_MCU_PORT", "/dev/ttyACM0")
        self.baud = int(os.getenv("MOSS_MCU_BAUD", "115200"))
        self.enabled = os.getenv("MOSS_HARDWARE_BACKEND", "mock" if mode == "mock" else "direct-rdk").lower() == "mcu"
        self._gateway = None
        self._error: str | None = None

        package_root = root / "moss-hardware" / "python"
        if str(package_root) not in sys.path:
            sys.path.insert(0, str(package_root))
        try:
            from moss_hardware import McuGateway  # type: ignore
            self._gateway_class = McuGateway
        except Exception as exc:  # pyserial/package may not be installed on dev host
            self._gateway_class = None
            self._error = str(exc)

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": "mcu" if self.enabled else "disabled",
            "port": self.port,
            "baud": self.baud,
            "gateway_available": self._gateway_class is not None,
            "connected": bool(self._gateway and self._gateway.connected),
            "error": self._error,
        }

    def _get_gateway(self):
        if not self.enabled:
            raise RuntimeError("MCU backend is disabled; set MOSS_HARDWARE_BACKEND=mcu")
        if self._gateway_class is None:
            raise RuntimeError(self._error or "moss_hardware gateway is unavailable")
        if self._gateway is None:
            self._gateway = self._gateway_class(self.port, self.baud, heartbeat_interval=2.0)
        return self._gateway

    async def _call(self, method: str, *args, **kwargs) -> dict[str, Any]:
        try:
            gateway = self._get_gateway()
            fn = getattr(gateway, method)
            response = await asyncio.to_thread(fn, *args, **kwargs)
            return {
                "ok": response.ok,
                "command_id": response.command_id,
                "data": response.data,
                "error": response.error,
                "message": response.message,
            }
        except Exception as exc:
            self._error = str(exc)
            return {"ok": False, "error": str(exc)}

    async def ping(self) -> dict[str, Any]:
        return await self._call("ping")

    async def status(self) -> dict[str, Any]:
        return await self._call("status")

    async def capabilities(self) -> dict[str, Any]:
        return await self._call("capabilities")

    async def move_head(self, yaw_deg: float, pitch_deg: float, speed: float) -> dict[str, Any]:
        return await self._call(
            "move_head",
            yaw_deg=yaw_deg,
            pitch_deg=pitch_deg,
            speed=speed,
        )

    async def center_head(self) -> dict[str, Any]:
        return await self._call("center_head")

    async def emergency_stop(self) -> dict[str, Any]:
        return await self._call("emergency_stop")

    async def clear_emergency_stop(self, operator_confirmed: bool) -> dict[str, Any]:
        return await self._call("clear_emergency_stop", operator_confirmed)

    async def set_light(self, brightness: float) -> dict[str, Any]:
        return await self._call("set_light", brightness=brightness)

    async def display_text(self, text: str, duration_ms: int) -> dict[str, Any]:
        return await self._call("display_text", text, duration_ms)

    async def read_sensors(self) -> dict[str, Any]:
        return await self._call("read_sensors")

    async def ir_learn(self, slot: str, timeout_ms: int) -> dict[str, Any]:
        return await self._call("ir_learn", slot, timeout_ms)

    async def ir_send(self, slot: str, repeat: int) -> dict[str, Any]:
        return await self._call("ir_send", slot, repeat)

    async def ir_list(self) -> dict[str, Any]:
        return await self._call("ir_list")

    async def close(self) -> None:
        gateway = self._gateway
        self._gateway = None
        if gateway is not None:
            await asyncio.to_thread(gateway.close)
