import memobird_mcp


def test_mcp_functions_exist_without_fastmcp():
    assert callable(memobird_mcp.get_device_status)
    assert callable(memobird_mcp.get_device_details)
    assert callable(memobird_mcp.set_auto_shutdown)
    assert callable(memobird_mcp.print_text)
    assert callable(memobird_mcp.print_image)
    assert callable(memobird_mcp.list_system_serial_ports)


class FakePrinter:
    def __init__(self, status=None, setting=None):
        self.status = status or {"connected": True, "mac": "AA:BB:CC:DD:EE:FF", "sn": "123", "battery_raw": 78, "battery_percent": 70, "auto_shutdown_minutes": 255, "raw_fields": {"0x13": "ff"}}
        self.setting = setting or {"acknowledged": True, "verified": True, "requested_minutes": 30, "observed_minutes": 30}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get_status(self):
        return self.status

    def set_auto_shutdown(self, minutes):
        return {**self.setting, "requested_minutes": minutes}


def test_get_device_details_returns_structured_status(monkeypatch):
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: FakePrinter())
    result = memobird_mcp.get_device_details("AA:BB:CC:DD:EE:FF")
    assert result["ok"] is True
    assert result["auto_shutdown_minutes"] == 255
    assert result["raw_fields"]["0x13"] == "ff"


def test_get_device_status_formats_known_fields(monkeypatch):
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: FakePrinter())
    result = memobird_mcp.get_device_status()
    assert "123" in result
    assert "电量: 70%" in result
    assert "255 分钟" in result


def test_set_auto_shutdown_reports_readback_not_only_ack(monkeypatch):
    printer = FakePrinter(setting={"acknowledged": False, "verified": False, "requested_minutes": 0, "observed_minutes": 255})
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: printer)
    result = memobird_mcp.set_auto_shutdown(0)
    assert result["ok"] is False
    assert result["acknowledged"] is False
    assert result["observed_minutes"] == 255


def test_print_image_rejects_inputs_over_limit_without_connecting(monkeypatch, tmp_path):
    image_path = tmp_path / "oversized.img"
    image_path.write_bytes(b"x" * (memobird_mcp.MAX_IMAGE_BYTES + 1))
    monkeypatch.setattr(memobird_mcp, "_configured_printer", lambda *args: (_ for _ in ()).throw(AssertionError("must not connect")))
    result = memobird_mcp.print_image(str(image_path))
    assert "exceeds 10 MiB" in result


def test_get_printer_normalizes_mac(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)
    monkeypatch.delenv("MEMOBIRD_TRANSPORT", raising=False)
    printer = memobird_mcp.get_printer("aa:bb:cc:dd:ee:ff")
    assert printer.mac == "AA:BB:CC:DD:EE:FF"
    assert printer.transport == "rfcomm"


def test_get_printer_selects_configured_system_port(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.setenv("MEMOBIRD_SERIAL_PORT", "/dev/cu.MEMOBIRD-SPP")
    monkeypatch.delenv("MEMOBIRD_TRANSPORT", raising=False)
    printer = memobird_mcp.get_printer()
    assert printer.mac is None
    assert printer.serial_port == "/dev/cu.MEMOBIRD-SPP"
    assert printer.transport == "serial"


def test_explicit_mac_does_not_use_ambient_serial_port(monkeypatch):
    monkeypatch.setenv("MEMOBIRD_SERIAL_PORT", "COM9")
    printer = memobird_mcp.get_printer("AA:BB:CC:DD:EE:FF")
    assert printer.transport == "rfcomm"
    assert printer.serial_port is None


def test_list_system_ports_reports_endpoint_inventory(monkeypatch):
    import memobird_transport
    monkeypatch.setattr(memobird_transport, "list_serial_ports", lambda: [{"device": "COM7", "description": "Bluetooth SPP"}])
    assert memobird_mcp.list_system_serial_ports() == {
        "ok": True, "ports": [{"device": "COM7", "description": "Bluetooth SPP"}]
    }


def test_get_printer_rejects_invalid_mac(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)
    try:
        memobird_mcp.get_printer("not-a-mac")
    except ValueError as exc:
        assert "colon-separated" in str(exc)
    else:
        raise AssertionError("invalid MAC was accepted")


def test_get_printer_requires_explicit_endpoint(monkeypatch):
    monkeypatch.delenv("MEMOBIRD_MAC", raising=False)
    monkeypatch.delenv("MEMOBIRD_SERIAL_PORT", raising=False)
    monkeypatch.delenv("MEMOBIRD_TRANSPORT", raising=False)
    try:
        memobird_mcp.get_printer()
    except ValueError as exc:
        assert "provide mac" in str(exc)
    else:
        raise AssertionError("get_printer unexpectedly selected an endpoint")
