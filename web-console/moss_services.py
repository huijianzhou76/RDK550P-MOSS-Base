from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


ALLOWED_MEMORY_FILES = {
    "SOUL": "SOUL.md.template",
    "IDENTITY": "IDENTITY.md.template",
    "MEMORY": "MEMORY.md.template",
    "USER": "USER.md.template",
    "TOOLS": "TOOLS.md.template",
    "HEARTBEAT": "HEARTBEAT.md.template",
    "AGENTS": "AGENTS.md.template",
}


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
    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


class OpenClawService:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def chat(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        session_id = session_id or f"web-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        if self.mode == "mock":
            await asyncio.sleep(0.35)
            return {
                "ok": True,
                "session_id": session_id,
                "reply": (
                    "[MOCK MOSS] 当前处于模拟模式。已接收任务："
                    f"{message}\n切换 MOSS_MODE=rdk 后将调用真实 OpenClaw Agent。"
                ),
            }

        code, stdout, stderr = await run_process(
            [
                "openclaw",
                "agent",
                "--session-id",
                session_id,
                "-m",
                message,
                "--timeout",
                "600",
                "--thinking",
                "off",
            ],
            timeout=610,
        )
        if code != 0:
            return {
                "ok": False,
                "session_id": session_id,
                "error": (stderr or stdout or f"exit code {code}")[-3000:],
            }
        return {"ok": True, "session_id": session_id, "reply": stdout.strip()}


class MissionStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "missions.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            return self._read()

    async def create(self, title: str, prompt: str, priority: str = "normal") -> dict[str, Any]:
        async with self._lock:
            rows = self._read()
            mission = {
                "id": uuid.uuid4().hex[:12],
                "title": title.strip() or prompt.strip()[:40] or "Untitled mission",
                "prompt": prompt.strip(),
                "priority": priority if priority in {"low", "normal", "high", "critical"} else "normal",
                "status": "queued",
                "progress": 0,
                "session_id": None,
                "result": None,
                "error": None,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
            rows.insert(0, mission)
            self._write(rows[:200])
            return mission

    async def update(self, mission_id: str, **updates: Any) -> dict[str, Any] | None:
        async with self._lock:
            rows = self._read()
            found = None
            for row in rows:
                if row.get("id") == mission_id:
                    row.update(updates)
                    row["updated_at"] = int(time.time())
                    found = row
                    break
            if found is not None:
                self._write(rows)
            return found

    async def get(self, mission_id: str) -> dict[str, Any] | None:
        rows = await self.list()
        return next((x for x in rows if x.get("id") == mission_id), None)


class MemoryService:
    def __init__(self, template_dir: Path) -> None:
        self.template_dir = template_dir

    def list(self) -> list[dict[str, Any]]:
        result = []
        for key, filename in ALLOWED_MEMORY_FILES.items():
            path = self.template_dir / filename
            result.append(
                {
                    "key": key,
                    "filename": filename,
                    "exists": path.exists(),
                    "size": path.stat().st_size if path.exists() else 0,
                }
            )
        return result

    def read(self, key: str) -> dict[str, Any]:
        key = key.upper()
        filename = ALLOWED_MEMORY_FILES.get(key)
        if not filename:
            raise KeyError(key)
        path = self.template_dir / filename
        return {
            "key": key,
            "filename": filename,
            "content": path.read_text(encoding="utf-8") if path.exists() else "",
        }

    def write(self, key: str, content: str) -> dict[str, Any]:
        key = key.upper()
        filename = ALLOWED_MEMORY_FILES.get(key)
        if not filename:
            raise KeyError(key)
        if len(content.encode("utf-8")) > 256_000:
            raise ValueError("memory document too large")
        path = self.template_dir / filename
        path.write_text(content, encoding="utf-8")
        return self.read(key)


class HardwareService:
    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.scripts = root / "scripts"
        self.media = root / "media"
        self.media.mkdir(exist_ok=True)

    async def snapshot(self) -> dict[str, Any]:
        output = self.media / "web-snapshot.jpg"
        if self.mode == "mock":
            await asyncio.sleep(0.2)
            return {"ok": True, "mock": True, "url": None}
        code, stdout, stderr = await run_process(
            ["python3", str(self.scripts / "snap.py"), str(output)], timeout=20
        )
        if code != 0:
            return {"ok": False, "error": (stderr or stdout)[-2000:]}
        return {"ok": True, "path": str(output), "url": "/media/web-snapshot.jpg?t=" + str(int(time.time()))}

    async def servo(self, action: str) -> dict[str, Any]:
        scripts = {
            "dance": "dance.py",
            "idle": "idle_motion.py",
        }
        filename = scripts.get(action)
        if not filename:
            return {"ok": False, "error": f"unsupported action: {action}"}
        if self.mode == "mock":
            await asyncio.sleep(0.2)
            return {"ok": True, "mock": True, "action": action}
        code, stdout, stderr = await run_process(
            ["python3", str(self.scripts / filename)], timeout=180
        )
        if code != 0:
            return {"ok": False, "error": (stderr or stdout)[-2000:]}
        return {"ok": True, "action": action, "message": stdout.strip()[-2000:]}


class VoiceService:
    SERVICE_NAME = "voice-assistant.service"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.mock_active = True

    async def status(self) -> dict[str, Any]:
        if self.mode == "mock":
            return {"ok": True, "active": self.mock_active, "status": "active" if self.mock_active else "inactive", "mock": True}
        code, stdout, _ = await run_process(
            ["systemctl", "is-active", self.SERVICE_NAME], timeout=8
        )
        status = stdout.strip() or "unknown"
        return {"ok": code == 0, "active": status == "active", "status": status}

    async def control(self, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            return {"ok": False, "error": "unsupported voice service action"}
        if self.mode == "mock":
            if action == "start":
                self.mock_active = True
            elif action == "stop":
                self.mock_active = False
            else:
                self.mock_active = True
            await asyncio.sleep(0.15)
            return await self.status()
        code, stdout, stderr = await run_process(
            ["sudo", "systemctl", action, self.SERVICE_NAME], timeout=30
        )
        if code != 0:
            return {"ok": False, "error": (stderr or stdout)[-2000:]}
        await asyncio.sleep(0.3)
        return await self.status()
