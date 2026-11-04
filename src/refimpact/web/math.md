# Ref Impact: mathematical definitions

Version 0.4. Every quantity below is descriptive or a conditional model comparison.
The pipeline uses the pinned open-source nflfastR/fastrmodels models documented in
`UPSTREAM.md`. We do not fit a model or convert EPA into win probability with a constant.
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
A return requires an unambiguous written endpoint. Unsupported touchdowns, other
turnovers, ambiguous gains, missing spots and missing states remain unpriced. A
wholly declined flag has exactly zero immediate spatial enforcement effect at the
common clock, but its acceptance choice and clock-stoppage effects can differ.

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
known-effects penalty sum, split into home-on-offense and home-on-defense subtotals.
If no estimate is known the ledger is unknown. Known zero-valued declined flags do
count as known observations. Coverage is `known events / reported events`.

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

Computations retain full precision; labels round EPA to the displayed decimal places
and win chance/WPA to the displayed percentage precision. Ranking penalties uses the
largest absolute known ruling/choice WP swing (pp), with absolute EPA only breaking
ties; points and percentage points are never compared as the same unit.

The scoreboard's decorative EPA-track marker is `clip(50 + 7*net_home_epa, 4, 96)` in
CSS percent coordinates. It is not win probability, a relative percentage benefit,
or a new statistic. History and scatter charts label their numerical units explicitly.

The original bot's optional queue filter keeps eligible legacy comparisons when
`abs(wp_swing_pp) >= min_swing`. Those four dead-ball types have stricter reconstruction
and timing coverage than the expanded dashboard ledger. Queue selection is not a
scientific significance threshold or a call-correctness decision. A transform never
queues or publishes tweets.

## What if? timelines

An edit replaces the state recorded after a play. At the moment, `win-chance swing = 100 * (WP(entered) - WP(recorded next))` and `points swing = (margin + EP)(entered) - (margin + EP)(recorded next)`, both on the same fixed-team model.

A drive result then ends the edited drive with `home score at rejoin = entered home score + points if home has the ball`, and likewise for away. Every later row uses its recorded pre-snap state with `score shift = alternate score at rejoin - recorded score at the end of that drive`. Nothing else about later rows changes, so the alternate line is hindsight accounting and not a forecast. Rows without a scrimmage state carry the last alternate value. The final row is 100% or 0% for a decided margin and unknown for a tie, because overtime is not modeled.

The disputed-calls range adds points swings as an approximation, from zero (none wrong) to all of them (all wrong). Win-chance swings are never added.

