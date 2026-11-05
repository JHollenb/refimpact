"""Team form from play-by-play: last-4-game metrics, shrinkage, in-game momentum and table cells.

Shared by the fit job (scripts/fit_conversion/worker.py) and the running API, so the numbers a fit
was validated on are computed the way the page computes them. Needs only polars.
"""

from __future__ import annotations

from collections import defaultdict, deque

import polars as pl

WINDOW_GAMES = 4
HEAT_SNAPS = 5
SNAPS = {"run", "pass", "qb_kneel", "qb_spike"}
SHAPE = (4, 14, 6)
GARBAGE_WP = 0.05

SUM_COLUMNS = [
    "plays",
    "epa",
    "success",
    "early_n",
    "early",
    "conv_n",
    "conv",
    "explosive",
    "drop",
    "drop_epa",
    "rush",
    "rush_epa",
    "cpoe_n",
    "cpoe",
    "sack",
    "hit",
]

# name, plain words, numerator, denominator, minimum denominator, higher is better for an offense
METRICS = [
    ("epa_play", "EPA per play", "epa", "plays", 120, True),
    ("success", "success rate", "success", "plays", 120, True),
    ("early", "early-down success", "early", "early_n", 80, True),
    ("conv", "third- and fourth-down conversions", "conv", "conv_n", 25, True),
    ("explosive", "explosive plays", "explosive", "plays", 120, True),
    ("pass_epa", "passing EPA", "drop_epa", "drop", 60, True),
    ("rush_epa", "rushing EPA", "rush_epa", "rush", 40, True),
    ("cpoe", "completion rate over expected", "cpoe", "cpoe_n", 50, True),
    ("sack", "sacks", "sack", "drop", 60, False),
    ("hit", "QB hits", "hit", "drop", 60, False),
]


def togo_bucket(togo: float) -> int:
    """Index of to-go bucket: 1..10 yards, then 11-13, 14-16, 17-20, 21+."""
    value = max(1, int(round(togo)))
    if value <= 10:
        return value - 1
    return 10 if value <= 13 else 11 if value <= 16 else 12 if value <= 20 else 13


def zone(yardline_100: float) -> int:
    """Index of field zone by yards to the opponent's goal."""
    value = yardline_100
    return (
        0
        if value <= 10
        else 1
        if value <= 20
        else 2
        if value <= 40
        else (3 if value <= 60 else 4 if value <= 80 else 5)
    )


def offensive_row(row: dict) -> bool:
    return row.get("down") in (1, 2, 3, 4) and (
        row.get("play_type") in SNAPS
        or (row.get("play_type") == "no_play" and row.get("penalty") == 1)
    )


class Heat:
    """Native EPA over each team's last five scrimmage snaps, fed in game order."""

    def __init__(self):
        self.snaps = defaultdict(lambda: deque(maxlen=HEAT_SNAPS))

    def value(self, team: str | None) -> float:
        return sum(self.snaps[team]) if team else 0.0

    def count(self, team: str | None) -> int:
        return len(self.snaps[team]) if team else 0

    def add(self, row: dict) -> None:
        if offensive_row(row):
            epa = row.get("epa")
            self.snaps[row.get("posteam")].append(epa if epa == epa and epa is not None else 0.0)


def heat_before(rows: list[dict], index: int) -> Heat:
    """Heat from the plays before `index`; `rows` are one game in order."""
    heat = Heat()
    for row in rows[:index]:
        heat.add(row)
    return heat


def unit_frame(frame: pl.DataFrame, *, garbage_filter: bool = True) -> pl.DataFrame:
    """One row per (game, team, unit) of summable metric parts."""
    rows = frame.filter(
        pl.col("play_type").is_in(["run", "pass"])
        & (pl.col("qtr") <= 4)
        & pl.col("epa").is_not_null()
        & (pl.col("play_deleted").fill_null(0) != 1)
    )
    if garbage_filter:
        rows = rows.filter(
            ~(
                (pl.col("qtr") >= 3)
                & ((pl.col("wp") < GARBAGE_WP) | (pl.col("wp") > 1 - GARBAGE_WP))
            )
        )

    def flag(name: str) -> pl.Expr:
        return pl.col(name).fill_null(0)

    drop = flag("qb_dropback")
    rush = ((flag("rush") == 1) & (drop == 0)).cast(pl.Float64)
    early = (pl.col("down") <= 2).cast(pl.Float64).fill_null(0.0)
    big = ((pl.col("play_type") == "run") & (pl.col("yards_gained") >= 10)) | (
        (pl.col("play_type") == "pass") & (pl.col("yards_gained") >= 20)
    )
    parts = [
        pl.len().alias("plays"),
        pl.col("epa").sum().alias("epa"),
        flag("success").sum().alias("success"),
        early.sum().alias("early_n"),
        (flag("success") * early).sum().alias("early"),
        (
            flag("third_down_converted")
            + flag("third_down_failed")
            + flag("fourth_down_converted")
            + flag("fourth_down_failed")
        )
        .sum()
        .alias("conv_n"),
        (flag("third_down_converted") + flag("fourth_down_converted")).sum().alias("conv"),
        big.fill_null(False).cast(pl.Float64).sum().alias("explosive"),
        drop.sum().alias("drop"),
        (pl.col("epa") * drop).sum().alias("drop_epa"),
        rush.sum().alias("rush"),
        (pl.col("epa") * rush).sum().alias("rush_epa"),
        pl.col("cpoe").is_not_null().sum().alias("cpoe_n"),
        pl.col("cpoe").sum().alias("cpoe"),
        flag("sack").sum().alias("sack"),
        flag("qb_hit").sum().alias("hit"),
    ]
    keys = ["season", "game_id", "game_date"]
    offense = rows.group_by([*keys, pl.col("posteam").alias("team")]).agg(parts)
    defense = rows.group_by([*keys, pl.col("defteam").alias("team")]).agg(parts)
    return pl.concat(
        [
            offense.with_columns(pl.lit("offense").alias("unit")),
            defense.with_columns(pl.lit("defense").alias("unit")),
        ]
    ).with_columns(pl.col(SUM_COLUMNS).cast(pl.Float64).fill_null(0.0))


def empty_sums() -> dict[str, float]:
    return dict.fromkeys(SUM_COLUMNS, 0.0)


def ratio(sums: dict, numerator: str, denominator: str) -> float | None:
    return sums[numerator] / sums[denominator] if sums[denominator] else None


class FormTable:
    """Each team's games by unit, with league means per season for centering."""

    def __init__(self, units: pl.DataFrame):
        self.games: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in units.sort("game_date").to_dicts():
            self.games[(row["team"], row["unit"])].append(row)
        self.league: dict[int, dict[str, float]] = {}
        totals: dict[int, dict] = defaultdict(empty_sums)
        for row in units.filter(pl.col("unit") == "offense").to_dicts():
            for name in SUM_COLUMNS:
                totals[row["season"]][name] += row[name]
        for season, sums in totals.items():
            self.league[season] = {
                "all": ratio(sums, "epa", "plays") or 0.0,
                "pass": ratio(sums, "drop_epa", "drop") or 0.0,
                "run": ratio(sums, "rush_epa", "rush") or 0.0,
            }

    def teams(self, season: int) -> list[str]:
        return sorted(
            {
                team
                for (team, unit), games in self.games.items()
                if games and any(g["season"] == season for g in games)
            }
        )

    def window(self, season: int, team: str, unit: str, before: str) -> tuple[dict, list[str]]:
        """Sums over the team's last four games before a date in a season; byes are skipped."""
        played = [
            g
            for g in self.games.get((team, unit), [])
            if g["season"] == season and g["game_date"] < before
        ][-WINDOW_GAMES:]
        sums = empty_sums()
        for game in played:
            for name in SUM_COLUMNS:
                sums[name] += game[name]
        return sums, [g["game_id"] for g in played]

    def season_values(self, season: int, team: str) -> dict[str, float] | None:
        """A team's whole-season centered values, in the frame where good is positive."""
        league = self.league.get(season)
        if league is None:
            return None
        result = {}
        for unit, sign, names in (
            ("offense", 1, ("O", "Op", "Or")),
            ("defense", -1, ("D", "Dp", "Dr")),
        ):
            sums = empty_sums()
            for game in self.games.get((team, unit), []):
                if game["season"] == season:
                    for name in SUM_COLUMNS:
                        sums[name] += game[name]
            if not sums["plays"]:
                return None
            for key, part, base in zip(
                names,
                (("epa", "plays"), ("drop_epa", "drop"), ("rush_epa", "rush")),
                ("all", "pass", "run"),
                strict=True,
            ):
                result[key] = sign * ((ratio(sums, *part) or league[base]) - league[base])
        return result


def shrunk(n: float, mean: float, k: float, target: float) -> float:
    return (n * mean + k * target) / (n + k)


def form_values(
    table: FormTable,
    season: int,
    team: str,
    before: str,
    *,
    k: float,
    r: dict[str, float],
    prior: dict[str, float] | None,
) -> dict:
    """Shrunken offense and defense values entering a game, good positive, in EPA per play."""
    league = table.league.get(season) or {"all": 0.0, "pass": 0.0, "run": 0.0}
    prior = prior or {}
    values = {}
    counts = {}
    for unit, sign, names in (
        ("offense", 1, ("O", "Op", "Or")),
        ("defense", -1, ("D", "Dp", "Dr")),
    ):
        sums, games = table.window(season, team, unit, before)
        counts[unit] = {"plays": int(sums["plays"]), "games": len(games)}
        for key, part, base in zip(
            names,
            (("epa", "plays"), ("drop_epa", "drop"), ("rush_epa", "rush")),
            ("all", "pass", "run"),
            strict=True,
        ):
            n = sums[part[1]]
            mean = sign * (sums[part[0]] / n - league[base]) if n else 0.0
            values[key] = shrunk(n, mean, k, r.get(key, 0.0) * prior.get(key, 0.0))
    return {"values": values, "counts": counts}
