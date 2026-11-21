#!/usr/bin/env python3
"""MCP server for GT1 status, battery, shutdown settings, and printing."""

import os
import sys
import urllib.request
import threading
from typing import Any, Optional

MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 引入同级驱动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memobird_driver import MemobirdGT1

try:
    from mcp.server import MCPServer
    from mcp.shared.exceptions import MCPError
    from mcp.types import LATEST_PROTOCOL_VERSION, UNSUPPORTED_PROTOCOL_VERSION
except ImportError as exc:
    MCPServer = None
    MCPError = None
    LATEST_PROTOCOL_VERSION = None
    UNSUPPORTED_PROTOCOL_VERSION = None
    _SDK_IMPORT_ERROR = str(exc)
else:
    _SDK_IMPORT_ERROR = None


class _LatestProtocolOnly:
    async def __call__(self, ctx, call_next):
        if ctx.method != "initialize" and ctx.protocol_version == LATEST_PROTOCOL_VERSION:
            return await call_next(ctx)

        # Stateless HTTP can dispatch legacy requests without an initialize call.
        if ctx.request_id is None:
            return None
        requested = (ctx.params or {}).get("protocolVersion") if ctx.method == "initialize" else ctx.protocol_version
        raise MCPError(
            code=UNSUPPORTED_PROTOCOL_VERSION,
            message=f"This server requires MCP protocol {LATEST_PROTOCOL_VERSION}; legacy protocol modes are not supported.",
            data={"supported": [LATEST_PROTOCOL_VERSION], "requested": requested},
        )


SERVER_VERSION = "0.3.0"
mcp = MCPServer(
    "MemobirdGT1",
    version=SERVER_VERSION,
    instructions=(
        f"Control a locally paired Memobird GT1 over Bluetooth SPP using MCP {LATEST_PROTOCOL_VERSION}. "
        "Read status before changing settings. Printing consumes paper; an ACK "
        "does not prove physical output. Do not automatically retry print jobs."
    ),
    middleware=[_LatestProtocolOnly()] if MCPServer else None,
) if MCPServer else None
_operation_lock = threading.Lock()


def get_printer(mac: Optional[str] = None, serial_port: Optional[str] = None,
                transport: Optional[str] = None):
    # An explicit MAC must not accidentally print to an unrelated env port.
    target_serial = serial_port if serial_port is not None else (
        None if mac is not None else os.environ.get("MEMOBIRD_SERIAL_PORT"))
    return MemobirdGT1(
        mac=mac if mac is not None else os.environ.get("MEMOBIRD_MAC"),
        serial_port=target_serial,
        transport=transport if transport is not None else os.environ.get("MEMOBIRD_TRANSPORT", "auto"),
        port=int(os.environ.get("MEMOBIRD_RFCOMM_CHANNEL", "1")),
        baudrate=int(os.environ.get("MEMOBIRD_BAUDRATE", "115200")),
        timeout=float(os.environ.get("MEMOBIRD_TIMEOUT", "10")),
    )


def _configured_printer(mac, serial_port, transport):
    if serial_port is None and transport is None:
        return get_printer(mac)
    return get_printer(mac, serial_port=serial_port, transport=transport)


def list_system_serial_ports() -> dict[str, Any]:
    """List OS serial endpoints without opening or probing any device."""
    from memobird_transport import list_serial_ports
    try:
        return {"ok": True, "ports": list_serial_ports()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_device_details(mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> dict[str, Any]:
    """读取设备结构化状态；未知字段为 null，原始 TLV 保留供诊断。"""
    try:
        with _operation_lock, _configured_printer(mac, serial_port, transport) as printer:
            return {"ok": True, **printer.get_status()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "connected": False}


def get_device_status(mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> str:
    """获取咕咕机GT1连接状态、TLV序列号和自动关机分钟值。"""
    try:
        details = get_device_details(mac, serial_port, transport)
        if not details["ok"]:
            return f"获取设备状态失败: {details['error']}"
        status = details
        minutes = status.get("auto_shutdown_minutes")
        shutdown = "未知" if minutes is None else ("永不关机" if minutes == 0 else f"{minutes} 分钟")
        battery = status.get("battery_percent")
        battery_text = "未知" if battery is None else f"{battery}%"
        return (f"咕咕机 GT1 连接正常！序列号: {status.get('sn') or '未知'}, "
                f"电量: {battery_text}, 连接: {status.get('serial_port') or status.get('mac')}, 自动关机: {shutdown}")
    except Exception as e:
        return f"获取设备状态失败: {str(e)}"


def set_auto_shutdown(minutes: int, mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> dict[str, Any]:
    """Set the one-byte shutdown value and report the device readback."""
    try:
        with _operation_lock, _configured_printer(mac, serial_port, transport) as printer:
            result = printer.set_auto_shutdown(minutes)
        return {"ok": result["verified"], **result}
    except Exception as exc:
        return {"ok": False, "requested_minutes": minutes, "error": str(exc)}


def print_text(content: str, bold: bool = False, underline: bool = False, mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> str:
    """发送文本打印任务；ACK 仅表示设备响应，不确认纸张已实际输出。"""
    try:
        with _operation_lock, _configured_printer(mac, serial_port, transport) as printer:
            acknowledged = printer.print_text(content, bold=bold, underline=underline)
        if acknowledged:
            return "文本打印数据已发送，设备已响应 ACK；无法确认纸张已实际输出。"
        return "文本打印数据已发送，但未收到设备 ACK；无法确认纸张已实际输出。"
    except Exception as e:
        return f"打印文本失败: {str(e)}"


def print_image(image_path_or_url: str, dither: bool = True, mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> str:
    """发送图片打印任务；ACK 仅表示设备响应，不确认纸张已实际输出。"""
    try:
        if image_path_or_url.startswith("http://") or image_path_or_url.startswith("https://"):
            req = urllib.request.Request(image_path_or_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                img_data = resp.read(MAX_IMAGE_BYTES + 1)
        else:
            with open(image_path_or_url, "rb") as f:
                img_data = f.read(MAX_IMAGE_BYTES + 1)
        if len(img_data) > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds 10 MiB input limit")

        with _operation_lock, _configured_printer(mac, serial_port, transport) as printer:
            acknowledged = printer.print_image(img_data, dither=dither)
        if acknowledged:
            return "图片打印数据已发送，设备已响应 ACK；无法确认纸张已实际输出。"
        return "图片打印数据已发送，但未收到设备 ACK；无法确认纸张已实际输出。"
    except Exception as e:
        return f"打印图片失败: {str(e)}"


MCP_TOOLS = (
    list_system_serial_ports,
    get_device_status,
    get_device_details,
    set_auto_shutdown,
    print_text,
    print_image,
)
PRINT_TOOLS = MCP_TOOLS

if mcp:
    from mcp.types import ToolAnnotations

    for tool in MCP_TOOLS:
        read_only = tool in (list_system_serial_ports, get_device_status, get_device_details)
        printing = tool in (print_text, print_image)
        mcp.tool(annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=printing,
            idempotent_hint=not printing,
            open_world_hint=tool is not list_system_serial_ports,
        ))(tool)


def main(argv=None) -> int:
    """Run a local stdio server or an explicitly selected loopback HTTP server."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {SERVER_VERSION}")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio",
        help="MCP transport (default: stdio; unrelated to Bluetooth transport)",
    )
    parser.add_argument(
        "--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1",
        help="loopback HTTP bind address; unauthenticated public binding is not supported",
    )
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if mcp is None:
        print(
            "MCP SDK 2.3+ is unavailable. Install requirements.txt with Python 3.10+. "
            f"Import error: {_SDK_IMPORT_ERROR}", file=sys.stderr,
        )
        return 1
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="streamable-http", host=args.host, port=args.port,
            json_response=True, stateless_http=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
