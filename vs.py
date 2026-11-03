#!/usr/bin/env python3
"""
VLESS over WebSocket 单文件服务端。

目标：
- Python 3.10+，只依赖标准库。
- 纯 asyncio 实现 HTTP、WebSocket 和 TCP 转发。
- 兼容 Xray / sing-box / Clash.Meta / Hiddify 常见 VLESS-WS 客户端。
- 保留 /time 和 /cw HTTP API。

说明：
- 当前实现面向 VLESS TCP over WebSocket，并支持主流客户端的 Mux.Cool TCP/UDP。
- 原生 VLESS UDP、XUDP 不在本文件实现范围内。
- 支持 Xray WebSocket early data：客户端可通过 Sec-WebSocket-Protocol 携带 VLESS 首包。
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import math
import socket
import ssl
import struct
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit


# =========================
# 默认配置
# =========================

DEFAULT_UUID = "eefb6230-b823-40bc-bbe9-6216449e3f9e"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 10086
DEFAULT_PATH = "/vless"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_HTTP_TIMEOUT = 15.0
DEFAULT_MAX_HEADER_SIZE = 64 * 1024
DEFAULT_MAX_WS_MESSAGE_SIZE = 4 * 1024 * 1024
DEFAULT_TCP_READ_SIZE = 64 * 1024
DEFAULT_UDP_IDLE_TIMEOUT = 60.0
DEFAULT_UDP_QUEUE_SIZE = 128
DEFAULT_LOG_LEVEL = "INFO"

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CMD_TCP = 0x01
CMD_UDP = 0x02
CMD_MUX = 0x03

MUX_NETWORK_TCP = 0x01
MUX_NETWORK_UDP = 0x02
MUX_STATUS_NEW = 0x01
MUX_STATUS_KEEP = 0x02
MUX_STATUS_END = 0x03
MUX_STATUS_KEEPALIVE = 0x04
MUX_OPTION_DATA = 0x01
MUX_OPTION_ERROR = 0x02
MUX_KNOWN_OPTIONS = MUX_OPTION_DATA | MUX_OPTION_ERROR
MUX_MAX_METADATA_SIZE = 512
MUX_MAX_CHUNK_SIZE = 0xFFFF
MUX_MIN_STREAM_BUFFER_SIZE = MUX_MAX_METADATA_SIZE + MUX_MAX_CHUNK_SIZE + 4

ADDR_IPV4 = 0x01
ADDR_DOMAIN = 0x02
ADDR_IPV6 = 0x03

OP_CONTINUATION = 0x00
OP_TEXT = 0x01
OP_BINARY = 0x02
OP_CLOSE = 0x08
OP_PING = 0x09
OP_PONG = 0x0A
VALID_WS_OPCODES = {
    OP_CONTINUATION,
    OP_TEXT,
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_PONG,
}


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)


class VlessError(Exception):
    """VLESS 协议错误。"""


class VlessNeedMore(VlessError):
    """首包还不够解析完整 VLESS header。"""


class HttpError(Exception):
    """HTTP 请求格式错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class WebSocketError(Exception):
    """WebSocket 帧或握手错误。"""


class WebSocketMessageTooLarge(WebSocketError):
    """WebSocket frame/message 超过配置大小。"""


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    user_uuid: uuid.UUID
    path: str
    connect_timeout: float
    http_timeout: float
    max_header_size: int
    max_ws_message_size: int
    tcp_read_size: int
    udp_idle_timeout: float
    udp_queue_size: int
    ssl_context: Optional[ssl.SSLContext]


@dataclass
class HttpRequest:
    method: str
    version: str
    path: str
    headers: Dict[str, str]
    peer: str


@dataclass
class VlessRequest:
    version: int
    command: int
    host: str
    port: int
    initial_payload: memoryview


@dataclass
class MuxFrame:
    session_id: int
    status: int
    option: int
    network: int
    host: str
    port: int
    data: bytes


def normalize_path(path: str) -> str:
    """兼容 /vless 与 /vless/，但不把根路径 / 变成空字符串。"""

    return path.rstrip("/") or "/"


def parse_headers(raw_headers: bytes, peer: str) -> HttpRequest:
    """
    解析 HTTP/1.x 请求头。

    这里不实现通用 HTTP 服务器，只解析本服务需要的字段。请求体会被忽略，
    因为 /cw 当前只返回固定状态，WebSocket 握手也不需要 body。
    """

    text = raw_headers.decode("iso-8859-1")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise HttpError(400, "Bad Request")

    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HttpError(400, "Bad Request")

    method, target, version = parts
    if not method or not target:
        raise HttpError(400, "Bad Request")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpError(400, "Bad Request")

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise HttpError(400, "Bad Request")
        name, value = line.split(":", 1)
        key = name.strip().lower()
        value = value.strip()
        if not key or any(char.isspace() for char in name):
            raise HttpError(400, "Bad Request")
        if key in headers:
            headers[key] = f"{headers[key]}, {value}"
        else:
            headers[key] = value

    if version == "HTTP/1.1" and not headers.get("host"):
        raise HttpError(400, "Bad Request")

    try:
        path = urlsplit(target).path or "/"
    except ValueError as exc:
        raise HttpError(400, "Bad Request") from exc
    return HttpRequest(method, version, path, headers, peer)


async def read_http_request(
    reader: asyncio.StreamReader,
    peer: str,
    timeout: float,
    max_header_size: int,
) -> HttpRequest:
    """读取并解析一个 HTTP 请求头，限制最大 header，避免无限缓存。"""

    try:
        raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.LimitOverrunError as exc:
        raise HttpError(431, "Request Header Fields Too Large") from exc
    except asyncio.IncompleteReadError as exc:
        raise HttpError(400, "Bad Request") from exc
    except asyncio.TimeoutError as exc:
        raise HttpError(408, "Request Timeout") from exc

    if len(raw_headers) > max_header_size:
        raise HttpError(431, "Request Header Fields Too Large")

    return parse_headers(raw_headers[:-4], peer)


def http_reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        426: "Upgrade Required",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error",
    }.get(status, "Error")


async def send_http_response(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes = b"",
    headers: Optional[Dict[str, str]] = None,
) -> None:
    """发送短 HTTP 响应。普通 HTTP API 使用 Connection: close。"""

    hdrs = {
        "Server": "py-vless-asyncio",
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    if body and not any(name.lower() == "content-type" for name in hdrs):
        hdrs["Content-Type"] = "text/plain; charset=utf-8"

    head = [f"HTTP/1.1 {status} {http_reason(status)}"]
    head.extend(f"{name}: {value}" for name, value in hdrs.items())
    head.append("")
    head.append("")

    writer.write("\r\n".join(head).encode("iso-8859-1"))
    if body:
        writer.write(body)
    await writer.drain()


async def send_json_response(
    writer: asyncio.StreamWriter,
    status: int,
    payload: Dict[str, object],
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await send_http_response(
        writer,
        status,
        body,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


async def send_method_not_allowed(
    writer: asyncio.StreamWriter,
    allowed_methods: Iterable[str],
) -> None:
    allow = ", ".join(allowed_methods)
    await send_http_response(
        writer,
        405,
        b"Method Not Allowed",
        {"Allow": allow},
    )


async def send_options_response(
    writer: asyncio.StreamWriter,
    allowed_methods: Iterable[str],
) -> None:
    allow = ", ".join(allowed_methods)
    await send_http_response(writer, 200, b"", {"Allow": allow})


def split_header_tokens(value: str) -> Iterable[str]:
    """按 HTTP token 列表拆分，如 Connection / Sec-WebSocket-Protocol。"""

    for token in value.split(","):
        token = token.strip()
        if token:
            yield token


def header_has_token(headers: Dict[str, str], name: str, token: str) -> bool:
    expected = token.lower()
    return any(item.lower() == expected for item in split_header_tokens(headers.get(name, "")))


def validate_websocket_key(client_key: str) -> None:
    try:
        raw = base64.b64decode(client_key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise HttpError(400, "Invalid Sec-WebSocket-Key") from exc
    if len(raw) != 16:
        raise HttpError(400, "Invalid Sec-WebSocket-Key")


def websocket_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_ws_close_payload(payload: bytes) -> None:
    if len(payload) == 1:
        raise WebSocketError("invalid websocket close payload length")
    if len(payload) < 2:
        return

    code = struct.unpack("!H", payload[:2])[0]
    if code < 1000:
        raise WebSocketError(f"invalid websocket close code: {code}")
    if code in (1004, 1005, 1006, 1015) or 1016 <= code <= 2999 or code >= 5000:
        raise WebSocketError(f"invalid websocket close code: {code}")
    try:
        payload[2:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketError("invalid websocket close reason encoding") from exc


def encode_ws_close_payload(code: int, reason: str) -> bytes:
    if code < 1000 or code in (1004, 1005, 1006, 1015) or 1016 <= code <= 2999 or code >= 5000:
        raise ValueError(f"invalid websocket close code: {code}")

    reason_bytes = reason.encode("utf-8", errors="ignore")
    while len(reason_bytes) > 123:
        reason = reason[:-1]
        reason_bytes = reason.encode("utf-8", errors="ignore")
    return struct.pack("!H", code) + reason_bytes


def decode_ws_early_data(
    protocol_header: str,
    expected_uuid: uuid.UUID,
) -> Tuple[bytes, Optional[str]]:
    """
    解析 Xray/sing-box/Hiddify 常用的 WebSocket early data。

    这些客户端会把 VLESS 首包做 base64url 后塞进 Sec-WebSocket-Protocol。
    为避免误把普通 subprotocol 当成数据，这里只接受解码后 UUID 匹配的 token。
    """

    for token in split_header_tokens(protocol_header):
        padded = token + "=" * (-len(token) % 4)
        try:
            decoded = base64.b64decode(
                padded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error):
            continue
        if len(decoded) >= 18 and decoded[1:17] == expected_uuid.bytes:
            return decoded, token
    return b"", None


def parse_vless_target(
    view: memoryview,
    port_index: int,
    allow_zero_port: bool = False,
) -> Tuple[str, int, int]:
    """解析 VLESS 普通目标地址，返回 host、port 和 payload 起始位置。"""

    if len(view) < port_index + 3:
        raise VlessNeedMore("missing target")

    target_port = struct.unpack("!H", view[port_index:port_index + 2])[0]
    if target_port == 0 and not allow_zero_port:
        raise VlessError("invalid target port: 0")

    address_index = port_index + 2
    address_type = view[address_index]
    address_index += 1

    if address_type == ADDR_IPV4:
        if len(view) < address_index + 4:
            raise VlessNeedMore("invalid ipv4 address length")
        target_host = str(ipaddress.IPv4Address(bytes(view[address_index:address_index + 4])))
        address_index += 4

    elif address_type == ADDR_DOMAIN:
        if len(view) < address_index + 1:
            raise VlessNeedMore("missing domain length")
        domain_len = view[address_index]
        address_index += 1
        if domain_len == 0:
            raise VlessError("empty domain")
        if len(view) < address_index + domain_len:
            raise VlessNeedMore("invalid domain length")
        raw_domain = bytes(view[address_index:address_index + domain_len])
        try:
            target_host = raw_domain.decode("idna")
        except UnicodeError as exc:
            raise VlessError("invalid domain encoding") from exc
        address_index += domain_len

    elif address_type == ADDR_IPV6:
        if len(view) < address_index + 16:
            raise VlessNeedMore("invalid ipv6 address length")
        target_host = str(ipaddress.IPv6Address(bytes(view[address_index:address_index + 16])))
        address_index += 16

    else:
        raise VlessError(f"unsupported address type: {address_type}")

    return target_host, target_port, address_index


def parse_vless_packet(data: bytes | bytearray | memoryview, expected_uuid: uuid.UUID) -> VlessRequest:
    """
    解析 VLESS 首包。

    VLESS 请求格式：
    1 byte    version
    16 bytes  uuid
    1 byte    addon/opt length
    N bytes   addon/opt
    1 byte    command
    2 bytes   port, network byte order
    1 byte    address type
    variable  address
    variable  initial payload
    """

    view = memoryview(data)

    if len(view) < 18:
        raise VlessNeedMore(f"packet too short: {len(view)}")

    version = view[0]
    if version != 0:
        raise VlessError(f"unsupported version: {version}")

    incoming_uuid = bytes(view[1:17])
    if incoming_uuid != expected_uuid.bytes:
        got_uuid = str(uuid.UUID(bytes=incoming_uuid))
        raise VlessError(f"invalid uuid: {got_uuid}")

    opt_len = view[17]
    command_index = 18 + opt_len
    if len(view) < command_index + 1:
        raise VlessNeedMore("packet too short after addon")

    command = view[command_index]
    if command == CMD_UDP:
        raise VlessError("unsupported command: UDP is not implemented")
    if command not in (CMD_TCP, CMD_MUX):
        raise VlessError(f"unsupported command: {command}")

    port_index = command_index + 1

    if command == CMD_MUX:
        # Xray/sing-box 兼容路径：
        # 1. 一些实现会发送外层目标 mux.cool/v1.mux.cool 后再跟 Mux payload。
        # 2. v2rayNG 常见实现会在 command 后直接跟 Mux.Cool payload，
        #    此时继续按 port/address 解析会把 metadata 的 session id 当成地址类型。
        try:
            target_host, target_port, payload_index = parse_vless_target(
                view,
                port_index,
                allow_zero_port=True,
            )
            if target_host == "mux.cool" or target_host.endswith(".mux.cool"):
                return VlessRequest(
                    version=version,
                    command=command,
                    host=target_host,
                    port=target_port,
                    initial_payload=view[payload_index:],
                )
        except VlessError:
            pass

        return VlessRequest(
            version=version,
            command=command,
            host="mux.cool",
            port=0,
            initial_payload=view[port_index:],
        )

    target_host, target_port, payload_index = parse_vless_target(view, port_index)

    return VlessRequest(
        version=version,
        command=command,
        host=target_host,
        port=target_port,
        initial_payload=view[payload_index:],
    )


async def open_target_connection(
    host: str,
    port: int,
    timeout: float,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    建立到目标服务器的 TCP 连接。

    对域名不强制 IPv4，让系统 happy-eyeballs / DNS 策略自行选择地址族；
    这比固定 AF_INET 更兼容 IPv6-only 或优先 IPv6 的目标站点。
    """

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except Exception as exc:
        raise VlessError(f"connect target failed: {host}:{port}, {exc}") from exc

    sock = writer.get_extra_info("socket")
    if sock:
        with suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    logging.info(
        "connected target %s:%s peer=%s local=%s",
        host,
        port,
        writer.get_extra_info("peername"),
        writer.get_extra_info("sockname"),
    )
    return reader, writer


class WebSocketConnection:
    """最小 RFC 6455 服务端实现，带消息大小限制和发送背压。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_message_size: int,
    ):
        self.reader = reader
        self.writer = writer
        self.max_message_size = max_message_size
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def send_frame(self, opcode: int, payload: bytes | bytearray | memoryview = b"") -> None:
        """发送服务端 WebSocket 帧。服务端帧不能 mask。"""

        if opcode not in VALID_WS_OPCODES or opcode == OP_CONTINUATION:
            raise WebSocketError(f"unsupported server opcode: {opcode}")
        if opcode >= 0x08:
            if len(payload) > 125:
                raise WebSocketError("server control frame too large")
            if opcode == OP_CLOSE:
                validate_ws_close_payload(bytes(payload))

        if self._closed and opcode != OP_CLOSE:
            return

        length = len(payload)
        if length <= 125:
            header = struct.pack("!BB", 0x80 | opcode, length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, length)

        async with self._send_lock:
            self.writer.write(header)
            if payload:
                self.writer.write(payload)
            await self.writer.drain()

    async def send_binary(self, payload: bytes | bytearray | memoryview) -> None:
        await self.send_frame(OP_BINARY, payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            await self.send_frame(OP_CLOSE, encode_ws_close_payload(code, reason))

    async def read_frame(self) -> Tuple[int, bytes, bool]:
        """读取单个客户端帧。客户端帧必须 mask。"""

        head = await self.reader.readexactly(2)
        first, second = head[0], head[1]

        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if rsv:
            raise WebSocketError("reserved bits are not supported")
        if opcode not in VALID_WS_OPCODES:
            raise WebSocketError(f"unsupported opcode: {opcode}")
        if not masked:
            raise WebSocketError("client frame is not masked")

        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            if length < 126:
                raise WebSocketError("non-minimal websocket length encoding")
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length < 0x10000:
                raise WebSocketError("non-minimal websocket length encoding")
            if length & (1 << 63):
                raise WebSocketError("invalid websocket length")

        if opcode >= 0x08:
            if not fin:
                raise WebSocketError("fragmented control frame")
            if length > 125:
                raise WebSocketError("control frame too large")

        if length > self.max_message_size:
            raise WebSocketMessageTooLarge(f"websocket frame too large: {length}")

        mask = await self.reader.readexactly(4)
        payload = bytearray(await self.reader.readexactly(length))
        for index in range(length):
            payload[index] ^= mask[index & 3]

        data = bytes(payload)
        if opcode == OP_CLOSE:
            validate_ws_close_payload(data)

        return opcode, data, fin

    async def receive_message(self) -> Tuple[int, bytes]:
        """
        读取完整 WebSocket message。

        控制帧在这里处理：ping 自动 pong，close 自动回 close。
        数据帧支持 continuation，但会按 max_message_size 限制总大小。
        """

        message_opcode: Optional[int] = None
        chunks = []
        total = 0

        while True:
            opcode, payload, fin = await self.read_frame()

            if opcode == OP_CLOSE:
                self._closed = True
                await self.send_frame(OP_CLOSE, payload[:125])
                return OP_CLOSE, b""

            if opcode == OP_PING:
                await self.send_frame(OP_PONG, payload)
                continue

            if opcode == OP_PONG:
                continue

            if opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise WebSocketError("new data frame before continuation finished")
                message_opcode = opcode
            elif opcode == OP_CONTINUATION:
                if message_opcode is None:
                    raise WebSocketError("unexpected continuation frame")
            else:
                raise WebSocketError(f"unsupported opcode: {opcode}")

            total += len(payload)
            if total > self.max_message_size:
                raise WebSocketMessageTooLarge(f"websocket message too large: {total}")
            chunks.append(payload)

            if fin:
                data = chunks[0] if len(chunks) == 1 else b"".join(chunks)
                return message_opcode, data


async def read_first_vless_packet(
    ws: WebSocketConnection,
    early_data: bytes,
    expected_uuid: uuid.UUID,
    max_message_size: int,
) -> Tuple[VlessRequest, bytes | bytearray]:
    """
    获取并解析 VLESS 首包。

    early data 有可能只包含一部分 header，因此这里允许继续读取 WebSocket
    binary message 补齐，但总缓存受 max_message_size 限制。
    """

    if early_data:
        buffer = bytearray(early_data)
    else:
        buffer = bytearray()

    while True:
        if len(buffer) > max_message_size:
            raise VlessError(f"first packet too large: {len(buffer)}")

        if buffer:
            try:
                return parse_vless_packet(buffer, expected_uuid), buffer
            except VlessNeedMore:
                pass

        opcode, payload = await ws.receive_message()
        if opcode == OP_CLOSE:
            raise VlessError("client closed before VLESS request")
        if opcode != OP_BINARY:
            raise VlessError(f"first websocket message is not binary: opcode={opcode}")

        buffer.extend(payload)
        if len(buffer) > max_message_size:
            raise VlessError(f"first packet too large: {len(buffer)}")


async def ws_to_tcp(ws: WebSocketConnection, remote_writer: asyncio.StreamWriter) -> None:
    """WebSocket -> TCP。每次写入都 drain，依赖 asyncio 背压避免无限缓存。"""

    total = 0
    try:
        while True:
            opcode, payload = await ws.receive_message()
            if opcode == OP_CLOSE:
                break
            if opcode != OP_BINARY:
                raise WebSocketError(f"unexpected websocket opcode: {opcode}")
            if not payload:
                continue
            total += len(payload)
            remote_writer.write(payload)
            await remote_writer.drain()
    except asyncio.CancelledError:
        raise
    except (asyncio.IncompleteReadError, ConnectionError, WebSocketError) as exc:
        logging.debug("ws_to_tcp stopped: %s", exc)
    finally:
        logging.info("ws_to_tcp closed, bytes=%s", total)
        with suppress(Exception):
            remote_writer.close()
            await remote_writer.wait_closed()


async def tcp_to_ws(
    remote_reader: asyncio.StreamReader,
    ws: WebSocketConnection,
    tcp_read_size: int,
) -> None:
    """TCP -> WebSocket。send_frame 内部 drain，客户端慢时自然施加背压。"""

    total = 0
    try:
        while True:
            data = await remote_reader.read(tcp_read_size)
            if not data:
                break
            total += len(data)
            await ws.send_binary(data)
    except asyncio.CancelledError:
        raise
    except (ConnectionError, WebSocketError, asyncio.IncompleteReadError) as exc:
        logging.debug("tcp_to_ws stopped: %s", exc)
    finally:
        logging.info("tcp_to_ws closed, bytes=%s", total)
        await ws.close()


class AsyncByteStream:
    """
    把 WebSocket message 转成连续字节流。

    Mux.Cool 是字节流协议，不关心 WebSocket message 边界。这里使用有界缓冲，
    当解析速度跟不上客户端发送速度时，feed 会等待，形成背压。
    """

    def __init__(self, high_watermark: int):
        self.high_watermark = high_watermark
        self.low_watermark = max(1, high_watermark // 2)
        self.buffer = bytearray()
        self.closed = False
        self.condition = asyncio.Condition()

    async def feed(self, data: bytes | bytearray | memoryview) -> None:
        data_len = len(data)
        if data_len > self.high_watermark:
            raise WebSocketError(f"mux input chunk too large: {data_len}")

        view = memoryview(data)
        offset = 0
        while offset < data_len:
            async with self.condition:
                while len(self.buffer) >= self.high_watermark and not self.closed:
                    await self.condition.wait()
                if self.closed:
                    return

                available = self.high_watermark - len(self.buffer)
                chunk_len = min(available, data_len - offset)
                self.buffer.extend(view[offset:offset + chunk_len])
                offset += chunk_len
                self.condition.notify_all()

    async def readexactly(self, n: int) -> bytes:
        async with self.condition:
            while len(self.buffer) < n:
                if self.closed:
                    partial = bytes(self.buffer)
                    self.buffer.clear()
                    raise asyncio.IncompleteReadError(partial, n)
                await self.condition.wait()

            data = bytes(self.buffer[:n])
            del self.buffer[:n]
            if len(self.buffer) <= self.low_watermark:
                self.condition.notify_all()
            return data

    async def close(self) -> None:
        async with self.condition:
            self.closed = True
            self.condition.notify_all()


async def ws_to_mux_stream(ws: WebSocketConnection, stream: AsyncByteStream) -> None:
    """把后续 WebSocket binary message 喂给 Mux.Cool 字节流。"""

    try:
        while True:
            opcode, payload = await ws.receive_message()
            if opcode == OP_CLOSE:
                break
            if opcode != OP_BINARY:
                raise WebSocketError(f"unexpected websocket opcode in mux: {opcode}")
            if payload:
                await stream.feed(payload)
    except asyncio.CancelledError:
        raise
    except (asyncio.IncompleteReadError, ConnectionError, WebSocketError) as exc:
        logging.debug("ws_to_mux_stream stopped: %s", exc)
    finally:
        await stream.close()


def parse_mux_target(metadata: memoryview, index: int) -> Tuple[int, str, int, int]:
    """
    解析 Mux.Cool NEW 帧里的目标地址。

    Xray/v2ray 使用 network byte + PortThenAddress：
    1 byte network, 2 bytes port, 1 byte address type, variable address。
    """

    if len(metadata) < index + 3:
        raise VlessError("mux metadata missing target")

    network = metadata[index]
    index += 1
    port = struct.unpack("!H", metadata[index:index + 2])[0]
    index += 2

    if len(metadata) < index + 1:
        raise VlessError("mux metadata missing address type")
    address_type = metadata[index]
    index += 1

    if address_type == ADDR_IPV4:
        if len(metadata) < index + 4:
            raise VlessError("mux invalid ipv4 address length")
        host = str(ipaddress.IPv4Address(bytes(metadata[index:index + 4])))
        index += 4
    elif address_type == ADDR_DOMAIN:
        if len(metadata) < index + 1:
            raise VlessError("mux missing domain length")
        domain_len = metadata[index]
        index += 1
        if domain_len == 0:
            raise VlessError("mux empty domain")
        if len(metadata) < index + domain_len:
            raise VlessError("mux invalid domain length")
        try:
            host = bytes(metadata[index:index + domain_len]).decode("idna")
        except UnicodeError as exc:
            raise VlessError("mux invalid domain encoding") from exc
        index += domain_len
    elif address_type == ADDR_IPV6:
        if len(metadata) < index + 16:
            raise VlessError("mux invalid ipv6 address length")
        host = str(ipaddress.IPv6Address(bytes(metadata[index:index + 16])))
        index += 16
    else:
        raise VlessError(f"mux unsupported address type: {address_type}")

    if port == 0:
        raise VlessError("mux invalid target port: 0")

    return network, host, port, index


async def read_mux_frame(stream: AsyncByteStream) -> MuxFrame:
    """读取一个 Mux.Cool 帧。"""

    metadata_len = struct.unpack("!H", await stream.readexactly(2))[0]
    if metadata_len < 4:
        raise VlessError(f"mux metadata too short: {metadata_len}")
    if metadata_len > MUX_MAX_METADATA_SIZE:
        raise VlessError(f"mux metadata too large: {metadata_len}")

    metadata = memoryview(await stream.readexactly(metadata_len))
    session_id = struct.unpack("!H", metadata[0:2])[0]
    status = metadata[2]
    option = metadata[3]
    if option & ~MUX_KNOWN_OPTIONS:
        raise VlessError(f"mux unsupported option: {option}")

    network = 0
    host = ""
    port = 0

    if status == MUX_STATUS_NEW:
        # Xray may append source/local/global-id metadata after the target.
        # The proxy path only needs the target and should ignore extensions.
        network, host, port, _ = parse_mux_target(metadata, 4)
    elif status == MUX_STATUS_KEEP:
        # Xray can carry a UDP target address in KEEP metadata. Only parse it
        # when the extension is explicitly marked as a UDP target; other
        # metadata is ignored for compatibility.
        if len(metadata) > 4 and metadata[4] == MUX_NETWORK_UDP:
            network, host, port, _ = parse_mux_target(metadata, 4)
    elif status == MUX_STATUS_END:
        if len(metadata) != 4:
            raise VlessError(f"mux end metadata too long: {metadata_len}")
        if option & MUX_OPTION_DATA:
            raise VlessError("mux end has data option")
    elif status == MUX_STATUS_KEEPALIVE:
        if option:
            raise VlessError(f"mux keepalive has unsupported option: {option}")
        if len(metadata) != 4:
            raise VlessError(f"mux keepalive metadata too long: {metadata_len}")
    elif status not in (MUX_STATUS_KEEP, MUX_STATUS_END, MUX_STATUS_KEEPALIVE):
        raise VlessError(f"mux unsupported status: {status}")

    data = b""
    if option & MUX_OPTION_DATA:
        data_len = struct.unpack("!H", await stream.readexactly(2))[0]
        data = await stream.readexactly(data_len) if data_len else b""

    return MuxFrame(
        session_id=session_id,
        status=status,
        option=option,
        network=network,
        host=host,
        port=port,
        data=data,
    )


def build_mux_frame(
    session_id: int,
    status: int,
    option: int = 0,
    data: bytes | bytearray | memoryview = b"",
) -> bytes:
    """构造服务端返回给客户端的 Mux.Cool 帧。"""

    if not 0 <= session_id <= 0xFFFF:
        raise ValueError(f"invalid mux session id: {session_id}")
    if status not in (MUX_STATUS_NEW, MUX_STATUS_KEEP, MUX_STATUS_END, MUX_STATUS_KEEPALIVE):
        raise ValueError(f"unsupported mux status: {status}")
    if option & ~MUX_KNOWN_OPTIONS:
        raise ValueError(f"unsupported mux option: {option}")
    if status == MUX_STATUS_KEEPALIVE and option:
        raise ValueError("mux keepalive cannot carry options")
    if status == MUX_STATUS_END and option & MUX_OPTION_DATA:
        raise ValueError("mux end cannot carry data")
    if data and not option & MUX_OPTION_DATA:
        raise ValueError("mux data requires DATA option")
    if option & MUX_OPTION_DATA and len(data) > MUX_MAX_CHUNK_SIZE:
        raise ValueError(f"mux data too large: {len(data)}")

    metadata = struct.pack("!HBB", session_id, status, option)
    frame = bytearray(struct.pack("!H", len(metadata)))
    frame.extend(metadata)
    if option & MUX_OPTION_DATA:
        frame.extend(struct.pack("!H", len(data)))
        frame.extend(data)
    return bytes(frame)


async def send_mux_data(
    ws: WebSocketConnection,
    send_lock: asyncio.Lock,
    session_id: int,
    data: bytes,
) -> None:
    """发送 Mux 数据帧，按 uint16 chunk 长度拆包。"""

    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + MUX_MAX_CHUNK_SIZE]
        offset += len(chunk)
        frame = build_mux_frame(
            session_id,
            MUX_STATUS_KEEP,
            MUX_OPTION_DATA,
            chunk,
        )
        async with send_lock:
            await ws.send_binary(frame)


async def send_mux_end(
    ws: WebSocketConnection,
    send_lock: asyncio.Lock,
    session_id: int,
    error: bool = False,
) -> None:
    option = MUX_OPTION_ERROR if error else 0
    frame = build_mux_frame(session_id, MUX_STATUS_END, option)
    async with send_lock:
        await ws.send_binary(frame)


async def safe_send_mux_end(
    ws: WebSocketConnection,
    send_lock: asyncio.Lock,
    session_id: int,
    error: bool = False,
) -> None:
    try:
        await send_mux_end(ws, send_lock, session_id, error=error)
    except Exception as exc:
        logging.debug("send mux end failed session=%s error=%s", session_id, exc)


@dataclass
class MuxSession:
    session_id: int
    network: int
    host: str
    port: int
    task: asyncio.Task
    writer: Optional[asyncio.StreamWriter] = None
    transport: Optional[asyncio.DatagramTransport] = None


class UdpSessionProtocol(asyncio.DatagramProtocol):
    """connected UDP socket 的接收端，把 datagram 放进有界队列。"""

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            logging.warning("udp receive queue full, drop datagram from %s", addr)

    def error_received(self, exc: Exception) -> None:
        logging.debug("udp error: %s", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)


async def mux_tcp_to_ws(
    session_id: int,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
    ws: WebSocketConnection,
    send_lock: asyncio.Lock,
    sessions: Dict[int, MuxSession],
    tcp_read_size: int,
) -> None:
    """单个 Mux session 的 TCP -> WS 转发。"""

    total = 0
    try:
        while True:
            data = await remote_reader.read(tcp_read_size)
            if not data:
                break
            total += len(data)
            await send_mux_data(ws, send_lock, session_id, data)
        await safe_send_mux_end(ws, send_lock, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.debug("mux session %s tcp_to_ws stopped: %s", session_id, exc)
        await safe_send_mux_end(ws, send_lock, session_id, error=True)
    finally:
        sessions.pop(session_id, None)
        with suppress(Exception):
            remote_writer.close()
            await remote_writer.wait_closed()
        logging.info("mux session %s tcp_to_ws closed, bytes=%s", session_id, total)


async def mux_udp_to_ws(
    session_id: int,
    queue: asyncio.Queue,
    transport: asyncio.DatagramTransport,
    ws: WebSocketConnection,
    send_lock: asyncio.Lock,
    sessions: Dict[int, MuxSession],
    idle_timeout: float,
) -> None:
    """单个 Mux UDP session 的 UDP -> WS 转发。每个 Mux DATA 对应一个 UDP datagram。"""

    total = 0
    try:
        while True:
            data = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            if data is None:
                break
            total += len(data)
            await send_mux_data(ws, send_lock, session_id, data)
    except asyncio.TimeoutError:
        logging.debug("mux udp session %s idle timeout", session_id)
        await safe_send_mux_end(ws, send_lock, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.debug("mux udp session %s stopped: %s", session_id, exc)
        await safe_send_mux_end(ws, send_lock, session_id, error=True)
    finally:
        sessions.pop(session_id, None)
        transport.close()
        logging.info("mux udp session %s closed, bytes=%s", session_id, total)


async def close_mux_session(session: MuxSession) -> None:
    session.task.cancel()
    if session.writer:
        with suppress(Exception):
            session.writer.close()
            await session.writer.wait_closed()
    if session.transport:
        session.transport.close()
    await asyncio.gather(session.task, return_exceptions=True)


async def handle_mux_ws(
    ws: WebSocketConnection,
    initial_payload: bytes | bytearray | memoryview,
    config: ServerConfig,
) -> None:
    """
    处理 VLESS command=Mux 的 Mux.Cool 多路复用。

    这里实现主流客户端使用的 TCP 分流和 connected UDP session。
    """

    stream = AsyncByteStream(max(config.max_ws_message_size, MUX_MIN_STREAM_BUFFER_SIZE))
    send_lock = asyncio.Lock()
    sessions: Dict[int, MuxSession] = {}

    if initial_payload:
        await stream.feed(initial_payload)

    feeder = asyncio.create_task(ws_to_mux_stream(ws, stream))

    try:
        while True:
            frame = await read_mux_frame(stream)

            if frame.status == MUX_STATUS_KEEPALIVE:
                continue

            if frame.status == MUX_STATUS_NEW:
                if frame.option & MUX_OPTION_ERROR:
                    logging.warning(
                        "mux new frame has error option session=%s target=%s:%s",
                        frame.session_id,
                        frame.host,
                        frame.port,
                    )
                    await send_mux_end(ws, send_lock, frame.session_id, error=True)
                    continue

                if frame.session_id in sessions:
                    await close_mux_session(sessions.pop(frame.session_id))

                if frame.network == MUX_NETWORK_TCP:
                    try:
                        remote_reader, remote_writer = await open_target_connection(
                            frame.host,
                            frame.port,
                            config.connect_timeout,
                        )
                    except VlessError as exc:
                        logging.warning(
                            "mux connect failed session=%s target=%s:%s error=%s",
                            frame.session_id,
                            frame.host,
                            frame.port,
                            exc,
                        )
                        await send_mux_end(ws, send_lock, frame.session_id, error=True)
                        continue

                    task = asyncio.create_task(
                        mux_tcp_to_ws(
                            frame.session_id,
                            remote_reader,
                            remote_writer,
                            ws,
                            send_lock,
                            sessions,
                            config.tcp_read_size,
                        )
                    )
                    sessions[frame.session_id] = MuxSession(
                        session_id=frame.session_id,
                        network=frame.network,
                        host=frame.host,
                        port=frame.port,
                        task=task,
                        writer=remote_writer,
                    )

                    logging.info(
                        "mux tcp new session=%s target=%s:%s initial_payload=%s",
                        frame.session_id,
                        frame.host,
                        frame.port,
                        len(frame.data),
                    )

                    if frame.data:
                        remote_writer.write(frame.data)
                        await remote_writer.drain()

                    continue

                if frame.network == MUX_NETWORK_UDP:
                    queue: asyncio.Queue = asyncio.Queue(maxsize=config.udp_queue_size)
                    protocol = UdpSessionProtocol(queue)
                    try:
                        transport, _ = await asyncio.wait_for(
                            asyncio.get_running_loop().create_datagram_endpoint(
                                lambda: protocol,
                                remote_addr=(frame.host, frame.port),
                            ),
                            timeout=config.connect_timeout,
                        )
                    except Exception as exc:
                        logging.warning(
                            "mux udp connect failed session=%s target=%s:%s error=%s",
                            frame.session_id,
                            frame.host,
                            frame.port,
                            exc,
                        )
                        await send_mux_end(ws, send_lock, frame.session_id, error=True)
                        continue

                    udp_transport = transport
                    task = asyncio.create_task(
                        mux_udp_to_ws(
                            frame.session_id,
                            queue,
                            udp_transport,
                            ws,
                            send_lock,
                            sessions,
                            config.udp_idle_timeout,
                        )
                    )
                    sessions[frame.session_id] = MuxSession(
                        session_id=frame.session_id,
                        network=frame.network,
                        host=frame.host,
                        port=frame.port,
                        task=task,
                        transport=udp_transport,
                    )

                    logging.info(
                        "mux udp new session=%s target=%s:%s initial_payload=%s",
                        frame.session_id,
                        frame.host,
                        frame.port,
                        len(frame.data),
                    )

                    if frame.data:
                        udp_transport.sendto(frame.data)

                    continue

                logging.warning(
                    "mux unsupported network session=%s network=%s",
                    frame.session_id,
                    frame.network,
                )
                await send_mux_end(ws, send_lock, frame.session_id, error=True)

            elif frame.status == MUX_STATUS_KEEP:
                session = sessions.get(frame.session_id)
                if not session:
                    await send_mux_end(ws, send_lock, frame.session_id, error=True)
                    continue
                if frame.option & MUX_OPTION_ERROR:
                    logging.debug("mux peer reported error session=%s", frame.session_id)
                    await send_mux_end(ws, send_lock, frame.session_id, error=True)
                    sessions.pop(frame.session_id, None)
                    await close_mux_session(session)
                    continue
                if frame.host and (
                    frame.network != session.network
                    or frame.host != session.host
                    or frame.port != session.port
                ):
                    logging.warning(
                        "mux target change is not supported session=%s old=%s:%s new=%s:%s",
                        frame.session_id,
                        session.host,
                        session.port,
                        frame.host,
                        frame.port,
                    )
                    await send_mux_end(ws, send_lock, frame.session_id, error=True)
                    sessions.pop(frame.session_id, None)
                    await close_mux_session(session)
                    continue
                if frame.data:
                    if session.network == MUX_NETWORK_TCP and session.writer:
                        session.writer.write(frame.data)
                        await session.writer.drain()
                    elif session.network == MUX_NETWORK_UDP and session.transport:
                        session.transport.sendto(frame.data)

            elif frame.status == MUX_STATUS_END:
                session = sessions.pop(frame.session_id, None)
                if session:
                    await close_mux_session(session)

    except asyncio.CancelledError:
        raise
    except asyncio.IncompleteReadError:
        logging.debug("mux stream closed")
    except VlessError as exc:
        logging.warning("mux protocol error: %s", exc)
    except (ConnectionError, WebSocketError) as exc:
        logging.debug("mux stopped: %s", exc)
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        pending_sessions = list(sessions.values())
        sessions.clear()
        await asyncio.gather(
            *(close_mux_session(session) for session in pending_sessions),
            return_exceptions=True,
        )


async def handle_time(request: HttpRequest, writer: asyncio.StreamWriter) -> None:
    """保留原 /time API。"""

    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)

    logging.info(
        "time request from %s ua=%s",
        request.peer,
        request.headers.get("user-agent", ""),
    )

    await send_json_response(
        writer,
        200,
        {
            "ok": True,
            "local_time": now_local.isoformat(),
            "utc_time": now_utc.isoformat(),
            "timestamp": int(now_local.timestamp()),
            "timezone": str(now_local.tzinfo),
        },
    )


async def handle_cw(request: HttpRequest, writer: asyncio.StreamWriter) -> None:
    """保留原 /cw API。"""

    logging.info(
        "cw request from %s ua=%s",
        request.peer,
        request.headers.get("user-agent", ""),
    )

    await send_json_response(
        writer,
        200,
        {
            "result": "success",
            "errorMsg": None,
            "data": {"status": 1},
        },
    )


async def send_websocket_handshake(
    request: HttpRequest,
    writer: asyncio.StreamWriter,
    selected_protocol: Optional[str],
) -> None:
    client_key = request.headers.get("sec-websocket-key", "")
    if not client_key:
        raise HttpError(400, "Missing Sec-WebSocket-Key")
    validate_websocket_key(client_key)

    headers = {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": websocket_accept_key(client_key),
    }
    if selected_protocol:
        # early data token 也是客户端声明的 subprotocol，回显可兼容严格客户端。
        headers["Sec-WebSocket-Protocol"] = selected_protocol

    head = ["HTTP/1.1 101 Switching Protocols"]
    head.extend(f"{name}: {value}" for name, value in headers.items())
    head.append("")
    head.append("")
    writer.write("\r\n".join(head).encode("iso-8859-1"))
    await writer.drain()


async def reject_bad_websocket_request(
    writer: asyncio.StreamWriter,
    message: str = "Bad Request",
    headers: Optional[Dict[str, str]] = None,
) -> None:
    await send_http_response(writer, 400, message.encode("utf-8"), headers)


async def handle_vless_ws(
    request: HttpRequest,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: ServerConfig,
) -> None:
    """处理 VLESS over WebSocket 连接。"""

    if request.method == "OPTIONS":
        await send_options_response(writer, ("GET", "OPTIONS"))
        return
    if request.method != "GET":
        await send_method_not_allowed(writer, ("GET",))
        return
    if request.version != "HTTP/1.1":
        await reject_bad_websocket_request(writer)
        return
    if not header_has_token(request.headers, "upgrade", "websocket"):
        await send_http_response(
            writer,
            426,
            b"Upgrade Required",
            {"Upgrade": "websocket"},
        )
        return
    if not header_has_token(request.headers, "connection", "upgrade"):
        await reject_bad_websocket_request(writer)
        return
    if request.headers.get("sec-websocket-version") != "13":
        await reject_bad_websocket_request(
            writer,
            "Bad Request",
            {"Sec-WebSocket-Version": "13"},
        )
        return

    client_key = request.headers.get("sec-websocket-key", "")
    if not client_key:
        await reject_bad_websocket_request(writer, "Missing Sec-WebSocket-Key")
        return
    try:
        validate_websocket_key(client_key)
    except HttpError as exc:
        await reject_bad_websocket_request(writer, exc.message)
        return

    early_data, selected_protocol = decode_ws_early_data(
        request.headers.get("sec-websocket-protocol", ""),
        config.user_uuid,
    )

    await send_websocket_handshake(request, writer, selected_protocol)

    ws = WebSocketConnection(reader, writer, config.max_ws_message_size)
    remote_writer: Optional[asyncio.StreamWriter] = None
    peer = request.peer

    try:
        vless_request, first_packet_buffer = await read_first_vless_packet(
            ws,
            early_data,
            config.user_uuid,
            config.max_ws_message_size,
        )

        logging.info(
            "client %s command=%s -> %s:%s first_packet=%s initial_payload=%s early_data=%s",
            peer,
            vless_request.command,
            vless_request.host,
            vless_request.port,
            len(first_packet_buffer),
            len(vless_request.initial_payload),
            len(early_data),
        )

        # VLESS 响应头必须先发给客户端：version + addon length 0。
        await ws.send_binary(bytes((vless_request.version, 0x00)))

        if vless_request.command == CMD_MUX:
            await handle_mux_ws(ws, vless_request.initial_payload, config)
            return

        remote_reader, remote_writer = await open_target_connection(
            vless_request.host,
            vless_request.port,
            config.connect_timeout,
        )

        if vless_request.initial_payload:
            remote_writer.write(vless_request.initial_payload)
            await remote_writer.drain()

        task_ws_to_tcp = asyncio.create_task(ws_to_tcp(ws, remote_writer))
        task_tcp_to_ws = asyncio.create_task(
            tcp_to_ws(remote_reader, ws, config.tcp_read_size)
        )

        done, pending = await asyncio.wait(
            {task_ws_to_tcp, task_tcp_to_ws},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        await asyncio.gather(*done, return_exceptions=True)
        await asyncio.gather(*pending, return_exceptions=True)

    except VlessError as exc:
        logging.warning("vless error from %s: %s", peer, exc)
        await ws.close(1008, "vless error")
    except WebSocketMessageTooLarge as exc:
        logging.warning("websocket too large from %s: %s", peer, exc)
        await ws.close(1009, "message too large")
    except (asyncio.IncompleteReadError, ConnectionError, WebSocketError) as exc:
        logging.debug("websocket closed from %s: %s", peer, exc)
    except Exception as exc:
        logging.exception("unexpected error from %s: %s", peer, exc)
        await ws.close(1011, "server error")
    finally:
        if remote_writer:
            with suppress(Exception):
                remote_writer.close()
                await remote_writer.wait_closed()


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: ServerConfig,
) -> None:
    peername = writer.get_extra_info("peername")
    peer = f"{peername[0]}:{peername[1]}" if isinstance(peername, tuple) else str(peername)

    try:
        request = await read_http_request(
            reader,
            peer,
            config.http_timeout,
            config.max_header_size,
        )

        logging.info(
            "request from %s method=%s path=%s upgrade=%s ua=%s",
            peer,
            request.method,
            request.path,
            request.headers.get("upgrade", ""),
            request.headers.get("user-agent", ""),
        )

        if request.path == "/time":
            if request.method == "OPTIONS":
                await send_options_response(writer, ("GET", "OPTIONS"))
                return
            if request.method != "GET":
                await send_method_not_allowed(writer, ("GET",))
                return
            await handle_time(request, writer)
            return

        if request.path == "" or request.path == "/" or request.path == "/health":
            if request.method == "OPTIONS":
                await send_options_response(writer, ("GET", "POST", "OPTIONS"))
                return
            if request.method not in ("GET", "POST"):
                await send_method_not_allowed(writer, ("GET", "POST"))
                return
            await handle_cw(request, writer)
            return

        if normalize_path(request.path) != normalize_path(config.path):
            await send_http_response(writer, 404, b"Not Found")
            return

        await handle_vless_ws(request, reader, writer, config)

    except HttpError as exc:
        logging.debug("http error from %s: %s", peer, exc)
        with suppress(Exception):
            await send_http_response(writer, exc.status, exc.message.encode("utf-8"))
    except Exception as exc:
        logging.exception("client error from %s: %s", peer, exc)
        with suppress(Exception):
            await send_http_response(writer, 500, b"Internal Server Error")
    finally:
        with suppress(Exception):
            writer.close()
            await writer.wait_closed()


def create_ssl_context(cert_file: Optional[str], key_file: Optional[str]) -> Optional[ssl.SSLContext]:
    if not cert_file and not key_file:
        return None
    if not cert_file or not key_file:
        raise SystemExit("--cert and --key must be used together")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def require_positive_finite(value: float, option_name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{option_name} must be a finite number > 0")


def format_url_host(host: str) -> str:
    if host in ("0.0.0.0", "::"):
        return "localhost"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        return f"[{host}]"
    return host


async def run_server(config: ServerConfig) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, config),
        host=config.host,
        port=config.port,
        ssl=config.ssl_context,
        limit=config.max_header_size,
        start_serving=True,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    logging.info("====================================")
    logging.info("VLESS WS server started")
    logging.info("listen: %s", sockets)
    logging.info("uuid: %s", config.user_uuid)
    logging.info("path: %s", config.path)
    scheme = "https" if config.ssl_context else "http"
    logging.info(
        "time endpoint: %s://%s:%s/time",
        scheme,
        format_url_host(config.host),
        config.port,
    )
    logging.info("tls: %s", "enabled" if config.ssl_context else "disabled")
    logging.info("====================================")

    async with server:
        await server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Python asyncio VLESS over WebSocket Server"
    )

    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"listen host, default: {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"listen port, default: {DEFAULT_PORT}",
    )
    parser.add_argument(
        "--uuid",
        default=DEFAULT_UUID,
        help=f"VLESS UUID, default: {DEFAULT_UUID}",
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help=f"WebSocket path, default: {DEFAULT_PATH}",
    )
    parser.add_argument(
        "--cert",
        default=None,
        help="TLS certificate file, default: disabled",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="TLS private key file, default: disabled",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"target connect timeout seconds, default: {DEFAULT_CONNECT_TIMEOUT}",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT,
        help=f"HTTP header timeout seconds, default: {DEFAULT_HTTP_TIMEOUT}",
    )
    parser.add_argument(
        "--max-header-size",
        type=int,
        default=DEFAULT_MAX_HEADER_SIZE,
        help=f"max HTTP header size bytes, default: {DEFAULT_MAX_HEADER_SIZE}",
    )
    parser.add_argument(
        "--max-ws-message-size",
        type=int,
        default=DEFAULT_MAX_WS_MESSAGE_SIZE,
        help=(
            "max WebSocket message size bytes, "
            f"min: {MUX_MIN_STREAM_BUFFER_SIZE}, default: {DEFAULT_MAX_WS_MESSAGE_SIZE}"
        ),
    )
    parser.add_argument(
        "--tcp-read-size",
        type=int,
        default=DEFAULT_TCP_READ_SIZE,
        help=f"TCP read chunk size bytes, default: {DEFAULT_TCP_READ_SIZE}",
    )
    parser.add_argument(
        "--udp-idle-timeout",
        type=float,
        default=DEFAULT_UDP_IDLE_TIMEOUT,
        help=f"Mux UDP idle timeout seconds, default: {DEFAULT_UDP_IDLE_TIMEOUT}",
    )
    parser.add_argument(
        "--udp-queue-size",
        type=int,
        default=DEFAULT_UDP_QUEUE_SIZE,
        help=f"Mux UDP receive queue size, default: {DEFAULT_UDP_QUEUE_SIZE}",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=DEFAULT_LOG_LEVEL,
        help=f"log level, default: {DEFAULT_LOG_LEVEL}",
    )

    args = parser.parse_args()

    if not args.path or not args.path.startswith("/"):
        raise SystemExit("--path must start with /")
    if "?" in args.path or "#" in args.path:
        raise SystemExit("--path must not contain query or fragment")
    args.path = normalize_path(args.path)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    require_positive_finite(args.connect_timeout, "--connect-timeout")
    require_positive_finite(args.http_timeout, "--http-timeout")
    if args.max_header_size < 4096:
        raise SystemExit("--max-header-size must be >= 4096")
    if args.max_ws_message_size < MUX_MIN_STREAM_BUFFER_SIZE:
        raise SystemExit(f"--max-ws-message-size must be >= {MUX_MIN_STREAM_BUFFER_SIZE}")
    if args.tcp_read_size < 1024:
        raise SystemExit("--tcp-read-size must be >= 1024")
    require_positive_finite(args.udp_idle_timeout, "--udp-idle-timeout")
    if args.udp_queue_size < 1:
        raise SystemExit("--udp-queue-size must be >= 1")

    return args


def main() -> None:
    args = parse_args()
    logging.getLogger().setLevel(args.log_level)

    try:
        user_uuid = uuid.UUID(args.uuid)
    except Exception as exc:
        raise SystemExit("invalid uuid") from exc

    config = ServerConfig(
        host=args.host,
        port=args.port,
        user_uuid=user_uuid,
        path=args.path,
        connect_timeout=args.connect_timeout,
        http_timeout=args.http_timeout,
        max_header_size=args.max_header_size,
        max_ws_message_size=args.max_ws_message_size,
        tcp_read_size=args.tcp_read_size,
        udp_idle_timeout=args.udp_idle_timeout,
        udp_queue_size=args.udp_queue_size,
        ssl_context=create_ssl_context(args.cert, args.key),
    )

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        logging.info("server stopped")


if __name__ == "__main__":
    main()
