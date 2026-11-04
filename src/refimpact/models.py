"""Use the MIT-licensed nflfastR models, without re-fitting or an R runtime.

Feature order/encoding follows nflfastR at the pinned commit in UPSTREAM.md.
fastrmodels v2 stores each booster as a named R raw vector (UBJSON bytes).
Only that precise, pinned serialization is supported; no pickle/code execution.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import math
import struct
from pathlib import Path

import httpx
import numpy as np
import xgboost as xgb

from refimpact.schema import Comparison, Estimate, Impact, State

MODEL_COMMIT = "75c7b68bc49535370236c38c9826265da075bd71"
# Filled from the pinned upstream files, never from an untrusted local manifest.
MODEL_SHA256 = {
    "ep": "fa45093d478fdd4534fa48860678ff99d597a67c340821db075c47fdab9102af",
    "wp": "a073049d2e5362d386ce479b5b64b6e2abd58795c723f574eb5e092be20e5ea1",
    "wp_spread": "5b89562edb2e6fd9dd12fd686a51f05d76222db9eb625995ed93a693ae5e44cb",
}
# The published object name in each .rda; the spread model is shown beside, never instead of, wp.
MODEL_OBJECT = {"ep": "ep_model", "wp": "wp_model", "wp_spread": "wp_model_spread"}
MODEL_ID = f"fastrmodels:{MODEL_COMMIT};features:nflfastR:701bbf38;wp:non-vegas"


def extract_raw(data: bytes, name: str) -> bytes:
    data = lzma.decompress(data)
    # RDX3 / XDR version 3, UTF-8, one named entry containing a raw vector.
    prefix = (
        b"RDX3\nX\n"
        + struct.pack(">IIII", 3, 0x40502, 0x30500, 5)
        + b"UTF-8"
        + struct.pack(">III", 0x402, 1, 0x40009)
        + struct.pack(">I", len(name))
        + name.encode()
        + struct.pack(">I", 24)
    )
    if not data.startswith(prefix):
        raise ValueError("Unexpected upstream model serialization")
    size = struct.unpack(">I", data[len(prefix) : len(prefix) + 4])[0]
    raw = data[len(prefix) + 4 : len(prefix) + 4 + size]
    if len(raw) != size or data[len(prefix) + 4 + size :] != struct.pack(">I", 254):
        raise ValueError("Invalid model length or R pairlist terminator")
    if not raw.startswith(b"{L"):
        raise ValueError("Expected an XGBoost UBJSON model")
    return raw


def fetch_models(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"model_id": MODEL_ID, "files": {}}
    for kind, checksum in MODEL_SHA256.items():
        url = (
            f"https://raw.githubusercontent.com/nflverse/fastrmodels/"
            f"{MODEL_COMMIT}/data/{MODEL_OBJECT[kind]}.rda"
        )
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError(f"Upstream {kind} model checksum mismatch")
        raw = extract_raw(data, MODEL_OBJECT[kind])
        target = directory / f"{kind}.ubj"
        temp = target.with_suffix(".tmp")
        temp.write_bytes(raw)
        temp.replace(target)
        manifest["files"][kind] = {
            "source": url,
            "rda_sha256": checksum,
            "ubj_sha256": hashlib.sha256(raw).hexdigest(),
        }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def features(s: State) -> tuple[list[float], list[float]]:
    home = int(s.possession == s.home_team)
    # NFL neutral-site EP uses home=0; WP follows the public WP calculator.
    ep_home = 0 if s.neutral_site else home
    roof = "retractable" if s.roof in ("open", "closed") else s.roof
    era = [
        s.season <= 2001,
        2001 < s.season <= 2005,
        2005 < s.season <= 2013,
        2013 < s.season <= 2017,
        s.season > 2017,
    ]
    offense_to = s.home_timeouts if home else s.away_timeouts
    defense_to = s.away_timeouts if home else s.home_timeouts
    ep = [
        s.half_seconds_remaining,
        s.yardline_100,
        ep_home,
        int(roof == "retractable"),
        int(roof == "dome"),
        int(roof == "outdoors"),
        s.ydstogo,
        *map(int, era),
        *[int(s.down == i) for i in range(1, 5)],
        offense_to,
        defense_to,
    ]
    diff = s.score_diff(s.possession)
    ratio = diff / math.exp(-4 * ((3600 - s.game_seconds_remaining) / 3600))
    wp = [
        int(s.qtr <= 2 and s.possession == s.second_half_receiver),
        home,
        s.half_seconds_remaining,
        s.game_seconds_remaining,
        ratio,
        diff,
        s.down,
        s.ydstogo,
        s.yardline_100,
        offense_to,
        defense_to,
    ]
    return ep, wp


def spread_features(s: State) -> list[float]:
    """Upstream wp_spread_model_select order; `home_spread` is nflverse spread_line."""
    _, wp = features(s)
    posteam_spread = s.home_spread if s.possession == s.home_team else -s.home_spread
    spread_time = posteam_spread * math.exp(-4 * ((3600 - s.game_seconds_remaining) / 3600))
    return [wp[0], spread_time, *wp[1:]]


class Scorer:
    def __init__(self, directory: Path):
        if not (directory / "manifest.json").is_file():
            raise ValueError("Models are missing; run uv run refimpact models-fetch")
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["model_id"] != MODEL_ID:
            raise ValueError("Model version mismatch; run models-fetch")
        self.models = {}
        self.cache = {}
        for kind in ("ep", "wp"):
            raw = (directory / f"{kind}.ubj").read_bytes()
            # Re-derive the approved raw checksum from a checked-in hash as well.
            if hashlib.sha256(raw).hexdigest() != RAW_SHA256[kind]:
                raise ValueError(f"Cached {kind} model checksum mismatch; run models-fetch")
            model = xgb.Booster(params={"nthread": 1})
            model.load_model(bytearray(raw))
            if model.num_features() != {"ep": 18, "wp": 11}[kind]:
                raise ValueError("Unexpected model feature count")
            self.models[kind] = model
        # Optional: older model directories still score; Vegas figures are then simply absent.
        spread = directory / "wp_spread.ubj"
        if spread.is_file():
            raw = spread.read_bytes()
            if hashlib.sha256(raw).hexdigest() != RAW_SHA256["wp_spread"]:
                raise ValueError("Cached wp_spread model checksum mismatch; run models-fetch")
            model = xgb.Booster(params={"nthread": 1})
            model.load_model(bytearray(raw))
            if model.num_features() != 12:
                raise ValueError("Unexpected model feature count")
            self.models["wp_spread"] = model

    def predict(self, states: list[State], team: str) -> list[Estimate]:
        keys = [(s.model_dump_json(), team) for s in states]
        missing = [
            (key, state) for key, state in zip(keys, states, strict=True) if key not in self.cache
        ]
        if not missing:
            return [self.cache[key].model_copy() for key in keys]
        vectors = [features(s) for _, s in missing]
        ep = self.models["ep"].predict(xgb.DMatrix(np.array([v[0] for v in vectors]), nthread=1))
        wp = self.models["wp"].predict(xgb.DMatrix(np.array([v[1] for v in vectors]), nthread=1))
        values = ep @ np.array([7, -7, 3, -3, 2, -2, 0])
        for (key, s), e, w in zip(missing, values, wp, strict=True):
            self.cache[key] = Estimate(
                ep=float(e) * (1 if s.possession == team else -1),
                wp=float(w) if s.possession == team else 1 - float(w),
            )
        result = [self.cache[key].model_copy() for key in keys]
        if len(self.cache) > 20000:
            self.cache.clear()
        return result

    def vegas_wp(self, states: list[State], team: str) -> list[float | None]:
        """Spread-model win chance; None without the booster or a spread for that game."""
        result: list[float | None] = [None] * len(states)
        model = self.models.get("wp_spread")
        known = [i for i, s in enumerate(states) if s.home_spread is not None]
        if model is None or not known:
            return result
        rows = [spread_features(states[i]) for i in known]
        values = model.predict(xgb.DMatrix(np.array(rows), nthread=1))
        for i, w in zip(known, values, strict=True):
            result[i] = float(w) if states[i].possession == team else 1 - float(w)
        return result

    def next_score(self, states: list[State], team: str) -> list[dict[str, float]]:
        """The EP model's next-score class probabilities, in `team`'s frame."""
        vectors = np.array([features(s)[0] for s in states])
        rows = self.models["ep"].predict(xgb.DMatrix(vectors, nthread=1))
        result = []
        for s, row in zip(states, rows, strict=True):
            own = s.possession == team
            td, opp_td, fg, opp_fg, safety, opp_safety, none = (float(v) for v in row)
            result.append(
                {
                    "td": td if own else opp_td,
                    "fg": fg if own else opp_fg,
                    "safety": safety if own else opp_safety,
                    "opp_td": opp_td if own else td,
                    "opp_fg": opp_fg if own else fg,
                    "opp_safety": opp_safety if own else safety,
                    "no_score": none,
                }
            )
        return result

    def compare(self, c: Comparison) -> Impact:
        before, actual, alternative = self.predict(
            [c.before, c.actual, c.alternative], c.reference_team
        )

        def epa(s: State, estimate: Estimate) -> float:
            return (
                s.score_diff(c.reference_team)
                - c.before.score_diff(c.reference_team)
                + estimate.ep
                - before.ep
            )

        actual_epa = epa(c.actual, actual)
        alternative_epa = epa(c.alternative, alternative)
        return Impact(
            comparison=c,
            model_id=MODEL_ID,
            before=before,
            actual=actual,
            alternative=alternative,
            actual_epa=actual_epa,
            alternative_epa=alternative_epa,
            epa_impact=actual_epa - alternative_epa,
            wp_swing_pp=100 * (actual.wp - alternative.wp),
        )


RAW_SHA256 = {
    "ep": "14a4b7e0c115d3f46fc9f341a024874b0dbcf44b30bb1f426d4f59b6f175335a",
    "wp": "43275017bbec6335bdb2b50c7ca70673c35ba158ce5db4537d1ddc4141b83e1f",
    "wp_spread": "c46beee503995cbaf668fd7b1de1eb61d5a4e54e9e2225302a5b9198746d560f",
}
