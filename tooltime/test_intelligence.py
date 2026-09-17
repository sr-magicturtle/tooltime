"""Meaningful invariants for the independently solved night demonstration."""
from copy import deepcopy
import unittest

from tooltime.intelligence import demo_jobs, parse_request, plan_night, predict_duration


class NightPlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = plan_night()

    def test_shared_isolation_adds_work_without_changing_duration(self):
        plan = self.plan
        self.assertTrue(all(check["passed"] for check in plan["checks"]))
        self.assertGreater(plan["metrics"]["productive_minutes"], plan["baseline"]["metrics"]["productive_minutes"])
        self.assertGreater(plan["metrics"]["overhead_saved_minutes"], 0)
        self.assertEqual(plan["metrics"]["scheduled_count"], 3)
        original = {j["id"]: j for j in plan["baseline"]["jobs"]}
        for job in plan["jobs"]:
            self.assertEqual(job["p90"], original[job["id"]]["p90"])
            self.assertEqual(job["rollback"], original[job["id"]]["rollback"])

    def test_commitment_uses_p90_and_keeps_rollback(self):
        for job in self.plan["jobs"]:
            self.assertEqual(job["latest_commit_minute"], 330 - job["p90"] - job["rollback"])
            if job["status"] == "scheduled":
                self.assertLessEqual(job["start_minute"], job["latest_commit_minute"])
                self.assertGreaterEqual(job["release_minute"], job["end_minute"] + job["rollback"])
                self.assertLessEqual(job["release_minute"], 330)

    def test_readiness_blocks_point_machine(self):
        job = next(j for j in self.plan["jobs"] if j["id"] == "TT-104")
        self.assertEqual(job["status"], "blocked")
        self.assertIsNone(job["start"])
        self.assertEqual(len(job["blockers"]), 2)

    def test_identical_workface_cannot_be_bundled(self):
        jobs = demo_jobs()[:2]
        jobs[1]["location"] = jobs[0]["location"]
        result = plan_night(jobs)
        self.assertEqual(result["metrics"]["scheduled_count"], 1)
        self.assertTrue(any(not p["accepted"] and "same physical" in p["reason"] for p in result["proposals"]))

    def test_single_plant_cannot_be_double_booked(self):
        jobs = demo_jobs()[:2]
        for job in jobs:
            job["plant"] = "SHARED-01"
        result = plan_night(jobs)
        self.assertEqual(result["metrics"]["scheduled_count"], 1)
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_adjacent_grinding_is_not_relaxed(self):
        jobs = demo_jobs()[:2]
        jobs[1]["location"] = "SEC:ALP:S03_S04:EB"
        jobs[1]["sector_index"] = 3
        result = plan_night(jobs)
        self.assertEqual(result["metrics"]["scheduled_count"], 1)
        self.assertTrue(any(not p["accepted"] and "intervening sector" in p["reason"] for p in result["proposals"]))

    def test_unverified_readiness_is_never_assumed(self):
        job = deepcopy(demo_jobs()[0])
        del job["part_available"]
        result = plan_night([job])
        self.assertEqual(result["jobs"][0]["status"], "blocked")
        self.assertEqual(result["metrics"]["scheduled_count"], 0)

    def test_traction_power_conflict_is_section_wide(self):
        jobs = [demo_jobs()[0], demo_jobs()[2]]
        jobs[1]["requires_traction_power"] = True
        jobs[1]["p50"] = 90
        jobs[1]["p90"] = 100
        result = plan_night(jobs)
        # Cable possession consumes >200 min; 100 powered minutes cannot coexist.
        self.assertEqual(result["metrics"]["scheduled_count"], 1)
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_delay_replans_safely_and_preserves_requests(self):
        result = plan_night(delay_minutes=130)
        self.assertEqual(len(result["jobs"]), 4)
        self.assertGreater(result["metrics"]["deferred_count"], 0)
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual(result["approval_status"], "draft")
        for job in result["jobs"]:
            if job["status"] == "scheduled":
                self.assertGreaterEqual(job["start_minute"], 160)
                self.assertLessEqual(job["release_minute"], 330)

    def test_empty_request_set_and_duplicate_ids(self):
        result = plan_night([])
        self.assertEqual(result["metrics"]["scheduled_count"], 0)
        duplicate = deepcopy(demo_jobs()[0])
        with self.assertRaises(ValueError):
            plan_night([duplicate, duplicate])

    def test_model_is_disclosed_and_quantiles_ordered(self):
        self.assertTrue(self.plan["model"]["synthetic"])
        self.assertEqual(self.plan["model"]["training_records"], 960)
        for job in demo_jobs():
            result = predict_duration(job)
            self.assertGreaterEqual(result["p90"], result["p50"])
            self.assertGreater(result["p50"], 0)


class ReaderTests(unittest.TestCase):
    def test_email_sample_skips_greeting_and_reads_explicit_competency(self):
        result = parse_request(
            "Hi Planning,\n\nPlease book signal cable replacement on ALP S02–S03 eastbound on Tuesday. "
            "We estimate120 minutes with a crew of4. Traction isolation is required. "
            "Our supervisor has signalling competency and we need a cable trolley.\n\n"
            "We can move to Thursday if needed. Parts are at Alpha depot.\nThanks, Team A."
        )
        self.assertEqual(result["fields"]["title"], "Signal cable replacement")
        self.assertEqual(result["fields"]["competency"], "signalling")
        self.assertEqual(result["fields"]["crew_size"], 4)
        self.assertEqual(result["fields"]["estimated_minutes"], 120)
        self.assertEqual(result["missing"], ["rollback"])
        self.assertIn("reinstate", result["question"])

    def test_certification_sentence_and_negated_supervisor(self):
        positive = parse_request("Hello team,\nRail grinding. Our supervisor is certified in track.")
        self.assertEqual(positive["fields"]["title"], "Rail grinding")
        self.assertEqual(positive["fields"]["competency"], "track")
        for sentence in ("No supervisor has signalling competency.",
                         "There is no supervisor certified in signalling.",
                         "Our supervisor is not certified in signalling."):
            result = parse_request("Signal cable replacement. " + sentence)
            self.assertIsNone(result["fields"]["competency"])

    def test_full_request_extracts_and_preserves_negation(self):
        result = parse_request("CCTV at Line Alpha S03 platform westbound, 90 minutes, crew of 3. "
                               "No isolation needed. No plant. Competency: communications; rollback 15 minutes.")
        self.assertEqual(result["fields"]["location"], "PLAT:ALP:S03:WB")
        self.assertEqual(result["fields"]["estimated_minutes"], 90)
        self.assertEqual(result["fields"]["crew_size"], 3)
        self.assertFalse(result["fields"]["isolation"])
        self.assertEqual(result["missing"], [])
        self.assertIsNone(result["question"])

    def test_incomplete_request_asks_one_question(self):
        result = parse_request("Signal cable replacement needs two hours.")
        self.assertEqual(result["fields"]["estimated_minutes"], 120)
        self.assertGreater(len(result["missing"]), 1)
        self.assertEqual(result["question"].count("?"), 1)
        self.assertIsNone(result["fields"]["isolation"])

    def test_rollback_is_not_mistaken_for_work_duration(self):
        result = parse_request("Rollback 45 minutes. Signal cable work takes 2 hours, "
                               "does not need isolation.")
        self.assertEqual(result["fields"]["estimated_minutes"], 120)
        self.assertEqual(result["fields"]["rollback"], 45)
        self.assertFalse(result["fields"]["isolation"])


if __name__ == "__main__":
    unittest.main()
