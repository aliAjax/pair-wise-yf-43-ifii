import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, TraceabilityError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.reference = self.service.create(
            self.actor, "instrument",
            {"name": "PrimaryRef", "serial": "REF-0", "is_reference": True},
        )
        self._calibrate(self.reference, {
            "result": "passed", "performed_at": "2026-01-05",
            "uncertainty": 0.001, "due_at": "2027-01-05",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _calibrate(self, instrument, perform_data, standard_id=None):
        calibration = self.service.create(
            self.actor, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        data = dict(perform_data)
        if standard_id:
            data["standard_id"] = standard_id
        self.service.transition(self.actor, calibration["id"], "perform", data)
        return self.service.transition(
            self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )

    def _make_standard(self, serial, performed_at, due_at):
        standard = self.service.create(
            self.actor, "instrument", {"name": "Std", "serial": serial}
        )
        self._calibrate(standard, {
            "result": "passed", "performed_at": performed_at,
            "uncertainty": 0.005, "due_at": due_at,
        }, standard_id=self.reference["id"])
        return standard

    def test_approve_saves_snapshot_and_activates_instrument(self):
        standard = self._make_standard("STD-1", "2026-02-01", "2027-02-01")
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.actor, instrument["id"], "send_calibration", {})
        approved = self._calibrate(instrument, {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
        }, standard_id=standard["id"])
        snapshot = approved["data"]["traceability_snapshot"]
        self.assertEqual([node["standard_id"] for node in snapshot],
                         [standard["id"], self.reference["id"]])
        self.assertEqual(snapshot[0]["performed_at"], "2026-02-01")
        self.assertEqual(snapshot[1]["due_at"], "2027-01-05")
        active = self.service.get(instrument["id"])
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["data"]["due_at"], "2027-03-01")

    def test_expired_standard_is_returned_with_problem_nodes(self):
        standard = self._make_standard("STD-2", "2026-02-01", "2026-02-15")
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-2"}
        )
        calibration = self.service.create(
            self.actor, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(self.actor, calibration["id"], "perform", {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
            "standard_id": standard["id"],
        })
        with self.assertRaises(TraceabilityError) as ctx:
            self.service.transition(
                self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )
        self.assertEqual(ctx.exception.problems[0]["issue"], "expired")
        self.assertEqual(ctx.exception.problems[0]["standard_id"], standard["id"])
        self.assertEqual(self.service.get(calibration["id"])["status"], "passed")
        returned = [
            row for row in self.service.audit_log(calibration["id"])
            if row["action"] == "approve_returned"
        ]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]["detail"]["problems"], ctx.exception.problems)

    def test_broken_chain_is_returned(self):
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-3"}
        )
        calibration = self.service.create(
            self.actor, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(self.actor, calibration["id"], "perform", {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
            "standard_id": "no-such-standard",
        })
        with self.assertRaises(TraceabilityError) as ctx:
            self.service.transition(
                self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )
        self.assertEqual(ctx.exception.problems[0]["issue"], "broken")

    def test_cycle_in_chain_is_returned(self):
        first = self.service.create(
            self.actor, "instrument", {"name": "A", "serial": "C-1"}
        )
        second = self.service.create(
            self.actor, "instrument", {"name": "B", "serial": "C-2"}
        )
        self._calibrate(first, {
            "result": "passed", "performed_at": "2026-02-01",
            "uncertainty": 0.01, "due_at": "2027-02-01",
        }, standard_id=self.reference["id"])
        self._calibrate(second, {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
        }, standard_id=first["id"])
        self._calibrate(first, {
            "result": "passed", "performed_at": "2026-04-01",
            "uncertainty": 0.01, "due_at": "2027-04-01",
        }, standard_id=second["id"])
        calibration = self.service.create(
            self.actor, "calibration",
            {"instrument_id": second["id"], "requested_at": "2026-05-01"},
        )
        self.service.transition(self.actor, calibration["id"], "perform", {
            "result": "passed", "performed_at": "2026-05-01",
            "uncertainty": 0.01, "due_at": "2027-05-01",
            "standard_id": first["id"],
        })
        with self.assertRaises(TraceabilityError) as ctx:
            self.service.transition(
                self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )
        self.assertEqual(ctx.exception.problems[-1]["issue"], "cycle")

    def test_returned_calibration_can_be_performed_again(self):
        standard = self._make_standard("STD-3", "2026-02-01", "2026-02-15")
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-4"}
        )
        calibration = self.service.create(
            self.actor, "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(self.actor, calibration["id"], "perform", {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
            "standard_id": standard["id"],
        })
        with self.assertRaises(TraceabilityError):
            self.service.transition(
                self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )
        self.service.transition(self.actor, calibration["id"], "perform", {
            "result": "passed", "performed_at": "2026-02-10",
            "uncertainty": 0.01, "due_at": "2027-02-10",
            "standard_id": standard["id"],
        })
        approved = self.service.transition(
            self.actor, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        self.assertEqual(approved["status"], "approved")

    def test_snapshot_is_not_rewritten_by_later_standard_calibration(self):
        standard = self._make_standard("STD-4", "2026-02-01", "2027-02-01")
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-5"}
        )
        approved = self._calibrate(instrument, {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2027-03-01",
        }, standard_id=standard["id"])
        snapshot = approved["data"]["traceability_snapshot"]
        self._calibrate(standard, {
            "result": "passed", "performed_at": "2026-06-01",
            "uncertainty": 0.005, "due_at": "2028-06-01",
        }, standard_id=self.reference["id"])
        reread = self.service.get(approved["id"])
        self.assertEqual(reread["data"]["traceability_snapshot"], snapshot)

    def test_release_returns_full_traceability_chain(self):
        standard = self._make_standard("STD-5", "2026-02-01", "2027-02-01")
        instrument = self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-6"}
        )
        approved = self._calibrate(instrument, {
            "result": "passed", "performed_at": "2026-03-01",
            "uncertainty": 0.01, "due_at": "2099-01-01",
        }, standard_id=standard["id"])
        method = self.service.create(
            self.actor, "method", {"name": "Assay", "version": "v1"}
        )
        self.service.transition(self.actor, method["id"], "validate_method", {
            "parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]],
        })
        result = self.service.create(
            self.actor, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        released = self.service.transition(self.actor, result["id"], "release", {
            "instrument_id": instrument["id"], "method_id": method["id"],
            "value": 1.0, "unit": "mg/L",
        })
        self.assertEqual(
            released["data"]["traceability_chain"],
            approved["data"]["traceability_snapshot"],
        )
        self.assertEqual(
            [node["standard_id"] for node in released["data"]["traceability_chain"]],
            [standard["id"], self.reference["id"]],
        )


if __name__ == "__main__":
    unittest.main()
