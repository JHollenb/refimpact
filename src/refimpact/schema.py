from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class State(Record):
    """A regulation scrimmage state immediately before a down (not kickoff/PAT)."""

    season: int = Field(ge=2000, le=2100)
    home_team: str = Field(pattern=r"^[A-Z]{2,3}$")
    away_team: str = Field(pattern=r"^[A-Z]{2,3}$")
    possession: str = Field(pattern=r"^[A-Z]{2,3}$")
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    qtr: int = Field(ge=1, le=4)
    game_seconds_remaining: float = Field(gt=0, le=3600)
    half_seconds_remaining: float = Field(gt=0, le=1800)
    down: int = Field(ge=1, le=4)
    ydstogo: float = Field(gt=0, le=100)
    yardline_100: float = Field(gt=0, lt=100)
    home_timeouts: int = Field(ge=0, le=3)
    away_timeouts: int = Field(ge=0, le=3)
    second_half_receiver: str | None = None
    roof: Literal["outdoors", "dome", "open", "closed", "retractable"]
    neutral_site: bool = False
    # nflverse spread_line (positive favors home); read only by the Vegas-model comparison.
    home_spread: float | None = None

    @model_validator(mode="after")
    def consistent(self):
        teams = {self.home_team, self.away_team}
        if len(teams) != 2 or self.possession not in teams:
            raise ValueError("possession must be one of two different teams")
        if self.second_half_receiver is not None and self.second_half_receiver not in teams:
            raise ValueError("second_half_receiver must be a game team")
        if self.qtr <= 2 and self.second_half_receiver is None:
            raise ValueError("first-half WP requires second_half_receiver")
        expected = self.half_seconds_remaining + (1800 if self.qtr <= 2 else 0)
        if abs(self.game_seconds_remaining - expected) > 0.01:
            raise ValueError("game and half clocks disagree")
        if self.qtr in (1, 3) and self.half_seconds_remaining < 900:
            raise ValueError("quarter and half clock disagree")
        if self.qtr in (2, 4) and self.half_seconds_remaining > 900:
            raise ValueError("quarter and half clock disagree")
        if self.ydstogo > self.yardline_100 + 0.01:
            raise ValueError("distance to gain exceeds distance to goal")
        return self

    def score_diff(self, team: str) -> int:
        return (self.home_score - self.away_score) * (1 if team == self.home_team else -1)


class Comparison(Record):
    game_id: str = Field(pattern=r"^\d{4}_\d{2}_[A-Z]{2,3}_[A-Z]{2,3}$")
    play_id: str = Field(pattern=r"^[\w.-]{1,64}$")
    ruling: str = Field(min_length=1, max_length=60)
    reference_team: str
    before: State
    actual: State
    alternative: State
    alternative_label: str = Field(min_length=1, max_length=80)
    assumptions: list[str] = Field(min_length=1)
    method: Literal["dead_ball_enforcement", "manual"] = "manual"
    source_url: HttpUrl | None = None

    @model_validator(mode="after")
    def same_game(self):
        b = self.before
        if self.reference_team not in {b.home_team, b.away_team}:
            raise ValueError("reference_team must be a game team")
        if self.game_id.split("_") != [
            str(b.season),
            self.game_id.split("_")[1],
            b.away_team,
            b.home_team,
        ]:
            raise ValueError("game_id must match state season and teams")
        for s in (self.actual, self.alternative):
            for name in (
                "season",
                "home_team",
                "away_team",
                "roof",
                "neutral_site",
                "second_half_receiver",
            ):
                if getattr(s, name) != getattr(b, name):
                    raise ValueError(f"branches disagree on {name}")
            if (s.qtr <= 2) != (b.qtr <= 2):
                raise ValueError("cross-half branches need a different EP horizon")
            if s.game_seconds_remaining > b.game_seconds_remaining:
                raise ValueError("branches cannot precede the common starting state")
        return self


class Estimate(Record):
    ep: float
    wp: float = Field(ge=0, le=1)


class Impact(Record):
    comparison: Comparison
    model_id: str
    before: Estimate
    actual: Estimate
    alternative: Estimate
    actual_epa: float
    alternative_epa: float
    epa_impact: float
    wp_swing_pp: float
