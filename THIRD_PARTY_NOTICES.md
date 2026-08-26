# Third-Party Notices

This repository contains original upstream RDK550P-MOSS code plus new MOSS 550W extensions.

## RDK550P-MOSS

Upstream: `RDK550W/RDK550P-MOSS`

License: MIT. The upstream `LICENSE` file remains part of this repository.

## moss-xiaozhi

Repository: `yokochen222/moss-xiaozhi`

License: MIT License

Copyright (c) 2024 Xiaoxia

The project is used as a hardware architecture reference for ESP32 board profiles, OLED/LED integration, motor control and infrared device patterns. Hardware V1 code in `moss-hardware/` and `moss-mcu/` is independently structured for the RDK X5 + MCU architecture and does not currently copy source files verbatim from `moss-xiaozhi`.

If a future commit directly adapts a substantial source file from `moss-xiaozhi`, that file must retain the applicable copyright / MIT notice and the change must be recorded here.

## xiaozhi-esp32

Repository: `78/xiaozhi-esp32`

License: MIT License

Copyright (c) 2025 Shenzhen Xinzhi Future Technology Co., Ltd.
Copyright (c) 2025 Project Contributors

Referenced indirectly through the `moss-xiaozhi` hardware ecosystem. No source file from this repository is currently copied into the MOSS Hardware V1 implementation.

## moss-xiaozhi-mcp / mcp-calculator

Repositories reviewed:

- `yokochen222/moss-xiaozhi-mcp`
- `78/mcp-calculator`

At the time of review, no explicit repository-root license file was found. Their MCP/WebSocket/ONVIF/Home Assistant behavior may be studied at the interface/architecture level, but source code should not be copied into this repository unless licensing is clarified.
