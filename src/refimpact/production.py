"""Observed offensive/defensive production and drive accounting from native EPA.

Penalty alternatives are separate conditional estimates. They are never added to
the observed play/drive EPA that already includes enforcement.
"""

from __future__ import annotations

from collections import defaultdict

import polars as pl

from refimpact.penalties import finite, is_flagged, took_effect

SNAPS = {"run", "pass", "qb_kneel", "qb_spike"}
DRIVE_END_PLAYS = {"punt", "field_goal"}


def sum_or_none(values: list) -> float | None:
    return sum(values) if values else None


def build_production(frame: pl.DataFrame, penalties: list[dict]) -> tuple[dict, list[dict]]:
    order = ["game_id", "order_sequence" if "order_sequence" in frame.columns else "play_id"]
    rows = frame.sort(order).to_dicts()
    by_game, drive_rows = defaultdict(list), defaultdict(list)
    events = defaultdict(list)
    for event in penalties:
        if event["drive_id"]:
            events[event["drive_id"]].append(event)
    for row in rows:
        by_game[row["game_id"]].append(row)
        if finite(row.get("fixed_drive")) and row.get("play_type") != "kickoff":
            drive_rows[f"{row['game_id']}:{int(row['fixed_drive'])}"].append(row)
    production = {}
    for game_id, game_rows in by_game.items():
        teams = [game_rows[0]["home_team"], game_rows[0]["away_team"]]
        metrics = {}
        for team in teams:
            scrimmage = [
                r
                for r in game_rows
                if r.get("posteam") == team
                and r.get("down") in (1, 2, 3, 4)
                and (
                    r.get("play_type") in SNAPS
                    or (r.get("play_type") == "no_play" and is_flagged(r))
                )
            ]
            snaps = [r for r in scrimmage if r.get("play_type") in SNAPS]
            valid = [r["epa"] for r in scrimmage if finite(r.get("epa"))]
            snap_values = [r["epa"] for r in snaps if finite(r.get("epa"))]
            unflagged = [r["epa"] for r in snaps if not is_flagged(r) and finite(r.get("epa"))]
            metrics[team] = {
                "team": team,
                "offensive_epa": sum_or_none(valid),
                "snap_epa": sum_or_none(snap_values),
                "snaps": len(snaps),
                "snap_epa_rows": len(snap_values),
                "offensive_epa_per_snap": sum(snap_values) / len(snap_values)
                if snap_values
                else None,
                "unflagged_snap_epa": sum_or_none(unflagged),
                "unflagged_snap_count": len(unflagged),
                "administrative_epa": sum_or_none(
                    [
                        r["epa"]
                        for r in scrimmage
                        if r.get("play_type") == "no_play" and finite(r.get("epa"))
                    ]
                ),
                "missing_epa_rows": len(scrimmage) - len(valid),
            }
        for team, opponent in [(teams[0], teams[1]), (teams[1], teams[0])]:
            metrics[team]["defensive_epa_allowed"] = metrics[opponent]["offensive_epa"]
            metrics[team]["defensive_epa_allowed_per_snap"] = metrics[opponent][
                "offensive_epa_per_snap"
            ]
            metrics[team]["defensive_snaps"] = metrics[opponent]["snaps"]
        production[game_id] = metrics
    drives = []
    for drive_id, group in drive_rows.items():
        action = [
            r
            for r in group
            if r.get("down") in (1, 2, 3, 4)
            and (r.get("play_type") in SNAPS | DRIVE_END_PLAYS or is_flagged(r))
        ]
        if not action:
            continue
        first = action[0]
        team = first.get("posteam")
        if not team:
            continue
        # The row's EPA is expressed for its possession team. Normalize all drive
        # rows to the driving team, including unusual change-of-possession records.
        values = [
            r["epa"] * (1 if r.get("posteam") == team else -1)
            for r in action
            if finite(r.get("epa")) and r.get("posteam")
        ]
        cumulative = 0.0
        timeline = []
        for r in action:
            epa = (
                r["epa"] * (1 if r.get("posteam") == team else -1)
                if finite(r.get("epa")) and r.get("posteam")
                else None
            )
            if epa is not None:
                cumulative += epa
            timeline.append(
                {
                    "play_id": str(int(r["play_id"])),
                    "qtr": r.get("qtr"),
                    "time": r.get("time"),
                    "down": r.get("down"),
                    "ydstogo": r.get("ydstogo"),
                    "yardline_100": r.get("yardline_100"),
                    "description": r.get("desc"),
                    "epa": epa,
                    "cumulative_epa": cumulative if values else None,
                    "flagged": is_flagged(r),
                    "play_type": r.get("play_type"),
                }
            )
        known_effects = [
            e["enforcement_epa"] * (1 if e["reference_team"] == team else -1)
            for e in events[drive_id]
            if e["enforcement_epa"] is not None
        ]
        outcome = next(
            (r.get("fixed_drive_result") for r in reversed(group) if r.get("fixed_drive_result")),
            None,
        )
        drives.append(
            {
                "id": drive_id,
                "game_id": first["game_id"],
                "season": first["season"],
                "week": first["week"],
                "drive": int(first["fixed_drive"]),
                "team": team,
                "qtr_start": first.get("qtr"),
                "clock_start": first.get("time"),
                "start_yardline_100": first.get("yardline_100"),
                "result": outcome,
                "drive_epa": sum_or_none(values),
                "epa_rows": len(values),
                "missing_epa_rows": len(action) - len(values),
                "penalty_events": len(events[drive_id]),
                "penalty_ids": [e["id"] for e in events[drive_id]],
                "known_penalty_enforcement_epa": sum_or_none(known_effects),
                "penalty_effects_valued": sum(
                    took_effect(e) and e["enforcement_epa"] is not None for e in events[drive_id]
                ),
                "penalty_events_took_effect": sum(took_effect(e) for e in events[drive_id]),
                "timeline": timeline,
                "accounting_note": "Drive EPA is the sum of native play EPA, including "
                "enforcement, "
                "punts and field goals; kickoffs and tries are excluded. Immediate "
                "penalty effects are a separate ledger and must not be added again. "
                "This is not a replay of the drive without penalties.",
            }
        )
    for game_id, metrics in production.items():
        for team, value in metrics.items():
            team_drives = [d for d in drives if d["game_id"] == game_id and d["team"] == team]
            value["drives"] = len(team_drives)
            value["drive_epa"] = sum_or_none(
                [d["drive_epa"] for d in team_drives if d["drive_epa"] is not None]
            )
    return production, drives
