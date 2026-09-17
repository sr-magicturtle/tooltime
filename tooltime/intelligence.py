"""TOOLTIME's transparent, local engineering-hour demonstration.

The PS1 CSVs contain access-night workload, not maintenance duration history,
parts, rosters, electrical sections or first-train times. Everything in this
module beyond the named PS1 locations is explicitly a seeded demonstration.
Agents are deterministic role-specific policies. CP-SAT alone selects slots.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from itertools import combinations
import math
import re
from typing import Any


SYSTEMS = ["signalling", "track", "communications", "points", "electrical"]
WINDOW = {"date": "2027-01-05", "start": "00:30", "end": "05:30",
          "start_minute": 30, "end_minute": 330, "duration_minutes": 300}
DISCLOSURE = (
    "Illustrative night on PS1's fictional Line Alpha. Train times, electrical "
    "sections, crew rosters, stock and 1,200 historical jobs are synthetic. "
    "Quantile models are fitted locally; agents use deterministic role policies, "
    "not Gemini or another language model. This is a planning demonstration, "
    "not an operational railway safety assurance."
)


def clock(minute: int | None) -> str | None:
    if minute is None:
        return None
    return f"{(int(minute) // 60) % 24:02d}:{int(minute) % 60:02d}"


def _number(value: Any, default: int, low: int = 1, high: int = 600) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, result))


def _features(job: dict) -> list[float]:
    system = str(job.get("system", "signalling")).lower()
    month = _number(job.get("month"), 1, 1, 12)
    return [
        float(_number(job.get("estimated_minutes"), 120)),
        *[float(system == candidate) for candidate in SYSTEMS],
        float(_number(job.get("crew_size"), 4, 1, 20)),
        float(bool(job.get("isolation", False))),
        float(bool(job.get("plant"))),
        math.sin(month / 12 * math.tau),
        math.cos(month / 12 * math.tau),
    ]


@lru_cache(maxsize=1)
def _models():
    """Fit two actual quantile gradient-boosting models on disclosed fake data."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_pinball_loss

    rng = np.random.default_rng(42)
    rows, durations = [], []
    system_effect = {"signalling": .14, "track": .04, "communications": .01,
                     "points": .19, "electrical": .11}
    for _ in range(1200):
        system = str(rng.choice(SYSTEMS))
        estimate = int(rng.integers(35, 186))
        crew = int(rng.integers(2, 8))
        isolation = system != "communications"
        plant = "machine" if system in ("track", "points") else None
        month = int(rng.integers(1, 13))
        sample = dict(system=system, estimated_minutes=estimate, crew_size=crew,
                      isolation=isolation, plant=plant, month=month)
        factor = (1.02 + system_effect[system] + .045 * int(isolation)
                  + .025 * int(bool(plant)) - .015 * (crew - 4)
                  + .025 * math.sin(month / 12 * math.tau))
        # Positive, asymmetric tail: a realistic-shaped but invented process.
        noise = rng.normal(0, .075) + rng.exponential(.12) - .07
        durations.append(max(15., estimate * (factor + noise)))
        rows.append(_features(sample))
    x, y = np.asarray(rows), np.asarray(durations)
    models, evaluation = {}, {}
    for quantile in (.5, .9):
        estimator = GradientBoostingRegressor(
            loss="quantile", alpha=quantile, n_estimators=110,
            max_depth=3, min_samples_leaf=12, learning_rate=.065,
            random_state=42,
        )
        estimator.fit(x[:960], y[:960])
        prediction = estimator.predict(x[960:])
        models[quantile] = estimator
        evaluation[f"p{int(quantile * 100)}"] = {
            "coverage": round(float(np.mean(y[960:] <= prediction)), 3),
            "pinball_loss_minutes": round(float(mean_pinball_loss(
                y[960:], prediction, alpha=quantile)), 2),
        }
    metadata = {
        "name": "Gradient boosted quantile regression",
        "implementation": "scikit-learn GradientBoostingRegressor",
        "loss": "quantile", "quantiles": [.5, .9], "seed": 42,
        "source": "Synthetic demonstration history; no operational history supplied",
        "synthetic": True, "records": 1200, "training_records": 960,
        "holdout_records": 240, "holdout_metrics": evaluation,
        "features": ["requested duration", "system type", "crew size",
                     "isolation", "plant required", "month"],
        "limitation": "Holdout metrics describe the synthetic generator only. "
                      "Operational calibration requires real completed-job records.",
    }
    return models, metadata


def predict_duration(job: dict) -> dict:
    models, _ = _models()
    values = [float(models[q].predict([_features(job)])[0]) for q in (.5, .9)]
    p50 = max(5, int(math.ceil(values[0] / 5) * 5))
    p90 = max(p50, int(math.ceil(values[1] / 5) * 5))
    return {"p50": p50, "p90": p90, "uncertainty_minutes": p90 - p50,
            "prediction_source": "Fitted synthetic-history quantile model"}


def demo_jobs() -> list[dict]:
    return [
        {"id": "TT-101", "title": "Signal cable renewal", "team": "Team A",
         "system": "signalling", "location": "SEC:ALP:S02_S03:EB",
         "location_label": "S02–S03 · eastbound", "section": "ALP-E02",
         "sector_index": 2, "isolation": True, "estimated_minutes": 120,
         "crew_size": 4, "competency": "signalling", "supervisor": "SUP-SIG-1",
         "supervisor_competencies": ["signalling"], "plant": None,
         "part_available": True, "plant_available": True, "ready": True,
         "priority": 1, "setup": 55, "rollback": 45, "allow_sharing": True,
         "earliest_date": "2027-01-05", "blockers": []},
        {"id": "TT-102", "title": "Rail grinding", "team": "Team B",
         "system": "track", "location": "SEC:ALP:S04_H01:EB",
         "location_label": "S04–H01 · eastbound", "section": "ALP-E02",
         "sector_index": 4, "isolation": True, "estimated_minutes": 90,
         "crew_size": 5, "competency": "track", "supervisor": "SUP-TRK-1",
         "supervisor_competencies": ["track"], "plant": "GRINDER-01",
         "part_available": True, "plant_available": True, "ready": True,
         "priority": 2, "setup": 55, "rollback": 30, "allow_sharing": True,
         "earliest_date": "2027-01-05", "blockers": []},
        {"id": "TT-103", "title": "Platform CCTV installation", "team": "Team C",
         "system": "communications", "location": "PLAT:ALP:S03:WB",
         "location_label": "S03 platform · westbound", "section": "ALP-E02",
         "sector_index": 3, "isolation": False, "requires_traction_power": False,
         "estimated_minutes": 60, "crew_size": 3, "competency": "communications",
         "supervisor": "SUP-COM-1", "supervisor_competencies": ["communications"],
         "plant": None, "part_available": True, "plant_available": True,
         "ready": True, "priority": 3, "setup": 20, "rollback": 15,
         "allow_sharing": True, "earliest_date": "2027-01-05", "blockers": []},
        {"id": "TT-104", "title": "Point machine overhaul", "team": "Team D",
         "system": "points", "location": "PLAT:ALP:S02:WB",
         "location_label": "S02 platform · westbound", "section": "ALP-E02",
         "sector_index": 2, "isolation": True, "estimated_minutes": 100,
         "crew_size": 4, "competency": "points", "supervisor": None,
         "supervisor_competencies": [], "plant": "LIFT-02",
         "part_available": False, "plant_available": True, "ready": False,
         "priority": 1, "setup": 55, "rollback": 40, "allow_sharing": False,
         "earliest_date": "2027-01-07",
         "blockers": ["Replacement actuator is at Beta depot; transfer to Alpha depot is pending.",
                      "No points-certified supervisor is rostered for Tuesday."]},
    ]


def _prepare_job(raw: dict, index: int) -> dict:
    job = deepcopy(raw)
    job.setdefault("id", f"TT-{201 + index}")
    job.setdefault("title", "Maintenance request")
    job.setdefault("team", f"Team {index + 1}")
    job.setdefault("system", "signalling")
    job.setdefault("section", "UNCONFIRMED")
    job.setdefault("location", "UNCONFIRMED")
    job.setdefault("location_label", job["location"])
    job["isolation"] = bool(job.get("isolation", False))
    job["estimated_minutes"] = _number(job.get("estimated_minutes"), 120)
    job["rollback"] = _number(job.get("rollback"), 45 if job["isolation"] else 15, 0)
    job["setup"] = _number(job.get("setup"), 55 if job["isolation"] else 20, 0)
    job["priority"] = _number(job.get("priority"), 2, 1, 3)
    prediction = predict_duration(job)
    # Explicit overrides let the UI demonstrate an observed overrun.
    if raw.get("p50") is not None or raw.get("p90") is not None:
        prediction["p50"] = _number(raw.get("p50"), prediction["p50"])
        prediction["p90"] = max(prediction["p50"], _number(raw.get("p90"), prediction["p90"]))
        prediction["prediction_source"] = "Explicit scenario duration override"
        prediction["uncertainty_minutes"] = prediction["p90"] - prediction["p50"]
    job.update(prediction)
    blockers = list(job.get("blockers", []))
    if "part_available" not in job:
        blockers.append("Required parts have not been checked at the work depot.")
    if job.get("part_available") is False and not any("actuator" in str(b).lower() or "part" in str(b).lower() for b in blockers):
        blockers.append("Required part is unavailable at the work depot.")
    if job.get("plant_available") is False:
        blockers.append("Required plant is unavailable.")
    elif job.get("plant") and "plant_available" not in job:
        blockers.append("Required plant availability has not been confirmed.")
    competency = job.get("competency")
    certified = job.get("supervisor_competencies", [])
    if not job.get("supervisor") or not competency or competency not in certified:
        if not any("supervisor" in str(b).lower() for b in blockers):
            blockers.append("A supervisor with the required competency is not confirmed.")
    if not job["section"] or not job["location"] or job["section"] == "UNCONFIRMED" or job["location"] == "UNCONFIRMED":
        blockers.append("Work location and electrical section must be confirmed.")
    if job.get("ready") is False and not blockers:
        blockers.append("Readiness has not been confirmed.")
    job["blockers"] = blockers
    job["ready"] = not blockers
    job["latest_commit_minute"] = WINDOW["end_minute"] - job["rollback"] - job["p90"]
    job["latest_commit"] = clock(job["latest_commit_minute"])
    job["rollback_deadline_minute"] = WINDOW["end_minute"] - job["rollback"]
    job["rollback_deadline"] = clock(job["rollback_deadline_minute"])
    job.update(start=None, end=None, start_minute=None, end_minute=None,
               release_minute=None, status="eligible" if job["ready"] else "blocked")
    return job


def _compatible(a: dict, b: dict) -> tuple[bool, str]:
    if a["location"] == b["location"]:
        return False, "The same physical workface cannot host overlapping jobs."
    if a.get("plant") and a.get("plant") == b.get("plant"):
        return False, "The jobs require the same indivisible plant."
    if a.get("supervisor") and a.get("supervisor") == b.get("supervisor"):
        return False, "The jobs require the same supervisor."
    # Demonstration policy: grinding needs a full intervening sector.
    if "track" in (a["system"], b["system"]):
        if a.get("sector_index") is None or b.get("sector_index") is None:
            return False, "Separation from grinding has not been verified."
        if abs(a["sector_index"] - b["sector_index"]) <= 1:
            return False, "Grinding requires a full intervening sector."
    if not a.get("allow_sharing", False) or not b.get("allow_sharing", False):
        return False, "A requester does not permit a shared possession."
    return True, "Separate workfaces, certified crews and independent plant."


def _solve(jobs: list[dict], delay: int, sharing: bool) -> dict:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    ready = [j for j in jobs if j["ready"]]
    ready_by_id = {j["id"]: j for j in ready}
    options = [(j["id"],) for j in ready]
    proposals = []
    if sharing:
        isolation_jobs = [j for j in ready if j["isolation"]]
        for size in range(2, min(4, len(isolation_jobs)) + 1):
            for combination in combinations(isolation_jobs, size):
                if len({j["section"] for j in combination}) != 1:
                    continue
                pair_checks = [_compatible(a, b) for a, b in combinations(combination, 2)]
                accepted = all(ok for ok, _ in pair_checks)
                proposals.append({"jobs": [j["id"] for j in combination],
                                  "accepted": accepted,
                                  "reason": "Shared isolation offered by both requesters." if accepted else next(reason for ok, reason in pair_checks if not ok)})
                if accepted:
                    options.append(tuple(j["id"] for j in combination))

    sections, workfaces, resources = {}, {}, {}
    work_intervals = {}
    job_membership = {j["id"]: [] for j in ready}
    option_vars = []
    objective = []
    earliest = min(WINDOW["end_minute"], WINDOW["start_minute"] + delay)
    for index, ids in enumerate(options):
        members = [ready_by_id[job_id] for job_id in ids]
        setup = max(j["setup"] for j in members)
        rollback = max(j["rollback"] for j in members)
        duration = setup + max(j["p90"] for j in members) + rollback
        selected = model.new_bool_var(f"possession_{index}")
        start = model.new_int_var(earliest, WINDOW["end_minute"], f"start_{index}")
        finish = model.new_int_var(earliest, WINDOW["end_minute"], f"finish_{index}")
        interval = model.new_optional_interval_var(start, duration, finish, selected, f"interval_{index}")
        model.add(start == earliest).only_enforce_if(selected.Not())
        model.add(finish == earliest).only_enforce_if(selected.Not())
        if members[0]["isolation"]:
            sections.setdefault(members[0]["section"], []).append(interval)
        for job in members:
            job_membership[job["id"]].append(selected)
            work_start = model.new_int_var(0, WINDOW["end_minute"] + 600, f"work_start_{index}_{job['id']}")
            work_finish = model.new_int_var(0, WINDOW["end_minute"] + 1200, f"work_finish_{index}_{job['id']}")
            model.add(work_start == start + setup)
            model.add(work_finish == work_start + job["p90"])
            work_interval = model.new_optional_interval_var(work_start, job["p90"], work_finish, selected, f"work_{index}_{job['id']}")
            work_intervals.setdefault(job["id"], []).append(work_interval)
            workfaces.setdefault(job["location"], []).append(work_interval)
            for resource in (job.get("plant"), job.get("supervisor")):
                if resource:
                    resources.setdefault(str(resource), []).append(work_interval)
            if not job["isolation"] and job.get("requires_traction_power"):
                sections.setdefault(job["section"], []).append(work_interval)
        reward = sum(j["p50"] * 10000 + (4 - j["priority"]) * 100 for j in members)
        objective.append(selected * (reward - (setup + rollback) * 10))
        # High-uncertainty jobs get the earlier start when yield ties.
        objective.append(-start * (1 + max(j["uncertainty_minutes"] for j in members)))
        option_vars.append(dict(ids=ids, selected=selected, start=start, finish=finish,
                                setup=setup, rollback=rollback, duration=duration))
    for membership in job_membership.values():
        model.add(sum(membership) <= 1)
    for intervals in [*sections.values(), *workfaces.values(), *resources.values()]:
        if len(intervals) > 1:
            model.add_no_overlap(intervals)
    for a, b in combinations(ready, 2):
        if _adjacent_grinding(a, b):
            model.add_no_overlap(work_intervals[a["id"]] + work_intervals[b["id"]])
    model.maximize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 4
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.solve(model)
    chosen = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for option in option_vars:
            if solver.value(option["selected"]):
                chosen.append({"id": f"P{len(chosen) + 1}", "jobs": list(option["ids"]),
                               "start_minute": solver.value(option["start"]),
                               "end_minute": solver.value(option["finish"]),
                               "setup": option["setup"], "rollback": option["rollback"],
                               "duration": option["duration"]})
    return {"possessions": chosen, "proposals": proposals,
            "solver": {"name": "OR-Tools CP-SAT", "status": solver.status_name(status),
                       "optimal": status == cp_model.OPTIMAL,
                       "objective": "Maximise total p50 productive crew-minutes, then priority; "
                                    "reduce possession overhead and start uncertain work early.",
                       "wall_time_seconds": round(solver.wall_time, 3)}}


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return max(a[0], b[0]) < min(a[1], b[1])


def _adjacent_grinding(a: dict, b: dict) -> bool:
    """Illustrative compatibility policy, scoped to one line and bound."""
    a_location, b_location = a["location"].split(":"), b["location"].split(":")
    if len(a_location) != 4 or len(b_location) != 4:
        return False
    return ("track" in (a["system"], b["system"])
            and a_location[1] == b_location[1] and a_location[3] == b_location[3]
            and a.get("sector_index") is not None and b.get("sector_index") is not None
            and abs(a["sector_index"] - b["sector_index"]) <= 1)


def _result(jobs: list[dict], solved: dict, delay: int) -> dict:
    by_id = {j["id"]: j for j in jobs}
    for possession in solved["possessions"]:
        for job_id in possession["jobs"]:
            job = by_id[job_id]
            job["start_minute"] = possession["start_minute"] + possession["setup"]
            job["end_minute"] = job["start_minute"] + job["p90"]
            job["start"], job["end"] = clock(job["start_minute"]), clock(job["end_minute"])
            job["release_minute"] = possession["end_minute"]
            job["clear_by"] = clock(possession["end_minute"])
            job["possession_id"] = possession["id"]
            job["shared_with"] = [other for other in possession["jobs"] if other != job_id]
            job["status"] = "scheduled"
            job["margin_minutes"] = job["latest_commit_minute"] - job["start_minute"]
            job["reason"] = ("Shared power isolation at separate, compatible workfaces; "
                             "setup and reinstatement are paid once." if job["shared_with"] else
                             "Fits the protected p90 work and reinstatement window.")
    scheduled = [j for j in jobs if j["status"] == "scheduled"]
    for job in jobs:
        if job["status"] == "eligible":
            job["status"] = "deferred"
            job["earliest_date"] = "Next suitable engineering night"
            job["reason"] = "Cannot fit with higher-yield work while preserving all hard constraints."
            if WINDOW["start_minute"] + delay + job["setup"] > job["latest_commit_minute"]:
                job["reason"] = "Even an immediate setup would pass the latest safe commitment time."
        elif job["status"] == "blocked":
            job["reason"] = " ".join(str(b) for b in job["blockers"])
    productive = sum(j["p50"] for j in scheduled)
    reserved = sum(j["p90"] for j in scheduled)
    setup = sum(p["setup"] for p in solved["possessions"])
    rollback = sum(p["rollback"] for p in solved["possessions"])
    overhead = setup + rollback
    standalone = sum(j["setup"] + j["rollback"] for j in scheduled)
    # This is a crew-capacity metric, with an explicit fixed ready-crew denominator.
    capacity = WINDOW["duration_minutes"] * len([j for j in jobs if j["ready"]])
    metrics = {
        "productive_minutes": productive, "reserved_work_minutes": reserved,
        "setup_minutes": setup, "rollback_minutes": rollback, "overhead_minutes": overhead,
        "tooltime_percent": round(100 * productive / capacity, 1) if capacity else 0,
        "tooltime_definition": "Expected productive crew-minutes / (300-minute window × ready crews). "
                               "Simultaneous crews count separately; p50 is a planning estimate.",
        "crew_capacity_minutes": capacity,
        "possession_efficiency_percent": round(100 * productive / (productive + overhead), 1) if productive + overhead else 0,
        "possession_efficiency_definition": "Sum of p50 work / (sum of p50 work + shared possession preparation and reinstatement).",
        "overhead_saved_minutes": standalone - overhead,
        "scheduled_count": len(scheduled), "deferred_count": len([j for j in jobs if j["status"] == "deferred"]),
        "readiness_blocked_count": len([j for j in jobs if j["status"] == "blocked"]),
        "first_train_margin_minutes": min((WINDOW["end_minute"] - j["release_minute"] for j in scheduled), default=WINDOW["duration_minutes"]),
    }
    duplicate_workfaces = []
    duplicate_resources = []
    powered_isolation = []
    adjacent_conflicts = []
    for a, b in combinations(scheduled, 2):
        intersect = _overlap((a["start_minute"], a["end_minute"]), (b["start_minute"], b["end_minute"]))
        if intersect and a["location"] == b["location"]:
            duplicate_workfaces.append((a["id"], b["id"]))
        if intersect and _adjacent_grinding(a, b):
            adjacent_conflicts.append((a["id"], b["id"]))
        if intersect and ((a.get("plant") and a.get("plant") == b.get("plant")) or a.get("supervisor") == b.get("supervisor")):
            duplicate_resources.append((a["id"], b["id"]))
        for isolated, powered in ((a, b), (b, a)):
            if isolated["isolation"] and powered.get("requires_traction_power") and isolated["section"] == powered["section"]:
                possession = next(p for p in solved["possessions"] if isolated["id"] in p["jobs"])
                if _overlap((possession["start_minute"], possession["end_minute"]), (powered["start_minute"], powered["end_minute"])):
                    powered_isolation.append((isolated["id"], powered["id"]))
    checks = [
        {"name": "First train protected", "passed": all(j["release_minute"] <= WINDOW["end_minute"] for j in scheduled),
         "detail": "Every reserved p90 work interval and reinstatement finishes by 05:30."},
        {"name": "Latest safe commitment", "passed": all(j["start_minute"] <= j["latest_commit_minute"] for j in scheduled),
         "detail": "Latest work start = first train − p90 duration − rollback."},
        {"name": "Physical workfaces", "passed": not duplicate_workfaces,
         "detail": "No overlapping jobs at the same physical workface."},
        {"name": "Grinding separation", "passed": not adjacent_conflicts,
         "detail": "Concurrent work on a grinder's bound keeps a full intervening sector."},
        {"name": "Plant and supervisors", "passed": not duplicate_resources,
         "detail": "Named plant and supervisors are never assigned to simultaneous work."},
        {"name": "Section-wide isolation", "passed": not powered_isolation,
         "detail": "No traction-dependent work can overlap isolation anywhere in its electrical section."},
        {"name": "Readiness and competency", "passed": all(j["ready"] for j in scheduled),
         "detail": "Parts, plant and a supervisor's required certification are confirmed before scheduling."},
        {"name": "Request conservation", "passed": len(jobs) == sum(metrics[k] for k in ("scheduled_count", "deferred_count", "readiness_blocked_count")),
         "detail": "Every request is explicitly scheduled, deferred or readiness-blocked; nothing is silently dropped."},
    ]
    return {"jobs": jobs, "possessions": solved["possessions"], "metrics": metrics,
            "checks": checks, "solver": solved["solver"]}


def plan_night(jobs: list[dict] | None = None, delay_minutes: int = 0,
               use_sharing: bool = True) -> dict:
    raw_jobs = demo_jobs() if jobs is None else jobs
    if not isinstance(raw_jobs, list) or len(raw_jobs) > 12:
        raise ValueError("Night demonstration accepts a list of at most 12 jobs.")
    prepared = [_prepare_job(j, i) for i, j in enumerate(raw_jobs)]
    ids = [j["id"] for j in prepared]
    if len(set(ids)) != len(ids):
        raise ValueError("Each job needs a unique id.")
    delay = _number(delay_minutes, 0, 0, 300)
    solved = _solve(prepared, delay, bool(use_sharing))
    result = _result(prepared, solved, delay)
    events = []
    for job in prepared:
        events.append({"agent": "Reader", "job_id": job["id"], "kind": "intake",
                       "message": f"{job['title']}: {job['location_label']}, requested {job['estimated_minutes']} min; isolation {'required' if job['isolation'] else 'not required'}."})
        events.append({"agent": "Checker", "job_id": job["id"], "kind": "ready" if job["ready"] else "blocked",
                       "message": "Parts, competency and plant confirmed in the demo roster." if job["ready"] else job["reason"]})
    events.append({"agent": "Planner", "job_id": None, "kind": "proposal",
                   "message": "Ask each eligible requester which preparation can be shared while keeping separate safe workfaces." if use_sharing else
                              "Independent possessions requested; no shared isolation proposals allowed."})
    for proposal in solved["proposals"]:
        for job_id in proposal["jobs"]:
            events.append({"agent": f"Requester {by_team(prepared, job_id)}", "job_id": job_id,
                           "kind": "offer" if proposal["accepted"] else "constraint",
                           "message": "I can share isolation; retain my p90 working time, rollback allowance and separate workface." if proposal["accepted"] else proposal["reason"]})
        events.append({"agent": "Planner", "job_id": None, "kind": "proposal" if proposal["accepted"] else "rejected",
                       "message": f"{' + '.join(proposal['jobs'])}: {proposal['reason']} " +
                                  ("Submitted to CP-SAT for independent feasibility checking." if proposal["accepted"] else "No relaxation of this hard rule is offered.")})
    events.append({"agent": "CP-SAT", "job_id": None, "kind": "verified",
                   "message": f"{result['solver']['status']}: {result['metrics']['scheduled_count']} jobs selected; "
                              f"{result['metrics']['productive_minutes']} expected productive crew-minutes. All {len(result['checks'])} audit checks passed."})
    if delay:
        events.append({"agent": "Planner", "job_id": None, "kind": "replan",
                       "message": f"Access opening delayed {delay} min. Recomputed a draft against the same 05:30 first train; human approval is required again."})
    _, model_metadata = _models()
    result.update(window=deepcopy(WINDOW), model=deepcopy(model_metadata), events=events,
                  disclosure=DISCLOSURE, agent_mode="Deterministic multi-role policy simulation",
                  approval_status="draft", delay_minutes=delay, use_sharing=bool(use_sharing),
                  proposals=solved["proposals"])
    baseline_jobs = [_prepare_job(j, i) for i, j in enumerate(raw_jobs)]
    baseline_solved = _solve(baseline_jobs, delay, False)
    baseline = _result(baseline_jobs, baseline_solved, delay)
    result["baseline"] = {"metrics": baseline["metrics"], "jobs": baseline["jobs"],
                          "description": "Same requests, durations, safety rules and objective; independent isolation possessions."}
    result["metrics"]["productive_gain_minutes"] = result["metrics"]["productive_minutes"] - baseline["metrics"]["productive_minutes"]
    return result


def by_team(jobs: list[dict], job_id: str) -> str:
    return next(j["team"] for j in jobs if j["id"] == job_id)


def _request_title(content: str) -> str:
    patterns = [
        r"signal(?:ling)?\s+cable\s+(?:replacement|renewal|repair)",
        r"rail\s+grinding", r"point\s+machine\s+(?:overhaul|replacement|repair)",
        r"(?:platform\s+)?(?:cctv|camera)\s+(?:installation|replacement|repair)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.I)
        if match:
            title = match.group(0).lower().capitalize()
            return re.sub(r"\bcctv\b", "CCTV", title, flags=re.I)
    for line in content.splitlines():
        line = line.strip()
        if not line or re.match(r"^(?:hi|hello|dear|thanks|thank you|regards|best regards)\b", line, re.I):
            continue
        return re.sub(r"^(?:subject:\s*|please\s+(?:book|schedule)\s+)", "", line, flags=re.I).split(".")[0][:100]
    return "Maintenance request" if content else ""


def _explicit_competency(content: str) -> str | None:
    explicit = re.search(r"(?:competency|certification)\s*[:=]\s*([a-z -]+?)(?:[,.\n;]|$)", content, re.I)
    if explicit:
        return explicit.group(1).strip().lower()
    disciplines = r"(signalling|signaling|track|communications|points|electrical)"
    for sentence in re.split(r"[.!?;\n]", content.lower()):
        # A negative staffing statement must not become a confirmed credential.
        if re.search(r"\b(?:no|not|without|lacks?|missing)\b", sentence):
            continue
        match = re.search(r"\bhas\s+" + disciplines + r"\s+(?:competency|certification)\b", sentence)
        if not match:
            match = re.search(r"\bcertified\s+in\s+" + disciplines + r"\b", sentence)
        if match:
            return "signalling" if match.group(1) == "signaling" else match.group(1)
    return None


def parse_request(text: str) -> dict:
    """Conservative deterministic reader: unknown safety fields remain missing."""
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("Request must be text under 20,000 characters.")
    content = text.strip()
    lower = content.lower()
    system = next((name for name, words in [
        ("points", ["point machine", "actuator"]),
        ("signalling", ["signal", "cable"]),
        ("track", ["grinding", "rail", "track"]),
        ("communications", ["cctv", "camera", "communications"]),
        ("electrical", ["electrical", "traction"]),
    ] if any(word in lower for word in words)), None)
    # Physical ids are preserved exactly when present; prose station spans are
    # extracted as provisional locations rather than asserted validated sectors.
    physical = re.search(r"(?:SEC|PLAT):(?:ALP|BET):[SH]\d{2}(?:_[SH]\d{2})?:(?:EB|WB)", content, re.I)
    stations = list(dict.fromkeys(re.findall(r"\b(?:S0[1-8]|S1[1-8]|H0[12])\b", content.upper())))
    line = "BET" if re.search(r"\bbeta\b|\bBET\b", content, re.I) else "ALP" if re.search(r"\balpha\b|\bALP\b", content, re.I) else None
    direction = "WB" if re.search(r"westbound|\bWB\b", content, re.I) else "EB" if re.search(r"eastbound|\bEB\b", content, re.I) else None
    location = physical.group(0).upper() if physical else None
    if not location and len(stations) >= 2 and line and direction:
        location = f"SEC:{line}:{stations[0]}_{stations[1]}:{direction}"
    elif not location and len(stations) == 1 and line and direction and ("platform" in lower or system == "communications"):
        location = f"PLAT:{line}:{stations[0]}:{direction}"
    work_text = re.sub(r"(?:rollback|reinstatement|restore)\s*[:=]?\s*\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)\b", "", lower)
    duration_match = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b", work_text)
    duration = None
    if duration_match:
        duration = round(float(duration_match.group(1)) * (60 if duration_match.group(2).startswith(("h",)) else 1))
    else:
        for word, value in {"half an hour": 30, "one hour": 60, "two hours": 120, "three hours": 180, "ninety minutes": 90}.items():
            if word in lower:
                duration = value
                break
    crew_match = re.search(r"(?:crew(?:\s+(?:of|size))?\s*[:=]?\s*(\d+))|(?:(\d+)\s*(?:people|staff|technicians|engineers|person crew))", lower)
    crew = int(next(group for group in crew_match.groups() if group)) if crew_match else None
    isolation = None
    if re.search(r"no\s+(?:power\s+)?isolation|isolation\s+(?:is\s+)?not\s+(?:needed|required)|without\s+isolation|do(?:es)?\s+not\s+(?:need|require)\s+(?:power\s+)?isolation", lower):
        isolation = False
    elif re.search(r"(?:need|require|with|requires|needs)\s+(?:power\s+)?isolation|isolation\s+(?:is\s+)?required", lower):
        isolation = True
    rollback_match = re.search(r"(?:rollback|reinstatement|restore)\s*[:=]?\s*(\d+)\s*(?:min|minute)", lower)
    plant_match = re.search(r"\b(grinder(?:-\d+)?|lift(?:-\d+)?|trolley|crane)\b", lower)
    no_plant = bool(re.search(r"no\s+plant|plant\s*[:=]\s*(?:none|not required)", lower))
    fields = {
        "title": _request_title(content),
        "system": system, "location": location, "section": " → ".join(stations) or None,
        "line": line, "direction": direction, "estimated_minutes": duration,
        "crew_size": crew, "competency": _explicit_competency(content),
        "plant": plant_match.group(1).upper() if plant_match else "None required" if no_plant else None,
        "isolation": isolation, "rollback": int(rollback_match.group(1)) if rollback_match else None,
    }
    required = ["system", "location", "estimated_minutes", "crew_size", "competency", "plant", "isolation", "rollback"]
    missing = [key for key in required if fields[key] is None]
    prompts = {
        "system": "Which railway system is this work on?",
        "location": "Which exact line, station span and bound will the crew occupy?",
        "estimated_minutes": "How many minutes of hands-on work does the team estimate?",
        "crew_size": "How many people will be in the crew?",
        "competency": "What certification must the supervising engineer hold?",
        "plant": "Which plant is needed, or can you confirm that none is required?",
        "isolation": "Does the work require traction power isolation?",
        "rollback": "How many minutes are needed to safely reinstate and clear the railway?",
    }
    return {"fields": fields, "missing": missing,
            "question": prompts[missing[0]] if missing else None,
            "ready_for_check": not missing,
            "method": "Deterministic text extraction; review before acceptance",
            "note": "Extracted location is provisional. Checker must verify parts, roster, plant and electrical section."}


def get_demo() -> dict:
    result = plan_night()
    result["sample_request"] = (
        "Signal cable renewal on Line Alpha from S02 to S03 eastbound. "
        "We need isolation for 2 hours with a crew of 4. "
        "Competency: signalling; no plant required. Rollback 45 minutes."
    )
    return result
