// web/robot_math.js 시험. Node.js 내장 시험 도구를 쓴다.
//   node --test tests/js
import test from "node:test";
import assert from "node:assert/strict";
import {
  DEG, COLORS, BEACON_COLORS, joint1ToYaw, joint2ToPitch, smoothTowards, robotColor, slotX, advanceCrate,
  fitDistance, beaconState, rollerAngle, laneHalfLength, dashXs, projectedHalfWidth, ndcToScreen,
  robotKind, scaraPose, SCARA_L1, SCARA_L2, ARTICULATED_ARM, columnCount, cellPlacement, laneZ, ROW_Z,
} from "../../web/robot_math.js";

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} 와 ${b} 가 다릅니다`);

test("joint1: 90도가 정면, 130도는 +40도, 50도는 -40도", () => {
  near(joint1ToYaw(90), 0);
  near(joint1ToYaw(130), 40 * DEG);
  near(joint1ToYaw(50), -40 * DEG);
});

test("joint2: 값이 클수록 더 아래로 숙인다(회전값이 더 작다)", () => {
  near(joint2ToPitch(0), 0);
  near(joint2ToPitch(60), -60 * DEG);
  assert.ok(joint2ToPitch(70) < joint2ToPitch(20));
});

test("각도 변환은 라디안 단위다 (180도는 파이)", () => {
  near(joint1ToYaw(270), Math.PI);
});

test("smoothTowards: dt=0이면 그대로, 오래 지나면 목표에 도달", () => {
  assert.equal(smoothTowards(5, 10, 0), 5);
  near(smoothTowards(0, 10, 100), 10, 1e-6);
});

test("smoothTowards: 한 번에 넘지 않고 목표 쪽으로만 다가간다", () => {
  let v = 0;
  for (let i = 0; i < 20; i++) {
    const next = smoothTowards(v, 10, 1 / 60);
    assert.ok(next > v && next < 10, `${i}번째: ${v} -> ${next}`);
    v = next;
  }
  let w = 20;                                         // 목표보다 큰 쪽에서도 같다
  const down = smoothTowards(w, 10, 1 / 60);
  assert.ok(down < w && down > 10);
});

test("smoothTowards: 프레임을 잘게 나눠도 같은 결과 (프레임 속도와 무관)", () => {
  const once = smoothTowards(0, 10, 0.1);
  const twice = smoothTowards(smoothTowards(0, 10, 0.05), 10, 0.05);
  const sixth = [1, 2, 3, 4, 5, 6].reduce((v) => smoothTowards(v, 10, 0.1 / 6), 0);
  near(once, twice);
  near(once, sixth);
});

test("smoothTowards: rate가 클수록 빨리 따라간다", () => {
  assert.ok(smoothTowards(0, 10, 0.1, 20) > smoothTowards(0, 10, 0.1, 5));
});

test("robotColor: 상태별 색, 끊기면 상태와 상관없이 회색", () => {
  assert.equal(robotColor(0, true), COLORS.normal);
  assert.equal(robotColor(1, true), COLORS.warning);
  assert.equal(robotColor(2, true), COLORS.fault);
  for (const status of [0, 1, 2]) assert.equal(robotColor(status, false), COLORS.offline);
});

test("robotColor: 모르는 상태 값은 정상 색으로 (화면이 깨지지 않게)", () => {
  assert.equal(robotColor(9, true), COLORS.normal);
  assert.equal(robotColor(undefined, true), COLORS.normal);
});

test("slotX: 가운데 정렬", () => {
  assert.deepEqual([0, 1, 2].map((i) => slotX(i, 3)), [-4.5, 0, 4.5]);
  assert.equal(slotX(0, 1), 0);
  assert.deepEqual([0, 1].map((i) => slotX(i, 2)), [-2.25, 2.25]);
  near(slotX(2, 4, 3) + slotX(1, 4, 3), 0);           // 좌우 대칭
});

test("advanceCrate: 속도 x 시간만큼 앞으로 간다", () => {
  near(advanceCrate(0, 500, 1, 3.4), 0.5);             // 500mm/s = 0.5m/s
  near(advanceCrate(-1, 1000, 0.25, 3.4), -0.75);
});

test("advanceCrate: 속도 0이면 멈춘다 (설비 이상 정지)", () => {
  assert.equal(advanceCrate(0.3, 0, 5, 3.4), 0.3);
});

test("advanceCrate: 끝에 닿으면 처음으로 돌아간다", () => {
  assert.equal(advanceCrate(1.69, 500, 1, 3.4), -1.7);
  near(advanceCrate(1.7, 0, 1, 3.4), 1.7);              // 끝 위치 그대로면 아직 안 넘음
});

test("fitDistance: 화면이 좁을수록 카메라가 멀어지고, 넓으면 세로 기준에서 멈춘다", () => {
  const wide = fitDistance(3, 7.5, 3, 45);          // 아주 넓은 화면: 세로가 기준
  const normal = fitDistance(1.6, 7.5, 3, 45);
  const narrow = fitDistance(0.8, 7.5, 3, 45);
  assert.ok(narrow > normal && normal > wide, `${narrow} ${normal} ${wide}`);
  near(wide, 3 / Math.tan(22.5 * DEG));             // 세로가 기준이면 가로 여유와 무관
  near(fitDistance(5, 7.5, 3, 45), wide);
});

test("fitDistance: 계산한 거리에서 가로 끝이 정확히 화면 끝에 닿는다", () => {
  const aspect = 1.2, halfWidth = 7.5;
  const d = fitDistance(aspect, halfWidth, 1, 45);
  const visibleHalfWidth = d * Math.tan(22.5 * DEG) * aspect;
  near(visibleHalfWidth, halfWidth);
});

test("beaconState: 통신이 끊기면 상태와 상관없이 꺼진다", () => {
  for (const status of [0, 1, 2]) {
    const b = beaconState(status, false, 0.1);
    assert.equal(b.color, BEACON_COLORS.off);
    assert.equal(b.level, 0);
  }
});

test("beaconState: 정상은 초록으로 계속 켜져 있다 (시간이 지나도 같다)", () => {
  for (const t of [0, 0.1, 0.37, 1.9, 12.5]) {
    const b = beaconState(0, true, t);
    assert.equal(b.color, BEACON_COLORS.normal);
    assert.equal(b.level, beaconState(0, true, 0).level);
  }
});

test("beaconState: 이상은 빨강이 초당 2번 깜빡이고, 주의는 노랑이 더 천천히 깜빡인다", () => {
  assert.equal(beaconState(2, true, 0.1).color, BEACON_COLORS.fault);
  assert.equal(beaconState(2, true, 0.1).level, 1);       // 0.5초 주기의 켜진 구간
  assert.ok(beaconState(2, true, 0.35).level < 0.2);      // 꺼진 구간
  assert.equal(beaconState(1, true, 0.1).color, BEACON_COLORS.warning);
  assert.equal(beaconState(1, true, 0.1).level, 1);
  assert.equal(beaconState(1, true, 0.1 + 1.25 / 2).level, 0.3);   // 주기 1.25초의 어두운 구간
  // 이상이 주의보다 더 자주 바뀐다: 0~5초 사이 밝기가 바뀌는 횟수 비교
  const flips = (status) => {
    let n = 0, prev = beaconState(status, true, 0).level;
    for (let t = 0.01; t < 5; t += 0.01) {
      const cur = beaconState(status, true, t).level;
      if (cur !== prev) n++;
      prev = cur;
    }
    return n;
  };
  assert.ok(flips(2) > flips(1));
});

test("rollerAngle: 물건이 가는 방향으로 윗면이 움직이도록 각도가 줄어든다", () => {
  near(rollerAngle(1, 100, 1, 0.1), 0);                  // 0.1m 이동 / 반지름 0.1 = 1라디안
  assert.ok(rollerAngle(3, 500, 0.1, 0.06) < 3);
});

test("rollerAngle: 속도 0이면 멈추고, 항상 0~2파이 안에 머문다", () => {
  assert.equal(rollerAngle(2, 0, 5, 0.06), 2);
  for (let i = 0; i < 200; i++) {
    const a = rollerAngle(i * 0.37, 700, 0.3, 0.06);
    assert.ok(a >= 0 && a < 2 * Math.PI, `${a}`);
  }
  near(rollerAngle(0, 100, 1, 0.1), 2 * Math.PI - 1);    // 0 아래로 내려가면 한 바퀴 돌려 되돌린다
});

test("laneHalfLength: 로봇이 늘면 길어지고, 1대여도 양옆 여유가 있다", () => {
  near(laneHalfLength(3), 4.5 + 3.2);
  assert.ok(laneHalfLength(6) > laneHalfLength(3));
  assert.ok(laneHalfLength(0) > 0);                      // 0대여도 깨지지 않는다
});

test("dashXs: 점선은 가운데 기준 좌우 대칭이고 길 밖으로 나가지 않는다", () => {
  const h = 7.7, step = 0.9;
  const xs = dashXs(h, step);
  assert.ok(xs.length > 10);
  for (let i = 0; i < xs.length; i++) near(xs[i], -xs[xs.length - 1 - i]);
  assert.ok(xs[0] >= -h && xs[xs.length - 1] <= h);
  near(xs[1] - xs[0], step);
  assert.deepEqual(dashXs(0.3, 0.9), []);                // 너무 짧으면 조각이 없다
});

test("projectedHalfWidth: 정면이면 가로 폭 그대로, 옆으로 비껴 보면 깊이가 더해진다", () => {
  near(projectedHalfWidth(7.5, 3, 0), 7.5);
  near(projectedHalfWidth(7.5, 3, 90 * DEG), 3);                          // 완전히 옆에서 보면 깊이만 보인다
  near(projectedHalfWidth(7.5, 3, 30 * DEG), 7.5 * Math.cos(30 * DEG) + 3 * 0.5);
  near(projectedHalfWidth(7.5, 3, -30 * DEG), projectedHalfWidth(7.5, 3, 30 * DEG));   // 좌우 대칭
});

test("ndcToScreen: 가운데는 화면 한가운데, 위쪽(y=1)은 픽셀 0 (화면 y축은 아래로 늘어난다)", () => {
  const c = ndcToScreen(0, 0, 0.5, 800, 400);
  near(c.x, 400); near(c.y, 200);
  const topLeft = ndcToScreen(-1, 1, 0.5, 800, 400);
  near(topLeft.x, 0); near(topLeft.y, 0);
  const bottomRight = ndcToScreen(1, -1, 0.5, 800, 400);
  near(bottomRight.x, 800); near(bottomRight.y, 400);
});

test("ndcToScreen: 카메라 뒤나 너무 먼 점은 보이지 않는 것으로 표시한다", () => {
  assert.equal(ndcToScreen(0, 0, 0.3, 100, 100).visible, true);
  assert.equal(ndcToScreen(0, 0, 1.2, 100, 100).visible, false);    // 너무 멀다
  assert.equal(ndcToScreen(0, 0, -1.5, 100, 100).visible, false);   // 카메라 뒤
});

// ---- 로봇 종류·스카라·배치 ------------------------------------------------------
test("robotKind: 이름 끝 번호가 홀수면 관절 팔, 짝수면 스카라, 번호가 없으면 관절 팔", () => {
  assert.deepEqual(["ARM-01", "ARM-02", "ARM-03", "ARM-04", "ARM-10"].map(robotKind),
    ["articulated", "scara", "articulated", "scara", "scara"]);
  assert.equal(robotKind("PICKER"), "articulated");
  assert.equal(robotKind(""), "articulated");
});

// 스카라 팔의 팔 끝 위치를 앞으로 계산(순운동학)한다: 마디 두 개를 이은 점. 방향 벡터는 (cos a, -sin a).
function scaraTip(pose) {
  const a1 = pose.shoulderYaw, a2 = pose.shoulderYaw + pose.elbowYaw;
  return [SCARA_L1 * Math.cos(a1) + SCARA_L2 * Math.cos(a2), -(SCARA_L1 * Math.sin(a1) + SCARA_L2 * Math.sin(a2))];
}

test("scaraPose: 관절 팔과 같은 점에 닿는다 (같은 컨베이어와 팔레트를 집고 놓는다)", () => {
  // 시뮬레이터의 자세: 집기(130도, 60도), 놓기(50도, 65도)와 그 사이 값들
  for (const [j1, j2] of [[130, 60], [50, 65], [100, 40], [70, 55], [120, 62]]) {
    const yaw = joint1ToYaw(j1), pitch = joint2ToPitch(j2);
    const [x, z] = scaraTip(scaraPose(yaw, pitch));
    const wanted = ARTICULATED_ARM * Math.cos(-pitch);
    near(x, wanted * Math.cos(yaw), 1e-6);
    near(z, -wanted * Math.sin(yaw), 1e-6);
  }
});

test("scaraPose: 대기 자세(너무 멀다)는 팔을 곧게 펴고 방향만 맞춘다", () => {
  const pose = scaraPose(joint1ToYaw(90), joint2ToPitch(20));
  near(pose.elbowYaw, 0, 1e-3);
  near(pose.shoulderYaw, 0, 1e-3);
  const turned = scaraPose(0.5, joint2ToPitch(20));
  near(turned.shoulderYaw, 0.5, 1e-3);                  // 곧게 편 채 목표 방향으로 돈다
});

test("scaraPose: 팔꿈치는 항상 같은 쪽(안쪽, 음수)으로 꺾이고, 집게는 집고 놓을 때 내려간다", () => {
  for (const j2 of [20, 30, 45, 60, 65]) assert.ok(scaraPose(0, joint2ToPitch(j2)).elbowYaw <= 1e-9);
  assert.ok(scaraPose(0, joint2ToPitch(60)).elbowYaw < -1);              // 많이 접힌다
  near(scaraPose(0, joint2ToPitch(20)).drop, 0);                        // 대기에서는 내려가지 않는다
  assert.ok(scaraPose(0, joint2ToPitch(65)).drop > scaraPose(0, joint2ToPitch(40)).drop);
  assert.ok(scaraPose(0, joint2ToPitch(90)).drop <= 0.4 + 1e-9);        // 최대 깊이를 넘지 않는다
});

test("scaraPose: 어떤 입력에서도 유한한 값을 돌려준다 (경계 값)", () => {
  for (const pitch of [0, -0.1, -Math.PI / 2, -3, 1]) {
    for (const yaw of [-3, 0, 3]) {
      const p = scaraPose(yaw, pitch);
      assert.ok(Number.isFinite(p.shoulderYaw) && Number.isFinite(p.elbowYaw) && Number.isFinite(p.drop), JSON.stringify(p));
    }
  }
});

test("columnCount: 3대까지는 대수만큼, 4대부터는 두 줄로 나눠 올림", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 7, 8].map(columnCount), [1, 1, 2, 3, 3, 3, 3, 4, 4]);
});

test("cellPlacement: 3대까지는 한 줄(z=0), 6대는 3대씩 두 줄이고 앞 줄은 뒤돌려 놓는다", () => {
  const three = [0, 1, 2].map((i) => cellPlacement(i, 3));
  assert.deepEqual(three.map((c) => c.x), [-4.5, 0, 4.5]);
  assert.ok(three.every((c) => c.z === 0 && c.rot === 0 && c.rows === 1));
  const six = [0, 1, 2, 3, 4, 5].map((i) => cellPlacement(i, 6));
  assert.deepEqual(six.map((c) => c.x), [-4.5, 0, 4.5, -4.5, 0, 4.5]);
  assert.deepEqual(six.map((c) => c.z), [-ROW_Z, -ROW_Z, -ROW_Z, ROW_Z, ROW_Z, ROW_Z]);
  assert.deepEqual(six.map((c) => c.rot), [0, 0, 0, Math.PI, Math.PI, Math.PI]);
  assert.ok(six.every((c) => c.rows === 2));
});

test("cellPlacement: 줄에 남는 로봇은 가운데 정렬, 같은 자리에 두 대가 놓이지 않는다", () => {
  const four = [0, 1, 2, 3].map((i) => cellPlacement(i, 4));
  assert.equal(four[3].x, 0);                            // 앞 줄에 혼자 있으면 가운데
  for (const count of [4, 5, 6, 7, 10]) {
    const cells = Array.from({ length: count }, (_, i) => cellPlacement(i, count));
    assert.equal(new Set(cells.map((c) => `${c.x},${c.z}`)).size, count, `count=${count}`);
  }
});

test("laneZ: 한 줄이면 로봇 앞쪽, 두 줄이면 두 줄 사이 한가운데", () => {
  assert.equal(laneZ(3), ROW_Z);
  assert.equal(laneZ(6), 0);
});
