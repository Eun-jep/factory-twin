"""시험 도구들이 함께 쓰는 측정 도우미 (표준 라이브러리만 사용).

- percentile / gap_stats   숫자 통계 (수집 간격의 분포를 보기 위함)
- ProcessMeter             다른 프로세스의 CPU 시간과 메모리를 잰다 (윈도우 전용, 그 밖에는 None)
- parse_frames / ws_collect  수집 서버의 WebSocket 방송을 받아 개수와 크기를 센다
- free_port                비어 있는 포트 하나를 찾는다
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import struct
import sys
import time


# ---- 통계 ----------------------------------------------------------------------
def percentile(values: list[float], p: float) -> float | None:
    """p(0~100) 백분위수. 값 사이는 직선으로 이어서 계산한다. 값이 없으면 None."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lower = int(k)
    upper = min(lower + 1, len(s) - 1)
    return s[lower] + (s[upper] - s[lower]) * (k - lower)


def gap_stats(timestamps: list[float]) -> dict | None:
    """연속한 시각 사이의 간격(초) 통계. 시각이 2개 미만이면 None."""
    ts = sorted(timestamps)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    if not gaps:
        return None
    return {
        "count": len(gaps), "mean": sum(gaps) / len(gaps),
        "p50": percentile(gaps, 50), "p95": percentile(gaps, 95), "p99": percentile(gaps, 99),
        "max": max(gaps),
    }


# ---- 프로세스 CPU·메모리 (윈도우) ------------------------------------------------
class ProcessMeter:
    """subprocess.Popen으로 띄운 프로세스의 CPU 시간(초)과 메모리(MB)를 읽는다.
    윈도우의 GetProcessTimes / GetProcessMemoryInfo를 ctypes로 부른다. 윈도우가 아니면 값은 None."""

    def __init__(self, popen) -> None:
        self.supported = sys.platform == "win32" and hasattr(popen, "_handle")
        self._handle = None
        if not self.supported:
            return
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

        class COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        self._FILETIME, self._COUNTERS, self._wintypes = FILETIME, COUNTERS, wintypes
        self._handle = wintypes.HANDLE(int(popen._handle))
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(COUNTERS), wintypes.DWORD]
        self._k32, self._psapi = k32, psapi

    def cpu_seconds(self) -> float | None:
        """지금까지 쓴 CPU 시간(사용자+커널, 초). 코어 여러 개를 쓰면 실제 시간보다 클 수 있다."""
        if not self.supported:
            return None
        c, e, k, u = (self._FILETIME() for _ in range(4))
        if not self._k32.GetProcessTimes(self._handle, c, e, k, u):
            return None
        to_s = lambda ft: ((ft.hi << 32) | ft.lo) / 1e7
        return to_s(k) + to_s(u)

    def memory_mb(self) -> float | None:
        """지금 쓰는 메모리(작업 집합, MB)."""
        if not self.supported:
            return None
        counters = self._COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(self._handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / (1024 * 1024)


# ---- 포트 ----------------------------------------------------------------------
def free_port() -> int:
    """운영체제가 지금 비어 있다고 알려 주는 포트 하나."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- WebSocket 방송 받기 ----------------------------------------------------------
def parse_frames(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """서버가 보낸 WebSocket 프레임을 (opcode, 내용) 목록으로 풀고, 아직 덜 온 나머지 바이트를 돌려준다.
    서버 -> 클라이언트 프레임은 마스크가 없다 (RFC 6455)."""
    frames = []
    pos = 0
    while True:
        if len(buffer) - pos < 2:
            break
        opcode = buffer[pos] & 0x0F
        length = buffer[pos + 1] & 0x7F
        header = 2
        if length == 126:
            if len(buffer) - pos < 4:
                break
            length = struct.unpack(">H", buffer[pos + 2:pos + 4])[0]
            header = 4
        elif length == 127:
            if len(buffer) - pos < 10:
                break
            length = struct.unpack(">Q", buffer[pos + 2:pos + 10])[0]
            header = 10
        if len(buffer) - pos < header + length:
            break
        frames.append((opcode, buffer[pos + header:pos + header + length]))
        pos += header + length
    return frames, buffer[pos:]


def ws_collect(host: str, port: int, seconds: float) -> dict:
    """수집 서버의 /ws에 접속해 seconds초 동안 받은 메시지를 센다.
    반환: messages, bytes, by_type(메시지 종류별 개수), state_sizes(state 메시지 크기들), state_times(받은 시각들)."""
    result = {"messages": 0, "bytes": 0, "by_type": {}, "state_sizes": [], "state_times": [], "error": None}
    try:
        sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("핸드셰이크 중 연결이 닫혔습니다")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("WebSocket 전환 실패: " + head.split(b"\r\n")[0].decode(errors="replace"))
        buffer = rest
        deadline = time.monotonic() + seconds
        sock.settimeout(0.5)
        while time.monotonic() < deadline:
            frames, buffer = parse_frames(buffer)
            for opcode, payload in frames:
                if opcode != 0x1:
                    continue
                result["messages"] += 1
                result["bytes"] += len(payload)
                kind = json.loads(payload).get("type", "?")
                result["by_type"][kind] = result["by_type"].get(kind, 0) + 1
                if kind == "state":
                    result["state_sizes"].append(len(payload))
                    result["state_times"].append(time.time())
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        sock.close()
    except Exception as e:            # 측정 도구는 실패해도 원인을 결과에 남기고 계속한다
        result["error"] = f"{type(e).__name__}: {e}"
    return result
