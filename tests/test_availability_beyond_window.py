"""compute_month_availability tags beyond-window days when beyond_window_fn is given.

The admin month calendar relies on this flag to tint past-window days as override
candidates; public omits the callable and must not gain the key. This pins the wiring
seam between booking_rules and the engine that the pure unit tests can't reach.

"""
from datetime import date, datetime

import pytz

from app.utils.availability_engine import compute_month_availability
from app.utils.booking_rules import is_day_beyond_window
from tests.factories import make_tenant, add_branch


def _run(session, *, beyond_window_fn):
    tenant = make_tenant(session)
    branch = add_branch(session, tenant)
    session.commit()
    tz = pytz.UTC
    now_local = datetime(2026, 7, 15, 9, 0, tzinfo=tz)
    return compute_month_availability(
        tenant_id=tenant.id, branch=branch, tz=tz, year=2026, month=9,
        target_stylist_ids=[], any_stylist=True, stylist_id=None,
        duration_min=30, buffer_min=0, step_minutes=5, lead_minutes=0,
        now_utc=now_local, now_local=now_local,
        is_day_offered=lambda d: d >= now_local.date(),
        beyond_window_fn=beyond_window_fn,
    )


def test_admin_days_carry_beyond_window_flag(session):
    # window 60 days from Jul 15 → Sep 13 is the last in-window day.
    settings = type("S", (), {"booking_window_days": 60})()
    today = date(2026, 7, 15)
    days = _run(session, beyond_window_fn=lambda d: is_day_beyond_window(d, settings, today))
    by_day = {d["day"]: d for d in days}
    assert "beyond_window" in by_day[13]           # every day has the key
    assert by_day[13]["beyond_window"] is False     # Sep 13 = day 60, in-window
    assert by_day[14]["beyond_window"] is True      # Sep 14 = day 61, beyond


def test_public_days_omit_beyond_window_flag(session):
    # No callable (public) → the key must not appear, keeping the public shape lean.
    days = _run(session, beyond_window_fn=None)
    assert all("beyond_window" not in d for d in days)
