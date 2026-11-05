"""Fit first-down and this-drive chance tables and team terms; score them on later seasons.

Rolling origin: fit 2024 to test 2025, then fit 2024-25 to test 2026. The second fit ships.
One job: the smoke checks run first and abort before anything is written.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np
import polars as pl
from strength import (
    SHAPE,
    FormTable,
    Heat,
    form_values,
    offensive_row,
    togo_bucket,
    unit_frame,
    zone,
)

SEASONS = [2024, 2025, 2026]
FOLDS = [{"fit": [2024], "test": 2025}, {"fit": [2024, 2025], "test": 2026}]
TERMS = ["intercept", "offense", "defense", "momentum", "opp_momentum"]
K_SMOOTH = 50
STRINGS = [
    "game_id",
    "game_date",
    "posteam",
    "defteam",
    "play_type",
    "series_result",
    "fixed_drive_result",
]
NUMBERS = [
    "season",
    "week",
    "qtr",
    "down",
    "ydstogo",
    "yardline_100",
    "half_seconds_remaining",
    "order_sequence",
    "play_deleted",
    "penalty",
    "epa",
    "success",
    "series_success",
    "first_down",
    "touchdown",
    "fixed_drive",
    "third_down_converted",
    "third_down_failed",
    "fourth_down_converted",
    "fourth_down_failed",
    "qb_dropback",
    "rush",
    "sack",
    "qb_hit",
    "cpoe",
    "xpass",
    "yards_gained",
    "wp",
    "score_differential",
    "touchback",
    "safety",
    "td_prob",
    "fg_prob",
]


def load(directory: Path) -> pl.DataFrame:
    frames = []
    for season in SEASONS:
        frame = pl.read_parquet(
            directory / f"play_by_play_{season}.parquet", columns=[*STRINGS, *NUMBERS]
        )
        frames.append(
            frame.with_columns(
                [pl.col(c).cast(pl.Float64, strict=False) for c in NUMBERS]
                + [pl.col(c).cast(pl.String) for c in STRINGS]
            ).with_columns(pl.col("season").cast(pl.Int64))
        )
    return pl.concat(frames).sort(["game_id", "order_sequence"])


def momentum_arrays(frame: pl.DataFrame) -> dict[str, np.ndarray]:
    """Own and opponent last-5 EPA before each row, the placebo after it, and the identity check."""
    rows = frame.select(
        "game_id", "posteam", "defteam", "down", "play_type", "penalty", "epa", "fixed_drive"
    ).to_dicts()
    n = len(rows)
    own, opp, allowed, lead = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    start = 0
    while start < n:
        end = start
        while end < n and rows[end]["game_id"] == rows[start]["game_id"]:
            end += 1
        heat, defended = Heat(), Heat()
        for i in range(start, end):
            row = rows[i]
            own[i] = heat.value(row["posteam"])
            opp[i] = heat.value(row["defteam"])
            allowed[i] = defended.value(row["defteam"])
            heat.add(row)
            if offensive_row(row):
                defended.add({**row, "posteam": row["defteam"]})
        # The placebo starts after this drive ends: later snaps of the same series or drive would
        # contain the outcome being predicted.
        future, pending, current = Heat(), [], None
        for i in range(end - 1, start - 1, -1):
            row = rows[i]
            if row["fixed_drive"] != current:
                for later in pending:
                    future.add(later)
                pending, current = [], row["fixed_drive"]
            lead[i] = future.value(row["posteam"])
            pending.append(row)
        start = end
    return {"own": own, "opp": opp, "allowed": allowed, "lead": lead}


def estimate_k_r(units: pl.DataFrame, frame: pl.DataFrame) -> tuple[float, dict, dict]:
    """Method-of-moments k from play and team-season variance; year-to-year r per metric."""
    rows = frame.filter(
        pl.col("play_type").is_in(["run", "pass"])
        & (pl.col("qtr") <= 4)
        & pl.col("epa").is_not_null()
    )
    team_season = rows.group_by(["season", "posteam"]).agg(
        pl.col("epa").mean().alias("m"), pl.col("epa").var().alias("v"), pl.len().alias("n")
    )
    within = float(team_season["v"].mean())
    observed = float(team_season["m"].var())
    n_mean = float(team_season["n"].mean())
    true_variance = max(observed - within / n_mean, 1e-9)
    measured = within / true_variance
    k = measured if 100 <= measured <= 400 else 200.0
    table = FormTable(units)
    seasons = {s: {t: table.season_values(s, t) for t in table.teams(s)} for s in SEASONS}
    r = {}
    for key in ("O", "D", "Op", "Or", "Dp", "Dr"):
        x, y = [], []
        for a, b in ((2024, 2025), (2025, 2026)):
            for team, value in seasons[a].items():
                other = seasons[b].get(team)
                if value and other:
                    x.append(value[key])
                    y.append(other[key])
        r[key] = float(np.corrcoef(x, y)[0, 1])
    return (
        k,
        r,
        {
            "k_measured": measured,
            "k_used": k,
            "within_variance": within,
            "between_true_variance": true_variance,
            "pairs": len(x),
        },
    )


def form_arrays(frame, units, k, r, season_priors) -> dict[str, np.ndarray]:
    table = FormTable(units)
    keys = frame.select("game_id", "game_date", "season", "posteam", "defteam").to_dicts()
    cache: dict[tuple, dict] = {}

    def value(season, team, before):
        key = (season, team, before)
        if key not in cache:
            prior = season_priors.get(season - 1, {}).get(team)
            cache[key] = form_values(table, season, team, before, k=k, r=r, prior=prior)["values"]
        return cache[key]

    out = {name: np.zeros(len(keys)) for name in ("O", "D", "Op", "Or", "Dp", "Dr")}
    for i, row in enumerate(keys):
        if not row["posteam"] or not row["defteam"]:
            continue
        offense = value(row["season"], row["posteam"], row["game_date"])
        defense = value(row["season"], row["defteam"], row["game_date"])
        out["O"][i], out["Op"][i], out["Or"][i] = offense["O"], offense["Op"], offense["Or"]
        out["D"][i], out["Dp"][i], out["Dr"][i] = defense["D"], defense["Dp"], defense["Dr"]
    return out


def dense(d, t, z, y, s=None, strata=1):
    """Smoothed table: an empty cell equals its parent, so lookups need no fallback."""
    s = np.zeros(len(d), dtype=int) if s is None else s
    n_cell = np.zeros((*SHAPE, strata))
    hit_cell = np.zeros((*SHAPE, strata))
    np.add.at(n_cell, (d, t, z, s), 1)
    np.add.at(hit_cell, (d, t, z, s), y)
    n_down, hit_down = n_cell.sum(axis=(1, 2, 3)), hit_cell.sum(axis=(1, 2, 3))
    overall = hit_cell.sum() / max(1, n_cell.sum())
    root = (hit_down + K_SMOOTH * overall) / (n_down + K_SMOOTH)
    n_parent, hit_parent = n_cell.sum(axis=(2, 3)), hit_cell.sum(axis=(2, 3))
    parent = (hit_parent + K_SMOOTH * root[:, None]) / (n_parent + K_SMOOTH)
    p = (hit_cell + K_SMOOTH * parent[:, :, None, None]) / (n_cell + K_SMOOTH)
    return p, n_cell


def logit(p):
    q = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(q / (1 - q))


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def fit(X, y, offset, weights=None, start=None, iterations=30):
    beta = np.zeros(X.shape[1]) if start is None else start.copy()
    w = np.ones(len(y)) if weights is None else weights
    for _ in range(iterations):
        p = np.clip(sigmoid(offset + X @ beta), 1e-6, 1 - 1e-6)
        hess = (X * (w * p * (1 - p))[:, None]).T @ X + 1e-9 * np.eye(X.shape[1])
        step = np.linalg.solve(hess, X.T @ (w * (y - p)))
        beta += step
        if np.abs(step).max() < 1e-7:
            break
    return beta


def bootstrap(X, y, offset, games, beta, draws, seed):
    labels, index = np.unique(games, return_inverse=True)
    rng = np.random.default_rng(seed)
    out = np.zeros((draws, len(beta)))
    for b in range(draws):
        counts = rng.multinomial(len(labels), [1 / len(labels)] * len(labels))
        out[b] = fit(X, y, offset, weights=counts[index].astype(float), start=beta, iterations=3)
    return np.percentile(out, [5, 95], axis=0).T


def logloss(y, p):
    q = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(q) + (1 - y) * np.log(1 - q)).mean())


def deciles(y, p):
    edges = np.quantile(p, np.linspace(0, 1, 11))
    index = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, 9)
    table = []
    for i in range(10):
        chosen = index == i
        if chosen.any():
            table.append(
                {
                    "n": int(chosen.sum()),
                    "predicted": float(p[chosen].mean()),
                    "observed": float(y[chosen].mean()),
                }
            )
    gaps = [abs(r["predicted"] - r["observed"]) for r in table]
    weights = [r["n"] for r in table]
    return {
        "ece": float(np.average(gaps, weights=weights)),
        "max_gap": float(max(gaps)),
        "deciles": table,
    }


def rank(values):
    order = np.argsort(np.argsort(values))
    return order.astype(float)


def rows_for(frame: pl.DataFrame, mask) -> dict:
    d = (frame["down"].to_numpy()[mask] - 1).astype(int)
    togo = frame["ydstogo"].to_numpy()[mask]
    yard = frame["yardline_100"].to_numpy()[mask]
    return {
        "mask": mask,
        "d": d,
        "t": np.array([togo_bucket(v) for v in togo]),
        "z": np.array([zone(v) for v in yard]),
    }


def design(arrays, mask, sds, h5_sd):
    return np.column_stack(
        [
            np.ones(mask.sum()),
            arrays["O"][mask] / sds["O"],
            arrays["D"][mask] / sds["D"],
            arrays["own"][mask] / h5_sd,
            arrays["opp"][mask] / h5_sd,
        ]
    )


def contribution_report(names, beta, ci):
    return {
        name: {"beta": float(b), "ci90": [float(c[0]), float(c[1])]}
        for name, b, c in zip(names, beta, ci, strict=True)
    }


def evaluate_target(frame, arrays, selection, y, *, season, fold, draws, strata=None, n_strata=1):
    """Fit a table and five terms on a fold's fit seasons and score on its test season."""
    fit_seasons, test_season = fold["fit"], fold["test"]
    mask = selection
    seasons = season[mask]
    r = rows_for(frame, mask)
    y_all = y[mask]
    s = np.zeros(mask.sum(), dtype=int) if strata is None else strata[mask]
    train = np.isin(seasons, fit_seasons)
    test = seasons == test_season
    p, n = dense(r["d"][train], r["t"][train], r["z"][train], y_all[train], s[train], n_strata)
    p0 = p[r["d"], r["t"], r["z"], s]
    fit_mask = np.isin(season, fit_seasons)
    # Standard deviations come from the fit seasons, per team-game row weighting.
    sds = {key: float(arrays[key][fit_mask & selection].std()) for key in arrays["sd_keys"]}
    h5_sd = float(arrays["own"][fit_mask & selection].std())
    X = design(arrays, mask, sds, h5_sd)
    offset = logit(p0)
    beta = fit(X[train], y_all[train], offset[train])
    games = frame["game_id"].to_numpy()[mask]
    ci = bootstrap(X[train], y_all[train], offset[train], games[train], beta, draws, 1234)
    pred = sigmoid(offset + X @ beta)
    no_mom = fit(X[train][:, :3], y_all[train], offset[train])
    pred_no_mom = sigmoid(offset + X[:, :3] @ no_mom)
    # Placebo: the offense's next five snaps, which cannot cause this one.
    placebo_X = np.column_stack([X[:, :3], arrays["lead"][mask] / h5_sd, X[:, 4]])
    placebo = fit(placebo_X[train], y_all[train], offset[train])
    placebo_ci = bootstrap(
        placebo_X[train],
        y_all[train],
        offset[train],
        games[train],
        placebo,
        max(100, draws // 2),
        5,
    )
    test_beta = fit(X[test], y_all[test], offset[test])
    test_ci = bootstrap(X[test], y_all[test], offset[test], games[test], test_beta, draws // 2, 7)
    return {
        "table": {"p": p, "n": n},
        "beta": beta,
        "ci": ci,
        "sds": sds,
        "h5_sd": h5_sd,
        "placebo_beta": float(placebo[3]),
        "placebo_ci": [float(v) for v in placebo_ci[3]],
        "test_beta": test_beta,
        "test_ci": test_ci,
        "n_fit": int(train.sum()),
        "n_test": int(test.sum()),
        "logloss": {
            "baseline": logloss(y_all[test], p0[test]),
            "model": logloss(y_all[test], pred[test]),
            "without_momentum": logloss(y_all[test], pred_no_mom[test]),
        },
        "y": y_all[test],
        "pred": pred[test],
        "p0": p0[test],
        "half": frame["half_seconds_remaining"].to_numpy()[mask][test],
        "diff": frame["score_differential"].to_numpy()[mask][test],
    }


def matchup_variant(frame, arrays, selection, y, season, fold):
    """V2: pass and run strength weighted by how often this cell passes."""
    mask = selection
    seasons = season[mask]
    r = rows_for(frame, mask)
    y_all = y[mask]
    train = np.isin(seasons, fold["fit"])
    test = seasons == fold["test"]
    p, _ = dense(r["d"][train], r["t"][train], r["z"][train], y_all[train])
    p0 = p[r["d"], r["t"], r["z"], np.zeros(mask.sum(), dtype=int)]
    xpass = np.nan_to_num(frame["xpass"].to_numpy()[mask], nan=0.55)
    pi_table, _ = dense(r["d"][train], r["t"][train], r["z"][train], xpass[train])
    pi = pi_table[r["d"], r["t"], r["z"], np.zeros(mask.sum(), dtype=int)]
    fit_mask = np.isin(season, fold["fit"]) & selection
    sd = {key: float(arrays[key][fit_mask].std()) for key in ("Op", "Or", "Dp", "Dr", "O", "D")}
    h5 = float(arrays["own"][fit_mask].std())
    own, opp = arrays["own"][mask] / h5, arrays["opp"][mask] / h5
    one = np.ones(mask.sum())
    V1 = np.column_stack([one, arrays["O"][mask] / sd["O"], arrays["D"][mask] / sd["D"], own, opp])
    V2 = np.column_stack(
        [
            one,
            pi * arrays["Op"][mask] / sd["Op"],
            (1 - pi) * arrays["Or"][mask] / sd["Or"],
            pi * arrays["Dp"][mask] / sd["Dp"],
            (1 - pi) * arrays["Dr"][mask] / sd["Dr"],
            own,
            opp,
        ]
    )
    offset = logit(p0)
    b1, b2 = (
        fit(V1[train], y_all[train], offset[train]),
        fit(V2[train], y_all[train], offset[train]),
    )
    return {
        "v1": logloss(y_all[test], sigmoid(offset + V1 @ b1)[test]),
        "v2": logloss(y_all[test], sigmoid(offset + V2 @ b2)[test]),
        "beta": b2.tolist(),
    }


def spots(frame: pl.DataFrame) -> dict:
    rows = frame.select(
        "game_id", "season", "play_type", "down", "yardline_100", "touchback", "safety"
    ).to_dicts()
    safety, touchback = {}, {}
    pending = None
    for row in rows:
        if pending and row["game_id"] != pending["game_id"]:
            pending = None
        if pending and row["down"] in (1, 2, 3, 4):
            target = safety if pending["kind"] == "safety" else touchback
            target.setdefault(int(row["season"]), []).append(row["yardline_100"])
            pending = None
        if row["safety"] == 1:
            pending = {"kind": "safety", "game_id": row["game_id"]}
        elif row["play_type"] == "kickoff" and row["touchback"] == 1:
            pending = {"kind": "touchback", "game_id": row["game_id"]}
    return {
        "safety_kick": {
            str(s): {"mean": round(float(np.mean(v)), 1), "n": len(v)} for s, v in safety.items()
        },
        "kickoff_touchback": {
            str(s): {"mode": float(Counter(v).most_common(1)[0][0]), "n": len(v)}
            for s, v in touchback.items()
        },
    }


def smoke(frame, arrays, series_mask, drive_mask):
    per = frame.group_by("season").len().sort("season").to_dicts()
    print("rows per season", per, flush=True)
    assert {r["season"] for r in per} == set(SEASONS), "a season is missing"
    assert series_mask.sum() > 50_000, f"too few series rows: {series_mask.sum()}"
    assert drive_mask.sum() > 50_000, f"too few drive rows: {drive_mask.sum()}"
    diff = float(np.abs(arrays["own"] - arrays["allowed"])[series_mask].max())
    print("momentum identity: max |H5_P - H5_allowed_Q| =", diff, flush=True)
    assert diff < 1e-9, "a defense's allowed heat should equal the offense's own heat"
    print("collinearity confirmed; the second term is the opponent offense's heat", flush=True)
    assert float(np.abs(arrays["O"]).max()) < 1.0, "offense form out of range"
    assert float(arrays["O"].std()) > 0, "offense form is constant"
    leak = float(np.corrcoef(arrays["own"][series_mask], arrays["lead"][series_mask])[0, 1])
    print("own vs lead heat correlation", round(leak, 3), flush=True)


def pp_per_sd(beta):
    return 100 * (float(sigmoid(np.array(beta))) - 0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--draws", type=int, default=500)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    frame = load(Path(a.pbp))
    print("loaded", frame.height, flush=True)
    units = unit_frame(frame)
    k, r, variances = estimate_k_r(units, frame)
    print("k and r", k, r, variances, flush=True)
    table = FormTable(units)
    priors = {
        s: {t: v for t in table.teams(s) if (v := table.season_values(s, t))} for s in SEASONS
    }
    arrays = {**form_arrays(frame, units, k, r, priors), **momentum_arrays(frame)}
    arrays["sd_keys"] = ["O", "D"]

    season = frame["season"].to_numpy()
    play_type = frame["play_type"].fill_null("").to_numpy()
    series_result = frame["series_result"].fill_null("").to_numpy()
    down = np.nan_to_num(frame["down"].to_numpy(), nan=0)
    qtr = frame["qtr"].to_numpy()
    deleted = np.nan_to_num(frame["play_deleted"].to_numpy(), nan=0)
    penalty = np.nan_to_num(frame["penalty"].to_numpy(), nan=0)
    half = frame["half_seconds_remaining"].to_numpy()
    togo = frame["ydstogo"].to_numpy()
    yard = frame["yardline_100"].to_numpy()
    valid = (
        (qtr <= 4)
        & np.isin(down, [1, 2, 3, 4])
        & (deleted != 1)
        & np.isfinite(togo)
        & np.isfinite(yard)
    )
    scrimmage = np.isin(play_type, ["run", "pass"]) | ((play_type == "no_play") & (penalty == 1))
    series_ok = ~np.isin(series_result, ["QB kneel", "End of half", ""])
    series_mask = valid & scrimmage & series_ok & np.isin(down, [1, 2, 3])
    go_mask = valid & (down == 4) & np.isin(play_type, ["run", "pass"])
    choice_mask = valid & (down == 4) & np.isin(play_type, ["run", "pass", "punt", "field_goal"])
    drive_mask = valid & (scrimmage | np.isin(play_type, ["punt", "field_goal"]))
    smoke(frame, arrays, series_mask, drive_mask)

    series_y = np.nan_to_num(frame["series_success"].to_numpy(), nan=0.0)
    converted = (
        (np.nan_to_num(frame["first_down"].to_numpy(), nan=0) == 1)
        | (np.nan_to_num(frame["touchdown"].to_numpy(), nan=0) == 1)
    ).astype(float)
    drive_result = frame["fixed_drive_result"].fill_null("").to_numpy()
    drive_td = (drive_result == "Touchdown").astype(float)
    drive_fg = (drive_result == "Field goal").astype(float)
    drive_y = drive_td + drive_fg
    late = (half <= 300).astype(int)

    fold_results = []
    for fold in FOLDS:
        common = {"season": season, "fold": fold}
        series = evaluate_target(frame, arrays, series_mask, series_y, draws=a.draws, **common)
        snap = evaluate_target(frame, arrays, series_mask, converted, draws=100, **common)
        go = evaluate_target(frame, arrays, go_mask, converted, draws=200, **common)
        drive = evaluate_target(
            frame,
            arrays,
            drive_mask,
            drive_y,
            draws=a.draws,
            strata=late,
            n_strata=2,
            **common,
        )
        matchup = matchup_variant(frame, arrays, series_mask, series_y, season, fold)
        fold_results.append(
            {
                "fold": fold,
                "series": series,
                "snap": snap,
                "go": go,
                "drive": drive,
                "matchup": matchup,
            }
        )
        print("fold", fold, "series", series["logloss"], "drive", drive["logloss"], flush=True)

    ship = fold_results[-1]

    def gates_for(name):
        rows = []
        for result in fold_results:
            model = result[name]
            ll = model["logloss"]
            rows.append(
                {
                    "test_season": result["fold"]["test"],
                    "n_fit": model["n_fit"],
                    "n_test": model["n_test"],
                    "baseline": ll["baseline"],
                    "model": ll["model"],
                    "without_momentum": ll["without_momentum"],
                    "lift": ll["model"] - ll["baseline"],
                    "momentum_lift": ll["model"] - ll["without_momentum"],
                }
            )
        y = np.concatenate([x[name]["y"] for x in fold_results])
        p = np.concatenate([x[name]["pred"] for x in fold_results])
        p0 = np.concatenate([x[name]["p0"] for x in fold_results])
        half_ = np.concatenate([x[name]["half"] for x in fold_results])
        diff_ = np.concatenate([x[name]["diff"] for x in fold_results])
        calibration = deciles(y, p)
        late_rows = half_ <= 120
        blow = np.abs(np.nan_to_num(diff_, nan=0)) > 16
        model = ship[name]
        return {
            "folds": rows,
            "calibration": calibration,
            "baseline_calibration": deciles(y, p0),
            "late_ece": deciles(y[late_rows], p[late_rows])["ece"]
            if late_rows.sum() > 100
            else None,
            "blowout_ece": deciles(y[blow], p[blow])["ece"] if blow.sum() > 100 else None,
            "G1_lift": all(row["lift"] <= -0.002 for row in rows),
            "G2_calibration": calibration["ece"] <= 0.015 and calibration["max_gap"] <= 0.03,
            "G3_signs": bool(
                model["beta"][1] > 0
                and model["beta"][2] < 0
                and model["ci"][1][0] > 0
                and model["ci"][2][1] < 0
            ),
            "G4_momentum_size": bool(abs(model["beta"][3]) <= 0.3 and abs(model["beta"][4]) <= 0.3),
            # What the page does: keep team terms that help on every later season, point the
            # right way and are no less calibrated than the table alone. G1's 0.002 is reported
            # beside it; it was set for a nine-season fit, and this fit has one or two seasons.
            "applied": bool(
                all(row["lift"] < 0 for row in rows)
                and model["beta"][1] > 0
                and model["beta"][2] < 0
                and model["ci"][1][0] > 0
                and model["ci"][2][1] < 0
                and calibration["ece"] <= deciles(y, p0)["ece"]
            ),
        }

    gates = {name: gates_for(name) for name in ("series", "snap", "go", "drive")}

    # Team-rank sanity: end-of-2025 form against 2025 series conversion by team.
    end_2025 = "2026-01-01"
    table_2025 = FormTable(units)
    rows_2025 = frame.filter(pl.Series(series_mask & (season == 2025)))
    by_team = rows_2025.group_by("posteam").agg(pl.col("series_success").mean().alias("rate"))
    pairs = []
    for row in by_team.to_dicts():
        if row["posteam"]:
            value = form_values(
                table_2025,
                2025,
                row["posteam"],
                end_2025,
                k=k,
                r=r,
                prior=priors[2024].get(row["posteam"]),
            )
            pairs.append((value["values"]["O"], row["rate"]))
    spearman = float(
        np.corrcoef(rank(np.array([p[0] for p in pairs])), rank(np.array([p[1] for p in pairs])))[
            0, 1
        ]
    )
    gates["team_rank_spearman"] = spearman
    gates["G3_team_rank"] = spearman >= 0.6

    # Does the garbage-time filter move team ranks?
    raw = FormTable(unit_frame(frame, garbage_filter=False))
    moved = 0
    teams = [t for t in table.teams(2025)]
    a_values = np.array([(table.season_values(2025, t) or {"O": 0})["O"] for t in teams])
    b_values = np.array([(raw.season_values(2025, t) or {"O": 0})["O"] for t in teams])
    moved = int((np.abs(rank(a_values) - rank(b_values)) >= 5).sum())

    # Momentum sentence inputs from the shipped series model.
    series = ship["series"]
    beta_m, ci_m = series["beta"][3], series["ci"][3]
    placebo = series["placebo_beta"]
    if abs(beta_m) >= 0.05 and (ci_m[0] > 0 or ci_m[1] < 0) and abs(placebo) <= abs(beta_m) / 2:
        verdict = "a measured streak effect"
    elif (ci_m[0] > 0 or ci_m[1] < 0) and abs(placebo) > abs(beta_m) / 2:
        # Later snaps predict just as well, so this is how the game is going, not a streak.
        verdict = "reads as game-level form, not a streak"
    else:
        verdict = "about zero"
    momentum = {
        "beta": float(beta_m),
        "ci90": [float(ci_m[0]), float(ci_m[1])],
        "pp_per_sd": pp_per_sd(beta_m),
        "pp_ci90": [pp_per_sd(ci_m[0]), pp_per_sd(ci_m[1])],
        "placebo_beta": placebo,
        "placebo_ci90": series["placebo_ci"],
        "test_beta": float(series["test_beta"][3]),
        "test_ci90": [float(v) for v in series["test_ci"][3]],
        "verdict": verdict,
    }

    # 4th-down go rate and this-drive TD and field-goal tables from the shipped fit seasons.
    fit_seasons = ship["fold"]["fit"]
    fit_rows = np.isin(season, fit_seasons)
    choice = rows_for(frame, choice_mask)
    went = np.isin(play_type[choice_mask], ["run", "pass"]).astype(float)
    choice_train = np.isin(season[choice_mask], fit_seasons)
    go_rate, go_n = dense(
        choice["d"][choice_train],
        choice["t"][choice_train],
        choice["z"][choice_train],
        went[choice_train],
    )
    drive_rows = rows_for(frame, drive_mask)
    drive_train = np.isin(season[drive_mask], fit_seasons)
    strata = late[drive_mask]
    td_table, _ = dense(
        drive_rows["d"][drive_train],
        drive_rows["t"][drive_train],
        drive_rows["z"][drive_train],
        drive_td[drive_mask][drive_train],
        strata[drive_train],
        2,
    )
    fg_table, _ = dense(
        drive_rows["d"][drive_train],
        drive_rows["t"][drive_train],
        drive_rows["z"][drive_train],
        drive_fg[drive_mask][drive_train],
        strata[drive_train],
        2,
    )

    # The next-score chance against this-drive touchdowns by zone, for the methodology.
    check = drive_mask & fit_rows & np.isin(down, [1, 2, 3])
    zones = np.array([zone(v) for v in yard[check]])
    td_prob = np.nan_to_num(frame["td_prob"].to_numpy()[check], nan=0.0)
    labels = ["1-10", "11-20", "21-40", "41-60", "61-80", "81-99"]
    a_vs_b = [
        {
            "yards_to_goal": labels[i],
            "n": int((zones == i).sum()),
            "next_score_td": float(td_prob[zones == i].mean()),
            "this_drive_td": float(drive_td[check][zones == i].mean()),
        }
        for i in range(6)
    ]

    def stored(p, n=None):
        result = {"p": [round(float(v), 5) for v in p.ravel()], "shape": list(p.shape)}
        if n is not None:
            result["n"] = [int(v) for v in n.ravel()]
        return result

    def model_report(name):
        model = ship[name]
        return {
            "names": TERMS,
            "beta": [float(v) for v in model["beta"]],
            "ci90": [[float(c[0]), float(c[1])] for c in model["ci"]],
            "sd": model["sds"],
            "h5_sd": model["h5_sd"],
            "table": stored(model["table"]["p"], model["table"]["n"]),
        }

    keep_v2 = all(x["matchup"]["v2"] <= x["matchup"]["v1"] - 0.001 for x in fold_results)
    payload = {
        "version": 1,
        "seasons": SEASONS,
        "fit_seasons": fit_seasons,
        "folds": [{"fit": x["fold"]["fit"], "test": x["fold"]["test"]} for x in fold_results],
        "k": k,
        "r": r,
        "variances": variances,
        "priors": {str(s): v for s, v in priors.items() if s < 2026},
        "models": {name: model_report(name) for name in ("series", "snap", "go", "drive")},
        "go_rate": stored(go_rate, go_n),
        "drive_td": stored(td_table),
        "drive_fg": stored(fg_table),
        "momentum": momentum,
        "matchup": {
            "kept": keep_v2,
            "folds": [{"test": x["fold"]["test"], **x["matchup"]} for x in fold_results],
        },
        "spots": spots(frame),
        "gates": gates,
        "garbage_filter_teams_moved_5_or_more": moved,
        "a_vs_b": a_vs_b,
    }
    (out / "conversion.json").write_text(json.dumps(payload, indent=None))
    summary = {
        key: value
        for key, value in payload.items()
        if key not in ("models", "go_rate", "drive_td", "drive_fg", "priors")
    }
    summary["coefficients"] = {
        name: {
            "beta": payload["models"][name]["beta"],
            "ci90": payload["models"][name]["ci90"],
            "sd": payload["models"][name]["sd"],
            "h5_sd": payload["models"][name]["h5_sd"],
        }
        for name in payload["models"]
    }
    (out / "gates.json").write_text(json.dumps(summary, indent=2))
    print(
        "GATES",
        json.dumps(
            {
                k2: {
                    g: v
                    for g, v in gates[k2].items()
                    if g.startswith("G") or g in ("folds", "applied")
                }
                for k2 in ("series", "snap", "go", "drive")
            },
            indent=1,
        ),
        flush=True,
    )
    print("spearman", spearman, "moved", moved, "momentum", momentum, flush=True)
    print("DONE", math.floor(frame.height), flush=True)


if __name__ == "__main__":
    main()
