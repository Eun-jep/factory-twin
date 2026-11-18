// 공장 건물 분위기: 바닥, 뒷벽, 철골 기둥, 천장 조명, 그리고 벽에 걸린 모니터 3대.
// 로봇과 컨베이어(scene.js)는 그대로 두고, 그 뒤와 위를 채우는 배경만 만든다. 전부 기본 도형으로 만든다.
//
// 벽 모니터는 3D 벽에 붙인 판(Plane)에 <canvas> 그림을 입힌 것이다. 1초마다 canvas를 다시 그리고 texture를 갱신한다.
//   1번 모니터: 공장 현황 (가동률, 생산량, 열린 알람 수, 최근 알람 3건)
//   2번 모니터: 설비 상태 (설비별 카드)
//   3번 모니터: 온도 추이 (설비별 선 그래프) + 출하 현황
// 보여 줄 값을 만드는 계산(개수 세기, 그래프 좌표 등)은 factory_logic.js에 따로 있어 Node로 시험한다.

import * as THREE from "three";
import {
  statusSummary, kpiLevel, formatPercent, pushHistory, chartRange, chartPoints, recentAlarms,
} from "./factory_logic.js";

const WALL_Z = -7.0;                 // 뒷벽의 z 위치 (뒤 줄 로봇 펜스 바로 뒤. 멀면 모니터 글자가 너무 작아진다)
const SCREEN_W = 1024, SCREEN_H = 576;
const REFRESH_SECONDS = 1;           // 모니터를 다시 그리는 간격
const HISTORY_MAX = 60;              // 온도 추이가 담는 값의 수 (1초 간격이면 60초)
const MAX_TILES = 6;                 // 설비 상태 모니터에 그리는 설비 수 (그 이상은 요약만)

const FONT = "'Malgun Gothic', 'Segoe UI', sans-serif";
const COLOR = {
  bg: "#0a0f16", panel: "#131a25", border: "#263043", text: "#e4e8ef", muted: "#8a94a6",
  good: "#2fb344", caution: "#f0b429", danger: "#e5484d", none: "#8a94a6", info: "#3b82f6",
};
const STATUS_COLOR = [COLOR.good, COLOR.caution, COLOR.danger];
const STATUS_LABEL = ["정상", "주의", "이상"];
const LINE_COLORS = ["#4ade80", "#38bdf8", "#f0b429", "#c084fc", "#fb7185", "#2dd4bf"];   // 설비별 선 색

// ---- 작은 도우미 ---------------------------------------------------------------
function box(parent, w, h, d, material, x, y, z, { shadow = false } = {}) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  mesh.position.set(x, y, z);
  mesh.castShadow = shadow;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function text(g, str, x, y, { size = 28, color = COLOR.text, weight = "normal", align = "left" } = {}) {
  g.font = `${weight} ${size}px ${FONT}`;
  g.fillStyle = color;
  g.textAlign = align;
  g.textBaseline = "alphabetic";
  g.fillText(str, x, y);
}

function clock() {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

// 모든 모니터의 공통 바탕: 어두운 배경, 제목, 시계
function drawFrame(g, title) {
  g.fillStyle = COLOR.bg;
  g.fillRect(0, 0, SCREEN_W, SCREEN_H);
  g.fillStyle = COLOR.panel;
  g.fillRect(0, 0, SCREEN_W, 72);
  g.fillStyle = COLOR.info;
  g.fillRect(0, 70, SCREEN_W, 3);
  text(g, title, 32, 49, { size: 36, weight: "bold" });
  text(g, clock(), SCREEN_W - 32, 49, { size: 32, color: COLOR.muted, align: "right" });
}

// 화면 아래 한 줄 요약: 정상 n · 주의 n · 이상 n · 끊김 n
function drawSummary(g, robots, y) {
  const s = statusSummary(robots);
  const items = [["정상", s.normal, COLOR.good], ["주의", s.warning, COLOR.caution], ["이상", s.fault, COLOR.danger], ["끊김", s.offline, COLOR.muted]];
  let x = 32;
  for (const [label, count, color] of items) {
    g.fillStyle = color;
    g.fillRect(x, y - 22, 14, 26);
    text(g, `${label} ${count}`, x + 24, y, { size: 28, color: COLOR.text });
    x += 190;
  }
}

// ---- 1번 모니터: 공장 현황 -------------------------------------------------------
const SEVERITY_COLOR = { 1: COLOR.danger, 2: COLOR.caution, 3: COLOR.info };      // P1 빨강, P2 노랑, P3 파랑

function clockOf(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

// 최근 알람 3건 (어느 알람을 고를지는 factory_logic.js의 recentAlarms가 정한다)
function drawAlarms(g, alarms, top) {
  g.fillStyle = COLOR.border;
  g.fillRect(32, top, SCREEN_W - 64, 2);
  const { items, more } = recentAlarms(alarms, 3);
  text(g, "최근 알람", 32, top + 38, { size: 26, color: COLOR.muted, weight: "bold" });
  if (more > 0) text(g, `외 ${more}건`, SCREEN_W - 32, top + 38, { size: 26, color: COLOR.muted, align: "right" });
  if (items.length === 0) {
    text(g, "열린 알람 없음", 32, top + 100, { size: 32, color: COLOR.good, weight: "bold" });
    return;
  }
  items.forEach((a, i) => {
    const y = top + 52 + i * 50;
    const color = SEVERITY_COLOR[a.severity] ?? COLOR.muted;
    g.fillStyle = color;
    roundRect(g, 32, y, 56, 38, 8);
    g.fill();
    text(g, "P" + a.severity, 60, y + 28, { size: 24, color: "#0a0f16", weight: "bold", align: "center" });
    text(g, clockOf(a.ts), 104, y + 29, { size: 26, color: COLOR.muted });
    text(g, a.device, 262, y + 29, { size: 28, weight: "bold" });
    text(g, a.label + (a.count > 1 ? ` x${a.count}` : ""), 396, y + 29, { size: 28 });
    // 확인 안 된 알람은 눈에 띄게, 확인된 알람은 흐리게
    text(g, a.acked ? "확인됨" : "미확인", SCREEN_W - 32, y + 29,
      { size: 26, color: a.acked ? COLOR.muted : color, weight: a.acked ? "normal" : "bold", align: "right" });
  });
}

function drawOverview(g, d) {
  drawFrame(g, "공장 현황");
  const k = d.kpi;
  const availability = k ? k.availability : null;
  const cards = [
    { label: "가동률 (10분)", value: formatPercent(availability), color: COLOR[kpiLevel("availability", availability)] },
    { label: "생산량 (10분)", value: k ? String(k.production) : "-", color: COLOR.text },
    { label: "열린 알람", value: String(d.openAlarms), color: COLOR[kpiLevel("alarms", d.openAlarms)] },
  ];
  cards.forEach((c, i) => {
    const x = 32 + i * 320, y = 88;
    g.fillStyle = COLOR.panel;
    roundRect(g, x, y, 300, 170, 14);
    g.fill();
    g.strokeStyle = COLOR.border;
    g.lineWidth = 2;
    g.stroke();
    text(g, c.label, x + 24, y + 42, { size: 26, color: COLOR.muted });
    text(g, c.value, x + 24, y + 130, { size: c.value.length > 6 ? 52 : 66, color: c.color, weight: "bold" });
  });
  const comm = k ? formatPercent(k.comm_success_rate) : "-";
  const latency = k && k.avg_latency_ms !== null ? k.avg_latency_ms.toFixed(1) + " ms" : "-";
  text(g, `통신 성공률 ${comm}   평균 응답 ${latency}   재접속 ${k ? k.reconnects : "-"}`, 32, 296, { size: 26, color: COLOR.muted });
  drawSummary(g, d.robots, 342);
  drawAlarms(g, d.alarms, 368);
}

// ---- 2번 모니터: 설비 상태 -------------------------------------------------------
function drawStatus(g, d) {
  drawFrame(g, "설비 상태");
  d.robots.slice(0, MAX_TILES).forEach((r, i) => {
    const x = 32 + (i % 3) * 320, y = 96 + Math.floor(i / 3) * 188;
    const color = r.online ? STATUS_COLOR[r.status] ?? COLOR.good : COLOR.none;
    g.fillStyle = COLOR.panel;
    roundRect(g, x, y, 300, 170, 14);
    g.fill();
    g.fillStyle = color;
    g.fillRect(x, y + 12, 8, 146);
    text(g, r.name, x + 26, y + 50, { size: 34, weight: "bold" });
    text(g, r.online ? STATUS_LABEL[r.status] ?? "?" : "끊김", x + 274, y + 50, { size: 30, color, align: "right", weight: "bold" });
    text(g, r.temp === null ? "- ℃" : r.temp.toFixed(1) + " ℃", x + 26, y + 118, { size: 52, color: r.online ? COLOR.text : COLOR.muted });
    if (!r.online) text(g, "값은 오래됨", x + 26, y + 152, { size: 22, color: COLOR.muted });
  });
  if (d.robots.length > MAX_TILES) text(g, `외 ${d.robots.length - MAX_TILES}대`, SCREEN_W - 32, 470, { size: 26, color: COLOR.muted, align: "right" });
  drawSummary(g, d.robots, 530);
}

// ---- 3번 모니터: 온도 추이 + 출하 현황 ----------------------------------------------
function drawTrend(g, d, histories) {
  drawFrame(g, "온도 추이 (최근 60초)");
  const left = 96, top = 100, width = 880, height = 300;
  const names = d.robots.slice(0, MAX_TILES).map((r) => r.name);
  const series = names.map((n) => histories.get(n) ?? []);
  const { lo, hi } = chartRange(series);
  g.strokeStyle = COLOR.border;
  g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {                                   // 가로 눈금선 5개와 온도 글자
    const y = top + (height * i) / 4;
    g.beginPath();
    g.moveTo(left, y);
    g.lineTo(left + width, y);
    g.stroke();
    text(g, `${Math.round(hi - ((hi - lo) * i) / 4)}`, left - 12, y + 9, { size: 24, color: COLOR.muted, align: "right" });
  }
  series.forEach((values, i) => {
    if (values.length < 2) return;
    const pts = chartPoints(values, width, height, lo, hi, HISTORY_MAX);
    g.strokeStyle = LINE_COLORS[i % LINE_COLORS.length];
    g.lineWidth = 3;
    g.beginPath();
    pts.forEach((p, j) => (j === 0 ? g.moveTo(left + p.x, top + p.y) : g.lineTo(left + p.x, top + p.y)));
    g.stroke();
  });
  names.forEach((n, i) => {                                        // 범례
    const x = left + i * 150;
    g.fillStyle = LINE_COLORS[i % LINE_COLORS.length];
    g.fillRect(x, 436, 22, 8);
    text(g, n, x + 30, 446, { size: 22, color: COLOR.text });
  });
  g.fillStyle = COLOR.panel;
  g.fillRect(0, 484, SCREEN_W, 92);
  text(g, `출하 ${d.shipped}상자`, 32, 540, { size: 40, weight: "bold", color: COLOR.good });
  text(g, `AGV ${d.agvLabel}`, SCREEN_W - 32, 540, { size: 34, color: COLOR.muted, align: "right" });
}

// 모니터 한 대: canvas + 그 그림을 입힌 판 + 테두리
function makeScreen(parent, x, y, z) {
  const canvas = document.createElement("canvas");
  canvas.width = SCREEN_W;
  canvas.height = SCREEN_H;
  const g = canvas.getContext("2d");
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  const w = 6.2, h = (w * SCREEN_H) / SCREEN_W;
  const plane = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: texture, toneMapped: false }));
  plane.position.set(x, y, z + 0.09);
  parent.add(plane);
  box(parent, w + 0.3, h + 0.3, 0.14, new THREE.MeshStandardMaterial({ color: 0x1a2130, metalness: 0.6, roughness: 0.4 }), x, y, z);
  box(parent, w + 0.38, h + 0.38, 0.05, new THREE.MeshStandardMaterial({ color: 0x1e6fd9, emissive: 0x1e6fd9, emissiveIntensity: 0.9 }), x, y, z - 0.07);
  return { g, texture };
}

// ---- 공장 만들기 -------------------------------------------------------------------
// scene에 바닥, 벽, 기둥, 천장, 조명, 모니터를 붙이고, 매 프레임 부르는 update를 돌려준다.
export function buildFactory(scene) {
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x1b2331, roughness: 0.9 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x121821, roughness: 0.85 });
  const steelMat = new THREE.MeshStandardMaterial({ color: 0x2d3646, metalness: 0.6, roughness: 0.45 });
  const yellowMat = new THREE.MeshStandardMaterial({ color: 0xf2c230, roughness: 0.6 });
  const glassMat = new THREE.MeshStandardMaterial({ color: 0x1d3556, emissive: 0x2a5a96, emissiveIntensity: 0.7, roughness: 0.2 });
  const lampMat = new THREE.MeshStandardMaterial({ color: 0xdbe9ff, emissive: 0xcfe3ff, emissiveIntensity: 1.6 });

  // 바닥: 약간 윤이 나게(roughness를 낮춤) 해서 조명과 한 방향 빛이 길게 번쩍인다. 거울처럼 비치는 반사는 아니다.
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(90, 56),
    new THREE.MeshStandardMaterial({ color: 0x161c26, roughness: 0.3, metalness: 0.25 }),
  );
  floor.rotation.x = -Math.PI / 2;                           // 평면은 기본이 서 있어서 눕힌다
  floor.receiveShadow = true;
  scene.add(floor);
  scene.add(new THREE.GridHelper(90, 22, 0x202938, 0x1d2533));   // 4m 간격의 바닥 이음선
  for (const z of [-6.2, 6.9]) {                             // 통로 안전선
    const line = box(scene, 44, 0.012, 0.1, yellowMat, 0, 0.006, z);
    line.castShadow = false;
  }

  // 뒷벽: 아래쪽 어두운 띠 + 노란 선 + 세로 리브(홈) + 창
  box(scene, 64, 12, 0.4, wallMat, 0, 6, WALL_Z);
  box(scene, 64, 1.2, 0.5, darkMat, 0, 0.6, WALL_Z + 0.05);
  box(scene, 64, 0.08, 0.52, yellowMat, 0, 1.24, WALL_Z + 0.05);
  for (let x = -30; x <= 30; x += 3) box(scene, 0.12, 10.4, 0.1, new THREE.MeshStandardMaterial({ color: 0x222c3c, roughness: 0.9 }), x, 6.4, WALL_Z + 0.25);
  for (const x of [-21, -14, 14, 21]) box(scene, 4.6, 1.5, 0.08, glassMat, x, 3.3, WALL_Z + 0.27);

  // 철골 기둥(노란 밑동 포함): 모니터 사이사이에 선다
  for (const x of [-18, -10.9, -3.7, 3.7, 10.9, 18]) {
    box(scene, 0.55, 12, 0.55, steelMat, x, 6, WALL_Z + 0.5);
    box(scene, 0.62, 0.7, 0.62, yellowMat, x, 0.35, WALL_Z + 0.5);
  }

  // 천장: 철골 보와 길쭉한 조명판. 카메라가 내려다보는 각도에서는 안 보이지만 마우스로 위를 보면 나온다.
  box(scene, 64, 0.2, 30, darkMat, 0, 11, 0);
  for (const z of [-8, -2.6, 2.6, 8]) {
    box(scene, 64, 0.4, 0.4, steelMat, 0, 10.6, z);
    for (const x of [-9, -3, 3, 9]) box(scene, 3.2, 0.12, 0.6, lampMat, x, 10.3, z);
  }

  // 조명: 하늘색 은은한 전체 빛 + 천장에서 바닥을 비추는 스포트라이트 3개 (바닥에 빛 웅덩이가 생긴다)
  scene.add(new THREE.HemisphereLight(0x4a6fa5, 0x141922, 0.55));
  for (const x of [-4.5, 0, 4.5]) {
    const spot = new THREE.SpotLight(0xd8e8ff, 240, 0, 0.8, 0.9, 2);
    spot.position.set(x, 9.6, 0);
    spot.target.position.set(x, 0, 0);
    scene.add(spot, spot.target);
  }

  // 벽 모니터 3대
  const screens = [-7.2, 0, 7.2].map((x) => makeScreen(scene, x, 3.5, WALL_Z + 0.35));
  const histories = new Map();                              // 설비 이름 -> 최근 온도 값들
  let lastDraw = -Infinity;

  return {
    // now: 시각(초). getData: 다시 그릴 때만 불러서 값을 받는다 (매 프레임 만들지 않으려고 함수로 받음).
    //   getData() => { robots: [{name, status, online, temp}], kpi, openAlarms, alarms, shipped, agvLabel }
    update(now, getData) {
      if (now - lastDraw < REFRESH_SECONDS) return;
      lastDraw = now;
      const d = getData();
      for (const r of d.robots) {
        if (r.temp !== null) histories.set(r.name, pushHistory(histories.get(r.name) ?? [], r.temp, HISTORY_MAX));
      }
      drawOverview(screens[0].g, d);
      drawStatus(screens[1].g, d);
      drawTrend(screens[2].g, d, histories);
      for (const s of screens) s.texture.needsUpdate = true;
    },
    // 시험과 점검용: index번째 모니터에 지금 그려진 그림을 PNG 주소로 돌려준다
    screenImage: (index) => screens[index].g.canvas.toDataURL("image/png"),
  };
}
