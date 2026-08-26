from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - optional on mock/dev hosts
    serial = None


class McuGatewayError(RuntimeError):
    pass


@dataclass(slots=True)
class McuResponse:
    ok: bool
    command_id: str
    data: dict[str, Any]
    error: str | None = None
    message: str | None = None
    raw: dict[str, Any] | None = None


class McuGateway:
    """JSONL serial gateway between RDK X5 and the realtime MCU.

    Only high-level, bounded hardware actions are exposed. A dedicated heartbeat
    keeps the MCU watchdog informed that the RDK control plane is alive even
    when the robot is idle.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout: float = 3.0,
        heartbeat_interval: float = 2.0,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.heartbeat_interval = max(0.5, float(heartbeat_interval))
        self.event_callback = event_callback
        self._serial = None
        self._reader: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._capabilities_cache: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return bool(self._serial and getattr(self._serial, "is_open", False))

    def connect(self) -> None:
        if serial is None:
            raise McuGatewayError("pyserial is not installed")
        if self.connected:
            return
        self._serial = serial.Serial(
            self.port,
            self.baud,
            timeout=0.25,
            write_timeout=1.0,
        )
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, name="moss-mcu-reader", daemon=True)
        self._reader.start()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="moss-mcu-heartbeat", daemon=True)
        self._heartbeat.start()

    def close(self) -> None:
        self._stop.set()
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        for thread in (self._reader, self._heartbeat):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if not self.connected:
                continue
            try:
                self.command("system.heartbeat", timeout=min(1.5, self.timeout))
            except Exception:
                # Status is surfaced by explicit API calls; the heartbeat thread
                # must never terminate the process just because the cable is gone.
                continue

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            ser = self._serial
            if ser is None:
                return
            try:
                raw = ser.readline()
            except Exception:
                return
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8", errors="replace").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue

            if message.get("type") == "response" and message.get("id"):
                command_id = str(message["id"])
                with self._pending_lock:
                    waiter = self._pending.get(command_id)
                if waiter is not None:
                    try:
                        waiter.put_nowait(message)
                    except queue.Full:
                        pass
                continue

            if message.get("type") == "event" and self.event_callback:
                try:
                    self.event_callback(message)
                except Exception:
                    pass

    def command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> McuResponse:
        if not self.connected:
            self.connect()
        if not self.connected:
            raise McuGatewayError("MCU serial connection is not available")

        command_id = "cmd-" + uuid.uuid4().hex[:10]
        message = {
            "v": 1,
            "id": command_id,
            "type": "command",
            "action": action,
            "ts": int(time.time() * 1000),
            "params": params or {},
        }
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[command_id] = waiter
        try:
            payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            with self._write_lock:
                ser = self._serial
                if ser is None:
                    raise McuGatewayError("MCU serial connection closed")
                ser.write(payload)
                ser.flush()
            try:
                response = waiter.get(timeout=timeout or self.timeout)
            except queue.Empty as exc:
                raise McuGatewayError(f"MCU timeout for {action}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(command_id, None)

        return McuResponse(
            ok=bool(response.get("ok")),
            command_id=command_id,
            data=response.get("data") if isinstance(response.get("data"), dict) else {},
            error=response.get("error"),
            message=response.get("message"),
            raw=response,
        )

    def ping(self) -> McuResponse:
        return self.command("system.ping")

    def status(self) -> McuResponse:
        return self.command("system.status")

    def capabilities(self, refresh: bool = False) -> McuResponse:
        response = self.command("system.capabilities")
        if response.ok:
            self._capabilities_cache = response.data
        return response

    def emergency_stop(self) -> McuResponse:
        return self.command("system.estop", timeout=1.0)

    def clear_emergency_stop(self, operator_confirmed: bool) -> McuResponse:
        return self.command(
            "system.estop_clear",
            {"operator_confirmed": bool(operator_confirmed)},
        )

    def move_head(
        self,
        *,
        yaw_deg: float,
        pitch_deg: float,
        speed: float = 0.5,
    ) -> McuResponse:
        return self.command(
            "head.move",
            {
                "yaw_deg": float(yaw_deg),
                "pitch_deg": float(pitch_deg),
                "speed": float(speed),
            },
            timeout=3.0,
        )

    def center_head(self, speed: float = 0.45) -> McuResponse:
        return self.command("head.center", {"speed": float(speed)}, timeout=3.0)

    def set_light(
        self,
        *,
        target: str = "eye",
        mode: str = "solid",
        brightness: float = 0.6,
        rgb: tuple[int, int, int] = (220, 38, 38),
    ) -> McuResponse:
        return self.command(
            "light.set",
            {
                "target": target,
                "mode": mode,
                "brightness": float(brightness),
                "rgb": [int(x) for x in rgb],
            },
        )

    def display_text(self, text: str, duration_ms: int = 3000) -> McuResponse:
        return self.command(
            "display.text",
            {"text": str(text)[:128], "duration_ms": int(duration_ms)},
        )

    def read_sensors(self) -> McuResponse:
        return self.command("sensor.read")

    def ir_learn(self, slot: str, timeout_ms: int = 4000) -> McuResponse:
        return self.command(
            "ir.learn",
            {"slot": str(slot)[:16], "timeout_ms": max(500, min(int(timeout_ms), 4500))},
            timeout=max(2.0, min(int(timeout_ms), 4500) / 1000.0 + 1.0),
        )

    def ir_send(self, slot: str, repeat: int = 1) -> McuResponse:
        return self.command(
            "ir.send",
            {"slot": str(slot)[:16], "repeat": max(1, min(int(repeat), 5))},
            timeout=4.0,
        )

    def ir_list(self) -> McuResponse:
        return self.command("ir.list")
