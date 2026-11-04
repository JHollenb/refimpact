"""Descriptive game-level associations, never counterfactual final scores."""

from __future__ import annotations

import math

import numpy as np


def correlation(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return None
    value = float(np.corrcoef(x, y)[0, 1])
    return value if math.isfinite(value) else None


def relationships(games: list[dict], metric: str = "penalty_epa") -> dict:
    """One home-oriented row per completed, measured game, avoiding mirrored samples."""
    rows = []
    fields = {
        "penalty_epa": "net_home_epa",
        "production_epa": "net_home_production_epa_per_snap",
        "drive_epa": "net_home_drive_epa",
    }
    field = fields[metric]
    for g in games:
        if (
            g["status"] != "final"
            or not g["pbp_complete"]
            or (metric == "penalty_epa" and g.get("valued_penalties", g["scored_calls"]) == 0)
            or g.get(field) is None
        ):
            continue
        rows.append(
            {
                "game_id": g["game_id"],
                "home_team": g["home_team"],
                "away_team": g["away_team"],
                "week": g["week"],
                "net_home_epa": g[field],
                "metric_value": g[field],
                "home_margin": g["home_score"] - g["away_score"],
                "total_points": g["home_score"] + g["away_score"],
                "scored_calls": g.get("valued_penalties", g["scored_calls"]),
                "penalty_rows": g["penalty_rows"],
                "flags_took_effect": g.get("penalty_coverage", {}).get(
                    "took_effect", g["penalty_rows"]
                ),
            }
        )
    x = np.array([r["net_home_epa"] for r in rows])
    y = np.array([r["home_margin"] for r in rows])
    r = correlation(x, y)
    # Exploratory game-resampling interval. Same teams recur; this is not a
    # team-clustered or causal confidence statement.
    rng = np.random.default_rng(20261103)
    boot = []
    if r is not None:
        for _ in range(1500):
            idx = rng.integers(0, len(rows), len(rows))
            value = correlation(x[idx], y[idx])
            if value is not None:
                boot.append(value)
    ci = [float(v) for v in np.quantile(boot, [0.025, 0.975])] if boot else None
    decisive = [row for row in rows if row["home_margin"] != 0 and row["net_home_epa"] != 0]
    aligned = sum((row["home_margin"] > 0) == (row["net_home_epa"] > 0) for row in decisive)
    home_won = np.array([float(row["home_margin"] > 0) for row in rows if row["home_margin"] != 0])
    non_tie_x = np.array([row["net_home_epa"] for row in rows if row["home_margin"] != 0])
    return {
        "unit": "one game; home-team perspective",
        "metric": metric,
        "metric_label": {
            "penalty_epa": "Expected margin shift for home team, ignores score (points)",
            "production_epa": "Home minus away offensive EPA per snap",
            "drive_epa": "Home minus away observed drive EPA (points)",
        }[metric],
        "games": len(rows),
        "total_final_games": sum(g["status"] == "final" for g in games),
        "excluded_games": len(games) - len(rows),
        "excluded_final_games": sum(g["status"] == "final" for g in games) - len(rows),
        "not_final_games": sum(g["status"] != "final" for g in games),
        "pearson_r_margin": r,
        "bootstrap_95_interval": ci,
        "bootstrap_replicates": len(boot),
        "point_biserial_r_home_win": correlation(non_tie_x, home_won),
        "benefited_team_wins": aligned,
        "decisive_games_with_nonzero_impact": len(decisive),
        "benefited_team_win_rate": aligned / len(decisive) if decisive else None,
        "scored_calls": sum(row["scored_calls"] for row in rows),
        "penalty_rows": sum(row["penalty_rows"] for row in rows),
        # Coverage's denominator: a declined flag has no effect to price.
        "flags_took_effect": sum(row["flags_took_effect"] for row in rows),
        "points": rows,
        "interpretation": (
            "Descriptive association for the selected metric. A positive correlation means "
            "home teams with higher metric values tended to have larger final margins. "
            "This does not identify a causal effect or whether rulings were correct."
        ),
        "limitations": [
            "All reported penalty dispositions are retained; missing foul spots and clock context "
            "produce scenarios or unknown values, not fabricated point estimates.",
            "No measured calls or incomplete play-by-play is missing evidence, not zero impact.",
            "Net EPA sums local enforcement estimates; it is not a "
            "score adjustment or game replay.",
            "Game-resampling intervals are exploratory; teams recur across games.",
            "Team strength, game state, coaching and penalty opportunities are not controlled.",
            "WP already uses score and time; its relation to winning "
            "is partly built into the model.",
            "Same-game production and drive EPA are descriptive "
            "accounting, not pregame predictions "
            "or controls for a penalty's downstream effect. "
            "Penalties are already included in native EPA.",
        ],
    }
