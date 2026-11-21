import pytest

import memobird_mcp


def test_mcp_exposes_printing_status_and_settings_tools():
    assert [tool.__name__ for tool in memobird_mcp.MCP_TOOLS] == [
        "list_system_serial_ports",
        "get_device_status",
        "get_device_details",
        "set_auto_shutdown",
        "print_text",
        "print_image",
    ]


async def assert_legacy_handshake_rejected(endpoint):
    from mcp import Client
    from mcp.shared.exceptions import MCPError
    from mcp.types import LATEST_PROTOCOL_VERSION, UNSUPPORTED_PROTOCOL_VERSION

    try:
        async with Client(endpoint, mode="legacy", read_timeout_seconds=10):
            pytest.fail("Legacy handshake unexpectedly succeeded")
    except BaseException as exc:
        pending = [exc]
        protocol_errors = []
        while pending:
            error = pending.pop()
            if isinstance(error, MCPError):
                protocol_errors.append(error)
            else:
                pending.extend(getattr(error, "exceptions", ()))
        assert protocol_errors, repr(exc)
        rejection = protocol_errors[0]
    else:
        pytest.fail("Legacy handshake unexpectedly succeeded")

    assert rejection.code == UNSUPPORTED_PROTOCOL_VERSION
    assert rejection.data["supported"] == [LATEST_PROTOCOL_VERSION]
    assert rejection.data["requested"] == "2025-11-25"


def test_mcp_v2_stdio_client_lists_tools_and_calls_inventory():
    import os
    import sys
    from pathlib import Path

    import anyio
    from mcp import Client, StdioServerParameters
    from mcp.types import LATEST_PROTOCOL_VERSION

    async def exercise_server():
        root = Path(__file__).resolve().parent
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("MEMOBIRD_"):
                env.pop(key)
        env["PYTHONUNBUFFERED"] = "1"
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(root / "memobird_mcp.py")],
            env=env,
            cwd=str(root),
        )
        async with Client(server, mode="auto", read_timeout_seconds=10) as client:
            assert client.protocol_version == LATEST_PROTOCOL_VERSION
            result = await client.list_tools()
            tools = {tool.name: tool for tool in result.tools}
            assert set(tools) == {
                "list_system_serial_ports",
                "get_device_status",
                "get_device_details",
                "set_auto_shutdown",
                "print_text",
                "print_image",
            }
            assert all(tool.output_schema is not None for tool in tools.values())
            assert tools["get_device_details"].annotations.read_only_hint is True
            assert tools["print_text"].annotations.destructive_hint is True
            ports = await client.call_tool("list_system_serial_ports", {})
            assert ports.is_error is False
            assert ports.structured_content["ok"] is True
            details = await client.call_tool("get_device_details", {})
            assert details.is_error is False
            assert details.structured_content["ok"] is False
            setting = await client.call_tool("set_auto_shutdown", {"minutes": 30})
            assert setting.is_error is False
            assert setting.structured_content["ok"] is False

        await assert_legacy_handshake_rejected(server)

    anyio.run(exercise_server)


def test_mcp_v2_streamable_http_client_on_loopback():
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import anyio
    import httpx
    from mcp import Client
    from mcp.types import LATEST_PROTOCOL_VERSION, UNSUPPORTED_PROTOCOL_VERSION

    root = Path(__file__).resolve().parent
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("MEMOBIRD_"):
            env.pop(key)
    process = subprocess.Popen(
        [sys.executable, str(root / "memobird_mcp.py"), "--transport", "streamable-http", "--port", str(port)],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError(f"HTTP MCP server exited with status {process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("HTTP MCP server did not become ready")
                time.sleep(0.05)

        async def exercise_server():
            endpoint = f"http://127.0.0.1:{port}/mcp"
            async with Client(endpoint, mode="auto", read_timeout_seconds=10) as client:
                assert client.protocol_version == LATEST_PROTOCOL_VERSION
                tools = await client.list_tools()
                assert len(tools.tools) == 6
                ports = await client.call_tool("list_system_serial_ports", {})
                assert ports.is_error is False
                assert ports.structured_content["ok"] is True
                details = await client.call_tool("get_device_details", {})
                assert details.is_error is False
                assert details.structured_content["ok"] is False

            await assert_legacy_handshake_rejected(endpoint)

            # A stateless legacy client can try a tool call without handshaking.
            async with httpx.AsyncClient(timeout=10) as http:
                for version_headers in ({}, {"MCP-Protocol-Version": "2025-11-25"}):
                    response = await http.post(
                        endpoint,
                        headers={"Accept": "application/json, text/event-stream", **version_headers},
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "list_system_serial_ports", "arguments": {}},
                        },
                    )
                    response.raise_for_status()
                    reply = response.json()
                    assert "result" not in reply
                    assert reply["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
                    assert reply["error"]["data"]["supported"] == [LATEST_PROTOCOL_VERSION]

        anyio.run(exercise_server)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class FakePrinter:
    def __init__(self, acknowledged=True):
        self.acknowledged = acknowledged
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get_status(self):
        return {
            "connected": True,
            "mac": "AA:BB:CC:DD:EE:FF",
            "serial_port": None,
            "sn": "TEST-SERIAL",
            "battery_raw": 78,
            "battery_percent": 70,
            "auto_shutdown_minutes": 255,
            "raw_fields": {"0x13": "ff"},
        }

    def set_auto_shutdown(self, minutes):
        return {
            "acknowledged": True,
            "verified": minutes == 30,
            "requested_minutes": minutes,
            "observed_minutes": 30,
        }

    def print_text(self, content, **options):
        self.calls.append(("text", content, options))
        return self.acknowledged

    def print_image(self, image, **options):
        self.calls.append(("image", image, options))
        return self.acknowledged


def test_status_and_shutdown_tools_remain_available(monkeypatch):
    printer = FakePrinter()
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: printer)

    details = memobird_mcp.get_device_details()
    status = memobird_mcp.get_device_status()
    setting = memobird_mcp.set_auto_shutdown(30)

    assert details["battery_percent"] == 70
    assert details["auto_shutdown_minutes"] == 255
    assert "电量: 70%" in status
    assert "255 分钟" in status
    assert setting["ok"] is True
    assert setting["acknowledged"] is True
    assert setting["observed_minutes"] == 30


def test_get_device_details_reports_transport_failure(monkeypatch):
    def fail(*args):
        raise ConnectionError("Bluetooth endpoint unavailable")

    monkeypatch.setattr(memobird_mcp, "_configured_printer", fail)

    assert memobird_mcp.get_device_details()["error"] == "Bluetooth endpoint unavailable"
    assert "Bluetooth endpoint unavailable" in memobird_mcp.get_device_status()


def test_set_auto_shutdown_keeps_ack_distinct_from_readback(monkeypatch):
    printer = FakePrinter()
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: printer)

    result = memobird_mcp.set_auto_shutdown(0)

    assert result["acknowledged"] is True
    assert result["verified"] is False
    assert result["ok"] is False
    assert result["observed_minutes"] == 30


def test_list_system_serial_ports_reports_enumeration_errors(monkeypatch):
    import memobird_transport

    def fail():
        raise RuntimeError("pyserial missing")

    monkeypatch.setattr(memobird_transport, "list_serial_ports", fail)

    assert memobird_mcp.list_system_serial_ports() == {
        "ok": False,
        "error": "pyserial missing",
    }


def test_get_printer_requires_a_bluetooth_or_serial_endpoint(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)
    monkeypatch.delenv("MEMOBIRD_TRANSPORT", raising=False)

    import pytest
    with pytest.raises(ValueError, match="provide mac"):
        memobird_mcp.get_printer()


def test_get_printer_rejects_invalid_mac(monkeypatch):
    import pytest
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)

    with pytest.raises(ValueError, match="colon-separated"):
        memobird_mcp.get_printer("not-a-mac")


def test_print_text_reports_device_ack_without_claiming_paper_output(monkeypatch):
    printer = FakePrinter()
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: printer)

    result = memobird_mcp.print_text("hello", bold=True)

    assert "设备已响应 ACK" in result
    assert "无法确认纸张已实际输出" in result
    assert printer.calls == [("text", "hello", {"bold": True, "underline": False})]


def test_print_text_reports_missing_ack(monkeypatch):
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: FakePrinter(False))

    assert "未收到设备 ACK" in memobird_mcp.print_text("hello")


def test_print_image_loads_local_data_before_connecting(monkeypatch, tmp_path):
    image_path = tmp_path / "image.bin"
    image_path.write_bytes(b"image-data")
    printer = FakePrinter()
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: printer)

    result = memobird_mcp.print_image(str(image_path), dither=False)

    assert "设备已响应 ACK" in result
    assert printer.calls == [("image", b"image-data", {"dither": False})]


def test_print_image_rejects_inputs_over_limit_without_connecting(monkeypatch, tmp_path):
    image_path = tmp_path / "oversized.img"
    image_path.write_bytes(b"x" * (memobird_mcp.MAX_IMAGE_BYTES + 1))
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: (_ for _ in ()).throw(AssertionError("must not connect")))

    result = memobird_mcp.print_image(str(image_path))

    assert "exceeds 10 MiB" in result


def test_list_system_serial_ports_returns_sorted_endpoint_metadata(monkeypatch):
    import memobird_transport

    monkeypatch.setattr(memobird_transport, "list_serial_ports", lambda: [
        {"device": "COM3", "description": "Printer"},
        {"device": "COM7", "description": "Bluetooth SPP"},
    ])

    assert memobird_mcp.list_system_serial_ports() == {
        "ok": True,
        "ports": [
            {"device": "COM3", "description": "Printer"},
            {"device": "COM7", "description": "Bluetooth SPP"},
        ],
    }


def test_get_printer_uses_configured_system_port_and_connection_settings(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.setenv("MEMOBIRD_SERIAL_PORT", "/dev/cu.MEMOBIRD-SPP")
    monkeypatch.setenv("MEMOBIRD_TRANSPORT", "serial")
    monkeypatch.setenv("MEMOBIRD_BAUDRATE", "9600")
    monkeypatch.setenv("MEMOBIRD_TIMEOUT", "4.5")

    printer = memobird_mcp.get_printer()

    assert printer.mac is None
    assert printer.serial_port == "/dev/cu.MEMOBIRD-SPP"
    assert printer.transport == "serial"
    assert printer.baudrate == 9600
    assert printer.timeout == 4.5


def test_get_printer_selects_explicit_mac_or_system_serial_port(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)
    monkeypatch.delenv("MEMOBIRD_TRANSPORT", raising=False)
    bluetooth_printer = memobird_mcp.get_printer("aa:bb:cc:dd:ee:ff")
    assert bluetooth_printer.mac == "AA:BB:CC:DD:EE:FF"
    assert bluetooth_printer.transport == "rfcomm"

    serial_printer = memobird_mcp.get_printer(serial_port="COM7")
    assert serial_printer.mac is None
    assert serial_printer.serial_port == "COM7"
    assert serial_printer.transport == "serial"


def test_explicit_mac_does_not_use_ambient_serial_port(monkeypatch):
    monkeypatch.setenv("MEMOBIRD_SERIAL_PORT", "COM9")

    printer = memobird_mcp.get_printer("AA:BB:CC:DD:EE:FF")

    assert printer.transport == "rfcomm"
    assert printer.serial_port is None


def test_http_cli_refuses_public_bind_addresses():
    import pytest

    with pytest.raises(SystemExit) as exit_info:
        memobird_mcp.main(["--transport", "streamable-http", "--host", "0.0.0.0"])

    assert exit_info.value.code == 2
