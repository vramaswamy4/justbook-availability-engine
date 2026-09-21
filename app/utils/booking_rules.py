"""Soft booking rules — hard for the public flow, override-able for admin.

Booking constraints come in two flavours:

  * **Hard rules** always block (double-book, service-not-offered-at-branch). These
    live at their own call sites and are never bypassed.
  * **Soft rules** are hard limits for the *public* booking flow but advisory for
    *admin*: an admin sees the boundary (a distinct colour + hover reason in the
    availability picker) and may knowingly book past it with a confirm-per-slot, which
    we audit-log. Public routes never pass ``allow_override``, so they stay fully
    enforced.

This module is the single registry of soft rules + the pure predicates the
availability engines and write paths use to *annotate* (not silently drop) slots that
violate one. Today the booking window is wired through here; the other schedule rules
(lead time, past date, working hours, stylist availability, buffer) are the documented
next increment — see docs/flows/booking-and-availability.md.
"""
from datetime import date as _date, datetime, timedelta

# ── Soft-rule registry ──────────────────────────────────────────────────────────
# key -> short human label (shown in the admin picker hover / confirm dialog).
RULE_BOOKING_WINDOW = "booking_window"
RULE_BOOKABLE_FROM = "stylist_bookable_from"

SOFT_RULE_LABELS = {
    RULE_BOOKING_WINDOW: "Beyond the booking window",
    RULE_BOOKABLE_FROM: "Before the stylist's start date",
}


def is_before_bookable_from(day_date: _date, bookable_from) -> bool:
    """True if a calendar day precedes the stylist's booking start date. Soft rule:
    public hard-hides these slots; admin sees them tagged ``before_bookable_from``
    and may book with allow_rule_override (confirm + audit), same as the window."""
    return bool(bookable_from) and day_date < bookable_from

# Whitelisted "Booking increment" values (minutes). Bounds the slot grid so a tenant
# can't pick a 1-minute increment and generate thousands of near-identical slots.
ALLOWED_SLOT_INTERVALS = (5, 10, 15, 20, 30, 60)
DEFAULT_SLOT_INTERVAL = 15
DEFAULT_BOOKING_WINDOW_DAYS = 60


def get_booking_window_days(settings) -> int:
    """Days-ahead the booking window allows. Falls back to the model default when
    settings is a bare fallback namespace (no persisted row)."""
    return int(getattr(settings, "booking_window_days", DEFAULT_BOOKING_WINDOW_DAYS)
               or DEFAULT_BOOKING_WINDOW_DAYS)


def get_slot_interval_minutes(settings) -> int:
    """Configured slot interval ("Booking increment"), clamped to the whitelist so a
    bad/legacy value can never explode the grid."""
    raw = int(getattr(settings, "slot_interval_minutes", DEFAULT_SLOT_INTERVAL)
              or DEFAULT_SLOT_INTERVAL)
    return raw if raw in ALLOWED_SLOT_INTERVALS else DEFAULT_SLOT_INTERVAL


def booking_window_end_date(settings, from_date: _date) -> _date:
    """Last local calendar date inside the booking window, measured from ``from_date``
    (the branch-local 'today'). Days after this are beyond-window."""
    return from_date + timedelta(days=get_booking_window_days(settings))


def is_day_beyond_window(day_date: _date, settings, from_date: _date) -> bool:
    """True if a whole calendar day falls past the booking window."""
    return day_date > booking_window_end_date(settings, from_date)


def is_beyond_window(start_dt: datetime, settings, now_utc: datetime) -> bool:
    """True if a concrete start instant is past the booking window. ``start_dt`` and
    ``now_utc`` must be the same-flavour (in practice: both naive branch-local, the
    appointment storage convention — pass branch_local_now(), not utc_now())."""
    days = get_booking_window_days(settings)
    return start_dt > now_utc + timedelta(days=days)
