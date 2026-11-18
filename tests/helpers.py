"""시험용 공통 도우미."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "simulator")):
    if p not in sys.path:
        sys.path.insert(0, p)

from collector.models import Snapshot  # noqa: E402


def snap(ts: float, count: int = 0, temp: float = 40.0, status: int = 0) -> Snapshot:
    """시각 ts에 읽은 설비 상태를 하나 만든다."""
    return Snapshot(
        timestamp=ts, joint1_deg=90.0, joint2_deg=20.0, gripper_closed=False, phase=0,
        box_detected=False, count=count, temp_c=temp, status=status, cycle_s=6.6,
        conveyor_mm_s=500,
    )
