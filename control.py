"""Single-node classroom controller and offline DC-load plant, standard library only.

The server is a supervisory controller. Firmware must independently enforce its
watchdog and physical protection limits. Demo telemetry is synthetic, not measured.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from datetime import datetime, timezone


DEFAULT_SETTINGS = {
    "budget_wh": 1.0, "power_limit_w": 12.0,
    "led_min_duty": .3, "led_duty": .6,
    "motor_target_rpm": 100.0, "job_target_revolutions": 200.0,
    "job_deadline_s": 180.0, "policy": "feedback",
}


def _number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{name}: 숫자가 필요합니다.")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name}: 숫자가 필요합니다.") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: {low}~{high} 범위의 유한한 숫자가 필요합니다.")
    return value


class LabController:
    FRESHNESS_S = 3.0
    MAX_GAP_MS = 3000
    MAX_LOGS = 5000

    def __init__(self, mode="demo"):
        if mode not in ("demo", "hardware"):
            raise ValueError("mode는 demo 또는 hardware여야 합니다.")
        self.mode = mode
        self.lock = threading.RLock()
        self.settings = dict(DEFAULT_SETTINGS)
        self.armed = False
        self.fault = ""
        self.telemetry = None
        self.command = None
        self.events = []
        self.logs = []
        self._last_receipt = None
        self._previous = None
        # Epoch-derived IDs prevent a host process restart from replaying low IDs
        # to a still-running ESP32; subsequent commands remain strictly increasing.
        self._command_id = time.time_ns() // 1_000_000
        self._reset_pending = False
        self._reset_command_id = None
        self._arm_command_id = None
        self._emergency = False
        self._energy_wh = 0.0
        self._energy_valid = True
        self._energy_missing_ms = 0
        self._job_baseline = None
        self._job_start_ms = None
        self._elapsed_s = 0.0
        self._completed = False
        self._completion_s = None
        self._integral_rpm = 0.0
        self._demo_rpm = 0.0
        self._demo_revolutions = 0.0
        self._demo_uptime_ms = 0
        self._demo_seq = 0
        self._emit_command()
        self._event("startup", "출력 정지 상태로 시작했습니다.")
        if mode == "demo":
            self.exchange(self._demo_packet(0.0))

    def _event(self, kind, message):
        self.events.append({"time_s": self._elapsed_s, "kind": kind, "message": message})
        self.events = self.events[-200:]

    def _fresh(self):
        return self._last_receipt is not None and (self.mode == "demo" or time.monotonic() - self._last_receipt <= self.FRESHNESS_S)

    def _check_stale(self):
        if self.mode == "hardware" and self._last_receipt is not None and not self._fresh() and self.armed:
            self._trip("telemetry_stale", "3초 동안 새 계측이 없어 출력을 해제했습니다.")

    def _trip(self, code, message):
        self.armed = False
        self._integral_rpm = 0.0
        if self.fault != code:
            self._event("fault", message)
        self.fault = code
        self._emit_command()

    def _metrics(self):
        revolutions = max(0.0, self.telemetry["total_revolutions"] - self._job_baseline) if self.telemetry and self._job_baseline is not None else 0.0
        elapsed = self._elapsed_s
        power = self.telemetry["power_w"] if self.telemetry else 0.0
        return {
            "energy_wh": self._energy_wh, "energy_valid": self._energy_valid,
            "energy_missing_ms": self._energy_missing_ms,
            "job_revolutions": revolutions, "job_completed": self._completed,
            "job_completion_s": self._completion_s,
            "deadline_elapsed_s": elapsed,
            "deadline_remaining_s": max(0.0, self.settings["job_deadline_s"] - elapsed),
            "deadline_missed": (self._completion_s > self.settings["job_deadline_s"] if self._completed
                                else elapsed >= self.settings["job_deadline_s"]),
            "budget_exceeded": self._energy_wh > self.settings["budget_wh"],
            "power_limit_exceeded": power > self.settings["power_limit_w"],
            "service_unmet": bool(self.armed and self.command and self.command["led_duty"] < self.settings["led_min_duty"]),
            "budget_remaining_wh": max(0.0, self.settings["budget_wh"] - self._energy_wh),
        }

    def _emit_command(self):
        self._command_id = max(self._command_id + 1, time.time_ns() // 1_000_000)
        led = motor = 0.0
        if self.armed and not self.fault and self.telemetry and not self.telemetry["fault"]:
            led = self.settings["led_duty"]
            if not self._completed:
                target = self.settings["motor_target_rpm"]
                # Teaching feed-forward plus proportional speed correction, not tuned PID.
                motor = min(1.0, max(0.0, target / 220.0))
                if self.settings["policy"] == "feedback":
                    motor += .002 * (target - self.telemetry["rpm"])
                    # Measured power margin throttles motor; LED minimum is retained.
                    if self.telemetry["power_w"] > self.settings["power_limit_w"]:
                        motor *= self.settings["power_limit_w"] / max(self.telemetry["power_w"], .001)
                    remaining = self.settings["budget_wh"] - self._energy_wh
                    if remaining <= 0:
                        led = self.settings["led_min_duty"]
                        motor = 0.0
                    else:
                        seconds = max(1, self.settings["job_deadline_s"] - self._elapsed_s)
                        allowed_w = remaining * 3600 / seconds
                        estimated_led_w = 4.32 * led
                        motor = min(motor, max(0.0, (allowed_w - estimated_led_w) / 6.0))
                motor = min(1.0, max(0.0, motor))
            led = max(self.settings["led_min_duty"], min(1.0, led))
        absolute_target = (self._job_baseline or 0.0) + self.settings["job_target_revolutions"]
        self.command = {
            "command_id": self._command_id, "led_duty": led, "motor_duty": motor,
            "ttl_ms": 1500, "arm": bool(self.armed and not self.fault),
            "reset_fault": self._reset_pending, "emergency_stop": self._emergency,
            "target_revolutions": absolute_target,
        }
        if self._reset_pending and self._reset_command_id is None:
            self._reset_command_id = self._command_id
        if self.command["arm"] and self._arm_command_id is None:
            self._arm_command_id = self._command_id
        if not self.command["arm"]:
            self._arm_command_id = None

    def status(self):
        with self.lock:
            self._check_stale()
            return copy.deepcopy({
                "mode": self.mode, "armed": self.armed, "fault": self.fault,
                "device_fresh": self._fresh(), "settings": self.settings,
                "telemetry": self.telemetry, "command": self.command,
                "metrics": self._metrics(), "events": self.events,
                "logs": self.logs,
                "capabilities": {
                    "synthetic": self.mode == "demo", "hardware_verified": False,
                    "note": "시연 값은 합성 물리 모델입니다. PWM은 서비스 대리지표이며 실제 밝기·쾌적성 측정이 아닙니다. 피드백 정책은 휴리스틱이며 예산·작업 완료를 보장하지 않습니다.",
                },
            })

    def _validate_telemetry(self, packet):
        if not isinstance(packet, dict):
            raise ValueError("계측은 JSON 객체여야 합니다.")
        required = {"device_id", "boot_id", "seq", "uptime_ms", "voltage_v", "current_a", "power_w", "rpm", "total_revolutions", "fault", "armed", "command_ack"}
        if not required.issubset(packet):
            raise ValueError("누락된 계측 필드: " + ", ".join(sorted(required - set(packet))))
        if packet["device_id"] != "node-1":
            raise ValueError("device_id는 node-1이어야 합니다.")
        if not isinstance(packet["boot_id"], str) or not 1 <= len(packet["boot_id"]) <= 80:
            raise ValueError("boot_id가 올바르지 않습니다.")
        result = {"device_id": "node-1", "boot_id": packet["boot_id"]}
        for name in ("seq", "uptime_ms", "command_ack"):
            value = _number(packet[name], name, 0, 2**53 - 1)
            if not value.is_integer():
                raise ValueError(f"{name}은 정수여야 합니다.")
            result[name] = int(value)
        for name, upper in (("voltage_v", 18), ("current_a", 3), ("power_w", 36), ("rpm", 10000), ("total_revolutions", 1e10)):
            result[name] = _number(packet[name], name, 0, upper)
        if not isinstance(packet["armed"], bool) or not isinstance(packet["fault"], str) or len(packet["fault"]) > 160:
            raise ValueError("armed는 참/거짓, fault는 160자 이하 문자열이어야 합니다.")
        result["armed"], result["fault"] = packet["armed"], packet["fault"]
        for name in ("led_applied_duty", "motor_applied_duty"):
            result[name] = _number(packet[name], name, 0, 1) if name in packet else None
        return result

    def exchange(self, telemetry):
        with self.lock:
            try:
                packet = self._validate_telemetry(telemetry)
            except ValueError:
                self._trip("invalid_telemetry", "잘못된 계측으로 출력을 해제했습니다.")
                raise
            self._check_stale()
            previous = self._previous
            reboot = previous is not None and previous["boot_id"] != packet["boot_id"]
            if previous and not reboot:
                if packet["seq"] <= previous["seq"] or packet["uptime_ms"] <= previous["uptime_ms"]:
                    self._trip("telemetry_order", "중복·역순 계측을 거부하고 출력을 해제했습니다.")
                    raise ValueError("seq와 uptime_ms는 같은 부팅 세션에서 엄격히 증가해야 합니다.")
                if packet["total_revolutions"] < previous["total_revolutions"]:
                    self._trip("encoder_reset", "부팅 변화 없이 엔코더 누적값이 감소했습니다.")
                    raise ValueError("total_revolutions는 같은 부팅 세션에서 감소할 수 없습니다.")
            if reboot:
                self._trip("device_reboot", "장치 재부팅을 감지했습니다. 작업 기준을 초기화하고 재승인이 필요합니다.")
                self._job_baseline = packet["total_revolutions"]
                self._job_start_ms = None
                self._elapsed_s = 0.0
                self._completed = False
                self._completion_s = None
                self._energy_valid = False
            elif previous:
                gap = packet["uptime_ms"] - previous["uptime_ms"]
                if gap > self.MAX_GAP_MS:
                    self._energy_valid = False
                    self._energy_missing_ms += gap
                    self._trip("telemetry_gap", "계측 간격이 3초를 넘어 누락 구간 에너지를 적분하지 않았습니다.")
                else:
                    # Trapezoidal integral of measured samples, not duty or commanded watts.
                    self._energy_wh += (previous["power_w"] + packet["power_w"]) * .5 * gap / 3_600_000
            self.telemetry = packet
            self._previous = packet
            self._last_receipt = time.monotonic()
            if self._job_baseline is None:
                self._job_baseline = packet["total_revolutions"]
            if self._job_start_ms is not None:
                self._elapsed_s = max(0.0, (packet["uptime_ms"] - self._job_start_ms) / 1000)
            if self._reset_pending and self._reset_command_id is not None and packet["command_ack"] >= self._reset_command_id:
                self._reset_pending = False
                self._reset_command_id = None
                if self.fault.startswith("device_fault:") and not packet["fault"]:
                    self.fault = ""
            if packet["fault"]:
                self._trip("device_fault:" + packet["fault"], "장치의 고장 상태로 출력을 해제했습니다: " + packet["fault"])
            elif self.armed and not packet["armed"] and self._arm_command_id is not None and packet["command_ack"] >= self._arm_command_id:
                self._trip("device_disarmed", "장치가 출력을 해제했습니다. 자동 재가동하지 않습니다.")
            if packet["power_w"] > 30 or packet["current_a"] > 2.5:
                self._trip("physical_limit", "교육용 장치 전력·전류 보호 기준을 초과했습니다.")
            progress = max(0.0, packet["total_revolutions"] - self._job_baseline)
            if progress >= self.settings["job_target_revolutions"] and not self._completed:
                self._completed = True
                self._completion_s = self._elapsed_s
                self._event("job_completed", "실측 엔코더 회전수로 작업 완료를 확인했습니다.")
            self._emit_command()
            self.logs.append({**packet, **self._metrics(),
                              "received_at_utc": datetime.now(timezone.utc).isoformat(),
                              "next_command_id": self.command["command_id"],
                              "next_led_duty": self.command["led_duty"],
                              "next_motor_duty": self.command["motor_duty"],
                              "led_duty": self.command["led_duty"],
                              "motor_duty": self.command["motor_duty"], "server_armed": self.armed})
            self.logs = self.logs[-self.MAX_LOGS:]
            return copy.deepcopy(self.command)

    def operate(self, action, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise ValueError("설정은 JSON 객체여야 합니다.")
        with self.lock:
            self._check_stale()
            if action == "arm":
                if not self._fresh() or not self.telemetry:
                    raise ValueError("정상적인 최신 계측을 먼저 수신해야 합니다.")
                if self.fault or self.telemetry["fault"] or self._reset_pending:
                    raise ValueError("고장 해제와 장치 확인 이후 별도로 가동 승인하세요.")
                self.armed = True
                self._emergency = False
                if self._job_start_ms is None:
                    self._job_start_ms = self.telemetry["uptime_ms"]
                self._event("armed", "사용자가 출력을 승인했습니다.")
            elif action == "stop":
                self.armed = False
                self._event("stop", "사용자가 출력을 정지했습니다.")
            elif action == "emergency_stop":
                self._emergency = True
                self._trip("emergency_stop", "비상정지했습니다. 해제 후 재승인이 필요합니다.")
            elif action in ("reset_fault", "reset"):
                self.armed = False
                self.fault = ""
                self._emergency = False
                self._reset_pending = True
                self._reset_command_id = None
                self._event("reset", "고장 해제를 요청했습니다. 자동으로 재가동하지 않습니다.")
            elif action in ("new_job", "reset_experiment"):
                if action == "reset_experiment" and self.armed:
                    raise ValueError("실험 초기화 전에 출력을 정지하세요.")
                self.armed = False
                self._job_baseline = self.telemetry["total_revolutions"] if self.telemetry else None
                self._job_start_ms = None
                self._elapsed_s = 0.0
                self._completed = False
                self._completion_s = None
                if action == "reset_experiment":
                    self._energy_wh = 0.0
                    self._energy_valid = True
                    self._energy_missing_ms = 0
                    self.logs = []
                self._event(action, "새 작업 기준을 설정했습니다. 별도 가동 승인이 필요합니다.")
            elif action == "update_settings":
                unknown = set(payload) - set(DEFAULT_SETTINGS)
                if unknown:
                    raise ValueError("알 수 없는 설정: " + ", ".join(sorted(unknown)))
                updated = {**self.settings, **payload}
                for name, low, high in (("budget_wh", .001, 10000), ("power_limit_w", .1, 30),
                                       ("led_min_duty", 0, 1), ("led_duty", 0, 1),
                                       ("motor_target_rpm", 1, 220), ("job_target_revolutions", 1, 1e6),
                                       ("job_deadline_s", 1, 86400)):
                    updated[name] = _number(updated[name], name, low, high)
                if updated["led_duty"] < updated["led_min_duty"]:
                    raise ValueError("조명 설정은 최소 요구 듀티 이상이어야 합니다.")
                if updated["policy"] not in ("fixed", "feedback"):
                    raise ValueError("policy는 fixed 또는 feedback이어야 합니다.")
                self.settings = updated
                self._event("settings", "운영 설정을 변경했습니다.")
            else:
                raise ValueError("지원하지 않는 동작입니다.")
            self._emit_command()
            return self.status()

    def _demo_packet(self, power):
        return {"device_id": "node-1", "boot_id": "synthetic-demo-1",
                "seq": self._demo_seq, "uptime_ms": self._demo_uptime_ms,
                "voltage_v": 12.0, "current_a": power / 12, "power_w": power,
                "rpm": self._demo_rpm, "total_revolutions": self._demo_revolutions,
                "led_applied_duty": self.command["led_duty"] if self.command["arm"] else 0.0,
                "motor_applied_duty": self.command["motor_duty"] if self.command["arm"] else 0.0,
                "fault": "", "armed": self.armed, "command_ack": self.command["command_id"]}

    def step_demo(self):
        with self.lock:
            if self.mode != "demo":
                raise ValueError("실물 모드에서는 가상 계측을 생성할 수 없습니다.")
            cmd = self.command
            duty = cmd["motor_duty"] if cmd["arm"] else 0.0
            led = cmd["led_duty"] if cmd["arm"] else 0.0
            # First-order synthetic motor speed, encoder integrates average RPM.
            before_rpm = self._demo_rpm
            target_rpm = 220 * max(0.0, (duty - .12) / .88)
            self._demo_rpm += .65 * (target_rpm - self._demo_rpm)
            self._demo_revolutions += (before_rpm + self._demo_rpm) * .5 / 60
            power = 4.32 * led + (1.0 + 5.0 * duty if duty > 0 else 0.0)
            self._demo_uptime_ms += 1000
            self._demo_seq += 1
            self.exchange(self._demo_packet(power))
            return self.status()
