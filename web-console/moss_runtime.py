from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import WebSocket


class MossRuntime:
    """Shared MOSS runtime state and WebSocket event bus."""

    def __init__(self, mode: str) -> None:
        self.started_at = time.time()
        self.clients: set[WebSocket] = set()
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        self._lock = asyncio.Lock()
        self.state: dict[str, Any] = {
            "mode": mode,
            "agent": "standby",
            "camera": "ready",
            "voice": "unknown",
            "hardware": "ready",
            "mission": "idle",
            "last_event": "system.initialized",
            "updated_at": int(time.time()),
        }

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await ws.send_json({
            "event": "system.state",
            "payload": self.snapshot(),
            "ts": int(time.time() * 1000),
        })
        await ws.send_json({
            "event": "system.event_history",
            "payload": {"events": list(self.events)[-60:]},
            "ts": int(time.time() * 1000),
        })

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def snapshot(self) -> dict[str, Any]:
        data = dict(self.state)
        data["uptime_seconds"] = int(time.time() - self.started_at)
        data["websocket_clients"] = len(self.clients)
        return data

    async def set_state(self, **updates: Any) -> None:
        async with self._lock:
            self.state.update(updates)
            self.state["updated_at"] = int(time.time())
        await self.broadcast("system.state", self.snapshot(), record=False)

    async def broadcast(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        record: bool = True,
    ) -> None:
        message = {
            "event": event,
            "payload": payload or {},
            "ts": int(time.time() * 1000),
        }
        self.state["last_event"] = event
        self.state["updated_at"] = int(time.time())
        if record:
            self.events.append(message)

        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 300))
        return list(self.events)[-limit:]


def _read_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    path = Path("/proc/meminfo")
    if not path.exists():
        return result
    try:
        for line in path.read_text().splitlines():
            key, raw = line.split(":", 1)
            value = raw.strip().split()[0]
            result[key] = int(value) * 1024
    except (OSError, ValueError):
        return {}
    return result


def _temperature_c() -> float | None:
    candidates = [
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/thermal/thermal_zone1/temp"),
    ]
    for path in candidates:
        try:
            if path.exists():
                raw = float(path.read_text().strip())
                return round(raw / 1000.0 if raw > 200 else raw, 1)
        except (OSError, ValueError):
            continue
    return None


def system_metrics(root: Path) -> dict[str, Any]:
    mem = _read_meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    used = max(total - available, 0) if total else 0
    disk = shutil.disk_usage(root)
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0

    return {
        "cpu_count": os.cpu_count() or 1,
        "load": {
            "1m": round(load1, 2),
            "5m": round(load5, 2),
            "15m": round(load15, 2),
        },
        "memory": {
            "total": total,
            "used": used,
            "available": available,
            "percent": round((used / total) * 100, 1) if total else None,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": round((disk.used / disk.total) * 100, 1) if disk.total else None,
        },
        "temperature_c": _temperature_c(),
        "timestamp": int(time.time()),
    }
