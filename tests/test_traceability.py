import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, TraceabilityError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _standard(self, code, calibrated_at, due_at, higher=None):
        data = {"code": code, "calibrated_at": calibrated_at, "due_at": due_at}
        if higher:
            data["higher_standard_code"] = higher
        return self.service.create(self.actor, "standard", data)

    def _instrument(self):
        return self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )

    def _passed_calibration(self, instrument_id, standard_code, performed_at="2026-01-02"):
        calibration = self.service.create(
            self.actor,
            "calibration",
            {
                "instrument_id": instrument_id,
                "requested_at": "2026-01-01",
                "standard_code": standard_code,
            },
        )
        return self.service.transition(
            self.actor,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": performed_at,
                "uncertainty": 0.01,
                "due_at": "2099-01-01",
            },
        )

    def _approve(self, calibration_id):
        return self.service.transition(
            self.actor, calibration_id, "approve", {"authorized_by": "QA-1"}
        )

    def test_approve_saves_snapshot_and_activates_instrument(self):
        self._standard("STD-ROOT", "2025-01-10", "2030-01-10")
        self._standard("STD-1", "2025-06-01", "2027-06-01", higher="STD-ROOT")
        instrument = self._instrument()
        self.service.transition(self.actor, instrument["id"], "send_calibration", {})
        calibration = self._passed_calibration(instrument["id"], "STD-1")

        approved = self._approve(calibration["id"])
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(
            [node["code"] for node in approved["data"]["traceability"]],
            ["STD-1", "STD-ROOT"],
        )

        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertEqual(instrument["data"]["due_at"], "2099-01-01")
        self.assertEqual(instrument["data"]["current_calibration_id"], calibration["id"])

        snapshot = self.service.get_snapshot(calibration["id"])
        self.assertEqual(snapshot["chain"], approved["data"]["traceability"])

    def test_expired_standard_is_returned_with_problem_nodes(self):
        self._standard("STD-OLD", "2024-01-01", "2025-12-31")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-OLD")

        with self.assertRaises(TraceabilityError) as ctx:
            self._approve(calibration["id"])
        problems = ctx.exception.details
        self.assertEqual([p["issue"] for p in problems], ["expired"])
        self.assertEqual(problems[0]["code"], "STD-OLD")
        # 退回后校准记录保持未批准状态
        self.assertEqual(self.service.get(calibration["id"])["status"], "passed")

    def test_date_order_violation_is_returned(self):
        # 标准器校准日期晚于使用日期，日期先后不成立
        self._standard("STD-LATE", "2026-06-01", "2027-06-01")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-LATE")

        with self.assertRaises(TraceabilityError) as ctx:
            self._approve(calibration["id"])
        self.assertEqual([p["issue"] for p in ctx.exception.details], ["date_order"])

    def test_broken_chain_is_returned(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-GHOST")

        with self.assertRaises(TraceabilityError) as ctx:
            self._approve(calibration["id"])
        problems = ctx.exception.details
        self.assertEqual(problems[0]["issue"], "broken_chain")
        self.assertEqual(problems[0]["code"], "STD-GHOST")

    def test_cycle_is_returned(self):
        self._standard("STD-A", "2025-01-01", "2027-01-01", higher="STD-B")
        self._standard("STD-B", "2025-01-01", "2027-01-01", higher="STD-A")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-A")

        with self.assertRaises(TraceabilityError) as ctx:
            self._approve(calibration["id"])
        self.assertEqual([p["issue"] for p in ctx.exception.details], ["cycle"])

    def test_upper_level_expiry_is_listed_with_node(self):
        # 工作标准本身有效，但其上级标准在校准工作标准时已过期
        self._standard("STD-ROOT", "2024-01-01", "2025-01-01")
        self._standard("STD-1", "2025-06-01", "2027-06-01", higher="STD-ROOT")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-1")

        with self.assertRaises(TraceabilityError) as ctx:
            self._approve(calibration["id"])
        problems = ctx.exception.details
        self.assertEqual([p["issue"] for p in problems], ["expired"])
        self.assertEqual(problems[0]["code"], "STD-ROOT")

    def test_recalibrating_standard_does_not_rewrite_snapshot(self):
        self._standard("STD-ROOT", "2025-01-10", "2030-01-10")
        working = self._standard("STD-1", "2025-06-01", "2027-06-01", higher="STD-ROOT")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-1")
        self._approve(calibration["id"])
        before = self.service.get_snapshot(calibration["id"])

        # 标准器再校准只更新标准器自身，不允许改写历史快照
        self.service.transition(
            self.actor,
            working["id"],
            "recalibrate",
            {"calibrated_at": "2026-06-01", "due_at": "2028-06-01"},
        )
        after = self.service.get_snapshot(calibration["id"])
        self.assertEqual(before, after)
        self.assertEqual(after["chain"][0]["calibrated_at"], "2025-06-01")

    def test_release_returns_full_chain_from_snapshot(self):
        self._standard("STD-ROOT", "2025-01-10", "2030-01-10")
        working = self._standard("STD-1", "2025-06-01", "2027-06-01", higher="STD-ROOT")
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-1")
        self._approve(calibration["id"])
        method = self.service.create(self.actor, "method", {"name": "Assay", "version": "v1"})
        self.service.transition(
            self.actor,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        # 标准器再校准后，放行仍返回审批时采用的链路
        self.service.transition(
            self.actor,
            working["id"],
            "recalibrate",
            {"calibrated_at": "2026-06-01", "due_at": "2028-06-01"},
        )
        result = self.service.create(
            self.actor, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        released = self.service.transition(
            self.actor,
            result["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": 1.0,
                "unit": "mg/L",
            },
        )
        chain = released["data"]["traceability_chain"]
        self.assertEqual([node["code"] for node in chain], ["STD-1", "STD-ROOT"])
        self.assertEqual(chain[0]["calibrated_at"], "2025-06-01")
        self.assertEqual(released["data"]["calibration_id"], calibration["id"])

    def test_release_requires_approved_traceable_calibration(self):
        instrument = self._instrument()
        instrument = self.service.transition(
            self.actor, instrument["id"], "send_calibration", {}
        )
        # 旧路径直接写到期日，没有溯源快照，放行必须拒绝
        self.service.transition(
            self.actor,
            instrument["id"],
            "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        method = self.service.create(self.actor, "method", {"name": "Assay", "version": "v1"})
        self.service.transition(
            self.actor,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        result = self.service.create(
            self.actor, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                result["id"],
                "release",
                {
                    "instrument_id": instrument["id"],
                    "method_id": method["id"],
                    "value": 1.0,
                    "unit": "mg/L",
                },
            )

    def test_failed_approval_can_be_corrected_and_reapproved(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(instrument["id"], "STD-GHOST")
        with self.assertRaises(TraceabilityError):
            self._approve(calibration["id"])
        # 补登标准器并重新执行校准后即可批准
        self._standard("STD-GHOST", "2025-06-01", "2027-06-01")
        self.service.transition(
            self.actor,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-03",
                "uncertainty": 0.01,
                "due_at": "2099-01-01",
            },
        )
        approved = self._approve(calibration["id"])
        self.assertEqual(approved["status"], "approved")

    def test_standard_code_must_be_unique(self):
        self._standard("STD-1", "2025-06-01", "2027-06-01")
        with self.assertRaises(ValidationError):
            self._standard("STD-1", "2025-06-01", "2027-06-01")

    def test_recalibrate_requires_later_date(self):
        working = self._standard("STD-1", "2025-06-01", "2027-06-01")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                working["id"],
                "recalibrate",
                {"calibrated_at": "2025-01-01", "due_at": "2027-01-01"},
            )


if __name__ == "__main__":
    unittest.main()
