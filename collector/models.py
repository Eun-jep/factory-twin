"""레지스터 값을 사람이 읽을 수 있는 설비 상태로 바꾼다.

레지스터 맵은 simulator/robot_sim.py와 같아야 한다 (tests가 일치 여부를 확인한다).
"""
from __future__ import annotations

from dataclasses import dataclass

REG_JOINT1 = 0
REG_JOINT2 = 1
REG_GRIPPER = 2
REG_PHASE = 3
REG_BOX_SENSOR = 4
REG_COUNT = 5
REG_TEMP = 6
REG_STATUS = 7
REG_CYCLE = 8
REG_CONVEYOR = 9
REGISTER_COUNT = 10

PHASE_NAMES = ["대기", "접근", "집기", "이동", "놓기", "복귀"]
STATUS_NAMES = ["정상", "주의", "이상"]

# 레지스터 이름 (패킷을 화면에 풀어서 보여 줄 때 쓴다). 위 REG_* 순서와 같다.
REGISTER_LABELS = ["관절1", "관절2", "집게", "로봇 단계", "상자 감지", "생산 카운트",
                   "온도", "상태", "사이클 시간", "컨베이어 속도"]

# 차트에 그리는 기준선. 시뮬레이터 설비가 '주의'/'이상'으로 판정하는 온도와 같아야 한다 (시험이 확인).
WARN_TEMP = 65.0
FAULT_TEMP = 80.0


def describe_register(index: int, raw: int) -> str:
    """레지스터 원시 값을 사람이 읽는 말로 바꾼다. 예: (0, 452) -> '45.2°'"""
    if index in (REG_JOINT1, REG_JOINT2):
        return f"{raw / 10:.1f}°"
    if index == REG_GRIPPER:
        return "닫힘" if raw else "열림"
    if index == REG_PHASE:
        return PHASE_NAMES[raw] if raw < len(PHASE_NAMES) else f"알 수 없음({raw})"
    if index == REG_BOX_SENSOR:
        return "감지" if raw else "없음"
    if index == REG_TEMP:
        return f"{raw / 10:.1f}℃"
    if index == REG_STATUS:
        return STATUS_NAMES[raw] if raw < len(STATUS_NAMES) else f"알 수 없음({raw})"
    if index == REG_CYCLE:
        return f"{raw / 10:.1f}초"
    if index == REG_CONVEYOR:
        return f"{raw} mm/s"
    return str(raw)


@dataclass(frozen=True)
class Snapshot:
    timestamp: float            # 읽은 시각 (epoch 초)
    joint1_deg: float
    joint2_deg: float
    gripper_closed: bool
    phase: int
    box_detected: bool
    count: int
    temp_c: float
    status: int
    cycle_s: float
    conveyor_mm_s: int

    @property
    def phase_name(self) -> str:
        return PHASE_NAMES[self.phase]

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status]


def parse_registers(regs: list[int], timestamp: float) -> Snapshot:
    """레지스터 10개를 Snapshot으로 바꾼다. 값이 이상하면 ValueError."""
    if len(regs) != REGISTER_COUNT:
        raise ValueError(f"레지스터 {REGISTER_COUNT}개가 필요합니다. 받은 개수: {len(regs)}")
    if regs[REG_PHASE] >= len(PHASE_NAMES):
        raise ValueError(f"알 수 없는 로봇 단계: {regs[REG_PHASE]}")
    if regs[REG_STATUS] >= len(STATUS_NAMES):
        raise ValueError(f"알 수 없는 상태 값: {regs[REG_STATUS]}")
    if regs[REG_GRIPPER] > 1 or regs[REG_BOX_SENSOR] > 1:
        raise ValueError("집게 또는 상자 감지 값이 0/1이 아닙니다.")

    return Snapshot(
        timestamp=timestamp,
        joint1_deg=regs[REG_JOINT1] / 10.0,
        joint2_deg=regs[REG_JOINT2] / 10.0,
        gripper_closed=bool(regs[REG_GRIPPER]),
        phase=regs[REG_PHASE],
        box_detected=bool(regs[REG_BOX_SENSOR]),
        count=regs[REG_COUNT],
        temp_c=regs[REG_TEMP] / 10.0,
        status=regs[REG_STATUS],
        cycle_s=regs[REG_CYCLE] / 10.0,
        conveyor_mm_s=regs[REG_CONVEYOR],
    )
