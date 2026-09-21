"""Shared availability engine — one month-grid computation, one day-slot generator.

admin_calendar_days (appointments/availability.py) and public_calendar_days
(public/availability.py) computed "which days this month have a bookable gap" with
~200 lines of near-identical timezone math each. This is the single implementation both
call. The routes still own resolution + guards (auth vs subdomain, branch access vs
draft-branch, tz derivation) and pass the resolved inputs in.

`compute_day_slots` is the same deal for the day list. It was the inline body of
public_day_slots, which meant the *only* way to ask "what slots exist on this day" was to
serve an HTTP request. The cancellation waitlist has to answer exactly that question for
each waiting entry when a slot frees up, and it must get the same answer the booking page
would give — so the generator moved here and the route calls it. Do NOT reimplement slot
finding with window arithmetic ("the freed gap is >= duration"): that is the bug
_grid_slot_exists exists to prevent, and a waitlist entry's service duration differs from
the cancelled appointment's, so the grid alignment differs too.

The two endpoints differ only in:
  - which days are offered  → `is_day_offered(day_date)` predicate
      (admin: any non-past date; public: inside the booking window)
  - stylist start dates      → `bookable_from_by_sid` (public passes the real map;
      admin passes {} so the check is a no-op)
Everything else — the bulk conflict load and the per-day / per-stylist slot-existence
check — is identical. The check (_grid_slot_exists) simulates the day-slots generator
exactly (grid alignment, buffer-extended appointment/hold conflicts, lead-time floor),
so a day the calendar offers always has at least one slot in the day list.
"""
from datetime import datetime, timedelta, time as dt_time

import pytz
from sqlalchemy import or_

from app import db
from app.models.models import (
    Appointment, AppointmentHold, AppointmentStatusDefinition,
    StylistAvailability, StylistTimeOff, utc_now,
)
from app.utils.appointment_helpers import (
    get_active_holds_query, get_working_hours_for_date,
)

_DOW = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def _first_grid_start(t_utc, tz, step_minutes):
    """Round a UTC-aware instant UP to the next booking-increment boundary on the
    BRANCH-LOCAL clock. The grid is the wall clock the customer reads ("9:00, 9:30"),
    so the minutes that must divide by the increment are the local ones. Taking them
    from the UTC value is only the same thing in whole-hour zones: a +5:30 branch
    (India, Adelaide) on a 60-minute increment opened at 09:00 local = 03:30 UTC, got
    bumped to 04:00 UTC, and offered 09:30, 10:30, … The one generator and the month
    check both start here so they cannot disagree about where the grid sits. Stepping
    from the result in UTC stays on the local grid — DST shifts are whole multiples of
    every allowed increment (Lord Howe's 30 minutes is the lone exception, and only
    for a block that spans its 02:00 transition)."""
    t = t_utc
    if t.second or t.microsecond:
        t = t.replace(second=0, microsecond=0)
    minute_mod = t.astimezone(tz).minute % step_minutes
    if minute_mod:
        t += timedelta(minutes=(step_minutes - minute_mod))
    return t


def _grid_slot_exists(block_start_utc, block_end_utc, conflicts_utc, earliest_utc,
                      step_minutes, dur_plus_buf, tz):
    """True when the DAY-SLOTS generator would emit at least one slot in this
    block. This deliberately mirrors compute_day_slots' loop exactly — grid-aligned
    starts (branch-local clock minutes % step, see _first_grid_start), [start, start +
    duration + buffer] fully inside the block, no overlap with the (already
    buffer-extended) conflicts, start no earlier than now + lead time. It replaced a
    plain "free run >= duration+buffer" check, which could pass on a gap that
    contained no valid grid start (e.g. the tail left behind a hold's buffered end) —
    the calendar then showed a clickable day whose slot list was empty (2026-07-18).
    All datetimes are UTC-aware."""
    t = _first_grid_start(max(block_start_utc, earliest_utc), tz, step_minutes)
    while t + dur_plus_buf <= block_end_utc:
        end = t + dur_plus_buf
        if not any(s < end and e > t for s, e in conflicts_utc):
            return True
        t += timedelta(minutes=step_minutes)
    return False


def compute_day_slots(
    *,
    tenant_id,
    branch,
    tz,
    date_obj,
    target_stylist_ids,
    stylist_names,
    any_stylist,
    stylist_id,
    duration_min,
    buffer_min,
    step_minutes,
    lead_minutes,
    now_utc,
    customer_appointments=None,
    exclude_appointment_id=None,
):
    """Return the bookable slots for one day, as the public booking page sees them.

    Slots look like::

        {"start": iso, "end": iso, "stylist_id": int|None, "stylist_name": str|None,
         "available_stylist": [{"id", "name"}], "available": True,
         "customer_conflict": {...}|None}

    ``start``/``end`` are branch-local ISO strings and ``end`` is the SERVICE end (no
    buffer) — the buffer only ever widens conflicts, it is never shown to a client.

    The caller owns resolution and guards: tenant/branch/service lookup, the draft-branch
    preview rule, tz derivation, and which stylists are in play. ``target_stylist_ids``
    must already be tenant- and branch-scoped and already filtered by ``bookable_from``
    (a stylist whose start date is after this day has no slots today); ``stylist_names``
    maps those ids to display names for the slot payload.

    ``customer_appointments`` are that customer's other appointments overlapping this day
    (ORM rows). Overlapping slots get a ``customer_conflict`` block instead of being
    dropped, so the UI can explain the clash. Pass None/[] when there is no customer
    context — every slot then carries ``customer_conflict: None``, which is also what a
    customer with no clashes gets.
    """
    customer_appointments = customer_appointments or []

    dur_plus_buf = timedelta(minutes=duration_min + buffer_min)
    buffer_td = timedelta(minutes=buffer_min)

    open_blocks = get_working_hours_for_date(branch, date_obj)
    if not open_blocks or not target_stylist_ids:
        return []

    blocks_utc = []
    for s_time, e_time in open_blocks:
        s_loc = tz.localize(datetime.combine(date_obj, s_time))
        e_loc = tz.localize(datetime.combine(date_obj, e_time))
        blocks_utc.append((s_loc.astimezone(pytz.UTC), e_loc.astimezone(pytz.UTC)))

    # Day range for SQL in NAIVE LOCAL (matches the DB columns).
    day_start_local = tz.localize(datetime.combine(date_obj, dt_time(0, 0))).replace(tzinfo=None)
    day_end_local = tz.localize(datetime.combine(date_obj, dt_time(23, 59, 59))).replace(tzinfo=None)

    # ---- bulk load overlaps (naive local in WHERE) ----
    appt_q = db.session.query(
        Appointment.stylist_id, Appointment.start_time, Appointment.end_time
    ).outerjoin(
        AppointmentStatusDefinition,
        Appointment.status_id == AppointmentStatusDefinition.id
    ).filter(
        Appointment.tenant_id == tenant_id,
        Appointment.branch_id == branch.id,
        Appointment.stylist_id.in_(target_stylist_ids),
        Appointment.start_time < day_end_local,
        Appointment.end_time > day_start_local,
        # Only exclude Cancelled — everything else (including Pending Deposit, no
        # status) blocks the slot. outerjoin so a NULL status still counts as active.
        or_(
            AppointmentStatusDefinition.id.is_(None),
            AppointmentStatusDefinition.name != "Cancelled",
        ),
    )
    if exclude_appointment_id:
        appt_q = appt_q.filter(Appointment.id != exclude_appointment_id)
    appts = appt_q.all()

    base_holds_q = get_active_holds_query(utc_now()).filter(
        AppointmentHold.tenant_id == tenant_id,
        AppointmentHold.branch_id == branch.id,
        AppointmentHold.start_time < day_end_local,
        AppointmentHold.end_time > day_start_local,
    )
    if any_stylist:
        stylist_match = or_(AppointmentHold.any_stylist == True,   # noqa: E712
                            AppointmentHold.stylist_id.in_(target_stylist_ids))
    else:
        stylist_match = or_(AppointmentHold.any_stylist == True,   # noqa: E712
                            AppointmentHold.stylist_id == stylist_id)
    holds = base_holds_q.filter(stylist_match).with_entities(
        AppointmentHold.stylist_id, AppointmentHold.any_stylist,
        AppointmentHold.start_time, AppointmentHold.end_time,
    ).all()

    timeoffs = db.session.query(
        StylistTimeOff.stylist_id, StylistTimeOff.start_datetime, StylistTimeOff.end_datetime
    ).filter(
        StylistTimeOff.branch_id == branch.id,
        StylistTimeOff.stylist_id.in_(target_stylist_ids),
        StylistTimeOff.start_datetime < day_end_local,
        StylistTimeOff.end_datetime > day_start_local,
    ).all()

    # Weekly availability -> UTC aware
    dow = _DOW[date_obj.weekday()]
    sa_rows = StylistAvailability.query.filter(
        StylistAvailability.branch_id == branch.id,
        StylistAvailability.stylist_id.in_(target_stylist_ids),
        StylistAvailability.day_of_week == dow,
    ).all()

    avail_blocks_utc = {sid: [] for sid in target_stylist_ids}
    for row in sa_rows:
        try:
            sh, sm = map(int, str(row.start_time).split(':')[:2])
            eh, em = map(int, str(row.end_time).split(':')[:2])
            s_loc = tz.localize(datetime.combine(date_obj, dt_time(sh, sm)))
            e_loc = tz.localize(datetime.combine(date_obj, dt_time(eh, em)))
            avail_blocks_utc[row.stylist_id].append(
                (s_loc.astimezone(pytz.UTC), e_loc.astimezone(pytz.UTC)))
        except Exception:
            continue

    # Normalize conflicts (DB stores NAIVE LOCAL) -> UTC aware
    def to_utc_aw(dt_naive):
        return tz.localize(dt_naive).astimezone(pytz.UTC)

    conflicts_by_stylist = {sid: [] for sid in target_stylist_ids}

    # Appointments — extend end by buffer
    for sid, s, e in appts:
        conflicts_by_stylist.setdefault(sid, []).append((to_utc_aw(s), to_utc_aw(e) + buffer_td))

    # Holds — extend end by buffer, and fan out if any_stylist
    for sid, anyflag, s, e in holds:
        s_utc, e_utc = to_utc_aw(s), to_utc_aw(e) + buffer_td
        if anyflag:
            for tid in target_stylist_ids:
                conflicts_by_stylist.setdefault(tid, []).append((s_utc, e_utc))
        else:
            conflicts_by_stylist.setdefault(sid, []).append((s_utc, e_utc))

    # Time off — do NOT extend by buffer (it already blocks its interval)
    for sid, s, e in timeoffs:
        conflicts_by_stylist.setdefault(sid, []).append((to_utc_aw(s), to_utc_aw(e)))

    def overlaps(a_start, a_end, b_start, b_end):
        return (a_start < b_end) and (b_start < a_end)

    def stylist_has_availability_utc(sid, s_utc, e_utc):
        for bs, be in avail_blocks_utc.get(sid, []):
            if s_utc >= bs and e_utc <= be:
                return True
        return False

    def stylist_is_free(sid, s_utc, e_utc):
        if not stylist_has_availability_utc(sid, s_utc, e_utc):
            return False
        return not any(overlaps(s_utc, e_utc, c_s, c_e)
                       for c_s, c_e in conflicts_by_stylist.get(sid, []))

    earliest_bookable_utc = now_utc + timedelta(minutes=int(lead_minutes or 0))

    slots = []
    for block_start_utc, block_end_utc in blocks_utc:
        t_utc = _first_grid_start(
            max(block_start_utc, earliest_bookable_utc), tz, step_minutes)

        while t_utc + dur_plus_buf <= block_end_utc:
            candidate_end_utc = t_utc + dur_plus_buf

            if any_stylist:
                available_stylist = [
                    {"id": sid, "name": stylist_names.get(sid)}
                    for sid in target_stylist_ids
                    if stylist_is_free(sid, t_utc, candidate_end_utc)
                ]
                free = bool(available_stylist)
                slot_stylist_id = available_stylist[0]["id"] if available_stylist else None
                slot_stylist_name = available_stylist[0]["name"] if available_stylist else None
            else:
                free = stylist_is_free(stylist_id, t_utc, candidate_end_utc)
                slot_stylist_id = stylist_id
                slot_stylist_name = stylist_names.get(stylist_id)
                available_stylist = [{"id": slot_stylist_id, "name": slot_stylist_name}]

            if free:
                start_local = t_utc.astimezone(tz)
                # service-only end — the buffer widens conflicts, it is never shown
                end_local = (t_utc + timedelta(minutes=duration_min)).astimezone(tz)

                slot = {
                    "start": start_local.isoformat(),
                    "end": end_local.isoformat(),
                    "stylist_id": slot_stylist_id,
                    "stylist_name": slot_stylist_name,
                    "available_stylist": available_stylist,
                    "available": True,
                    "customer_conflict": None,
                }

                if customer_appointments:
                    slot_start_naive = start_local.replace(tzinfo=None)
                    slot_end_naive = (
                        start_local + timedelta(minutes=duration_min + buffer_min)
                    ).replace(tzinfo=None)
                    conflicting = [
                        a for a in customer_appointments
                        if slot_start_naive < a.end_time and a.start_time < slot_end_naive
                    ]
                    if conflicting:
                        slot["customer_conflict"] = {
                            "message": "Conflicts with another appointment",
                            "conflicts": [{
                                "id": a.id,
                                "start_time": a.start_time.isoformat(),
                                "end_time": a.end_time.isoformat(),
                                "service_name": a.service.name if a.service else None,
                                "booking_reference": a.booking_reference,
                                "status": a.status.name if a.status else None,
                            } for a in conflicting],
                        }

                slots.append(slot)

            t_utc += timedelta(minutes=step_minutes)

    return slots


def compute_month_availability(
    *,
    tenant_id,
    branch,
    tz,
    year,
    month,
    target_stylist_ids,
    any_stylist,
    stylist_id,
    duration_min,
    buffer_min,
    step_minutes,
    lead_minutes,
    now_utc,
    now_local,
    is_day_offered,
    customer_id=None,
    exclude_appointment_id=None,
    bookable_from_by_sid=None,
    beyond_window_fn=None,
    before_start_fn=None,
):
    """Return [{"day": int, "available": bool}, ...] for the given month.

    Args:
        branch: the Branch row (for working hours).
        tz: pytz timezone the branch/local times are in.
        target_stylist_ids: stylist ids to consider (already tenant/branch-scoped).
        any_stylist: True if the day is available when ANY target stylist has a gap.
        stylist_id: the single stylist id when not any_stylist.
        duration_min / buffer_min / step_minutes / lead_minutes: the same numbers
            the day-slots generator uses. The per-day check simulates that
            generator exactly (see _grid_slot_exists) so month == day always.
        now_utc / now_local: aware "now" in UTC / branch-local.
        is_day_offered: callable(date) -> bool, the booking-window gate. Public passes
            the window predicate (beyond-window days -> available:False, never shown);
            admin passes "any non-past date" so beyond-window days are still computed.
        bookable_from_by_sid: {stylist_id: date} of first bookable day, or None.
        beyond_window_fn: optional callable(date) -> bool. When provided (admin flow),
            each day gains a ``"beyond_window"`` flag so the picker can render those
            days in the override colour while still showing their real availability.
        before_start_fn: optional callable(date) -> bool. Same pattern for the
            stylist ``bookable_from`` soft rule — days before the stylist's start
            date gain a ``"before_bookable_from"`` flag (admin passes it INSTEAD of
            bookable_from_by_sid, so the days still show their real availability;
            public keeps the hard bookable_from_by_sid gate).
    """
    from calendar import monthrange
    bookable_from_by_sid = bookable_from_by_sid or {}

    num_days = monthrange(year, month)[1]
    month_start_local = tz.localize(datetime(year, month, 1, 0, 0, 0)).replace(tzinfo=None)
    month_end_local = tz.localize(datetime(year, month, num_days, 23, 59, 59)).replace(tzinfo=None)

    # ---- Bulk-load month conflicts (naive-local WHEREs, matching the DB columns) ----
    appt_q = db.session.query(
        Appointment.stylist_id, Appointment.start_time, Appointment.end_time
    ).outerjoin(
        AppointmentStatusDefinition,
        Appointment.status_id == AppointmentStatusDefinition.id
    ).filter(
        Appointment.tenant_id == tenant_id,
        Appointment.branch_id == branch.id,
        Appointment.stylist_id.in_(target_stylist_ids),
        Appointment.start_time < month_end_local,
        Appointment.end_time > month_start_local,
        # Only exclude Cancelled — everything else (incl. Pending Deposit, no status)
        # blocks the slot. Mirrors the day-slots path so month == day. (outerjoin so a
        # NULL status still counts as active.)
        or_(
            AppointmentStatusDefinition.id.is_(None),
            AppointmentStatusDefinition.name != "Cancelled",
        ),
    )
    if exclude_appointment_id:
        appt_q = appt_q.filter(Appointment.id != exclude_appointment_id)
    appts = appt_q.all()

    holds = get_active_holds_query(utc_now()).filter(
        AppointmentHold.tenant_id == tenant_id,
        AppointmentHold.branch_id == branch.id,
        AppointmentHold.start_time < month_end_local,
        AppointmentHold.end_time > month_start_local,
        or_(AppointmentHold.any_stylist == True,   # noqa: E712
            AppointmentHold.stylist_id.in_(target_stylist_ids)),
    ).with_entities(
        AppointmentHold.stylist_id, AppointmentHold.any_stylist,
        AppointmentHold.start_time, AppointmentHold.end_time,
    ).all()

    timeoffs = db.session.query(
        StylistTimeOff.stylist_id, StylistTimeOff.start_datetime, StylistTimeOff.end_datetime
    ).filter(
        StylistTimeOff.branch_id == branch.id,
        StylistTimeOff.stylist_id.in_(target_stylist_ids),
        StylistTimeOff.start_datetime < month_end_local,
        StylistTimeOff.end_datetime > month_start_local,
    ).all()

    sa_rows = StylistAvailability.query.filter(
        StylistAvailability.branch_id == branch.id,
        StylistAvailability.stylist_id.in_(target_stylist_ids),
    ).all()

    # Bulk-load the month's special-hours overrides ONCE (keyed by date), so the
    # per-day working-hours lookup below doesn't fire one query per day — that was an
    # N+1 (≈31 extra queries/month) on the public + admin month-calendar reads.
    from datetime import date as _date
    from app.models.models import BranchSpecialHours
    _first_day = _date(year, month, 1)
    _last_day = _date(year, month, num_days)
    special_hours_by_date = {
        _first_day + timedelta(days=i): None for i in range((_last_day - _first_day).days + 1)
    }
    for row in BranchSpecialHours.query.filter(
        BranchSpecialHours.branch_id == branch.id,
        BranchSpecialHours.date >= _first_day,
        BranchSpecialHours.date <= _last_day,
    ).all():
        special_hours_by_date[row.date] = row

    customer_appts_month = []
    if customer_id:
        # Same conflict semantics as the stylist load above and the day-slots
        # customer check: Cancelled releases the slot, NULL status still blocks,
        # and in edit mode the appointment being edited never blocks itself.
        # (Cancelled counting here made the admin calendar mark days unavailable
        # that the day-slots endpoint — and the public flow — offered.)
        customer_q = db.session.query(
            Appointment.start_time, Appointment.end_time
        ).outerjoin(
            AppointmentStatusDefinition,
            Appointment.status_id == AppointmentStatusDefinition.id
        ).filter(
            Appointment.tenant_id == tenant_id,
            Appointment.customer_id == customer_id,
            Appointment.start_time < month_end_local,
            Appointment.end_time > month_start_local,
            or_(
                AppointmentStatusDefinition.id.is_(None),
                AppointmentStatusDefinition.name != "Cancelled",
            ),
        )
        if exclude_appointment_id:
            customer_q = customer_q.filter(Appointment.id != exclude_appointment_id)
        customer_appts_month = customer_q.all()

    # ---- Bucket conflicts by day (UTC-aware) ----
    def to_utc_aw(dt_naive):
        return tz.localize(dt_naive).astimezone(pytz.UTC)

    conflicts_by_day_stylist = {}
    customer_conflicts_by_day = {}

    def _local_days_covered(s_utc, e_utc):
        """Every branch-local date the [s_utc, e_utc) interval touches. A conflict
        must be bucketed into ALL of them, not just its start day: multi-day time
        off (Jul 18 00:00 → Jul 21 00:00) blocks Jul 19 and 20 in the day-slots
        route, so bucketing it only under Jul 18 made the calendar offer covered
        days whose slot list was empty. An end at exactly local midnight doesn't
        touch that day (half-open interval, same as the overlap check)."""
        d = s_utc.astimezone(tz).date()
        last = (e_utc.astimezone(tz) - timedelta(microseconds=1)).date()
        while d <= last:
            yield d.isoformat()
            d += timedelta(days=1)

    def add_conf(sid, s, e):
        for day_iso in _local_days_covered(s, e):
            conflicts_by_day_stylist.setdefault(day_iso, {}).setdefault(sid, []).append((s, e))

    # Appointments and holds get their END extended by the buffer — the same
    # extension the day-slots generator applies — so the month check can't find
    # room the day list won't offer. Time-off stays raw (day does the same).
    _buffer_td = timedelta(minutes=buffer_min)

    for sid, s, e in appts:
        add_conf(sid, to_utc_aw(s), to_utc_aw(e) + _buffer_td)

    for sid, anyflag, s, e in holds:
        s_utc, e_utc = to_utc_aw(s), to_utc_aw(e) + _buffer_td
        if anyflag:
            for tid in target_stylist_ids:
                add_conf(tid, s_utc, e_utc)
        else:
            add_conf(sid, s_utc, e_utc)

    for sid, s, e in timeoffs:
        add_conf(sid, to_utc_aw(s), to_utc_aw(e))

    for s, e in customer_appts_month:
        s_utc, e_utc = to_utc_aw(s), to_utc_aw(e)
        for day_iso in _local_days_covered(s_utc, e_utc):
            customer_conflicts_by_day.setdefault(day_iso, []).append((s_utc, e_utc))

    sa_by_dow = {}
    for r in sa_rows:
        try:
            sh, sm = map(int, str(r.start_time).split(':')[:2])
            eh, em = map(int, str(r.end_time).split(':')[:2])
        except Exception:
            continue
        sa_by_dow.setdefault(r.day_of_week, {}).setdefault(r.stylist_id, []).append((sh, sm, eh, em))

    # ---- Per-day availability ----
    _dur_plus_buf = timedelta(minutes=duration_min + buffer_min)
    _earliest_utc = now_utc + timedelta(minutes=int(lead_minutes or 0))
    days_resp = []

    def _entry(day_num, available, day_date, full=False):
        e = {"day": day_num, "available": available}
        # `full`: a day the branch is open and a target stylist works, inside the
        # booking window, with bookable hours still ahead of the lead time — and
        # no gap left. Distinct from "not offered / closed / nobody rostered" so
        # the public calendar can keep it SELECTABLE: the cancellation waitlist's
        # public entry point is the fully-booked day's empty state, and a disabled
        # cell made that state unreachable exactly when it mattered.
        if full:
            e["full"] = True
        if beyond_window_fn is not None:
            e["beyond_window"] = bool(beyond_window_fn(day_date))
        if before_start_fn is not None:
            e["before_bookable_from"] = bool(before_start_fn(day_date))
        return e

    for d in range(1, num_days + 1):
        day_date = tz.localize(datetime(year, month, d, 0, 0)).date()

        if not is_day_offered(day_date):
            days_resp.append(_entry(d, False, day_date))
            continue

        loc_blocks = get_working_hours_for_date(
            branch, day_date, special_hours_by_date=special_hours_by_date)
        if not loc_blocks:
            days_resp.append(_entry(d, False, day_date))
            continue

        loc_blocks_utc = []
        for s_t, e_t in loc_blocks:
            s_aw = tz.localize(datetime.combine(day_date, s_t)).astimezone(pytz.UTC)
            e_aw = tz.localize(datetime.combine(day_date, e_t)).astimezone(pytz.UTC)
            loc_blocks_utc.append((s_aw, e_aw))

        dow = _DOW[day_date.weekday()]
        day_key = day_date.isoformat()
        found = False
        worked = False   # some target stylist has rostered, still-ahead hours this day

        for sid in (target_stylist_ids if any_stylist else [stylist_id]):
            bf = bookable_from_by_sid.get(sid)
            if bf and day_date < bf:
                if not any_stylist:
                    found = False
                    break
                continue  # stylist not accepting bookings yet that day

            sa_blocks = []
            for tpl in sa_by_dow.get(dow, {}).get(sid, []):
                sh, sm, eh, em = tpl
                s_aw = tz.localize(datetime.combine(day_date, dt_time(sh, sm))).astimezone(pytz.UTC)
                e_aw = tz.localize(datetime.combine(day_date, dt_time(eh, em))).astimezone(pytz.UTC)
                sa_blocks.append((s_aw, e_aw))
            if not sa_blocks:
                if not any_stylist:
                    found = False
                    break
                continue

            cand_blocks = []
            for ls, le in loc_blocks_utc:
                for ss, se in sa_blocks:
                    s = max(ls, ss)
                    e = min(le, se)
                    if s < e:
                        cand_blocks.append((s, e))
            if not cand_blocks:
                if any_stylist:
                    continue
                found = False
                break
            if any(e > _earliest_utc for _s, e in cand_blocks):
                worked = True

            conflicts = (conflicts_by_day_stylist.get(day_key, {}).get(sid, []) +
                         customer_conflicts_by_day.get(day_key, []))

            if any(_grid_slot_exists(s, e, conflicts, _earliest_utc, step_minutes,
                                     _dur_plus_buf, tz) for s, e in cand_blocks):
                found = True
                if any_stylist:
                    break
            else:
                if not any_stylist:
                    found = False
                    break

        days_resp.append(_entry(d, found, day_date, full=(worked and not found)))

    return days_resp
