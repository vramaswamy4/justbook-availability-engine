"""EXCERPT of production `app/utils/appointment_helpers.py`.

The production module is ~1,700 lines of booking helpers (customers, catalog,
serializers, totals, …). These are the functions the availability engine imports plus
the WRITE-side guard that pairs with it — the code that actually prevents a
double-booking. Every function body below is copied verbatim; only this header and
the import block were written for this repository.

  read side   get_working_hours_for_date, get_active_holds_query   (engine imports)
  time zones  resolve_branch_tz, branch_local_now
  write side  booking_lock_ids, lock_booking_slot,
              check_slot_has_conflicts, check_stylist_is_available
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, date, time as dt_time
from typing import List, Tuple, Optional

import pytz
from sqlalchemy import or_, text

from app import db
from app.models.models import (
    Appointment, AppointmentHold, AppointmentStatusDefinition, Branch,
    Stylist, StylistAvailability, StylistTimeOff, utc_now,
)


# ---------------------------------------------------------------------------
# Concurrency — the booking lock
# ---------------------------------------------------------------------------

def booking_lock_ids(tenant_id, branch_id, start, end=None, buffer_minutes=0) -> List[int]:
    """The advisory-lock ids a booking of [start, end) must hold: one per
    branch-local DATE its buffer-widened window touches, ascending.

    Two bookings can only conflict if their widened windows share an instant, and a
    shared instant is a shared date — so any two writes that could double-book hold
    at least one id in common, whatever their start times. (The key used to be the
    exact start, which serialized 40 clients racing ONE slot but not 10:00–11:00
    against 10:30–11:30: different keys, both pass the conflict check, both insert.)

    Ids are a 64-bit digest computed here rather than Postgres `hashtext` (32-bit) so
    they can be SORTED before acquisition: every transaction takes its ids in the
    same global order, which is what makes a multi-lock booking (one spanning
    midnight) unable to deadlock against another.
    """
    def _naive(dt):
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    buf = timedelta(minutes=int(buffer_minutes or 0))
    start = _naive(start)
    end = _naive(end) if end is not None else start
    first = (start - buf).date()
    # half-open: a window ending exactly at midnight does not touch the next day
    last = max(first, (end + buf - timedelta(microseconds=1)).date())

    ids = []
    day = first
    while day <= last:
        key = f"book:{tenant_id}:{branch_id}:{day.isoformat()}".encode()
        ids.append(int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(),
                                  "big", signed=True))
        day += timedelta(days=1)
    return sorted(ids)


def lock_booking_slot(tenant_id, branch_id, start, end=None, buffer_minutes=0):
    """Serialize concurrent bookings that could conflict at one branch.

    Transaction-scoped Postgres advisory lock(s) taken before the conflict-check →
    insert critical section in EVERY write that lands an appointment on a slot
    (public confirm, admin create/update, walk-in seat, portal reschedule). Without
    it, two bookings racing one slot both pass the conflict check before either
    commits and both insert — a double-book (reproduced by the Phase 5 same-slot load
    test). The loser blocks until the winner commits, then sees the conflict → a
    clean 409.

    Keyed on branch + local date (see `booking_lock_ids`), not on the stylist, so it
    also serializes the any_stylist selection path; not on the start time, so
    overlapping bookings with different starts serialize too. The cost is that two
    unrelated bookings at the same branch on the same day queue behind each other
    for the length of one check-and-insert — negligible at salon volumes, and the
    reason this is a lock rather than an exclusion constraint is that a walk-in may
    deliberately override a conflict (`override_conflict`), which a constraint
    could not allow. Pass the UNBUFFERED service window and the tenant's buffer.
    Auto-released on commit/rollback.
    """
    for lock_id in booking_lock_ids(tenant_id, branch_id, start, end, buffer_minutes):
        db.session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------

def resolve_branch_tz(branch, settings) -> pytz.BaseTzInfo:
    """Canonical branch timezone resolution: branch override → tenant default → UTC.
    Appointment times are stored as naive *branch-local* wall time, so every
    now-vs-start_time comparison must go through this (never utc_now() directly)."""
    tzname = (
        (branch.timezone if branch and branch.timezone else None)
        or (getattr(settings, "default_timezone", None) if settings else None)
        or "UTC"
    )
    try:
        return pytz.timezone(tzname)
    except Exception:
        return pytz.UTC


def branch_local_now(branch, settings) -> datetime:
    """Naive branch-local "now" — the correct comparand for stored start/end times."""
    return datetime.now(resolve_branch_tz(branch, settings)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Working hours
# ---------------------------------------------------------------------------

def _special_hours_blocks(special) -> Optional[List[Tuple[dt_time, dt_time]]]:
    """Translate a BranchSpecialHours row into working-hours blocks, or None if the
    row doesn't apply (caller should fall back to the weekly schedule). An override
    that is `is_closed` returns [] (explicitly closed)."""
    if special is None:
        return None
    if special.is_closed:
        return []
    try:
        sh, sm = map(int, special.open_time.split(":"))
        eh, em = map(int, special.close_time.split(":"))
        return [(dt_time(sh, sm), dt_time(eh, em))]
    except Exception:
        return []


def get_working_hours_for_date(
    branch: Branch, date_obj: date, *, special_hours_by_date=None
) -> List[Tuple[dt_time, dt_time]]:
    """
    Get working hours for a specific date. Special hours override the regular weekly schedule.

    Returns: list[(start_time, end_time)] as datetime.time objects, empty list = closed.

    ``special_hours_by_date``: an optional {date: BranchSpecialHours-or-None} map,
    pre-loaded by a caller iterating many dates (the month availability engine), so
    the per-date special-hours lookup doesn't fire one query per day (an N+1 on the
    hot calendar reads). When provided, a missing key means "not loaded" and we fall
    back to a single-row query; an explicit None value means "no override for this
    date" (skip the query).
    """
    if not branch:
        return []

    # Check for a special hours override first.
    from app.models.models import BranchSpecialHours
    if special_hours_by_date is not None and date_obj in special_hours_by_date:
        special = special_hours_by_date[date_obj]
    else:
        special = BranchSpecialHours.query.filter_by(
            branch_id=branch.id, date=date_obj
        ).first()
    blocks = _special_hours_blocks(special)
    if blocks is not None:
        return blocks

    # Fall back to regular weekly working hours
    if not branch.working_hours:
        return []

    key = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][date_obj.weekday()]
    day_val = branch.working_hours.get(key)
    if not isinstance(day_val, list) or len(day_val) != 2:
        return []

    start_s, end_s = (day_val[0] or "").strip(), (day_val[1] or "").strip()
    if not start_s or not end_s:
        return []

    try:
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        return [(dt_time(sh, sm), dt_time(eh, em))]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Conflicts (write path)
# ---------------------------------------------------------------------------

def get_active_holds_query(now_utc: Optional[datetime] = None):
    """
    Return a query for active appointment holds.
    """
    if now_utc is None:
        now_utc = utc_now()
    return AppointmentHold.query.filter(
        AppointmentHold.status == 'active',
        AppointmentHold.expires_at > now_utc
    )


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """
    Check if two time intervals overlap.
    """
    return (a_start < b_end) and (b_start < a_end)


def check_slot_has_conflicts(
    tenant_id: int,
    branch_id: int,
    service_id: int,
    stylist_id: Optional[int],
    any_stylist: bool,
    stylist_ids_for_service: Optional[List[int]],
    start_dt: datetime,
    end_dt: datetime,
    buffer_minutes: int = 0,
    exclude_appointment_id: Optional[int] = None,
) -> bool:
    """
    Comprehensive conflict check for a time slot.
    Checks appointments, holds, and stylist availability/time-off.

    All datetimes should be NAIVE LOCAL (matching DB columns). ``start_dt``/``end_dt``
    are the UNBUFFERED service window — ``buffer_minutes`` is applied here (the
    appointment/hold overlap window is widened by the buffer on both sides, giving
    the documented "gap around each appointment": at least one buffer between the
    new slot and any neighbour). Don't pre-buffer the end at the call site.

    Holds block regardless of ``service_id`` — a held slot is a held stylist, the
    same rule every availability read applies. (The param is kept for signature
    stability; it no longer filters.)

    Returns True if there's a conflict, False if slot is free.

    Args:
        exclude_appointment_id: Optional appointment ID to exclude from conflict checks
                                (useful when rescheduling an existing appointment)
    """
    now_utc = utc_now()
    buf = timedelta(minutes=buffer_minutes or 0)

    def _stylist_is_busy(sid: int) -> bool:
        # Appointments: only active ones block — Cancelled releases the slot;
        # a NULL status still blocks (outerjoin), matching every read path.
        appt_q = (
            Appointment.query
            .outerjoin(AppointmentStatusDefinition, Appointment.status_id == AppointmentStatusDefinition.id)
            .filter(
                Appointment.tenant_id == tenant_id,
                Appointment.branch_id == branch_id,
                Appointment.stylist_id == sid,
                Appointment.start_time < end_dt + buf,
                Appointment.end_time > start_dt - buf,
                or_(
                    AppointmentStatusDefinition.id.is_(None),
                    AppointmentStatusDefinition.name != "Cancelled"
                )
            )
        )
        if exclude_appointment_id:
            appt_q = appt_q.filter(Appointment.id != exclude_appointment_id)
        if db.session.query(appt_q.exists()).scalar():
            return True

        hold_q = get_active_holds_query(now_utc).filter(
            AppointmentHold.tenant_id == tenant_id,
            AppointmentHold.branch_id == branch_id,
            AppointmentHold.start_time < end_dt + buf,
            AppointmentHold.end_time > start_dt - buf,
            or_(
                AppointmentHold.any_stylist == True,  # noqa: E712
                AppointmentHold.stylist_id == sid
            )
        )
        return db.session.query(hold_q.exists()).scalar()

    if any_stylist:
        # Free if at least ONE qualified stylist is working and unbooked — the same
        # rule the availability reads apply. (This used to run a branch-wide
        # appointment check first, so any unrelated stylist's booking 409'd a slot
        # the day grid legitimately offered.)
        for sid in (stylist_ids_for_service or []):
            if not check_stylist_is_available(sid, branch_id, start_dt, end_dt):
                continue
            if not _stylist_is_busy(sid):
                return False  # At least one stylist can take it
        return True  # All stylists conflicted (or none supplied)
    else:
        if _stylist_is_busy(stylist_id):
            return True
        # Specific stylist must be working & not on time off
        return not check_stylist_is_available(stylist_id, branch_id, start_dt, end_dt)


def check_stylist_is_available(stylist_id: int, branch_id: int, start_dt: datetime, end_dt: datetime) -> bool:
    """
    Check if stylist is available (bookable, not on time-off, has working hours)
    for a time slot.

    All datetimes should be NAIVE LOCAL (matching DB columns).
    """
    # Offboarded stylists take no bookings; bookable_from gates slots before the
    # stylist's booking start date (pre-booking later slots is allowed).
    row = db.session.query(Stylist.is_bookable, Stylist.bookable_from).filter(
        Stylist.id == stylist_id
    ).first()
    if not row or not row.is_bookable:
        return False
    if row.bookable_from and start_dt.date() < row.bookable_from:
        return False

    # Check time off overlaps
    timeoff_exists = db.session.query(
        StylistTimeOff.query.filter(
            StylistTimeOff.stylist_id == stylist_id,
            StylistTimeOff.branch_id == branch_id,
            StylistTimeOff.start_datetime < end_dt,
            StylistTimeOff.end_datetime > start_dt
        ).exists()
    ).scalar()
    if timeoff_exists:
        return False

    # Branch opening hours (special hours honoured) — defense in depth. Slot
    # GENERATION already intersects stylist × branch hours, so the public flow is
    # safe; but a directly-posted hold/confirm skips generation, and a stylist
    # whose shift runs past closing would otherwise pass this guard.
    branch = Branch.query.filter_by(id=branch_id).first()
    if not branch:
        return False
    loc_blocks = get_working_hours_for_date(branch, start_dt.date())
    if not loc_blocks:
        return False
    within_branch_hours = False
    for s_t, e_t in loc_blocks:
        open_dt = start_dt.replace(hour=s_t.hour, minute=s_t.minute, second=0, microsecond=0)
        close_dt = start_dt.replace(hour=e_t.hour, minute=e_t.minute, second=0, microsecond=0)
        if start_dt >= open_dt and end_dt <= close_dt:
            within_branch_hours = True
            break
    if not within_branch_hours:
        return False

    # Check weekly availability for the day
    dow = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][start_dt.weekday()]
    sa_rows = StylistAvailability.query.filter_by(
        stylist_id=stylist_id,
        branch_id=branch_id,
        day_of_week=dow
    ).all()
    if not sa_rows:
        return False

    # Slot must fit fully inside at least one availability block
    for row in sa_rows:
        try:
            sh, sm = map(int, str(row.start_time).split(':')[:2])
            eh, em = map(int, str(row.end_time).split(':')[:2])
            block_start = start_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
            block_end = start_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
            if start_dt >= block_start and end_dt <= block_end:
                return True
        except Exception:
            continue

    return False

