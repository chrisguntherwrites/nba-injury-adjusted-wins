#!/usr/bin/env python3
"""Build the Injury Adjusted Wins dashboard (index.html) from the two daily CSVs.

Usage (run from the folder holding the CSVs):
    python build_dashboard.py
or with explicit paths:
    python build_dashboard.py --wins path/to/running_injury_adjusted_wins.csv \
        --players path/to/running_injured_players.csv --out path/to/index.html

Needs only Python 3.8+ (standard library). template.html must sit next to this script.
"""
import argparse
import csv
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

WINS_COLS = {"GAME_ID", "GAME_DATE", "TEAM_ABBREVIATION", "ACTUAL_WIN",
             "ADJUSTED_WIN", "OWN_INJURED_DPM_PCT", "OPP_INJURED_DPM_PCT"}
PLAYERS_COLS = {"GAME_ID", "TEAM_ABBREVIATION", "PLAYER_NAME",
                "PLAYER_DPM_SCALED", "PCT_OF_TEAM_DPM"}


def read_csv_text(path, required, label):
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: {label} file not found: {p}")
    text = p.read_text(encoding="utf-8-sig").strip()
    header = next(csv.reader(io.StringIO(text.splitlines()[0])))
    missing = required - set(header)
    if missing:
        sys.exit(f"ERROR: {label} is missing columns: {', '.join(sorted(missing))}")
    if len(text.splitlines()) < 2:
        sys.exit(f"ERROR: {label} has no data rows")
    return text


def js_template_safe(text):
    """Make text safe to place inside a JavaScript template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wins", default="running_injury_adjusted_wins.csv")
    ap.add_argument("--players", default="running_injured_players.csv")
    ap.add_argument("--template", default=str(HERE / "template.html"))
    ap.add_argument("--out", default="index.html")
    a = ap.parse_args()

    wins = read_csv_text(a.wins, WINS_COLS, "wins CSV")
    players = read_csv_text(a.players, PLAYERS_COLS, "players CSV")

    tpl = Path(a.template).read_text(encoding="utf-8")
    for marker in ("__WINS_CSV__", "__PLAYERS_CSV__"):
        if tpl.count(marker) != 1:
            sys.exit(f"ERROR: template must contain {marker} exactly once")

    html = (tpl.replace("__WINS_CSV__", js_template_safe(wins))
               .replace("__PLAYERS_CSV__", js_template_safe(players)))
    Path(a.out).write_text(html, encoding="utf-8")
    print(f"Wrote {a.out} ({len(html) // 1024} KB) from "
          f"{len(wins.splitlines()) - 1} game rows and "
          f"{len(players.splitlines()) - 1} injured-player rows.")


if __name__ == "__main__":
    main()
