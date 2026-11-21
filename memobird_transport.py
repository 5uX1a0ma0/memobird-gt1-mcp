"""Byte-stream backends. Pairing and SPP endpoint creation belong to the OS."""
import math
import re
import socket
import time


def validate_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")


def load_serial():
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("System serial transport requires pyserial; install requirements-serial.txt first") from exc
    if not hasattr(serial, "Serial"):
        raise RuntimeError("The installed 'serial' module is not pyserial")
    return serial


class SerialStream:
    """Adapt pySerial timeout/partial-write semantics to the driver's socket interface."""
    def __init__(self, device, timeout, baudrate):
        serial = load_serial()
        self._serial_errors = serial.SerialException
        self._timeout_errors = serial.SerialTimeoutException
        self._timeout = timeout
        self._port = serial.Serial(port=device, baudrate=baudrate, timeout=timeout,
                                   write_timeout=timeout, xonxoff=False, rtscts=False, dsrdtr=False)

    def settimeout(self, timeout):
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be None or a finite nonnegative number")
        self._port.timeout = timeout
        self._port.write_timeout = timeout
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def recv(self, size):
        if size <= 0:
            return b""
        try:
            # Wait for one byte, then return currently available bytes instead
            # of waiting for an entire socket-sized read to fill.
            first = self._port.read(1)
            if not first:
                raise TimeoutError("serial read timed out")
            remaining = min(size - 1, self._port.in_waiting)
            return first + (self._port.read(remaining) if remaining else b"")
        except self._serial_errors as exc:
            raise ConnectionError("serial read failed: %s" % exc) from exc

    def sendall(self, data):
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        sent = 0
        try:
            while sent < len(data):
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("serial write timed out; data may be partially sent; not retried")
                    self._port.write_timeout = remaining
                count = self._port.write(data[sent:])
                if count is None or count <= 0 or count > len(data) - sent:
                    raise ConnectionError("serial write made invalid progress; data may be partially sent")
                sent += count
        except self._timeout_errors as exc:
            raise TimeoutError("serial write timed out; data may be partially sent; not retried") from exc
        except self._serial_errors as exc:
            raise ConnectionError("serial write failed; data may be partially sent: %s" % exc) from exc
        finally:
            self._port.write_timeout = self._timeout

    def close(self):
        self._port.close()


def select_backend(mac=None, serial_port=None, transport="auto", port=1, baudrate=115200, timeout=10.0):
    validate_timeout(timeout)
    if transport not in ("auto", "rfcomm", "serial"):
        raise ValueError("transport must be auto, rfcomm or serial")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 30:
        raise ValueError("RFCOMM channel must be an integer from 1 to 30")
    if not isinstance(baudrate, int) or isinstance(baudrate, bool) or baudrate <= 0:
        raise ValueError("baudrate must be a positive integer")
    if mac is not None and (not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac)):
        raise ValueError("mac must use the colon-separated Bluetooth address format")
    if serial_port is not None and (not isinstance(serial_port, str) or not serial_port.strip() or "://" in serial_port or "\x00" in serial_port):
        raise ValueError("serial_port must name a local OS serial device, not a URL")
    backend = ("serial" if serial_port else "rfcomm") if transport == "auto" else transport
    if backend == "serial" and not serial_port:
        raise ValueError("serial transport requires serial_port or MEMOBIRD_SERIAL_PORT")
    if backend == "rfcomm":
        if serial_port:
            raise ValueError("serial_port conflicts with rfcomm transport")
        if not mac:
            raise ValueError("provide mac or set MEMOBIRD_MAC, or configure a system serial_port")
    return backend


def open_transport(mac, serial_port, transport, port, baudrate, timeout):
    backend = select_backend(mac, serial_port, transport, port, baudrate, timeout)
    if backend == "serial":
        return SerialStream(serial_port, timeout, baudrate)
    if not all(hasattr(socket, name) for name in ("AF_BLUETOOTH", "BTPROTO_RFCOMM")):
        raise RuntimeError("This Python/OS has no native RFCOMM socket; pair using system Bluetooth and configure serial_port (COMx or /dev/...)")
    stream = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        stream.settimeout(timeout)
        stream.connect((mac, port))
        return stream
    except Exception:
        stream.close()
        raise


def list_serial_ports():
    """Enumerate only; never opens, probes, pairs or selects a port."""
    load_serial()
    from serial.tools import list_ports
    return [{"device": p.device, "description": p.description}
            for p in sorted(list_ports.comports(), key=lambda p: p.device)]
