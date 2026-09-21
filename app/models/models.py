"""Slim copies of the tables the availability engine reads.

Production's `models.py` defines 127 models; the engine touches these. Each class keeps
the production table name, column names and column types for every column the engine,
the write-path guard or the tests use — other columns (pricing, payments, branding,
audit fields, …) are left out. Two deliberate differences, both so the suite can run
on SQLite with no services:

  * JSON columns are `JSONB` in production (a hard rule there — Postgres has no
    equality operator for `json`, which breaks SELECT DISTINCT). Here they are
    `JSON` with a `JSONB` variant on Postgres.
  * `Stylist.name` does not exist in production: a stylist's display name lives on
    the linked `User` row. The engine never reads it (callers pass a
    `{stylist_id: name}` map in), so the shim keeps one column instead of a users
    table.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

from app import db

_JSON = JSON().with_variant(JSONB(), "postgresql")


def utc_now():
    # Naive UTC, non-deprecated (replaces datetime.utcnow()). A tz-naive UTC datetime.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(db.Model):
    __tablename__ = 'tenants'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String)


class Branch(db.Model):
    __tablename__ = 'branches'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    name = db.Column(db.String)
    # IANA zone name. Appointment times are stored as naive wall time IN THIS ZONE.
    timezone = db.Column(db.String, nullable=True)
    # {"mon": ["09:00", "17:00"], ...} — a missing/empty day means closed.
    working_hours = db.Column(_JSON, nullable=True)


class BranchSpecialHours(db.Model):
    __tablename__ = 'branch_special_hours'
    id = db.Column(db.Integer, primary_key=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    is_closed = db.Column(db.Boolean, default=False, nullable=False)
    open_time = db.Column(db.String(5), nullable=True)   # "HH:MM"
    close_time = db.Column(db.String(5), nullable=True)  # "HH:MM"


class Stylist(db.Model):
    __tablename__ = 'stylists'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    name = db.Column(db.String)  # shim only — see module docstring
    is_bookable = db.Column(db.Boolean, default=True, nullable=False)
    bookable_from = db.Column(db.Date, nullable=True)


class StylistAvailability(db.Model):
    """Weekly roster: one row per stylist × branch × weekday block."""
    __tablename__ = 'stylist_availability'
    id = db.Column(db.Integer, primary_key=True)
    stylist_id = db.Column(db.Integer, db.ForeignKey('stylists.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    day_of_week = db.Column(db.String)   # 'mon' … 'sun'
    start_time = db.Column(db.String)    # "HH:MM"
    end_time = db.Column(db.String)


class StylistTimeOff(db.Model):
    __tablename__ = 'stylist_time_off'
    id = db.Column(db.Integer, primary_key=True)
    stylist_id = db.Column(db.Integer, db.ForeignKey('stylists.id'))
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    start_datetime = db.Column(db.DateTime)  # naive branch-local
    end_datetime = db.Column(db.DateTime)


class Service(db.Model):
    __tablename__ = 'services'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    name = db.Column(db.String)
    duration_minutes = db.Column(db.Integer)


class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'))
    first_name = db.Column(db.String)
    last_name = db.Column(db.String)


class AppointmentStatusDefinition(db.Model):
    """Statuses are per-tenant rows, not an enum. The engine cares about one name:
    'Cancelled' releases a slot; every other status — and NO status — blocks it."""
    __tablename__ = 'appointment_status_definitions'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    name = db.Column(db.String, nullable=False)


class Appointment(db.Model):
    __tablename__ = 'appointments'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'))
    stylist_id = db.Column(db.Integer, db.ForeignKey('stylists.id'))
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'))
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'))
    status_id = db.Column(db.Integer, db.ForeignKey('appointment_status_definitions.id'))
    # Naive branch-local wall time; end_time is the SERVICE end (no buffer stored).
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    booking_reference = db.Column(db.String, nullable=True)

    service = db.relationship('Service')
    status = db.relationship('AppointmentStatusDefinition')

    __table_args__ = (
        db.Index('ix_appt_tenant_start', 'tenant_id', 'start_time'),
    )


class AppointmentHold(db.Model):
    """A short-lived claim on a slot while a customer fills in the booking form.
    Holds do NOT exclusively reserve: several can coexist on one slot, and the
    confirm step is where the slot is finally claimed (see booking guard)."""
    __tablename__ = 'appointment_holds'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenants.id'), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)

    any_stylist = db.Column(db.Boolean, default=False, nullable=False, index=True)
    stylist_id = db.Column(db.Integer, db.ForeignKey('stylists.id'), nullable=True, index=True)

    start_time = db.Column(db.DateTime, nullable=False, index=True)
    end_time = db.Column(db.DateTime, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, nullable=False)

    status = db.Column(db.String, nullable=False, default='active', index=True)  # 'active' | 'released' | 'confirmed' | 'expired'
    hold_token = db.Column(db.String, unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)  # naive UTC (absolute, not wall time)

    stylist_options = db.Column(_JSON, nullable=True)  # e.g. {"ids": [3, 7, 12]}

    __table_args__ = (
        db.Index('ix_hold_tenant_start', 'tenant_id', 'start_time'),
        db.Index('ix_hold_tenant_staff_start', 'tenant_id', 'stylist_id', 'start_time'),
        db.Index('ix_hold_tenant_branch_start', 'tenant_id', 'branch_id', 'start_time'),
    )
