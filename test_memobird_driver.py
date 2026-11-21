import pytest

from memobird_driver import MemobirdGT1, decode_electricity, le16, make_frame, parse_status_tlvs, split_gbk, tlv
import socket
from PIL import Image


def test_make_frame_has_expected_length_and_checksum():
    frame = make_frame(4, b"payload")

    assert frame[:4] == b"\xaa\x08\x00\x04"
    assert sum(frame) & 0xFF == 0


def test_split_gbk_preserves_multibyte_characters_at_chunk_boundary():
    text = "a" * 299 + "中" + "b" * 301

    chunks = split_gbk(text, 300)

    assert all(len(chunk) <= 300 for chunk in chunks)
    assert b"".join(chunks) == text.encode("gbk")
    assert all(chunk.decode("gbk") for chunk in chunks)


def test_split_gbk_empty_input_is_empty():
    assert split_gbk("", 300) == []


@pytest.mark.parametrize("value", [-1, 0x10000])
def test_le16_rejects_out_of_range_values(value):
    with pytest.raises(ValueError, match="16-bit protocol field"):
        le16(value)


def test_tlv_rejects_values_too_large_for_wire_length():
    with pytest.raises(ValueError, match="16-bit protocol field"):
        tlv(7, b"x" * 0x10000)


@pytest.mark.parametrize("raw,percent", [(71, 10), (72, 15), (73, 20), (74, 30), (75, 40), (76, 50), (77, 60), (78, 70), (79, 80), (80, 90), (81, 100)])
def test_decode_electricity_uses_official_generic_gt1_table(raw, percent):
    assert decode_electricity(raw) == percent


def test_unknown_electricity_code_stays_unknown():
    assert decode_electricity(None) is None
    assert decode_electricity(0x4E) == 70
    assert decode_electricity(82) is None


def test_parse_status_tlvs_extracts_serial_from_real_device_capture():
    response = bytes.fromhex(
        "aa4f0001ff010001010100120201000d030100010401004f05010001060100001301000a14080000000000000000000f200036333634303439363337353030303030303000000000000000000000000000007c"
    )

    fields = parse_status_tlvs(response)

    assert fields[0x13] == b"\x0a"
    assert fields[0x0F].split(b"\x00", 1)[0].decode("ascii") == "636404963750000000"


def test_parse_status_tlvs_rejects_bad_checksum():
    response = bytearray(make_frame(1, tlv(255, bytes([1]))))
    response[-1] ^= 0x01
    assert parse_status_tlvs(bytes(response)) == {}


def test_driver_reports_disconnected_socket_before_io():
    printer = MemobirdGT1(mac="00:11:22:33:44:55")

    with pytest.raises(RuntimeError, match="not connected"):
        printer.get_status()


def test_connect_consumes_handshake_frame_before_next_status(monkeypatch):
    import memobird_driver

    handshake = make_frame(1, tlv(19, b"\x0a"))
    fresh_status = make_frame(1, tlv(19, b"\x1e"))
    fake = FakeSocket(responses_by_command={1: [handshake, fresh_status]})
    monkeypatch.setattr(memobird_driver.socket, "socket", lambda *args: fake)
    printer = MemobirdGT1(mac="00:11:22:33:44:55", timeout=0.1)

    printer.connect()
    status = printer.get_status()

    assert status["auto_shutdown_minutes"] == 30
    assert fake.sent == [make_frame(1, b""), make_frame(1, b"")]


class FakeSocket:
    def __init__(self, response=None, timeout=7.0, chunks=None, responses_by_command=None):
        self.response = response
        self.chunks = list(chunks if chunks is not None else ([] if response is None else [response]))
        self.responses_by_command = {key: list(value) for key, value in (responses_by_command or {}).items()}
        self.timeout = timeout
        self.sent = []
        self.closed = False

    def connect(self, address):
        self.address = address

    def sendall(self, data):
        self.sent.append(data)
        if isinstance(getattr(self, "send_error", None), Exception):
            raise self.send_error
        if len(data) >= 4 and data[0] == 0xAA:
            command = data[3]
            responses = self.responses_by_command.get(command, [])
            if responses:
                response = responses.pop(0)
                if isinstance(response, list):
                    self.chunks.extend(response)
                else:
                    self.chunks.append(response)

    def recv(self, size):
        if self.chunks:
            chunk = self.chunks.pop(0)
            if isinstance(chunk, Exception):
                raise chunk
            return chunk[:size]
        raise socket.timeout()

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


def connected_printer(monkeypatch, response=None, responses_by_command=None):
    import memobird_driver

    monkeypatch.setattr(memobird_driver.time, "sleep", lambda _: None)
    printer = MemobirdGT1(mac="00:11:22:33:44:55")
    if response is not None:
        printer.sock = FakeSocket(response=response, responses_by_command=responses_by_command)
    else:
        printer.sock = FakeSocket(responses_by_command=responses_by_command or {4: [make_frame(4, b"")]})
    return printer


def test_print_image_uses_sendall_and_restores_timeout(monkeypatch):
    printer = connected_printer(monkeypatch)
    fake = printer.sock

    assert printer.print_image(Image.new("L", (1, 1))) is True
    assert fake.timeout == 7.0
    assert len(fake.sent) >= 3  # two warmups and image/feed frames
    assert all(isinstance(frame, bytes) for frame in fake.sent)


def test_print_image_timeout_is_not_success_and_restores_timeout(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={4: [socket.timeout()]})
    fake = printer.sock

    assert printer.print_image(Image.new("L", (1, 1)), feed_lines=0) is False
    assert fake.timeout == 7.0


def test_print_image_send_error_does_not_retry_or_reconnect(monkeypatch):
    printer = connected_printer(monkeypatch)
    fake = printer.sock
    fake.send_error = OSError("partial transport failure")
    reconnect_calls = []
    printer.connect = lambda: reconnect_calls.append(True)

    with pytest.raises(OSError, match="partial transport failure"):
        printer.print_image(Image.new("L", (1, 1)), feed_lines=0)
    assert reconnect_calls == []
    assert fake.timeout == 7.0


def test_auto_shutdown_uses_setting_frame_and_verified_readback(monkeypatch):
    ack = make_frame(3, tlv(14, b"\x01"))
    status = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={3: [ack], 1: [status]})
    fake = printer.sock

    result = printer.set_auto_shutdown(30)
    assert result == {
        "acknowledged": True, "verified": True,
        "requested_minutes": 30, "observed_minutes": 30,
    }
    assert fake.sent == [make_frame(3, tlv(19, b"\x1e")), make_frame(1, b"")]
    assert fake.timeout == 7.0


@pytest.mark.parametrize("minutes", [0, 1, 10, 30, 60, 120, 180, 240, 255])
def test_auto_shutdown_accepts_integer_byte_values(monkeypatch, minutes):
    ack = make_frame(3, b"")
    status = make_frame(1, tlv(19, bytes([minutes])))
    printer = connected_printer(monkeypatch, responses_by_command={3: [ack], 1: [status]})
    assert printer.set_auto_shutdown(minutes)["verified"] is True


@pytest.mark.parametrize("minutes", [-1, 256, True, 1.0, 10.5])
def test_auto_shutdown_rejects_invalid_values_before_writing(monkeypatch, minutes):
    printer = connected_printer(monkeypatch)
    fake = printer.sock

    with pytest.raises(ValueError, match="minutes must be an integer"):
        printer.set_auto_shutdown(minutes)
    assert fake.sent == []


def test_auto_shutdown_timeout_is_not_ack_and_restores_timeout(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={3: [socket.timeout()], 1: [socket.timeout()]})
    fake = printer.sock

    assert printer.set_auto_shutdown(0) == {
        "acknowledged": False, "verified": False,
        "requested_minutes": 0, "observed_minutes": None,
    }
    assert fake.timeout == 7.0


def test_print_text_ack_timeout_is_not_success(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={4: [socket.timeout()]})

    assert printer.print_text("hello") is False


def test_invalid_print_parameters_fail_before_writes(monkeypatch):
    printer = connected_printer(monkeypatch)
    fake = printer.sock

    with pytest.raises(ValueError, match="font_size"):
        printer.print_text("hello", font_size=256)
    with pytest.raises(ValueError, match="feed_lines"):
        printer.print_image(Image.new("L", (2, 2)), feed_lines=-1)
    assert fake.sent == []


def test_status_decodes_serial_and_shutdown_from_tlv(monkeypatch):
    response = make_frame(1, tlv(4, bytes([78])) + tlv(15, b"636404963750000000" + bytes([0]) * 14) + tlv(19, bytes([10])))
    printer = connected_printer(monkeypatch, responses_by_command={1: [response]})

    status = printer.get_status()

    assert status["sn"] == "636404963750000000"
    assert status["auto_shutdown_minutes"] == 10
    assert status["battery_raw"] == 78
    assert status["battery_percent"] == 70
    assert status["raw_fields"]["0x13"] == "0a"
    assert status["raw_resp"] == response.hex()


@pytest.mark.parametrize("response", [b"garbage", make_frame(1, tlv(15, b"sn"))[:-1], make_frame(3, tlv(15, b"sn"))])
def test_status_rejects_incomplete_or_wrong_command_frames(monkeypatch, response):
    printer = connected_printer(monkeypatch, responses_by_command={1: [response]})

    with pytest.raises((ValueError, TimeoutError, ConnectionError)):
        printer.get_status()


def test_status_empty_response_is_connection_failure(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={1: [b""]})

    with pytest.raises(ConnectionError, match="closed the connection"):
        printer.get_status()


def test_status_reassembles_fragmented_frame(monkeypatch):
    frame = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [[frame[:2], frame[2:5], frame[5:]]]})

    assert printer.get_status()["auto_shutdown_minutes"] == 30


def test_status_preserves_coalesced_next_frame(monkeypatch):
    status = make_frame(1, tlv(19, b"\x0a"))
    notification = make_frame(0x0A, b"event")
    printer = connected_printer(monkeypatch, responses_by_command={1: [status + notification]})

    assert printer.get_status()["auto_shutdown_minutes"] == 10
    assert printer.sock  # frame bytes remain available across requests
    assert printer._rx_buffer == notification


def test_pre_request_drain_discards_stale_reply_but_uses_new_reply(monkeypatch):
    stale_ack = make_frame(3, b"")
    stale_status = make_frame(1, tlv(19, b"\x0a"))
    fresh_ack = make_frame(3, b"")
    fresh_status = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(
        monkeypatch,
        response=stale_ack + stale_status,
        responses_by_command={3: [fresh_ack], 1: [fresh_status]},
    )

    result = printer.set_auto_shutdown(30)

    assert result["acknowledged"] is True
    assert result["verified"] is True
    assert result["observed_minutes"] == 30


def test_notification_is_queued_not_returned_as_requested_reply(monkeypatch):
    notification = make_frame(0x0A, b"event")
    status = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [notification + status]})

    result = printer.get_status()

    assert result["auto_shutdown_minutes"] == 30
    assert printer.notifications == [notification]


def test_bad_checksum_is_discarded_and_valid_frame_resynchronized(monkeypatch):
    bad = bytearray(make_frame(1, tlv(19, b"\x0a")))
    bad[-1] ^= 0x80
    valid = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [bytes(bad) + valid]})

    assert printer.get_status()["auto_shutdown_minutes"] == 30


def test_status_deadline_returns_timeout(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={1: [socket.timeout()]})

    with pytest.raises(TimeoutError, match="timed out"):
        printer.get_status()


def test_auto_shutdown_readback_mismatch_is_not_verified(monkeypatch):
    printer = connected_printer(
        monkeypatch,
        responses_by_command={3: [make_frame(3, b"")], 1: [make_frame(1, tlv(19, b"\xff"))]},
    )

    result = printer.set_auto_shutdown(0)

    assert result["acknowledged"] is True
    assert result["verified"] is False
    assert result["observed_minutes"] == 255
