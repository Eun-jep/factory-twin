"""저장된 데이터에서 차트용 이력과 지표(KPI)를 계산한다.

화면이 DB를 직접 읽지 않고, 서버가 계산해서 JSON으로 준다 (계산은 여기서 한 번만, 시험이 있다).
"""
from __future__ import annotations

import time
from typing import Iterable

from .device import DeviceWorker
from .models import FAULT_TEMP, WARN_TEMP
from .store import Store

STATUS_FAULT = 2


def downsample(rows: list, max_points: int) -> list:
    """점이 너무 많으면 일정 간격으로 솎아낸다. 마지막 점은 항상 남긴다 (최신 값이 빠지면 안 되므로)."""
    n = len(rows)
    if n <= max_points:
        return list(rows)
    stride = -(-n // max_points)                 # 올림 나눗셈
    out = rows[::stride]
    if out[-1] is not rows[-1]:
        if len(out) < max_points:
            out.append(rows[-1])
        else:
            out[-1] = rows[-1]                   # 자리가 없으면 마지막 자리를 최신 값으로 바꾼다 (최대 개수는 지킨다)
    return out


def history(store: Store, device: str, minutes: float, now: float, max_points: int = 300) -> dict:
    """최근 minutes분의 측정값 (차트용). max_points를 넘으면 솎아서 준다."""
    t0 = now - minutes * 60
    rows = store.samples_between(device, t0, now)
    points = [{"ts": r["ts"], "temp": r["temp"], "joint1": r["joint1"], "joint2": r["joint2"],
               "status": r["status"], "phase": r["phase"], "count": r["count"]}
              for r in downsample(rows, max_points)]
    return {"device": device, "from": t0, "to": now, "total_samples": len(rows), "points": points,
            "thresholds": {"warn": WARN_TEMP, "fault": FAULT_TEMP}}


def availability(store: Store, device: str, now: float, window_seconds: float, interval: float) -> float | None:
    """가동률 = 이상(정지)이 아닌 상태로 읽은 측정값 수 / 그 구간에 읽었어야 할 측정값 수.

    수집이 안 된 시간(통신 끊김, 수집 서버가 꺼져 있던 시간)은 가동하지 않은 것으로 센다.
    단, 그 설비의 첫 측정 이전은 세지 않는다 (처음 켠 직후에 가동률이 낮게 나오지 않도록).
    아직 표본이 없거나 구간이 너무 짧으면 None.
    """
    first = store.first_sample_ts(device)
    if first is None:
        return None
    start = max(now - window_seconds, first)
    span = now - start
    if span < interval:
        return None
    expected = span / interval
    good = store.count_samples(device, start, now, status_not=STATUS_FAULT)
    return min(1.0, good / expected)


def compute_kpis(store: Store, workers: Iterable[DeviceWorker], now: float,
                 window_minutes: float = 10, interval: float = 0.5) -> dict:
    """화면 위쪽에 보여 줄 지표.

    가동률과 생산량은 최근 window_minutes분 기준이고, 통신 성공률·평균 응답·재접속은 수집 서버를 켠 뒤 누적이다.
    """
    workers = list(workers)
    window = window_minutes * 60
    per_device: dict[str, dict] = {}
    for w in workers:
        per_device[w.name] = {
            "availability": availability(store, w.name, now, window, interval),
            "production": store.production_between(w.name, now - window, now),
        }

    ok = sum(w.stats.reads_ok for w in workers)
    failed = sum(w.stats.reads_failed for w in workers)
    latencies = [w.stats.avg_latency_ms for w in workers if w.stats.reads_ok > 0]
    avail = [d["availability"] for d in per_device.values() if d["availability"] is not None]
    return {
        "window_minutes": window_minutes,
        "computed_at": now,
        "availability": sum(avail) / len(avail) if avail else None,
        "comm_success_rate": ok / (ok + failed) if ok + failed else None,
        "avg_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "reconnects": sum(w.stats.reconnects for w in workers),
        "production": sum(d["production"] for d in per_device.values()),
        "per_device": per_device,
    }


class KpiCache:
    """지표 계산은 DB를 읽으므로 매초가 아니라 ttl초에 한 번만 다시 계산한다."""

    def __init__(self, store: Store, workers: list[DeviceWorker], interval: float,
                 window_minutes: float = 10, ttl: float = 5.0) -> None:
        self._store = store
        self._workers = workers
        self._interval = interval
        self._window = window_minutes
        self._ttl = ttl
        self._cached: dict | None = None
        self._at = 0.0

    def get(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if self._cached is None or now - self._at >= self._ttl:
            self._cached = compute_kpis(self._store, self._workers, now, self._window, self._interval)
            self._at = now
        return self._cached
