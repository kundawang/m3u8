# Copyright 2024 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.

import datetime

import playlists

import m3u8
from m3u8.ad_tracking import AdBreak, AdBreakList, AdBreakTracker

UTC = datetime.timezone.utc


def _parse(content):
    return m3u8.M3U8(content)


def _tracker_for(*contents):
    tracker = AdBreakTracker()
    for content in contents:
        tracker.merge(_parse(content))
    return tracker


# --------------------------------------------------------------------------- #
# requirement 1: extraction, ordering, preserved id and raw markers
# --------------------------------------------------------------------------- #
def test_scte35_out_in_pair_consolidates_into_one_closed_break():
    tracker = _tracker_for(playlists.DATERANGE_SCTE35_OUT_AND_IN_PLAYLIST)
    breaks = tracker.breaks

    assert len(breaks) == 1
    ad = breaks[0]
    assert ad.id == "splice-6FFFFFF0"
    assert not ad.is_open
    assert ad.start_date == datetime.datetime(2014, 3, 5, 11, 15, tzinfo=UTC)
    assert ad.duration == 59.993
    assert ad.scte35_out.startswith("0xFC002F")
    assert ad.scte35_in.startswith("0xFC002A")
    # original markers retained
    assert ad.start_marker is not None
    assert ad.end_marker is not None
    assert len(ad.tags) == 2
    assert all(tag.id == "splice-6FFFFFF0" for tag in ad.tags)


def test_explicit_start_and_end_dates_build_closed_break():
    tracker = _tracker_for(playlists.AD_BREAK_EXPLICIT_START_END_PLAYLIST)
    ad = tracker.breaks[0]

    assert ad.id == "timed-1"
    assert not ad.is_open
    assert ad.start_date == datetime.datetime(2024, 5, 1, 11, 0, tzinfo=UTC)
    assert ad.end_date == datetime.datetime(2024, 5, 1, 11, 0, 30, tzinfo=UTC)


def test_duration_derives_end_date_from_start():
    content = """#EXTM3U
#EXT-X-DATERANGE:ID="d",START-DATE="2024-05-01T10:00:00Z",DURATION=15
#EXTINF:6,
x.ts
"""
    ad = _tracker_for(content).breaks[0]
    assert ad.end_date == datetime.datetime(2024, 5, 1, 10, 0, 15, tzinfo=UTC)
    assert not ad.is_open


def test_breaks_are_sorted_by_start_date():
    content = """#EXTM3U
#EXT-X-DATERANGE:ID="late",START-DATE="2024-05-01T10:00:30Z",SCTE35-OUT=0xFC02
#EXTINF:6,
a.ts
#EXT-X-DATERANGE:ID="early",START-DATE="2024-05-01T10:00:00Z",SCTE35-OUT=0xFC01
#EXTINF:6,
b.ts
"""
    ids = [ad.id for ad in _tracker_for(content).breaks]
    assert ids == ["early", "late"]


def test_ad_breaks_property_on_m3u8():
    playlist = _parse(playlists.DATERANGE_SCTE35_OUT_AND_IN_PLAYLIST)
    assert isinstance(playlist.ad_breaks, AdBreakList)
    assert len(playlist.ad_breaks) == 1


# --------------------------------------------------------------------------- #
# requirement 2: incremental merge, dedup and late closing markers
# --------------------------------------------------------------------------- #
def test_three_sliding_windows_open_then_dedup_then_close():
    tracker = AdBreakTracker()

    # window 1: opening tag only -> one open break
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_1))
    assert len(tracker.breaks) == 1
    first = tracker.breaks.by_id("splice-1")
    assert first is not None
    assert first.is_open

    # window 2: same opening tag re-sent in a shifted window -> no duplicate
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_2))
    assert len(tracker.breaks) == 1
    same = tracker.breaks.by_id("splice-1")
    assert same is first
    assert same.is_open
    # the duplicated tag must not be appended again
    assert len(same.tags) == 1

    # window 3: late SCTE35-IN closes it; a new open break appears
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_3))
    assert len(tracker.breaks) == 2

    closed = tracker.breaks.by_id("splice-1")
    assert not closed.is_open
    assert closed.duration == 30
    assert closed.scte35_in is not None
    assert closed.end_marker is not None
    assert len(closed.tags) == 2

    opened = tracker.breaks.by_id("splice-2")
    assert opened.is_open
    assert opened.scte35_out is not None


def test_closed_and_open_helpers():
    tracker = _tracker_for(playlists.LIVE_AD_SLIDING_WINDOW_3)
    # splice-1 has only an IN here, splice-2 is open
    assert {ad.id for ad in tracker.breaks.open} == {"splice-2"}
    assert {ad.id for ad in tracker.breaks.closed} == {"splice-1"}


def test_merging_same_playlist_twice_is_idempotent():
    playlist = _parse(playlists.DATERANGE_SCTE35_OUT_AND_IN_PLAYLIST)
    tracker = AdBreakTracker()
    tracker.merge(playlist)
    tracker.merge(playlist)

    assert len(tracker.breaks) == 1
    assert len(tracker.breaks[0].tags) == 2


def test_late_end_marker_completes_existing_open_break():
    opener = """#EXTM3U
#EXT-X-DATERANGE:ID="s",START-DATE="2024-05-01T09:00:00Z",SCTE35-OUT=0xFC01
#EXTINF:6,
a.ts
"""
    closer = """#EXTM3U
#EXT-X-DATERANGE:ID="s",DURATION=12,SCTE35-IN=0xFC02
#EXTINF:6,
b.ts
"""
    tracker = _tracker_for(opener)
    assert tracker.breaks[0].is_open

    tracker.merge(_parse(closer))
    ad = tracker.breaks.by_id("s")
    assert not ad.is_open
    assert ad.end_date == datetime.datetime(2024, 5, 1, 9, 0, 12, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# requirement 3: deterministic edge cases
# --------------------------------------------------------------------------- #
def test_missing_clock_anchor_keeps_open_break_with_none_dates():
    tracker = _tracker_for(playlists.AD_BREAK_NO_CLOCK_ANCHOR_PLAYLIST)
    ad = tracker.breaks[0]

    assert ad.id == "anchorless-1"
    assert ad.start_date is None
    assert ad.is_open
    # anchorless break sorts last instead of raising
    assert tracker.breaks[-1] is ad


def test_orphan_closing_marker_is_reported_as_conflict():
    tracker = _tracker_for(playlists.AD_BREAK_ORPHAN_IN_PLAYLIST)
    ad = tracker.breaks[0]

    assert ad.id == "orphan-1"
    assert ad.conflicts
    assert any("no START-DATE" in msg for msg in ad.conflicts)
    assert any("orphan-1" in msg for msg in tracker.conflicts)


def test_end_before_start_is_flagged_but_break_is_emitted():
    tracker = _tracker_for(playlists.AD_BREAK_END_BEFORE_START_PLAYLIST)
    ad = tracker.breaks[0]

    assert ad.id == "reversed-1"
    assert ad.end_date < ad.start_date
    assert any("precedes START-DATE" in msg for msg in ad.conflicts)


def test_conflicting_duplicate_id_is_flagged_and_not_duplicated():
    tracker = _tracker_for(playlists.AD_BREAK_CONFLICTING_ID_PLAYLIST)
    assert len(tracker.breaks) == 1
    ad = tracker.breaks[0]
    # first START-DATE wins
    assert ad.start_date == datetime.datetime(2024, 5, 1, 14, 0, tzinfo=UTC)
    assert any("conflicting" in msg for msg in ad.conflicts)


def test_overlapping_breaks_are_flagged():
    tracker = _tracker_for(playlists.AD_BREAK_OVERLAPPING_PLAYLIST)
    assert len(tracker.breaks) == 2
    for ad in tracker.breaks:
        assert any("overlapping" in msg for msg in ad.conflicts)


def test_end_on_next_break_closes_at_next_same_class_start():
    tracker = _tracker_for(playlists.AD_BREAK_END_ON_NEXT_PLAYLIST)
    first = tracker.breaks.by_id("eon-1")

    assert not first.is_open
    assert first.end_date == datetime.datetime(2024, 5, 1, 16, 0, 20, tzinfo=UTC)
    # the second break stays open until another same-class break arrives
    assert tracker.breaks.by_id("eon-2").is_open


def test_playlist_rollback_does_not_corrupt_state():
    tracker = AdBreakTracker()
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_3))
    # an older window re-arrives (rollback): its opener was never seen, so it
    # fills the missing start of splice-1 rather than duplicating it
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_1))

    assert len(tracker.breaks) == 2
    splice1 = tracker.breaks.by_id("splice-1")
    assert splice1.start_date == datetime.datetime(2024, 5, 1, 10, 0, tzinfo=UTC)
    assert not splice1.is_open
    assert len(splice1.tags) == 2
    # full re-send of everything afterwards changes nothing
    tracker.merge(_parse(playlists.LIVE_AD_SLIDING_WINDOW_3))
    assert len(tracker.breaks) == 2
    assert len(splice1.tags) == 2


def test_non_ad_dateranges_are_ignored():
    # a DATERANGE without SCTE markers or START-DATE carries no timing signal
    content = """#EXTM3U
#EXT-X-DATERANGE:ID="not-an-ad"
#EXTINF:6,
x.ts
"""
    assert _tracker_for(content).breaks == []


# --------------------------------------------------------------------------- #
# requirement 4: original tests / dumping remain intact
# --------------------------------------------------------------------------- #
def test_dumping_playlists_is_unchanged():
    for fixture in (
        playlists.DATERANGE_SIMPLE_PLAYLIST,
        playlists.DATERANGE_SCTE35_OUT_AND_IN_PLAYLIST,
        playlists.DATERANGE_ENDDATE_SCTECMD_PLAYLIST,
        playlists.DATERANGE_IN_PART_PLAYLIST,
    ):
        obj = m3u8.M3U8(fixture)
        dumped = obj.dumps()
        # every DATERANGE ID from the source is still present after a
        # round-trip, and dumping stays stable across repeated calls
        source_ids = {
            line.split('ID="', 1)[1].split('"', 1)[0]
            for line in fixture.splitlines()
            if line.startswith("#EXT-X-DATERANGE")
        }
        dumped_ids = {
            line.split('ID="', 1)[1].split('"', 1)[0]
            for line in dumped.splitlines()
            if line.startswith("#EXT-X-DATERANGE")
        }
        assert source_ids == dumped_ids
        assert obj.dumps() == dumped


def test_adbreak_repr_and_equality():
    a = AdBreak(id="x", start_date=None, end_date=None)
    b = AdBreak(id="x", start_date=None, end_date=None)
    assert a == b
    assert "open" in repr(a)
    b.end_date = datetime.datetime(2024, 5, 1, tzinfo=UTC)
    assert a != b
    assert "closed" in repr(b)


def test_adbreaklist_by_id_missing_returns_none():
    assert AdBreakList().by_id("nope") is None
