import types
import pytest
import memobird_transport as t
from memobird_driver import MemobirdGT1, make_frame, tlv


class SerialError(Exception):
    pass


class WriteTimeout(SerialError):
    pass


class Port:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.data = bytearray()
        self.written = bytearray()
        self.closed = False
        self.chunk = 2
        self.error = None

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, size):
        if self.error:
            raise self.error
        result = bytes(self.data[:size])
        del self.data[:size]
        return result

    def write(self, data):
        if self.error:
            raise self.error
        count = min(self.chunk, len(data))
        self.written.extend(data[:count])
        return count

    def close(self):
        self.closed = True


@pytest.fixture
def stream(monkeypatch):
    module = types.SimpleNamespace(Serial=Port, SerialException=SerialError, SerialTimeoutException=WriteTimeout)
    monkeypatch.setattr(t, 'load_serial', lambda: module)
    return t.SerialStream('COM12', 1.0, 115200)


def test_serial_partial_writes_are_completed_once(stream):
    stream.sendall(b'abcdef')
    assert stream._port.written == b'abcdef'
    assert stream._port.write_timeout == 1.0


def test_serial_read_returns_available_bytes_without_full_buffer_wait(stream):
    stream._port.data.extend(b'abcdef')
    assert stream.recv(3) == b'abc'
    assert stream.recv(1024) == b'def'


def test_serial_empty_read_is_timeout_not_disconnect(stream):
    with pytest.raises(TimeoutError):
        stream.recv(100)
    stream.settimeout(0)
    with pytest.raises(TimeoutError):
        stream.recv(100)
    assert stream.gettimeout() == 0


def test_serial_write_failure_is_not_replayed(stream):
    stream._port.error = WriteTimeout('timeout')
    with pytest.raises(TimeoutError, match='partially sent'):
        stream.sendall(b'abc')
    assert stream._port.written == b''


def test_serial_disconnect_is_connection_error(stream):
    stream._port.error = SerialError('removed')
    with pytest.raises(ConnectionError, match='removed'):
        stream.recv(1)


def test_serial_zero_write_fails_without_looping(stream):
    stream._port.chunk = 0
    with pytest.raises(ConnectionError, match='progress'):
        stream.sendall(b'abc')


def test_serial_close_and_timeout_updates(stream):
    stream.settimeout(0.5)
    assert stream._port.timeout == 0.5
    assert stream._port.write_timeout == 0.5
    stream.close()
    assert stream._port.closed


@pytest.mark.parametrize('device', ['COM12', '/dev/rfcomm0', '/dev/cu.MEMOBIRD-SPP'])
def test_system_ports_select_serial_without_mac(device):
    printer = MemobirdGT1(serial_port=device)
    assert printer.transport == 'serial'
    assert printer.mac is None


@pytest.mark.parametrize('kwargs', [
    {'serial_port':'socket://example:123'}, {'serial_port':''},
    {'serial_port':'COM3','transport':'rfcomm'}, {'transport':'serial'},
    {'mac':'bad'}, {'mac':'00:11:22:33:44:55','timeout':float('nan')},
    {'mac':'00:11:22:33:44:55','baudrate':True},
    {'mac':'00:11:22:33:44:55','port':0},
])
def test_invalid_configuration_fails_before_open(kwargs):
    with pytest.raises(ValueError):
        MemobirdGT1(**kwargs)


def test_missing_pyserial_has_install_hint(monkeypatch):
    def unavailable():
        raise RuntimeError("System serial transport requires pyserial; install requirements-serial.txt first")
    monkeypatch.setattr(t, 'load_serial', unavailable)
    with pytest.raises(RuntimeError, match='requirements-serial.txt'):
        t.SerialStream('COM7', 1.0, 115200)


def test_missing_native_bluetooth_has_actionable_error(monkeypatch):
    monkeypatch.delattr(t.socket, 'AF_BLUETOOTH', raising=False)
    with pytest.raises(RuntimeError, match='configure serial_port'):
        t.open_transport('00:11:22:33:44:55',None,'rfcomm',1,115200,1)


def test_serial_backend_connects_queries_status_sets_value_and_prints(monkeypatch):
    ports = []

    class ResponsivePort(Port):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.chunk = 65536
            self.status_requests = 0
            self.shutdown = 255
            ports.append(self)

        def write(self, data):
            count = super().write(data)
            if data and data[0] == 0xAA and len(data) >= 4:
                command = data[3]
                if command == 1:
                    self.status_requests += 1
                    if self.status_requests == 1:
                        response = make_frame(1, b"")
                    else:
                        response = make_frame(1, tlv(4, b"\x4e") + tlv(15, b"TEST-SERIAL\x00") + tlv(19, bytes([self.shutdown])))
                    self.data.extend(response)
                elif command == 3:
                    self.shutdown = data[7]
                    self.data.extend(make_frame(3, b"ack"))
                elif command == 4:
                    self.data.extend(make_frame(4, b"ack"))
            return count

    module = types.SimpleNamespace(Serial=ResponsivePort, SerialException=SerialError, SerialTimeoutException=WriteTimeout)
    monkeypatch.setattr(t, 'load_serial', lambda: module)
    printer = MemobirdGT1(serial_port='COM12', timeout=0.5)

    with printer:
        status = printer.get_status()
        setting = printer.set_auto_shutdown(30)
        assert printer.print_text("hello") is True

    assert status["battery_percent"] == 70
    assert status["auto_shutdown_minutes"] == 255
    assert setting["verified"] is True
    assert setting["observed_minutes"] == 30
    assert ports[0].closed
