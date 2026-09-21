"""lock_booking_slot — the lock must cover every pair of bookings that could conflict.

The lock used to be keyed on the exact start time. That serialized N clients racing
ONE slot (what the Phase 5 load test bursts), but 10:00–11:00 and 10:30–11:30 for the
same stylist hashed to different keys: both passed the conflict check before either
committed, and both inserted. The key is now one id per branch-local DATE the
buffer-widened window touches (`booking_lock_ids`), so any two windows that share an
instant share a lock.

The first half is pure (no DB) and pins that property; the second half proves against
real Postgres that a second connection actually waits.

"""
from datetime import datetime, timedelta
from itertools import combinations

from sqlalchemy import text

from app.extensions import db
from app.utils.appointment_helpers import booking_lock_ids, lock_booking_slot
from tests.conftest import requires_postgres

T, B = 7, 3


def _ids(start, minutes=60, buffer=0, tenant=T, branch=B):
    return booking_lock_ids(tenant, branch, start, start + timedelta(minutes=minutes), buffer)


# --------------------------------------------------------------------------- #
# The property: could-conflict ⇒ shares a lock id
# --------------------------------------------------------------------------- #

def test_overlapping_bookings_with_different_starts_share_a_lock():
    a = _ids(datetime(2035, 7, 2, 10, 0))
    b = _ids(datetime(2035, 7, 2, 10, 30))
    assert set(a) & set(b), "10:00–11:00 and 10:30–11:30 must serialize"


def test_every_conflicting_pair_shares_a_lock_across_midnight():
    # Sweep 45-minute windows every 15 minutes across two midnights with a 20-minute
    # buffer, and check the property against the write path's own conflict rule
    # (check_slot_has_conflicts: a.start < b.end + buf and a.end > b.start - buf).
    buf = timedelta(minutes=20)
    base = datetime(2035, 7, 1, 21, 0)
    windows = [(base + timedelta(minutes=15 * i), base + timedelta(minutes=15 * i + 45))
               for i in range(120)]
    for (a_s, a_e), (b_s, b_e) in combinations(windows, 2):
        if a_s < b_e + buf and a_e > b_s - buf:
            shared = (set(booking_lock_ids(T, B, a_s, a_e, 20))
                      & set(booking_lock_ids(T, B, b_s, b_e, 20)))
            assert shared, f"{a_s}–{a_e} conflicts with {b_s}–{b_e} but shares no lock"


def test_a_window_spanning_midnight_locks_both_days():
    assert len(_ids(datetime(2035, 7, 2, 23, 30))) == 2


def test_the_buffer_alone_can_reach_the_next_day():
    start = datetime(2035, 7, 2, 23, 0)
    assert len(_ids(start, minutes=45)) == 1            # ends 23:45
    assert len(_ids(start, minutes=45, buffer=30)) == 2  # buffered to 00:15


def test_ending_exactly_at_midnight_does_not_touch_the_next_day():
    assert len(_ids(datetime(2035, 7, 2, 23, 0), minutes=60)) == 1


def test_ids_come_back_sorted_so_acquisition_order_is_global():
    # Sorted ids are what makes a two-lock booking unable to deadlock another one.
    for day in range(1, 28):
        ids = _ids(datetime(2035, 7, day, 23, 30), buffer=15)
        assert ids == sorted(ids)


def test_other_branches_tenants_and_days_do_not_share_a_lock():
    mine = set(_ids(datetime(2035, 7, 2, 10, 0)))
    assert not mine & set(_ids(datetime(2035, 7, 3, 10, 0)))
    assert not mine & set(_ids(datetime(2035, 7, 2, 10, 0), branch=B + 1))
    assert not mine & set(_ids(datetime(2035, 7, 2, 10, 0), tenant=T + 1))


def test_aware_and_naive_starts_lock_the_same_thing():
    import pytz
    naive = datetime(2035, 7, 2, 10, 0)
    aware = pytz.timezone("America/New_York").localize(naive)
    assert booking_lock_ids(T, B, naive) == booking_lock_ids(T, B, aware)


def test_end_is_optional_for_a_point_lock():
    assert booking_lock_ids(T, B, datetime(2035, 7, 2, 10, 0)) == \
        _ids(datetime(2035, 7, 2, 10, 0))


# --------------------------------------------------------------------------- #
# Against Postgres: a second connection really is held out
# --------------------------------------------------------------------------- #

def _other_connection_can_lock(start, minutes=60):
    """From a SEPARATE backend, try (without waiting) every id that booking needs."""
    with db.engine.connect() as other:
        with other.begin():
            return all(
                other.execute(text("SELECT pg_try_advisory_xact_lock(:id)"),
                              {"id": lock_id}).scalar()
                for lock_id in _ids(start, minutes)
            )


@requires_postgres
def test_a_second_booker_is_held_out_of_an_overlapping_window(session):
    lock_booking_slot(T, B, datetime(2035, 7, 2, 10, 0), datetime(2035, 7, 2, 11, 0), 0)

    assert not _other_connection_can_lock(datetime(2035, 7, 2, 10, 30)), \
        "an overlapping booking with a different start was not serialized"
    assert _other_connection_can_lock(datetime(2035, 7, 3, 10, 30)), \
        "a booking on another day should not wait"


@requires_postgres
def test_the_lock_is_released_with_the_transaction(session):
    lock_booking_slot(T, B, datetime(2035, 7, 2, 10, 0), datetime(2035, 7, 2, 11, 0), 0)
    db.session.rollback()
    assert _other_connection_can_lock(datetime(2035, 7, 2, 10, 30))
