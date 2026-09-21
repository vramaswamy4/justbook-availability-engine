"""Query-budget / N+1 guard.

Instruments SQLAlchemy's before_cursor_execute to count queries around a hot read
path and asserts it stays under a budget. Distinct from load-testing: this catches
the *cause* (a new N+1) deterministically, not just the symptom (latency).

The engine `compute_month_availability` is the expensive read that backs both the
public month calendar and the admin calendar month view. It is deliberately written
to BULK-LOAD its conflicts (appointments, holds, time-off, weekly availability,
customer conflicts) up front and then iterate in memory — so its query count must be
(a) small and (b) FLAT as the data grows. A regression that moves a query inside the
per-stylist or per-day loop shows up here as the count scaling with the inputs.

"""
from datetime import datetime, timedelta

import pytest
import pytz
from sqlalchemy import event

from app.extensions import db
from app.models.models import Stylist
from app.utils.availability_engine import compute_day_slots, compute_month_availability
from tests.factories import (
    make_tenant, add_appointment, add_branch, add_hours, add_service, add_stylist,
)

UTC = pytz.UTC
YEAR, MONTH = 2035, 7


class _QueryCounter:
    """Context manager counting DB round-trips via before_cursor_execute."""
    def __init__(self):
        self.count = 0
        self.statements = []

    def _on_exec(self, conn, cursor, statement, params, context, executemany):
        self.count += 1
        self.statements.append(statement)

    def __enter__(self):
        event.listen(db.engine, "before_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *exc):
        event.remove(db.engine, "before_cursor_execute", self._on_exec)
        return False


def _branch_with_stylists(session, n_stylists, *, wh=("09:00", "17:00")):
    t = make_tenant(session)
    b = add_branch(session, t)
    b.timezone = "UTC"
    b.working_hours = {d: list(wh) for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    stylists = []
    for _ in range(n_stylists):
        s = add_stylist(session, t)
        for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            add_hours(session, s, b, d, start=wh[0], end=wh[1])
        stylists.append(s)
    svc = add_service(session, t, stylists=stylists)
    session.flush()
    stylist_ids = [s.id for s in stylists]
    return t, b, svc, stylist_ids


def _run_month(t, b, stylist_ids, *, any_stylist=True):
    now = UTC.localize(datetime(YEAR, MONTH - 1, 1, 0, 0))
    return compute_month_availability(
        tenant_id=t.id, branch=b, tz=UTC, year=YEAR, month=MONTH,
        target_stylist_ids=list(stylist_ids),
        any_stylist=any_stylist, stylist_id=stylist_ids[0],
        duration_min=30, buffer_min=0, step_minutes=5, lead_minutes=0,
        now_utc=now, now_local=now,
        customer_id=None, exclude_appointment_id=None,
        bookable_from_by_sid=None, is_day_offered=lambda d: True,
    )


# Budget: the engine issues a fixed, small set of bulk queries (appointments, holds,
# time-off, weekly availability, and the working-hours/special-hours reads). Well
# under this ceiling; a new N+1 blows past it.
MONTH_AVAILABILITY_BUDGET = 15


# The engine takes plain ids + a live branch object; we pass captured ids (not ORM
# instances) so the *test's* attribute access can't itself leak a lazy reload into
# the count. What's measured is exactly the engine's own SQL.
def test_month_availability_under_budget(session):
    t, b, _svc, sids = _branch_with_stylists(session, 3)
    session.commit()
    with _QueryCounter() as qc:
        _run_month(t, b, sids)
    assert qc.count <= MONTH_AVAILABILITY_BUDGET, (
        f"month availability issued {qc.count} queries (budget {MONTH_AVAILABILITY_BUDGET}); "
        f"an N+1 likely crept in.\n" + "\n".join(qc.statements)
    )


def test_month_availability_query_count_is_flat_in_stylist_count(session):
    """The bulk-load design means adding stylists must NOT add queries. If the count
    grows with the roster, a query moved inside the per-stylist loop — the N+1 the
    engine was built to avoid."""
    t3, b3, _s, s3 = _branch_with_stylists(session, 3)
    session.commit()
    with _QueryCounter() as qc3:
        _run_month(t3, b3, s3)

    t8, b8, _s, s8 = _branch_with_stylists(session, 8)
    session.commit()
    with _QueryCounter() as qc8:
        _run_month(t8, b8, s8)

    assert qc8.count == qc3.count, (
        f"query count scaled with stylist roster (3 stylists: {qc3.count}, "
        f"8 stylists: {qc8.count}) — an N+1 over stylists"
    )


def test_month_availability_query_count_is_flat_in_appointment_count(session):
    """Adding appointments (conflicts) must not add queries either — they're bulk
    loaded once, not fetched per day/per stylist."""
    t, b, _svc, sids = _branch_with_stylists(session, 3, wh=("09:00", "17:00"))
    session.commit()
    with _QueryCounter() as qc_empty:
        _run_month(t, b, sids)

    # Scatter 20 appointments across the month for the first stylist.
    first = db.session.get(Stylist, sids[0])
    for d in range(1, 21):
        add_appointment(session, t, stylist=first, branch=b,
                        start=datetime(YEAR, MONTH, d, 11, 0), minutes=30)
    session.commit()
    with _QueryCounter() as qc_full:
        _run_month(t, b, sids)

    assert qc_full.count == qc_empty.count, (
        f"query count scaled with appointment volume (0 appts: {qc_empty.count}, "
        f"20 appts: {qc_full.count}) — an N+1 over conflicts"
    )


# --- Day slots ---------------------------------------------------------------
# ADAPTED for this repository. In production these two tests drive the HTTP route
# (`GET /api/public/availability`) and also cover the route's own lookups; the route
# is not part of this extract, so here they measure `compute_day_slots` directly.
# What they pin is the same: the slot builder must not query per slot or per
# stylist. (That N+1 shipped once — slots × stylists queries made one tenant's
# fully-open day view ~8× slower than a comparable tenant's.)
DAY_SLOTS_BUDGET = 10


def _run_day(t, b, stylist_ids):
    now = UTC.localize(datetime(YEAR, MONTH - 1, 1, 0, 0))
    return compute_day_slots(
        tenant_id=t.id, branch=b, tz=UTC, date_obj=datetime(YEAR, MONTH, 15).date(),
        target_stylist_ids=list(stylist_ids),
        stylist_names={sid: f"S{sid}" for sid in stylist_ids},
        any_stylist=True, stylist_id=None,
        duration_min=30, buffer_min=0, step_minutes=15, lead_minutes=0, now_utc=now,
    )


def test_day_slots_under_budget_and_returns_slots(session):
    t, b, _svc, sids = _branch_with_stylists(session, 3)
    session.commit()
    with _QueryCounter() as qc:
        slots = _run_day(t, b, sids)
    assert slots, "expected a fully-open day to return slots"
    assert all(s["stylist_name"] for s in slots)
    assert qc.count <= DAY_SLOTS_BUDGET, (
        f"day slots issued {qc.count} queries for {len(slots)} slots "
        f"(budget {DAY_SLOTS_BUDGET}); a per-slot N+1 likely crept in.\n"
        + "\n".join(qc.statements)
    )


def test_day_slots_query_count_is_flat_in_stylist_count(session):
    t2, b2, _s, s2 = _branch_with_stylists(session, 2)
    session.commit()
    with _QueryCounter() as qc2:
        _run_day(t2, b2, s2)

    t8, b8, _s, s8 = _branch_with_stylists(session, 8)
    session.commit()
    with _QueryCounter() as qc8:
        _run_day(t8, b8, s8)

    assert qc8.count == qc2.count, (
        f"query count scaled with stylist roster (2 stylists: {qc2.count}, "
        f"8 stylists: {qc8.count}) — a per-slot N+1 over stylists"
    )
