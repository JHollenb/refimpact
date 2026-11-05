"""First-down and this-drive chances for a game state, from the fit in data/conversion.json.

The tables and coefficients come from scripts/fit_conversion, fit on 2024-25 and scored on 2025 and
2026. They are a separate fit from the pinned nflfastR models, which still supply every win chance
and expected-points value. Whole-season team strength never decides a call: these describe a team
with the ball in a state someone chose.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import polars as pl

from refimpact.models import Scorer
from refimpact.schema import State
from refimpact.strength import (
    METRICS,
    FormTable,
    Heat,
    form_values,
    ratio,
    togo_bucket,
    unit_frame,
    zone,
)

DATA = Path(__file__).parent / "data" / "conversion.json"
LATE_SECONDS = 300
THIN_CELL = 200
BLOWOUT = 16
OFFENSE_WORDS = {
    "epa_play": "EPA per play",
    "success": "success rate",
    "early": "early-down success",
    "conv": "third- and fourth-down conversions",
    "explosive": "explosive plays",
    "pass_epa": "passing EPA",
    "rush_epa": "rushing EPA",
    "cpoe": "completions over expected",
    "sack": "avoiding sacks",
    "hit": "avoiding QB hits",
}
DEFENSE_WORDS = {
    "epa_play": "limiting EPA per play",
    "success": "limiting success rate",
    "early": "early-down defense",
    "conv": "third- and fourth-down defense",
    "explosive": "limiting explosive plays",
    "pass_epa": "pass defense",
    "rush_epa": "run defense",
    "cpoe": "limiting completions over expected",
    "sack": "sacks",
    "hit": "QB hits",
}
NOT_APPLIED = (
    "Team and momentum terms didn't improve on the situation alone on later seasons, so this is "
    "the league rate from here."
)
FORM_COLUMNS = [
    "season",
    "game_id",
    "game_date",
    "posteam",
    "defteam",
    "qtr",
    "down",
    "ydstogo",
    "yardline_100",
    "half_seconds_remaining",
    "play_type",
    "play_deleted",
    "fixed_drive",
    "fixed_drive_result",
    "order_sequence",
    "epa",
    "wp",
    "success",
    "yards_gained",
    "third_down_converted",
    "third_down_failed",
    "fourth_down_converted",
    "fourth_down_failed",
    "qb_dropback",
    "rush",
    "cpoe",
    "sack",
    "qb_hit",
]


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-value))


def logit(p: float) -> float:
    q = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(q / (1 - q))


class Table:
    def __init__(self, stored: dict):
        self.p, self.n, self.shape = stored["p"], stored.get("n"), stored["shape"]

    def index(self, down: int, togo: float, yard: float, stratum: int) -> int:
        d, t, z, strata = self.shape
        return ((down - 1) * t + togo_bucket(togo)) * z * strata + zone(yard) * strata + stratum

    def read(self, down: int, togo: float, yard: float, stratum: int = 0) -> tuple[float, int]:
        i = self.index(down, togo, yard, stratum)
        return self.p[i], self.n[i] if self.n else 0


class SeasonForm:
    """A season's team windows and this-drive residuals, kept while the source version stands."""

    def __init__(self, frame: pl.DataFrame, fit: Conversion):
        self.frame = frame
        self.table = FormTable(unit_frame(frame))
        self.drive_table = fit.tables["drive"]
        self.ranks: dict[tuple[str, str], dict] = {}
        self.drives = self.drive_residuals(frame)

    def drive_residuals(self, frame: pl.DataFrame) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = defaultdict(list)
        rows = (
            frame.filter(
                pl.col("down").is_in([1, 2, 3, 4])
                & (pl.col("qtr") <= 4)
                & pl.col("yardline_100").is_not_null()
                & pl.col("ydstogo").is_not_null()
                & pl.col("posteam").is_not_null()
            )
            .sort(["game_id", "order_sequence"])
            .unique(subset=["game_id", "fixed_drive"], keep="first", maintain_order=True)
            .to_dicts()
        )
        for row in rows:
            stratum = int(row["half_seconds_remaining"] <= LATE_SECONDS)
            expected, _ = self.drive_table.read(
                int(row["down"]), row["ydstogo"], row["yardline_100"], stratum
            )
            scored = row["fixed_drive_result"] in ("Touchdown", "Field goal")
            result[row["posteam"]].append(
                {
                    "game_id": row["game_id"],
                    "game_date": row["game_date"],
                    "residual": float(scored) - expected,
                }
            )
        return result

    def rank_panel(self, season: int, before: str) -> dict:
        """Rank every team over its own last four games before a date, for each metric."""
        key = (str(season), before)
        if key in self.ranks:
            return self.ranks[key]
        panel: dict = {"offense": {}, "defense": {}}
        teams = self.table.teams(season)
        for unit in panel:
            sums = {t: self.table.window(season, t, unit, before)[0] for t in teams}
            for name, _, top, bottom, minimum, higher_better in METRICS:
                values = {t: ratio(s, top, bottom) for t, s in sums.items() if s[bottom] >= minimum}
                # A defense wants the opposite of an offense on every metric.
                best_high = higher_better if unit == "offense" else not higher_better
                ordered = sorted(values, key=lambda t: values[t], reverse=best_high)
                for position, team in enumerate(ordered, 1):
                    panel[unit].setdefault(team, []).append(
                        {"metric": name, "rank": position, "of": len(ordered)}
                    )
        self.ranks[key] = panel
        return panel

    def words(self, season: int, team: str, unit: str, before: str) -> dict:
        rows = self.rank_panel(season, before)[unit].get(team, [])
        names = OFFENSE_WORDS if unit == "offense" else DEFENSE_WORDS
        strong = sorted((r for r in rows if r["rank"] <= 5), key=lambda r: r["rank"])[:2]
        weak = sorted(
            (r for r in rows if r["rank"] > r["of"] - 5), key=lambda r: r["rank"], reverse=True
        )[:2]
        sums, games = self.table.window(season, team, unit, before)
        return {
            "strengths": [f"top 5 in {names[r['metric']]} (#{r['rank']})" for r in strong],
            "weaknesses": [f"bottom 5 in {names[r['metric']]} (#{r['rank']})" for r in weak],
            "games": len(games),
            "plays": int(sums["plays"]),
        }

    def drive_form(self, team: str, before: str) -> dict | None:
        dates = sorted(
            {d["game_date"] for d in self.drives.get(team, []) if d["game_date"] < before}
        )
        window = set(dates[-4:])
        rows = [d for d in self.drives.get(team, []) if d["game_date"] in window]
        if not rows:
            return None
        return {
            "pp": 100 * sum(d["residual"] for d in rows) / len(rows),
            "drives": len(rows),
            "games": len(window),
        }


class Conversion:
    def __init__(self, path: Path = DATA):
        self.data = json.loads(path.read_text())
        self.models = self.data["models"]
        self.tables = {name: Table(self.models[name]["table"]) for name in self.models}
        self.go_rate = Table(self.data["go_rate"])
        self.drive_td = Table(self.data["drive_td"])
        self.drive_fg = Table(self.data["drive_fg"])
        self.gates = self.data["gates"]
        self.momentum = self.data["momentum"]

    def applied(self, name: str) -> bool:
        return bool(self.gates[name]["applied"])

    def spot(self, kind: str, season: int) -> tuple[float, bool]:
        """A measured spot for a season, and whether it is an approximation."""
        spots = self.data["spots"].get(kind, {})
        stored = spots.get(str(season))
        key = "mean" if kind == "safety_kick" else "mode"
        if stored and stored["n"] >= 10:
            return float(round(stored[key])), False
        # Thin season: fall back on the latest season with enough of these, or a labeled constant.
        for name in sorted(spots, reverse=True):
            if spots[name]["n"] >= 10:
                return float(round(spots[name][key])), True
        return 57.0 if kind == "safety_kick" else 65.0, True

    def form(self, season_form: SeasonForm, season: int, team: str, before: str) -> dict:
        prior = self.data["priors"].get(str(season - 1), {}).get(team)
        return form_values(
            season_form.table, season, team, before, k=self.data["k"], r=self.data["r"], prior=prior
        )

    def terms(self, name: str, offense: dict, defense: dict, own: float, opp: float) -> dict:
        model = self.models[name]
        beta = dict(zip(model["names"], model["beta"], strict=True))
        sd = model["sd"]
        return {
            "intercept": beta["intercept"],
            "offense": beta["offense"] * offense["values"]["O"] / sd["O"],
            "defense": beta["defense"] * defense["values"]["D"] / sd["D"],
            "own": beta["momentum"] * own / model["h5_sd"],
            "opp": beta["opp_momentum"] * opp / model["h5_sd"],
            "z_offense": offense["values"]["O"] / sd["O"],
            "z_defense": defense["values"]["D"] / sd["D"],
            "z_own": own / model["h5_sd"],
            "z_opp": opp / model["h5_sd"],
        }

    def adjusted(self, name: str, p0: float, terms: dict) -> tuple[float, dict[str, float]]:
        """The chance after each term in turn, so the listed changes add up to the total."""
        value = logit(p0) + terms["intercept"]
        start = sigmoid(value)
        steps = {}
        for key, group in (
            ("offense", ("offense",)),
            ("defense", ("defense",)),
            ("momentum", ("own", "opp")),
        ):
            before = sigmoid(value)
            value += sum(terms[g] for g in group)
            steps[key] = 100 * (sigmoid(value) - before)
        steps["league"] = 100 * start
        return sigmoid(value), steps

    def momentum_line(self, team: str, own: float, z: float, effect: float) -> str:
        m = self.momentum
        return (
            f"Momentum: {team} last 5 snaps {own:+.1f} EPA ({z:+.1f} SD) → {effect:+.1f} pp. "
            f"Measured size {m['pp_per_sd']:.1f} pp per SD (90% CI {m['pp_ci90'][0]:.1f} to "
            f"{m['pp_ci90'][1]:.1f}); {m['verdict']}."
        )

    def first_down(
        self,
        state: State,
        season_form: SeasonForm,
        before: str,
        heat: Heat,
    ) -> dict:
        team = state.possession
        rival = state.away_team if team == state.home_team else state.home_team
        season = state.season
        offense = self.form(season_form, season, team, before)
        defense = self.form(season_form, season, rival, before)
        own, opp = heat.value(team), heat.value(rival)
        fourth = state.down == 4
        name = "go" if fourth else "series"
        p0, n = self.tables[name].read(state.down, state.ydstogo, state.yardline_100)
        applied = self.applied(name)
        terms = self.terms(name, offense, defense, own, opp)
        chance, steps = self.adjusted(name, p0, terms)
        result = {
            "team": team,
            "rival": rival,
            "kind": name,
            "league": p0,
            "n": n,
            "chance": chance if applied else p0,
            "applied": applied,
            "range": None,
            "momentum_line": None,
            "factors": [],
            "note": None if applied else NOT_APPLIED,
            "snap": None,
            "go_rate": None,
        }
        if n < THIN_CELL:
            half = 1.64 * math.sqrt(max(p0 * (1 - p0), 1e-6) / max(n, 1))
            result["range"] = [max(0.0, p0 - half), min(1.0, p0 + half)]
        offense_words = season_form.words(season, team, "offense", before)
        defense_words = season_form.words(season, rival, "defense", before)
        if applied:
            result["factors"] = [
                {
                    "key": "offense",
                    "label": f"{team} offense, last 4 games",
                    "pp": steps["offense"],
                    "sd": terms["z_offense"],
                    "words": offense_words,
                },
                {
                    "key": "defense",
                    "label": f"{rival} defense, last 4 games",
                    "pp": steps["defense"],
                    "sd": terms["z_defense"],
                    "words": defense_words,
                },
                {
                    "key": "momentum",
                    "label": "Momentum, this game",
                    "pp": steps["momentum"],
                    "sd": terms["z_own"],
                    "words": None,
                },
            ]
            result["league_adjusted"] = steps["league"] / 100
            result["momentum_line"] = (
                self.momentum_line(
                    team, own, terms["z_own"], 100 * (sigmoid(logit(p0) + terms["own"]) - p0)
                )
                + f" {rival}'s last 5 snaps: {opp:+.1f} EPA → "
                f"{100 * (sigmoid(logit(p0) + terms['opp']) - p0):+.1f} pp."
            )
        else:
            result["words"] = {"offense": offense_words, "defense": defense_words}
        if fourth:
            result["go_rate"] = self.go_rate.read(4, state.ydstogo, state.yardline_100)[0]
        else:
            snap0, _ = self.tables["snap"].read(state.down, state.ydstogo, state.yardline_100)
            snap_terms = self.terms("snap", offense, defense, own, opp)
            result["snap"] = (
                self.adjusted("snap", snap0, snap_terms)[0] if self.applied("snap") else snap0
            )
        return result

    def scoring(
        self,
        state: State,
        scorer: Scorer,
        season_form: SeasonForm,
        before: str,
        heat: Heat,
    ) -> dict:
        team = state.possession
        rival = state.away_team if team == state.home_team else state.home_team
        classes = scorer.next_score([state], team)[0]
        stratum = int(state.half_seconds_remaining <= LATE_SECONDS)
        p0, n = self.tables["drive"].read(state.down, state.ydstogo, state.yardline_100, stratum)
        td, _ = self.drive_td.read(state.down, state.ydstogo, state.yardline_100, stratum)
        fg, _ = self.drive_fg.read(state.down, state.ydstogo, state.yardline_100, stratum)
        applied = self.applied("drive")
        terms = self.terms(
            "drive",
            self.form(season_form, state.season, team, before),
            self.form(season_form, state.season, rival, before),
            heat.value(team),
            heat.value(rival),
        )
        matchup = self.adjusted("drive", p0, terms)[0]
        late = " Late in a half the clock can end a drive before it scores." if stratum else ""
        if abs(state.score_diff(team)) > BLOWOUT:
            late += " Less reliable in a blowout."
        ep = (
            7 * classes["td"]
            - 7 * classes["opp_td"]
            + 3 * classes["fg"]
            - 3 * classes["opp_fg"]
            + 2 * classes["safety"]
            - 2 * classes["opp_safety"]
        )
        form = season_form.drive_form(team, before)
        measures = [
            {
                "key": "next_td",
                "label": f"Next score is a {team} touchdown",
                "value": classes["td"],
                "note": "nflfastR model. Who scores next in this half, over any number of drives.",
            },
            {
                "key": "next_fg",
                "label": f"Next score is a {team} field goal",
                "value": classes["fg"],
                "note": f"nflfastR model. Nobody scores before the half ends: "
                f"{round(100 * classes['no_score'])}%.",
            },
            {
                "key": "drive_league",
                "label": "This drive scores, league history",
                "value": p0,
                "note": f"Touchdown {round(100 * td)}%, field goal {round(100 * fg)}%. "
                f"{n:,} plays from this down, distance and zone in 2024-25.{late}",
            },
            {
                "key": "drive_matchup",
                "label": f"This drive scores, {team} against {rival}",
                "value": matchup if applied else None,
                "note": "League history adjusted for both teams' last 4 games and this game so "
                f"far.{late}"
                if applied
                else "Team terms didn't improve on league history on later seasons, so none is "
                "shown.",
            },
            {
                "key": "ep",
                "label": "Expected points",
                "points": ep,
                "note": "nflfastR model. Value of the next score, net of the opponent's.",
            },
            {
                "key": "own_form",
                "label": f"{team} finishing drives, last 4 games",
                "pp": form["pp"] if form else None,
                "note": f"Scoring rate against the league's from the same starting spots, over "
                f"{form['drives']} drives."
                if form
                else "No earlier games this season.",
            },
        ]
        return {"team": team, "measures": measures}


def form_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [{key: row.get(key) for key in FORM_COLUMNS} for row in rows], infer_schema_length=None
    )
