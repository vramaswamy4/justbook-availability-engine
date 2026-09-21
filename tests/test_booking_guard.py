"""Write-path conflict rules — `check_slot_has_conflicts`.

PORTED from production's `tests/test_booking_conflict_semantics.py`. That file pins
admin ↔ public parity across HTTP routes as well; the cases here are the ones that
exercise the guard function directly. Assertions are unchanged; the fixtures are
simplified to the tables in this extract (production's build users, a branch roster
and tenant settings too).

The read side (the engine) decides what to OFFER. This is what decides whether a
booking may LAND, and it must use the same rulebook — otherwise the calendar offers
slots the write refuses, or worse, refuses to offer slots the write would accept.
"""
from datetime import datetime, timedelta

from app.models.models import AppointmentHold, utc_now
from app.utils.appointment_helpers import check_slot_has_conflicts
from tests.factories import (
    make_tenant, add_appointment, add_branch, add_hours, add_service, add_status,
    add_stylist,
)

# A Monday 4–5 weeks out: always ahead of "now", computed so it cannot expire.
_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
MONDAY = _today + timedelta(days=28 + (7 - _today.weekday()) % 7)
assert MONDAY.weekday() == 0


def _world(session):
    tenant = make_tenant(session)
    branch = add_branch(session, tenant, working_hours={"mon": ["09:00", "17:00"]})
    service = add_service(session, tenant, duration=60)
    return tenant, branch, service


def _stylist(session, tenant, branch, name):
    s = add_stylist(session, tenant, name=name)
    add_hours(session, s, branch, "mon", start="09:00", end="17:00")
    return s


def _slot_conflicts(tenant, branch, service, *, stylist_id=None, any_stylist=False,
                    ids=None, start=None, minutes=60, buffer_minutes=0):
    start = start or MONDAY.replace(hour=10)
    return check_slot_has_conflicts(
        tenant_id=tenant.id, branch_id=branch.id, service_id=service.id,
        stylist_id=stylist_id, any_stylist=any_stylist, stylist_ids_for_service=ids,
        start_dt=start, end_dt=start + timedelta(minutes=minutes),
        buffer_minutes=buffer_minutes,
    )


# ── any-stylist means "at least one stylist free" ────────────────────────────────

def test_any_stylist_free_when_one_of_two_is_busy(session):
    t, b, svc = _world(session)
    busy = _stylist(session, t, b, "Busy")
    free = _stylist(session, t, b, "Free")
    add_appointment(session, t, stylist=busy, branch=b, service=svc,
                    start=MONDAY.replace(hour=10), minutes=60)
    session.commit()
    assert _slot_conflicts(t, b, svc, any_stylist=True,
                           ids=[busy.id, free.id]) is False


def test_any_stylist_blocked_when_all_busy(session):
    t, b, svc = _world(session)
    s1 = _stylist(session, t, b, "One")
    s2 = _stylist(session, t, b, "Two")
    for s in (s1, s2):
        add_appointment(session, t, stylist=s, branch=b, service=svc,
                        start=MONDAY.replace(hour=10), minutes=60)
    session.commit()
    assert _slot_conflicts(t, b, svc, any_stylist=True, ids=[s1.id, s2.id]) is True


def test_any_stylist_cancelled_appointment_releases(session):
    t, b, svc = _world(session)
    s = _stylist(session, t, b, "Solo")
    add_appointment(session, t, stylist=s, branch=b, service=svc,
                    status=add_status(session, t, "Cancelled"),
                    start=MONDAY.replace(hour=10), minutes=60)
    session.commit()
    assert _slot_conflicts(t, b, svc, any_stylist=True, ids=[s.id]) is False


# ── the buffer is a gap on BOTH sides, enforced by the write path ────────────────

def test_buffer_blocks_back_to_back_after_existing(session):
    # Existing 10–11, buffer 15 → 11:00 start violates the gap; 11:15 is fine.
    t, b, svc = _world(session)
    s = _stylist(session, t, b, "Buffered")
    add_appointment(session, t, stylist=s, branch=b, service=svc,
                    start=MONDAY.replace(hour=10), minutes=60)
    session.commit()
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=11), buffer_minutes=15) is True
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=11, minute=15),
                           buffer_minutes=15) is False


def test_buffer_blocks_back_to_back_before_existing(session):
    # Existing 12–13, buffer 15, new 60-min slot: start 11:00 violates (ends 12:00,
    # needs to end by 11:45); 10:45 is fine.
    t, b, svc = _world(session)
    s = _stylist(session, t, b, "Buffered")
    add_appointment(session, t, stylist=s, branch=b, service=svc,
                    start=MONDAY.replace(hour=12), minutes=60)
    session.commit()
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=11), buffer_minutes=15) is True
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=10, minute=45),
                           buffer_minutes=15) is False


# ── holds block regardless of service ────────────────────────────────────────────

def test_hold_for_other_service_still_blocks(session):
    t, b, svc = _world(session)
    other = add_service(session, t, name="Color", duration=60)
    s = _stylist(session, t, b, "Held")
    session.add(AppointmentHold(
        tenant_id=t.id, branch_id=b.id, service_id=other.id, stylist_id=s.id,
        any_stylist=False, start_time=MONDAY.replace(hour=10),
        end_time=MONDAY.replace(hour=11), duration_minutes=60, status="active",
        hold_token="HLD_test_other_service",
        expires_at=utc_now() + timedelta(minutes=5),
    ))
    session.commit()
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=10)) is True


# ── defence in depth: a directly-posted booking skips slot generation ────────────

def test_a_slot_outside_branch_hours_is_refused_even_if_the_stylist_works_then(session):
    # WRITTEN for this repository (production covers this at the route level).
    t, b, svc = _world(session)                      # branch closes 17:00
    s = add_stylist(session, t, name="Late shift")
    add_hours(session, s, b, "mon", start="09:00", end="20:00")
    session.commit()
    assert _slot_conflicts(t, b, svc, stylist_id=s.id,
                           start=MONDAY.replace(hour=18)) is True
