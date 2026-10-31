"use strict";
const $ = (id) => document.getElementById(id);
const state = { status: null, forecast: null, csvText: null, polling: false, pending: false, initialized: false, formDirty: false, online: false };
const fmt = (v, digits = 1) => v == null || !Number.isFinite(v) ? "—" : new Intl.NumberFormat("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
const percent = (v) => v == null ? "—" : `${fmt(v * 100, 0)}%`;
const text = (id, value) => { $(id).textContent = value; };
const faultNames = { emergency_stop: "비상정지 상태", telemetry_stale: "계측 수신 지연", device_fault: "장치가 고장을 보고했습니다", invalid_telemetry: "계측 형식 오류", device_reboot: "장치 재부팅 감지", encoder_reset: "엔코더 누적값 초기화 감지", command_not_armed: "장치 가동 승인 확인 대기" };

function message(value, error = false) { text("status", value); $("status").className = `status${error ? " error" : ""}`; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let result; try { result = await response.json(); } catch (_) { throw new Error(`서버 응답을 읽을 수 없습니다 (${response.status})`); }
  if (!response.ok) throw new Error(result.error || result.message || `요청 실패 (${response.status})`);
  return result;
}
function syncForm(settings) {
  const fields = { budget: "budget_wh", "power-cap": "power_limit_w", "target-revs": "job_target_revolutions", deadline: "job_deadline_s", "motor-target": "motor_target_rpm", policy: "policy" };
  for (const [id, key] of Object.entries(fields)) if (settings[key] !== undefined) $(id).value = settings[key];
  if (settings.led_duty !== undefined) $("led-demand").value = Math.round(settings.led_duty * 100);
  if (settings.led_min_duty !== undefined) $("led-min").value = Math.round(settings.led_min_duty * 100);
  state.formDirty = false;
}
function settingsPayload() {
  const val = (id) => Number($(id).value);
  return { budget_wh: val("budget"), power_limit_w: val("power-cap"), job_target_revolutions: val("target-revs"), job_deadline_s: val("deadline"), led_duty: val("led-demand") / 100, led_min_duty: val("led-min") / 100, motor_target_rpm: val("motor-target"), policy: $("policy").value };
}
function controlsDisabled() {
  const s = state.status; const offline = !state.online; const busy = state.pending || offline;
  $("stop-button").disabled = offline;
  $("arm-button").disabled = busy || !s?.device_fresh || Boolean(s?.fault || s?.telemetry?.fault) || Boolean(s?.armed);
  $("new-job-button").disabled = busy;
  $("reset-fault-button").disabled = busy;
  $("reset-experiment-button").disabled = busy || Boolean(s?.armed);
  $("settings-button").disabled = busy;
  $("forecast-button").disabled = busy;
  $("apply-budget").disabled = busy || !state.forecast;
}
async function action(kind, payload = {}) {
  if (state.pending && kind !== "emergency_stop") return;
  state.pending = true; controlsDisabled();
  try {
    const result = await api("/api/action", { action: kind, payload });
    state.online = true; state.status = result.status || result;
    if (kind === "update_settings") syncForm(state.status.settings);
    render();
    const labels = { arm: "가동 승인을 요청했습니다. 장치 수신 확인과 센서 응답을 확인하세요.", emergency_stop: "STOP을 요청했습니다. 고장 해제 후 별도 가동 승인이 필요합니다.", stop: "출력 해제를 요청했습니다.", new_job: "작업 기준을 새로 설정했습니다. 누적 에너지는 유지되며 별도 가동 승인이 필요합니다.", reset_experiment: "에너지·작업 기준·관측 로그를 초기화했습니다. 별도 가동 승인이 필요합니다.", reset_fault: "고장 해제를 요청했습니다. 장치 확인 후 별도로 가동 승인하세요.", update_settings: "운영 설정을 적용했습니다. 가동 승인 상태를 확인하세요." };
    message(labels[kind] || "요청을 처리했습니다.");
  } catch (error) { message(error.message, true); } finally { state.pending = false; controlsDisabled(); }
}
async function poll() {
  if (state.polling || state.pending) return;
  state.polling = true;
  try {
    const result = await api("/api/status");
    state.online = true; state.status = result.status || result;
    if (!state.initialized) { syncForm(state.status.settings); message("서버에 연결했습니다. 설정과 데이터 출처를 확인한 뒤 가동을 승인하세요."); state.initialized = true; }
    if (state.status.forecast && !state.forecast) renderForecast(state.status.forecast);
    render();
  } catch (error) {
    state.online = false; text("link-state", "서버 연결 끊김"); $("link-state").className = "link-state error";
    text("power-value", "—"); text("rpm-value", "—"); text("arm-state", "연결 확인 필요"); $("arm-state").className = "state-pill fault";
    message(`서버에 연결하지 못했습니다. 화면의 값은 최신 상태가 아닙니다. ${error.message}`, true);
    controlsDisabled();
  } finally { state.polling = false; }
}
function render() {
  const s = state.status; if (!s) return;
  const demo = s.mode === "demo", t = s.telemetry, m = s.metrics || {}, c = s.command || {}, settings = s.settings || {};
  const fresh = Boolean(s.device_fresh && t), fault = s.fault || t?.fault || "";
  $("mode-badge").className = `mode-badge${demo ? "" : " hardware"}`;
  text("mode-badge", demo ? "DEMO · 가상 관측" : "HARDWARE · ESP32");
  $("provenance").className = `provenance${demo ? "" : " hardware"}`;
  text("provenance-title", demo ? "데모 모드 · 합성 물리 모델의 가상 응답" : fresh ? "실물 모드 · ESP32가 전송한 센서 관측" : "실물 모드 · 유효한 최신 계측 대기");
  text("provenance-note", demo ? "화면의 전력·RPM·회전량은 가상 모델이 생성합니다. 실제 센서 측정이나 실물 시험 결과가 아닙니다." : fresh ? "INA260 전력과 엔코더 회전량을 기록합니다. 보드가 전송한 값의 정확도는 별도 계측·교정으로 확인하세요." : "가상값으로 대체하지 않습니다. ESP32의 서버 주소·네트워크·노드 설정을 확인하세요.");
  $("link-state").replaceChildren(); const dot = document.createElement("i"); $("link-state").append(dot, document.createTextNode(fresh ? (demo ? "가상 노드 연결" : "계측 수신 중") : "노드 관측 대기")); $("link-state").className = `link-state ${fresh ? "online" : "error"}`;
  text("arm-state", fault ? "고장 · 출력 해제" : s.armed ? "서버 가동 승인" : "출력 해제 · DISARMED");
  $("arm-state").className = `state-pill${fault ? " fault" : s.armed ? " armed" : ""}`;
  text("fault-message", fault ? `${faultNames[fault] || fault} · 고장 해제와 재승인은 별도입니다.` : "");
  text("control-note", s.armed ? "서버 승인과 장치의 수신·가동 상태를 각각 확인하세요." : "정지 상태입니다. 연결 복구나 설정 적용만으로 가동하지 않습니다.");
  text("node-label", t ? `노드 ${t.device_id} · 장치 ${t.armed ? "가동 보고" : "출력 해제 보고"}` : "노드 미수신");
  text("last-seen", t ? `계측 seq ${t.seq} · 가동 후 ${fmt(t.uptime_ms / 1000, 0)}초${fresh ? "" : " · 수신 지연"}` : "마지막 수신 —");
  text("ack-label", t ? `장치 확인 #${t.command_ack} / 서버 최신 #${c.command_id ?? "—"}` : "명령 확인 —");
  text("power-value", fresh ? `${fmt(t.power_w, 2)} W` : "—");
  text("electrical-value", fresh ? `${fmt(t.voltage_v, 2)} V · ${fmt(t.current_a, 3)} A${demo ? " · 가상" : ""}` : "최신 전압·전류 관측 대기");
  text("energy-value", t ? `${fmt(m.energy_wh, 3)} Wh` : "—");
  text("budget-value", `예산 ${fmt(settings.budget_wh, 3)} Wh${m.energy_valid === false ? " · 관측 누락으로 불완전" : m.budget_exceeded ? " · 초과" : ""}`);
  $("energy-bar").style.width = `${Math.min(100, Math.max(0, (m.energy_wh || 0) / (settings.budget_wh || 1) * 100))}%`;
  $("energy-bar").style.background = m.budget_exceeded ? "#c66a4b" : "#7abcae";
  text("rpm-value", fresh ? `${fmt(t.rpm)} RPM` : "—");
  const progress = t ? Math.min(1, (m.job_revolutions || 0) / (settings.job_target_revolutions || 1)) : null;
  text("revs-value", progress == null ? "—" : m.job_completed ? "완료 · 100%" : percent(progress));
  text("job-time", t ? `${fmt(m.job_revolutions)} / ${fmt(settings.job_target_revolutions, 0)}회전 · ${m.deadline_missed ? "시한 초과" : `잔여 ${fmt(m.deadline_remaining_s, 0)}초`}` : "회전량 — · 잔여 시간 —");
  $("job-bar").style.width = `${(progress || 0) * 100}%`;
  text("policy-badge", settings.policy === "fixed" ? "고정 출력 기준 정책" : "실측 피드백 휴리스틱");
  text("loop-observe", fresh ? `관측 전력 ${fmt(t.power_w, 2)} W\n누적 작업 ${fmt(m.job_revolutions)}회전` : "최신 관측 없음\n결측을 가상값으로 채우지 않음");
  text("loop-decision", `잔여 예산 ${fmt(m.budget_remaining_wh, 3)} Wh\n목표 ${fmt(settings.motor_target_rpm, 0)} RPM · 상한 ${fmt(settings.power_limit_w)} W`);
  text("loop-command", `LED ${percent(c.led_duty)} · 모터 ${percent(c.motor_duty)}\n명령 #${c.command_id ?? "—"} · 유효 ${c.ttl_ms ?? "—"}ms`);
  text("loop-feedback", fresh ? `${demo ? "가상" : "엔코더"} RPM ${fmt(t.rpm)}\n장치 누적 ${fmt(t.total_revolutions)}회전` : "센서 응답 대기\n명령과 관측을 별도 기록");
  renderEvents(s.events || []); renderCharts(s.logs || [], settings);
  text("log-note", `에너지 관측 ${m.energy_valid === false ? `불완전 · 누락 ${fmt((m.energy_missing_ms || 0) / 1000)}초` : "유효"} · 전력 상한은 운영 목표이며 피드백 정책이 준수를 보장하지 않습니다.`);
  controlsDisabled();
}
function renderEvents(events) {
  const body = $("events-body"); body.replaceChildren();
  for (const event of events.slice(-10).reverse()) {
    const tr = document.createElement("tr");
    for (const value of [`작업 ${fmt(event.time_s, 0)}초`, event.kind, event.message]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); }
    body.append(tr);
  }
  if (!events.length) { const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan = 3; td.textContent = "기록을 기다리고 있습니다."; tr.append(td); body.append(tr); }
}
function renderForecast(forecast) {
  state.forecast = forecast; $("forecast-result").hidden = false;
  text("forecast-source", /synthetic|demo/.test(forecast.source || "") ? "합성 시연 이력 · 기준 예측" : "업로드 일별 이력 · 기준 예측");
  text("forecast-prediction", `${fmt(forecast.predicted_kwh)} kWh`);
  text("forecast-history", `${forecast.history_days ?? "—"}일 이력 · 기준일 ${forecast.cutoff_date ?? "—"}`);
  text("forecast-budget", `${fmt(forecast.budget_wh, 3)} Wh`); controlsDisabled();
}
async function makeForecast() {
  if (state.pending) return; state.pending = true; controlsDisabled();
  try {
    const body = { scale_wh_per_kwh: Number($("forecast-scale").value), model: "seasonal_naive" };
    if (state.csvText != null) body.csv_text = state.csvText;
    const result = await api("/api/forecast", body); renderForecast(result.forecast || result);
    if (result.status) { state.status = result.status; render(); }
    message("일별 예측을 계산했습니다. 추천 예산을 적용하기 전까지 운전 설정은 유지됩니다.");
  } catch (error) { message(error.message, true); } finally { state.pending = false; controlsDisabled(); }
}

const NS = "http://www.w3.org/2000/svg";
function node(tag, attrs, label) { const item = document.createElementNS(NS, tag); for (const [key, value] of Object.entries(attrs || {})) item.setAttribute(key, value); if (label !== undefined) item.textContent = label; return item; }
function chart(id, logs, series, limit = null, fixedMax = null) {
  const host = $(id); host.replaceChildren();
  if (!logs.length) { const empty = document.createElement("p"); empty.className = "empty-chart"; empty.textContent = "유효한 관측 기록을 기다리고 있습니다."; host.append(empty); return; }
  const svg = node("svg", { viewBox: "0 0 550 210", "aria-hidden": "true" }), left = 40, right = 530, top = 10, bottom = 177;
  const max = fixedMax || Math.max(1, limit || 0, ...logs.flatMap((r) => series.map((s) => Number(r[s.key]) * (s.factor || 1) || 0))) * 1.12;
  for (let i = 0; i <= 4; i++) { const y = top + (bottom - top) * i / 4; svg.append(node("line", { x1: left, x2: right, y1: y, y2: y, stroke: "#e9f0f3" })); svg.append(node("text", { x: left - 8, y: y + 3, "text-anchor": "end", "font-size": 9, fill: "#8a9daa" }, fmt(max * (1 - i / 4), max > 30 ? 0 : 1))); }
  if (limit != null) { const y = bottom - (bottom - top) * limit / max; svg.append(node("line", { x1: left, x2: right, y1: y, y2: y, stroke: "#ce9a77", "stroke-width": 1.2, "stroke-dasharray": "4 4" })); }
  for (const s of series) {
    let path = ""; logs.forEach((r, index) => { const value = Number(r[s.key]); if (!Number.isFinite(value)) return; const x = left + (right - left) * index / Math.max(1, logs.length - 1), y = bottom - (bottom - top) * value * (s.factor || 1) / max; path += `${path ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; });
    svg.append(node("path", { d: path, fill: "none", stroke: s.color, "stroke-width": 1.9, "stroke-linejoin": "round" }));
  }
  for (let i = 0; i < 3; i++) { const row = logs[Math.round((logs.length - 1) * i / 2)]; svg.append(node("text", { x: left + (right - left) * i / 2, y: 201, "text-anchor": i === 0 ? "start" : i === 2 ? "end" : "middle", "font-size": 9, fill: "#8a9daa" }, `장치 ${fmt(row.uptime_ms / 1000, 0)}초`)); }
  host.append(svg);
}
function renderCharts(allLogs, settings) {
  const logs = allLogs.slice(-120);
  chart("power-chart", logs, [{ key: "power_w", color: "#087f7a" }], settings.power_limit_w);
  chart("rpm-chart", logs, [{ key: "rpm", color: "#587eb8" }]);
  chart("command-chart", logs, [{ key: "led_duty", color: "#ba934a", factor: 100 }, { key: "motor_duty", color: "#158b83", factor: 100 }], null, 100);
}
function download(filename, data, type = "text/plain;charset=utf-8") { const url = URL.createObjectURL(new Blob([data], { type })); const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
$("settings-form").addEventListener("submit", (event) => { event.preventDefault(); action("update_settings", settingsPayload()); });
$("settings-form").addEventListener("input", () => { state.formDirty = true; message("설정란을 변경했습니다. ‘설정 적용’을 누르면 제어 설정에 반영됩니다."); });
$("arm-button").addEventListener("click", () => action("arm"));
$("stop-button").addEventListener("click", () => action("emergency_stop"));
$("new-job-button").addEventListener("click", () => action("new_job"));
$("reset-fault-button").addEventListener("click", () => action("reset_fault"));
$("reset-experiment-button").addEventListener("click", () => action("reset_experiment"));
$("forecast-button").addEventListener("click", makeForecast);
$("forecast-file").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) { state.csvText = null; text("file-name", "선택한 파일 없음 · 기본 합성 자료 사용"); return; }
  if (file.size > 1024 * 1024) { message("일별 CSV는 1MB 이하로 준비하세요.", true); event.target.value = ""; return; }
  state.csvText = await file.text(); text("file-name", file.name); message("일별 자료를 선택했습니다. ‘다음 날 수요 예측’을 눌러 검증하고 예측하세요.");
});
$("apply-budget").addEventListener("click", () => { if (state.forecast) action("update_settings", { budget_wh: state.forecast.budget_wh }); });
$("note-export").addEventListener("click", () => download("physical_ai_learning_note.txt", `Physical AI 에너지 제어 실험 노트\n\n모드: ${state.status?.mode || "미연결"}\n\n운영 설정\n${JSON.stringify(state.status?.settings || {}, null, 2)}\n\n예측\n${JSON.stringify(state.forecast || {}, null, 2)}\n\n관측 지표\n${JSON.stringify(state.status?.metrics || {}, null, 2)}\n\n나의 기록\n${$("learning-note").value}\n`));
poll(); const pollingTimer = setInterval(poll, 1000); window.addEventListener("pagehide", () => clearInterval(pollingTimer));
