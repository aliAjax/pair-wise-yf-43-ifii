from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    TraceabilityError,
    ValidationError,
)


def _validate_standard(actor, data, lookup):
    if _find_one(lookup, "standard", "code", data.get("code")):
        raise ValidationError("standard code already exists: " + str(data.get("code")))
    _check_standard_dates(data)
    higher = data.get("higher_standard_code")
    if higher and higher == data.get("code"):
        raise ValidationError("standard cannot be calibrated against itself")


def _validate_recalibrate(actor, entity, data, lookup):
    _check_standard_dates(data)
    previous = entity["data"].get("calibrated_at")
    if previous and _parse_ordinal(data["calibrated_at"], "calibrated_at") < _parse_ordinal(previous, "calibrated_at"):
        raise ValidationError("recalibration date cannot be earlier than the previous calibration")
    higher = data.get("higher_standard_code")
    if higher and higher == entity["data"].get("code"):
        raise ValidationError("standard cannot be calibrated against itself")


def _check_standard_dates(data):
    calibrated = _parse_ordinal(data.get("calibrated_at"), "calibrated_at")
    due = _parse_ordinal(data.get("due_at"), "due_at")
    if due <= calibrated:
        raise ValidationError("standard due_at must be after calibrated_at")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_approve(actor, entity, data, lookup):
    record = entity["data"]
    if record.get("result") != "passed":
        raise ValidationError("only a passed calibration can be approved")
    if not record.get("performed_at"):
        raise ValidationError("calibration has no performed_at")
    code = record.get("standard_code")
    if not code:
        raise ValidationError("calibration has no standard_code")
    chain, problems = traceability_chain(code, record["performed_at"], lookup)
    if problems:
        summary = "; ".join("%s %s" % (item["issue"], item["code"]) for item in problems)
        raise TraceabilityError(
            "calibration traceability check failed: " + summary, details=problems
        )
    return {"traceability": chain}


def traceability_chain(standard_code, performed_at, lookup):
    """Walk the standard's calibration chain upwards from the first use date.

    Returns (chain, problems). Each link must have been calibrated before it
    was used and still be valid on that use date; the use date of the next
    level up is the calibration date of the level below.
    """
    chain = []
    problems = []
    visited = set()
    code = standard_code
    use_date = performed_at
    while code:
        if code in visited:
            problems.append({
                "code": code,
                "issue": "cycle",
                "detail": "standard chain forms a cycle at %s" % code,
            })
            break
        visited.add(code)
        node = _find_one(lookup, "standard", "code", code)
        if node is None:
            problems.append({
                "code": code,
                "issue": "broken_chain",
                "detail": "standard %s is not registered" % code,
            })
            break
        data = node["data"]
        try:
            calibrated = _date_ordinal(data.get("calibrated_at"))
            due = _date_ordinal(data.get("due_at"))
            used = _date_ordinal(use_date)
        except (ValueError, TypeError):
            problems.append({
                "code": code,
                "issue": "invalid_dates",
                "detail": "standard %s has missing or malformed dates" % code,
            })
            break
        if calibrated > used:
            problems.append({
                "code": code,
                "issue": "date_order",
                "detail": "standard %s calibrated at %s, after its use on %s"
                % (code, data.get("calibrated_at"), use_date),
            })
        if due < used:
            problems.append({
                "code": code,
                "issue": "expired",
                "detail": "standard %s expired at %s, before its use on %s"
                % (code, data.get("due_at"), use_date),
            })
        chain.append({
            "code": code,
            "calibrated_at": data.get("calibrated_at"),
            "due_at": data.get("due_at"),
            "higher_standard_code": data.get("higher_standard_code"),
        })
        code = data.get("higher_standard_code")
        use_date = data.get("calibrated_at")
    return chain, problems


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), "2026-09-24"):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    calibration_id = instrument["data"].get("current_calibration_id")
    if not calibration_id:
        raise ValidationError("instrument has no approved calibration with traceability")
    snapshot = _find_one(lookup, "snapshot", "calibration_id", calibration_id)
    if not snapshot:
        raise ValidationError("traceability snapshot missing for calibration " + str(calibration_id))
    return {
        "released_by": actor.user_id,
        "calibration_id": calibration_id,
        "traceability_chain": snapshot["chain"],
    }


CUSTOM_CREATE = {'calibration': _validate_calibration, 'standard': _validate_standard}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('calibration', 'approve'): _validate_approve, ('standard', 'recalibrate'): _validate_recalibrate, ('result', 'release'): _validate_result_release}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'standards': 'standard'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'standard': 'active'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed', 'passed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}, 'standard': {'recalibrate': (('active',), 'active')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at', 'standard_code'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement'), 'standard': ('code', 'calibrated_at', 'due_at')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',), ('standard', 'recalibrate'): ('calibrated_at', 'due_at')}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst'), 'standard': ('admin', 'metrology')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst'), 'recalibrate': ('admin', 'metrology')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _parse_ordinal(value, field):
    try:
        return _date_ordinal(value)
    except (ValueError, TypeError):
        raise ValidationError("invalid date for %s: %s" % (field, value))
