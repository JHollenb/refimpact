# Ref Impact: mathematical definitions

Version 0.5. Every quantity below is descriptive or a conditional model comparison.
Win chance and expected points come from the pinned open-source nflfastR/fastrmodels models
documented in `UPSTREAM.md`, which are not refit, and EPA is never converted into win probability
with a constant. The one fit of our own is the first-down and this-drive chance in section 14. It
values no flag and feeds no win chance.
All missing/unsupported values are SQL NULL / JSON null, unless an identity establishes
an exact zero. Known-only sums disclose missing counts.

## 1. States, reference teams and model outputs

A state `s` contains season, home/away teams and score, possession, quarter, game/half
seconds remaining, down, yards to gain, distance to the opponent goal, remaining
home/away timeouts, second-half receiver, roof and neutral-site indicator. Applicable
states are regulation scrimmage states: no kickoff/try, no end-zone location, no
expired clock, and no cross-half counterfactual.

Let `T` be a fixed reference team, `P(s)` the possession team and

\[
\sigma_T(s)=\begin{cases}+1 & P(s)=T\\-1 & P(s)\ne T.\end{cases}
\]

The EP model predicts seven mutually exclusive next-score/half-end probabilities,
ordered as touchdown, opponent touchdown, field goal, opponent field goal, safety,
opponent safety, no score. With possession-team probabilities `p_k(s)`:

\[
EP_P(s)=7p_{TD}-7p_{oppTD}+3p_{FG}-3p_{oppFG}
       +2p_{S}-2p_{oppS}+0p_{none},\qquad
EP_T(s)=\sigma_T(s)EP_P(s).
\]

This estimates net expected points through the next scoring event or half end;
it is not expected final score, drive points or total future offensive production.

The next-score classes also feed the opportunity view in section 10. Valued states
preceding a field goal or kneel use this base EP, not upstream's special-cased EP for those
rows (mean gap 0.22, max 1.92 points; 72 priced flags in 2024 and 2026 have such a next row).
Both branches use the same model, so the comparison is coherent; it can differ from the
published EPA on those rows.

The non-Vegas WP model directly predicts possession-team win probability `w(s)`:

\[
WP_T(s)=\begin{cases}w(s)&P(s)=T\\1-w(s)&P(s)\ne T.\end{cases}
\]

Its feature vector includes possession team's score differential `D_P`, down,
distance, field position, both clocks, offensive/defensive timeouts, home indicator
and whether that team receives the second-half kickoff (first half only). Its
clock-adjusted score-ratio feature is

\[
R(s)=\frac{D_P(s)}{\exp[-4(3600-t_{game}(s))/3600]}.
\]

The EP features use half clock, field position, distance, down indicators, timeouts,
home/neutral indicator, roof indicators and the upstream season-era indicators
(through 2001; 2002–05; 2006–13; 2014–17; 2018 onward).

At neutral sites the WP model keeps the home indicator at 1, as upstream does. It moves WP
about 5.9 pp at the median and is inherited, not adjusted. Only EP uses home=0 there.

The spread (Vegas) WP model is the same upstream family with one more feature,
`spread_time = S_P exp[-4(3600-t_game)/3600]`, where `S_P` is nflverse `spread_line` from the
possession team's side. It is pinned at the same commit and shown beside the non-Vegas model,
never instead of it. Measured against published `vegas_wp` on 58,881 regulation snaps in 2024
and 2026 the largest error is 1.2e-7, except 73 rows of 2024_05_DAL_PIT (up to 7.5 pp), where
upstream reads a pregame comment row as the opening kick and mislabels the second-half receiver.

Learned tree models are approximate and need not be locally monotone for every
apparently favorable yardage change. We preserve their predictions and signs.
Neither model evaluates whether an official's decision was correct.

## 2. EPA, WPA and a ruling's effect

Let `b` be the common pre-play state, `a` the actual branch, `u` an alternative and
`D_T(s)` the reference team's score minus its opponent's score. Net realized points are

\[
Q_T(s;b)=D_T(s)-D_T(b).
\]

For any applicable branch:

\[
EPA_T(s;b)=Q_T(s;b)+EP_T(s)-EP_T(b),
\qquad WPA_T(s;b)=100[WP_T(s)-WP_T(b)].
\]

EPA units are expected points. WPA units here are **percentage points (pp)**;
native nflverse `wpa` uses probability units, so multiply it by 100 to obtain pp.
A change from 40% to 45% is +5 pp; a relative percentage change would be
`100(0.45-0.40)/0.40 = 12.5%`. We report the former.

A penalty comparison is actual minus alternative:

\[
I^{EPA}_T=EPA_T(a;b)-EPA_T(u;b)
        =[Q_T(a;b)-Q_T(u;b)]+EP_T(a)-EP_T(u),
\]
\[
I^{WP}_T=100[WP_T(a)-WP_T(u)]
        =WPA_T(a;b)-WPA_T(u;b).
\]

The before-state cancels in the ruling difference. Possession is normalized to the
same reference team on both branches, including turnovers and failed fourth downs.
For the opposite team the EPA/WPA/swing sign reverses and win chance complements to
one. A branch's entire EPA or WPA is not attributed to the penalty.

### Worked example: 2026 Week 8, CHI at SEA, play 3158

SEA has 2nd-and-1 at the CHI 16. The description explicitly records an interception
in the end zone followed by a touchback, erased by defensive holding. With enforcement,
SEA has 1st-and-10 at the CHI 11. Without it, CHI has 1st-and-10 at its own 20.
Both branches use the next recorded clock, 12:37 remaining; before WP is 23.505%.

| SEA perspective | EP after | EPA | Win chance | WPA (pp) |
|---|---:|---:|---:|---:|
| Enforcement stands | 5.096352 | 0.001212 | 22.479248% | −1.026244 |
| Interception stands | −0.593183 | −5.688324 | 7.874417% | −15.631075 |
| Actual minus alternative | 5.689535 | 5.689535 | +14.604831 pp | +14.604831 |

The play's recorded WPA is negative, while the ruling's conditional benefit is positive.
Those compare against different baselines. There are no realized points on this play.

## 3. Reconstructed alternatives and enforcement

For original possession team's distance to goal `y`, distance to gain `g`, down `d`
and an explicitly written gain `x`:

\[
y'=y-x.
\]

If `x >= g`, the next state has first down and `g'=min(10,y')`. Otherwise a played
snap consumes a down, `d'=d+1`, and `g'=g-x`. A failed fourth down gives the opponent
first down at `y_{opp}=100-y'`, with `g_{opp}=min(10,y_{opp})`.
An incompletion/no gain uses `x=0`. A pre-snap dead-ball foul does not consume a down.
For a foul awarding an automatic first down, first down overrides the ordinary down rule.

For a nominal distance `k` when half-distance applies:

\[
k_{off}=\min(k,(100-y)/2),\qquad k_{def}=\min(k,y/2).
\]

The offensive/defensive direction changes `y` by `+k_off` / `-k_def` respectively.
Specific rules override these generic distances. End-zone DPI is at the one-yard line,
or half-distance if the previous spot is inside the two. A run's enforcement spot is
not assumed to be a passing play's previous spot. Observed accepted geometry is
preferentially read from the next recorded scrimmage state.

The no-enforcement branch holds the **described physical action** fixed. This is not
proof of what a foul-free play would have produced. For a pre-snap flag the default
alternative is the fresh unplayed down without yardage enforcement. A written erased
action is a separate conditional branch. It is never inferred from a no-play yardage
zero, penalty yards, a later conversion or an assumed punt.

Interception touchbacks use the receiving team's own 20 (not a kickoff touchback spot).
A return requires an unambiguous written endpoint. A wholly declined flag has exactly zero
immediate spatial enforcement effect at the common clock, but its acceptance choice and
clock-stoppage effects can differ.

**Erased scores (approximate, labeled).** An erased touchdown or field goal is a branch with
the points on the board and the opponent's first down after the kick, at the same clock:

\[
EPA_T(u;b)=p+EP_T(s_{kick})-EP_T(b),\qquad p=\pm7\text{ or }\pm3,
\]

so the value against an actual branch is `7 - EP_opp(s_kick)` rather than a flat 7, and it is
coherent with how the actual branch is valued. The extra point is assumed good. `s_kick` has
distance to goal equal to the season's whole-yard mean on the receiver's first snap after a
kickoff, measured from the rows themselves (70 in 2024, 69 in 2026). The win-chance branch adds
the points and uses the same state. A defensive or return touchdown credits that team. A score
that stood, with enforcement on the try or kickoff, stays unpriced.

**Kicks and fumbles.** A punt's no-flag branch is the receiving team's first down at the written
return endpoint, fair catch, out-of-bounds spot or touchback. A missed field goal gives the
opponent the spot of the kick (line of scrimmage plus eight) or its own 20 if that is farther
out. A kickoff row has no scrimmage pre-state, so it compares the next recorded snap with the
written return, using that snap's clock and timeouts for both; the shared before-state is the
no-flag branch. A single written fumble recovery, with any return, gives possession and spot.
A single muff, or a single recovery of a blocked kick, works the same way. A fumble out of bounds
stays with the offense at whichever is farther from the goal, the spot of the fumble or the
inbounds spot (Rule 8-7). An interception fumbled back to the offense is a new series. A lateral
carry ends at the last written spot. A spike uses the down.

**Kick placements.** A kickoff out of bounds or short of the landing zone is placed by rule, so the
feed has no foul-free result. It is compared with a kick kept in play: the receiver's first down at
`s_kick`. Onside kicks, offsetting placements and illegal touches stay unpriced.

**Tries.** A try has no scrimmage state, so both sides are read after the next kick, at the row's
clock, with the opponent at `s_kick`. When a written result is erased and the try is run again,

\[
EPA_T = r\,V(\text{points made}) + (1-r)\,V(0) - V(\text{points written}),
\]

where `V` is margin plus EP (or WP), and `r` is the season's measured rate for the kind of try that
followed. Distance is not separated: a re-try from 10 yards farther or half the distance uses the
same rate, and the label says so. A try that stood with enforcement between downs, and a foul with
no try written, stay unpriced.

**No written action.** A no-play row with nothing written before the flag has no snap to erase, so
the foul-free state is the same down, not yet played. Checked against the next recorded snap on the
63 such flags: 62 show the same down or a first down with the yard line moved by the reported
yards. The other's next row is a timeout, so there is nothing to compare. A row that also mentions
an injury or lacks a pre-state stays unpriced.

**Safeties.** A safety enforced on the offense is the next recorded snap, with two points added to
the defense. A safety that was nullified stays unpriced.

Every remaining unpriced flag carries an `unpriced_reason`: overtime, tries with no written result,
onside or offsetting placements, scores that stood, feeds that do not describe the play, text that
names no foul, or ambiguous kick and turnover sequences. Across 2024 and 2026, 5,537 flags took
effect. 165 (3%) remain unpriced, down from 1,051 (19%). A declined flag has no effect to price, so
coverage is 97% of flags that took effect, not a share of all reported flags.

The signed choice edge is `EPA(actual option)-EPA(other option)` and likewise for
WPA. It is not an assertion that the selected option was optimal. Multiple fouls on
one play are evaluated jointly, once; individual values are not invented. Missing
DPI spots produce labeled spot scenarios. The sampled minimum/maximum is neither a
confidence interval nor an exhaustive bound; scenarios have no assigned weights.

Rules: [2026 NFL official rulebook](https://operations.nfl.com/rules-officiating/2026-nfl-rulebook),
especially Rules 4, 8, 11 and 14.

## 4. Clock/runoff and timeout interventions

For branch `j`, let its base half/game clocks be `h_j,t_j`, reported applied runoff
`r_j`, declared next-snap opportunity delay `delta`, and restart state `c_j`.

\[
e_j(\delta)=r_j+\delta\,1\{c_j\in\{running,ready\}\},
\qquad h'_j=h_j-e_j,\quad t'_j=t_j-e_j.
\]

A snap restart consumes no game-clock opportunity delay. An unknown restart is
represented by separate running/snap scenarios, never averaged. Actual-minus-alternative
comparisons pair the same `delta` across both branches with each branch's restart rule.
These are sensitivities, not measured time saved or inferred coaching decisions.

If a branch explicitly spends a timeout for team `U`, `TO'_U=TO_U-1`. It cannot also
apply the avoided runoff. A charged timeout on an adjacent administrative row is
linked only when the feed explicitly associates it with avoiding the runoff and its
remaining-timeout count confirms one charge. Other timeout changes are held at the
pre-state budget and are not attributed to the flag.

Reported ten-second runoffs are applied exactly once. For a post-snap following
clock already reflecting the deduction, restore it by ten before the explicit
intervention. If that restoration would precede the pre-play clock, timing is
inconsistent and the estimate stays unpriced. Pre-snap flags use their reported clock.
The next recorded down's clock is a common evaluation horizon, not a known timestamp
for when erased action ended.

If `e_j >= h_j`, no invalid scrimmage state is passed to the model. EP for the expired
half is zero. At regulation end with a non-tied score, WP is exactly 1 or 0 according
to the reference team's lead. Halftime WP and regulation-end ties remain unknown in
this evaluator because a supported next-half/overtime state is not supplied.

A branch's declared clock/timeout component is

\[
C^{EPA}_j=EP_T(s'_j)-EP_T(s_j),\quad
C^{WP}_j=100[WP_T(s'_j)-WP_T(s_j)].
\]

The additional effect of the matched timing scenario beyond the immediate comparison is

\[
A^{WP}(\delta)=I^{WP}(\delta)-I^{WP}(0),\qquad
A^{EPA}(\delta)=I^{EPA}(\delta)-I^{EPA}(0).
\]

These include the declared interventions jointly; the model is nonlinear, so separate
clock and timeout effects are not assumed independently additive.

Inside 2:00 of a half the flag's win-chance swing also reports a range over `delta` of 0, 10 and
20 seconds; these are sampled scenarios, not a confidence interval.

The headline `enforcement_wp_swing_pp` charges a wait only when the team needing time could not
stop the clock. That team is the offense when tied or behind, otherwise the defense, read from
the score and timeouts in the branch whose clock runs:

| Team needing time | Timeouts | Wait charged |
|---|---:|---:|
| Either | 1 or more | 0 s, same-clock headline |
| Has the ball | 0 | 10 s, a hurry-up |
| On defense | 0 | 20 s, the offense runs it down |

`enforcement_wp_common_clock_pp` always keeps the same-clock value. An unknown restart, or two
running branches that need different waits, keeps the same-clock headline and the range. A wait
that runs out the half counts only for a team that is ahead, which wants that outcome; a team tied
or behind would hurry or spike, so its 0% is left out. The 10 s and 20 s are assumptions, not
measurements. The timeout a team spends to stop the clock is not charged, so a same-clock headline
slightly favors the branch where that timeout would be used.
Expected points stay at the common clock everywhere.

## 5. Observed offense, defense and drive production

Let `S_T` be known-EPA scrimmage rows for offense `T`: runs, passes, kneels, spikes,
and flagged no-play rows with down 1–4. Native row EPA is already inclusive of the
actual enforcement. Let `N_T` be actual snap rows in that set with known EPA.

\[
O_T=\sum_{i\in S_T}EPA_i,\qquad
O^{snap}_T=\sum_{i\in N_T}EPA_i,\qquad
\overline{EPA}^{snap}_T=O^{snap}_T/|N_T|.
\]

The rate denominator uses known-EPA snaps; `snaps` also reports all qualifying snaps.
`administrative_epa` sums flagged no-play rows. `unflagged_snap_epa` sums known-EPA
snaps without a reported flag, with its count. Empty sets are unknown, not zero.

For opponent `U`, defense EPA allowed is `D^{allowed}_T=O_U`, and its per-snap rate is
`O_snap_U/|N_U|`. Lower is better. A defense contribution expressed as benefit to `T`
would be `-D_allowed_T`; the displayed allowed metric is not negated.

For drive `d` driven by `T_d`, qualifying action rows also include punts/field goals
and exclude kickoffs/tries. Normalize each native EPA to the driving team:

\[
E_{i,d}=EPA_i\,[+1\text{ if }P_i=T_d\text{ else }-1],\qquad
DriveEPA_d=\sum_{i\in d,\ known}E_{i,d}.
\]

Drive cumulative EPA is the prefix sum within that drive. Team cumulative drive EPA
is the sum of that team's completed/recorded drive totals. Team `drive_epa` is the
sum of its known drive totals. These can differ from offensive totals because the
included play types differ. Missing action EPA is counted explicitly.

The separate drive penalty-benefit ledger is the sum of known penalty comparisons
oriented to `T_d`; it is not added to observed drive EPA. Drive WP change is the
native driving-team WP at its last assigned event minus WP at its first qualifying
action, times 100. Tries/admin assigned to the drive may affect this WP window;
kickoffs are excluded. Missing endpoints remain unknown.

## 6. Cumulative accounting and game-level quantities

For team `T`, through event `n`:

\[
C^O_T(n)=\sum_{i\le n,\ i\in S_T}EPA_i,
\quad C^{pen}_H(n)=\sum_{e\le n,\ known}\sigma_H(b_e)I^{EPA}_{P_e}.
\]

The penalty ledger spans both possessions. The game's `net_home_epa` is its final
known-effects penalty sum, split into home-on-offense and home-on-defense subtotals. It is shown
as the **expected margin shift, ignores score**: EP has no score input, so a flag in a blowout
counts in full. In 2024 and 2026, three-score flags were 10.5% of absolute ledger EPA and 1.4%
of absolute WP. The game view therefore leads with each flag's win-chance swing at the moment,
the largest swing and the count of swings of 5 pp or more, with the unpriced count beside them.
Swings are never added together.

If no estimate is known the ledger is unknown. A declined or picked-up flag's exact zero is not
a measurement: it adds nothing to the sum and is not counted as priced. Coverage is priced flags
over flags that took effect.

Native observed home play WPA is `100(home_wp_post-home_wp)`. Game cumulative WP
change is `100(latest recorded WP-first recorded WP)`, **not** a sum over gaps and
not a sum of counterfactual penalty swings. Administrative start/end markers do
not create extra play WPA. Final ties do not display 50% as a remaining chance to win.

The relationship views use these home-oriented predictors:

\[
X_{pen}=net\_home\_epa,\quad
X_{prod}=\overline{EPA}^{snap}_H-\overline{EPA}^{snap}_A,\quad
X_{drive}=DriveEPA_H-DriveEPA_A.
\]

Final margin is `M=home_score-away_score`; total points is `home_score+away_score`.
Observed offense already includes penalties. Subtracting the comparison ledger from
production or the score does not reconstruct a penalty-free game. Future playcalling,
field position, possession, clock, turnovers and scoring would all change.

## 7. Association statistics

Use one row per completed game with complete PBP and a known selected metric; penalty
analysis additionally requires a known penalty comparison. Exclusions are reported,
not imputed. For selected predictors `x_i` and final margins `m_i`:

\[
r_{XM}=\frac{\sum_i(x_i-\bar x)(m_i-\bar m)}
{\sqrt{\sum_i(x_i-\bar x)^2\sum_i(m_i-\bar m)^2}}.
\]

Pearson correlation requires at least three observations and variation in both arrays.
The point-biserial home-win statistic is the same correlation formula with
`y_i=1{M_i>0}` instead of margin, after excluding ties.

The exploratory 95% interval draws 1,500 game-resampling bootstrap samples of size
`n`, with replacement, using NumPy seed `20261103`. It reports the 2.5th and 97.5th
percentiles of defined bootstrap correlations and the number of valid replicates.
It is not team-clustered and does not establish causation or pregame predictive value.

For decisive games with nonzero selected metric, metric-aligned win rate is

\[
\frac{\#\{i: sign(X_i)=sign(M_i),\ X_i\ne0,\ M_i\ne0\}}
{\#\{i:X_i\ne0,\ M_i\ne0\}}.
\]

This fraction is null when its denominator is zero. Repeated teams, game situation,
coaching, team strength and penalty opportunities are not controlled. WP's relationship
to score and winning is partly built into the WP model itself.

## 8. Audit diagnostics, storage and reproducibility

Native model parity reports `max_i |predicted_EP_i-source_EP_i|` and similarly for WP.
The arithmetic audit verifies branch identities, opposite-team signs/complements,
matched clock WPA, event attribution and game-ledger reconciliation. This verifies
implementation/inference and arithmetic, not calibration, whistle timing or causality.

Source versions are content-addressed. A transformation identity hashes the source
IDs, season/as-of date, pinned model, pipeline version and numerical code version.
Reusing that identity reuses its stored results. All downstream joins must include
`run_id`; joining raw plays additionally uses `runs.pbp_source_id` as `source_id` and
`game_id,play_id`. Missingness and scenario labels survive storage unchanged.

## 9. Display and bot selection conventions

Computations and the API retain full precision. Labels round EPA to the displayed decimal
places and show win chance and WPA in whole points, because the models are tree steps: one extra
yard moves WP up to 2 pp at the 99th percentile, with a median of zero. A ruling swing under 2 pp
is labeled within model noise. Ranking penalties uses the largest absolute known ruling/choice WP
swing (pp), with absolute EPA only breaking ties; points and percentage points are never compared
as the same unit. History and scatter charts label their numerical units explicitly.

The original bot's optional queue filter keeps eligible legacy comparisons when
`abs(wp_swing_pp) >= min_swing`. Those four dead-ball types have stricter reconstruction
and timing coverage than the expanded dashboard ledger. Queue selection is not a
scientific significance threshold or a call-correctness decision. A transform never
queues or publishes tweets.

## What if? timelines

An edit replaces the state recorded after a play. At the moment, `win-chance swing = 100 * (WP(entered) - WP(recorded next))` and `points swing = (margin + EP)(entered) - (margin + EP)(recorded next)`, both on the same fixed-team model.

A drive result then ends the edited drive with `home score at rejoin = entered home score + points if home has the ball`, and likewise for away. Every later row uses its recorded pre-snap state with `score shift = alternate score at rejoin - recorded score at the end of that drive`. Nothing else about later rows changes, so the alternate line is hindsight accounting and not a forecast. Rows without a scrimmage state carry the last alternate value. The final row is 100% or 0% for a decided margin and unknown for a tie, because overtime is not modeled.

A drive result belongs to the team named in the edit. The dashboard defaults to the team that ran the play. The API defaults to the team with the ball in the entered state and warns when that differs, because on a drive's last play it is the next team, whose real drive still happens. In an overtime game the alternate stays regulation-only: `final.actual_regulation` gives the matching actual score and the last row ends at the decided alternate result. A play with no later snap in its half starts from its own pre-snap state and says so. At the edit point the chart's actual line reads the same post-play state as the card, so the gap is the same number.

### Stacked what-ifs

A chain continues from an alternate state with states you author. Each node `n` has a state `s_n`, chosen by you, and an optional drive result. After a scoring result the next prefill is the other team's first down at `s_kick` with the points on the board. After no score it is the other team's ball at the same spot. Every node reads the same pinned models:

\[
WP(s_n),\ WP_{Vegas}(s_n),\ EP(s_n),\qquad swing_n = 100\,(WP(s_n)-WP(s_{n-1})).
\]

A step's clock cannot run backward, a score cannot fall below the previous node plus its drive result, timeouts stay in range, and quarters 1-2 and 3-4 keep the half's second-half receiver. Overtime is refused. Nothing is computed for the chance of reaching a node: steps are choices, not forecasts, and swings are not added. A zero-change branch reads exactly the state it came from.

There are two endings. **Prediction** (the default) ends at the last node, has no final score, and reads as "the model's win chance if the game got here". **Rejoin** holds recorded plays from a later play, with scores shifted by the difference at that point, and is labeled hindsight. The two never mix. Chains are left out of the disputed-calls report and are never posted.

The disputed-calls report sums points swings as `home_sum_at_moment_expected_points`. That is a sum of at-the-moment expected points, not a final margin: expected points estimate the next score of the half, and later plays would have changed. Win-chance swings are never added.

## 10. Opportunity: downs in hand and next-score chances

A flag or edit shows, for the team with the ball before the snap,

\[
downs(s)=5-d(s)\text{ with the ball, else }0,
\]

and the EP model's next-score classes `p_TD`, `p_FG`, `p_none` from each state, in that team's frame. A reset to first down gains 0 downs on first, +1 on second, +2 on third and +3 on fourth. These are chances available. Plays run is the wrong count: it stops when a team scores, so a quick touchdown reads as less opportunity. Extra downs flip field goals into touchdowns. The classes are the next score of the half, not the result of this drive, and nothing here is fitted or added to the models.

Measured from 11 seasons of nflverse play-by-play (2016 to 2026 Week 8), with the scripts outside this repository:

- Inside the 5 at goal-to-go, TD chance at drive end rises with downs in hand at every yard: zero violations in 15 down pairs over the five seasons 2022 to 2026.
- Per-snap TD rate is flat by down: .51, .55, .53, .58 at the 1-2 and .33, .34, .39, .34 at the 3-5, so each down is close to a fresh chance.
- A defensive flag that resets to first on 3rd or 4th and goal inside the 5 (n=77): TD 88% vs 49% from the same state without a flag, FG 4% vs 31%. Realized gain +1.81 points (90% CI 1.28 to 2.26) against a model gain of +1.86 (gap −0.05, −0.57 to 0.41). The same flag on 1st down gains +0.28 (n=114) and on 2nd +0.79 (n=76).
- 1st & 15+ between the own 20 and the opponent 40 (n=2,250): realized minus model −0.03 (−0.23 to 0.16). The −0.51 seen in 2025-26 did not replicate (2016-24 +0.05, n=1,916).

So the EP model already prices the extra downs, and no adjustment is applied.

## 11. Heat: the game so far

Each game shows, for each team, the sum of native EPA on its last 5 scrimmage snaps (penalties included), big plays in its last 10 snaps (a run of 10+ yards or a completed pass of 20+ yards), and the current scoring run (points since the opponent last scored). There is no strength adjustment. This describes the game so far. It is not a forecast and feeds no value. Measured on 2025 and 2026 play-by-play (n=44,902): one standard deviation of last-5 EPA goes with +0.011 EPA on the next snap (90% CI .001 to .022), and the team's following 5 snaps "predict" the current one just as well (+.012), so it reflects game-level form rather than a streak.

## 12. The week headline

Each game's card leads with its **biggest call**: `max |I^WP|` over priced flags that took effect, signed to the team it helped, with the moment. Beside it is the count of **big calls**, priced flags of at least 5 pp, split by the team each helped. Counts add; swings never do. A single at-the-moment swing cannot say who would have won, and neither can the count.

The game page also gives the net of all priced flags in expected points, labeled as ignoring score. It isn't the headline because, measured in the 2026-11-03 audit, three-score flags are 10.5% of its absolute total and 1.4% of absolute win-chance swing. Flags without a price are counted beside these figures, never as zero.

## 13. Changing a play's outcome

Any recorded play can take a different result: a run, pass, kneel or spike; a flag-only row with a down; a punt, kickoff or field goal; an extra point or two-point try. Rows that aren't plays (timeouts, quarter ends, comments) are not listed.

A result becomes a post-play state from the row's pre-snap state `b`, by the rules in section 3. A gain `x` gives `y' = y - x`. If `x >= g` it is first down with `g' = min(10, y')`; otherwise `d' = d + 1` and `g' = g - x`. A failed fourth down hands the opponent first down at `100 - y'`. `y' <= 0` is a touchdown and `y' >= 100` a safety.

- **Scores** put the points on the board and give the other team the ball at the season's measured post-kick spot, with the extra point assumed good. A try's result is 0, 1 or 2 points on the same terms.
- **Punts and kickoffs** give the receiving team first down at the chosen spot, with touchbacks at the 20 on punts and at the season's measured spot on kickoffs. An onside recovery or muff goes to the kicking team.
- **A missed field goal** gives the opponent the spot of the kick (`y + 8`) or its own 20, whichever is better for it.
- **A safety** gives the defense 2 points and the ball at the season's measured mean spot after the kick: 58 yards from goal in 2024 (17 kicks) and 48 in 2025 (10 kicks). A season with under 10 kicks, such as 2026 (2 kicks), borrows the latest season with enough and is labeled approximate.
- **No flag** uses the flag's priced play-as-written branch from section 3, offered only where one exists.

Both branches use the next recorded snap's clock, because the time a different result would have taken is not recorded. With no later snap in the half, both read at the play's own clock and the page says so.

**The drive stops there.** The edited drive ends at that state and later drives stay exactly as recorded: no score is carried forward, no later play is re-read, and the recorded win-chance line is not redrawn. The comparison is `WP(yours) - WP(recorded next)`, in both models, along with the change in margin plus expected points. Each of these is an at-the-moment reading.

**Overtime.** Overtime plays can be edited. Win chance is not given for your version, because the port of upstream's overtime rule missed published win chance by over 1 pp on 215 of 368 stored overtime rows (measured; maximum 47 pp), so it isn't used. The expected-points classes match published on 349 of those rows, so those and the chances in section 14 are given.

**Play and drive changes.** A play's change runs from its win chance to the next listed play's, so a game's changes add up exactly. Native `home_wp_post` is not used for this: on `no_play` rows it disagrees with the next row by more than 0.1 pp on 131 of 2,665 rows in 2024, by up to 35 pp, and on no other row kind (measured). Flag dots show the ruling's swing against its alternative, a different baseline, and the tooltip shows both. A drive's change runs from its first scrimmage or penalty row to its last assigned event, and the changes across drives don't add to the game's because a kickoff return can fall between them. Vegas win chance is shown beside the main model everywhere. Its Brier score on run and pass rows is lower, .137 in 2024 and .168 in 2026 against .157 and .179 (measured; a tie counts as .5).

## 14. Chance of a first down and of scoring

**Fit.** Tables and coefficients come from `scripts/fit_conversion`, on 2024, 2025 and 2026 weeks 1-8. It is rolling origin: fit 2024 and score 2025, then fit 2024-25 and score 2026. The second fit ships; every number below that says "later seasons" comes from those two scores. `docs/conversion-gates.json` holds them all.

**Target.** On downs 1 to 3 the chance is that this series earns a new first down or touchdown (nflverse `series_success`). Series cut off by the half or ended by kneeling are left out, since they are censored and not failures. A smaller line gives the chance this snap converts. On 4th down the series is mostly a kicking decision, so the headline is "if they go for it", on run and pass rows only, beside the league's go rate. Teams that go chose to, so that is the rate for teams like them, not for a forced attempt.

**Model.** With `p0` the league rate from a table by down, 14 to-go buckets and 6 field zones (m-estimate toward its parent, `k = 50`, with `n` shown):

\[
\text{logit } p = \text{logit } p_0 + \alpha + \beta_O O_P + \beta_D D_Q + \beta_M M_P + \beta_R M_Q
\]

`O` and `D` are each team's shrunken EPA per play over its last 4 games before this one, in league SDs, with good positive. Byes are skipped, this game is excluded, second-half rows outside 5-95% win chance are dropped, and values are centered on the season. Shrinkage is `(n m + k r X_prev) / (n + k)`, where `X_prev` is the team's previous season, `r` its measured year-to-year correlation (.39 for both) and `k` = 277 plays, measured from play and team-season variance.

`M_P` is the offense's native EPA over its last 5 scrimmage snaps before this play, in SDs. `M_Q` is the same for the other team's offense. A term for what this defense allowed recently cannot be separate: in one game a defense has only faced this offense, so it equals `-M_P` exactly (measured on all rows).

The listed changes apply each term in turn, so they add up to the chance.

**Fitted (shipped, series target; 90% intervals from a game-cluster bootstrap).** Per SD in log-odds: offense +.095 (.075 to .119), defense −.065 (−.086 to −.042), momentum +.039 (.014 to .059), other offense's momentum +.038 (.019 to .058).

**Results on later seasons.**

| target | lift in log-loss, 2025 | lift, 2026 | ECE | max decile gap | team terms applied |
|---|---:|---:|---:|---:|---|
| Series | −.0017 | −.0022 | .006 | .018 | yes |
| This snap | −.0008 | −.0008 | .006 | .014 | yes |
| This drive | −.0045 | −.0018 | .020 | .062 | yes |
| 4th down, go | +.0020 | −.0045 | .035 | .064 | no |

Team terms apply when they improve log-loss on both later seasons, point the right way with intervals clear of zero, and are no less calibrated than the table alone. The advisor's stricter bar of −.002 on both seasons was set for a nine-season fit and is missed narrowly on series; it is reported here. The pass and run matchup variant lost on 2025 and is not used. Fourth-down team terms lost on 2025, so the 4th-down reading is the table alone. This-drive chances run about 2 pp off on average and up to 6 pp at the low end, where predicted .17 was observed .11, and are less reliable in blowouts.

**Momentum.** The own-offense term is about 1.0 pp per SD (0.4 to 1.5). A placebo using the same team's snaps after this drive ends comes out larger (+.066, .041 to .092) and those snaps cannot cause this one, so it reads as how the game is going, not a streak. It stays in at its fitted size, whatever that size says, and the page prints it. Dropping both momentum terms costs .0001 to .0006 in log-loss for series and snap on later seasons. The this-drive model carries a larger term, +.157 per SD (.129 to .179), and dropping it costs .0011 to .0022. That term reads the current drive's own snaps too, so it says even less about a streak.

**Team words.** "Top 5" and "bottom 5" come from ranking every team over its own last 4 games on ten metrics, with minimum sample sizes. They describe the inputs. The number that moves the chance is the offense or defense term.

**Ways to measure scoring**, all for the team with the ball in your version:

- *Next score is a touchdown or field goal*: the pinned EP model's classes. It spans any number of drives in the half.
- *This drive scores, league history*: share of rows from the same cell with `fixed_drive_result` of Touchdown or Field goal, separated by whether over 5 minutes remain in the half. An end-of-half drive counts as no score, so late readings are labeled.
- *This drive scores, these teams*: the same logistic form with this rate as its offset.
- *Expected points*: the pinned EP value, in points, net of the opponent's next score.
- *Finishing drives*: over the offense's last 4 games, scoring rate minus the league rate from each drive's starting state. Raw scoring rate would mix in field position.

Next-score and this-drive touchdown chances agree near the goal line and part with distance, as they should, since the first spans later possessions. Measured on 2024-25: at 1-10 yards .66 and .66; at 41-60 yards .37 and .28; at 81-99 yards .25 and .16.
