
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

LOG = logging.getLogger("cbus_bridge.cni")


@dataclass
class CniFailoverState:
    primary_fail_count: int = 0
    primary_fail_since_mono: Optional[float] = None
    failed_over: bool = False
    failed_over_at_epoch: Optional[int] = None


class CniConnection:
    """
    CNI TCP connection wrapper for a single endpoint. Legacy failover settings are ignored.

    Key RX behavior:
    - Read in chunks into a buffer.
    - Return CR/LF delimited lines for normal traffic.
    - ALSO detect ACK tokens (e.g. 'g.' or '!') anywhere in the buffer and return them immediately,
      even if not newline-terminated.

    This makes ACK handling reliable under load and independent of line framing.
    """

    def __init__(
        self,
        host: str,
        primary_port: int,
        secondary_port: Optional[int] = None,
        timeout: float = 10.0,
        failover_after_seconds: int = 60,
        failover_after_attempts: int = 10,
        rx_timeout_s: float = 0.2,  # important: small timeout so ACKs surface promptly
    ):
        self.host = host
        self.primary_port = primary_port
        # Accept legacy callers/settings for a transition release, but disable failover.
        if secondary_port:
            LOG.warning("Secondary CNI port is obsolete and ignored; using port %d only", primary_port)
        self.secondary_port = None
        self.timeout = timeout

        self.failover_after_seconds = max(int(failover_after_seconds), 0)
        self.failover_after_attempts = max(int(failover_after_attempts), 0)

        self.rx_timeout_s = float(rx_timeout_s)

        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()  # protects self.sock

        self._active_port: Optional[int] = None
        self._fo = CniFailoverState()

        self._rxbuf = bytearray()

    # ----------------------------
    # Public status helpers
    # ----------------------------
    @property
    def active_endpoint(self) -> Tuple[str, int]:
        port = self._active_port if self._active_port is not None else self.primary_port
        return (self.host, port)

    @property
    def active_port(self) -> int:
        return self.active_endpoint[1]

    def failover_status(self) -> dict:
        since_epoch = None
        if self._fo.primary_fail_since_mono is not None:
            elapsed = time.monotonic() - self._fo.primary_fail_since_mono
            since_epoch = int(time.time() - elapsed)

        return {
            "cni_primary_port": self.primary_port,
            "cni_secondary_port": self.secondary_port,
            "cni_active_port": self._active_port,
            "cni_failed_over": self._fo.failed_over,
            "cni_failed_over_at": self._fo.failed_over_at_epoch,
            "cni_primary_fail_count": self._fo.primary_fail_count,
            "cni_primary_fail_since": since_epoch,
            "cni_failover_after_seconds": self.failover_after_seconds,
            "cni_failover_after_attempts": self.failover_after_attempts,
        }

    # ----------------------------
    # Internal socket management
    # ----------------------------
    def _drop_socket_locked(self) -> None:
        s = self.sock
        self.sock = None
        self._active_port = None
        self._rxbuf.clear()
        if s:
            try:
                s.close()
            except Exception:
                pass

    def close(self) -> None:
        with self.lock:
            self._drop_socket_locked()

    def _connect_to(self, host: str, port: int) -> socket.socket:
        LOG.info("Connecting to CNI %s:%d", host, port)
        s = socket.create_connection((host, port), timeout=self.timeout)
        s.settimeout(self.rx_timeout_s)
        return s

    def connect(self) -> None:
        """Reconnect to the single configured CNI endpoint; never try a second port."""
        try:
            sock = self._connect_to(self.host, self.primary_port)
        except Exception:
            self._fo.primary_fail_count += 1
            if self._fo.primary_fail_since_mono is None:
                self._fo.primary_fail_since_mono = time.monotonic()
            raise
        with self.lock:
            self._drop_socket_locked()
            self.sock = sock
            self._active_port = self.primary_port
        self._fo.primary_fail_count = 0
        self._fo.primary_fail_since_mono = None
        LOG.info("Connected to CNI %s:%d", self.host, self.primary_port)

    # ----------------------------
    # Send/recv primitives
    # ----------------------------
    def send_line(self, line: str) -> None:
        data = (line.rstrip("\r\n") + "\r\n").encode("ascii", errors="strict")
        with self.lock:
            if not self.sock:
                raise ConnectionError("CNI socket not connected")
            try:
                self.sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                self._drop_socket_locked()
                raise ConnectionError(f"CNI send failed: {e!r}")

    def send_hex_frame(self, hex_payload: str) -> None:
        self.send_line("\\" + hex_payload)

    def _extract_ack_from_buffer(self) -> Optional[bytes]:
        """
        Look for ACK tokens anywhere in the buffer:
          - '!' (single byte)
          - '<tag><code>' where tag is g..z and code is . # $ %
        If found, remove it from buffer and return it as a standalone "line".
        """
        if not self._rxbuf:
            return None

        # 1) '!' anywhere
        idx = self._rxbuf.find(b"!")
        if idx != -1:
            del self._rxbuf[idx : idx + 1]
            return b"!"

        # 2) tag ack pattern anywhere
        for i in range(0, len(self._rxbuf) - 1):
            a = self._rxbuf[i]
            b = self._rxbuf[i + 1]
            if 0x67 <= a <= 0x7A and b in (0x2E, 0x23, 0x24, 0x25):  # g..z and . # $ %
                token = bytes(self._rxbuf[i : i + 2])
                del self._rxbuf[i : i + 2]
                return token

        return None

    def recv_line_bytes(self) -> bytes | None:
        """
        Returns:
          - a CR/LF-delimited line (without CR/LF), OR
          - an ACK token (b'g.' / b'!' etc) extracted from the stream,
          - None on timeout when no complete line or ACK token is available.
        """
        with self.lock:
            sock = self.sock
        if not sock:
            raise ConnectionError("CNI socket not connected")

        while True:
            # ACK tokens take priority (they may not be newline-terminated)
            ack = self._extract_ack_from_buffer()
            if ack is not None:
                return ack

            # Normal line-delimited traffic
            nl = self._rxbuf.find(b"\n")
            if nl != -1:
                line = bytes(self._rxbuf[:nl])
                del self._rxbuf[: nl + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line

            # Need more data
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                return None
            except (ConnectionResetError, OSError) as e:
                with self.lock:
                    if self.sock is sock:
                        self._drop_socket_locked()
                raise ConnectionError(f"CNI recv failed: {e!r}")

            if chunk == b"":
                with self.lock:
                    if self.sock is sock:
                        self._drop_socket_locked()
                raise ConnectionError("CNI socket closed")

            self._rxbuf += chunk
            if len(self._rxbuf) > 262144:
                self._rxbuf = self._rxbuf[-65536:]

