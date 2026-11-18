// 상자 쌓기와 이동 로봇(AGV)의 '판단'만 모아 둔 파일.
// Three.js를 쓰지 않아서 브라우저 없이 Node로 시험할 수 있다 (tests/js/agv_logic.test.mjs).
// scene.js가 이 결과를 받아 도형을 움직인다.
//
// 이야기의 흐름 (서버의 실제 값과 이어져 있다)
//   1. 로봇이 한 사이클을 끝낼 때마다 서버의 '생산 카운트'가 1 늘어난다.
//   2. 카운트가 늘 때마다 그 로봇의 팔레트에 상자가 하나 쌓인다. (stackUpdate)
//   3. 한 팔레트가 가득 차면(STACK_CAPACITY개) AGV를 부른다.
//   4. AGV는 가득 찬 팔레트로 가서 상자를 싣고, 출하장에 내려놓고, 대기 위치로 돌아간다. (agvStep)

export const STACK_CAPACITY = 4;      // 팔레트 한 장에 쌓는 상자 수 (2x2 한 층)
export const PALLET_OFFSET_X = 0.65;  // 로봇 가운데에서 팔레트까지의 x 거리 (scene.js의 팔레트 위치와 같다)
export const AGV_SPEED = 2.2;         // AGV 속도 (장면 단위/초)
export const LOAD_TIME = 1.6;         // 싣는 데 걸리는 시간(초)
export const UNLOAD_TIME = 1.2;       // 내리는 데 걸리는 시간(초)

// 로봇이 뒤돌려져 있으면(앞 줄, rot=파이) 팔레트도 반대쪽(-x)에 있다.
// robotX: 로봇의 x 위치, rot: 로봇이 y축으로 돌아간 각도(라디안)
export function palletX(robotX, rot) {
  return robotX + PALLET_OFFSET_X * Math.cos(rot);
}

// 서버의 로봇 단계 값(0 대기, 1 접근, 2 집기, 3 이동, 4 놓기, 5 복귀) 중 상자를 손에 들고 있는 단계.
// 집기에서 닫힌 집게는 놓기가 시작될 때 열리지만, 상자는 놓기가 끝나(카운트가 오를 때) 팔레트에 내려앉는 것으로 그린다.
const HOLDING_PHASES = new Set([2, 3, 4]);
export function isHolding(phase) {
  return HOLDING_PHASES.has(phase);
}

// ---- 팔레트에 쌓인 상자 수 ------------------------------------------------------
// prev: 이전 결과({stacked, lastCount}) 또는 처음이면 null.
// 처음 본 카운트는 '이미 쌓여 있던 것'으로 보고 한꺼번에 떨어뜨리지 않는다 (count % cap 개로 시작).
// 카운트가 늘면 늘어난 만큼 쌓되 팔레트가 가득 차면 더 쌓지 못한다.
// 카운트가 줄면(수집기나 설비를 다시 켠 경우) 기준만 새로 잡는다.
// 반환: stacked(지금 쌓인 수), lastCount(기준), added(이번에 새로 쌓인 수)
export function stackUpdate(prev, count, cap = STACK_CAPACITY) {
  if (!prev) return { stacked: count % cap, lastCount: count, added: 0 };
  if (count < prev.lastCount) return { stacked: prev.stacked, lastCount: count, added: 0 };
  const added = Math.min(count - prev.lastCount, cap - prev.stacked);
  return { stacked: prev.stacked + added, lastCount: count, added };
}

// ---- AGV ------------------------------------------------------------------------
// 상태(phase)
//   idle       대기 위치에서 쉼
//   toPallet   가득 찬 팔레트로 가는 중
//   loading    상자를 싣는 중
//   toDock     출하장으로 가는 중
//   unloading  상자를 내리는 중
//   toHome     대기 위치로 돌아가는 중 (돌아가다가 새 요청이 생기면 바로 그쪽으로 간다)
export const AGV_PHASE_LABELS = {
  idle: "대기", toPallet: "팔레트로 이동", loading: "적재 중",
  toDock: "출하 운반", unloading: "하역 중", toHome: "복귀",
};

export function agvInit(x) {
  return { phase: "idle", x, timer: 0, targetId: null, targetX: null, cargo: 0 };
}

function moveToward(x, target, step) {
  const gap = target - x;
  return Math.abs(gap) <= step ? target : x + Math.sign(gap) * step;
}

// 가장 가까운 요청 (거리가 같으면 이름 순). requests: [{id, x}] = 가득 찬 팔레트들
function nearest(requests, x) {
  let best = null;
  for (const r of requests) {
    if (!best) { best = r; continue; }
    const a = Math.abs(r.x - x), b = Math.abs(best.x - x);
    if (a < b - 1e-9 || (Math.abs(a - b) <= 1e-9 && r.id < best.id)) best = r;
  }
  return best;
}

// dt초가 지난 뒤의 AGV 상태를 계산한다. 이전 상태를 고치지 않고 새 객체를 돌려준다.
// 반환: { agv: 새 상태, events: [{type: "loadStart"|"loaded"|"unloaded", ...}] }
//   loaded    => 해당 팔레트(id)의 상자를 모두 실었다. 호출한 쪽은 그 팔레트를 비운다.
//   unloaded  => 출하장에 count개를 내려놓았다.
export function agvStep(agv, dt, requests, homeX, dockX, cap = STACK_CAPACITY) {
  const s = { ...agv };
  const events = [];
  const step = AGV_SPEED * dt;

  if (s.phase === "idle" || s.phase === "toHome") {
    const r = nearest(requests, s.x);
    if (r) { s.phase = "toPallet"; s.targetId = r.id; s.targetX = r.x; }
  }

  switch (s.phase) {
    case "toPallet": {
      const r = requests.find((q) => q.id === s.targetId);
      if (!r) { s.phase = "toHome"; s.targetId = null; s.targetX = null; break; }   // 요청이 사라졌다
      s.targetX = r.x;                                                              // 로봇 배치가 바뀌면 따라간다
      s.x = moveToward(s.x, s.targetX, step);
      if (s.x === s.targetX) { s.phase = "loading"; s.timer = 0; events.push({ type: "loadStart", id: s.targetId }); }
      break;
    }
    case "loading":
      s.timer += dt;
      if (s.timer >= LOAD_TIME) {
        s.cargo = cap;
        events.push({ type: "loaded", id: s.targetId });
        s.phase = "toDock"; s.timer = 0; s.targetId = null; s.targetX = null;
      }
      break;
    case "toDock":
      s.x = moveToward(s.x, dockX, step);
      if (s.x === dockX) { s.phase = "unloading"; s.timer = 0; }
      break;
    case "unloading":
      s.timer += dt;
      if (s.timer >= UNLOAD_TIME) {
        events.push({ type: "unloaded", count: s.cargo });
        s.cargo = 0; s.phase = "toHome"; s.timer = 0;
      }
      break;
    case "toHome":
      s.x = moveToward(s.x, homeX, step);
      if (s.x === homeX) s.phase = "idle";
      break;
    default:
      break;
  }
  return { agv: s, events };
}

// ---- 모양 계산 ------------------------------------------------------------------
// 상자 k번째 자리 (가운데 기준 좌우 dx, 앞뒤 dz, 몇 층째 layer). 2x2로 놓고 4개가 넘으면 위층.
export function slotOffset(k, spacing) {
  return { dx: ((k % 2) - 0.5) * spacing, dz: ((Math.floor(k / 2) % 2) - 0.5) * spacing, layer: Math.floor(k / 4) };
}

// 떨어지는 상자의 높이 보정. t=0이면 height만큼 위에 있고 duration에 닿는다. 가속하며 떨어진다(중력처럼).
export function dropOffset(t, duration, height) {
  if (t >= duration) return 0;
  if (t <= 0) return height;
  const u = t / duration;
  return height * (1 - u * u);
}

// 싣는 동안 k번째 상자의 이동 진행도(0 = 아직 팔레트, 1 = AGV 위). 상자들이 차례로 겹치며 날아간다.
// progress = 싣기 전체의 진행도(0~1).
export function crateFlight(progress, k, cap = STACK_CAPACITY) {
  const window = 0.5;                                     // 상자 하나가 날아가는 데 쓰는 전체 시간의 비율
  const gap = cap > 1 ? (1 - window) / (cap - 1) : 0;     // 다음 상자가 출발하는 간격
  const u = (progress - k * gap) / window;
  return Math.min(1, Math.max(0, u));
}
