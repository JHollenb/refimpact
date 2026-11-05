"""Compact read models for the week and game pages, from stored rows and generated tables."""

from __future__ import annotations

from refimpact.catalog import LARGE_SWING_PP
from refimpact.outcomes import MENUS, OPTIONS, menu, play_kind
from refimpact.penalties import finite, took_effect
from refimpact.scenarios import ordered, play_id


def week_card(game: dict, events: list[dict]) -> dict:
    """Biggest flag swing, big flags by beneficiary, and each team's swings added up."""
    home, away = game["home_team"], game["away_team"]
    priced = [e for e in events if took_effect(e) and e.get("home_wp_swing_pp") is not None]
    priced.sort(key=lambda e: abs(e["home_wp_swing_pp"]), reverse=True)
    biggest = None
    if priced:
        top = priced[0]
        swing = top["home_wp_swing_pp"]
        biggest = {
            "team": home if swing >= 0 else away,
            "pp": abs(swing),
            "qtr": top.get("qtr"),
            "time": top.get("time"),
            "ruling": " + ".join(f["type"] for f in top["fouls"]),
            "id": top["id"],
        }
    big = [e for e in priced if abs(e["home_wp_swing_pp"]) >= LARGE_SWING_PP]
    helped = {
        home: sum(e["home_wp_swing_pp"] > 0 for e in big),
        away: sum(e["home_wp_swing_pp"] < 0 for e in big),
    }
    # Each flag's swing at its own moment, added up. This isn't a change in final win chance.
    home_gained = sum(e["home_wp_swing_pp"] for e in priced if e["home_wp_swing_pp"] > 0)
    away_gained = -sum(e["home_wp_swing_pp"] for e in priced if e["home_wp_swing_pp"] < 0)
    return {
        "game_id": game["game_id"],
        "gameday": game["gameday"],
        "gametime": game.get("gametime"),
        "home_team": home,
        "away_team": away,
        "home_score": game.get("home_score"),
        "away_score": game.get("away_score"),
        "status": game["status"],
        "pbp_available": game.get("pbp_available", False),
        "flags": len(events),
        "priced": len(priced),
        "unpriced": game.get("unpriced_penalties"),
        "biggest": biggest,
        "big_calls": {"total": len(big), "by_team": helped},
        "swing_totals": {
            home: {"gained": home_gained, "lost": away_gained},
            away: {"gained": away_gained, "lost": home_gained},
        },
        "net_home_epa": game.get("net_home_epa"),
    }


def elapsed(row: dict) -> float | None:
    """Game seconds elapsed; overtime runs on past 3600."""
    if not finite(row.get("qtr")):
        return None
    if row["qtr"] <= 4:
        seconds = row.get("game_seconds_remaining")
        return 3600 - seconds if finite(seconds) else None
    seconds = row.get("half_seconds_remaining")
    return 3600 + 900 * (row["qtr"] - 5) + (600 - seconds) if finite(seconds) else None


def wp_of(row: dict, key: str) -> float | None:
    value = row.get(key)
    return float(value) if finite(value) and 0 <= value <= 1 else None


def flag_view(event: dict, row: dict | None) -> dict:
    fouls = event.get("fouls") or []
    return {
        "id": event["id"],
        "play_id": event["play_id"],
        "drive_id": event.get("drive_id"),
        "qtr": event.get("qtr"),
        "time": event.get("time"),
        "ruling": " + ".join(f["type"] for f in fouls) or "Flag",
        "on": fouls[0].get("team") if fouls else None,
        "disposition": event.get("disposition"),
        "home_swing": event.get("home_wp_swing_pp"),
        "home_swing_vegas": event.get("home_wp_vegas_swing_pp"),
        "approximate": bool(event.get("approximate")),
        "unpriced_reason": event.get("unpriced_reason"),
        "basis": event.get("enforcement_wp_basis"),
        "elapsed": elapsed(row) if row else None,
    }


def game_view(game: dict, rows: list[dict], events: list[dict], drives: list[dict]) -> dict:
    rows = ordered(rows)
    home = game["home_team"]
    by_play = {e["play_id"]: e for e in events}
    stored = {d["id"]: d for d in drives}

    shown = []
    for row in rows:
        kind = play_kind(row)
        if kind is None:
            continue
        pid = play_id(row)
        shown.append({"row": row, "kind": kind, "play_id": pid, "event": by_play.get(pid)})

    plays_by_drive: dict[str, list[dict]] = {}
    chart = []
    for position, item in enumerate(shown):
        row, kind, pid, event = item["row"], item["kind"], item["play_id"], item["event"]
        wp, vegas = wp_of(row, "home_wp"), wp_of(row, "vegas_home_wp")
        following = shown[position + 1]["row"] if position + 1 < len(shown) else None
        if following is not None:
            wp_after, vegas_after = wp_of(following, "home_wp"), wp_of(following, "vegas_home_wp")
        else:
            wp_after = wp_of(row, "home_wp_post")
            wpa = row.get("vegas_home_wpa")
            vegas_after = (
                max(0.0, min(1.0, vegas + wpa)) if vegas is not None and finite(wpa) else None
            )
        # Each row's change runs to the next shown row, so a game's changes add up exactly.
        drive_id = (
            f"{row['game_id']}:{int(row['fixed_drive'])}" if finite(row.get("fixed_drive")) else ""
        )
        play = {
            "play_id": pid,
            "drive_id": drive_id,
            "qtr": row.get("qtr"),
            "time": row.get("time"),
            "team": row.get("posteam"),
            "rival": row.get("defteam"),
            "down": row.get("down"),
            "ydstogo": row.get("ydstogo"),
            "yardline_100": row.get("yardline_100"),
            "description": row.get("desc"),
            "kind": kind,
            "menu": menu(kind, event),
            "home_wp": wp,
            "home_wp_after": wp_after,
            "vegas_wp": vegas,
            "vegas_wp_after": vegas_after,
            "delta_pp": 100 * (wp_after - wp) if wp is not None and wp_after is not None else None,
            "delta_vegas_pp": 100 * (vegas_after - vegas)
            if vegas is not None and vegas_after is not None
            else None,
            "flag": event["id"] if event else None,
        }
        plays_by_drive.setdefault(drive_id, []).append(play)
        x = elapsed(row)
        if x is not None and wp is not None:
            chart.append({"x": x, "wp": wp, "vegas": vegas, "play_id": pid})
    if shown:
        last = shown[-1]["row"]
        x, end = elapsed(last), wp_of(last, "home_wp_post")
        if x is not None and end is not None:
            chart.append(
                {
                    "x": x + 1,
                    "wp": end,
                    "vegas": plays_by_drive[next(reversed(plays_by_drive))][-1]["vegas_wp_after"],
                    "play_id": shown[-1]["play_id"],
                }
            )

    drive_rows = []
    for drive_id, plays in plays_by_drive.items():
        record = stored.get(drive_id)
        team = record["team"] if record else plays[-1]["team"]
        scrimmage = [p for p in plays if p["kind"] != "kickoff"] or plays
        start, end = scrimmage[0], plays[-1]
        flip = (lambda v: v) if team == home else (lambda v: None if v is None else 1 - v)
        drive_rows.append(
            {
                "id": drive_id,
                "number": int(drive_id.rsplit(":", 1)[1]) if drive_id else None,
                "team": team,
                "qtr": start["qtr"],
                "time": start["time"],
                "start_yardline_100": record["start_yardline_100"]
                if record
                else start["yardline_100"],
                "result": (record or {}).get("result") or "Kickoff",
                "plays": plays,
                "flags": sum(1 for p in plays if p["flag"]),
                # Stored drive values are the driving team's; these are the home team's.
                "home_wp_start": flip(record["wp_before"]) if record else start["home_wp"],
                "home_wp_end": flip(record["wp_after"]) if record else end["home_wp_after"],
                "vegas_wp_start": start["vegas_wp"],
                "vegas_wp_end": end["vegas_wp_after"],
            }
        )

    return {
        "game": {
            key: game.get(key)
            for key in (
                "game_id",
                "season",
                "week",
                "gameday",
                "gametime",
                "stadium",
                "status",
                "home_team",
                "away_team",
                "home_score",
                "away_score",
                "net_home_epa",
                "unpriced_penalties",
                "declined_penalties",
                "valued_penalties",
                "penalty_rows",
            )
        },
        "card": week_card(game, events),
        "chart": chart,
        "flags": [
            flag_view(e, next((s["row"] for s in shown if s["play_id"] == e["play_id"]), None))
            for e in events
        ],
        "drives": drive_rows,
        "options": {key: OPTIONS[key] for key in OPTIONS},
        "menus": MENUS,
    }
