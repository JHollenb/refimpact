from refimpact.schema import Impact


def render_tweet(impact: Impact) -> str:
    c = impact.comparison
    seconds = int(c.before.half_seconds_remaining) - (900 if c.before.qtr in (1, 3) else 0)
    clock = f"{seconds // 60}:{seconds % 60:02d}"
    text = (
        f"{c.before.away_team} @ {c.before.home_team} | Q{c.before.qtr} {clock}\n"
        f"{c.ruling}\n"
        f"{c.reference_team} win chance: {impact.alternative.wp:.1%} -> {impact.actual.wp:.1%} "
        f"({impact.wp_swing_pp:+.1f} pp)\n"
        f"EPA impact: {impact.epa_impact:+.2f} points\n"
        f"vs {c.alternative_label}\n"
        "Model estimate; ruling correctness not assessed."
    )
    # Use plain ASCII and no URLs so 280 code points is also X's weighted length.
    if not text.isascii() or len(text) > 280:
        raise ValueError("Tweet exceeds 280 ASCII characters; shorten ruling/alternative_label")
    return text
