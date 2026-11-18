// 3D와 상관없는 계산만 모아 둔 파일.
// Three.js를 쓰지 않아서 브라우저 없이도 시험할 수 있다 (tests/js/robot_math.test.mjs).
// scene.js가 이 계산 결과를 가져다가 도형에 적용한다.

export const DEG = Math.PI / 180;      // 도(degree)를 라디안으로 바꿀 때 곱하는 값

// ---- 관절 각도 -> 회전값 ------------------------------------------------------
// 서버가 보내는 각도는 '도' 단위이고, Three.js의 회전은 '라디안' 단위다.

// joint1 = 어깨 회전. 서버 값 90도를 '정면'(회전 0)으로 삼는다.
// 130도면 +40도, 50도면 -40도 돈다. (위에서 보면 +는 컨베이어 쪽, -는 팔레트 쪽)
export function joint1ToYaw(deg) {
  return (deg - 90) * DEG;
}

// joint2 = 팔꿈치. 값이 클수록 팔이 수평에서 아래로 더 숙인다.
// z축 회전에서 음수는 아래쪽이라서 부호를 뒤집는다.
export function joint2ToPitch(deg) {
  return -deg * DEG;
}

// ---- 부드럽게 따라가기 --------------------------------------------------------
// 서버는 0.5초마다 값을 보낸다. 그대로 적용하면 팔이 뚝뚝 끊겨 보이므로,
// 화면은 매 프레임 '목표 값'에 조금씩 다가가게 한다 (지수 감쇠).
// dt = 지난 프레임 이후 흐른 시간(초). 프레임 속도가 달라도 같은 속도로 따라가도록 dt를 반영한다.
// rate가 클수록 빨리 따라간다.
export function smoothTowards(current, target, dt, rate = 8) {
  return current + (target - current) * (1 - Math.exp(-rate * dt));
}

// ---- 상태 색 ------------------------------------------------------------------
export const COLORS = {
  normal: 0xf08a1c,    // 주황 (정상)
  warning: 0xf0b429,   // 노랑 (주의)
  fault: 0xe5484d,     // 빨강 (이상)
  offline: 0x566079,   // 회색 (통신이 끊겨 값이 오래됨)
};

// status: 0 정상, 1 주의, 2 이상 (서버의 레지스터 값). 끊긴 설비는 상태와 상관없이 회색.
export function robotColor(status, online) {
  if (!online) return COLORS.offline;
  return [COLORS.normal, COLORS.warning, COLORS.fault][status] ?? COLORS.normal;
}

// ---- 배치 ---------------------------------------------------------------------
// 로봇 count대를 가운데를 기준으로 가로로 늘어놓을 때, index번째 로봇의 x 위치.
// 예: 3대, 간격 4.5 -> [-4.5, 0, 4.5]
export function slotX(index, count, spacing = 4.5) {
  return (index - (count - 1) / 2) * spacing;
}

// ---- 카메라 거리 ---------------------------------------------------------------
// 가로 halfWidth, 세로 halfHeight(장면의 반쪽 크기)가 화면에 다 들어오려면 카메라가 얼마나 멀리 있어야 하는가.
// 세로 시야각이 vfovDeg일 때, 거리 d에서 보이는 세로 반쪽은 d*tan(시야각/2)이고 가로 반쪽은 거기에 aspect(가로/세로)를 곱한 값이다.
// 화면이 좁아(aspect가 작아)질수록 가로가 먼저 잘리므로 더 멀어져야 한다.
export function fitDistance(aspect, halfWidth, halfHeight, vfovDeg) {
  const t = Math.tan((vfovDeg * DEG) / 2);
  return Math.max(halfHeight / t, halfWidth / (t * aspect));
}

// ---- 비스듬한 시점의 가로 폭 ------------------------------------------------------
// 카메라가 장면을 yaw(라디안)만큼 옆으로 비껴서 보면, 가로로 넓은 장면이 화면에서 차지하는 폭이 달라진다.
// 장면의 가로 반쪽 halfWidth와 깊이 반쪽 halfDepth가 화면 가로 방향으로 투영된 반쪽 폭
// (정면에서 보면 yaw=0이라 halfWidth 그대로). 가까운 쪽이 더 커 보이는 원근 효과는 호출하는 쪽에서 여유를 곱해 반영한다.
export function projectedHalfWidth(halfWidth, halfDepth, yaw) {
  return halfWidth * Math.cos(yaw) + halfDepth * Math.abs(Math.sin(yaw));
}

// ---- 로봇 종류 ------------------------------------------------------------------
// 설비 이름 끝의 번호로 모양을 정한다: 홀수는 관절 팔, 짝수는 스카라(SCARA) 팔. (같은 모양만 늘어서지 않게)
// 서버는 모양을 모르고, 같은 3가지 값(어깨 회전, 팔꿈치, 집게)만 보낸다. 모양은 화면이 정하는 규칙이다.
export function robotKind(name) {
  const m = /(\d+)\s*$/.exec(name);
  return m && Number(m[1]) % 2 === 0 ? "scara" : "articulated";
}

// ---- 스카라 팔의 관절 각도 -------------------------------------------------------
// 관절 팔은 '어깨 회전(yaw)'과 '팔꿈치가 숙인 정도(pitch)'로 팔 끝이 놓이는 위치가 정해진다:
//   방향 = yaw, 수평 거리 = 팔 길이 x cos(숙인 각도).
// 스카라 팔은 마디 두 개가 수평으로 돌기만 해서, 같은 위치에 닿으려면 두 관절 각도를 거꾸로 계산(역운동학)해야 한다.
// 그래야 서로 다른 모양의 로봇이 같은 컨베이어와 팔레트를 정확히 집고 놓는다.
export const ARTICULATED_ARM = 2.4;   // 관절 팔의 길이 (scene.js의 팔과 같다)
export const SCARA_L1 = 1.15;         // 스카라 첫째 마디 길이
export const SCARA_L2 = 1.0;          // 스카라 둘째 마디 길이
const SCARA_DROP = 0.4;               // 집고 놓을 때 아래로 내려가는 최대 깊이

// yaw: 목표 방향(라디안, scene.js의 어깨 회전과 같은 규칙), pitch: joint2ToPitch가 준 값(음수, 숙일수록 작다)
// 반환: shoulderYaw(첫째 마디의 방향), elbowYaw(둘째 마디가 첫째에서 꺾인 각도), drop(집게가 내려가는 깊이)
// 각도 a의 방향 벡터는 (cos a, -sin a)이다 (Three.js에서 +yaw는 +x를 -z 쪽으로 돌린다).
export function scaraPose(yaw, pitch) {
  const down = Math.max(0, -pitch);                                       // 숙인 각도(양수)
  const wanted = ARTICULATED_ARM * Math.cos(down);                        // 관절 팔이 닿을 수평 거리
  const lo = Math.abs(SCARA_L1 - SCARA_L2) + 1e-6, hi = SCARA_L1 + SCARA_L2;
  const reach = Math.min(hi, Math.max(lo, wanted));                       // 마디 길이로 닿을 수 있는 범위로 제한
  const cosElbow = (reach * reach - SCARA_L1 * SCARA_L1 - SCARA_L2 * SCARA_L2) / (2 * SCARA_L1 * SCARA_L2);
  const bend = -Math.acos(Math.min(1, Math.max(-1, cosElbow)));           // 팔꿈치는 안쪽(-)으로 꺾는다: 마디가 옆으로 덜 튀어나온다
  const lead = Math.atan2(SCARA_L2 * Math.sin(bend), SCARA_L1 + SCARA_L2 * Math.cos(bend));
  const t = Math.min(1, Math.max(0, (down / DEG - 20) / 45));             // 20도(대기)~65도(놓기) 사이를 0~1로
  return { shoulderYaw: yaw - lead, elbowYaw: bend, drop: SCARA_DROP * t };
}

// ---- 로봇 배치 (한 줄 또는 두 줄) ---------------------------------------------------
// 3대까지는 한 줄. 4대부터는 두 줄로 놓고, 가운데 길(AGV가 다니는 곳)을 서로 마주 본다.
// 뒤 줄은 그대로(rot=0), 앞 줄은 뒤돌려(rot=파이) 놓는다. 그래야 두 줄 모두 팔레트가 길 쪽을 향한다.
export const ROW_Z = 2.6;             // 두 줄일 때 가운데 길에서 각 줄까지의 거리

// 한 줄에 들어가는 로봇 수 (열 수)
export function columnCount(count) {
  return count <= 3 ? Math.max(1, count) : Math.max(3, Math.ceil(count / 2));
}

// index번째 로봇(이름 순)의 자리: {x, z, rot, rows}
export function cellPlacement(index, count, spacing = 4.5) {
  const columns = columnCount(count);
  const rows = count <= 3 ? 1 : 2;
  const row = Math.floor(index / columns);
  const inRow = Math.min(columns, count - row * columns);                 // 이 줄에 실제로 있는 로봇 수 (가운데 정렬용)
  const x = slotX(index % columns, inRow, spacing);
  if (rows === 1) return { x, z: 0, rot: 0, rows };
  return row === 0 ? { x, z: -ROW_Z, rot: 0, rows } : { x, z: ROW_Z, rot: Math.PI, rows };
}

// AGV 길(안내선)의 z 위치: 한 줄이면 로봇 앞쪽, 두 줄이면 두 줄의 가운데
export function laneZ(count) {
  return count <= 3 ? ROW_Z : 0;
}

// ---- 3D 위치 -> 화면 위치 ---------------------------------------------------------
// Three.js는 3D 점을 카메라로 '찍으면' -1~1 범위의 좌표(NDC)로 바꿔 준다.
// x: -1(왼쪽 끝)~1(오른쪽 끝), y: -1(아래 끝)~1(위 끝), z: -1(가장 가까움)~1(가장 멂)을 벗어나면 카메라 뒤나 시야 밖이다.
// 화면에 HTML 카드를 붙이려면 이것을 픽셀로 바꿔야 한다. 화면의 y축은 아래로 늘어나므로 뒤집는다.
export function ndcToScreen(x, y, z, width, height) {
  return {
    x: ((x + 1) / 2) * width,
    y: ((1 - y) / 2) * height,
    visible: z > -1 && z < 1,
  };
}

// ---- 바닥 안내선 ----------------------------------------------------------------
// 로봇 셀 앞쪽 바닥에 깔 점선(이동 로봇이 다니는 길)의 반쪽 길이. 로봇이 늘면 길도 길어진다.
export function laneHalfLength(count, spacing = 4.5) {
  return ((Math.max(count, 1) - 1) / 2) * spacing + 3.2;
}

// 길이 2*halfLength인 길 위에 점선 조각 x 위치를 step 간격으로 가운데 정렬해 놓는다.
export function dashXs(halfLength, step = 0.9) {
  const n = Math.floor((halfLength * 2) / step);
  return Array.from({ length: n }, (_, i) => (i - (n - 1) / 2) * step);
}

// ---- 경광등 -------------------------------------------------------------------
// 공장 설비 위의 신호등. 정상은 초록 켜짐, 주의는 노랑이 천천히 깜빡, 이상은 빨강이 빠르게 깜빡,
// 통신이 끊기면 꺼진다. t = 시각(초). 같은 t면 항상 같은 결과라서 시험하기 쉽다.
export const BEACON_COLORS = {
  normal: 0x2fe05a,
  warning: 0xffb020,
  fault: 0xff3b3b,
  off: 0x3a4254,
};

// 반환: color(색), level(밝기 0~1)
export function beaconState(status, online, t) {
  if (!online) return { color: BEACON_COLORS.off, level: 0 };
  if (status === 2) return { color: BEACON_COLORS.fault, level: Math.sin(t * 2 * Math.PI * 2) > 0 ? 1 : 0.12 };
  if (status === 1) return { color: BEACON_COLORS.warning, level: Math.sin(t * 2 * Math.PI * 0.8) > 0 ? 1 : 0.3 };
  return { color: BEACON_COLORS.normal, level: 0.85 };
}

// ---- 롤러 회전 ----------------------------------------------------------------
// 컨베이어 위 물건이 +x로 speed(mm/s)만큼 가면, 롤러 윗면도 같이 +x로 간다.
// z축을 축으로 도는 롤러는 각도가 줄어드는 쪽이 +x 방향이다. 한 바퀴를 넘으면 0~2π로 되돌린다.
export function rollerAngle(angle, speedMmPerSec, dt, radius) {
  const next = angle - (speedMmPerSec / 1000) * dt / radius;
  const full = 2 * Math.PI;
  return ((next % full) + full) % full;
}

// ---- 컨베이어 위 상자 ---------------------------------------------------------
// 상자의 x 위치를 dt초 만큼 앞으로 보낸다. 컨베이어 끝에 닿으면 처음으로 돌아간다.
// speedMmPerSec: 컨베이어 속도(mm/s). 장면의 1칸을 1m로 보고 환산한다.
// length: 컨베이어 길이. 상자는 -length/2 ~ +length/2 사이를 오간다.
export function advanceCrate(x, speedMmPerSec, dt, length) {
  const next = x + (speedMmPerSec / 1000) * dt;
  return next > length / 2 ? -length / 2 : next;
}
