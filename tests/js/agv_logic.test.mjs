// web/agv_logic.js 시험 (상자 쌓기, AGV 상태 변화, 모양 계산). Node.js 내장 시험 도구를 쓴다.
//   node --test tests/js/agv_logic.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  STACK_CAPACITY, AGV_SPEED, LOAD_TIME, UNLOAD_TIME,
  isHolding, stackUpdate, agvInit, agvStep, slotOffset, dropOffset, crateFlight, palletX, PALLET_OFFSET_X,
} from "../../web/agv_logic.js";

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} 와 ${b} 가 다릅니다`);

// ---- 상자 쌓기 -----------------------------------------------------------------
test("isHolding: 집기·이동·놓기 단계에서만 상자를 들고 있다", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5].map(isHolding), [false, false, true, true, true, false]);
});

test("stackUpdate: 처음 본 카운트는 한꺼번에 떨어뜨리지 않고 count % 용량 개로 시작", () => {
  assert.deepEqual(stackUpdate(null, 0), { stacked: 0, lastCount: 0, added: 0 });
  assert.deepEqual(stackUpdate(null, 6), { stacked: 2, lastCount: 6, added: 0 });
  assert.deepEqual(stackUpdate(null, 8), { stacked: 0, lastCount: 8, added: 0 });
});

test("stackUpdate: 카운트가 늘면 늘어난 만큼 쌓인다", () => {
  let s = stackUpdate(null, 0);
  s = stackUpdate(s, 1);
  assert.deepEqual(s, { stacked: 1, lastCount: 1, added: 1 });
  s = stackUpdate(s, 1);                                  // 같은 값이면 그대로
  assert.deepEqual(s, { stacked: 1, lastCount: 1, added: 0 });
  s = stackUpdate(s, 3);                                  // 한 번에 2개 늘 수도 있다 (수집 간격 사이에 사이클이 2번)
  assert.deepEqual(s, { stacked: 3, lastCount: 3, added: 2 });
});

test("stackUpdate: 가득 차면 더 쌓이지 않지만 기준 카운트는 따라간다", () => {
  let s = { stacked: STACK_CAPACITY - 1, lastCount: 10 };
  s = stackUpdate(s, 12);                                 // 2개 늘었지만 자리는 1개뿐
  assert.deepEqual(s, { stacked: STACK_CAPACITY, lastCount: 12, added: 1 });
  s = stackUpdate(s, 13);
  assert.deepEqual(s, { stacked: STACK_CAPACITY, lastCount: 13, added: 0 });
});

test("stackUpdate: 카운트가 줄면(설비 재시작) 쌓인 상자는 두고 기준만 새로 잡는다", () => {
  const s = stackUpdate({ stacked: 2, lastCount: 50 }, 0);
  assert.deepEqual(s, { stacked: 2, lastCount: 0, added: 0 });
  assert.deepEqual(stackUpdate(s, 1), { stacked: 3, lastCount: 1, added: 1 });   // 이후엔 다시 정상으로 쌓인다
});

// ---- AGV -----------------------------------------------------------------------
test("agvStep: 요청이 없으면 제자리에서 쉰다", () => {
  let agv = agvInit(-7);
  const events = [];
  for (let i = 0; i < 100; i++) {                          // 5초 동안 요청 없이 진행
    const r = agvStep(agv, 0.05, [], -7, 7);
    agv = r.agv;
    events.push(...r.events);
  }
  assert.equal(agv.phase, "idle");
  assert.equal(agv.x, -7);
  assert.deepEqual(events, []);
});

test("agvStep: 가득 찬 팔레트가 생기면 가서 싣고, 출하장에 내리고, 돌아와서 쉰다 (전체 흐름)", () => {
  // 팔레트는 x=-3인 곳 하나. 'loaded' 이벤트를 받으면 호출한 쪽(scene.js)이 팔레트를 비우므로 그 뒤 요청은 없다.
  let full = true;
  let agv = agvInit(-7);
  const events = [], phases = [agv.phase];
  for (let i = 0; i < 800; i++) {
    const r = agvStep(agv, 0.05, full ? [{ id: "ARM-01", x: -3 }] : [], -7, 7);
    agv = r.agv;
    for (const e of r.events) { events.push(e); if (e.type === "loaded") full = false; }
    if (phases[phases.length - 1] !== agv.phase) phases.push(agv.phase);
  }
  assert.deepEqual(phases, ["idle", "toPallet", "loading", "toDock", "unloading", "toHome", "idle"]);
  assert.deepEqual(events.map((e) => e.type), ["loadStart", "loaded", "unloaded"]);
  assert.equal(events[0].id, "ARM-01");
  assert.equal(events[1].id, "ARM-01");
  assert.equal(events[2].count, STACK_CAPACITY);
  assert.equal(agv.x, -7);                                // 대기 위치로 돌아왔다
  assert.equal(agv.cargo, 0);
});

test("agvStep: 싣고 나서 팔레트를 비우면(호출한 쪽 일) 같은 팔레트를 다시 부르지 않는다", () => {
  let stacked = true;
  let agv = agvInit(-7);
  let loadedEvents = 0;
  for (let i = 0; i < 800; i++) {
    const r = agvStep(agv, 0.05, stacked ? [{ id: "ARM-01", x: -3 }] : [], -7, 7);
    agv = r.agv;
    for (const e of r.events) if (e.type === "loaded") { stacked = false; loadedEvents++; }
  }
  assert.equal(loadedEvents, 1);
});

test("agvStep: 속도는 AGV_SPEED이고, 움직이는 동안 목표를 넘어가지 않는다", () => {
  const r = agvStep(agvInit(-7), 0.5, [{ id: "A", x: -3 }], -7, 7);
  near(r.agv.x, -7 + AGV_SPEED * 0.5);
  const near1 = agvStep({ ...agvInit(-3.01), phase: "toPallet", targetId: "A", targetX: -3 }, 1, [{ id: "A", x: -3 }], -7, 7);
  assert.equal(near1.agv.x, -3);                          // 한 걸음이 거리보다 크면 딱 목표에서 멈춘다
  assert.equal(near1.agv.phase, "loading");
});

test("agvStep: 가장 가까운 팔레트부터 가고, 거리가 같으면 이름 순", () => {
  const far = { id: "ARM-03", x: 5 }, close = { id: "ARM-01", x: -3 };
  assert.equal(agvStep(agvInit(-7), 0.05, [far, close], -7, 7).agv.targetId, "ARM-01");
  const a = { id: "ARM-02", x: -2 }, b = { id: "ARM-01", x: 2 };       // 원점에서 같은 거리
  assert.equal(agvStep(agvInit(0), 0.05, [a, b], -7, 7).agv.targetId, "ARM-01");
});

test("agvStep: 돌아가는 중에 새 요청이 생기면 대기 위치까지 가지 않고 바로 그쪽으로 간다", () => {
  const s = { ...agvInit(2), phase: "toHome" };
  const r = agvStep(s, 0.05, [{ id: "ARM-03", x: 5 }], -7, 7);
  assert.equal(r.agv.phase, "toPallet");
  assert.equal(r.agv.targetId, "ARM-03");
});

test("agvStep: 가는 도중 요청이 사라지면 대기 위치로 돌아간다", () => {
  const s = { ...agvInit(0), phase: "toPallet", targetId: "A", targetX: 5 };
  const r = agvStep(s, 0.05, [], -7, 7);
  assert.equal(r.agv.phase, "toHome");
  assert.equal(r.agv.targetId, null);
});

test("agvStep: 가는 도중 로봇 위치가 바뀌면 새 위치를 따라간다", () => {
  const s = { ...agvInit(0), phase: "toPallet", targetId: "A", targetX: 5 };
  const r = agvStep(s, 0.05, [{ id: "A", x: 6 }], -7, 7);
  assert.equal(r.agv.targetX, 6);
});

test("agvStep: 싣는 시간과 내리는 시간을 지킨다", () => {
  let s = { ...agvInit(-3), phase: "toPallet", targetId: "A", targetX: -3 };
  s = agvStep(s, 0.05, [{ id: "A", x: -3 }], -7, 7).agv;                   // 도착해서 싣기 시작
  assert.equal(s.phase, "loading");
  s = agvStep(s, LOAD_TIME - 0.1, [{ id: "A", x: -3 }], -7, 7).agv;
  assert.equal(s.phase, "loading");                                          // 아직 안 끝났다
  const done = agvStep(s, 0.2, [{ id: "A", x: -3 }], -7, 7);
  assert.equal(done.agv.phase, "toDock");
  assert.equal(done.agv.cargo, STACK_CAPACITY);
  assert.deepEqual(done.events.map((e) => e.type), ["loaded"]);

  let u = { ...agvInit(7), phase: "unloading", cargo: STACK_CAPACITY };
  u = agvStep(u, UNLOAD_TIME - 0.1, [], -7, 7).agv;
  assert.equal(u.phase, "unloading");
  assert.equal(agvStep(u, 0.2, [], -7, 7).agv.phase, "toHome");
});

test("agvStep: 이전 상태 객체를 고치지 않는다", () => {
  const before = agvInit(-7);
  const snapshot = JSON.stringify(before);
  agvStep(before, 0.5, [{ id: "A", x: -3 }], -7, 7);
  assert.equal(JSON.stringify(before), snapshot);
});

test("agvStep: 요청이 여러 개면 하나씩 차례로 처리한다 (동시에 두 곳을 가지 않는다)", () => {
  const full = new Set(["ARM-01", "ARM-03"]);
  const xs = { "ARM-01": -3, "ARM-03": 5 };
  let agv = agvInit(-7);
  const order = [];
  for (let i = 0; i < 2000; i++) {
    const requests = [...full].map((id) => ({ id, x: xs[id] }));
    const r = agvStep(agv, 0.05, requests, -7, 7);
    agv = r.agv;
    for (const e of r.events) {
      if (e.type === "loaded") { full.delete(e.id); order.push(e.id); }
    }
  }
  assert.deepEqual(order, ["ARM-01", "ARM-03"]);          // 가까운 쪽부터, 한 번씩만
});

// ---- 모양 계산 -----------------------------------------------------------------
test("slotOffset: 4개는 2x2 한 층, 5번째부터 위층 (가운데 기준 좌우 대칭)", () => {
  const slots = [0, 1, 2, 3].map((k) => slotOffset(k, 0.5));
  assert.deepEqual(slots.map((s) => s.layer), [0, 0, 0, 0]);
  assert.deepEqual(slots.map((s) => [s.dx, s.dz]), [[-0.25, -0.25], [0.25, -0.25], [-0.25, 0.25], [0.25, 0.25]]);
  assert.equal(slotOffset(4, 0.5).layer, 1);
  assert.equal(new Set(slots.map((s) => `${s.dx},${s.dz}`)).size, 4);   // 겹치는 자리가 없다
});

test("dropOffset: 위에서 시작해 점점 빨라지며 내려와 정확히 0에서 끝난다", () => {
  near(dropOffset(0, 0.4, 1), 1);
  near(dropOffset(0.4, 0.4, 1), 0);
  near(dropOffset(1, 0.4, 1), 0);                         // 끝난 뒤에도 0
  near(dropOffset(-1, 0.4, 1), 1);                        // 시작 전에는 맨 위
  const drop1 = dropOffset(0, 0.4, 1) - dropOffset(0.1, 0.4, 1);
  const drop2 = dropOffset(0.3, 0.4, 1) - dropOffset(0.4, 0.4, 1);
  assert.ok(drop2 > drop1 + 0.2, `${drop1} ${drop2}`);     // 끝으로 갈수록 확실히 더 많이 내려간다 (0.25 대 0.75)
  near(dropOffset(0.2, 0.4, 1), 0.75);                    // 중간 지점의 정확한 값 (1 - 0.5^2)
});

test("crateFlight: 상자들이 차례로 출발하고, 시작·끝에서 0과 1이다", () => {
  for (let k = 0; k < STACK_CAPACITY; k++) {
    near(crateFlight(0, k), 0);
    near(crateFlight(1, k), 1);
  }
  assert.ok(crateFlight(0.2, 0) > crateFlight(0.2, 1));   // 앞 번호가 먼저 간다
  assert.equal(crateFlight(0.2, 3), 0);                   // 마지막 상자는 아직 출발 전
  for (let p = 0; p <= 1; p += 0.01) {
    for (let k = 0; k < STACK_CAPACITY; k++) {
      const u = crateFlight(p, k);
      assert.ok(u >= 0 && u <= 1);
    }
  }
});

test("palletX: 그대로 놓인 로봇은 +x 쪽, 뒤돌려 놓인 로봇(rot=파이)은 -x 쪽에 팔레트가 있다", () => {
  near(palletX(2, 0), 2 + PALLET_OFFSET_X);
  near(palletX(2, Math.PI), 2 - PALLET_OFFSET_X, 1e-12);
  near(palletX(-4.5, Math.PI), -4.5 - PALLET_OFFSET_X, 1e-12);
});
