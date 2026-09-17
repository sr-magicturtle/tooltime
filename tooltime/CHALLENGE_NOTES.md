# PS1 implementation notes

## Source dataset

The supplied files contain 54 activities, 14 contracts and 192 required access units. The horizon begins Monday 2027-01-04 and lasts 30 weeks. There are two lines, 20 station-on-line records (18 distinct station IDs because H01/H02 occur on both lines), 18 sector records and 76 bound-specific locations. Six activities declare predecessors. Weekly supply has no week field: the flat capacity applies each week. Location capacities are 1 (12 locations), 2 (40), or 4 (24).

## Data and semantics traps

- Key stations by `(line_code, station_id)`. Station sequence resets for each line; sector sequence does not (Beta starts at 10).
- Export every traversed tunnel and platform, including both endpoints. Exclusion closures are a derived safety footprint, not productive work.
- `access_night` is a local index within contract + activity type + week. It is not a network-wide calendar date. `co_share_group` is location/week specific in the published schema; the sample assigns different groups at different locations for one activity/week. A model that makes these groups globally consistent is conservative and should document that choice.
- An activity receives at most one access per week. Standard yield is 1, ECLO yield 1.5. Do not drop activities or partial workload to improve scores.
- Only Live mirrors opposite-bound closures and crosses to the other line at H01/H02. Non-live interchange footprints remain line-specific. The interchange tunnels are not a shared physical track.
- Live has a two-sector buffer, Consist one, Others none. Buffers must clear spans and other buffers unless the activities share a legal possession. Co-sharing legality is PM alone, PC with up to three C, or up to four C; two PC are not legal.
- A week ends Sunday; sample completion dates follow `horizon_start + 7 * week - 1 day`.
- Use explicit score formulas: priority band 100/10/1 times activity modifier 1.3/1.2/1.0; excess access cost 7; ECLO cost 5. Adjacent explanatory prose and ratio columns contain inconsistent comparisons.
- Predecessors are present in data but their precise timing is not specified in the rigid-rule prose. Requiring successor start in a later week than predecessor completion is a conservative disclosed interpretation.

## Acceptance checks

1. Every activity appears and total yield reaches its demand; no duplicate activity/week.
2. All activities respect planned start, dependencies, weekly contract caps, and workfront limits.
3. Every work span expands to the expected bound-specific sectors and endpoint/intermediate platforms.
4. PM cannot share; PC+PC and any group larger than four fail.
5. A Live job excludes opposite-bound work and affects both lines at the interchange; an otherwise identical non-live job does not cross lines.
6. A buffer overlapping another buffer or span fails unless an explicitly legal common possession permits sharing.
7. Scenario A has no ECLO or supply excess. B has no completion overrun. C has at most one excess slot per location/week, and ECLO on each affected line is confined to one span of at most two consecutive weeks.
8. Export schemas exactly match the three published CSV headers, with a separate RESULTS file per scenario.
9. A disruption rerun preserves all work and reports moved work. An approval belongs to one plan revision and is invalidated by a changed plan.

## Honest prototype boundaries

The official validator is not included. Report internal validation, never official certification or proven global optimality. Source data does not include historical task durations, actual night times, inventory, competency rosters, plant availability, or jobs' rollback times. Any hourly demonstration, quantile training corpus, readiness record, or agent concession using these fields must be identified as synthetic/demo input. A trained model on synthetic records proves the integration, not real-world calibration. No p90 is a guaranteed finish time.

The user's illustrative cable/grinding bundle must retain physical separation or an explicit compatibility rule. Sharing isolation alone does not authorize overlapping unsafe work. Cable p90 of 3.1 hours plus 45 minutes rollback needs 231 minutes after readiness and before first-train handback; sharing setup only helps if the resulting window genuinely meets that inequality. The 41% to 68% example is not a result derived from the PS1 data.

At a decision time, the latest safe commit time is handback deadline minus remaining setup, p90 work and rollback. The rollback trigger during active work is handback deadline minus rollback. These are different clocks and should not be conflated.
