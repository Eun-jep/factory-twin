// 공장 벽 모니터에 보여 줄 값을 만드는 계산만 모은 파일.
// Three.js도 canvas도 쓰지 않아서 Node.js로 시험할 수 있다 (tests/js/factory_logic.test.mjs).
// factory_env.js가 이 결과를 받아 화면(canvas)에 그린다.

// ---- 상태별 개수 -----------------------------------------------------------------
// robots: [{status: 0|1|2, online: bool}]. 통신이 끊긴 설비는 상태와 상관없이 '끊김'으로 센다.
export function statusSummary(robots) {
  const summary = { normal: 0, warning: 0, fault: 0, offline: 0 };
  for (const r of robots) {
    if (!r.online) summary.offline += 1;
    else if (r.status === 1) summary.warning += 1;
    else if (r.status === 2) summary.fault += 1;
    else summary.normal += 1;
  }
  return summary;
}

// ---- 지표 단계 (화면의 지표 띠와 같은 기준) -----------------------------------------
// 반환: "good" | "caution" | "danger" | "none"(값이 아직 없음)
export function kpiLevel(name, value) {
  if (value === null || value === undefined) return "none";
  if (name === "availability") return value >= 0.95 ? "good" : value >= 0.8 ? "caution" : "danger";
  if (name === "comm") return value >= 0.99 ? "good" : value >= 0.9 ? "caution" : "danger";
  if (name === "alarms") return value === 0 ? "good" : "caution";
  return "none";
}

export function formatPercent(ratio) {
  return ratio === null || ratio === undefined ? "-" : (ratio * 100).toFixed(1) + "%";
}

// ---- 온도 추이 ---------------------------------------------------------------------
// 새 값을 끝에 붙이고 가장 오래된 것부터 버려 max개만 남긴다. 이전 배열은 고치지 않는다.
export function pushHistory(history, value, max) {
  const next = [...history, value];
  return next.length > max ? next.slice(next.length - max) : next;
}

// 여러 설비의 값 전체가 들어오는 세로 범위. 너무 좁으면 최소 폭(minSpan)으로 넓히고, 눈금이 깔끔하도록 정수로 맞춘다.
// 값이 하나도 없으면 기본 범위(30~80)를 쓴다.
export function chartRange(histories, minSpan = 20, pad = 2) {
  let lo = Infinity, hi = -Infinity;
  for (const values of histories) {
    for (const v of values) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (lo === Infinity) return { lo: 30, hi: 80 };
  lo = Math.floor(lo - pad);
  hi = Math.ceil(hi + pad);
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = Math.floor(mid - minSpan / 2);
    hi = lo + minSpan;
  }
  return { lo, hi };
}

// 값 배열을 가로 width, 세로 height 영역 안의 점 좌표로 바꾼다. 가장 최근 값이 오른쪽 끝(x=width)이다.
// maxPoints: 가로축 전체가 담는 값의 개수. 값이 적으면 오른쪽에 붙여 그린다.
export function chartPoints(values, width, height, lo, hi, maxPoints) {
  const span = hi - lo || 1;
  const step = maxPoints > 1 ? width / (maxPoints - 1) : 0;
  const first = width - (values.length - 1) * step;
  return values.map((v, i) => ({
    x: first + i * step,
    y: height - ((Math.min(hi, Math.max(lo, v)) - lo) / span) * height,
  }));
}

// ---- 최근 알람 ---------------------------------------------------------------------
// 서버의 알람 종류 코드(collector/alarms.py)를 화면용 한글 이름으로 바꾼다. 모르는 코드는 그대로 보여 준다(숨기지 않는다).
export const ALARM_KIND_LABELS = {
  EQUIPMENT_FAULT: "설비 이상",
  TEMP_WARNING: "온도 경고",
  COMM_LOST: "통신 끊김",
  COUNTER_RESET: "카운터 초기화",
};

export function alarmKindLabel(kind) {
  return ALARM_KIND_LABELS[kind] ?? String(kind);
}

// 열린 알람 목록에서 가장 최근에 발생한 limit건을 고른다. 최근 = 마지막으로 (다시) 발생한 시각이 늦은 순,
// 같은 시각이면 번호(id)가 큰 것이 먼저. 원래 배열은 고치지 않는다.
// 반환: { items: [{id, severity, device, label, ts, acked, count}], more: 보여 주지 못한 나머지 건수 }
export function recentAlarms(alarms, limit = 3) {
  const stamp = (a) => a.last_raised_at ?? a.raised_at ?? 0;
  const sorted = [...alarms].sort((a, b) => stamp(b) - stamp(a) || b.id - a.id);
  return {
    items: sorted.slice(0, limit).map((a) => ({
      id: a.id,
      severity: a.severity,
      device: a.device,
      label: alarmKindLabel(a.kind),
      ts: stamp(a),
      acked: a.acked_at !== null && a.acked_at !== undefined,
      count: a.repeat_count ?? 1,
    })),
    more: Math.max(0, alarms.length - limit),
  };
}
