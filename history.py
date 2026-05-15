import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = Path("picks_history.csv")
FIELDNAMES = [
    "date", "sport", "teams", "pick", "odds", "confidence",
    "units", "pick_type", "whop_id", "result", "profit_loss",
]


def _ensure_csv():
    if not HISTORY_FILE.exists():
        with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def log_picks(picks: list[dict], date: str | None = None) -> None:
    _ensure_csv()
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        for p in picks:
            writer.writerow({
                "date": date,
                "sport": p.get("sport", ""),
                "teams": p.get("teams", ""),
                "pick": p.get("pick", ""),
                "odds": p.get("odds", ""),
                "confidence": p.get("confidence", ""),
                "units": p.get("units", ""),
                "pick_type": p.get("pick_type", "top"),
                "whop_id": p.get("whop_id", ""),
                "result": "",
                "profit_loss": "",
            })


def update_pick_result(whop_id: str, result: str) -> bool:
    """
    Find the row with matching whop_id and fill in result + profit_loss.
    Returns True if a row was updated.
    """
    _ensure_csv()
    rows = []
    updated = False

    with open(HISTORY_FILE, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if row.get("whop_id") == whop_id and row.get("result") == "":
            row["result"] = result.upper()[0]  # W / L / P
            try:
                units = float(row.get("units") or 1)
                odds_str = row.get("odds", "")
                row["profit_loss"] = f"{_calc_pl(odds_str, units, result):.2f}"
            except Exception:
                pass
            updated = True
            break

    if updated:
        with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

    return updated


def _calc_pl(odds_str: str, units: float, result: str) -> float:
    result = result.lower()
    if result == "push":
        return 0.0
    if result == "loss":
        return -units
    try:
        o = int(odds_str)
        return units * (o / 100) if o > 0 else units * (100 / abs(o))
    except (ValueError, ZeroDivisionError):
        return units


def _aggregate_rows(rows: list[dict]) -> dict:
    wins = losses = pushes = 0
    unit_pl = 0.0
    for row in rows:
        result = row.get("result", "").upper()
        try:
            units = float(row.get("units") or 1)
        except ValueError:
            units = 1.0
        if result == "W":
            wins += 1
            unit_pl += _calc_pl(row.get("odds", ""), units, "win")
        elif result == "L":
            losses += 1
            unit_pl -= units
        elif result == "P":
            pushes += 1
    total_decided = wins + losses
    win_rate = (wins / total_decided * 100) if total_decided > 0 else 0.0
    return {"wins": wins, "losses": losses, "pushes": pushes, "win_rate": win_rate, "unit_pl": unit_pl}


def get_yesterday_record() -> dict:
    _ensure_csv()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    with open(HISTORY_FILE, "r", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date") == yesterday]
    stats = _aggregate_rows(rows)
    stats["date"] = yesterday
    return stats


def get_weekly_stats() -> dict:
    _ensure_csv()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    with open(HISTORY_FILE, "r", newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date", "") >= cutoff]
    return _aggregate_rows(rows)


def get_all_stats() -> dict:
    _ensure_csv()
    with open(HISTORY_FILE, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return _aggregate_rows(rows)
