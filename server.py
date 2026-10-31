#!/usr/bin/env python3
"""One-node ESP32 classroom lab, with a clearly separated offline demo plant."""
from __future__ import annotations

import argparse
import csv
import hmac
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from control import LabController
from forecast import demo_history, make_forecast

ROOT = Path(__file__).resolve().parent
MAX_BODY = 1_100_000
ASSETS = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


class LabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, mode="demo", device_token=""):
        if mode == "hardware" and len(device_token) < 16:
            raise ValueError("장치 모드에는 16자 이상의 TSPAI_DEVICE_TOKEN이 필요합니다.")
        self.lab = LabController(mode=mode)
        self.device_token = device_token
        self.forecast = make_forecast()
        self.forecast_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        super().__init__(address, Handler)

    def snapshot(self, full=False):
        result = self.lab.status()
        with self.forecast_lock:
            result["forecast"] = self.forecast.copy()
        if not full:
            result["logs"] = result.get("logs", [])[-300:]
            result["events"] = result.get("events", [])[-60:]
        return result

    def tick(self):
        while not self.shutdown_event.wait(1):
            if self.lab.mode == "demo":
                self.lab.step_demo()
            else:
                self.lab.status()  # The host watchdog also runs without browser polling.


class Handler(BaseHTTPRequestHandler):
    server_version = "TSPhysicalAI2/1.0"

    def send_body(self, body, mime="application/json", status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send_body(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), status=status)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ASSETS:
            file, mime = ASSETS[path]
            self.send_body((ROOT / "static" / file).read_bytes(), mime)
        elif path == "/api/health":
            self.json({"status": "ok", "mode": self.server.lab.mode, "version": "1.0"})
        elif path == "/api/status":
            self.json(self.server.snapshot())
        elif path in ("/api/log.csv", "/api/forecast-template.csv"):
            rows = self.server.snapshot(full=True).get("logs", []) if path == "/api/log.csv" else demo_history()
            buffer = io.StringIO(newline="")
            columns = list(dict.fromkeys(key for row in rows for key in row))
            if columns:
                writer = csv.DictWriter(buffer, fieldnames=columns)
                writer.writeheader()
                writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()} for row in rows)
            self.send_body(buffer.getvalue().encode("utf-8-sig"), "text/csv", filename="telemetry.csv" if path == "/api/log.csv" else "daily_demo.csv")
        elif path == "/api/events.json":
            self.send_body(json.dumps(self.server.snapshot(full=True), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), filename="experiment.json")
        else:
            self.json({"error": "요청한 경로가 없습니다."}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/action", "/api/forecast", "/api/device/exchange"):
            self.json({"error": "요청한 경로가 없습니다."}, 404)
            return
        if self.headers.get_content_type() != "application/json":
            self.json({"error": "JSON 형식이 필요합니다."}, 415)
            return
        is_device = path == "/api/device/exchange"
        if is_device:
            if self.server.lab.mode != "hardware":
                self.json({"error": "장치 교환 API는 hardware 모드에서만 사용합니다."}, 409)
                return
            supplied = self.headers.get("X-Device-Token", "")
            if not hmac.compare_digest(supplied.encode(), self.server.device_token.encode()):
                self.json({"error": "장치 인증 실패"}, 401)
                return
        else:
            # Operate from the PC's own browser; LAN nodes can only exchange telemetry.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self.json({"error": "운영 명령은 서버 PC의 로컬 브라우저에서 실행하세요."}, 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.json({"error": "다른 사이트에서 보낸 요청은 허용하지 않습니다."}, 403)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("요청은 1.1MB 이하 JSON이어야 합니다.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON 객체를 입력하세요.")
            if is_device:
                command = self.server.lab.exchange(payload)
                self.json(command)
            elif path == "/api/action":
                action = payload.get("action")
                if not isinstance(action, str):
                    raise ValueError("action 문자열이 필요합니다.")
                settings = payload.get("payload", {})
                if not isinstance(settings, dict):
                    raise ValueError("payload 객체가 필요합니다.")
                self.server.lab.operate(action, settings)
                self.json(self.server.snapshot())
            else:
                forecast = make_forecast(payload.get("csv_text"), payload.get("scale_wh_per_kwh", .002), payload.get("model", "seasonal_naive"))
                with self.server.forecast_lock:
                    self.server.forecast = forecast
                self.json({"forecast": forecast, "status": self.server.snapshot()})
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.json({"error": str(exc) or "입력을 확인하세요."}, 400)


def main():
    parser = argparse.ArgumentParser(description="ESP32 1노드 시계열 Physical AI 교육 실험")
    parser.add_argument("--mode", choices=["demo", "hardware"], default="demo")
    parser.add_argument("--host", help="기본: demo 127.0.0.1 / hardware 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ.get("TSPAI_DEVICE_TOKEN", "")
    if args.mode == "hardware" and len(token) < 16:
        parser.error("장치 모드: TSPAI_DEVICE_TOKEN 환경 변수에 16자 이상의 장치용 토큰을 설정하세요.")
    host = args.host or ("0.0.0.0" if args.mode == "hardware" else "127.0.0.1")
    with LabServer((host, args.port), args.mode, token) as server:
        ticker = threading.Thread(target=server.tick, daemon=True)
        ticker.start()
        print(f"TSPhysicalAI2 [{args.mode}] port {server.server_port} | PC 브라우저: http://127.0.0.1:{server.server_port}", flush=True)
        print("초기 출력 정지. 새 작업 → ARM으로 시작 / 종료 Ctrl+C", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.lab.operate("stop")
            server.shutdown_event.set()
            ticker.join(timeout=2)


if __name__ == "__main__":
    main()
