// web/factory_logic.js 시험 (벽 모니터에 보여 줄 값 계산). Node.js 내장 시험 도구를 쓴다.
//   node --test tests/js/factory_logic.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  statusSummary, kpiLevel, formatPercent, pushHistory, chartRange, chartPoints,
  alarmKindLabel, recentAlarms, ALARM_KIND_LABELS,
} from "../../web/factory_logic.js";

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} 와 ${b} 가 다릅니다`);

test("statusSummary: 상태별로 센다", () => {
  const list = [
    { status: 0, online: true }, { status: 0, online: true }, { status: 1, online: true },
    { status: 2, online: true }, { status: 2, online: true }, { status: 2, online: true },
  ];
  assert.deepEqual(statusSummary(list), { normal: 2, warning: 1, fault: 3, offline: 0 });
});

test("statusSummary: 통신이 끊긴 설비는 상태와 상관없이 '끊김'으로만 센다 (이중 집계 없음)", () => {
  const list = [{ status: 2, online: false }, { status: 0, online: false }, { status: 1, online: true }];
  const s = statusSummary(list);
  assert.deepEqual(s, { normal: 0, warning: 1, fault: 0, offline: 2 });
  assert.equal(s.normal + s.warning + s.fault + s.offline, list.length);
});

test("statusSummary: 설비가 없으면 모두 0, 모르는 상태 값은 정상으로 센다", () => {
  assert.deepEqual(statusSummary([]), { normal: 0, warning: 0, fault: 0, offline: 0 });
  assert.equal(statusSummary([{ status: 9, online: true }]).normal, 1);
});

test("kpiLevel: 가동률 95%, 80% 경계", () => {
  assert.equal(kpiLevel("availability", 0.95), "good");
  assert.equal(kpiLevel("availability", 0.9499), "caution");
  assert.equal(kpiLevel("availability", 0.8), "caution");
  assert.equal(kpiLevel("availability", 0.7999), "danger");
});

test("kpiLevel: 통신 성공률 99%, 90% 경계와 알람 수", () => {
  assert.equal(kpiLevel("comm", 0.99), "good");
  assert.equal(kpiLevel("comm", 0.98), "caution");
  assert.equal(kpiLevel("comm", 0.9), "caution");
  assert.equal(kpiLevel("comm", 0.89), "danger");
  assert.equal(kpiLevel("alarms", 0), "good");
  assert.equal(kpiLevel("alarms", 3), "caution");
});

test("kpiLevel: 값이 없거나 모르는 이름이면 none", () => {
  assert.equal(kpiLevel("availability", null), "none");
  assert.equal(kpiLevel("comm", undefined), "none");
  assert.equal(kpiLevel("unknown", 1), "none");
});

test("formatPercent: 소수 한 자리, 값이 없으면 '-'", () => {
  assert.equal(formatPercent(0.9876), "98.8%");
  assert.equal(formatPercent(1), "100.0%");
  assert.equal(formatPercent(0), "0.0%");
  assert.equal(formatPercent(null), "-");
  assert.equal(formatPercent(undefined), "-");
});

test("pushHistory: 끝에 붙이고, 넘치면 오래된 것부터 버린다. 원래 배열은 그대로", () => {
  const a = [1, 2, 3];
  const b = pushHistory(a, 4, 3);
  assert.deepEqual(b, [2, 3, 4]);
  assert.deepEqual(a, [1, 2, 3]);
  assert.deepEqual(pushHistory([], 5, 3), [5]);
  assert.deepEqual(pushHistory([1, 2], 3, 3), [1, 2, 3]);
  assert.equal(pushHistory(new Array(60).fill(1), 2, 60).length, 60);
});

test("chartRange: 값을 모두 담고, 너무 좁으면 최소 폭으로 넓힌다", () => {
  const r = chartRange([[40.2, 41.8], [45, 55.5]]);
  assert.ok(r.lo <= 40.2 && r.hi >= 55.5);
  assert.equal(Number.isInteger(r.lo) && Number.isInteger(r.hi), true);
  const narrow = chartRange([[50, 50.2]]);
  assert.ok(narrow.hi - narrow.lo >= 20);
  assert.ok(narrow.lo <= 50 && narrow.hi >= 50.2);
});

test("chartRange: 가장 낮은 값과 높은 값 둘 다 위아래에 여백(2)을 둬서 선이 가장자리에 붙지 않는다", () => {
  assert.deepEqual(chartRange([[30, 70]]), { lo: 28, hi: 72 });
  assert.deepEqual(chartRange([[30, 70]], 20, 5), { lo: 25, hi: 75 });
});

test("chartRange: 값이 없으면 기본 범위", () => {
  assert.deepEqual(chartRange([]), { lo: 30, hi: 80 });
  assert.deepEqual(chartRange([[], []]), { lo: 30, hi: 80 });
});

test("chartPoints: 가장 최근 값이 오른쪽 끝, 큰 값이 위(y가 작다)", () => {
  const pts = chartPoints([40, 60, 80], 100, 50, 40, 80, 3);
  assert.equal(pts.length, 3);
  near(pts[2].x, 100);
  near(pts[0].x, 0);
  near(pts[0].y, 50);             // 가장 낮은 값 = 아래 끝
  near(pts[2].y, 0);              // 가장 높은 값 = 위 끝
  near(pts[1].y, 25);
});

test("chartPoints: 값이 적으면 오른쪽에 붙여 그리고, 범위를 벗어난 값은 가장자리에 붙인다", () => {
  const pts = chartPoints([50, 60], 100, 100, 0, 100, 11);       // 11칸 중 2개: 간격 10
  near(pts[1].x, 100);
  near(pts[0].x, 90);
  const clipped = chartPoints([-50, 500], 100, 100, 0, 100, 2);
  near(clipped[0].y, 100);
  near(clipped[1].y, 0);
});

test("chartPoints: 값이 하나뿐이거나 범위가 0이어도 깨지지 않는다", () => {
  const one = chartPoints([50], 100, 100, 0, 100, 60);
  assert.equal(one.length, 1);
  assert.ok(Number.isFinite(one[0].x) && Number.isFinite(one[0].y));
  const flat = chartPoints([5, 5], 100, 100, 5, 5, 2);
  assert.ok(flat.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)));
  assert.deepEqual(chartPoints([], 100, 100, 0, 100, 60), []);
});

// ---- 최근 알람 ------------------------------------------------------------------
const alarm = (id, over = {}) => ({
  id, device: "ARM-01", kind: "EQUIPMENT_FAULT", severity: 1, raised_at: 1000 + id, last_raised_at: 1000 + id,
  acked_at: null, acked_by: null, repeat_count: 1, ...over,
});

test("alarmKindLabel: 서버 코드 4종을 한글로, 모르는 코드는 그대로 보여 준다", () => {
  assert.equal(alarmKindLabel("EQUIPMENT_FAULT"), "설비 이상");
  assert.equal(alarmKindLabel("TEMP_WARNING"), "온도 경고");
  assert.equal(alarmKindLabel("COMM_LOST"), "통신 끊김");
  assert.equal(alarmKindLabel("COUNTER_RESET"), "카운터 초기화");
  assert.equal(alarmKindLabel("SOMETHING_NEW"), "SOMETHING_NEW");
  assert.equal(Object.keys(ALARM_KIND_LABELS).length, 4);
});

test("recentAlarms: 가장 최근에 발생한 순으로 limit건만 고르고 나머지 건수를 센다", () => {
  const list = [alarm(1), alarm(5), alarm(3), alarm(2), alarm(4)];
  const r = recentAlarms(list, 3);
  assert.deepEqual(r.items.map((a) => a.id), [5, 4, 3]);
  assert.equal(r.more, 2);
});

test("recentAlarms: 다시 발생한 알람(last_raised_at이 늦음)은 처음 발생 시각이 오래돼도 앞에 온다", () => {
  const old = alarm(1, { raised_at: 100, last_raised_at: 9000, repeat_count: 4 });
  const fresh = alarm(2, { raised_at: 2000, last_raised_at: 2000 });
  const r = recentAlarms([fresh, old], 2);
  assert.deepEqual(r.items.map((a) => a.id), [1, 2]);
  assert.equal(r.items[0].count, 4);
  assert.equal(r.items[0].ts, 9000);
});

test("recentAlarms: 시각이 같으면 번호가 큰 것이 먼저, last_raised_at이 없으면 raised_at을 쓴다", () => {
  const a = alarm(1, { last_raised_at: 500, raised_at: 500 }), b = alarm(2, { last_raised_at: 500, raised_at: 500 });
  assert.deepEqual(recentAlarms([a, b]).items.map((x) => x.id), [2, 1]);
  const noLast = alarm(3, { last_raised_at: null, raised_at: 7000 });
  assert.equal(recentAlarms([noLast]).items[0].ts, 7000);
});

test("recentAlarms: 확인 여부, 종류 이름, 등급이 들어간다", () => {
  const r = recentAlarms([alarm(1, { acked_at: 1234, kind: "TEMP_WARNING", severity: 2 }), alarm(2)]);
  const byId = Object.fromEntries(r.items.map((a) => [a.id, a]));
  assert.equal(byId[1].acked, true);
  assert.equal(byId[2].acked, false);
  assert.equal(byId[1].label, "온도 경고");
  assert.equal(byId[1].severity, 2);
  assert.equal(byId[2].device, "ARM-01");
});

test("recentAlarms: 알람이 없으면 빈 목록, 3건 이하면 more가 0, 원래 배열은 그대로", () => {
  assert.deepEqual(recentAlarms([]), { items: [], more: 0 });
  assert.equal(recentAlarms([alarm(1), alarm(2), alarm(3)]).more, 0);
  const list = [alarm(1), alarm(3), alarm(2)];
  const copy = JSON.stringify(list);
  recentAlarms(list);
  assert.equal(JSON.stringify(list), copy);
});

test("recentAlarms: 반복 횟수가 없으면 1로 본다", () => {
  const a = alarm(1);
  delete a.repeat_count;
  assert.equal(recentAlarms([a]).items[0].count, 1);
});

test("recentAlarms: limit을 바꾸면 그만큼만 고르고 나머지 건수도 따라 바뀐다", () => {
  const list = [alarm(1), alarm(2), alarm(3), alarm(4), alarm(5)];
  const two = recentAlarms(list, 2);
  assert.deepEqual(two.items.map((a) => a.id), [5, 4]);
  assert.equal(two.more, 3);
  const five = recentAlarms(list, 5);
  assert.equal(five.items.length, 5);
  assert.equal(five.more, 0);
});
