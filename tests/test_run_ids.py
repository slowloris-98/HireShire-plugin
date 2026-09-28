"""A run's two names, and the calendar day they put it under.

The day a sweep belongs to is the date in its *stamp*, which is local. That choice is
what these tests pin, and the DST cases are the reason it is a choice at all: a UTC
range over `run_id` would coincide with it most of the time and disagree exactly at
the boundaries a user would notice.

The zone is hand-rolled rather than taken from `zoneinfo`. `time.tzset()` does not
exist on Windows, which is this repo's development platform, and `zoneinfo` has no
database to read there — `tzdata` is not a dependency. So `run_stamp` and
`day_of_run_id` take a `tz` argument, and this is the thing it exists for.
"""

from datetime import datetime, timedelta, timezone, tzinfo

from hireshire import run_ids


# --- a zone with real DST, built by hand --------------------------------------

_STD = timedelta(hours=-5)
_DST = timedelta(hours=-4)
# The two instants, in UTC, at which this zone changes: 02:00 local, spring and autumn.
_DST_FROM = datetime(2026, 3, 8, 7, 0)
_DST_UNTIL = datetime(2026, 11, 1, 6, 0)


class _Eastern(tzinfo):
    """-05:00 in winter, -04:00 in summer.

    `fromutc` is overridden rather than left to the base class: it receives the UTC
    wall time in its fields, so the switch can be decided on the instant itself,
    which is exact and needs none of the ambiguous-local-time reasoning the stdlib
    recipe carries.
    """

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        offset = _DST if _DST_FROM <= naive < _DST_UNTIL else _STD
        return (dt + offset).replace(tzinfo=self)

    def utcoffset(self, dt):
        if dt is None:
            return _STD
        naive = dt.replace(tzinfo=None)
        summer = (_DST_FROM + _DST) <= naive < (_DST_UNTIL + _DST)
        return _DST if summer else _STD

    def dst(self, dt):
        return _DST - _STD if self.utcoffset(dt) == _DST else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self.utcoffset(dt) == _DST else "EST"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _run_id(text: str) -> str:
    return _utc(text).strftime(run_ids.RUN_ID_FMT)


# --- day_of -------------------------------------------------------------------


def test_the_day_is_the_date_in_the_stamp():
    assert run_ids.day_of("2026-09-27_143005") == "2026-09-27"
    assert run_ids.day_of("2026-01-01_000000") == "2026-01-01"


def test_a_stamp_that_names_no_date_has_no_day():
    """`finalise_abandoned_runs` falls back to `stamp = run_id` for a run id it cannot
    parse, so this has to answer "no day" rather than hand back a fragment that a
    caller would turn into a directory name."""
    for bad in ("", "nonsense", "2026-9-27_143005", "26-09-27_1430", "hireshire"):
        assert run_ids.day_of(bad) == "", bad


# --- the round trip -----------------------------------------------------------


def test_the_day_of_a_run_is_derived_exactly_as_its_folder_name_was():
    """The whole basis of the day scope: the page's set of runs is chosen by the same
    composition that named the directory the page sits in, so a sweep cannot be in the
    folder and missing from the numbers."""
    for text in ("2026-03-08T04:30:00", "2026-07-04T12:00:00", "2026-11-01T23:00:00"):
        rid = _run_id(text)
        expected = run_ids.day_of(run_ids.run_stamp(run_ids.run_started_at(rid)))
        assert run_ids.day_of_run_id(rid) == expected, rid


def test_a_run_id_that_will_not_parse_has_no_day():
    assert run_ids.day_of_run_id("not-a-run-id") == ""
    assert run_ids.run_started_at("not-a-run-id") is None


# --- DST ----------------------------------------------------------------------


def test_a_sweep_is_filed_under_its_local_date_not_its_utc_one():
    """The case that rules out bucketing on `run_id`: a late-evening sweep is a UTC
    day ahead of the folder the user is looking at."""
    tz = _Eastern()
    rid = _run_id("2026-07-05T02:30:00")          # 22:30 local on the 4th
    assert rid.startswith("2026-07-05")
    assert run_ids.day_of_run_id(rid, tz) == "2026-07-04"


def test_midnight_on_a_spring_forward_day_still_splits_the_days():
    """The clocks jump at 02:00 on 2026-03-08, which is after local midnight — so the
    midnight boundary that decides the two folders sits at a different UTC instant on
    either side of it."""
    tz = _Eastern()
    before = _run_id("2026-03-08T04:30:00")       # 23:30 local, still the 7th
    after = _run_id("2026-03-08T05:30:00")        # 00:30 local, now the 8th

    assert run_ids.day_of_run_id(before, tz) == "2026-03-07"
    assert run_ids.day_of_run_id(after, tz) == "2026-03-08"


def test_a_spring_forward_day_is_a_twenty_three_hour_bucket():
    tz = _Eastern()
    # The day opens on standard time (-05:00) and closes on DST (-04:00), so its two
    # ends sit at UTC instants 23 hours apart rather than 24.
    first = _run_id("2026-03-08T05:00:00")        # 00:00 local on the 8th
    last = _run_id("2026-03-09T03:59:00")         # 23:59 local on the 8th
    next_day = _run_id("2026-03-09T04:00:00")     # 00:00 local on the 9th

    assert run_ids.day_of_run_id(first, tz) == "2026-03-08"
    assert run_ids.day_of_run_id(last, tz) == "2026-03-08"
    assert run_ids.day_of_run_id(next_day, tz) == "2026-03-09"
    assert (run_ids.run_started_at(next_day)
            - run_ids.run_started_at(first)) == timedelta(hours=23)


def test_a_fall_back_day_is_one_twenty_five_hour_bucket():
    """Local midnight happens once on 2026-11-01 but the day runs 25 hours, and every
    sweep in it belongs to one folder. A fixed-width UTC range could not express this."""
    tz = _Eastern()
    first = _run_id("2026-11-01T04:00:00")        # 00:00 local (DST)
    middle = _run_id("2026-11-01T06:30:00")       # 01:30 local, after the clocks go back
    last = _run_id("2026-11-02T04:59:00")         # 23:59 local (standard)
    next_day = _run_id("2026-11-02T05:00:00")     # 00:00 local on the 2nd

    assert run_ids.day_of_run_id(first, tz) == "2026-11-01"
    assert run_ids.day_of_run_id(middle, tz) == "2026-11-01"
    assert run_ids.day_of_run_id(last, tz) == "2026-11-01"
    assert run_ids.day_of_run_id(next_day, tz) == "2026-11-02"
    assert (run_ids.run_started_at(next_day)
            - run_ids.run_started_at(first)) == timedelta(hours=25)


# --- the delegates orchestrate still exposes ----------------------------------


def test_orchestrate_still_answers_to_its_own_names():
    """Both were moved here, and both are called by name elsewhere in the suite."""
    import orchestrate

    now = _utc("2026-09-27T06:51:12")
    assert orchestrate._run_stamp(now) == run_ids.run_stamp(now)
    assert orchestrate._run_started_at("2026-09-27T06-51-12Z") == now
