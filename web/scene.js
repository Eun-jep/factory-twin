// 3D 장면: Three.js로 로봇 팔 3대와 컨베이어를 그리고, 서버가 보낸 값대로 움직이게 한다.
//
// Three.js를 처음 본다면 이 다섯 가지만 알면 된다.
//   1. Scene(장면)    3D 물체를 올려 두는 무대
//   2. Camera(카메라) 어디서 보는지
//   3. Renderer       장면을 화면의 <canvas>에 그리는 일꾼
//   4. Mesh           물체 하나 = 모양(Geometry) + 재질(Material)
//   5. 애니메이션 루프  매 프레임(약 1/60초)마다 값을 고치고 다시 그리는 반복
//
// 이 프로젝트의 핵심 아이디어는 '부모-자식'이다.
//   어깨 그룹(shoulder)을 돌리면 그 안에 든 기둥, 팔꿈치, 팔, 집게가 전부 같이 돈다.
//   팔꿈치 그룹(elbow)을 돌리면 팔과 집게만 같이 돈다.
//   그래서 각 관절 그룹의 회전값 하나만 바꾸면 로봇 팔 전체가 자연스럽게 움직인다.
//
// 외형은 전부 Three.js 기본 도형(상자, 원기둥, 구, 도넛)을 조합해서 만든다. 외부 모델 파일은 쓰지 않는다.
// 계산(각도 변환, 부드럽게 따라가기, 색 고르기, 경광등 깜빡임)은 robot_math.js에 따로 있다.

import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import {
  robotColor, joint1ToYaw, joint2ToPitch, smoothTowards, advanceCrate, fitDistance,
  beaconState, rollerAngle, laneHalfLength, dashXs, projectedHalfWidth, ndcToScreen,
  robotKind, scaraPose, SCARA_L1, SCARA_L2, cellPlacement, columnCount, laneZ, ROW_Z,
} from "./robot_math.js";
import { buildFactory } from "./factory_env.js";
import {
  STACK_CAPACITY, PALLET_OFFSET_X, LOAD_TIME, UNLOAD_TIME, AGV_PHASE_LABELS,
  isHolding, stackUpdate, agvInit, agvStep, slotOffset, dropOffset, crateFlight, palletX,
} from "./agv_logic.js";

const SPACING = 4.5;                 // 로봇 사이 간격
const CONVEYOR_LENGTH = 3.4;         // 컨베이어 길이
const ROLLER_COUNT = 14;             // 컨베이어 롤러 개수
const ROLLER_RADIUS = 0.06;
const STATUS_LABELS = ["정상", "주의", "이상"];

const FENCE_X = 2.2;                 // 안전 펜스: 좌우 끝(로봇 가운데 기준)
const FENCE_BACK = -1.95;            // 안전 펜스: 뒤쪽(컨베이어 쪽)
const FENCE_FRONT = 1.75;            // 안전 펜스: 앞쪽 경계선 (펜스는 세우지 않고 바닥에 줄만 긋는다)

const PERSPECTIVE_MARGIN = 1.3;      // 카메라 거리 계산의 가로 여유 (원근으로 가까운 쪽이 커 보이는 만큼)
const CRATE = 0.36;                 // 팔레트·AGV 위 상자 한 변
const PALLET_Z = 0.55;              // 팔레트의 z 위치 (로봇 기준)
const PALLET_TOP = 0.13;            // 팔레트 윗면 높이
const DECK_TOP = 0.3;               // AGV 짐칸 윗면 높이
const DROP_TIME = 0.35;             // 상자가 팔레트에 떨어지는 시간(초)
const DROP_HEIGHT = 0.9;            // 떨어지기 시작하는 높이
const BACKGROUND = 0x0d1117;         // 배경과 안개 색 (같아야 멀리서 자연스럽게 사라진다)

const robots = new Map();            // 설비 이름 -> 그 로봇의 3D 물체와 현재 값
let scene, camera, renderer, controls, lastTime = 0;
let lane = null;                     // 바닥 안내선 (점선 조각들을 담은 그룹)
let overlay = null;                  // 3D 화면 위에 겹친 HTML 층 (로봇 위 정보 카드가 여기 들어간다)
let sceneWidth = 0;                  // 3D 화면의 가로 픽셀 (카드를 작게 할지 정하는 데 쓴다)
let lastRows = 1;                    // 지난번 배치의 줄 수 (바뀔 때만 카메라를 다시 맞춘다)
let laneZNow = laneZ(3);             // 바닥 안내선(이동 로봇 길)의 z 위치. 한 줄이면 로봇 앞쪽, 두 줄이면 두 줄 사이
let agvLogic = agvInit(-laneHalfLength(3, SPACING) + 1.0);  // AGV의 '판단' 상태 (agv_logic.js가 계산)
let agvView = null;                  // AGV의 3D 물체들 (몸체, 바퀴, 짐 상자, 출하장)
let dockCard = null;                 // 출하장 정보 카드
let bloomFx = null;                  // 빛 번짐 효과 (불러오기에 성공하면 들어간다)
let bloomOn = true;                  // 사용자가 빛 번짐을 켜 두었는가
let factory = null;                  // 공장 건물과 벽 모니터 (factory_env.js)
let factoryKpi = { kpi: null, openAlarms: 0 };   // 벽 모니터 1번에 보여 줄 지표 (app.js가 알려 준다)
let factoryAlarms = [];              // 벽 모니터 1번의 '최근 알람'에 쓸 열린 알람 목록 (app.js가 알려 준다)
let shipped = 0;                     // 출하장에 내려놓은 상자 수 (화면을 연 이후 누적)
const tmpPoint = new THREE.Vector3();   // 매 프레임 새로 만들지 않고 돌려 쓰는 임시 점

// ---- 1. 준비: 장면, 카메라, 조명, 바닥 ----------------------------------------
// options.onBloomReady(사용 가능 여부): 빛 번짐 파일을 불러온 결과를 알려 준다 (app.js가 스위치를 켜거나 끄는 데 쓴다)
export function initScene(container, options = {}) {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(BACKGROUND);
  scene.fog = new THREE.Fog(BACKGROUND, 24, 70);            // 멀리 있는 것은 어둠 속으로 흐려진다 (깊이감). 뒷벽의 모니터는 보이게 먼 곳부터

  camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);   // 시야각 45도, 비율은 resize()에서 맞춘다
  camera.position.set(1.6, 8.0, 10.5);                    // 오른쪽 앞 위에서 살짝 비스듬히 내려다본다 (정면보다 입체적)

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;                        // 그림자를 그린다 (빛이 막힌 곳이 어두워진다)
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;         // 그림자 가장자리를 부드럽게
  container.append(renderer.domElement);
  overlay = document.createElement("div");                  // 캔버스 위에 겹치는 투명한 층. 마우스는 아래 캔버스로 통과시킨다
  overlay.className = "scene-cards";
  container.append(overlay);

  // 조명이 없으면 재질이 있어도 검게 보인다. 전체를 은은하게 밝히는 빛 + 한 방향에서 오는 빛.
  // 한 방향 빛(sun)만 그림자를 만든다.
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xffffff, 2.4);
  sun.position.set(5, 10, 6);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -12, right: 12, top: 7, bottom: -7, near: 1, far: 30 });
  sun.shadow.bias = -0.0005;                                // 그림자 표면의 줄무늬 방지
  scene.add(sun);

  // 공장 건물: 바닥, 뒷벽, 기둥, 천장 조명, 벽 모니터 (factory_env.js)
  factory = buildFactory(scene);
  rebuildLane(3);
  agvView = buildAgv();
  dockCard = makeDockCard();

  // 빛 번짐(후처리)은 따로 불러온다. 불러오지 못해도(파일 없음, 그래픽 미지원) 빛 번짐만 빠지고 나머지는 정상이다.
  import("./bloom.js")
    .then((module) => {
      bloomFx = module.createBloom(renderer, scene, camera, container.clientWidth || 1, container.clientHeight || 1);
      if (options.onBloomReady) options.onBloomReady(true);
    })
    .catch((err) => {
      console.warn("빛 번짐을 켜지 못했습니다 (나머지 화면은 정상):", err);
      if (options.onBloomReady) options.onBloomReady(false);
    });

  // 마우스로 돌리고 확대하는 조작 (왼쪽 버튼 회전, 휠 확대, 오른쪽 버튼 이동)
  controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 1.9, 0);                           // 카메라가 바라보는 점 (로봇 가운데보다 약간 위: 뒷벽 모니터가 더 보이게)
  controls.enableDamping = true;                            // 놓은 뒤에도 부드럽게 멈춘다
  controls.maxPolarAngle = Math.PI / 2.05;                  // 바닥 밑으로 내려가지 못하게

  // 화면 크기가 바뀌면 캔버스와 카메라 비율을 맞춘다
  new ResizeObserver(() => resize(container)).observe(container);
  resize(container);

  requestAnimationFrame(animate);
}

function resize(container) {
  const w = container.clientWidth, h = container.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h);
  if (bloomFx) bloomFx.setSize(w, h);                        // 후처리 그림판도 같은 크기로
  sceneWidth = w;
  updateCompact();
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  fitCamera();
}

// 정보 카드는 화면이 좁거나 로봇이 4대 이상이면 작게 한다 (이름·상태·온도만). 카드끼리 겹치지 않게.
function updateCompact() {
  overlay.classList.toggle("compact", sceneWidth < 700 || robots.size > 3);
}

// 화면 모양이 달라져도 로봇이 잘리지 않게, 보는 방향은 그대로 두고 카메라 거리만 맞춘다.
// (마우스로 돌려 둔 각도는 유지되고, 확대한 정도만 처음으로 돌아간다)
function fitCamera() {
  const twoRows = robots.size > 3;                             // 4대부터는 두 줄 (cellPlacement)
  const offset = camera.position.clone().sub(controls.target);
  const yaw = Math.atan2(offset.x, offset.z);                  // 카메라가 정면에서 옆으로 비껴 있는 각도
  const halfWidth = laneHalfLength(laneColumns(robots.size), SPACING) + 0.4;   // 안내선 양 끝(출하장과 AGV 대기 위치)까지 들어오게
  const halfDepth = twoRows ? ROW_Z + 2.2 : 3.0;               // 두 줄이면 가운데 길에서 양쪽 줄 끝까지
  const halfHeight = twoRows ? 5.0 : 3.0;                      // 로봇 높이와 위쪽 정보 카드 (두 줄이면 앞 줄이 아래로 내려온다)
  // 비스듬히 보면 가까운 쪽 로봇이 더 크게 보이므로 폭에 여유(PERSPECTIVE_MARGIN)를 곱한다
  const wide = projectedHalfWidth(halfWidth, halfDepth, yaw) * PERSPECTIVE_MARGIN;
  const distance = Math.max(7, fitDistance(camera.aspect, wide, halfHeight, camera.fov));
  offset.setLength(distance);
  camera.position.copy(controls.target).add(offset);
}

// ---- 2. 부품 만드는 도우미 -------------------------------------------------------
// 도형 하나를 만들어 parent에 붙이고, 그림자를 만들도록 켠다.
function part(parent, geometry, material, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

// 노랑·검정 대각선 줄무늬 (공장에서 '위험, 가까이 오지 마시오'를 뜻하는 무늬). 한 번만 만들어 같이 쓴다.
let hazardTexture = null;
function getHazardTexture() {
  if (hazardTexture) return hazardTexture;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#f2c230";
  ctx.fillRect(0, 0, 64, 64);
  ctx.fillStyle = "#1a1d24";
  for (let i = -64; i < 128; i += 32) {                      // 기울어진 검정 띠를 32칸 간격으로
    ctx.beginPath();
    ctx.moveTo(i, 64);
    ctx.lineTo(i + 16, 64);
    ctx.lineTo(i + 80, 0);
    ctx.lineTo(i + 64, 0);
    ctx.closePath();
    ctx.fill();
  }
  hazardTexture = new THREE.CanvasTexture(canvas);
  hazardTexture.wrapS = hazardTexture.wrapT = THREE.RepeatWrapping;
  hazardTexture.repeat.set(10, 1);                           // 둘레를 따라 10번 반복
  hazardTexture.colorSpace = THREE.SRGBColorSpace;
  return hazardTexture;
}

// ---- 3. 로봇 한 대 만들기 ------------------------------------------------------
function createRobot(name) {
  const group = new THREE.Group();                          // 이 로봇의 모든 부품을 담는 상자
  // bodyMat만 상태에 따라 색이 바뀐다. 나머지 재질은 늘 같은 색.
  const bodyMat = new THREE.MeshStandardMaterial({ color: robotColor(0, false), metalness: 0.3, roughness: 0.5 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x2a3140, metalness: 0.6, roughness: 0.4 });
  const steelMat = new THREE.MeshStandardMaterial({ color: 0x8b95a8, metalness: 0.8, roughness: 0.35 });
  const yellowMat = new THREE.MeshStandardMaterial({ color: 0xf2c230, roughness: 0.6 });

  // 받침 (움직이지 않음): 경고 줄무늬 띠를 두른 넓은 판 + 그 위의 어두운 원통
  const stripedBase = part(
    group,
    new THREE.CylinderGeometry(1.0, 1.05, 0.14, 40),
    [new THREE.MeshStandardMaterial({ map: getHazardTexture(), roughness: 0.6 }), darkMat, darkMat],  // 옆면, 윗면, 아랫면
    0, 0.07, 0,
  );
  stripedBase.name = "hazard-base";
  part(group, new THREE.CylinderGeometry(0.7, 0.8, 0.16, 32), darkMat, 0, 0.22, 0);
  for (let i = 0; i < 8; i++) {                              // 받침 윗면의 볼트 8개
    const a = (i / 8) * Math.PI * 2;
    part(group, new THREE.CylinderGeometry(0.04, 0.04, 0.03, 8), steelMat, Math.cos(a) * 0.62, 0.31, Math.sin(a) * 0.62);
  }

  // 어깨 (joint1): 이 그룹을 위아래 축(y)으로 돌린다. 아래에 붙인 것이 전부 같이 돈다.
  // 팔 모양은 두 가지다. 같은 3가지 값(어깨, 팔꿈치, 집게)으로 움직이고, 어떤 모양인지는 이름이 정한다(robotKind).
  const kind = robotKind(name);
  const shoulder = new THREE.Group();
  shoulder.position.y = 0.3;
  group.add(shoulder);
  const elbow = new THREE.Group();
  const hand = new THREE.Group();
  if (kind === "scara") {
    // 스카라(SCARA) 팔: 짧고 굵은 기둥 위에서 마디 두 개가 수평으로 돈다. 집게는 아래로 내려갔다 올라온다.
    part(shoulder, new THREE.CylinderGeometry(0.46, 0.46, 0.2, 28), darkMat, 0, 0.1, 0);              // 아래 받침 고리
    part(shoulder, new THREE.CylinderGeometry(0.34, 0.42, 1.0, 24), bodyMat, 0, 0.7, 0);               // 기둥
    part(shoulder, new THREE.CylinderGeometry(0.4, 0.4, 0.18, 28), darkMat, 0, 1.25, 0);               // 기둥 윗부분
    part(shoulder, new THREE.BoxGeometry(SCARA_L1 + 0.1, 0.2, 0.32), bodyMat, SCARA_L1 / 2, 1.4, 0);   // 첫째 마디
    part(shoulder, new THREE.CylinderGeometry(0.24, 0.24, 0.28, 24), darkMat, 0, 1.4, 0);              // 어깨 관절
    part(shoulder, new THREE.CylinderGeometry(0.21, 0.21, 0.3, 24), darkMat, SCARA_L1, 1.4, 0);        // 팔꿈치 관절
    elbow.position.set(SCARA_L1, 1.4, 0);                                                              // 둘째 마디 (이 그룹을 y축으로 돌린다)
    part(elbow, new THREE.BoxGeometry(SCARA_L2 + 0.1, 0.18, 0.28), bodyMat, SCARA_L2 / 2, -0.14, 0);   // 둘째 마디
    part(elbow, new THREE.CylinderGeometry(0.19, 0.19, 0.26, 24), darkMat, SCARA_L2, -0.14, 0);        // 끝 관절
    hand.position.set(SCARA_L2, -0.3, 0);                                                              // 높이는 매 프레임 내려갔다 올라온다
    part(hand, new THREE.CylinderGeometry(0.05, 0.05, 0.9, 12), steelMat, 0, 0.45, 0);                 // 위아래로 움직이는 축
    part(hand, new THREE.BoxGeometry(0.14, 0.14, 0.62), steelMat, 0, -0.02, 0);                        // 손바닥
  } else {
    // 관절 팔: 높은 기둥 꼭대기의 팔꿈치(joint2)를 옆 축(z)으로 돌려 팔을 아래로 숙인다.
    part(shoulder, new THREE.CylinderGeometry(0.4, 0.4, 0.25, 28), darkMat, 0, 0.12, 0);       // 아래 관절 몸통
    part(shoulder, new THREE.CylinderGeometry(0.28, 0.34, 1.7, 24), bodyMat, 0, 1.05, 0);       // 기둥
    part(shoulder, new THREE.TorusGeometry(0.31, 0.035, 10, 32), steelMat, 0, 0.55, 0).rotation.x = Math.PI / 2;   // 기둥 가운데 띠
    part(shoulder, new THREE.CylinderGeometry(0.34, 0.34, 0.22, 28), darkMat, 0, 1.98, 0);      // 위 관절 몸통
    elbow.position.y = 2.1;
    part(elbow, new THREE.CylinderGeometry(0.34, 0.34, 0.62, 28), darkMat).rotation.x = Math.PI / 2;     // 옆으로 누운 관절 통
    part(elbow, new THREE.CylinderGeometry(0.17, 0.17, 0.7, 16), steelMat).rotation.x = Math.PI / 2;     // 축 머리 (양쪽으로 살짝 튀어나옴)
    // 팔: 팔꿈치에서 바깥(+x)으로 뻗는 원기둥. 끝으로 갈수록 가늘다.
    // 원기둥은 기본이 세로(y축)라서 눕히고, 위쪽(radiusTop)이 +x(바깥)로 가도록 -90도 돌린다.
    part(elbow, new THREE.CylinderGeometry(0.13, 0.19, 2.2, 20), bodyMat, 1.25, 0, 0).rotation.z = -Math.PI / 2;
    part(elbow, new THREE.CylinderGeometry(0.2, 0.2, 0.2, 20), darkMat, 0.5, 0, 0).rotation.z = -Math.PI / 2;  // 팔 중간 마디
    hand.position.x = 2.4;
    part(hand, new THREE.CylinderGeometry(0.14, 0.14, 0.18, 16), darkMat, -0.05, 0, 0).rotation.z = Math.PI / 2;   // 손목
    part(hand, new THREE.BoxGeometry(0.14, 0.14, 0.62), steelMat, 0.06, -0.02, 0);                                  // 손바닥 (손가락이 매달리는 가로대)
  }
  shoulder.add(elbow);
  elbow.add(hand);

  // 집게: 팔 끝의 두 손가락. 벌어짐은 z 위치로 표현한다.
  const fingerGeometry = new THREE.BoxGeometry(0.1, 0.5, 0.1);
  const fingerA = part(hand, fingerGeometry, darkMat, 0, -0.3, 0);
  const fingerB = part(hand, fingerGeometry, darkMat, 0, -0.3, 0);

  // 컨베이어(상자를 집는 쪽, -z)와 팔레트(상자를 놓는 쪽, +z)
  // 롤러 컨베이어: 양옆 틀 + 가로로 놓인 롤러 여러 개 + 다리. 롤러는 속도에 맞춰 돈다.
  const beltZ = -0.9;
  for (const side of [-1, 1]) {
    part(group, new THREE.BoxGeometry(CONVEYOR_LENGTH + 0.1, 0.12, 0.06), darkMat, 0, 0.27, beltZ + side * 0.33);   // 옆 틀
    for (const end of [-1, 1]) {
      part(group, new THREE.BoxGeometry(0.08, 0.21, 0.08), darkMat, end * (CONVEYOR_LENGTH / 2 - 0.1), 0.105, beltZ + side * 0.33);  // 다리
    }
  }
  const rollers = [];
  const rollerSpacing = CONVEYOR_LENGTH / ROLLER_COUNT;
  const rollerBody = new THREE.CylinderGeometry(ROLLER_RADIUS, ROLLER_RADIUS, 0.6, 10);
  const stripe = new THREE.BoxGeometry(0.03, 0.012, 0.58);   // 롤러 겉면의 밝은 줄: 돌아가는 모습이 보이게
  for (let i = 0; i < ROLLER_COUNT; i++) {
    const roller = new THREE.Group();                         // 이 그룹을 z축으로 돌린다
    roller.position.set(-CONVEYOR_LENGTH / 2 + rollerSpacing * (i + 0.5), 0.29, beltZ);
    part(roller, rollerBody, steelMat).rotation.x = Math.PI / 2;   // 원기둥을 눕혀 z 방향 축으로
    part(roller, stripe, yellowMat, 0, ROLLER_RADIUS, 0);
    group.add(roller);
    rollers.push(roller);
  }

  // 팔레트: 위판 4장 + 밑받침 3개
  const palletMat = new THREE.MeshStandardMaterial({ color: 0x6a5538, roughness: 0.9 });
  const pallet = new THREE.Group();
  pallet.position.set(PALLET_OFFSET_X, 0, PALLET_Z);
  group.add(pallet);
  for (const z of [-0.45, -0.15, 0.15, 0.45]) part(pallet, new THREE.BoxGeometry(1.2, 0.05, 0.24), palletMat, 0, 0.105, z);
  for (const x of [-0.5, 0, 0.5]) part(pallet, new THREE.BoxGeometry(0.1, 0.08, 1.2), palletMat, x, 0.04, 0);

  // 컨베이어 위에서 움직이는 상자 3개 (가운데 테이프 줄 포함)
  const crateMat = new THREE.MeshStandardMaterial({ color: 0xc8a06a, roughness: 0.8 });
  const tapeMat = new THREE.MeshStandardMaterial({ color: 0x8a6a3c, roughness: 0.8 });

  // 팔레트 위에 쌓이는 상자 (처음엔 모두 숨겨 두고, 서버의 생산 카운트가 늘 때마다 하나씩 보인다)
  // 손에 든 상자: 집게에 매달려 팔을 따라 움직인다 (집기~놓기 단계 동안만 보인다)
  const stackGeometry = new THREE.BoxGeometry(CRATE, CRATE, CRATE);
  const stackCrates = [0, 1, 2, 3].map((k) => {
    const { dx, dz } = slotOffset(k, 0.5);
    const crate = part(pallet, stackGeometry, crateMat, dx, PALLET_TOP + CRATE / 2, dz);
    crate.visible = false;
    return crate;
  });
  const heldCrate = part(hand, stackGeometry, crateMat, 0, -0.45, 0);
  heldCrate.visible = false;
  const crates = [0, 1, 2].map((i) => {
    const crate = part(group, new THREE.BoxGeometry(0.42, 0.42, 0.42), crateMat,
      -CONVEYOR_LENGTH / 2 + (i * CONVEYOR_LENGTH) / 3, 0.56, beltZ);
    part(crate, new THREE.BoxGeometry(0.06, 0.43, 0.43), tapeMat);
    return crate;
  });

  // 안전 펜스: 뒤와 양옆에만 세우고, 앞(카메라 쪽)은 시야를 위해 비워 바닥에 노란 선만 긋는다.
  const postGeometry = new THREE.BoxGeometry(0.08, 1.2, 0.08);
  const corners = [[-FENCE_X, FENCE_BACK], [FENCE_X, FENCE_BACK], [-FENCE_X, FENCE_FRONT], [FENCE_X, FENCE_FRONT]];
  for (const [x, z] of corners) part(group, postGeometry, yellowMat, x, 0.6, z);
  for (const y of [0.5, 1.1]) {
    part(group, new THREE.BoxGeometry(FENCE_X * 2, 0.05, 0.05), yellowMat, 0, y, FENCE_BACK);                       // 뒤쪽 가로대
    for (const side of [-1, 1]) {
      part(group, new THREE.BoxGeometry(0.05, 0.05, FENCE_FRONT - FENCE_BACK), yellowMat,
        side * FENCE_X, y, (FENCE_FRONT + FENCE_BACK) / 2);                                                          // 옆쪽 가로대
    }
  }
  const floorLine = part(group, new THREE.BoxGeometry(FENCE_X * 2, 0.012, 0.07), yellowMat, 0, 0.006, FENCE_FRONT);
  floorLine.castShadow = false;

  // 경광등: 뒤쪽 오른쪽 기둥 위에 올린다. 색과 깜빡임은 설비 상태를 따른다 (animate에서 갱신).
  part(group, new THREE.CylinderGeometry(0.05, 0.05, 0.4, 10), darkMat, FENCE_X, 1.4, FENCE_BACK);   // 기둥을 더 높게 연장
  const lampMat = new THREE.MeshStandardMaterial({ color: 0x3a4254, emissive: 0x3a4254, emissiveIntensity: 0, roughness: 0.3 });
  part(group, new THREE.CylinderGeometry(0.13, 0.13, 0.3, 20), lampMat, FENCE_X, 1.75, FENCE_BACK);   // 불이 켜지는 통
  part(group, new THREE.CylinderGeometry(0.15, 0.15, 0.05, 20), darkMat, FENCE_X, 1.92, FENCE_BACK);  // 윗 뚜껑
  const lampLight = new THREE.PointLight(0xffffff, 0, 5, 2);                                          // 주변 바닥에 번지는 빛
  lampLight.position.set(FENCE_X, 1.75, FENCE_BACK + 0.4);
  group.add(lampLight);

  // 머리 위 정보 카드 (3D 물체가 아니라 HTML 요소. 매 프레임 로봇의 화면 위치로 옮긴다)
  const card = makeInfoCard(name);

  scene.add(group);
  return {
    name, group, shoulder, elbow, hand, fingerA, fingerB, bodyMat, crates, card, kind, pallet,
    cardY: kind === "scara" ? 2.7 : 3.2,                     // 정보 카드가 붙는 높이 (스카라가 더 낮다)
    stackCrates, heldCrate,
    stackState: null,                                        // {stacked, lastCount}: 팔레트에 쌓인 수 (agv_logic.stackUpdate)
    dropT: [Infinity, Infinity, Infinity, Infinity],         // 각 상자가 놓인 뒤 흐른 시간 (떨어지는 동안만 작은 값)
    phase: 0, count: 0,
    info: { temp: null, phase: "-", availability: null },     // 카드에 보여 줄 값 (서버가 보낸 것)
    rollers, rollerAngle: 0, lampMat, lampLight, beacon: { color: 0, level: 0 },
    // target = 서버가 보낸 값(목표), current = 지금 화면에 그려진 값. current가 target을 따라간다.
    target: { yaw: 0, pitch: joint2ToPitch(20), grip: 0 },
    current: { yaw: 0, pitch: joint2ToPitch(20), grip: 0 },
    status: 0, online: false, conveyor: 0, joint1: 90, joint2: 20,
    seen: false,                                             // 값을 한 번이라도 받았는가
  };
}

// 정보 카드: 3D 물체가 아니라 평범한 HTML 요소다. 글자가 선명하고, 모양은 CSS(style.css의 .rcard)가 정한다.
// 글자는 textContent로만 넣는다 (서버 값이 HTML로 해석되지 않게).
function makeInfoCard(name) {
  const span = (cls, text = "") => {
    const node = document.createElement("span");
    node.className = cls;
    node.textContent = text;
    return node;
  };
  const row = (...children) => {
    const node = document.createElement("div");
    node.className = "rcard-row";
    node.append(...children);
    return node;
  };
  const root = document.createElement("div");
  root.className = "rcard";
  const state = span("rcard-state");
  const temp = span("rcard-temp");
  const avail = span("rcard-avail");
  const phase = span("rcard-phase");
  const head = row(span("rcard-name", name), state);
  head.classList.add("rcard-head");
  const phaseRow = row(phase);
  phaseRow.classList.add("rcard-phase-row");
  root.append(head, row(temp, avail), phaseRow);
  overlay.append(root);

  let lastKey = "";
  let lastTransform = "";
  return {
    // 내용이 바뀔 때만 DOM을 고친다 (매 프레임 글자를 다시 쓰지 않는다)
    setInfo({ online, status, temp: t, availability, phase: p }) {
      const key = [online, status, t, availability, p].join("|");
      if (key === lastKey) return;
      lastKey = key;
      const level = !online ? "offline" : ["normal", "warning", "fault"][status] ?? "normal";
      root.className = "rcard s-" + level;
      state.textContent = online ? STATUS_LABELS[status] ?? "?" : "끊김";
      temp.textContent = t === null ? "온도 -" : `온도 ${t.toFixed(1)}℃`;
      avail.textContent = `가동률 ${availability === null ? "-" : (availability * 100).toFixed(1) + "%"}`;
      phase.textContent = online ? `단계 ${p}` : "통신 끊김 · 값은 오래됨";
    },
    // 화면 좌표(픽셀)로 옮긴다. 카드의 아래 가운데가 그 점에 오게 한다. 시야 밖이면 숨긴다.
    place(x, y, visible) {
      root.style.visibility = visible ? "visible" : "hidden";
      const transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) translate(-50%, -100%)`;
      if (transform === lastTransform) return;
      lastTransform = transform;
      root.style.transform = transform;
    },
  };
}

function getRobot(name) {
  let robot = robots.get(name);
  if (!robot) {
    robot = createRobot(name);
    robots.set(name, robot);
    layout();                                                // 새 로봇이 생기면 가운데 정렬로 다시 배치
  }
  return robot;
}

function layout() {
  const names = [...robots.keys()].sort();
  names.forEach((name, i) => {
    const cell = cellPlacement(i, names.length, SPACING);   // 한 줄 또는 두 줄 배치 (두 줄이면 앞 줄은 뒤돌려 길을 마주 본다)
    const group = robots.get(name).group;
    group.position.set(cell.x, 0, cell.z);
    group.rotation.y = cell.rot;
  });
  laneZNow = laneZ(names.length);
  const rows = names.length > 3 ? 2 : 1;
  updateCompact();
  if (rows !== lastRows) { lastRows = rows; fitCamera(); }   // 한 줄에서 두 줄이 되면 카메라 거리를 다시 맞춘다
  rebuildLane(names.length);                                 // 로봇 수가 바뀌면 안내선 길이와 위치도 맞춘다
}

// 바닥 안내선: 로봇 셀 앞쪽에 빛나는 점선을 깐다. 나중에 이동 로봇(AGV)이 이 선을 따라 다닌다.
// 점선 조각의 위치 계산은 robot_math.js(dashXs)가 한다.
function rebuildLane(count) {
  const columns = laneColumns(count);
  if (lane) scene.remove(lane);                              // 조각은 공유 도형을 쓰므로 따로 해제할 것이 없다
  lane = new THREE.Group();
  const dash = new THREE.Mesh(
    laneDash.geometry,
    laneDash.material,
  );
  for (const x of dashXs(laneHalfLength(columns, SPACING))) {
    const piece = dash.clone();
    piece.position.set(x, 0.006, laneZNow);
    lane.add(piece);
  }
  scene.add(lane);
}
// 안내선·카메라가 기준으로 삼는 열 수 (로봇이 적어도 3열 자리는 잡는다)
function laneColumns(count) {
  return Math.max(3, columnCount(count));
}

const laneDash = {
  geometry: new THREE.BoxGeometry(0.5, 0.01, 0.09),
  material: new THREE.MeshStandardMaterial({ color: 0x0b2a2e, emissive: 0x2dd4bf, emissiveIntensity: 0.9 }),
};

// ---- 4. 서버 값을 받아 목표를 바꾼다 (app.js가 부른다) -----------------------------
// 여기서는 '목표'만 바꾼다. 실제 움직임은 아래 animate()가 부드럽게 따라간다.
export function updateRobot(name, s) {
  const r = getRobot(name);
  r.target.yaw = joint1ToYaw(s.joint1);
  r.target.pitch = joint2ToPitch(s.joint2);
  r.target.grip = s.gripper ? 1 : 0;
  r.status = s.status;
  r.conveyor = s.conveyor;
  r.joint1 = s.joint1;
  r.joint2 = s.joint2;
  r.info.temp = s.temp;
  r.info.phase = s.phase_name;
  r.phase = s.phase;
  r.count = s.count;
  // 생산 카운트가 늘면 그만큼 팔레트에 상자가 쌓인다 (새로 쌓인 상자는 위에서 떨어진다)
  const stack = stackUpdate(r.stackState, s.count);
  for (let i = 0; i < stack.added; i++) r.dropT[stack.stacked - 1 - i] = 0;
  r.stackState = { stacked: stack.stacked, lastCount: stack.lastCount };
  if (!r.seen) {
    // 처음 받은 값은 그 자세로 바로 맞춘다. (기본 자세에서 출발해 따라가면, 페이지를 연 시점에
    // 이미 통신이 끊긴 로봇은 마지막 자세를 영영 보여 주지 못한다)
    r.current.yaw = r.target.yaw;
    r.current.pitch = r.target.pitch;
    r.current.grip = r.target.grip;
    r.seen = true;
  }
}

// 정보 카드에 보여 줄 가동률 (0~1, 아직 없으면 null). app.js가 지표를 받을 때 부른다.
export function setRobotKpi(name, availability) {
  getRobot(name).info.availability = availability;
}

// 통신이 끊기면 로봇을 회색으로 만들고 컨베이어를 멈춘다 (오래된 값을 현재 값처럼 보이지 않게)
export function setRobotOnline(name, online) {
  getRobot(name).online = online;
}

// 시험과 점검용: 지금 장면에 있는 로봇들의 값을 숫자로 돌려준다
export function getRobotStates() {
  return [...robots.values()].map((r) => ({
    name: r.name, online: r.online, status: r.status, x: r.group.position.x,
    yaw: r.current.yaw, pitch: r.current.pitch, grip: r.current.grip,
    targetYaw: r.target.yaw, targetPitch: r.target.pitch,
    color: r.bodyMat.color.getHex(), joint1: r.joint1, joint2: r.joint2,
    crateX: r.crates.map((c) => c.position.x),
    beaconColor: r.beacon.color, beaconLevel: r.beacon.level, rollerAngle: r.rollerAngle,
    count: r.count, phase: r.phase, stacked: r.stackState ? r.stackState.stacked : 0, holding: r.heldCrate.visible,
  }));
}

// 시험과 점검용: AGV의 지금 상태와 출하 누적 수
export function getAgvState() {
  return { ...agvLogic, shipped };
}

// ---- AGV(이동 로봇)와 출하장 -----------------------------------------------------
// 판단(언제 어디로 가고 언제 싣는가)은 agv_logic.js가 하고, 여기서는 그 결과를 도형에 적용만 한다.
const WHEEL_RADIUS = 0.1;
const DECK_SPACING = 0.4;            // AGV 짐칸 위 상자 간격
const tmpFrom = new THREE.Vector3(), tmpTo = new THREE.Vector3();

function buildAgv() {
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x2f3a4c, metalness: 0.5, roughness: 0.45 });
  const trimMat = new THREE.MeshStandardMaterial({ color: 0x2dd4bf, emissive: 0x2dd4bf, emissiveIntensity: 0.8 });
  const wheelMat = new THREE.MeshStandardMaterial({ color: 0x14181f, roughness: 0.8 });
  const crateMat = new THREE.MeshStandardMaterial({ color: 0xc8a06a, roughness: 0.8 });

  const body = new THREE.Group();                            // 이 그룹을 x로 옮기면 AGV 전체가 간다
  part(body, new THREE.BoxGeometry(1.15, 0.16, 0.9), darkMat, 0, 0.19, 0);                // 몸체
  part(body, new THREE.BoxGeometry(1.0, 0.04, 0.8), darkMat, 0, DECK_TOP - 0.02, 0);      // 짐칸 바닥
  part(body, new THREE.BoxGeometry(0.05, 0.03, 0.9), trimMat, 0.575, 0.2, 0).castShadow = false;    // 앞뒤 불빛 띠
  part(body, new THREE.BoxGeometry(0.05, 0.03, 0.9), trimMat, -0.575, 0.2, 0).castShadow = false;
  const wheels = [];
  for (const sx of [-0.4, 0.4]) {
    for (const sz of [-0.47, 0.47]) {
      const wheel = new THREE.Group();                        // 바퀴 그룹을 z축으로 돌린다
      part(wheel, new THREE.CylinderGeometry(WHEEL_RADIUS, WHEEL_RADIUS, 0.1, 14), wheelMat).rotation.x = Math.PI / 2;
      part(wheel, new THREE.BoxGeometry(0.03, 0.2, 0.11), trimMat).castShadow = false;       // 돌아가는 모습이 보이는 줄
      wheel.position.set(sx, WHEEL_RADIUS, sz);
      body.add(wheel);
      wheels.push(wheel);
    }
  }
  body.position.set(agvLogic.x, 0, laneZNow);
  scene.add(body);

  // 짐 상자 4개: 로봇 팔레트에서 날아오는 동안에도 쓰므로 AGV 몸체가 아니라 장면에 직접 둔다
  const crateGeometry = new THREE.BoxGeometry(CRATE, CRATE, CRATE);
  const cargo = [0, 1, 2, 3].map(() => {
    const crate = part(scene, crateGeometry, crateMat);
    crate.visible = false;
    return crate;
  });

  // 출하장: 안내선 오른쪽 끝의 청록 테두리 판
  const dock = new THREE.Group();
  part(dock, new THREE.BoxGeometry(1.4, 0.03, 1.2), trimMat, 0, 0.015, 0).castShadow = false;
  part(dock, new THREE.BoxGeometry(1.3, 0.05, 1.1), darkMat, 0, 0.045, 0);
  dock.position.set(0, 0, laneZNow);
  scene.add(dock);

  return { body, wheels, cargo, dock, lastX: agvLogic.x };
}

// 출하장 정보 카드: 누적 출하 수와 AGV의 지금 상태
function makeDockCard() {
  const span = (cls, text = "") => { const n = document.createElement("span"); n.className = cls; n.textContent = text; return n; };
  const row = (...kids) => { const n = document.createElement("div"); n.className = "rcard-row"; n.append(...kids); return n; };
  const root = document.createElement("div");
  root.className = "rcard s-normal";
  const total = span("rcard-state");
  const agvState = span("rcard-phase");
  const head = row(span("rcard-name", "출하장"), total);
  head.classList.add("rcard-head");
  const stateRow = row(span("rcard-temp", "AGV"), agvState);
  stateRow.classList.add("rcard-phase-row");
  root.append(head, stateRow);
  overlay.append(root);
  let lastKey = "", lastTransform = "";
  return {
    set(count, label) {
      const key = count + "|" + label;
      if (key === lastKey) return;
      lastKey = key;
      total.textContent = `${count}상자`;
      agvState.textContent = label;
    },
    place(x, y, visible) {
      root.style.visibility = visible ? "visible" : "hidden";
      const transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) translate(-50%, -100%)`;
      if (transform === lastTransform) return;
      lastTransform = transform;
      root.style.transform = transform;
    },
  };
}

// 매 프레임: 가득 찬 팔레트를 AGV에게 알리고, AGV가 계산한 결과(위치, 싣는 중, 내리는 중)를 도형에 적용한다.
function updateAgv(dt) {
  const half = laneHalfLength(laneColumns(robots.size), SPACING);
  const homeX = -half + 1.0, dockX = half - 1.2;
  const requests = [...robots.values()]
    .filter((r) => r.stackState && r.stackState.stacked >= STACK_CAPACITY)
    .map((r) => ({ id: r.name, x: palletX(r.group.position.x, r.group.rotation.y) }));

  const result = agvStep(agvLogic, dt, requests, homeX, dockX);
  agvLogic = result.agv;
  for (const event of result.events) {
    if (event.type === "loaded") {                           // 상자를 모두 실었다 -> 그 팔레트는 빈다
      const robot = robots.get(event.id);
      if (robot) robot.stackState = { ...robot.stackState, stacked: 0 };
    } else if (event.type === "unloaded") {
      shipped += event.count;
    }
  }

  const v = agvView;
  v.body.position.set(agvLogic.x, 0, laneZNow);              // 한 줄에서 두 줄로 바뀌면 길의 z도 바뀐다
  for (const wheel of v.wheels) wheel.rotation.z -= (agvLogic.x - v.lastX) / WHEEL_RADIUS;   // 간 거리만큼 바퀴가 구른다
  v.lastX = agvLogic.x;
  v.dock.position.set(dockX, 0, laneZNow);

  // 짐 상자: 싣는 동안은 팔레트에서 짐칸으로 날아가고(포물선), 운반 중에는 짐칸에, 내리는 동안은 작아지며 사라진다
  const target = agvLogic.phase === "loading" ? robots.get(agvLogic.targetId) : null;
  for (let k = 0; k < v.cargo.length; k++) {
    const crate = v.cargo[k];
    const deck = slotOffset(k, DECK_SPACING);
    tmpTo.set(agvLogic.x + deck.dx, DECK_TOP + CRATE / 2, laneZNow + deck.dz);
    crate.scale.setScalar(1);
    if (target) {
      const u = crateFlight(agvLogic.timer / LOAD_TIME, k);
      crate.visible = u > 0;
      if (u > 0) {
        const slot = slotOffset(k, 0.5);
        target.group.updateMatrixWorld(true);                // 뒤돌려 놓인 로봇도 맞도록 팔레트의 실제 세계 좌표로 바꾼다
        target.pallet.localToWorld(tmpFrom.set(slot.dx, PALLET_TOP + CRATE / 2, slot.dz));
        crate.position.lerpVectors(tmpFrom, tmpTo, u);
        crate.position.y += Math.sin(Math.PI * u) * 0.6;     // 위로 포물선을 그리며 날아간다
      }
    } else if (agvLogic.phase === "toDock" || agvLogic.phase === "unloading") {
      const shrink = agvLogic.phase === "unloading" ? crateFlight(agvLogic.timer / UNLOAD_TIME, k) : 0;
      crate.visible = shrink < 0.99;
      crate.position.copy(tmpTo);
      crate.scale.setScalar(Math.max(0.01, 1 - shrink));
    } else {
      crate.visible = false;
    }
  }
  dockCard.set(shipped, AGV_PHASE_LABELS[agvLogic.phase] ?? "-");
}

// 각 로봇 머리 위의 3D 점을 카메라로 찍어 화면 픽셀로 바꾼 뒤, 그 자리에 카드를 둔다.
// 카메라를 돌리거나 로봇이 움직여도 카드가 따라다니는 이유가 이것이다.
function placeCards() {
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  for (const r of robots.values()) {
    tmpPoint.set(r.group.position.x, r.cardY, r.group.position.z);
    tmpPoint.project(camera);                                // 3D 점 -> -1~1 범위의 화면 좌표
    const p = ndcToScreen(tmpPoint.x, tmpPoint.y, tmpPoint.z, w, h);
    r.card.place(p.x, p.y, p.visible);
  }
  tmpPoint.set(agvView.dock.position.x + 1.4, 1.0, laneZNow);   // 출하장 카드는 출하장 판 오른쪽 위 (앞 줄 로봇의 카드와 겹치지 않게)
  tmpPoint.project(camera);
  const d = ndcToScreen(tmpPoint.x, tmpPoint.y, tmpPoint.z, w, h);
  dockCard.place(d.x, d.y, d.visible);
}

// 벽 모니터에 보여 줄 값 모으기: 모니터를 다시 그릴 때(1초에 한 번)만 부른다
function collectFactoryData() {
  const list = [...robots.values()]
    .sort((a, b) => (a.name < b.name ? -1 : 1))
    .map((r) => ({ name: r.name, status: r.status, online: r.online, temp: r.info.temp }));
  return {
    robots: list, kpi: factoryKpi.kpi, openAlarms: factoryKpi.openAlarms, alarms: factoryAlarms,
    shipped, agvLabel: AGV_PHASE_LABELS[agvLogic.phase] ?? "-",
  };
}

// 열린 알람 목록을 벽 모니터에도 전달한다 (app.js가 1초마다 부른다)
export function setFactoryAlarms(alarms) {
  factoryAlarms = alarms;
}

// 시험과 점검용: 벽 모니터 index번(0~2)에 지금 그려진 그림 (PNG 주소)
export function getWallScreenImage(index) {
  return factory.screenImage(index);
}

// 빛 번짐 켜기/끄기 (화면 구석의 스위치가 부른다)
export function setBloom(on) {
  bloomOn = on;
}

// 지표 띠에 쓰는 값을 벽 모니터에도 전달한다 (app.js가 부른다)
export function setFactoryKpi(kpi, openAlarms) {
  factoryKpi = { kpi, openAlarms };
}

// ---- 5. 애니메이션 루프: 매 프레임 실행 ---------------------------------------------
function animate(now) {
  requestAnimationFrame(animate);                            // 다음 프레임에도 나를 불러 달라고 예약
  const dt = Math.min((now - lastTime) / 1000, 0.1);          // 지난 프레임 이후 흐른 시간(초). 탭이 멈췄다 돌아와도 튀지 않게 상한을 둔다
  lastTime = now;

  for (const r of robots.values()) {
    // 현재 값이 목표 값을 조금씩 따라간다 (끊긴 로봇은 그 자리에 멈춘다)
    if (r.online) {
      r.current.yaw = smoothTowards(r.current.yaw, r.target.yaw, dt);
      r.current.pitch = smoothTowards(r.current.pitch, r.target.pitch, dt);
      r.current.grip = smoothTowards(r.current.grip, r.target.grip, dt, 12);
    }
    // 계산된 값을 관절 그룹의 회전에 적용 -> 자식들이 같이 움직인다
    if (r.kind === "scara") {
      // 스카라: 같은 목표 지점에 닿도록 두 관절 각도를 거꾸로 계산하고(scaraPose), 집게는 아래로 내려간다
      const pose = scaraPose(r.current.yaw, r.current.pitch);
      r.shoulder.rotation.y = pose.shoulderYaw;
      r.elbow.rotation.y = pose.elbowYaw;
      r.hand.position.y = -0.3 - pose.drop;
    } else {
      r.shoulder.rotation.y = r.current.yaw;
      r.elbow.rotation.z = r.current.pitch;
    }
    const gap = 0.22 - 0.15 * r.current.grip;                // 열림 0.22, 닫힘 0.07
    r.fingerA.position.z = gap;
    r.fingerB.position.z = -gap;

    r.bodyMat.color.setHex(robotColor(r.status, r.online));  // 상태에 따라 색 바꾸기

    // 컨베이어: 설비가 보고한 속도(mm/s)대로 상자가 움직이고 롤러가 돈다. 정지(이상) 때는 속도가 0이다.
    if (r.online) {
      for (const crate of r.crates) {
        crate.position.x = advanceCrate(crate.position.x, r.conveyor, dt, CONVEYOR_LENGTH);
      }
      r.rollerAngle = rollerAngle(r.rollerAngle, r.conveyor, dt, ROLLER_RADIUS);
      for (const roller of r.rollers) roller.rotation.z = r.rollerAngle;
    }

    // 경광등: 상태에 맞는 색으로 켜지고, 주의·이상이면 깜빡인다
    const beacon = beaconState(r.status, r.online, now / 1000);
    r.beacon = beacon;
    r.lampMat.color.setHex(beacon.color);
    r.lampMat.emissive.setHex(beacon.color);
    r.lampMat.emissiveIntensity = beacon.level * 1.8;
    r.lampLight.color.setHex(beacon.color);
    r.lampLight.intensity = beacon.level * 6;

    // 손에 든 상자: 집기~놓기 단계 동안 집게에 매달려 있다
    r.heldCrate.visible = r.online && isHolding(r.phase);

    // 팔레트 위 상자: 쌓인 수만큼 보인다. AGV가 싣는 중이면 날아가기 시작한 상자는 팔레트에서 숨긴다.
    const stacked = r.stackState ? r.stackState.stacked : 0;
    const loadingProgress = agvLogic.phase === "loading" && agvLogic.targetId === r.name ? agvLogic.timer / LOAD_TIME : -1;
    for (let k = 0; k < r.stackCrates.length; k++) {
      const crate = r.stackCrates[k];
      const flying = loadingProgress >= 0 && crateFlight(loadingProgress, k) > 0;
      crate.visible = k < stacked && !flying;
      r.dropT[k] += dt;
      crate.position.y = PALLET_TOP + CRATE / 2 + dropOffset(r.dropT[k], DROP_TIME, DROP_HEIGHT);
    }

    r.card.setInfo({ online: r.online, status: r.status, ...r.info });
  }

  updateAgv(dt);
  factory.update(now / 1000, collectFactoryData);            // 벽 모니터는 1초에 한 번만 다시 그린다

  controls.update();
  camera.updateMatrixWorld();                                // 카메라가 방금 움직였을 수 있으니 위치 계산에 쓰는 행렬을 먼저 갱신
  placeCards();
  // 장면을 카메라로 찍어 캔버스에 그린다. 빛 번짐이 켜져 있으면 후처리를 거쳐서 그린다.
  if (bloomFx && bloomOn) bloomFx.render();
  else renderer.render(scene, camera);
}
