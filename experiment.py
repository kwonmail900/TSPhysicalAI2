#!/usr/bin/env python3
"""Unpaced, repeatable *synthetic* policy comparison; never drives an ESP32."""
import argparse
import csv
import json
from pathlib import Path

from control import LabController
from forecast import make_forecast


def main():
    parser = argparse.ArgumentParser(description="가상 DC 부하에서 fixed/feedback 정책 비교")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--budget-wh", type=float, default=None, help="생략하면 합성 일별 예측의 교육용 환산 예산")
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds는 1~3600 범위입니다.")
    forecast = make_forecast()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for policy in ("fixed", "feedback"):
        lab = LabController("demo")
        lab.operate("update_settings", {"policy": policy,
                    "budget_wh": forecast["budget_wh"] if args.budget_wh is None else args.budget_wh})
        lab.operate("reset_experiment")
        lab.operate("arm")
        for _ in range(args.seconds):
            lab.step_demo()
        lab.operate("stop")
        result = lab.status()
        results.append({"policy": policy, **result["metrics"], "source": "synthetic"})
        (args.output / f"{policy}.json").write_text(json.dumps({"forecast": forecast, **result}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with (args.output / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print("합성 DC 부하 비교 완료; 실물 계측·교육 효과 검증 결과가 아닙니다.")
    for row in results:
        print(f"{row['policy']}: {row['energy_wh']:.4f} Wh / {row['job_revolutions']:.1f} 회전 / 완료 {row['job_completed']}")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
