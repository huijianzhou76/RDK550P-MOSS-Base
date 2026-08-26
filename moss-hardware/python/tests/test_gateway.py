import json
import queue
import sys
import threading
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

import moss_hardware.gateway as gateway_module
from moss_hardware.gateway import McuGateway


class FakeSerialPort:
    def __init__(self, *args, **kwargs):
        self.is_open = True
        self.read_queue = queue.Queue()
        self.last_command = None

    def write(self, payload):
        command = json.loads(payload.decode("utf-8").strip())
        self.last_command = command
        action = command["action"]
        data = {"action": action}
        if action == "head.move":
            data.update(command["params"])
        response = {
            "v": 1,
            "id": command["id"],
            "type": "response",
            "ok": True,
            "data": data,
        }
        self.read_queue.put((json.dumps(response) + "\n").encode("utf-8"))
        return len(payload)

    def flush(self):
        return None

    def readline(self):
        try:
            return self.read_queue.get(timeout=0.1)
        except queue.Empty:
            return b""

    def close(self):
        self.is_open = False


class FakeSerialModule:
    Serial = FakeSerialPort


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.old_serial = gateway_module.serial
        gateway_module.serial = FakeSerialModule()

    def tearDown(self):
        gateway_module.serial = self.old_serial

    def test_ping_roundtrip(self):
        client = McuGateway("FAKE", timeout=1)
        try:
            response = client.ping()
            self.assertTrue(response.ok)
            self.assertEqual(response.data["action"], "system.ping")
        finally:
            client.close()

    def test_move_head_serializes_high_level_command(self):
        client = McuGateway("FAKE", timeout=1)
        try:
            response = client.move_head(yaw_deg=12, pitch_deg=-4, speed=0.3)
            self.assertTrue(response.ok)
            self.assertEqual(response.data["action"], "head.move")
            self.assertEqual(response.data["yaw_deg"], 12.0)
            self.assertEqual(response.data["pitch_deg"], -4.0)
            self.assertEqual(response.data["speed"], 0.3)
        finally:
            client.close()

    def test_estop_clear_requires_explicit_flag_in_command(self):
        client = McuGateway("FAKE", timeout=1)
        try:
            response = client.clear_emergency_stop(True)
            self.assertTrue(response.ok)
            self.assertEqual(response.data["action"], "system.estop_clear")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
