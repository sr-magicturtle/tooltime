# Penalty optimization before submission

The graded penalty formula and physical safety rules are unchanged. The work
improves construction, search efficiency, and selection of validated schedules.
The official challenge validator is not supplied; all validation described here
uses the project's independent CSV validator and the engine's stricter checks.

## Paths evaluated

1. **Better starting schedules — implemented.** Try a bounded portfolio of
   priority, slack, dependency and sharing orderings. For B, try schedules that
   stay within nominal supply before buying extra access. For C, evaluate
   selected two-week ECLO windows per line. Keep the original heuristic as a
   candidate so a new construction cannot replace it with a worse valid plan.
2. **Use spare capacity before paying for extra nights — implemented for B.**
   Move complete accesses within their readiness, dependency and deadline
   bounds, checking crews, sharing and Live closures. This polishing step has
   a shared limit of 50,000 trials across two candidates and three passes;
   large instances skip it. All resulting schedules are checked again.
3. **Spend search on the graded objective — implemented.** The previous
   secondary early-completion objective spent time rearranging work whose
   penalty was already zero. CP-SAT now minimizes the published penalty alone.
   A complete feasible hint includes derived occupancy and cost variables.
4. **Shrink equivalent search work — implemented.** A checked incumbent sets
   a maximum penalty and bounds each activity's affordable delay. Locations
   with identical occupants share model variables. Every physical location
   still contributes its own excess-night cost, including when capacities differ.
5. **Preserve improvements — implemented.** Reruns receive a rechecked incumbent
   as a search hint/bound. The app retains the better independently scored plan
   only for matching data, scenario, disruptions and overrides. Cached scores
   are not trusted. Failed or stale reruns cannot replace a valid best plan.
6. **Correct misleading search paths — implemented.** Negotiated extra access
   no longer increases nominal supply or discounts its penalty. C retains its
   one-extra-night limit. Fallbacks and negotiation must satisfy accepted
   decisions; a deadline before readiness cannot move work before its start.
7. **Relax Live exclusions or global possession-night consistency — deferred.**
   This would change the physical interpretation used by the current validators.
   Score gains obtained by weakening those checks would not establish a better
   compliant submission.

Installed packages also take precedence over a copied `.deps` folder, preventing
native wheels from another platform from silently disabling the optimizer.

## Measured results

Baseline: original engine at commit `f6284ac`. Both versions used Python 3.12.14,
OR-Tools 9.15.6755, seed 11, eight search workers and a six-second CP-SAT budget.
Construction and validation are additional wall time. The same public CSVs and
fixed-seed mutations were used; none of the source datasets was changed.

| Dataset | A before → after | B before → after | C before → after |
|---|---:|---:|---:|
| Public | 32.2 → 32.2 | 30 → 30 | 26.1 → 26.1 |
| Reduced supply, seed 3 | 32.2 → 32.2 | 30 → 30 | **74.9 → 26.1** |
| Increased workload, seed 4 | 53.2 → 53.2 | 50 → 50 | **57.4 → 47.1** |
| Combined pressure, seed 7 | 907.9 → 907.9 | No feasible result → same | **489 → 77.9** |

All 11 previously feasible cases remained feasible, with complete workload and
no hard violations in either checker. The remaining B case has no valid penalty
score in either run. These are measured results, not a guarantee over the judges'
hidden instances: parallel time-limited search can vary between runs.

The already-saved public submission penalties were 32.2 / 30 / 26.1, so their
CSV files did not need replacing. The new engine also reproduces these scores
without CP-SAT: the deterministic starting plans improve from **70.7 / 163 /
70.7** to **32.2 / 30 / 26.1**. On the increased-workload B fixture, polishing
improves the starting penalty from 225 to 57; CP-SAT reaches 50.

Machine-readable measurements are in `outputs/optimization_before.json` and
`outputs/optimization_after.json`. The final public scores are proven optimal
for the encoded CP-SAT model; that claim does not extend to interpretations
outside its documented conservative possession-night model.

## Reproduce checks

With the project's Python environment:

```text
python -m pip install -r requirements-test.txt
python -m unittest discover -s tooltime -t . -p "test_*.py"
python -m tooltime.benchmark --seconds 6 --output outputs/benchmark.json
```

`requirements-test.txt` adds the dependency needed by the existing synthetic
duration demonstration tests. The production app still needs only the packages
in `requirements.txt`.

Regression coverage includes exact penalty arithmetic, ECLO windows, distinct
location capacities, negotiated excess, readiness dates, invalid fallback
decisions, incumbent integrity, concurrent input changes and refused overrides.
The HTTP tests now exercise the current Session API, including independently
checked scenario CSV exports and upload/solve behavior.
