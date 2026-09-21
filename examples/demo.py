"""Runnable walk-through of the availability engine. Written for this repository.

    python examples/demo.py

Builds one salon branch in New York (in-memory SQLite, synthetic data) and asks the
engine the two questions the booking page asks:

  1. compute_month_availability — which days in March still have a bookable gap?
  2. compute_day_slots          — what start times exist on one of those days?

March is chosen because US daylight saving starts on its second Sunday, so the month
contains a 23-hour day. The scenario is fixed in the year after next so the output
never depends on when you run it.
"""
import calendar
import os
import sys
from datetime import date, datetime, timedelta

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db  # noqa: E402
from app.models.models import (  # noqa: E402
    Appointment, AppointmentHold, AppointmentStatusDefinition, Branch,
    BranchSpecialHours, Service, Stylist, StylistAvailability, StylistTimeOff, Tenant,
    utc_now,
)
from app.utils.appointment_helpers import check_slot_has_conflicts  # noqa: E402
from app.utils.availability_engine import (  # noqa: E402
    compute_day_slots, compute_month_availability,
)

YEAR, MONTH = date.today().year + 2, 3
TZ = pytz.timezone("America/New_York")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat"]          # closed Sundays
DURATION, BUFFER, STEP = 60, 15, 30                            # minutes



def nth_weekday(n, weekday):
    days = [d for d in range(1, 32) if date(YEAR, MONTH, d).weekday() == weekday]
    return days[n - 1]


CLOSED_DAY = nth_weekday(4, 4)


def build():
    tenant = Tenant(name="Example Salon")
    db.session.add(tenant)
    db.session.flush()

    branch = Branch(tenant_id=tenant.id, name="Downtown", timezone=TZ.zone,
                    working_hours={d: ["09:00", "17:00"] for d in WEEKDAYS})
    service = Service(tenant_id=tenant.id, name="Cut & finish", duration_minutes=DURATION)
    booked = AppointmentStatusDefinition(tenant_id=tenant.id, name="Booked")
    cancelled = AppointmentStatusDefinition(tenant_id=tenant.id, name="Cancelled")
    ana = Stylist(tenant_id=tenant.id, name="Ana")
    ben = Stylist(tenant_id=tenant.id, name="Ben")
    db.session.add_all([branch, service, booked, cancelled, ana, ben])
    db.session.flush()

    # Ana works Mon–Sat 09–17; Ben works Tue–Sat 12–17 (so Mondays are Ana-only).
    for d in WEEKDAYS:
        db.session.add(StylistAvailability(stylist_id=ana.id, branch_id=branch.id,
                                           day_of_week=d, start_time="09:00", end_time="17:00"))
    for d in WEEKDAYS[1:]:
        db.session.add(StylistAvailability(stylist_id=ben.id, branch_id=branch.id,
                                           day_of_week=d, start_time="12:00", end_time="17:00"))

    first_monday, third_monday = nth_weekday(1, 0), nth_weekday(3, 0)
    first_tuesday = nth_weekday(1, 1)

    def appt(stylist, day, hour, minute, minutes, status):
        start = datetime(YEAR, MONTH, day, hour, minute)
        db.session.add(Appointment(
            tenant_id=tenant.id, branch_id=branch.id, stylist_id=stylist.id,
            service_id=service.id, status_id=status.id,
            start_time=start, end_time=start + timedelta(minutes=minutes)))

    # First Monday: Ana is booked solid → the day is FULL (open, rostered, no gap).
    appt(ana, first_monday, 9, 0, 480, booked)
    # First Tuesday: a morning booking, a CANCELLED lunchtime one (must not block),
    # and another customer's live hold on Ana at 15:00 (must block).
    appt(ana, first_tuesday, 10, 0, 60, booked)
    appt(ana, first_tuesday, 12, 0, 60, cancelled)
    db.session.add(AppointmentHold(
        tenant_id=tenant.id, branch_id=branch.id, service_id=service.id,
        stylist_id=ana.id, any_stylist=False,
        start_time=datetime(YEAR, MONTH, first_tuesday, 15, 0),
        end_time=datetime(YEAR, MONTH, first_tuesday, 16, 0),
        duration_minutes=60, status="active", hold_token="HLD_demo",
        expires_at=utc_now() + timedelta(minutes=5)))
    # Third Monday onwards: Ana is away for three days (multi-day time off).
    db.session.add(StylistTimeOff(
        stylist_id=ana.id, branch_id=branch.id,
        start_datetime=datetime(YEAR, MONTH, third_monday, 0, 0),
        end_datetime=datetime(YEAR, MONTH, third_monday + 3, 0, 0)))
    # One special-hours override: the branch is closed on the last Friday.
    db.session.add(BranchSpecialHours(branch_id=branch.id, date=date(YEAR, MONTH, CLOSED_DAY),
                                      is_closed=True))
    db.session.commit()
    return tenant, branch, service, ana, ben, first_tuesday


def print_month(title, days):
    print(f"\n{title}")
    print("  Mo  Tu  We  Th  Fr  Sa  Su      ## bookable   FF rostered but no gap left   .. closed")
    by_day = {d["day"]: d for d in days}
    for week in calendar.monthcalendar(YEAR, MONTH):
        cells = []
        for d in week:
            if d == 0:
                cells.append("    ")
                continue
            e = by_day[d]
            mark = "##" if e["available"] else ("FF" if e.get("full") else "..")
            cells.append(f"{d:>2}{mark}")
        print("  " + "".join(cells))


def main():
    app = create_app("sqlite:///:memory:")
    with app.app_context():
        db.create_all()
        tenant, branch, service, ana, ben, first_tuesday = build()

        # "Now" is pinned a month before, so nothing is in the past and no lead time bites.
        now_local = TZ.localize(datetime(YEAR, MONTH - 1, 1, 9, 0))
        now_utc = now_local.astimezone(pytz.UTC)
        common = dict(tenant_id=tenant.id, branch=branch, tz=TZ,
                      duration_min=DURATION, buffer_min=BUFFER, step_minutes=STEP,
                      lead_minutes=0, now_utc=now_utc)

        print(f"Branch: {branch.name} ({TZ.zone}), open Mon–Sat 09:00–17:00, "
              f"closed Friday {CLOSED_DAY} (special hours)")
        print(f"Service: {DURATION} min + {BUFFER} min buffer, {STEP}-minute booking increment")

        month_ana = compute_month_availability(
            **common, year=YEAR, month=MONTH, now_local=now_local,
            target_stylist_ids=[ana.id], any_stylist=False, stylist_id=ana.id,
            is_day_offered=lambda d: True)
        print_month(f"{calendar.month_name[MONTH]} {YEAR} — Ana only", month_ana)

        month_any = compute_month_availability(
            **common, year=YEAR, month=MONTH, now_local=now_local,
            target_stylist_ids=[ana.id, ben.id], any_stylist=True, stylist_id=None,
            is_day_offered=lambda d: True)
        print_month(f"{calendar.month_name[MONTH]} {YEAR} — any stylist "
                    "(Ben covers Ana's time off, Tue–Sat afternoons)", month_any)

        day = date(YEAR, MONTH, first_tuesday)
        slots = compute_day_slots(
            **common, date_obj=day, target_stylist_ids=[ana.id],
            stylist_names={ana.id: ana.name}, any_stylist=False, stylist_id=ana.id)
        print(f"\n{day:%A %d %B} — Ana. Booked 10:00–11:00, a CANCELLED 12:00–13:00, "
              "and another customer's live hold 15:00–16:00:")
        print("  " + "  ".join(s["start"][11:16] for s in slots))
        print("  09:00/09:30  60 min + 15 min buffer would run into the 10:00 booking\n"
              "  11:30        first grid point after the booking's buffered end (11:15)\n"
              "  12:00        offered: a Cancelled appointment releases its slot\n"
              "  14:00+       would run into the hold; after the hold's buffered end (16:15)\n"
              "               75 minutes no longer fit before closing")

        # DST: the branch is closed Sundays, so look at the Saturday/Monday around it.
        dst_sunday = nth_weekday(2, 6)
        print(f"\nDST starts Sunday {dst_sunday} {calendar.month_name[MONTH]}. "
              "First slot either side, as UTC offsets:")
        for d in (dst_sunday - 1, dst_sunday + 1):
            s = compute_day_slots(
                **common, date_obj=date(YEAR, MONTH, d), target_stylist_ids=[ana.id],
                stylist_names={ana.id: ana.name}, any_stylist=False, stylist_id=ana.id)
            print(f"  {date(YEAR, MONTH, d):%a %d}: {s[0]['start']}")

        # The write side: would a booking actually be allowed to land?
        def would_conflict(hour, minute=0):
            start = datetime(YEAR, MONTH, first_tuesday, hour, minute)
            return check_slot_has_conflicts(
                tenant_id=tenant.id, branch_id=branch.id, service_id=service.id,
                stylist_id=ana.id, any_stylist=False, stylist_ids_for_service=None,
                start_dt=start, end_dt=start + timedelta(minutes=DURATION),
                buffer_minutes=BUFFER)
        print("\nWrite-path guard for Ana that Tuesday (same rules, checked again at booking time):")
        for h, m in ((11, 0), (11, 30), (12, 0), (15, 0)):
            print(f"  {h:02d}:{m:02d} → {'CONFLICT' if would_conflict(h, m) else 'ok'}")


if __name__ == "__main__":
    main()
