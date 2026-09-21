"""compute_day_slots — the extracted day-slot generator.

The generator used to be the inline body of `public_day_slots`, which meant the only way
to ask "what slots exist on this day" was to serve an HTTP request. The cancellation
waitlist has to ask exactly that when a cancellation frees a slot, and it must get the
answer the booking page would give.

In production the load-bearing test in this file is `test_route_payload_matches_engine`,
which pins the HTTP route's `slots` array to a direct engine call with the same resolved
inputs, so neither side can reimplement slot finding. The route is not part of this
extract, so that one test is omitted here. The rest are unchanged and cover the
properties the waitlist matcher depends on: grid alignment (on the branch-local clock),
the buffer-extended conflict, `available_stylist` under any-stylist, and the
cancelled-status rule that makes backfill possible at all.
"""
from datetime import date, datetime, timedelta

import pytz

from app.models.models import AppointmentStatusDefinition
from app.utils.availability_engine import compute_day_slots
from tests.factories import (
    make_tenant, add_appointment, add_branch, add_hours, add_service, add_stylist,
)

TZ = "UTC"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fixture(session, *, wh=("09:00", "17:00"), n_stylists=1):
    t = make_tenant(session)
    b = add_branch(session, t)
    b.timezone = TZ
    b.working_hours = {d: list(wh) for d in DAYS}
    stylists = []
    for _ in range(n_stylists):
        s = add_stylist(session, t)
        for d in DAYS:
            add_hours(session, s, b, d, start=wh[0], end=wh[1])
        stylists.append(s)
    svc = add_service(session, t, stylists=stylists)
    session.commit()
    return t, b, stylists, svc


def _target_date():
    # Inside the default 60-day public booking window, and never "today" (so a
    # lead-time floor can't make the expectations time-of-day dependent).
    return date.today() + timedelta(days=5)


def _run(t, b, stylists, d, *, duration_min=30, buffer_min=0, step_minutes=30,
         any_stylist=False, lead_minutes=0, now_utc=None, stylist_names=None):
    tz = pytz.timezone(TZ)
    ids = [s.id for s in stylists]
    return compute_day_slots(
        tenant_id=t.id, branch=b, tz=tz, date_obj=d,
        target_stylist_ids=ids,
        stylist_names=stylist_names or {s.id: f"S{s.id}" for s in stylists},
        any_stylist=any_stylist, stylist_id=ids[0],
        duration_min=duration_min, buffer_min=buffer_min,
        step_minutes=step_minutes, lead_minutes=lead_minutes,
        now_utc=now_utc or (tz.localize(datetime.combine(d, datetime.min.time()))
                            - timedelta(days=1)).astimezone(pytz.UTC),
    )


def _starts(slots):
    return [s["start"][11:16] for s in slots]


# --------------------------------------------------------------------------- #
# Properties the waitlist matcher relies on
# --------------------------------------------------------------------------- #

def test_slots_are_grid_aligned_and_end_excludes_buffer(app, session):
    t, b, stylists, _ = _fixture(session)
    d = _target_date()

    slots = _run(t, b, stylists, d, duration_min=30, buffer_min=15, step_minutes=30)

    assert _starts(slots)[:3] == ["09:00", "09:30", "10:00"]
    # end is the SERVICE end — the 15-minute buffer widens conflicts, it is never shown
    assert slots[0]["end"][11:16] == "09:30"
    # last start on the 30-minute grid that still fits duration + buffer before 17:00
    # (16:00 + 45m = 16:45; 16:30 would run to 17:15)
    assert _starts(slots)[-1] == "16:00"


def test_buffer_extends_the_conflict_not_the_slot(app, session):
    """A 30-minute booking with a 15-minute buffer also eats the following 15 minutes."""
    t, b, stylists, _ = _fixture(session)
    d = _target_date()
    add_appointment(session, t, stylist=stylists[0], branch=b,
                    start=datetime(d.year, d.month, d.day, 10, 0), minutes=30)
    session.commit()

    starts = _starts(_run(t, b, stylists, d, duration_min=30, buffer_min=15,
                          step_minutes=15))

    assert "10:00" not in starts          # the booking itself
    assert "10:30" not in starts          # inside the buffer that follows it
    assert "10:45" in starts              # first start clear of booking + buffer
    assert "09:30" not in starts          # 09:30 + 30m + 15m buffer would run to 10:15
    # 09:15 IS offered: it ends at 09:45 and its buffer runs to exactly 10:00, which
    # is back-to-back with the booking, not an overlap.
    assert "09:15" in starts


def test_cancelled_frees_the_slot_but_no_show_does_not(app, session):
    """The whole feature rests on this: Cancelled releases, everything else blocks."""
    t, b, stylists, _ = _fixture(session)
    d = _target_date()
    s = stylists[0]

    # (Production seeds the tenant's default statuses via a helper and adds "No Show"
    # with its default colour; the engine only ever reads the status NAME.)
    from tests.factories import add_status
    cancelled = add_status(session, t, "Cancelled")
    no_show = add_status(session, t, "No Show")

    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(d.year, d.month, d.day, 9, 0), minutes=60,
                    status=cancelled)
    add_appointment(session, t, stylist=s, branch=b,
                    start=datetime(d.year, d.month, d.day, 13, 0), minutes=60,
                    status=no_show)
    session.commit()

    starts = _starts(_run(t, b, [s], d, duration_min=30, step_minutes=30))

    assert "09:00" in starts, "a cancelled appointment must release its slot"
    assert "13:00" not in starts, "a no-show still occupies the chair"


def test_any_stylist_lists_every_free_stylist(app, session):
    t, b, stylists, _ = _fixture(session, n_stylists=2)
    d = _target_date()
    busy, free = stylists

    add_appointment(session, t, stylist=busy, branch=b,
                    start=datetime(d.year, d.month, d.day, 9, 0), minutes=60)
    session.commit()

    slots = _run(t, b, stylists, d, any_stylist=True, duration_min=30, step_minutes=30)
    by_start = {s["start"][11:16]: s for s in slots}

    # 09:00 survives on the free stylist alone
    assert [x["id"] for x in by_start["09:00"]["available_stylist"]] == [free.id]
    # once the booking is over, both are offered
    assert {x["id"] for x in by_start["10:00"]["available_stylist"]} == {busy.id, free.id}


def test_no_working_hours_returns_no_slots(app, session):
    t, b, stylists, _ = _fixture(session)
    b.working_hours = {}
    session.commit()

    assert _run(t, b, stylists, _target_date()) == []


def test_lead_time_floor_pushes_the_first_slot(app, session):
    """Same-day: slots inside the lead-time window are not offered."""
    t, b, stylists, _ = _fixture(session)
    tz = pytz.timezone(TZ)
    d = _target_date()
    # Pretend "now" is 09:10 on the target day with a 60-minute lead time: the first
    # bookable grid start is 10:30 (09:10 + 60m = 10:10, rounded up to the 30m grid).
    now = tz.localize(datetime(d.year, d.month, d.day, 9, 10)).astimezone(pytz.UTC)

    starts = _starts(_run(t, b, stylists, d, duration_min=30, step_minutes=30,
                          lead_minutes=60, now_utc=now))

    assert starts[0] == "10:30"


def test_grid_is_the_branch_local_clock_in_a_half_hour_offset_zone(app, session):
    """The booking increment divides the minutes the CUSTOMER reads. In a +5:30 zone
    09:00 local is 03:30 UTC, so aligning on UTC minutes bumped a 60-minute grid to
    09:30, 10:30, … (and a 20-minute grid to 09:10, 09:30, …)."""
    t, b, stylists, _ = _fixture(session)
    b.timezone = "Asia/Kolkata"
    session.commit()
    tz = pytz.timezone("Asia/Kolkata")
    d = _target_date()
    now = (tz.localize(datetime.combine(d, datetime.min.time()))
           - timedelta(days=1)).astimezone(pytz.UTC)

    def starts(step):
        return _starts(compute_day_slots(
            tenant_id=t.id, branch=b, tz=tz, date_obj=d,
            target_stylist_ids=[stylists[0].id],
            stylist_names={stylists[0].id: "S"},
            any_stylist=False, stylist_id=stylists[0].id,
            duration_min=30, buffer_min=0, step_minutes=step,
            lead_minutes=0, now_utc=now,
        ))

    assert starts(60)[:3] == ["09:00", "10:00", "11:00"]
    assert starts(20)[:3] == ["09:00", "09:20", "09:40"]
    assert starts(30)[:3] == ["09:00", "09:30", "10:00"]


def test_lead_time_rounds_up_to_the_local_grid_in_a_half_hour_offset_zone(app, session):
    t, b, stylists, _ = _fixture(session)
    b.timezone = "Asia/Kolkata"
    session.commit()
    tz = pytz.timezone("Asia/Kolkata")
    d = _target_date()
    # 09:10 local + 60m lead = 10:10 local → next 60-minute LOCAL boundary is 11:00.
    now = tz.localize(datetime(d.year, d.month, d.day, 9, 10)).astimezone(pytz.UTC)

    slots = compute_day_slots(
        tenant_id=t.id, branch=b, tz=tz, date_obj=d,
        target_stylist_ids=[stylists[0].id], stylist_names={stylists[0].id: "S"},
        any_stylist=False, stylist_id=stylists[0].id,
        duration_min=30, buffer_min=0, step_minutes=60, lead_minutes=60, now_utc=now,
    )

    assert _starts(slots)[0] == "11:00"
