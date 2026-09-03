"""Phase-7 warm-standby fencing contract; preparation only, authority=false."""
from __future__ import annotations

from dataclasses import dataclass

VERSION = "EXECUTION_FENCING_CONTRACT_V1"
AUTHORITY = False
COORDINATOR_STATUS = "EXTERNAL_FENCING_COORDINATOR_UNAPPROVED"
TAKEOVER_STATES = (
    "STANDBY", "ACQUIRE_FENCE", "ENTRY_SEALED", "EXCHANGE_RECONCILIATION",
    "POSITION_AND_ORDER_DISCOVERY", "HARD_STOP_VERIFICATION", "EPOCH_REBUILD",
    "WARM_STATE_READY", "MANUAL_APPROVAL_REQUIRED", "EXECUTION_AUTHORITY",
)
FAIL_STATES = frozenset({"NO_ENTRY", "SAFETY_ONLY"})


@dataclass(frozen=True)
class Lease:
    token: int
    lease_id: str
    owner_instance_id: str
    issued_at: float
    renewed_at: float
    expires_at: float


class TestOnlyStrongCoordinator:
    """Deterministic in-memory coordinator for unit tests, never production."""
    production_approved = False
    def __init__(self):
        self.token = 0; self.lease = None; self.reachable = True; self.now = 0.0
    def set_time(self, value): self.now = float(value)
    def acquire(self, owner_instance_id, ttl):
        if not self.reachable:
            return None
        if self.lease is not None and self.now < self.lease.expires_at:
            return None
        self.token += 1
        self.lease = Lease(self.token, f"lease-{self.token}", str(owner_instance_id), self.now, self.now, self.now+float(ttl))
        return self.lease
    def renew(self, lease, ttl):
        if not self.reachable or self.lease != lease or self.now >= lease.expires_at:
            return None
        self.lease = Lease(lease.token, lease.lease_id, lease.owner_instance_id, lease.issued_at, self.now, self.now+float(ttl))
        return self.lease
    def current_token(self):
        return self.lease.token if self.reachable and self.lease and self.now < self.lease.expires_at else None


def validate_fence(lease, *, coordinator_reachable, coordinator_token, coordinator_now):
    if not coordinator_reachable or lease is None:
        return False, "COORDINATOR_UNAVAILABLE"
    if int(lease.token) != int(coordinator_token or -1):
        return False, "STALE_FENCING_TOKEN"
    if float(coordinator_now) < float(lease.issued_at) or float(coordinator_now) >= float(lease.expires_at):
        return False, "LEASE_EXPIRED_OR_CLOCK_INVALID"
    return True, "FENCE_VALID"


def takeover_decision(lease, *, coordinator_reachable, coordinator_token, coordinator_now,
                      exchange_reconciled=False, orders_reconciled=False,
                      hard_stop_verified=False, exposure_present=False,
                      epochs_rebuilt=False, data_health_fresh=False,
                      previous_authority_fenced=False, manual_approval=False,
                      local_clock=None):
    """Local clock is intentionally ignored; coordinator time owns lease validity."""
    ok, reason = validate_fence(
        lease, coordinator_reachable=coordinator_reachable,
        coordinator_token=coordinator_token, coordinator_now=coordinator_now,
    )
    if not ok:
        return {"state":"NO_ENTRY","execution_authority":False,"entry_authority":False,"reason":reason}
    if not exchange_reconciled or not orders_reconciled:
        return {"state":"SAFETY_ONLY","execution_authority":False,"entry_authority":False,"reason":"EXCHANGE_RECONCILIATION_INCOMPLETE"}
    if exposure_present and not hard_stop_verified:
        return {"state":"SAFETY_ONLY","execution_authority":False,"entry_authority":False,"reason":"EXPOSURE_PROTECTION_REQUIRED","required_action":"PROTECT_OR_FLATTEN"}
    if not epochs_rebuilt or not data_health_fresh:
        return {"state":"NO_ENTRY","execution_authority":False,"entry_authority":False,"reason":"WARM_STATE_NOT_READY"}
    if not previous_authority_fenced:
        return {"state":"NO_ENTRY","execution_authority":False,"entry_authority":False,"reason":"PREVIOUS_AUTHORITY_NOT_FENCED"}
    if not manual_approval:
        return {"state":"MANUAL_APPROVAL_REQUIRED","execution_authority":False,"entry_authority":False,"reason":"MANUAL_APPROVAL_REQUIRED"}
    return {
        "state":"EXECUTION_AUTHORITY", "execution_authority":True,
        "entry_authority":True, "reason":"ALL_TAKEOVER_GATES_PASS",
        "fencing_token":lease.token,
    }


def submission_allowed(lease, *, coordinator_reachable, coordinator_token, coordinator_now):
    ok, reason = validate_fence(lease, coordinator_reachable=coordinator_reachable,
                                coordinator_token=coordinator_token, coordinator_now=coordinator_now)
    return {"allowed":ok,"reason":reason,"token":lease.token if lease else None}
