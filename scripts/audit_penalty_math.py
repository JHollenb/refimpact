"""Reconcile the saved penalty ledger and independently verify native-state inference.

Run from the repository: .venv/bin/python scripts/audit_penalty_math.py
This validates arithmetic/inference, not whistle timing or causal foul-free outcomes.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from refimpact.models import Scorer
from refimpact.penalties import finite
from refimpact.schema import State
from refimpact.storage import Database


def audit():
    database = Database()
    data = database.read(all_weeks=True, sections=("penalties",))
    rows = {
        (r["game_id"], str(int(r["play_id"]))): r
        for r in database.source_frame(
            "pbp", data["default_season"], database.run(data["run_id"])["pbp_source_id"]
        ).to_dicts()
    }
    scorer = Scorer(Path(".cache/models"))
    checks, errors, ep_errors, wp_errors, examples = 0, [], [], [], []

    def equal(a, b, label, tolerance=1e-8):
        nonlocal checks
        checks += 1
        if a is None or b is None:
            if a is not b:
                errors.append(label)
        elif abs(a - b) > tolerance:
            errors.append(f"{label}: {a} != {b}")

    for e in data["penalties"]:
        d = e.get("decision")
        if not d:
            continue
        before = State.model_validate(e["before"])
        team = d["reference_team"]
        pre = scorer.predict([before], team)[0]
        source = rows[e["game_id"], e["play_id"]]
        if finite(source.get("ep")):
            ep_errors.append(abs(pre.ep - source["ep"]))
        if finite(source.get("wp")):
            wp_errors.append(abs(pre.wp - source["wp"]))
        actual = next(b for b in d["branches"] if b["id"] == d["actual_branch"])
        for b in d["branches"]:
            points = State.model_validate(b["base_state"]).score_diff(team) - before.score_diff(
                team
            )
            equal(b["epa"], points + b["ep"] - pre.ep, f"{e['id']} {b['id']} EPA")
            equal(
                b["wpa_pp"],
                100 * (b["wp"] - pre.wp) if b["wp"] is not None else None,
                f"{e['id']} {b['id']} WPA",
            )
            equal(
                b["actual_minus_branch_epa"], actual["epa"] - b["epa"], f"{e['id']} EPA subtraction"
            )
            equal(
                b["actual_minus_branch_wp_pp"],
                actual["wpa_pp"] - b["wpa_pp"]
                if actual["wpa_pp"] is not None and b["wpa_pp"] is not None
                else None,
                f"{e['id']} WPA subtraction",
            )
            if b["state"]:
                s = State.model_validate(b["state"])
                other = before.away_team if team == before.home_team else before.home_team
                opposite = scorer.predict([s], other)[0]
                equal(b["ep"], -opposite.ep, f"{e['id']} EP possession sign")
                equal(b["wp"], 1 - opposite.wp, f"{e['id']} WP possession complement")
        nf = next((b for b in d["branches"] if b["id"] == "no_flag"), None)
        if nf:
            equal(e["enforcement_epa"], nf["actual_minus_branch_epa"], f"{e['id']} event EPA")
            equal(
                e["enforcement_wp_swing_pp"],
                nf["actual_minus_branch_wp_pp"],
                f"{e['id']} event WPA",
            )
        for c in d["timing_comparisons"]:
            equal(
                c["actual_minus_branch_wp_pp"],
                c["actual_wpa_pp"] - c["alternative_wpa_pp"]
                if c["actual_wpa_pp"] is not None and c["alternative_wpa_pp"] is not None
                else None,
                f"{e['id']} matched clock WPA",
            )
        if e["game_id"] == "2026_08_CHI_SEA" and e["play_id"] in {"3158", "3410", "2785"}:
            examples.append(
                {
                    "play_id": e["play_id"],
                    "description": e["description"],
                    "reference_team": team,
                    "penalty_epa": e["enforcement_epa"],
                    "penalty_wp_pp": e["enforcement_wp_swing_pp"],
                    "before_wp": pre.wp,
                    "branches": d["branches"],
                }
            )
    for g in data["games"]:
        events = [e for e in data["penalties"] if e["game_id"] == g["game_id"]]
        known = [e["home_epa_impact"] for e in events if e["home_epa_impact"] is not None]
        if known:
            equal(g["net_home_epa"], sum(known), f"{g['game_id']} ledger sum")
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "snapshot_generated_at": data["generated_at"],
        "arithmetic_checks": checks,
        "errors": errors,
        "coverage": data["coverage"],
        "native_pre_state_parity": {
            "ep_states": len(ep_errors),
            "wp_states": len(wp_errors),
            "max_ep_error": max(ep_errors, default=0),
            "max_wp_error": max(wp_errors, default=0),
        },
        "examples": examples,
        "scope": "Arithmetic, fixed-team possession signs, matched timing "
        "WPA and ledger reconciliation; "
        "native inference parity is reported separately. This does not establish a causal "
        "foul-free outcome, whistle timing, or accuracy/calibration of the upstream model.",
    }
    target = Path("reports") / (
        "penalty-math-audit-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    target.write_text(json.dumps(report, indent=2) + "\n")
    database.archive_file(target)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"examples", "scope"}}
            | {"report": str(target)},
            indent=2,
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    audit()
