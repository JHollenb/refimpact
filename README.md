# Ref Impact

An NFL Twitter/X bot that compares an enforced ruling with an explicit alternative,
then tweets its expected-points impact and win-probability swing in percentage points.
Uses the open-source nflfastR models without training a replacement model or requiring R.

```text
ARI @ BUF | Q2 2:40
False Start
BUF win chance: 17.4% -> 16.3% (-1.1 pp)
EPA impact: -0.57 points
vs no flag; same pre-snap state
Model estimate; ruling correctness not assessed.
```

This example is from 2024 Week 1. The negative values refer to BUF. The underlying
comparisons and full precision estimates are retained separately from tweets.

## Local dashboard and API

The dashboard shows scores, all reported flag dispositions, accept/decline alternatives,
offensive and defensive production, drive EPA timelines, and game-level relationships. It defaults to the latest NFL slate
that has started as of the generation cutoff, with explicit date ranges and a week selector.
On a Tuesday this includes the just-completed Thursday–Monday slate; upcoming weeks
remain available with missing impact values rather than invented zeros.

```sh
uv sync --locked
uv run refimpact models-fetch
uv run refimpact refresh
uv run refimpact serve --port 8787
```

For everyday launching, use the Makefile from this directory:

```sh
make setup     # First run: install locked dependencies
make start     # Background server, accessible from your phone on the same Wi-Fi
make status    # List this checkout's HTTP server instances
make clean     # Stop those instances; keep all cached data and reports
```

`make run` serves in the foreground instead. Both launch targets default to
`HOST=0.0.0.0 PORT=8787`; startup prints the current LAN URL for your phone.
Keep the Mac awake and connect the phone to the same Wi-Fi. For loopback only,
use `make start HOST=127.0.0.1`; use `PORT=8788` to change the port.
Run `make clean` before changing the bind/port. Server logs append to
`.cache/server.log`. Cleanup matches this checkout's absolute server executable
rather than killing unrelated Python processes or whatever occupies a port.
`make refresh` refreshes results, and `make test` runs the test suite.

Open **http://127.0.0.1:8787** and the interactive API documentation at
**http://127.0.0.1:8787/docs**. The direct `serve` command defaults to loopback; the Makefile launch targets enable LAN access.
It serves the site and API from one origin. No X credentials are needed for the site.

The source of truth is **`.cache/refimpact.sqlite`**. Source imports, generated
penalties/branches, play metrics, drives, production and association statistics live
in indexed tables. The site does not load a season-wide JSON file or download data.

`make refresh` reuses stored sources and existing transformations. On the first run,
missing sources are imported. **`make update`** explicitly downloads new upstream
schedule/PBP revisions. Neither command posts or queues tweets. The running API sees
new results only after a complete generation publishes atomically; Reload view reads
local results and revalidates their revision.

```sh
uv run refimpact refresh --season 2026 --as-of 2026-11-03
# Optional first import from local files:
uv run refimpact refresh --season 2026 --pbp /path/to/plays.parquet --schedule /path/to/games.csv
```

`--database PATH` selects the database for both `refresh` and `serve`. The as-of date
limits game dates, not when upstream corrections became available. No background
updater or hosted deployment is enabled.

- [All mathematical definitions and equations](src/refimpact/web/math.md)
- [Database tables, join examples, generation API and cleanup](docs/database.md)
- Interactive API documentation: **http://127.0.0.1:8787/docs**


| Endpoint | Result |
| --- | --- |
| `GET /api/v1/status` | Generation time, default slate, source IDs/hashes/import times, model version |
| `GET /api/v1/weeks` | Scheduled weeks, dates, coverage and inference-parity diagnostics |
| `GET /api/v1/games?season=2026&week=8` | Scoreboard and measured coverage, one row per game |
| `GET /api/v1/games/2026_08_CHI_SEA` | Full measured calls, alternatives, assumptions and review rows |
| `GET /api/v1/penalties?season=2026&week=8&disposition=declined` | All reported penalty/clock events, alternatives, assumptions, and coverage; optional limit/offset |
| `GET /api/v1/production?season=2026&week=8` | Offensive EPA, snap rates, defensive EPA allowed, drive totals |
| `GET /api/v1/drives?season=2026&week=8` | Drive summaries, native EPA, and separate known penalty effects |
| `GET /api/v1/drives/2026_08_CLE_PIT:22` | A drive's play-by-play EPA timeline and penalty alternatives; use an ID from the summaries |
| `GET /api/v1/calls?season=2026&week=8&min_swing=1` | Original four-type dead-ball comparisons retained for bot compatibility |
| `GET /api/v1/analysis?season=2026&week=8` | The selected slate's game-level associations |
| `GET /api/v1/analysis?season=2026` | Season-to-date associations; omitting week includes all weeks |
| `GET /api/v1/export.csv?season=2026&week=8` | Downloadable game-level CSV |
| `GET /api/v1/methodology` | Units, definitions and coverage boundaries |
| `GET /api/v1/math` | Complete mathematical definitions and equations (Markdown) |
| `POST /api/v1/transforms` | Generate and persist versioned tables from stored sources; returns a job ID |
| `GET /api/v1/transforms/{run_id}` | Job status, source/model/code lineage and errors |
| `GET /api/v1/data/schema` | Actual table columns, keys and indexes |
| `GET /api/v1/data/{table}` | Paginated raw/transformed rows; optional season/game/run filters |
| `GET /api/v1/penalties/{id}` | Full decision branches for one penalty, loaded on demand |
| `GET /api/v1/calculations/{id}` | Saved interactive comparison/decision request and result |
| `POST /api/v1/compare` | Score and store a validated comparison without queuing or publishing |
| `POST /api/v1/decisions` | Evaluate explicit accept/decline/no-flag branches, clock delays, runoffs and timeout use |

```sh
curl 'http://127.0.0.1:8787/api/v1/games?season=2026&week=8'
curl -X POST http://127.0.0.1:8787/api/v1/decisions \
  -H 'Content-Type: application/json' --data-binary @examples/declined-dpi-decision.json
curl -X POST http://127.0.0.1:8787/api/v1/compare \
  -H 'Content-Type: application/json' --data-binary @examples/false-start.json
```

The API is local and unauthenticated. It exposes no tweet-publishing, credential,
arbitrary-file or arbitrary-URL endpoints. If you later expose it beyond loopback,
add the access controls appropriate for that deployment.

## What if? Enter a play yourself

The penalty ledger below values flags that were thrown; it does not say whether a call was
right, and a no-call never appears in play-by-play. To test a disputed moment, open a game and
use **What if?** Pick any play, including one with no flag. Its fields start at what was
recorded next (possession, down, distance, yard line, quarter, clock, scores and timeouts).
Change them to what you think should have happened and the card compares win chance and points
against the recorded state at that moment.

Then choose how that drive ends (TD + XP, TD + 2-pt, field goal or no score) to draw an
alternate timeline. The rest of that drive is replaced. Later drives keep their recorded states
on a shifted scoreboard, and each later snap is re-scored by the same published WP model. Add
more plays to stack edits; each is valued on the scoreboard the earlier ones produced.

```sh
curl -X POST http://127.0.0.1:8787/api/v1/scenarios/evaluate \
  -H 'Content-Type: application/json' --data '{"game_id":"2026_08_CHI_SEA","edits":[
    {"play_id":"2895","changes":{"down":1,"ydstogo":1,"yardline_100":1},"drive_result":"td_xp"}]}'
```

For that edit SEA's win chance at the moment is 30% rather than the recorded 21%. Holding
later plays, SEA leads 24-20 at the Q4 9:01 delay of game (68% rather than 27%) and the
final is CHI 34-24. The two numbers answer different questions. The first is what the call was
worth when it happened. The second is **hindsight**: a touchdown would have changed everything
after it, including field position, play-calling and the turnover that set up SEA's later score.
The result also reports the real game's next win-chance move separately from the call, because
a blocked kick is not the officials' doing.

Without edits the same code reproduces the published `home_wp` line, and every response reports
that check. Overtime is not modeled: a tied alternate final says so rather than giving a win
chance, overtime rows are not re-scored, and the alternate is compared with the regulation score.
Kickoffs and tries have no scrimmage state, so the alternate line carries its last value across
them. A blank state gets a win chance with no timeline. `drive_team` names who a drive result
belongs to; the dashboard defaults to the team that ran the play, and the API warns when its
default gives the result to the next team.

**Stacking: a prediction timeline.** Each alternate point has a **What if from here?** button. It
opens a step prefilled from the alternate state: after a score, the other team's ball at the
season's measured post-kick spot, with the score updated; after no score, the other team's ball.
You set the state and how that drive ended, and add more. Each node shows win chance, Vegas win
chance, EP, the swing from the node before, downs and next-score TD and FG chances. You can remove
the last step, or go back to an earlier node and continue, which replaces what came after. There is
one active path.

A chain ends one of two ways, and they are not mixed. **Prediction**, the default, stops at the last
node: no recorded plays follow, no final score is given, and the win chance is conditional on your
steps. **Rejoin** holds recorded plays from a later play you pick, with scores shifted, and is
labeled hindsight. No chance of reaching a path is computed: the steps are your choices, not a
forecast. The clock cannot run backward, scores cannot fall, and overtime is refused, each with an
error on the field. Chains save with a scenario but are left out of the disputed-calls report and
are never posted. A zero-change branch reproduces its source state exactly.

```sh
curl -X POST http://127.0.0.1:8787/api/v1/scenarios/evaluate \
  -H 'Content-Type: application/json' --data '{"game_id":"2026_08_CHI_SEA","edits":[
    {"play_id":"2895","changes":{"down":1,"ydstogo":1,"yardline_100":1},"drive_result":"td_xp"}],
    "chain":{"from_play_id":"2895","steps":[
      {"possession":"CHI","down":1,"ydstogo":10,"yardline_100":69,"qtr":4,"quarter_seconds":900,
       "home_score":16,"away_score":20,"home_timeouts":3,"away_timeouts":3,"drive_result":"none"},
      {"possession":"SEA","down":1,"ydstogo":10,"yardline_100":65,"qtr":4,"quarter_seconds":750,
       "home_score":16,"away_score":20,"home_timeouts":3,"away_timeouts":3}]}}'
```

SEA's win chance reads 30% at the changed call, 31% with CHI's ball after the extra point and 39%
with a stop and SEA's ball at Q4 12:30. Vegas reads 38%, 33% and 41%.

Each edit also shows the **opportunity** view: downs in hand and the next-score TD, FG and no-score
chances, for the recorded state and yours. Extra downs flip field goals into touchdowns; see
sections 10 and 11 of `/api/v1/math` for the measured goal-line results and the heat panel.

Save a scenario with a label, type, assumptions and source link. Marking it disputed requires a
source and adds it to the game's **Disputed calls** report, which gives the sum of at-the-moment
expected points for those calls. That sum is not a final margin. Win-chance swings are not added.
A source shows who disputes a call, not that the call
was wrong, and this tool never decides that. Report only what you have looked for: disputes
collected for one team alone will lean toward that team.

Scenarios never change source rows and never reach the tweet queue. Anyone on your network can
compute one, but only this computer can save or delete, because the LAN server has no login.

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/scenarios/plays?game_id=...` | Editable plays with prefilled states |
| `POST /api/v1/scenarios/evaluate` | Moment comparison and timeline; saves nothing |
| `POST /api/v1/scenarios/state` | Win chance for one blank state |
| `GET` / `POST /api/v1/scenarios`, `GET` / `DELETE /api/v1/scenarios/{id}` | Saved scenarios |
| `GET /api/v1/scenarios/disputes?game_id=...` | Disputed calls and their summed expected points |

## Reading the scoreboard and game history

The scoreboard leads with **each game's biggest flag swings**: one flag's effect on win chance
when it happened, against keeping the described play, in whole points. Swings under 2 pp are within
model noise, and the spread (Vegas) model is shown beside the non-Vegas one. Inside 2:00 a swing
also reports a clock range; see `/api/v1/math`. Swings of 5+ pp are counted, unpriced flags are
counted beside them, and swings are never added together.

The **expected margin shift, ignores score** is a known-effects comparison ledger across both
possessions. EP has no score input, so a blowout flag counts in full. It measures what penalties
were worth, not what officials did: a correct flag on a real foul counts as a benefit to the other
team. **Observed offense EPA · includes penalties** is actual scrimmage production. These are
separate measurements, not components to add together.

A game's **How the game went** panel gives each team's EPA over its last 5 snaps, big plays over
its last 10 and the current scoring run. It describes the game so far, is not a forecast, and
feeds no value.
For CHI at SEA in Week 8, SEA's +14.16 estimated benefit consists of +13.97 while SEA had
the ball and +0.19 while CHI had it. SEA's observed offensive EPA is -7.75, already
including actual penalties. The local comparison sum cannot be subtracted to replay a
penalty-free game. Unknown effects are disclosed in coverage counts.

Open a game to select either team's perspective and explore:

- Native win chance at recorded game states, with before/after changes in percentage points.
- Cumulative observed offensive EPA (including penalties).
- Cumulative estimated penalty benefit, in a separate ledger across both possessions.
- Cumulative observed drive EPA at each drive ending.
- Every play/event, just penalties, or drive summaries. Select a chart point or event
  for its EPA, cumulative totals, actual win-chance change, and ruling alternatives.

Actual progression comes from nflverse's non-Vegas nflfastR `home_wp` and
`home_wp_post`, normalized to the selected team. A 40% to 45% movement is +5 pp.
Game cumulative chance change uses the latest recorded probability minus the first,
not a sum of local ruling swings. Gaps between rows need not equal the sum of observed
play changes. Missing endpoints remain unknown; a finished tie is labeled as a tie,
not a remaining 50% chance to win. Final non-tie endpoints represent the known result.

Drive chance changes span the first scrimmage/penalty action through the final recorded event assigned to the drive,
including tries/admin records but excluding kickoffs. Drive EPA retains its own
previously documented scope. Cumulative EPA adds available rows; missing-row counts
identify partial sums. Observed progression includes football action, actual penalties,
and clock effects; it does not isolate the causal effect of officials.

`GET /api/v1/games/{game_id}/progression` returns the timeline, drive endpoints,
cumulative EPA, native probabilities, and penalty breakdown. The full game endpoint
also includes this object; an upcoming game returns a null progression. Versioned
replay reports save `progression.json` alongside the original production and drive records.

## Relationship with score and winning

The metric selector compares one of three home-oriented values with final home score
margin, once per game: the expected margin shift (net measured enforcement EPA), home minus away offensive
EPA per snap, or home minus away observed drive EPA. Defense allowed mirrors the
opponent's offensive production; it is not an independent second contribution to add.
The selected-metric win count compares the sign of the metric with the winner, excluding
ties and exact zeros from that count. `GET /api/v1/analysis?metric=production_epa` or
`metric=drive_epa` returns the corresponding analysis. Omit `week` for the season.

Same-game offensive and drive production naturally relate to scoring. These displays
are descriptive accounting, not evidence that adding production improves a ruling's
causal estimate. Native EPA already reflects the actual penalty. Conditioning on later
production can also remove part of a penalty's downstream consequence. We keep the
observed production and conditional penalty alternatives separate.

Week 8's refreshed ledger contains 227 reported flag/clock events, including 30 entirely
declined events, across 14 final games. This is broader than the original accepted-flag
indicator. Exact valuation still depends on recoverable play and enforcement evidence;
coverage counts and missing values are returned with each generated dataset.

The bootstrap resamples games 1,500 times with a fixed seed and retains directional
results and uncertainty. Repeated teams, team strength, coaching, game state and
penalty opportunities are not controlled. These are descriptive estimates rather
than causal attribution or a test of referee bias. WP already uses score and clock,
so an association of WPA with winning is partly built into the model; the game-level
plot uses EPA instead and never adds WP swings into a counterfactual win probability.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11–3.13 (uv can install Python).
Run these commands from this repository:

```sh
uv sync --python 3.12 --locked
uv run refimpact models-fetch
uv run refimpact compare examples/false-start.json
uv run refimpact replay --season 2024 --week 1 --output reports/2024-week-01
```

`models-fetch` downloads about 3 MB from a pinned fastrmodels commit and verifies
both upstream files and extracted model checksums. Inference runs on CPU with one thread.
The replay reuses stored public nflverse play-by-play (importing only if absent) and filters the requested
week, including postseason week numbers 19–22 where present in the source.
For a local source, pass `--pbp path/to/play_by_play.parquet`.

## What the numbers mean

EPA measures a change in expected points; WPA measures a before/after change in
winning probability. A penalty's effect compares the actual ruling with a specific
alternative, using the same reference team. Its win-chance swing is the difference
between those branches' WPA, in percentage points.

The complete equations, model features, enforcement/clock rules, production and drive
accounting, cumulative metrics, correlations and bootstrap statistics are defined in
[the mathematics reference](src/refimpact/web/math.md). The API serves the same canonical
file at `/api/v1/math`. This avoids maintaining competing copies of the definitions.

For CHI–SEA play 3158, accepting defensive holding rather than keeping the written
interception touchback gives SEA +5.690 EPA and +14.605 pp of conditional ruling benefit,
even though the recorded play's WPA is −1.026 pp. The worked derivation is in the reference.

Reproduce the database-backed arithmetic and native-inference audit with:

```sh
.venv/bin/python scripts/audit_penalty_math.py
```

## Automatic coverage and manual comparisons

The API/dashboard ledger retains accepted, declined, offsetting, rescinded, mixed,
and unclassified flags, including flags reported only in text when `penalty=0`. Multiple
fouls on one play are kept together, avoiding duplicate EPA attribution. Native play EPA
is retained even where numerical alternatives cannot be reconstructed.

For recoverable scrimmage plays, the ledger evaluates the described play result with
no flag, declining enforcement, and accepting enforcement. The choice edge is the
actual option's EPA minus the alternative option's EPA, from the possession team's
perspective. A declined flag has zero immediate spatial enforcement effect at the common
clock instant, but its accept/decline choice edge can be nonzero. It can also affect clock
restart timing. Do not equate those quantities.

Nullified actions are processed automatically from the written description preceding
the flag. The API's `nullified_play` record retains the exact text and source row, and
reports `valued`, `not_recorded`, or `unpriced`. This ledger has no manual-review or video
step; use **What if?** to enter an action the source does not record.
Use `/api/v1/penalties?season=2026&week=8&nullified_status=valued` to find calculated
comparisons; `nullified_coverage` discloses the available and missing result counts.

When text supplies a gain/loss, no gain, or an incompletion, the parser constructs the
state if that action counted, and compares it with actual enforcement in EPA and WP
percentage points. A failed third down becomes fourth down, not an assumed punt. A
failed fourth down changes possession. Erased touchdowns and field goals are points on the board
plus the opponent after the kick, at a measured season-mean start spot, with the extra point assumed
good; these values are approximate and labeled. So are kick placements (against the season-mean
kick), erased tries (the written points against the re-try at the season's rate), single muffs and
blocked-kick recoveries, and safeties enforced on the offense. Several loose balls, onside kicks,
scores that stood and non-applicable states retain their written text without an invented numerical
value, with an `unpriced_reason`. A declined flag has no effect to price, so coverage is priced
flags over flags that took effect: 97% across 2024 and 2026. Revised play descriptions use the final
written version.
The action is conditional evidence: it does not establish when a whistle sounded or
whether players reacted to it.

Interception touchbacks and explicit, unambiguous interception return endpoints are
recoverable automatically, including on no-play rows, as are written punt and kickoff results and
a single written fumble recovery, a fumble out of bounds, and a flag with nothing written before
it. A kickoff row has no scrimmage pre-state, so it compares the next
recorded snap with the written return. Anything else stays unpriced; it is not assigned zero.

Clock comparisons apply a reported runoff once. Adjacent administrative rows explicitly
recording a timeout to avoid runoff are linked to the penalty, and the actual branch
spends that timeout once; unrelated timeout budget changes are held fixed. A reported
runoff inconsistent with adjacent clocks stays unpriced. `timing_comparisons` pairs
the actual and alternative branches at the same declared next-snap opportunity, using
their respective restart rules. It reports each WP/WPA, the ruling swing, and the extra
clock effect beyond the immediate comparison. Unknown restarts retain both conditional
cases; they are not averaged or silently treated as observed time saved.

Rule-specific alternatives follow the [2026 NFL rulebook](https://operations.nfl.com/rules-officiating/2026-nfl-rulebook):
an end-zone DPI scenario uses the one-yard line (half the distance inside the two),
and defensive holding on a run is not blindly assigned the passing play's previous-spot
enforcement. Accepted geometry preferentially comes from the next recorded down.

No-play statistical yardage is often zero even when the description contains a gain
or loss. That zero is never used to recover the action, and penalty yardage is never
mistaken for action yardage. For a pre-snap flag with no action text, the original
enforcement metric compares with a fresh down without yardage enforcement; the separate
erased-result metric remains unavailable. For example, CHI at SEA at Q4 9:01 is recorded
only as delay of game, with no erased run description. The pipeline does not fabricate
that missing run from a later conversion or from the previous illustrative scenario.

The erased-result comparison uses a common evaluation clock because the discarded
action's duration is not reliably recorded. It appears alongside enforcement EPA and
native production; the separate erased-result field is never added on top of enforcement
in drive EPA or the game-level ledger. When the same written result also supplies the
no-flag branch, that enforcement comparison is counted once. A positive effect versus an erased result is possible even when the
five-yard enforcement cost is negative.

A caught 20-yard pass with declined DPI does not reveal the interference spot. We retain
the catch and show labeled acceptance scenarios at sampled legal spots, without treating
the catch location as the foul location or assigning a probability. The sampled range is
neither a confidence interval nor an exhaustive bound. Supply the actual foul spot via
`POST /api/v1/decisions` for a precise conditional comparison.

Reported ten-second runoffs are deducted once, including replay-only clock administration.
If a deduction exhausts the half or regulation, the evaluator uses an explicit terminal
state instead of feeding a zero-time scrimmage state to the model. Regulation ties and
halftime WP remain unknown because this version does not model their continuation.
Foul-related runoff waiver/timeout options are labeled scenarios where applicable;
a timeout option consumes one available timeout. Avoided runoffs remain recorded.

Rule 4 restart conditions distinguish snap, ready/running, and unknown clocks. The
published model has no clock-running feature, so late-half clock stoppage is evaluated
at explicit 10/20-second next-snap delay scenarios. Unknown restarts preserve both
possibilities. These are sensitivity assumptions, not observed time saved. Post-snap
spatial alternatives use the next recorded down's timestamp as a common horizon because
the source does not reliably supply a play-end timestamp. Not every flag implies a runoff.
Rules reference: [2026 NFL Rulebook, Rule 4](https://operations.nfl.com/rules-officiating/2026-nfl-rulebook).

Offensive totals include native scrimmage EPA and flag-only scrimmage rows; per-snap
rates use run/pass/kneel/spike rows with available EPA. Missing-EPA row counts are
shown. Defensive EPA allowed equals the opponent's offensive EPA (lower is better).
Drive EPA sums native EPA from that drive's offense perspective, including punt/field-goal
endings and excluding kickoffs/tries. The penalty effect ledger appears alongside it;
**never add it to observed drive EPA again**. This is not a replay of the full drive
without penalties. Missing EPA makes a sum partial, not zero.

The bot enqueue path and `/calls` preserve the original four verified dead-ball types
(false start, offensive delay of game, encroachment, neutral-zone infraction), with their
original timing exclusions and enforcement checks. Expanded alternatives are available
through `/penalties` and `/decisions`; speculative scenarios are not automatically tweeted.
Opening-kickoff data supplies the expected second-half receiver, following nflfastR's
opening-possession assumption. Unusual coin-toss choices need manual correction.

Use `examples/manual-holding.json` as a template for a disputed call or missed call:

```sh
uv run refimpact compare examples/manual-holding.json --output reports/manual.json
uv run refimpact compare my-comparison.json --enqueue
```

The manual example is **hypothetical**, not an adjudication of an actual game. Supply
the entire actual and alternative states, a concise `alternative_label`, and the full
assumptions. Both branches must stay within the same half and represent regulation
scrimmage states. Possession changes are normalized to the fixed reference team.
The original `/compare` contract excludes overtime, terminal states, kickoffs, PATs, and cross-half EP comparisons. The `/decisions` clock evaluator handles expiration as described above.
Scoring rulings generally need those states and should remain unscored in this version.
Tweet labels must be ASCII and the resulting post must fit 280 characters.

## Queue and publish

Preview and enqueue a week, optionally filtering for larger swings:

```sh
uv run refimpact replay --season 2024 --week 1 --enqueue --min-swing 1
uv run refimpact queue --status pending
uv run refimpact publish --limit 3
```

Publishing previews by default. To post, copy `.env.example` to `.env` and configure
your own bot account's X developer app credentials. Keep `.env` private; it is ignored
by Git. The account must have API access, write permissions, and sufficient credits.
`X_EXPECTED_USER_ID` prevents an accidental post to the wrong account.

```sh
uv run refimpact publish --live --env-file .env --limit 1
```

No credentials are included, and this repository's verification does not publish posts.
Uses the [official X API](https://docs.x.com/x-api/posts/create-post) through Tweepy.
X API fees and data access are separate from the open-source software licenses.
See [current pricing](https://docs.x.com/x-api/getting-started/pricing) and
[automation rules](https://help.x.com/en/rules-and-policies/x-automation).

Every comparison revision has a deterministic content ID in SQLite. Repeated polling
does not duplicate posts. Revised pending data supersedes older pending data. Revisions
to already posted or uncertain calls are held as `review`; they are not silently tweeted.

A timeout or uncertain API response becomes `uncertain`, and a crash during a request
leaves `sending`. Neither is retried automatically. A definite 429 rejection stays
pending for a later cycle; rejected credentials or invalid requests become `failed`.
Stop workers, inspect the account, and resolve the local audit record:

```sh
# The tweet exists: record its numeric ID.
uv run refimpact reconcile POST_ID --tweet-id 123456789
# The tweet does not exist: explicitly authorize a retry.
uv run refimpact reconcile POST_ID --confirm-not-posted
```

`review` records can also be reconciled, but inspect the prior published version first.
If publishing a correction, make its ruling/alternative wording clearly identify it as
a correction. You can create a corrected manual comparison rather than replaying the
same unlabeled revision. Inspect the SQLite `audit` table for the full transition history.

## Run the bot

One postgame polling cycle (preview only):

```sh
uv run refimpact run --season 2024 --week 1 --min-swing 1
```

Continuously refresh a selected week and publish at most five pending tweets per cycle:

```sh
uv run refimpact run --season 2026 --week 9 --watch --download --live --env-file .env \
  --interval 900 --min-swing 1 --limit 5
```

Choose the season and week deliberately: this also processes historical calls in that
week. The worker remains on that week; change it when the next week begins. Ctrl-C stops
the worker. Run one polling worker per tweet-outbox database. `--download` explicitly
refreshes the source before each cycle; omit it to reuse the stored input. A source
revision creates a new source version rather than overwriting prior rows.

This version is **postgame**. nflverse public play-by-play is generally released after
games, with later corrections; it is not a play-by-play live feed. Polling faster does
not change that. [nflverse availability](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html).
A future live provider must supply equivalent state and enforcement fields, with data
rights appropriate for public posts. No undocumented live endpoint is assumed here.

## Reports and verification

The optional standalone `replay` command exports the following (the web pipeline writes directly to tables):

- A content hash referring to source rows retained in the database; no duplicate raw week JSON.
- Full comparisons, assumptions, EP, EPA, WP, model identity, and tweet previews.
- `penalties.jsonl`, `production.json`, and `drives.jsonl` with all reported events, alternatives, native production, and drive timelines.
- Every legacy unscored penalty with its review reason.
- A cross-game sample comparing Python inference to published nflfastR EP/WP.
- A summary with source and code hashes, in a versioned run directory.

An enqueue request requires published-model inference agreement (absolute EP difference
at most 0.00001 points, WP at most 0.000001). These tolerances cover float32 model
serialization and EP accumulation. A mismatch retains the reports and leaves the queue
unchanged. This checks implementation compatibility, not a scientific effect threshold.
Source artifacts are versioned; later interpretations do not rewrite older failure rows.

```sh
uv run ruff check .
uv run pytest -q
```

Tests exercise real published EP/WP fixtures, fixed-team signs, state contracts,
enforcement reconstruction, score timing, tweet units, and failure-safe posting. Model
tests skip if `models-fetch` has not run. See [UPSTREAM.md](UPSTREAM.md) for provenance
and [THIRD_PARTY.md](THIRD_PARTY.md) for upstream notices.
