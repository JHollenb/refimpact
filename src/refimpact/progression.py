"""Observed game progression and separate cumulative penalty-comparison ledgers.

Win chance uses native nflfastR home_wp/home_wp_post, never a sum of ruling swings.
"""

from __future__ import annotations

from collections import defaultdict

import polars as pl

from refimpact.penalties import finite
from refimpact.production import SNAPS


def probability(value):
    return float(value) if finite(value) and 0 <= value <= 1 else None


def difference(after, before):
    return 100 * (after - before) if after is not None and before is not None else None


def offensive_row(row):
    return row.get("down") in (1, 2, 3, 4) and (
        row.get("play_type") in SNAPS
        or (row.get("play_type") == "no_play" and row.get("_flagged", False))
    )


def build_progression(frame: pl.DataFrame, penalties: list[dict], drives: list[dict]) -> dict:
    """Attach actual WP/progression to events/drives and return one timeline per game."""
    order = ["game_id", "order_sequence" if "order_sequence" in frame.columns else "play_id"]
    by_game = defaultdict(list)
    for row in frame.sort(order).to_dicts():
        by_game[row["game_id"]].append(row)
    events = {e["id"]: e for e in penalties}
    drive_map = {d["id"]: d for d in drives}
    result = {}
    for game_id, rows in by_game.items():
        home, away = rows[0]["home_team"], rows[0]["away_team"]
        teams = (home, away)
        cumulative = {team: 0.0 for team in teams}
        offense_known = {team: 0 for team in teams}
        offense_missing = {team: 0 for team in teams}
        penalty_total, penalty_known, penalty_missing = 0.0, 0, 0
        parts = {"on_offense": [], "on_defense": []}
        timeline, drive_windows = [], defaultdict(list)
        initial_wp, latest_wp = None, None
        for row in rows:
            play_id = str(int(row["play_id"]))
            event = events.get(f"{game_id}:{play_id}")
            row["_flagged"] = event is not None
            before = probability(row.get("home_wp"))
            after = probability(row.get("home_wp_post"))
            terminal = (
                row.get("play_type_nfl") == "END_GAME"
                or (row.get("desc") or "").strip() == "END GAME"
            )
            marker = terminal or row.get("play_type_nfl") in {"END_QUARTER", "GAME_START"}
            terminal_tie = (
                terminal
                and finite(row.get("total_home_score"))
                and finite(row.get("total_away_score"))
                and row["total_home_score"] == row["total_away_score"]
            )
            # A final record is an observed endpoint, not another football play.
            if marker:
                after = before
            if terminal_tie:
                before, after = None, None
            if initial_wp is None and before is not None:
                initial_wp = before
            if terminal_tie:
                latest_wp = None
            elif after is not None or before is not None:
                latest_wp = after if after is not None else before
            possession = row.get("posteam")
            epa = float(row["epa"]) if finite(row.get("epa")) else None
            if offensive_row(row) and possession in teams:
                if epa is not None:
                    cumulative[possession] += epa
                    offense_known[possession] += 1
                else:
                    offense_missing[possession] += 1
            if event:
                impact = event.get("home_epa_impact")
                if impact is not None:
                    penalty_total += impact
                    penalty_known += 1
                    parts[
                        "on_offense" if event.get("reference_team") == home else "on_defense"
                    ].append(impact)
                else:
                    penalty_missing += 1
            drive_id = (
                f"{game_id}:{int(row['fixed_drive'])}" if finite(row.get("fixed_drive")) else None
            )
            point = {
                "play_id": play_id,
                "qtr": row.get("qtr"),
                "time": row.get("time"),
                "play_type": row.get("play_type"),
                "description": row.get("desc"),
                "possession": possession,
                "down": row.get("down"),
                "ydstogo": row.get("ydstogo"),
                "yardline_100": row.get("yardline_100"),
                "drive_id": drive_id,
                "penalty_id": event["id"] if event else None,
                "epa": epa,
                "epa_reference_team": possession,
                "cumulative_offense_epa": {
                    team: cumulative[team] if offense_known[team] else None for team in teams
                },
                "missing_offense_epa_rows": dict(offense_missing),
                "cumulative_home_penalty_benefit_epa": penalty_total if penalty_known else None,
                "penalty_effects_known": penalty_known,
                "penalty_effects_missing": penalty_missing,
                "home_wp_before": before,
                "home_wp_after": after,
                "home_wp_change_pp": difference(after, before) if not marker else None,
                "home_wp_change_from_start_pp": difference(latest_wp, initial_wp),
                "terminal": terminal,
            }
            timeline.append(point)
            if drive_id in drive_map and row.get("play_type") != "kickoff":
                drive_windows[drive_id].append(point)
            if event:
                event["progression"] = {
                    key: point[key]
                    for key in [
                        "home_wp_before",
                        "home_wp_after",
                        "home_wp_change_pp",
                        "home_wp_change_from_start_pp",
                        "cumulative_offense_epa",
                        "cumulative_home_penalty_benefit_epa",
                        "penalty_effects_known",
                        "penalty_effects_missing",
                    ]
                }
        game_drives = []
        drive_cumulative = {team: 0.0 for team in teams}
        drive_known = {team: 0 for team in teams}
        drive_missing = {team: 0 for team in teams}
        for drive_id, window in drive_windows.items():
            drive = drive_map[drive_id]
            # GAME_START/comments may share a fixed_drive with the opening kickoff.
            # Anchor at the first scrimmage/penalty action used by the drive ledger,
            # so excluding kickoff rows also excludes their before/after effect.
            first_action = drive["timeline"][0]["play_id"]
            window = window[
                next(i for i, point in enumerate(window) if point["play_id"] == first_action) :
            ]
            team = drive["team"]
            sign = 1 if team == home else -1
            start = window[0]["home_wp_before"]
            # Do not silently substitute an earlier point for a missing endpoint.
            end = window[-1]["home_wp_after"]
            drive["start_play_id"] = window[0]["play_id"]
            drive["end_play_id"] = window[-1]["play_id"]
            drive["wp_before"] = start if sign == 1 or start is None else 1 - start
            drive["wp_after"] = end if sign == 1 or end is None else 1 - end
            drive["wp_change_pp"] = difference(drive["wp_after"], drive["wp_before"])
            if drive["drive_epa"] is not None:
                drive_cumulative[team] += drive["drive_epa"]
                drive_known[team] += 1
            drive_missing[team] += drive["missing_epa_rows"]
            drive["cumulative_team_drive_epa"] = (
                drive_cumulative[team] if drive_known[team] else None
            )
            drive["cumulative_missing_epa_rows"] = drive_missing[team]
            drive["wp_note"] = (
                "Native nflfastR win chance for the driving team, from the first scrimmage/penalty "
                "recorded drive event to its final recorded event; includes tries/admin assigned "
                "to that drive, excludes kickoffs. A missing endpoint remains unknown."
            )
            by_id = {p["play_id"]: p for p in window}
            for play in drive["timeline"]:
                p = by_id[play["play_id"]]
                pre, post = p["home_wp_before"], p["home_wp_after"]
                play.update(
                    wp_before=pre if sign == 1 or pre is None else 1 - pre,
                    wp_after=post if sign == 1 or post is None else 1 - post,
                    wp_change_pp=p["home_wp_change_pp"] * sign
                    if p["home_wp_change_pp"] is not None
                    else None,
                )
            game_drives.append(
                {
                    key: drive[key]
                    for key in [
                        "id",
                        "drive",
                        "team",
                        "drive_epa",
                        "cumulative_team_drive_epa",
                        "wp_before",
                        "wp_after",
                        "wp_change_pp",
                        "missing_epa_rows",
                        "end_play_id",
                    ]
                }
            )
        result[game_id] = {
            "game_id": game_id,
            "home_team": home,
            "away_team": away,
            "initial_home_wp": initial_wp,
            "latest_home_wp": latest_wp,
            "home_wp_change_from_start_pp": difference(latest_wp, initial_wp),
            "penalty_benefit": {
                "home_net_epa": penalty_total if penalty_known else None,
                "home_on_offense_epa": sum(parts["on_offense"]) if parts["on_offense"] else None,
                "home_on_defense_epa": sum(parts["on_defense"]) if parts["on_defense"] else None,
                "known_events": penalty_known,
                "missing_events": penalty_missing,
            },
            "timeline": timeline,
            "drives": game_drives,
            "note": "Observed offense EPA already includes actual penalties. Estimated penalty "
            "benefit is a separate sum of local comparisons across both possessions, never added "
            "to production. Native win chance is a state probability; ruling swings are not summed "
            "into a no-penalties probability. Cumulative EPA uses "
            "available rows; missing rows are disclosed.",
        }
    return result
