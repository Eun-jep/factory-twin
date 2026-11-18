// 이 파일이 하는 일
//   1. 서버와 같은 주소의 /ws 로 WebSocket 연결을 열고, 서버가 보내는 JSON 메시지를 받아 화면을 고친다.
//        state    전체 상태 (접속할 때와 1초마다): 설비, 알람, 지표(KPI), 주고받은 패킷. 접속할 때는 최근 이벤트도 들어 있다
//        snapshot 설비 값 하나 (0.5초마다)
//        event    통신 이벤트 하나
//        ack_result / error   내가 보낸 명령에 대한 답
//   2. 알람 "확인" 버튼을 누르면 같은 연결로 {"type":"ack"} 명령을 보낸다.
//   3. 온도 차트는 /api/history 를 5초마다 불러와서 canvas에 직접 그린다 (차트 라이브러리 없음).
//   4. 패킷 뷰어는 서버가 필드별로 해석해 보낸 요청·응답 바이트를 색으로 나눠 보여 준다.
//   5. 연결이 끊기면 1초, 2초, 4초, 8초(최대)로 늘려 가며 다시 연결한다.
//
// 화면에 글자를 넣을 때는 항상 textContent를 쓴다. innerHTML에 서버 데이터를 넣으면
// 데이터에 섞인 HTML 태그가 그대로 실행될 수 있기 때문이다.
//
// 이 파일은 <script type="module">로 불러온다 (그래서 import를 쓸 수 있다).

const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
const MAX_RETRY_MS = 8000;
const STALE_AFTER_SEC = 3;         // 이 시간보다 오래된 값은 "오래됨"으로 표시한다
const CHART_REFRESH_MS = 5000;
const MAX_EVENTS = 100;
const OPERATOR_KEY = "factory-twin.operator";

const $ = (id) => document.getElementById(id);
const connBadge = $("conn");
const clockEl = $("clock");
const devicesEl = $("devices");
const alarmsTable = $("alarms");
const alarmsBody = alarmsTable.querySelector("tbody");
const noAlarms = $("no-alarms");
const noticeEl = $("notice");
const operatorInput = $("operator");
const eventsEl = $("events");
const sceneEl = $("scene");
const sceneNote = $("scene-note");
const chartCanvas = $("chart");
const chartNote = $("chart-note");
const chartDevice = $("chart-device");
const chartMinutes = $("chart-minutes");
const packetEl = $("packet");
const packetDevice = $("packet-device");
const packetMode = $("packet-mode");

const cards = new Map();           // 설비 이름 -> 그 설비 카드의 화면 요소들
let packetInfo = null;              // 지금 고른 설비의 패킷 기록 { name, data } (/api/packet으로 가져온다)
let socket = null;

// ---- 3D 장면 불러오기 ----------------------------------------------------------
// 3D는 따로 불러온다. 그래야 3D가 안 되는 브라우저(그래픽 가속이 꺼진 곳 등)에서도
// 카드와 알람 표는 계속 동작한다.
let scene3d = null;                // 불러오기에 성공하면 scene.js의 함수들이 들어간다

// 빛 번짐 스위치: 파일을 불러왔을 때만 켤 수 있고, 사용자의 선택은 기억한다 (저장이 막힌 환경에서도 동작해야 한다)
const bloomToggle = $("bloom-toggle");
const bloomLabel = $("bloom-label");
function setupBloomToggle(available) {
  if (!available) {
    bloomToggle.checked = false;
    bloomLabel.textContent = "빛 번짐 (사용 불가)";
    return;                                                  // disabled 그대로: 누를 수 없다
  }
  bloomToggle.disabled = false;
  const saved = loadSaved("bloom");
  bloomToggle.checked = saved !== "off";                      // 기본은 켜짐
  scene3d.setBloom(bloomToggle.checked);
  bloomToggle.addEventListener("change", () => {
    scene3d.setBloom(bloomToggle.checked);
    save("bloom", bloomToggle.checked ? "on" : "off");
  });
}
import("./scene.js")
  .then((module) => {
    module.initScene(sceneEl, { onBloomReady: setupBloomToggle });
    scene3d = module;
    sceneNote.textContent = "마우스: 왼쪽 버튼 회전, 휠 확대, 오른쪽 버튼 이동";
  })
  .catch((err) => {
    console.error("3D 화면을 불러오지 못했습니다:", err);
    sceneNote.textContent = "3D 화면을 표시할 수 없습니다 (" + err.message + "). 나머지 화면은 정상 동작합니다.";
  });

// ---- 작은 도우미 ---------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clock(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

function percent(ratio) {
  return ratio === null || ratio === undefined ? "-" : (ratio * 100).toFixed(1) + "%";
}

// 브라우저 저장소는 막혀 있을 수 있으니(사생활 보호 모드 등) 실패해도 화면은 동작해야 한다
function loadSaved(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 저장 못 해도 괜찮다 */ }
}

setInterval(() => { clockEl.textContent = clock(Date.now() / 1000); }, 1000);

// ---- 지표(KPI) 띠 ---------------------------------------------------------------
function setKpi(id, text, level) {
  const node = $(id);
  node.textContent = text;
  node.className = "kpi-value" + (level ? " " + level : "");
}

function showKpis(kpi, openAlarms) {
  const a = kpi ? kpi.availability : null;
  setKpi("kpi-availability", percent(a), a === null ? "" : a >= 0.95 ? "good" : a >= 0.8 ? "caution" : "danger");
  setKpi("kpi-production", kpi ? String(kpi.production) : "-");
  const c = kpi ? kpi.comm_success_rate : null;
  setKpi("kpi-comm", percent(c), c === null ? "" : c >= 0.99 ? "good" : c >= 0.9 ? "caution" : "danger");
  setKpi("kpi-latency", kpi && kpi.avg_latency_ms !== null ? kpi.avg_latency_ms.toFixed(1) + " ms" : "-");
  setKpi("kpi-reconnects", kpi ? String(kpi.reconnects) : "-");
  setKpi("kpi-alarms", String(openAlarms), openAlarms === 0 ? "good" : "caution");
  if (scene3d) scene3d.setFactoryKpi(kpi, openAlarms);       // 3D 공장 벽의 모니터에도 같은 값
}

// ---- 설비 카드 -----------------------------------------------------------------
const FIELDS = [
  ["phase", "단계"], ["count", "생산 카운트"],
  ["joint1", "관절1"], ["joint2", "관절2"],
  ["gripper", "집게"], ["temp", "온도"],
  ["status", "상태"], ["cycle", "사이클 시간"],
];

function getCard(name) {
  if (cards.has(name)) return cards.get(name);

  const root = el("article", "card");
  const head = el("div", "card-head");
  head.append(el("span", "name", name));
  const state = el("span", "state", "-");
  head.append(state);

  const stale = el("div", "stale-note", "");
  const list = el("dl", "values");
  const values = {};
  for (const [key, label] of FIELDS) {
    list.append(el("dt", null, label));
    values[key] = el("dd", null, "-");
    list.append(values[key]);
  }
  const kpiLine = el("div", "kpi-line muted", "");
  const stats = el("div", "stats", "");
  root.append(head, stale, list, kpiLine, stats);

  // 설비 이름 순서대로 끼워 넣는다
  const card = { root, state, stale, values, kpiLine, stats, name };
  cards.set(name, card);
  const ordered = [...cards.keys()].sort();
  const next = ordered[ordered.indexOf(name) + 1];
  devicesEl.insertBefore(root, next ? cards.get(next).root : null);
  syncDeviceSelects();
  return card;
}

// 차트·패킷 뷰어의 설비 선택 목록을 카드와 맞춘다 (선택은 유지)
function syncDeviceSelects() {
  const names = [...cards.keys()].sort();
  for (const select of [chartDevice, packetDevice]) {
    const have = [...select.options].map((o) => o.value);
    if (have.join("|") === names.join("|")) continue;
    const chosen = select.value;
    select.replaceChildren(...names.map((n) => { const o = el("option", null, n); o.value = n; return o; }));
    select.value = names.includes(chosen) ? chosen : names[0];
  }
  if (cards.size === 1) { loadChart(); refreshPacket(); }         // 첫 설비가 생기면 바로 그린다
}

function showSnapshot(card, s) {
  const v = card.values;
  v.phase.textContent = s.phase_name;
  v.count.textContent = s.count;
  v.joint1.textContent = s.joint1.toFixed(1) + "°";
  v.joint2.textContent = s.joint2.toFixed(1) + "°";
  v.gripper.textContent = s.gripper ? "닫힘" : "열림";
  v.temp.textContent = s.temp.toFixed(1) + " ℃";
  v.status.textContent = s.status_name;
  v.cycle.textContent = s.cycle.toFixed(1) + " 초";
  card.root.classList.remove("s-normal", "s-warning", "s-fault");
  card.root.classList.add(["s-normal", "s-warning", "s-fault"][s.status] || "s-normal");
  if (scene3d) scene3d.updateRobot(card.name, s);          // 같은 값을 3D 로봇에도 전달
}

function showDevice(d, kpi) {
  const card = getCard(d.name);
  if (d.snapshot) showSnapshot(card, d.snapshot);

  card.state.textContent = "통신 " + d.state_label;          // 설비 '상태'(정상/주의/이상)와 구분되게 '통신'을 붙인다
  card.stats.textContent =
    `지연 ${d.avg_latency_ms}ms · 성공 ${d.reads_ok} · 실패 ${d.reads_failed} · 재접속 ${d.reconnects}`;
  const per = kpi && kpi.per_device ? kpi.per_device[d.name] : null;
  if (scene3d) scene3d.setRobotKpi(d.name, per ? per.availability : null);    // 3D 정보 카드의 가동률
  card.kpiLine.textContent = per ? `가동률 ${percent(per.availability)} · 생산 ${per.production}개 (최근 ${kpi.window_minutes}분)` : "";

  // 연결이 끊겼거나 값이 오래되면, 마지막 값을 현재 값처럼 보이지 않게 흐리게 하고 안내를 붙인다
  const old = d.state !== "ONLINE" || (d.data_age !== null && d.data_age > STALE_AFTER_SEC);
  card.root.classList.toggle("stale", old);
  if (scene3d) scene3d.setRobotOnline(d.name, !old);       // 3D 로봇도 회색으로 멈춘다
  if (old && d.data_age !== null) {
    card.stale.textContent = `마지막 수신 ${Math.round(d.data_age)}초 전 (값은 오래됨)`;
  } else if (old) {
    card.stale.textContent = "아직 값을 받지 못했습니다";
  } else {
    card.stale.textContent = "";
  }
}

// ---- 알람 표와 확인 ---------------------------------------------------------------
let noticeTimer = null;
function notify(text, ok = false) {
  noticeEl.textContent = text;
  noticeEl.classList.toggle("ok", ok);
  clearTimeout(noticeTimer);
  if (text) noticeTimer = setTimeout(() => { noticeEl.textContent = ""; }, 5000);
}

function sendCommand(command) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    notify("서버와 연결되어 있지 않아 보낼 수 없습니다.");
    return false;
  }
  socket.send(JSON.stringify(command));
  return true;
}

function ackAlarm(alarm, button) {
  const name = operatorInput.value.trim();
  if (!name) {
    notify("확인자 이름을 먼저 입력하세요.");
    operatorInput.focus();
    return;
  }
  save(OPERATOR_KEY, name);
  if (sendCommand({ type: "ack", id: alarm.id, by: name })) button.disabled = true;   // 답이 올 때까지 다시 누르지 못하게
}

// 알람 표는 1초마다 오는 state로 갱신된다. 표를 매번 새로 만들면 버튼을 누르는 도중(마우스를 누르고 떼는
// 사이)에 버튼이 교체되어 클릭이 사라진다. 그래서 알람 번호별로 줄을 한 번만 만들고, 이후에는 글자만 고친다.
const alarmRows = new Map();       // 알람 번호 -> { row, cells, confirmCell, confirmState, latest }

function makeAlarmRow(a) {
  const cells = [el("td"), el("td"), el("td"), el("td"), el("td"), el("td"), el("td")];
  const confirmCell = el("td");
  const row = el("tr");
  row.append(...cells, confirmCell);
  return { row, cells, confirmCell, confirmState: null, latest: a };
}

function updateAlarmRow(entry, a) {
  entry.latest = a;                                          // 버튼은 항상 가장 최근 알람 정보를 쓴다
  const [sev, device, kind, state, count, value, message] = entry.cells;
  sev.className = "sev-" + a.severity;
  const texts = [["P" + a.severity, sev], [a.device, device], [a.kind, kind], [a.state, state],
                 [String(a.repeat_count), count], [a.last_value === null ? "-" : a.last_value.toFixed(1), value],
                 [a.message, message]];
  for (const [text, cell] of texts) if (cell.textContent !== text) cell.textContent = text;

  // 확인 칸은 '미확인 -> 확인됨'으로 바뀔 때만 다시 만든다
  const acked = a.acked_at !== null;
  const stateKey = acked ? `acked|${a.acked_by}|${a.acked_at}` : "open";
  if (entry.confirmState === stateKey) return;
  entry.confirmState = stateKey;
  if (acked) {
    entry.confirmCell.replaceChildren(el("span", "acked-by", `${a.acked_by} ${clock(a.acked_at)}`));
  } else {
    const button = el("button", "ack", "확인");
    button.type = "button";
    button.addEventListener("click", () => ackAlarm(entry.latest, button));
    entry.confirmCell.replaceChildren(button);
  }
}

function showAlarms(alarms) {
  noAlarms.hidden = alarms.length > 0;
  alarmsTable.hidden = alarms.length === 0;
  const present = new Set(alarms.map((a) => a.id));
  for (const [id, entry] of alarmRows) {
    if (!present.has(id)) { entry.row.remove(); alarmRows.delete(id); }
  }
  alarms.forEach((a, index) => {
    let entry = alarmRows.get(a.id);
    if (!entry) { entry = makeAlarmRow(a); alarmRows.set(a.id, entry); }
    updateAlarmRow(entry, a);
    // 순서가 달라졌을 때만 줄을 옮긴다 (옮기지 않으면 마우스 아래의 버튼이 그대로 유지된다)
    if (alarmsBody.children[index] !== entry.row) alarmsBody.insertBefore(entry.row, alarmsBody.children[index] || null);
  });
}

// ---- 이벤트 목록 ---------------------------------------------------------------
function eventItem(e) {
  const li = el("li");
  li.append(el("span", "time", clock(e.ts)), document.createTextNode(`[${e.device}] ${e.kind}: ${e.message}`));
  return li;
}

function addEvent(e) {
  eventsEl.prepend(eventItem(e));
  while (eventsEl.children.length > MAX_EVENTS) eventsEl.lastChild.remove();     // 오래된 것은 지운다
}

// 접속할 때 서버가 최근 이벤트를 함께 보낸다(최신순). 다시 접속해도 중복되지 않게 통째로 바꾼다
function setEvents(events) {
  eventsEl.replaceChildren(...events.slice(0, MAX_EVENTS).map(eventItem));
}

// ---- 온도 차트 -----------------------------------------------------------------
const STATUS_COLOR = ["#3b82f6", "#f0b429", "#e5484d"];        // 정상, 주의, 이상
let chartData = null;
let chartRequest = 0;                                           // 늦게 도착한 옛 응답을 버리기 위한 번호

async function loadChart() {
  const device = chartDevice.value;
  if (!device) return;
  const ticket = ++chartRequest;
  try {
    const res = await fetch(`/api/history?device=${encodeURIComponent(device)}&minutes=${chartMinutes.value}&points=300`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (ticket !== chartRequest) return;                         // 그 사이 다른 설비를 골랐다
    chartData = data;
    chartNote.textContent = data.points.length === 0
      ? "아직 저장된 데이터가 없습니다."
      : `표본 ${data.total_samples}개 중 ${data.points.length}개 표시 · 점선은 주의(${data.thresholds.warn}℃)·이상(${data.thresholds.fault}℃) 기준`;
  } catch (err) {
    if (ticket !== chartRequest) return;
    chartNote.textContent = "이력을 불러오지 못했습니다 (" + err.message + ")";
  }
  drawChart();
}

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const w = chartCanvas.clientWidth, h = chartCanvas.clientHeight;
  if (w === 0 || h === 0) return;
  chartCanvas.width = Math.round(w * dpr);
  chartCanvas.height = Math.round(h * dpr);
  const g = chartCanvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const pad = { l: 40, r: 12, t: 12, b: 24 };
  g.font = "11px Consolas, monospace";
  if (!chartData || chartData.points.length < 2) {
    g.fillStyle = "#8a94a6";
    g.fillText("그릴 데이터가 부족합니다", pad.l, h / 2);
    return;
  }
  const { points, thresholds } = chartData;
  // 데이터가 구간보다 짧게만 쌓여 있으면 선이 한쪽 끝에 몰리므로, 실제 데이터가 시작된 시점부터 그린다
  const t0 = Math.max(chartData.from, points[0].ts), t1 = chartData.to;
  const temps = points.map((p) => p.temp);
  const lo = Math.floor(Math.min(...temps, 40) / 5) * 5 - 5;
  const hi = Math.ceil(Math.max(...temps, thresholds.fault + 5) / 5) * 5;
  const x = (t) => pad.l + ((t - t0) / (t1 - t0)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);

  // 눈금과 기준선
  g.strokeStyle = "#2a3140"; g.fillStyle = "#8a94a6"; g.lineWidth = 1;
  for (let v = lo; v <= hi; v += 10) {
    g.beginPath(); g.moveTo(pad.l, y(v)); g.lineTo(w - pad.r, y(v)); g.stroke();
    g.fillText(String(v), 6, y(v) + 4);
  }
  for (let i = 0; i <= 4; i++) {
    const t = t0 + ((t1 - t0) * i) / 4;
    g.fillText(clock(t), Math.min(x(t) - 22, w - 60), h - 6);
  }
  g.setLineDash([5, 4]);
  for (const [value, color] of [[thresholds.warn, STATUS_COLOR[1]], [thresholds.fault, STATUS_COLOR[2]]]) {
    g.strokeStyle = color;
    g.beginPath(); g.moveTo(pad.l, y(value)); g.lineTo(w - pad.r, y(value)); g.stroke();
  }
  g.setLineDash([]);

  // 온도 선: 구간마다 그 시점의 설비 상태 색으로 칠한다
  g.lineWidth = 2;
  for (let i = 1; i < points.length; i++) {
    g.strokeStyle = STATUS_COLOR[points[i].status] || STATUS_COLOR[0];
    g.beginPath();
    g.moveTo(x(points[i - 1].ts), y(points[i - 1].temp));
    g.lineTo(x(points[i].ts), y(points[i].temp));
    g.stroke();
  }
}

chartDevice.addEventListener("change", loadChart);
chartMinutes.addEventListener("change", loadChart);
new ResizeObserver(drawChart).observe(chartCanvas);
setInterval(loadChart, CHART_REFRESH_MS);

// ---- 패킷 뷰어 -----------------------------------------------------------------
let packetKey = "";                // 같은 패킷이면 다시 그리지 않는다 (1초마다 오는 state마다 그리면 마우스 올리기가 끊긴다)

function packetBlock(title, part) {
  const box = el("div");
  box.append(el("h3", null, title));
  if (!part) {
    box.append(el("p", "muted small", "받은 바이트가 없습니다."));
    return box;
  }
  const raw = el("div", "raw");
  const table = el("table");
  const body = el("tbody");
  part.fields.forEach((f, i) => {
    const span = el("span", "g-" + f.group, f.hex);
    span.title = `${f.name}: ${f.value}`;
    raw.append(span);

    const row = el("tr");
    row.append(el("td", "g-" + f.group, f.name), el("td", "hexcell", f.hex), el("td", null, f.value));
    body.append(row);
    // 표의 줄과 위쪽 바이트를 함께 강조한다
    const on = () => { span.classList.add("hot"); row.classList.add("hot"); };
    const off = () => { span.classList.remove("hot"); row.classList.remove("hot"); };
    span.addEventListener("mouseenter", on); span.addEventListener("mouseleave", off);
    row.addEventListener("mouseenter", on); row.addEventListener("mouseleave", off);
  });
  table.append(body);
  box.append(raw, table);
  return box;
}

function renderPacket() {
  const name = packetDevice.value;
  const info = packetInfo && packetInfo.name === name ? packetInfo.data : null;     // 다른 설비의 기록은 보여 주지 않는다
  const ex = info ? info[packetMode.value] : null;
  const key = `${name}|${packetMode.value}|${ex ? ex.tid + "|" + ex.ts : "-"}`;
  if (key === packetKey) return;
  packetKey = key;

  packetEl.replaceChildren();
  if (!ex) {
    // 접속 자체가 안 될 때는 주고받은 바이트가 없으므로 실패 패킷으로 남지 않는다 (이벤트 목록에서 확인)
    const text = packetMode.value === "last_failed"
      ? "읽는 도중 실패한 패킷이 없습니다. (접속이 안 된 경우는 오간 바이트가 없어 기록되지 않습니다. 아래 통신 이벤트를 보세요.)"
      : "아직 주고받은 패킷이 없습니다.";
    packetEl.append(el("p", "muted pad", text));
    return;
  }
  const meta = el("div", "packet-meta", `${clock(ex.ts)} · 트랜잭션 ${ex.tid}`);
  if (ex.error) meta.append(el("div", "packet-error", "실패 원인: " + ex.error));
  packetEl.append(meta, packetBlock("요청 (수집 서버 -> 설비)", ex.request), packetBlock("응답 (설비 -> 수집 서버)", ex.response));
}

// 패킷 기록은 1초마다 오는 state 메시지에 싣지 않는다 (설비 수에 비례해 메시지가 커지므로).
// 고른 설비 하나만 1초마다 /api/packet으로 가져온다.
const PACKET_REFRESH_MS = 1000;
let packetFetching = false;

async function refreshPacket() {
  const name = packetDevice.value;
  if (!name || packetFetching) return;                      // 이전 요청이 끝나기 전에 또 보내지 않는다
  packetFetching = true;
  try {
    const res = await fetch(`/api/packet?device=${encodeURIComponent(name)}`);
    if (res.ok) {
      const data = await res.json();
      if (packetDevice.value === name) {                    // 기다리는 사이 다른 설비를 골랐다면 버린다
        packetInfo = { name, data };
        renderPacket();
      }
    }
  } catch {
    /* 연결이 불안정하면 다음 주기에 다시 가져온다 */
  } finally {
    packetFetching = false;
  }
}

packetDevice.addEventListener("change", () => { packetKey = ""; packetInfo = null; renderPacket(); refreshPacket(); });
packetMode.addEventListener("change", () => { packetKey = ""; renderPacket(); });
setInterval(refreshPacket, PACKET_REFRESH_MS);

// ---- 서버 메시지 처리 ----------------------------------------------------------
function handleMessage(msg) {
  switch (msg.type) {
    case "state": {                    // 전체 상태 (접속할 때와 1초마다)
      msg.devices.forEach((d) => showDevice(d, msg.kpi));
      showAlarms(msg.alarms);
      if (scene3d) scene3d.setFactoryAlarms(msg.alarms);        // 3D 공장 벽 모니터의 최근 알람
      showKpis(msg.kpi, msg.alarms.length);
      if (msg.events) setEvents(msg.events);          // 접속 직후에만 들어 있다
      break;
    }
    case "snapshot": {                 // 설비 값 하나 (0.5초마다)
      const card = getCard(msg.device);
      showSnapshot(card, msg);
      card.root.classList.remove("stale");
      card.stale.textContent = "";
      if (scene3d) scene3d.setRobotOnline(msg.device, true);
      break;
    }
    case "event":
      addEvent(msg);
      break;
    case "alarm":                      // 알람 변화는 1초 안에 state 메시지로 표에 반영된다
      break;
    case "ack_result":
      if (msg.ok) notify(`알람 #${msg.id}을(를) 확인했습니다.`, true);
      else notify(`알람 #${msg.id} 확인 실패: ${msg.reason}`);
      break;
    case "error":
      notify("서버 응답: " + msg.message);
      break;
  }
}

// ---- 연결 (끊기면 다시 연결) -----------------------------------------------------
let retryMs = 1000;

function setConnection(online, text) {
  connBadge.textContent = text;
  connBadge.className = "badge " + (online ? "on" : "off");
}

function connect() {
  setConnection(false, "연결 중");
  const ws = new WebSocket(WS_URL);
  socket = ws;

  ws.onopen = () => {
    retryMs = 1000;                    // 연결에 성공하면 대기 시간을 처음으로 되돌린다
    setConnection(true, "연결됨");
  };

  ws.onmessage = (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (err) {
      console.error("메시지를 처리하지 못했습니다:", err, event.data);
    }
  };

  ws.onclose = () => {
    const wait = retryMs;
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
    setConnection(false, `연결 끊김, ${wait / 1000}초 후 다시 연결`);
    // 연결이 끊긴 동안 카드의 값은 오래된 값이다
    cards.forEach((c) => {
      c.root.classList.add("stale");
      c.stale.textContent = "서버와 연결이 끊겨 값이 갱신되지 않습니다";
      if (scene3d) scene3d.setRobotOnline(c.name, false);
    });
    setTimeout(connect, wait);
  };
}

operatorInput.value = loadSaved(OPERATOR_KEY);
connect();
