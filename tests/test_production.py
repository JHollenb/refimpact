import polars as pl
import pytest

from refimpact.production import build_production


def frame(values):
    return pl.DataFrame(
        [
            {
                "game_id": "2026_08_ARI_BUF",
                "season": 2026,
                "week": 8,
                "play_id": float(i + 1),
                "home_team": "BUF",
                "away_team": "ARI",
                "posteam": "BUF",
                "qtr": 1,
                "time": "10:00",
                "down": 1,
                "ydstogo": 10,
                "yardline_100": 60,
                "fixed_drive": 1,
                "fixed_drive_result": "Punt",
                "play_type": "run" if i != 1 else "no_play",
                "epa": v,
                "penalty": int(i == 1),
                "desc": "PENALTY on BUF, False Start, 5 yards." if i == 1 else "Run",
            }
            for i, v in enumerate(values)
        ]
    )


def test_drive_epa_counts_native_penalty_epa_once_and_defense_mirrors_opponent():
    production, drives = build_production(
        frame([1.0, -0.5, 2.0]),
        [
            {
                "drive_id": "2026_08_ARI_BUF:1",
                "enforcement_epa": -0.6,
                "reference_team": "BUF",
                "id": "p",
            }
        ],
    )
    drive = drives[0]
    assert drive["drive_epa"] == 2.5  # Not 1.9: don't add enforcement again.
    assert drive["known_penalty_enforcement_epa"] == -0.6
    assert drive["timeline"][-1]["cumulative_epa"] == 2.5
    buf, ari = production["2026_08_ARI_BUF"]["BUF"], production["2026_08_ARI_BUF"]["ARI"]
    assert buf["offensive_epa"] == 2.5
    assert buf["offensive_epa_per_snap"] == 1.5
    assert ari["defensive_epa_allowed"] == buf["offensive_epa"]
    assert ari["offensive_epa"] is None


def test_missing_native_epa_is_disclosed():
    _, drives = build_production(frame([None, -0.5, 2.0]), [])
    assert drives[0]["missing_epa_rows"] == 1
    assert drives[0]["drive_epa"] == pytest.approx(1.5)
    assert drives[0]["timeline"][0]["epa"] is None
