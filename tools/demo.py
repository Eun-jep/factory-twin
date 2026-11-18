"""시연 실행기: 가상 로봇 6대와 수집 서버를 한 번에 띄우고, 같은 창에서 명령으로 장애를 일으킨다.

    python tools/demo.py

브라우저에서 http://127.0.0.1:8000/ 을 연 뒤, 이 창에 명령을 입력한다 (영상 촬영이나 발표 때 같은 장면을 매번 똑같이 만들려고).

    k 4   로봇 4의 전원을 끈다            -> 3D에서 회색으로 멈추고, 몇 초 뒤 '통신 끊김' 알람
    r 4   로봇 4의 전원을 다시 켠다       -> 몇 초 안에 값이 돌아오고 알람이 해제된다
    f 5   로봇 5를 빨리 과열되게 켠다     -> 주의(노랑) -> 이상(빨강) 정지, 경광등이 깜빡이고 알람이 뜬다
    d 2   로봇 2의 응답을 1.5초 늦춰 켠다 -> 제한 시간(1초)을 넘어 통신이 끊긴다
    s     상태 보기      h  도움말      q  종료 (모든 로봇과 수집기를 끈다)

다른 프로그램이 5021~5026이나 8000 포트를 쓰고 있을 때는 포트를 옮겨서 띄울 수 있다 (예: 로봇 6021~6026, 웹 8100):
    python tools/demo.py --port-offset 1000 --web-port 8100

시작할 때마다 data/demo.db를 새로 만든다 (이전 시연의 알람이 섞이지 않도록). 평소 쓰는 data/factory.db는 건드리지 않는다.
로봇과 수집기의 출력은 data/demo_logs/ 아래 파일로 저장해서 이 창을 깨끗하게 둔다.
5021~5026과 8000 포트를 이미 다른 프로그램이 쓰고 있으면 아무것도 끄지 않고 알려 준 뒤 멈춘다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from modbus_sim import port_in_use  # noqa: E402
from run_all import ROBOTS  # noqa: E402  (이름, 포트, 시드) 6대

WEB_PORT = 8000
DB_PATH = ROOT / "data" / "demo.db"
LOG_DIR = ROOT / "data" / "demo_logs"

# 로봇을 켜는 방식 -> 시뮬레이터에 줄 추가 옵션
MODES = {
    "normal": ["--heat-rate", "0.3"],                              # 대부분 정상으로 일한다
    "fault": ["--heat-rate", "6"],                                 # 빨리 과열돼 주의 -> 이상 정지
    "slow": ["--heat-rate", "0.3", "--delay-ms", "1500"],          # 응답이 제한 시간(1초)보다 늦다
}
MODE_LABELS = {"normal": "정상", "fault": "과열 시연", "slow": "응답 지연 시연", "off": "전원 꺼짐"}

HELP = """명령
  k N   로봇 N의 전원 끄기                (예: k 4)
  r N   로봇 N의 전원 켜기 (정상)
  f N   로봇 N을 빨리 과열되게 켜기       (주의 -> 이상 정지, 알람)
  d N   로봇 N의 응답을 1.5초 늦춰 켜기    (통신 끊김, 알람)
  s     상태 보기      h  도움말      q  종료"""


def parse_command(line: str, count: int) -> tuple[str, int | None] | str:
    """입력 한 줄을 (동작, 로봇 번호)로 바꾼다. 번호는 0부터. 잘못된 입력이면 안내 문장(str)을 돌려준다.
    동작: noop, help, status, quit, kill, normal, fault, slow"""
    parts = line.strip().lower().split()
    if not parts:
        return ("noop", None)
    word = parts[0]
    simple = {"h": "help", "?": "help", "s": "status", "q": "quit"}
    if word in simple:
        return (simple[word], None) if len(parts) == 1 else f"'{word}'는 번호 없이 입력합니다"
    actions = {"k": "kill", "r": "normal", "f": "fault", "d": "slow"}
    if word not in actions:
        return f"모르는 명령입니다: {parts[0]} (도움말: h)"
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        return f"로봇 번호가 필요합니다. 예: {word} 3 (1~{count})"
    number = int(parts[1])
    if not 1 <= number <= count:
        return f"로봇 번호는 1~{count}입니다"
    return (actions[word], number - 1)


def robot_command(name: str, port: int, seed: int, mode: str) -> list[str]:
    """가상 로봇 한 대를 띄우는 명령줄."""
    return [sys.executable, str(ROOT / "simulator" / "modbus_sim.py"), "--name", name, "--port", str(port),
            "--seed", str(seed), *MODES[mode]]


def shifted_robots(offset: int) -> list[tuple[str, int, int]]:
    """로봇 목록(이름, 포트, 시드)의 포트를 offset만큼 옮긴다. 0이면 기본(5021~5026)."""
    return [(name, port + offset, seed) for name, port, seed in ROBOTS]


def collector_command(db_path: Path, web_port: int, robots: list[tuple[str, int, int]] | None = None) -> list[str]:
    """수집기를 띄우는 명령줄. 로봇 포트를 옮겼다면 설비 목록(--devices)도 같이 넘긴다."""
    cmd = [sys.executable, "-m", "collector", "--db", str(db_path), "--web-port", str(web_port)]
    if robots is not None:
        cmd += ["--devices", ",".join(f"{name}:127.0.0.1:{port}" for name, port, _ in robots)]
    return cmd


class Demo:
    def __init__(self, robots: list[tuple[str, int, int]] | None = None, web_port: int = WEB_PORT) -> None:
        self.robots = robots if robots is not None else list(ROBOTS)
        self.web_port = web_port
        self.procs: dict[str, subprocess.Popen] = {}       # 이름 -> 프로세스 (꺼져 있으면 없음)
        self.modes: dict[str, str] = {name: "off" for name, _, _ in self.robots}
        self.collector: subprocess.Popen | None = None
        self._logs = []

    def _log(self, label: str):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        f = open(LOG_DIR / f"{label}.log", "a", encoding="utf-8")
        self._logs.append(f)
        return f

    def start_robot(self, index: int, mode: str) -> None:
        name, port, seed = self.robots[index]
        self.stop_robot(index)
        self.procs[name] = subprocess.Popen(robot_command(name, port, seed, mode), cwd=ROOT,
                                            stdout=self._log(name), stderr=subprocess.STDOUT)
        self.modes[name] = mode
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not port_in_use("127.0.0.1", port):
            time.sleep(0.1)

    def stop_robot(self, index: int) -> None:
        name, port, _ = self.robots[index]
        proc = self.procs.pop(name, None)
        if proc is not None:
            _stop(proc)
        self.modes[name] = "off"
        deadline = time.monotonic() + 3                      # 포트가 비워질 때까지 잠깐 기다린다 (바로 다시 켤 수 있게)
        while time.monotonic() < deadline and port_in_use("127.0.0.1", port):
            time.sleep(0.1)

    def start_collector(self) -> None:
        self.collector = subprocess.Popen(collector_command(DB_PATH, self.web_port, self.robots), cwd=ROOT,
                                          stdout=self._log("collector"), stderr=subprocess.STDOUT)

    def shutdown(self) -> None:
        for index in range(len(self.robots)):
            self.stop_robot(index)
        if self.collector is not None:
            _stop(self.collector)
        for f in self._logs:
            f.close()

    def status_lines(self) -> list[str]:
        lines = []
        for i, (name, port, _) in enumerate(self.robots):
            alive = name in self.procs and self.procs[name].poll() is None
            mode = self.modes[name] if alive else "off"
            lines.append(f"  {i + 1}  {name}  포트 {port}  {MODE_LABELS[mode]}")
        collector_alive = self.collector is not None and self.collector.poll() is None
        lines.append(f"  수집 서버  {'실행 중  http://127.0.0.1:%d/' % self.web_port if collector_alive else '꺼짐'}")
        return lines


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def busy_ports(robots: list[tuple[str, int, int]] | None = None, web_port: int = WEB_PORT) -> list[int]:
    """이미 다른 프로그램이 쓰고 있는 시연 포트 (로봇 6개 + 웹 화면)."""
    robots = robots if robots is not None else ROBOTS
    return [p for p in [port for _, port, _ in robots] + [web_port] if port_in_use("127.0.0.1", p)]


def main() -> None:
    parser = argparse.ArgumentParser(description="시연 실행기: 가상 로봇 6대 + 수집 서버, 명령으로 장애 유발")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="로봇 포트를 이만큼 옮긴다 (기본 0: 5021~5026). 다른 프로그램과 겹칠 때 쓴다")
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help="웹 화면 포트 (기본 8000)")
    args = parser.parse_args()
    robots = shifted_robots(args.port_offset)
    web_port = args.web_port

    busy = busy_ports(robots, web_port)
    if busy:
        print(f"포트 {', '.join(map(str, busy))}을(를) 이미 다른 프로그램이 쓰고 있어서 시작하지 않습니다.\n"
              "먼저 그 프로그램(이전에 띄운 로봇이나 수집기)을 종료하거나, 포트를 옮겨서 실행하세요.\n"
              "예: python tools/demo.py --port-offset 1000 --web-port 8100")
        raise SystemExit(1)
    for suffix in ("", "-wal", "-shm"):                      # 시연용 DB만 새로 시작한다
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    demo = Demo(robots, web_port)
    try:
        print("로봇 6대를 켜는 중...", flush=True)
        for i in range(len(robots)):
            demo.start_robot(i, "normal")
        demo.start_collector()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not port_in_use("127.0.0.1", web_port):
            time.sleep(0.2)
        print(f"\n준비됐습니다. 브라우저에서 http://127.0.0.1:{web_port}/ 을 여세요.\n")
        print(HELP)
        while True:
            try:
                line = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                break
            parsed = parse_command(line, len(robots))
            if isinstance(parsed, str):
                print(parsed)
                continue
            action, index = parsed
            if action == "noop":
                continue
            if action == "quit":
                break
            if action == "help":
                print(HELP)
            elif action == "status":
                print("\n".join(demo.status_lines()))
            elif action == "kill":
                demo.stop_robot(index)
                print(f"{robots[index][0]} 전원을 껐습니다. 3D에서 회색으로 멈추고 몇 초 뒤 통신 끊김 알람이 뜹니다.")
            else:
                demo.start_robot(index, action)
                print(f"{robots[index][0]} 전원을 켰습니다 ({MODE_LABELS[action]}).")
    finally:
        print("\n종료하는 중...", flush=True)
        demo.shutdown()
        print("모두 껐습니다.")


if __name__ == "__main__":
    main()
