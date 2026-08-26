from __future__ import annotations

import argparse
import json
import sys

from .gateway import McuGateway, McuGatewayError, McuResponse


def print_response(response: McuResponse) -> None:
    print(json.dumps({
        "ok": response.ok,
        "command_id": response.command_id,
        "data": response.data,
        "error": response.error,
        "message": response.message,
    }, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MOSS RDK↔MCU hardware bring-up tool")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyUSB0 or /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping")
    sub.add_parser("status")
    sub.add_parser("capabilities")
    sub.add_parser("sensors")
    sub.add_parser("center")
    sub.add_parser("estop")
    sub.add_parser("ir-list")

    clear = sub.add_parser("estop-clear")
    clear.add_argument("--confirm", action="store_true", required=True)

    move = sub.add_parser("move")
    move.add_argument("--yaw", type=float, required=True)
    move.add_argument("--pitch", type=float, required=True)
    move.add_argument("--speed", type=float, default=0.5)

    light = sub.add_parser("light")
    light.add_argument("--brightness", type=float, default=0.6)

    display = sub.add_parser("display")
    display.add_argument("text")

    learn = sub.add_parser("ir-learn")
    learn.add_argument("slot")
    learn.add_argument("--timeout-ms", type=int, default=4000)

    send = sub.add_parser("ir-send")
    send.add_argument("slot")
    send.add_argument("--repeat", type=int, default=1)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    gateway = McuGateway(args.port, args.baud)
    try:
        if args.command == "ping":
            result = gateway.ping()
        elif args.command == "status":
            result = gateway.status()
        elif args.command == "capabilities":
            result = gateway.capabilities()
        elif args.command == "sensors":
            result = gateway.read_sensors()
        elif args.command == "center":
            result = gateway.center_head()
        elif args.command == "estop":
            result = gateway.emergency_stop()
        elif args.command == "estop-clear":
            result = gateway.clear_emergency_stop(args.confirm)
        elif args.command == "move":
            result = gateway.move_head(yaw_deg=args.yaw, pitch_deg=args.pitch, speed=args.speed)
        elif args.command == "light":
            result = gateway.set_light(brightness=args.brightness)
        elif args.command == "display":
            result = gateway.display_text(args.text)
        elif args.command == "ir-list":
            result = gateway.ir_list()
        elif args.command == "ir-learn":
            result = gateway.ir_learn(args.slot, args.timeout_ms)
        elif args.command == "ir-send":
            result = gateway.ir_send(args.slot, args.repeat)
        else:  # pragma: no cover
            raise RuntimeError("unknown command")
        print_response(result)
        return 0 if result.ok else 2
    except McuGatewayError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    finally:
        gateway.close()


if __name__ == "__main__":
    raise SystemExit(main())
