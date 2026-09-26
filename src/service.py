from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if kind == "snapshot":
            if field == "calibration_id":
                snapshot = self.repository.get_snapshot(value)
                return [snapshot] if snapshot else []
            return []
        return self.repository.find_entities(kind, field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if entity["kind"] == "calibration" and action == "approve":
            return self._approve_calibration(actor, entity, expected, next_status, patch, merged)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _approve_calibration(self, actor, calibration, expected, next_status, patch, merged):
        """Approve a calibration: store the traceability snapshot and activate
        the instrument in one transaction, so a failure anywhere rolls back."""
        instrument = self.repository.get_entity(merged.get("instrument_id"))
        if not instrument:
            raise NotFoundError("instrument not found: " + str(merged.get("instrument_id")))
        instrument_data = dict(instrument["data"])
        if merged.get("due_at"):
            instrument_data["due_at"] = merged["due_at"]
        instrument_data["current_calibration_id"] = calibration["id"]
        instrument_status = (
            "active" if instrument["status"] == "calibrating" else instrument["status"]
        )
        self.repository.apply_updates(
            [
                {
                    "id": calibration["id"],
                    "expected_version": expected,
                    "status": next_status,
                    "data": merged,
                },
                {
                    "id": instrument["id"],
                    "expected_version": instrument["version"],
                    "status": instrument_status,
                    "data": instrument_data,
                },
            ],
            snapshots=[
                {"calibration_id": calibration["id"], "chain": merged["traceability"]}
            ],
        )
        updated = self.repository.get_entity(calibration["id"])
        self.audit.record(
            calibration["id"],
            actor,
            "approve",
            calibration["status"],
            updated["status"],
            {"patch": patch},
        )
        self.audit.record(
            instrument["id"],
            actor,
            "calibration_activated",
            instrument["status"],
            instrument_status,
            {
                "calibration_id": calibration["id"],
                "due_at": instrument_data.get("due_at"),
            },
        )
        return updated

    def get_snapshot(self, calibration_id):
        snapshot = self.repository.get_snapshot(calibration_id)
        if not snapshot:
            raise NotFoundError("snapshot not found: " + calibration_id)
        return snapshot

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
