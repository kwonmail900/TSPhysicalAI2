"""Past-only daily energy forecast used to set an educational model's Wh budget."""
import csv
import io
import math
import statistics
from datetime import date, timedelta


def demo_history():
    start = date(2025, 1, 6)
    return [{"date": (start + timedelta(days=i)).isoformat(),
             "energy_kwh": round((450 if (start + timedelta(days=i)).weekday() < 5 else 280)
                                  + 25 * math.sin(i * .7), 3)} for i in range(35)]


def parse_history(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("일별 CSV는 UTF-8 텍스트 1MB 이하로 입력하세요.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != ["date", "energy_kwh"]:
        raise ValueError("CSV 헤더는 date,energy_kwh 순서여야 합니다.")
    rows = []
    previous = None
    try:
        for n, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{n}행: 열 개수를 확인하세요.")
            day = date.fromisoformat(row["date"])
            energy = float(row["energy_kwh"])
            if not math.isfinite(energy) or not 0 <= energy <= 1e12:
                raise ValueError(f"{n}행: 유효한 비음수 kWh가 필요합니다.")
            if previous is not None and day - previous != timedelta(days=1):
                raise ValueError(f"{n}행: 날짜 중복·결측·역순을 먼저 해결하세요.")
            rows.append({"date": day.isoformat(), "energy_kwh": energy})
            previous = day
            if len(rows) > 10000:
                raise ValueError("최대 10,000일 자료까지 지원합니다.")
    except (csv.Error, TypeError, OverflowError) as exc:
        raise ValueError("일별 CSV 형식을 확인하세요.") from exc
    if len(rows) < 8:
        raise ValueError("연속된 일별 자료가 최소 8일 필요합니다.")
    return rows


def make_forecast(csv_text=None, scale_wh_per_kwh=.002, model="seasonal_naive"):
    if isinstance(scale_wh_per_kwh, bool):
        raise ValueError("환산 계수는 유한한 양수여야 합니다.")
    try:
        scale = float(scale_wh_per_kwh)
    except (ValueError, TypeError) as exc:
        raise ValueError("환산 계수를 확인하세요.") from exc
    if not math.isfinite(scale) or not 0 < scale <= 10:
        raise ValueError("환산 계수는 0보다 크고 10 이하여야 합니다.")
    if model not in ("seasonal_naive", "weekday_mean"):
        raise ValueError("지원하는 예측은 seasonal_naive 또는 weekday_mean입니다.")
    rows = parse_history(csv_text) if csv_text is not None else demo_history()

    def predict(target):
        lags = [7] if model == "seasonal_naive" else [7, 14, 21, 28]
        return statistics.fmean(rows[target - lag]["energy_kwh"] for lag in lags if target >= lag)

    prediction = predict(len(rows))
    errors = [abs(rows[i]["energy_kwh"] - predict(i)) for i in range(7, len(rows))]
    total = sum(row["energy_kwh"] for row in rows[7:])
    return {"model": model, "source": "uploaded" if csv_text is not None else "synthetic",
            "predicted_kwh": prediction, "budget_wh": prediction * scale,
            "scale_wh_per_kwh": scale, "history_days": len(rows),
            "cutoff_date": rows[-1]["date"],
            "target_date": (date.fromisoformat(rows[-1]["date"]) + timedelta(days=1)).isoformat(),
            "mae_kwh": statistics.fmean(errors), "wape": sum(errors) / total if total else None,
            "backtest_n": len(errors), "history": rows,
            "note": "과거 일별 kWh 예측을 교육 모형의 Wh 예산으로 환산합니다. 실제 건물의 시간별 부하나 설비별 비중을 복원하지 않습니다."}
