"""Behaviour pins for the shared month-availability engine.

compute_month_availability now backs both admin_calendar_days and public_calendar_days.
These DB-backed scenarios lock its per-day / per-stylist gap logic and every
parameterized branch (day-offered gate, bookable_from, any_stylist, customer conflicts,
time-off, buffer). tz=UTC keeps local==UTC so the dates are trivial to reason about.

"""
import calendar
from datetime import datetime, timedelta, date

import pytz

from app.models.models import StylistTimeOff
from app.utils.availability_engine import compute_month_availability
from tests.factories import (
    make_tenant, add_branch, add_customer,
    add_stylist, add_service, add_appointment, add_hours, add_status,
)

UTC = pytz.UTC
YEAR, MONTH = 2035, 7  # far future → never past-blocked


def _mondays():
    return [d for d in range(1, calendar.monthrange(YEAR, MONTH)[1] + 1)
            if date(YEAR, MONTH, d).weekday() == 0]


def _now_before():
    return UTC.localize(datetime(YEAR, MONTH - 1, 1, 0, 0))


def _base(session, *, wh=("09:00", "17:00"), sa=("09:00", "17:00")):
    """Tenant + UTC branch open Mondays + one stylist available Mondays."""
    t = make_tenant(session)
    b = add_branch(session, t)
    b.timezone = "UTC"
    b.working_hours = {"mon": list(wh)}
    s = add_stylist(session, t)
    add_service(session, t, stylists=[s])
    add_hours(session, s, b, "mon", start=sa[0], end=sa[1])
    session.flush()
    return t, b, s


def _run(t, b, s, *, min_gap_min=30, buffer_min=0, step_minutes=5, any_stylist=False,
         target=None, customer_id=None, exclude=None, bookable=None, offered=None):
    now = _now_before()
    return compute_month_availability(
        tenant_id=t.id, branch=b, tz=UTC, year=YEAR, month=MONTH,
        target_stylist_ids=target if target is not None else [s.id],
        any_stylist=any_stylist, stylist_id=s.id,
        duration_min=min_gap_min, buffer_min=buffer_min,
        step_minutes=step_minutes, lead_minutes=0,
        now_utc=now, now_local=now,
        customer_id=customer_id, exclude_appointment_id=exclude,
        bookable_from_by_sid=bookable,
        is_day_offered=offered or (lambda d: True),
    )


def _avail(resp):
    return {r["day"] for r in resp if r["available"]}


def test_basic_only_mondays_available(session):
    t, b, s = _base(session)
    assert _avail(_run(t, b, s)) == set(_mondays())


def test_appointment_fills_block(session):
    # 60-min block; an appointment covering the whole hour leaves no 30-min gap.
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    avail = _avail(_run(t, b, s))
    assert mondays[0] not in avail        # filled
    assert set(mondays[1:]) <= avail      # other mondays still free


def test_gap_requirement_includes_buffer(session):
    # A 60-min block fits a 45-min (service+buffer) gap but not a 75-min one.
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    assert _avail(_run(t, b, s, min_gap_min=45)) == set(_mondays())
    assert _avail(_run(t, b, s, min_gap_min=75)) == set()


def test_any_stylist_available_if_one_has_hours(session):
    t, b, s1 = _base(session)
    s2 = add_stylist(session, t)  # no availability rows
    resp = _run(t, b, s1, any_stylist=True, target=[s1.id, s2.id])
    assert _avail(resp) == set(_mondays())


def test_bookable_from_hides_early_days(session):
    t, b, s = _base(session)
    mondays = _mondays()
    resp = _run(t, b, s, bookable={s.id: date(YEAR, MONTH, mondays[1])})
    avail = _avail(resp)
    assert mondays[0] not in avail
    assert {m for m in mondays if m >= mondays[1]} <= avail


def test_is_day_offered_gate(session):
    t, b, s = _base(session)
    mondays = _mondays()
    cutoff = date(YEAR, MONTH, mondays[1])
    resp = _run(t, b, s, offered=lambda d: d <= cutoff)
    assert _avail(resp) == {mondays[0], mondays[1]}


def test_customer_conflict_blocks_only_with_customer_id(session):
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    c = add_customer(session, t)
    other = add_stylist(session, t)  # customer busy on a DIFFERENT stylist
    add_appointment(session, t, stylist=other, branch=b, customer=c,
                    start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    # target stylist s is free that day → available without customer scoping...
    assert mondays[0] in _avail(_run(t, b, s))
    # ...but blocked once we scope to the (busy) customer.
    assert mondays[0] not in _avail(_run(t, b, s, customer_id=c.id))


def test_customer_cancelled_appointment_does_not_block(session):
    # Regression: the customer-conflict load counted Cancelled appointments, so the
    # admin new-appointment calendar (which passes customer_id) marked days
    # unavailable that the public flow (no customer_id) and the day-slots route
    # both offered. Cancelled must release the customer's day too.
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    c = add_customer(session, t)
    other = add_stylist(session, t)
    cancelled = add_status(session, t, "Cancelled")
    add_appointment(session, t, stylist=other, branch=b, customer=c, status=cancelled,
                    start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    assert mondays[0] in _avail(_run(t, b, s, customer_id=c.id))


def test_customer_null_status_appointment_still_blocks(session):
    # NULL-status customer appointments stay blocking (outerjoin, id IS NULL is
    # active) — same semantics as the stylist load and the day-slots route.
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    c = add_customer(session, t)
    other = add_stylist(session, t)
    add_appointment(session, t, stylist=other, branch=b, customer=c, status=None,
                    start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    assert mondays[0] not in _avail(_run(t, b, s, customer_id=c.id))


def test_customer_conflict_honours_exclude_appointment_id(session):
    # Edit mode: the appointment being edited must not block its own customer's
    # calendar (the stylist load already excluded it; the customer load didn't).
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    c = add_customer(session, t)
    other = add_stylist(session, t)
    appt = add_appointment(session, t, stylist=other, branch=b, customer=c,
                           start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    assert mondays[0] not in _avail(_run(t, b, s, customer_id=c.id))
    assert mondays[0] in _avail(_run(t, b, s, customer_id=c.id, exclude=appt.id))


def test_cancelled_appointment_does_not_block_month(session):
    # A day fully booked (60-min appt fills the single 60-min block) is unavailable on
    # the month grid; once that appointment is Cancelled it must free up again — the
    # month engine mirrors the day-slots route, which only excludes Cancelled.
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    booked = add_status(session, t, "Booked")
    cancelled = add_status(session, t, "Cancelled")

    appt = add_appointment(session, t, stylist=s, branch=b, status=booked,
                           start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    # Booked → the day is filled and drops off the month grid.
    assert mondays[0] not in _avail(_run(t, b, s))

    # Cancel it → its slot is released, and the month grid shows the day again.
    appt.status_id = cancelled.id
    session.flush()
    assert mondays[0] in _avail(_run(t, b, s))


def test_null_status_appointment_still_blocks_month(session):
    # An appointment with no status set (outerjoin NULL) must still block, matching
    # the day-slots filter (id IS NULL is treated as active, not Cancelled).
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    add_appointment(session, t, stylist=s, branch=b, status=None,
                    start=datetime(YEAR, MONTH, mondays[0], 9, 0), minutes=60)
    assert mondays[0] not in _avail(_run(t, b, s))


def test_timeoff_blocks_day(session):
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    mondays = _mondays()
    session.add(StylistTimeOff(
        stylist_id=s.id, branch_id=b.id,
        start_datetime=datetime(YEAR, MONTH, mondays[0], 9, 0),
        end_datetime=datetime(YEAR, MONTH, mondays[0], 10, 0),
    ))
    session.flush()
    assert mondays[0] not in _avail(_run(t, b, s))


# ── Grid/buffer/lead parity with the day-slots generator (reported from a tenant's booking page) ──
# The old check accepted any free run >= duration+buffer; the day generator needs a
# grid-aligned start whose [start, start+dur+buf] misses the buffer-extended
# conflicts and sits past now+lead. These pin the new _grid_slot_exists semantics.

def _first_monday():
    return _mondays()[0]


def test_buffered_hold_tail_kills_the_day(session):
    """THE reported bug: branch open 09:00-12:00, service 90 + buffer 15 (105
    total), appointment 09:00-10:00. Raw gap 10:00-12:00 = 120 >= 105 → the old
    month said available. But the day generator extends the conflict to 10:15,
    and the next grid start (step 30) is 10:30 → 10:30+105 = 12:15 > 12:00: no
    slot. Month must now say unavailable too."""
    t, b, s = _base(session, wh=("09:00", "12:00"), sa=("09:00", "12:00"))
    booked = add_status(session, t, "Booked")
    d = _first_monday()
    add_appointment(session, t, stylist=s, branch=b, status=booked,
                    start=datetime(YEAR, MONTH, d, 9, 0),
                    minutes=60)
    session.commit()
    resp = _run(t, b, s, min_gap_min=90, buffer_min=15, step_minutes=30)
    avail = {e["day"] for e in resp if e["available"]}
    assert d not in avail, "a gap with no valid grid start must not mark the day available"


def test_hold_blocks_day_like_day_slots(session):
    """An active hold covering the only slot makes the day unavailable (the
    calendar used to offer the day while the slot list came back empty)."""
    from app.models.models import AppointmentHold, Service, utc_now as _un
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    svc = Service.query.filter_by(tenant_id=t.id).first()
    d = _first_monday()
    session.add(AppointmentHold(
        tenant_id=t.id, branch_id=b.id, service_id=svc.id,
        stylist_id=s.id, any_stylist=False,
        start_time=datetime(YEAR, MONTH, d, 9, 0),
        end_time=datetime(YEAR, MONTH, d, 10, 0),
        duration_minutes=60, hold_token="t-hold-1", status="active",
        expires_at=_un() + timedelta(days=36500),
    ))
    session.commit()
    resp = _run(t, b, s, min_gap_min=30, step_minutes=30)
    avail = {e["day"] for e in resp if e["available"]}
    assert d not in avail


def test_lead_time_hides_today_when_remaining_slots_inside_lead(session):
    """now + lead past the last possible start → day unavailable (day-slots
    applies the same earliest-bookable floor)."""
    t, b, s = _base(session, wh=("09:00", "10:00"), sa=("09:00", "10:00"))
    session.commit()
    d = _first_monday()
    # "now" = 08:00 that Monday; lead 90min → earliest 09:30; last 30-min start 09:30
    # fits… so use lead 120 → earliest 10:00 → nothing fits.
    now = UTC.localize(datetime(YEAR, MONTH, d, 8, 0))
    resp = compute_month_availability(
        tenant_id=t.id, branch=b, tz=UTC, year=YEAR, month=MONTH,
        target_stylist_ids=[s.id], any_stylist=False, stylist_id=s.id,
        duration_min=30, buffer_min=0, step_minutes=30, lead_minutes=120,
        now_utc=now, now_local=now,
        is_day_offered=lambda dd: True,
    )
    avail = {e["day"] for e in resp if e["available"]}
    assert d not in avail
    # sanity: with lead 0 the same day IS available
    resp2 = compute_month_availability(
        tenant_id=t.id, branch=b, tz=UTC, year=YEAR, month=MONTH,
        target_stylist_ids=[s.id], any_stylist=False, stylist_id=s.id,
        duration_min=30, buffer_min=0, step_minutes=30, lead_minutes=0,
        now_utc=now, now_local=now,
        is_day_offered=lambda dd: True,
    )
    assert d in {e["day"] for e in resp2 if e["available"]}


# ── Multi-day conflicts must block EVERY covered day (2026-07-18 bug) ──
# Conflicts were bucketed only under their START day, so a time off spanning
# Jul 18 00:00 → Jul 21 00:00 blocked only the 18th in the month view while the
# day-slots route (which loads every overlapping row per day) returned zero
# slots for the 19th/20th — the calendar offered days whose slot list was empty.


def test_multiday_timeoff_blocks_every_covered_day(session):
    t, b, s = _base(session)
    mondays = _mondays()
    # Time off from first Monday 00:00 through the day AFTER the second Monday
    # at 00:00 — covers both Mondays end to end.
    session.add(StylistTimeOff(
        stylist_id=s.id, branch_id=b.id,
        start_datetime=datetime(YEAR, MONTH, mondays[0], 0, 0),
        end_datetime=datetime(YEAR, MONTH, mondays[1] + 1, 0, 0),
    ))
    session.flush()
    avail = _avail(_run(t, b, s))
    assert mondays[0] not in avail   # start day (always worked)
    assert mondays[1] not in avail   # covered NON-start day (the regression)
    assert set(mondays[2:]) <= avail


def test_timeoff_ending_at_midnight_frees_that_day(session):
    # Half-open interval: ending exactly at a Monday's local midnight must not
    # block that Monday.
    t, b, s = _base(session)
    mondays = _mondays()
    session.add(StylistTimeOff(
        stylist_id=s.id, branch_id=b.id,
        start_datetime=datetime(YEAR, MONTH, mondays[0], 0, 0),
        end_datetime=datetime(YEAR, MONTH, mondays[1], 0, 0),
    ))
    session.flush()
    avail = _avail(_run(t, b, s))
    assert mondays[0] not in avail
    assert set(mondays[1:]) <= avail


def test_multiday_customer_conflict_blocks_covered_days(session):
    # The customer-conflict buckets use the same day-span expansion.
    t, b, s = _base(session)
    mondays = _mondays()
    c = add_customer(session, t)
    other = add_stylist(session, t)
    add_appointment(session, t, stylist=other, branch=b, customer=c,
                    start=datetime(YEAR, MONTH, mondays[0], 0, 0),
                    minutes=(mondays[1] - mondays[0] + 1) * 24 * 60)
    avail = _avail(_run(t, b, s, customer_id=c.id))
    assert mondays[0] not in avail
    assert mondays[1] not in avail
    assert set(mondays[2:]) <= avail
