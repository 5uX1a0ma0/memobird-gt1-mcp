import socket

import pytest
from PIL import Image

from memobird_driver import (MemobirdGT1, decode_electricity, le16, make_frame,
                             parse_status_tlvs, split_gbk, tlv)


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


def test_driver_reports_disconnected_socket_before_io():
    printer = MemobirdGT1(mac="00:11:22:33:44:55")

    with pytest.raises(RuntimeError, match="not connected"):
        printer.get_status()


class FakeSocket:
    def __init__(self, response=None, timeout=7.0, chunks=None, responses_by_command=None):
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
    printer.sock = FakeSocket(response=response, responses_by_command=responses_by_command or {4: [make_frame(4, b"")]})
    return printer


def test_connect_consumes_handshake_before_print_ack(monkeypatch):
    import memobird_driver

    handshake = make_frame(1, tlv(19, b"\x0a"))
    ack = make_frame(4, b"")
    fake = FakeSocket(responses_by_command={1: [handshake], 4: [ack]})
    monkeypatch.setattr(memobird_driver, "open_transport", lambda *args: fake)
    printer = MemobirdGT1(mac="00:11:22:33:44:55", timeout=0.1)

    printer.connect()
    acknowledged = printer.print_text("hello")

    assert acknowledged is True
    assert fake.sent[0] == make_frame(1, b"")
    assert fake.sent[-1][3] == 4
    printer.close()
    assert fake.closed


def test_connect_consumes_handshake_before_next_status(monkeypatch):
    import memobird_driver

    handshake = make_frame(1, tlv(19, b"\x0a"))
    fresh_status = make_frame(1, tlv(19, b"\x1e"))
    fake = FakeSocket(responses_by_command={1: [handshake, fresh_status]})
    monkeypatch.setattr(memobird_driver, "open_transport", lambda *args: fake)
    printer = MemobirdGT1(mac="00:11:22:33:44:55", timeout=0.1)

    printer.connect()
    status = printer.get_status()

    assert status["auto_shutdown_minutes"] == 30
    assert fake.sent == [make_frame(1, b""), make_frame(1, b"")]
    printer.close()


def test_print_image_uses_sendall_and_restores_timeout(monkeypatch):
    printer = connected_printer(monkeypatch)
    fake = printer.sock

    assert printer.print_image(Image.new("L", (1, 1))) is True
    assert fake.timeout == 7.0
    assert len(fake.sent) >= 3
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


def test_print_ack_reader_reassembles_fragmented_frames(monkeypatch):
    frame = make_frame(4, b"ack")
    printer = connected_printer(monkeypatch, responses_by_command={4: [[frame[:2], frame[2:5], frame[5:]]]})

    assert printer.print_text("hello") is True


def test_notifications_are_queued_before_print_ack(monkeypatch):
    notification = make_frame(0x0A, b"event")
    ack = make_frame(4, b"")
    printer = connected_printer(monkeypatch, responses_by_command={4: [notification + ack]})

    assert printer.print_text("hello") is True
    assert printer.notifications == [notification]


def test_bad_checksum_is_discarded_and_valid_ack_resynchronized(monkeypatch):
    bad = bytearray(make_frame(4, b"bad"))
    bad[-1] ^= 0x80
    valid = make_frame(4, b"ok")
    printer = connected_printer(monkeypatch, responses_by_command={4: [bytes(bad) + valid]})

    assert printer.print_text("hello") is True


@pytest.mark.parametrize("raw,percent", [(71, 10), (72, 15), (73, 20), (74, 30), (75, 40), (76, 50), (77, 60), (78, 70), (79, 80), (80, 90), (81, 100)])
def test_decode_electricity_uses_gt1_table(raw, percent):
    assert decode_electricity(raw) == percent


def test_unknown_electricity_code_stays_unknown():
    assert decode_electricity(None) is None
    assert decode_electricity(82) is None


def test_parse_status_tlvs_extracts_fields_from_valid_frame():
    response = make_frame(1, tlv(4, b"\x4e") + tlv(15, b"TEST-SERIAL\x00") + tlv(19, b"\x0a"))

    assert parse_status_tlvs(response) == {4: b"\x4e", 15: b"TEST-SERIAL\x00", 19: b"\x0a"}


def test_parse_status_tlvs_rejects_bad_checksum():
    response = bytearray(make_frame(1, tlv(19, b"\x0a")))
    response[-1] ^= 1

    assert parse_status_tlvs(bytes(response)) == {}


def test_status_decodes_serial_battery_and_shutdown(monkeypatch):
    response = make_frame(1, tlv(4, b"\x4e") + tlv(15, b"TEST-SERIAL\x00") + tlv(19, b"\xff"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [response]})

    status = printer.get_status()

    assert status["sn"] == "TEST-SERIAL"
    assert status["battery_raw"] == 78
    assert status["battery_percent"] == 70
    assert status["auto_shutdown_minutes"] == 255
    assert status["raw_fields"]["0x13"] == "ff"
    assert status["raw_resp"] == response.hex()


@pytest.mark.parametrize("response", [
    b"garbage",
    make_frame(1, tlv(15, b"sn"))[:-1],
    make_frame(3, tlv(15, b"sn")),
])
def test_status_rejects_incomplete_or_wrong_command_frames(monkeypatch, response):
    printer = connected_printer(monkeypatch, responses_by_command={1: [response]})

    with pytest.raises((ValueError, TimeoutError, ConnectionError)):
        printer.get_status()


def test_status_timeout_is_not_reported_as_a_success(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={1: [socket.timeout()]})

    with pytest.raises(TimeoutError, match="timed out"):
        printer.get_status()


def test_auto_shutdown_setting_requires_matching_status_readback(monkeypatch):
    ack = make_frame(3, tlv(14, b"\x01"))
    status = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={3: [ack], 1: [status]})
    fake = printer.sock

    assert printer.set_auto_shutdown(30) == {
        "acknowledged": True,
        "verified": True,
        "requested_minutes": 30,
        "observed_minutes": 30,
    }
    assert fake.sent == [make_frame(3, tlv(19, b"\x1e")), make_frame(1, b"")]
    assert fake.timeout == 7.0


@pytest.mark.parametrize("minutes", [-1, 256, True, 1.0, 10.5])
def test_auto_shutdown_rejects_invalid_values_before_writing(monkeypatch, minutes):
    printer = connected_printer(monkeypatch)

    with pytest.raises(ValueError, match="integer from 0 to 255"):
        printer.set_auto_shutdown(minutes)
    assert printer.sock.sent == []


def test_auto_shutdown_mismatch_is_not_verified(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={
        3: [make_frame(3, b"")],
        1: [make_frame(1, tlv(19, b"\xff"))],
    })

    result = printer.set_auto_shutdown(0)

    assert result == {
        "acknowledged": True,
        "verified": False,
        "requested_minutes": 0,
        "observed_minutes": 255,
    }


@pytest.mark.parametrize("minutes", [0, 1, 10, 30, 60, 120, 180, 240, 255])
def test_auto_shutdown_accepts_integer_byte_values(monkeypatch, minutes):
    printer = connected_printer(monkeypatch, responses_by_command={
        3: [make_frame(3, b"")],
        1: [make_frame(1, tlv(19, bytes([minutes])))],
    })

    assert printer.set_auto_shutdown(minutes)["verified"] is True


def test_auto_shutdown_timeout_is_not_ack_and_restores_timeout(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={
        3: [socket.timeout()],
        1: [socket.timeout()],
    })
    fake = printer.sock

    assert printer.set_auto_shutdown(0) == {
        "acknowledged": False,
        "verified": False,
        "requested_minutes": 0,
        "observed_minutes": None,
    }
    assert fake.timeout == 7.0


def test_status_empty_response_is_connection_failure(monkeypatch):
    printer = connected_printer(monkeypatch, responses_by_command={1: [b""]})

    with pytest.raises(ConnectionError, match="closed the connection"):
        printer.get_status()


def test_status_reassembles_fragmented_response(monkeypatch):
    frame = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [[frame[:2], frame[2:5], frame[5:]]]})

    assert printer.get_status()["auto_shutdown_minutes"] == 30


def test_status_preserves_coalesced_notification(monkeypatch):
    status = make_frame(1, tlv(19, b"\x0a"))
    notification = make_frame(0x0A, b"event")
    printer = connected_printer(monkeypatch, responses_by_command={1: [status + notification]})

    assert printer.get_status()["auto_shutdown_minutes"] == 10
    assert printer._rx_buffer == notification


def test_status_queues_notification_before_status_response(monkeypatch):
    notification = make_frame(0x0A, b"event")
    status = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [notification + status]})

    assert printer.get_status()["auto_shutdown_minutes"] == 30
    assert printer.notifications == [notification]


def test_status_resynchronizes_after_bad_checksum(monkeypatch):
    bad = bytearray(make_frame(1, tlv(19, b"\x0a")))
    bad[-1] ^= 1
    valid = make_frame(1, tlv(19, b"\x1e"))
    printer = connected_printer(monkeypatch, responses_by_command={1: [bytes(bad) + valid]})

    assert printer.get_status()["auto_shutdown_minutes"] == 30


def test_setting_discards_stale_replies_before_command(monkeypatch):
    printer = connected_printer(
        monkeypatch,
        response=make_frame(3, b"") + make_frame(1, tlv(19, b"\x0a")),
        responses_by_command={
            3: [make_frame(3, b"")],
            1: [make_frame(1, tlv(19, b"\x1e"))],
        },
    )

    result = printer.set_auto_shutdown(30)

    assert result["acknowledged"] is True
    assert result["verified"] is True
    assert result["observed_minutes"] == 30
