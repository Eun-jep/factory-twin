"""가상 설비 (Modbus TCP 서버 시뮬레이터)

로봇 팔 1대(robot_sim.RobotSim)를 하나의 가상 PLC로 노출한다.
표준 라이브러리만 사용한다. 로봇마다 프로세스를 하나씩, 포트를 다르게 띄운다.

    python simulator/modbus_sim.py --name ARM-01 --port 5021

지원 기능: 0x03 (Read Holding Registers)
그 외 기능 코드는 예외 응답 0x01(ILLEGAL FUNCTION), 범위 밖 주소는 0x02(ILLEGAL DATA ADDRESS)

통신 장애 주입 (수집 서버의 재접속·타임아웃·오류 처리를 시험하기 위함)
  --delay-ms N       응답을 N ms 늦춘다
  --drop-every N     N번째 요청마다 응답 없이 연결을 끊는다
  --garbage-every N  N번째 요청마다 깨진 응답(프로토콜 ID가 틀린 헤더)을 보낸다
"""
from __future__ import annotations

import argparse
import socket
import struct
import threading
import time

from robot_sim import PHASE_NAMES, REGISTER_COUNT, RobotSim

TICK_SECONDS = 0.2   # 시뮬레이터 내부 시계 (순서도: 0.2초마다 갱신)


class Faults:
    """통신 장애 설정."""

    def __init__(self, delay_ms: int = 0, drop_every: int = 0, garbage_every: int = 0) -> None:
        self.delay_ms = delay_ms
        self.drop_every = drop_every
        self.garbage_every = garbage_every


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data += chunk
    return data


def build_response(tid: int, unit: int, pdu: bytes) -> bytes:
    # MBAP: transaction id(2) protocol id(2)=0 length(2) unit id(1) + PDU
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def handle_pdu(pdu: bytes, regs: list[int]) -> bytes:
    func = pdu[0]
    if func == 0x03 and len(pdu) == 5:
        addr, qty = struct.unpack(">HH", pdu[1:5])
        if qty < 1 or qty > 125 or addr + qty > len(regs):
            return bytes([func | 0x80, 0x02])
        values = regs[addr:addr + qty]
        return bytes([func, qty * 2]) + b"".join(struct.pack(">H", v) for v in values)
    return bytes([func | 0x80, 0x01])


def port_in_use(host: str, port: int) -> bool:
    """누군가 이미 그 포트에서 듣고 있는가.

    윈도우에서는 SO_REUSEADDR를 켜면 이미 쓰는 포트에도 두 번째 서버가 오류 없이 붙을 수 있다.
    그러면 수집기가 어느 쪽 로봇에 접속할지 알 수 없게 되므로, 시작하기 전에 직접 확인한다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


class SimServer:
    """로봇 모델 + Modbus TCP 서버."""

    def __init__(self, sim: RobotSim, host: str, port: int, faults: Faults | None = None,
                 verbose: bool = False) -> None:
        self.sim = sim
        self.host = host
        self.port = port
        self.faults = faults or Faults()
        self.verbose = verbose
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._requests = 0
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._conns: set[socket.socket] = set()

    def start(self) -> None:
        if self.port != 0 and port_in_use(self.host, self.port):
            raise OSError(f"{self.host}:{self.port} 포트를 이미 다른 프로그램이 쓰고 있습니다. "
                          f"먼저 그 프로그램을 종료하세요.")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen()
        server.settimeout(0.5)
        self.port = server.getsockname()[1]   # port=0이면 실제로 배정된 포트
        self._server = server
        for target in (self._tick_loop, self._accept_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        print(f"[{self.sim.name}] Modbus TCP 대기 중 {self.host}:{self.port}", flush=True)

    def stop(self) -> None:
        """서버를 끈다. 전원이 꺼진 설비처럼 이미 열린 연결도 함께 끊는다."""
        self._stop.set()
        if self._server:
            self._server.close()
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            for action in (lambda: c.shutdown(socket.SHUT_RDWR), c.close):
                try:
                    action()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def _snapshot(self) -> list[int]:
        with self._lock:
            return self.sim.registers()

    def _tick_loop(self) -> None:
        last = time.monotonic()
        last_phase = None
        while not self._stop.is_set():
            time.sleep(TICK_SECONDS)
            now = time.monotonic()
            dt = min(now - last, 1.0)
            last = now
            with self._lock:
                self.sim.step(dt)
                phase = self.sim.phase
            if self.verbose and phase != last_phase:
                print(f"[{self.sim.name}] 단계 {PHASE_NAMES[phase]}", flush=True)
            last_phase = phase

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_client, args=(conn, addr), daemon=True).start()

    def _serve_client(self, conn: socket.socket, addr) -> None:
        print(f"[{self.sim.name}] 연결: {addr}", flush=True)
        with self._lock:
            self._conns.add(conn)
        try:
            while not self._stop.is_set():
                header = _recv_exact(conn, 7)
                tid, _proto, length, unit = struct.unpack(">HHHB", header)
                pdu = _recv_exact(conn, length - 1)

                with self._lock:
                    self._requests += 1
                    n = self._requests
                f = self.faults
                if f.drop_every and n % f.drop_every == 0:
                    print(f"[{self.sim.name}] #{n} 응답 없이 연결 끊음", flush=True)
                    return
                if f.delay_ms:
                    time.sleep(f.delay_ms / 1000.0)
                if f.garbage_every and n % f.garbage_every == 0:
                    print(f"[{self.sim.name}] #{n} 깨진 응답 전송", flush=True)
                    # 프로토콜 ID가 0이 아닌 헤더 + 의미 없는 2바이트
                    conn.sendall(struct.pack(">HHHB", tid, 0x1234, 3, unit) + b"\x00\x00")
                    continue

                conn.sendall(build_response(tid, unit, handle_pdu(pdu, self._snapshot())))
        except (ConnectionError, OSError):
            pass
        finally:
            with self._lock:
                self._conns.discard(conn)
            conn.close()
            print(f"[{self.sim.name}] 연결 종료: {addr}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Modbus TCP 로봇 시뮬레이터")
    parser.add_argument("--name", default="ARM-01")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5021)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--heat-rate", type=float, default=0.15, help="움직일 때 초당 오르는 온도")
    parser.add_argument("--cool-rate", type=float, default=0.2, help="쉴 때 초당 내리는 온도")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--drop-every", type=int, default=0)
    parser.add_argument("--garbage-every", type=int, default=0)
    parser.add_argument("--verbose", action="store_true", help="단계가 바뀔 때마다 출력")
    args = parser.parse_args()

    sim = RobotSim(args.name, seed=args.seed, heat_rate=args.heat_rate, cool_rate=args.cool_rate)
    faults = Faults(args.delay_ms, args.drop_every, args.garbage_every)
    server = SimServer(sim, args.host, args.port, faults, verbose=args.verbose)
    try:
        server.start()
    except OSError as e:
        print(f"[{args.name}] 시작하지 못했습니다: {e}", flush=True)
        raise SystemExit(1)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
