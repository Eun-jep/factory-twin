"""로봇 6대(ARM-01~06)를 각각 별개의 가상 PLC로 한 번에 띄운다.

    python simulator/run_all.py

포트: ARM-01~06 = 5021~5026
Ctrl+C로 모두 종료한다. 한 대만 끄고 싶으면 해당 프로세스만 종료하면 된다
(수집 서버가 그 설비만 '통신 끊김'으로 처리하는지 시험할 수 있다).

추가 인자는 모든 로봇에 그대로 전달된다. 예)
    python simulator/run_all.py --heat-rate 1.5      # 과열 알람을 빨리 보고 싶을 때
"""
import subprocess
import sys
import time
from pathlib import Path

# (이름, 포트, 시드). 시드는 첫 상자가 도착하는 시각이 서로 달라서 로봇들이 처음부터
# 엇갈려 움직이도록 골랐다 (앞의 3개는 약 5.8초, 8.7초, 10.8초).
ROBOTS = [("ARM-01", 5021, 1), ("ARM-02", 5022, 5), ("ARM-03", 5023, 15),
          ("ARM-04", 5024, 2), ("ARM-05", 5025, 7), ("ARM-06", 5026, 11)]


def main() -> None:
    script = Path(__file__).with_name("modbus_sim.py")
    extra = sys.argv[1:]
    procs = []
    try:
        for name, port, seed in ROBOTS:
            cmd = [sys.executable, str(script), "--name", name, "--port", str(port),
                   "--seed", str(seed), *extra]
            procs.append(subprocess.Popen(cmd))
        print(f"로봇 {len(ROBOTS)}대 실행 중. 종료하려면 Ctrl+C", flush=True)
        # 한 대가 꺼져도 나머지는 계속 돈다 (장애 격리 시연). 전부 꺼지면 끝낸다.
        while any(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
