# Upstream provenance

Models: `nflverse/fastrmodels` commit
`75c7b68bc49535370236c38c9826265da075bd71`, MIT license.

- https://github.com/nflverse/fastrmodels/tree/75c7b68bc49535370236c38c9826265da075bd71
- `data/ep_model.rda`: SHA256 `fa45093d478fdd4534fa48860678ff99d597a67c340821db075c47fdab9102af`
- `data/wp_model.rda`: SHA256 `a073049d2e5362d386ce479b5b64b6e2abd58795c723f574eb5e092be20e5ea1`

Feature encoding and point weights translated from `nflverse/nflfastR` commit
`701bbf38bc1c030ff9f1f4825a23191287d3903c`, MIT license:

- `R/helper_add_nflscrapr_mutations.R`: `make_model_mutations`
- `R/helper_add_ep_wp.R`: `ep_model_select`, `wp_model_select`, `prepare_wp_data`
- `R/ep_wp_calculators.R`: `calculate_expected_points`, `calculate_win_probability`
- https://github.com/nflverse/nflfastR/tree/701bbf38bc1c030ff9f1f4825a23191287d3903c

The models are loaded as published. No fitting, private data, proprietary model,
LLM-generated statistic, or heuristic EP-to-WP conversion is used.

`tests/fixtures/published.json` contains sixteen ordinary scrimmage rows from 2024
Week 1 nflverse play-by-play, spanning four games, four quarters, and a neutral venue.
Their expected EP/WP values come directly from the published dataset:
https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.parquet

The public dataset contains post-play cumulative `total_home_score` / `total_away_score`;
inference uses pre-play `posteam_score` / `defteam_score`. Fixtures also cover that distinction.
Data access does not imply that every upstream data field or live-feed redistribution is
licensed identically to software; see nflverse's dataset-specific licenses and notices:
https://github.com/nflverse/nflverse-data

Model updates must explicitly change the pinned commit and both sets of compiled-in
checksums, then rerun inference parity. Historical published values may come from a
different model version; inspect that disagreement before enabling automated publication.
