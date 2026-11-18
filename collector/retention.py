"""측정값 보관 정책: 보관 기간이 지난 측정값과 통신 이벤트를 자동으로 지운다.

왜 필요한가: 설비 1대는 하루에 약 17만 건(초당 2건)을 쌓고, 건당 약 110바이트라서 6대는 하루 110MB, 100대는 1.9GB가 된다.
지우는 기능이 없으면 DB가 끝없이 커진다 (7단계 부하 시험에서 확인).

규칙
- 측정값(samples)과 통신 이벤트(events)만 지운다. 알람은 건수가 적고 '누가 언제 확인했는가'를 남기는 기록이라 지우지 않는다.
- 보관 기간은 --retention-days (기본 7일). 0이면 지우지 않는다.
- 한 번에 조금씩(chunk건) 지우고 사이사이 쉬어서, 오래 쌓인 DB를 처음 정리할 때도 수집이 멈추지 않는다.
- 시작할 때 한 번, 그다음 주기적으로(기본 1시간) 정리한다.
- SQLite 파일 크기는 지운다고 줄지 않는다 (빈 공간을 다시 쓸 뿐). 보관 기간 안의 양만큼에서 더 커지지 않는 것이 목표다.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from .store import Store

DEFAULT_RETENTION_DAYS = 7.0
DEFAULT_PERIOD_SECONDS = 3600.0
DEFAULT_CHUNK = 5000


def retention_cutoff(now: float, days: float) -> float | None:
    """이 시각보다 오래된 것을 지운다. 보관 기간이 0 이하(끄기)이면 None."""
    return None if days <= 0 else now - days * 86400.0


async def prune_once(store: Store, cutoff: float, chunk: int = DEFAULT_CHUNK, pause: float = 0.05) -> int:
    """cutoff보다 오래된 것을 더 지울 것이 없을 때까지 chunk건씩 지운다. 지운 총 건수를 돌려준다.
    한 덩어리를 지울 때마다 pause초 쉬어서 수집(같은 이벤트 루프)에 차례를 넘긴다."""
    total = 0
    while True:
        deleted = store.prune_old(cutoff, chunk)
        if deleted == 0:
            return total
        total += deleted
        await asyncio.sleep(pause)


async def prune_loop(store: Store, days: float, *, period: float = DEFAULT_PERIOD_SECONDS,
                     clock: Callable[[], float] = time.time, chunk: int = DEFAULT_CHUNK,
                     pause: float = 0.05, on_pruned: Callable[[int], None] | None = None) -> None:
    """보관 기간이 지난 것을 주기적으로 지운다 (취소될 때까지). 정리 중 오류가 나도 수집은 멈추지 않는다."""
    while True:
        cutoff = retention_cutoff(clock(), days)
        if cutoff is not None:
            try:
                total = await prune_once(store, cutoff, chunk, pause)
                if total and on_pruned:
                    on_pruned(total)
            except asyncio.CancelledError:
                raise
            except Exception as e:                      # 정리가 실패해도 다음 주기에 다시 시도한다
                print(f"보관 정책 정리 실패 (다음 주기에 다시 시도): {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(period)
