"""Availability-engine edges the month suite doesn't cover.

test_month_availability_engine.py pins the per-day/per-stylist gap logic in a trivial
UTC branch. This suite adds the harder edges:

  - **DST transitions**: a branch in a DST-observing tz across spring-forward and
    fall-back. pytz.localize must give each day its correct UTC offset, so the
    branch's 09:00–17:00 window is honoured on both sides of the transition (the day
    is available regardless of the clock change) — a naive fixed-offset conversion
    would drop or shift the window.
  - **Month boundaries**: an appointment on the LAST day of the month fills only that
    day; the first day of the month is unaffected. (Guards the month_start/month_end
    windowing.)
  - **Hold expiry racing a second booker**: an ACTIVE hold blocks the day; an EXPIRED
    hold (expires_at in the past) does not — get_active_holds_query filters it out, so
    an abandoned hold frees the slot for the next booker.

Reuses the builders from test_month_availability_engine so the engine wiring can't drift.

"""
import calendar
import uuid
from datetime import date, datetime, time as dt_time, timedelta

import pytz

from app.models.models import AppointmentHold, utc_now
from app.utils.availability_engine import compute_month_availability
from tests.factories import (
    make_tenant, add_appointment, add_hours, add_service,
)
from tests.factories import add_stylist as _add_stylist


def _build(session, tzname, *, wh=("09:00", "17:00")):
    """Tenant + branch in `tzname` open every weekday + one stylist available too."""
    tz = pytz.timezone(tzname)
    t = make_tenant(session)
    from tests.factories import add_branch
    b = add_branch(session, t)
    b.timezone = tzname
    b.working_hours = {d: list(wh) for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    s = _add_stylist(session, t)
    add_service(session, t, stylists=[s])
    for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        add_hours(session, s, b, d, start=wh[0], end=wh[1])
    session.flush()
    return t, b, s, tz


def _run(t, b, s, tz, year, month, *, min_gap_min=30, buffer_min=0, step_minutes=5,
         customer_id=None):
    now = tz.localize(datetime(year, month, 1, 0, 0)) - timedelta(days=45)
    return compute_month_availability(
        tenant_id=t.id, branch=b, tz=tz, year=year, month=month,
        target_stylist_ids=[s.id], any_stylist=False, stylist_id=s.id,
        duration_min=min_gap_min, buffer_min=buffer_min,
        step_minutes=step_minutes, lead_minutes=0,
        now_utc=now.astimezone(pytz.UTC), now_local=now,
        customer_id=customer_id, exclude_appointment_id=None,
        bookable_from_by_sid=None, is_day_offered=lambda d: True,
    )


def _avail(resp):
    return {r["day"] for r in resp if r["available"]}


# --------------------------------------------------------------------------- #
# DST transitions
# --------------------------------------------------------------------------- #
def test_spring_forward_day_still_available(session):
    """US spring-forward is the 2nd Sunday of March (clocks skip 02:00→03:00). The
    branch's 09:00–17:00 window is well clear of the gap, so the transition day and
    its neighbours must all be available — no window dropped by a bad tz conversion."""
    year = 2035
    t, b, s, tz = _build(session, "America/New_York")
    resp = _avail(_run(t, b, s, tz, year, 3))
    # 2nd Sunday of March:
    marchdays = [d for d in range(1, 32) if date(year, 3, d).weekday() == 6]
    dst_day = marchdays[1]
    assert dst_day in resp, "spring-forward day was wrongly unavailable"
    assert (dst_day - 1) in resp and (dst_day + 1) in resp


def test_fall_back_day_still_available(session):
    """US fall-back is the 1st Sunday of November (02:00→01:00 repeats). Same window,
    same expectation — the ambiguous hour is far from the 09:00 open."""
    year = 2035
    t, b, s, tz = _build(session, "America/New_York")
    resp = _avail(_run(t, b, s, tz, year, 11))
    novsundays = [d for d in range(1, 31) if date(year, 11, d).weekday() == 6]
    dst_day = novsundays[0]
    assert dst_day in resp, "fall-back day was wrongly unavailable"
    assert (dst_day - 1) in resp and (dst_day + 1) in resp


def test_dst_appointment_localizes_to_correct_day(session):
    """An appointment booked at the branch's local 09:00 on the fall-back day fills
    THAT day (not the day before/after) — the naive-local start_time is localized in
    the branch tz, so DST doesn't smear it across the date boundary."""
    year = 2035
    t, b, s, tz = _build(session, "America/New_York", wh=("09:00", "10:00"))
    novsundays = [d for d in range(1, 31) if date(year, 11, d).weekday() == 6]
    dst_day = novsundays[0]
    # Appointment 09:00-10:00 local fills the only hour of that day.
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(year, 11, dst_day, 9, 0), minutes=60)
    resp = _avail(_run(t, b, s, tz, year, 11))
    assert dst_day not in resp, "DST-day appointment did not fill its own day"
    # An unrelated day (a week later, same weekday) is untouched.
    assert (dst_day + 7) in resp, "an unrelated day was wrongly blocked"


# --------------------------------------------------------------------------- #
# Month boundaries
# --------------------------------------------------------------------------- #
def test_last_day_of_month_appointment_fills_only_that_day(session):
    year, month = 2035, 7
    t, b, s, tz = _build(session, "UTC", wh=("09:00", "10:00"))
    last = calendar.monthrange(year, month)[1]
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(year, month, last, 9, 0), minutes=60)
    resp = _avail(_run(t, b, s, tz, year, month))
    assert last not in resp, "last-day appointment did not fill the last day"
    assert 1 in resp, "the first day was wrongly affected by a last-day appointment"


def test_first_day_of_month_appointment_fills_only_that_day(session):
    year, month = 2035, 7
    t, b, s, tz = _build(session, "UTC", wh=("09:00", "10:00"))
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(year, month, 1, 9, 0), minutes=60)
    resp = _avail(_run(t, b, s, tz, year, month))
    last = calendar.monthrange(year, month)[1]
    assert 1 not in resp
    assert last in resp


# --------------------------------------------------------------------------- #
# Hold expiry racing a second booker
# --------------------------------------------------------------------------- #
def _add_hold(session, t, b, s, svc, *, start, minutes, expires_at, status="active"):
    h = AppointmentHold(
        tenant_id=t.id, branch_id=b.id, service_id=svc.id,
        any_stylist=False, stylist_id=s.id,
        start_time=start, end_time=start + timedelta(minutes=minutes),
        duration_minutes=minutes, status=status,
        hold_token=uuid.uuid4().hex, expires_at=expires_at,
    )
    session.add(h)
    session.flush()
    return h


def test_active_hold_blocks_the_day(session):
    year, month = 2035, 7
    t, b, s, tz = _build(session, "UTC", wh=("09:00", "10:00"))
    svc = add_service(session, t, stylists=[s])   # service_id for the hold FK
    day = 15
    _add_hold(session, t, b, s, svc,
              start=datetime(year, month, day, 9, 0), minutes=60,
              expires_at=utc_now() + timedelta(minutes=10))
    resp = _avail(_run(t, b, s, tz, year, month))
    assert day not in resp, "an active hold did not block the held day"


def test_expired_hold_frees_the_day_for_the_next_booker(session):
    """An abandoned hold whose expires_at has passed must NOT block the slot —
    get_active_holds_query filters it out, so the next booker sees the day free."""
    year, month = 2035, 7
    t, b, s, tz = _build(session, "UTC", wh=("09:00", "10:00"))
    svc = add_service(session, t, stylists=[s])
    day = 15
    _add_hold(session, t, b, s, svc,
              start=datetime(year, month, day, 9, 0), minutes=60,
              expires_at=utc_now() - timedelta(minutes=1))  # already expired
    resp = _avail(_run(t, b, s, tz, year, month))
    assert day in resp, "an expired hold wrongly kept the day blocked"


def test_released_hold_does_not_block(session):
    """A hold that was explicitly released (status != active) also frees the slot."""
    year, month = 2035, 7
    t, b, s, tz = _build(session, "UTC", wh=("09:00", "10:00"))
    svc = add_service(session, t, stylists=[s])
    day = 15
    _add_hold(session, t, b, s, svc,
              start=datetime(year, month, day, 9, 0), minutes=60,
              expires_at=utc_now() + timedelta(minutes=10), status="released")
    resp = _avail(_run(t, b, s, tz, year, month))
    assert day in resp, "a released hold wrongly kept the day blocked"


# --------------------------------------------------------------------------- #
# Fully-booked days are flagged `full` — and only those
# --------------------------------------------------------------------------- #
def test_fully_booked_working_day_is_flagged_full_but_closed_days_are_not(session):
    """The cancellation waitlist's public entry point is the fully-booked day's empty
    state, so the calendar has to tell a FULL day (open, rostered, no gap) apart from
    a day that simply isn't offered — the first must stay selectable, the second not."""
    year = 2035
    t, b, s, tz = _build(session, "America/New_York", wh=("09:00", "10:00"))
    # Only open Mon–Fri for this one: a weekend day is closed, not full.
    b.working_hours = {d: (["09:00", "10:00"] if d in ("mon", "tue", "wed", "thu", "fri") else None)
                       for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    session.flush()
    # Book the single hour of the first Wednesday solid.
    weds = [d for d in range(1, 32) if date(year, 7, d).weekday() == 2]
    full_day = weds[0]
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(year, 7, full_day, 9, 0), minutes=60)
    resp = {r["day"]: r for r in _run(t, b, s, tz, year, 7)}

    assert resp[full_day]["available"] is False
    assert resp[full_day].get("full") is True
    # a free weekday: available, not full
    assert resp[full_day + 1]["available"] is True and "full" not in resp[full_day + 1]
    # a closed weekend day: neither
    sat = next(d for d in range(1, 32) if date(year, 7, d).weekday() == 5)
    assert resp[sat]["available"] is False and "full" not in resp[sat]


def test_a_day_already_behind_the_lead_time_is_not_full(session):
    """Today after closing: nothing left to book, but that is 'gone', not 'full' —
    offering a waitlist for a day that is over would be wrong."""
    t, b, s, tz = _build(session, "America/New_York", wh=("09:00", "10:00"))
    now = tz.localize(datetime(2035, 7, 10, 12, 0))   # noon, shop closed at 10:00
    resp = {r["day"]: r for r in compute_month_availability(
        tenant_id=t.id, branch=b, tz=tz, year=2035, month=7,
        target_stylist_ids=[s.id], any_stylist=False, stylist_id=s.id,
        duration_min=30, buffer_min=0, step_minutes=5, lead_minutes=0,
        now_utc=now.astimezone(pytz.UTC), now_local=now,
        customer_id=None, exclude_appointment_id=None,
        bookable_from_by_sid=None, is_day_offered=lambda d: True)}
    assert resp[10]["available"] is False and "full" not in resp[10]
    assert resp[11]["available"] is True


# --------------------------------------------------------------------------- #
# Half-hour-offset zones — the grid is the branch-local clock
# --------------------------------------------------------------------------- #
def test_half_hour_offset_zone_keeps_a_day_whose_only_slot_is_on_the_local_hour(session):
    """Open 09:00–10:00 in Asia/Kolkata (+5:30), 60-minute service on a 60-minute
    increment: the one valid start is 09:00 local = 03:30 UTC. Aligning the grid on
    UTC minutes bumped it to 04:00 UTC (09:30 local), where 60 minutes no longer fit
    — every day in the month read as unavailable."""
    year = date.today().year + 3
    t, b, s, tz = _build(session, "Asia/Kolkata", wh=("09:00", "10:00"))
    resp = _avail(_run(t, b, s, tz, year, 7, min_gap_min=60, step_minutes=60))
    assert resp == set(range(1, 32))
