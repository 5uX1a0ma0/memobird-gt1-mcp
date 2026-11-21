#!/usr/bin/env python3
"""
Memobird GT1 MCP Server
为咕咕机 GT1 热敏打印机提供标准 MCP 协议工具支持。
"""

import os
import sys
import urllib.request
import threading
from typing import Optional

MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 引入同级驱动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memobird_driver import MemobirdGT1

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

mcp = FastMCP("MemobirdGT1") if FastMCP else None
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


def list_system_serial_ports() -> dict:
    """List OS serial endpoints without opening or probing any device."""
    from memobird_transport import list_serial_ports
    try:
        return {"ok": True, "ports": list_serial_ports()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_device_details(mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> dict:
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


def set_auto_shutdown(minutes: int, mac: Optional[str] = None, serial_port: Optional[str] = None, transport: Optional[str] = None) -> dict:
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


if mcp:
    mcp.tool()(list_system_serial_ports)
    mcp.tool()(get_device_status)
    mcp.tool()(get_device_details)
    mcp.tool()(set_auto_shutdown)
    mcp.tool()(print_text)
    mcp.tool()(print_image)

if __name__ == "__main__":
    if mcp:
        mcp.run()
    else:
        print("FastMCP is unavailable. Install this project's requirements, then run memobird_mcp.py.", file=sys.stderr)
        sys.exit(1)
