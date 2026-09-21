"""Row builders for the tests — written for this repository.

Production's factories build a full tenant (subscription plan, roles, users, branch
roster, …). These keep the same function names and call signatures the ported tests
use, and create only the rows the engine reads. All data is synthetic.
"""
from datetime import timedelta
from itertools import count

from app.models.models import (
    Appointment, AppointmentStatusDefinition, Branch, Customer, Service, Stylist,
    StylistAvailability, Tenant, utc_now,
)

_seq = count(1)


def make_tenant(session):
    t = Tenant(name=f"Test Salon {next(_seq)}")
    session.add(t)
    session.flush()
    return t


def add_branch(session, tenant, *, name=None, working_hours=None):
    b = Branch(tenant_id=tenant.id, name=name or f"Branch {next(_seq)}",
               working_hours=working_hours)
    session.add(b)
    session.flush()
    return b


def add_customer(session, tenant):
    n = next(_seq)
    c = Customer(tenant_id=tenant.id, first_name="Customer", last_name=str(n))
    session.add(c)
    session.flush()
    return c


def add_stylist(session, tenant, *, is_bookable=True, bookable_from=None, name=None):
    s = Stylist(tenant_id=tenant.id, name=name or f"Stylist {next(_seq)}",
                is_bookable=is_bookable, bookable_from=bookable_from)
    session.add(s)
    session.flush()
    return s


def add_service(session, tenant, *, name="Cut", duration=30, stylists=()):
    # `stylists` is accepted for signature parity with production, where it writes
    # the service↔stylist qualification rows. Qualification is resolved by the
    # routes BEFORE the engine is called (it receives `target_stylist_ids`), so
    # there is no such table here.
    svc = Service(tenant_id=tenant.id, name=name, duration_minutes=duration)
    session.add(svc)
    session.flush()
    return svc


def add_appointment(session, tenant, *, stylist=None, branch=None, customer=None,
                    start=None, minutes=30, status=None, service=None):
    start = start or (utc_now() + timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    a = Appointment(
        tenant_id=tenant.id,
        stylist_id=stylist.id if stylist else None,
        branch_id=branch.id if branch else None,
        customer_id=customer.id if customer else None,
        service_id=service.id if service else None,
        status_id=status.id if status else None,
        start_time=start,
        end_time=start + timedelta(minutes=minutes),
    )
    session.add(a)
    session.flush()
    return a


def add_status(session, tenant, name):
    st = AppointmentStatusDefinition(tenant_id=tenant.id, name=name)
    session.add(st)
    session.flush()
    return st


def add_hours(session, stylist, branch, day, start="09:00", end="18:00"):
    row = StylistAvailability(stylist_id=stylist.id, branch_id=branch.id,
                              day_of_week=day, start_time=start, end_time=end)
    session.add(row)
    session.flush()
    return row
