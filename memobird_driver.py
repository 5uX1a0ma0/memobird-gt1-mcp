import io
import time
import socket
from typing import Optional, Dict, Any, List
from PIL import Image
from memobird_transport import open_transport, select_backend

DEFAULT_PORT = 1
DOT_WIDTH = 384  # GT1 打印头物理宽度：384 点阵
AUTO_SHUTDOWN_MINUTES = (0, 10, 30, 60, 120, 180, 240)
ELECTRICITY_PERCENT = {71: 10, 72: 15, 73: 20, 74: 30, 75: 40, 76: 50, 77: 60, 78: 70, 79: 80, 80: 90, 81: 100}

def le16(n: int) -> bytes:
    if not 0 <= n <= 0xFFFF:
        raise ValueError("16-bit protocol field out of range")
    return bytes([n & 0xFF, (n >> 8) & 0xFF])

def tlv(tag: int, val: bytes) -> bytes:
    if not 0 <= tag <= 0xFF:
        raise ValueError("TLV tag out of range")
    return bytes([tag]) + le16(len(val)) + val

def make_frame(cmd: int, payload: bytes) -> bytes:
    if not 0 <= cmd <= 0xFF:
        raise ValueError("command out of range")
    cmd_bytes = bytes([cmd])
    total_len = len(cmd_bytes) + len(payload)
    len_bytes = le16(total_len)
    body = b"\xAA" + len_bytes + cmd_bytes + payload
    chk = (256 - (sum(body) & 0xFF)) & 0xFF
    return body + bytes([chk])

def parse_status_tlvs(response: bytes) -> Dict[int, bytes]:
    if len(response) < 5 or response[0] != 0xAA or response[3] != 1:
        return {}
    declared_length = int.from_bytes(response[1:3], "little")
    if declared_length != len(response) - 4 or sum(response) & 0xFF:
        return {}

    fields = {}
    offset = 4
    end = len(response) - 1
    while offset < end:
        if offset + 3 > end:
            return {}
        tag = response[offset]
        size = int.from_bytes(response[offset + 1:offset + 3], "little")
        offset += 3
        if offset + size > end:
            return {}
        fields[tag] = response[offset:offset + size]
        offset += size
    return fields


def decode_electricity(raw_value: Optional[int]) -> Optional[int]:
    """Decode the GT1's raw electricity code using the official generic table."""
    if raw_value is None:
        return None
    return ELECTRICITY_PERCENT.get(raw_value)


def split_gbk(text: str, max_bytes: int) -> List[bytes]:
    """Split GBK text without breaking a multibyte character."""
    if max_bytes < 2:
        raise ValueError("max_bytes must be at least 2")
    chunks: List[bytes] = []
    current = bytearray()
    for char in text:
        encoded = char.encode("gbk", errors="replace")
        if current and len(current) + len(encoded) > max_bytes:
            chunks.append(bytes(current))
            current.clear()
        current.extend(encoded)
    if current:
        chunks.append(bytes(current))
    return chunks

class MemobirdGT1:
    def __init__(self, mac: Optional[str] = None, port: int = DEFAULT_PORT, timeout: float = 10.0,
                 serial_port: Optional[str] = None, transport: str = "auto", baudrate: int = 115200):
        self.transport = select_backend(mac, serial_port, transport, port, baudrate, timeout)
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.mac = mac.upper() if mac and self.transport == "rfcomm" else None
        self.port = port
        self.timeout = timeout
        self.sock: Optional[Any] = None
        self._rx_buffer = bytearray()
        self.notifications: List[bytes] = []

    def connect(self):
        self.close()
        self._rx_buffer.clear()
        self.notifications.clear()
        sock = open_transport(self.mac, self.serial_port, self.transport,
                              self.port, self.baudrate, self.timeout)
        self.sock = sock
        try:
            sock.sendall(make_frame(cmd=1, payload=b""))
            try:
                # Consume the handshake reply as a complete frame so it cannot
                # satisfy a later status request.
                sock.settimeout(min(self.timeout, 0.25))
                self._wait_for_command(1, time.monotonic() + min(self.timeout, 0.25))
            finally:
                if self.sock is sock:
                    sock.settimeout(self.timeout)
            self._discard_pending()
        except Exception:
            self.close()
            raise

    def _socket(self) -> Any:
        if self.sock is None:
            raise RuntimeError("printer is not connected")
        return self.sock

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._rx_buffer.clear()

    def _read_frame(self, deadline: float) -> Optional[bytes]:
        """Read one checksum-valid frame from the byte stream before deadline."""
        sock = self._socket()
        while True:
            while self._rx_buffer and self._rx_buffer[0] != 0xAA:
                del self._rx_buffer[0]
            if len(self._rx_buffer) >= 4:
                body_length = int.from_bytes(self._rx_buffer[1:3], "little")
                if body_length < 1:
                    del self._rx_buffer[0]
                    continue
                frame_length = body_length + 4
                if len(self._rx_buffer) >= frame_length:
                    candidate = bytes(self._rx_buffer[:frame_length])
                    if sum(candidate) & 0xFF:
                        del self._rx_buffer[0]
                        continue
                    del self._rx_buffer[:frame_length]
                    return candidate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                data = sock.recv(1024)
            except (socket.timeout, TimeoutError):
                return None
            if not data:
                self.close()
                raise ConnectionError("printer closed the connection before returning a frame")
            self._rx_buffer.extend(data)

    def _wait_for_command(self, command: int, deadline: float) -> Optional[bytes]:
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                return None
            if frame[3] == 0x0A:
                self.notifications.append(frame)
                continue
            if frame[3] == command:
                return frame

    def _discard_pending(self, max_bytes: int = 65536):
        """Discard stale buffered/socket data without blocking for new replies."""
        sock = self._socket()
        previous_timeout = sock.gettimeout()
        self._rx_buffer.clear()
        drained = 0
        try:
            sock.settimeout(0.0)
            while drained < max_bytes:
                try:
                    data = sock.recv(min(1024, max_bytes - drained))
                except (socket.timeout, TimeoutError, BlockingIOError):
                    break
                if not data:
                    self.close()
                    raise ConnectionError("printer closed the connection while draining stale data")
                drained += len(data)
                self._rx_buffer.extend(data)
                while self._rx_buffer:
                    while self._rx_buffer and self._rx_buffer[0] != 0xAA:
                        del self._rx_buffer[0]
                    if len(self._rx_buffer) < 4:
                        break
                    body_length = int.from_bytes(self._rx_buffer[1:3], "little")
                    if body_length < 1:
                        del self._rx_buffer[0]
                        continue
                    frame_length = body_length + 4
                    if len(self._rx_buffer) < frame_length:
                        break
                    candidate = bytes(self._rx_buffer[:frame_length])
                    if sum(candidate) & 0xFF:
                        del self._rx_buffer[0]
                        continue
                    del self._rx_buffer[:frame_length]
                    if candidate[3] == 0x0A:
                        self.notifications.append(candidate)
            # Any remaining partial frame predates the next request and is stale.
            self._rx_buffer.clear()
        finally:
            if self.sock is sock:
                sock.settimeout(previous_timeout)

    def _request_frame(self, command: int, payload: bytes = b"") -> Optional[bytes]:
        sock = self._socket()
        previous_timeout = sock.gettimeout()
        deadline = time.monotonic() + max(0.0, self.timeout)
        try:
            self._discard_pending()
            sock.sendall(make_frame(command, payload))
            return self._wait_for_command(command, deadline)
        finally:
            if self.sock is sock:
                sock.settimeout(previous_timeout)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_status(self) -> Dict[str, Any]:
        resp = self._request_frame(1)
        if resp is None:
            raise TimeoutError("timed out waiting for GT1 status frame")
        fields = parse_status_tlvs(resp)
        if not fields:
            raise ValueError("invalid or incomplete GT1 status frame")
        raw_sn = fields.get(0x0F, b"").split(b"\x00", 1)[0]
        sn = raw_sn.decode("ascii", errors="replace")
        battery_raw = fields[0x04][0] if len(fields.get(0x04, b"")) == 1 else None
        return {
            "connected": True,
            "mac": self.mac,
            "transport": self.transport,
            "serial_port": self.serial_port,
            "sn": sn,
            "battery_raw": battery_raw,
            "battery_percent": decode_electricity(battery_raw),
            "auto_shutdown_minutes": fields[0x13][0] if len(fields.get(0x13, b"")) == 1 else None,
            "raw_fields": {f"0x{tag:02x}": value.hex() for tag, value in fields.items()},
            "raw_resp": resp.hex()
        }

    def set_auto_shutdown(self, minutes: int) -> Dict[str, Any]:
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= 0xFF:
            raise ValueError("minutes must be an integer from 0 to 255")
        sock = self._socket()
        previous_timeout = sock.gettimeout()
        ack = None
        observed = None
        try:
            self._discard_pending()
            sock.sendall(make_frame(cmd=3, payload=tlv(19, bytes([minutes]))))
            ack_deadline = time.monotonic() + min(max(0.0, self.timeout), 3.0)
            ack = self._wait_for_command(3, ack_deadline)

            # Read back even without an ACK: a lost/omitted ACK must not hide
            # whether the device actually changed its stored value.
            self._discard_pending()
            sock.sendall(make_frame(cmd=1, payload=b""))
            status_deadline = time.monotonic() + min(max(0.0, self.timeout), 3.0)
            status = self._wait_for_command(1, status_deadline)
            if status is not None:
                fields = parse_status_tlvs(status)
                value = fields.get(19, b"")
                if len(value) == 1:
                    observed = value[0]
            return {
                "acknowledged": ack is not None,
                "verified": observed == minutes,
                "requested_minutes": minutes,
                "observed_minutes": observed,
            }
        finally:
            if self.sock is sock:
                sock.settimeout(previous_timeout)

    def _warmup(self):
        # 官方驱动固件保护预热序列
        sock = self._socket()
        sock.sendall(b"\x00" * 1024)
        time.sleep(0.08)
        sock.sendall(b"\x00" * 1024)
        time.sleep(0.08)

    def print_text(self, text: str, bold: bool = False, underline: bool = False, font_size: int = 0) -> bool:
        if not isinstance(font_size, int) or isinstance(font_size, bool) or not 0 <= font_size <= 0xFF:
            raise ValueError("font_size must be an integer from 0 to 255")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text.endswith("\n"):
            text += "\n"
        # Prepare every protocol field before touching the printer.
        chunks = split_gbk(text, 300) or [b"\n"]
        total = le16(len(chunks))
        packets = []
        for idx, chunk in enumerate(chunks, 1):
            payload = (
                tlv(11, total) + tlv(12, le16(idx))
                + tlv(13, bytes([1 if bold else 0]))
                + tlv(17, bytes([1 if underline else 0]))
                + tlv(16, bytes([font_size])) + tlv(7, chunk)
            )
            packets.append(make_frame(cmd=4, payload=payload))

        self._warmup()
        sock = self._socket()
        for packet in packets:
            sock.sendall(packet)
            time.sleep(0.05)
        previous_timeout = sock.gettimeout()
        try:
            return self._wait_for_command(4, time.monotonic() + max(0.0, self.timeout)) is not None
        finally:
            if self.sock is sock:
                sock.settimeout(previous_timeout)

    def print_image(self, image_input, dither: bool = True, feed_lines: int = 3) -> bool:
        """
        Return whether an ACK was received; this does not confirm physical output.
        image_input: PIL.Image 实例、文件路径 (str) 或 二进制流 (bytes)
        """
        if not isinstance(feed_lines, int) or isinstance(feed_lines, bool) or not 0 <= feed_lines <= 65509:
            raise ValueError("feed_lines must be an integer from 0 to 65509")
        if isinstance(image_input, str):
            img = Image.open(image_input)
        elif isinstance(image_input, bytes):
            img = Image.open(io.BytesIO(image_input))
        elif isinstance(image_input, Image.Image):
            img = image_input
        else:
            raise ValueError("Unsupported image input type")

        # 转换为单色点阵图（宽度固定为 384 点）
        if img.mode != 'L':
            img = img.convert('L')

        # 按比例缩放到宽 384
        w, h = img.size
        new_w = DOT_WIDTH
        if w <= 0 or h <= 0:
            raise ValueError("image dimensions must be positive")
        new_h = max(1, int(h * (new_w / w)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # 二值化（抖动算法保留灰度层次细节）
        if dither:
            img = img.convert('1', dither=Image.Dither.FLOYDSTEINBERG)
        else:
            img = img.point(lambda x: 0 if x < 128 else 255, '1')

        # 与官方 SDK 对齐：BMPFile.createBMPArray 生成的是 biHeight 为正、
        # 像素行序 top-down 的非标准 BMP，打印机固件即按 top-down 解析数据。
        # PIL 默认输出 bottom-up 行序，先垂直翻转图像再以标准格式保存，
        # 最终文件数据行序即变为 top-down，与官方 BMP 完全一致。
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        bmp_io = io.BytesIO()
        img.save(bmp_io, format="BMP")
        bmp_bytes = bmp_io.getvalue()

        CHUNK_SIZE = 1024
        total_pkts = (len(bmp_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
        tlv_total = tlv(11, le16(total_pkts))
        frames = []
        for i in range(total_pkts):
            pkt_no = i + 1
            chunk = bmp_bytes[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
            payload = tlv_total + tlv(12, le16(pkt_no)) + tlv(8, chunk)
            frames.append(make_frame(cmd=4, payload=payload))
        feed_frame = None
        if feed_lines:
            feed_txt = "\n" * feed_lines
            feed_payload = (tlv(11, le16(1)) + tlv(12, le16(1)) + tlv(13, bytes([0]))
                            + tlv(17, bytes([0])) + tlv(16, bytes([0])) + tlv(7, feed_txt.encode("gbk")))
            feed_frame = make_frame(cmd=4, payload=feed_payload)

        self._warmup()
        sock = self._socket()
        previous_timeout = sock.gettimeout()
        try:
            sock.settimeout(3.0)
            for frame in frames:
                sock.sendall(frame)
                # Firmware accepts a burst; per-packet ACK waits cause long print timeouts.
                time.sleep(0.05)
            if feed_frame is not None:
                time.sleep(0.8)
                sock.sendall(feed_frame)
            return self._wait_for_command(4, time.monotonic() + max(0.0, self.timeout)) is not None
        finally:
            if self.sock is sock:
                sock.settimeout(previous_timeout)
