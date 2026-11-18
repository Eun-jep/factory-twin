"""로봇 팔 1대의 동작을 흉내 내는 모델 (통신과 무관한 순수 로직).

한 사이클: 대기 -> 접근 -> 집기 -> 이동 -> 놓기 -> 복귀
- 컨베이어 센서 앞에 상자가 도착하면(상자 감지) 로봇이 움직이기 시작한다.
- 놓기가 끝나면 생산 카운트가 1 올라간다.
- 온도는 움직일 때 오르고 쉴 때 내린다. 80도 이상이면 정지(이상), 70도 이하로 식으면 복구한다.

step(dt)로 시간을 흘려 보내고, registers()로 현재 상태를 Modbus 레지스터 값으로 읽는다.
"""
from __future__ import annotations

import random

# ---- 레지스터 맵 (Holding Register, 주소는 0부터) -------------------------
REG_JOINT1 = 0      # 관절1 각도, 0.1도 단위
REG_JOINT2 = 1      # 관절2 각도, 0.1도 단위
REG_GRIPPER = 2     # 0 열림, 1 닫힘
REG_PHASE = 3       # 로봇 단계 (아래 PHASE_*)
REG_BOX_SENSOR = 4  # 상자 감지 0/1
REG_COUNT = 5       # 생산 카운트 (누적, 65536에서 0으로 돌아감)
REG_TEMP = 6        # 온도, 0.1도 단위
REG_STATUS = 7      # 0 정상, 1 주의, 2 이상
REG_CYCLE = 8       # 마지막 사이클 시간, 0.1초 단위
REG_CONVEYOR = 9    # 컨베이어 속도, mm/s
REGISTER_COUNT = 10

PHASE_IDLE, PHASE_APPROACH, PHASE_GRAB, PHASE_MOVE, PHASE_RELEASE, PHASE_RETURN = range(6)
PHASE_NAMES = ["대기", "접근", "집기", "이동", "놓기", "복귀"]

STATUS_NORMAL, STATUS_WARNING, STATUS_FAULT = range(3)

# ---- 동작 설정 --------------------------------------------------------------
POSES = {                      # (관절1, 관절2) 도 단위
    "home": (90.0, 20.0),
    "pick": (130.0, 60.0),
    "place": (50.0, 70.0),
}
# 단계별 (걸리는 시간 초, 목표 자세)
PHASE_PLAN = {
    PHASE_APPROACH: (1.5, "pick"),
    PHASE_GRAB: (0.8, "pick"),
    PHASE_MOVE: (2.0, "place"),
    PHASE_RELEASE: (0.8, "place"),
    PHASE_RETURN: (1.5, "home"),
}
NEXT_PHASE = {
    PHASE_APPROACH: PHASE_GRAB,
    PHASE_GRAB: PHASE_MOVE,
    PHASE_MOVE: PHASE_RELEASE,
    PHASE_RELEASE: PHASE_RETURN,
    PHASE_RETURN: PHASE_IDLE,
}

SENSOR_PULSE = 0.6        # 상자 감지 신호가 켜져 있는 시간(초)
AMBIENT_TEMP = 35.0       # 이보다 더 식지 않는다
START_TEMP = 40.0
WARN_TEMP = 65.0
FAULT_TEMP = 80.0
RECOVER_TEMP = 70.0       # 이상 상태는 이 온도 이하로 식어야 풀린다
CONVEYOR_SPEED = 500


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


class RobotSim:
    def __init__(
        self,
        name: str,
        seed: int | None = None,
        heat_rate: float = 0.15,      # 움직이는 동안 초당 오르는 온도
        cool_rate: float = 0.2,       # 쉬는 동안 초당 내리는 온도
        box_interval: tuple[float, float] = (5.0, 11.0),  # 다음 상자가 올 때까지(초). 사이클(6.6초)보다 길어야 로봇이 쉬는 시간이 생긴다
    ) -> None:
        self.name = name
        self.rng = random.Random(seed)
        self.heat_rate = heat_rate
        self.cool_rate = cool_rate
        self.box_interval = box_interval

        self.sim_time = 0.0
        self.phase = PHASE_IDLE
        self.phase_t = 0.0
        self.j1, self.j2 = POSES["home"]
        self._pose_from = (self.j1, self.j2)
        self._pose_to = (self.j1, self.j2)
        self.gripper = 0
        self.count = 0
        self.temp = START_TEMP
        self.status = STATUS_NORMAL
        self._fault_latched = False
        self.last_cycle = 0.0
        self._cycle_start = 0.0

        self.box_waiting = False
        self._box_timer = self.rng.uniform(*box_interval)
        self._sensor_left = 0.0

    # ------------------------------------------------------------------
    def step(self, dt: float) -> None:
        """시간을 dt초 흘려 보낸다."""
        self.sim_time += dt
        self._update_box(dt)
        self._update_temperature(dt)
        self._update_status()
        if self.status == STATUS_FAULT:
            self._stop_in_place()
            return
        self._update_motion(dt)

    def registers(self) -> list[int]:
        return [
            int(round(self.j1 * 10)),
            int(round(self.j2 * 10)),
            self.gripper,
            self.phase,
            1 if self._sensor_left > 0 else 0,
            self.count & 0xFFFF,
            int(round(self.temp * 10)),
            self.status,
            int(round(self.last_cycle * 10)),
            0 if self.status == STATUS_FAULT else CONVEYOR_SPEED,
        ]

    # ------------------------------------------------------------------
    def _update_box(self, dt: float) -> None:
        if not self.box_waiting:
            self._box_timer -= dt
            if self._box_timer <= 0:
                self.box_waiting = True
                self._sensor_left = SENSOR_PULSE
        if self._sensor_left > 0:
            self._sensor_left = max(0.0, self._sensor_left - dt)

    def _update_temperature(self, dt: float) -> None:
        active = self.phase != PHASE_IDLE
        rate = self.heat_rate if active else -self.cool_rate
        self.temp += rate * dt + self.rng.uniform(-0.02, 0.02)
        self.temp = max(AMBIENT_TEMP, self.temp)

    def _update_status(self) -> None:
        if self.temp >= FAULT_TEMP:
            self._fault_latched = True
        elif self.temp <= RECOVER_TEMP:
            self._fault_latched = False

        if self._fault_latched:
            self.status = STATUS_FAULT
        elif self.temp >= WARN_TEMP:
            self.status = STATUS_WARNING
        else:
            self.status = STATUS_NORMAL

    def _stop_in_place(self) -> None:
        """이상이 되면 그 자리에서 멈춘다. 복구 후 현재 자세에서 다시 시작한다."""
        self.phase = PHASE_IDLE
        self.phase_t = 0.0

    def _update_motion(self, dt: float) -> None:
        if self.phase == PHASE_IDLE:
            if self.box_waiting:
                self._cycle_start = self.sim_time
                self._enter(PHASE_APPROACH)
            return

        duration = PHASE_PLAN[self.phase][0]
        self.phase_t += dt
        t = min(self.phase_t / duration, 1.0)
        s = _smoothstep(t)
        self.j1 = self._pose_from[0] + (self._pose_to[0] - self._pose_from[0]) * s
        self.j2 = self._pose_from[1] + (self._pose_to[1] - self._pose_from[1]) * s
        if self.phase_t >= duration:
            self._advance()

    def _enter(self, phase: int) -> None:
        self.phase = phase
        self.phase_t = 0.0
        self._pose_from = (self.j1, self.j2)
        self._pose_to = POSES[PHASE_PLAN[phase][1]]
        if phase == PHASE_GRAB:
            self.gripper = 1
            self.box_waiting = False      # 상자를 집었으니 다음 상자를 기다린다
            self._box_timer = self.rng.uniform(*self.box_interval)
        elif phase == PHASE_RELEASE:
            self.gripper = 0

    def _advance(self) -> None:
        finished = self.phase
        if finished == PHASE_RELEASE:
            self.count = (self.count + 1) & 0xFFFF
        nxt = NEXT_PHASE[finished]
        if nxt == PHASE_IDLE:
            self.phase = PHASE_IDLE
            self.phase_t = 0.0
            self.last_cycle = self.sim_time - self._cycle_start
        else:
            self._enter(nxt)
