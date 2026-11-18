# Copyright 2014 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.

"""
Tests for cross-manifest timeline alignment (m3u8.alignment).

The playlist files under playlists/alignment are produced by
generate_alignment_fixtures.py and describe one program cut asynchronously
into 10s video segments and 6s audio segments, with independent media
sequence numbering, sliding windows, a discontinuity, sparse PDT tags and a
GAP segment.
"""

import datetime
from os.path import abspath, dirname, join

import pytest

import m3u8
from m3u8 import (
    REASON_CLOCK_BACKWARDS,
    REASON_CROSS_DISCONTINUITY,
    REASON_EMPTY_PLAYLIST,
    REASON_NO_COMMON_ANCHOR,
    REASON_OFFSET_OUT_OF_BOUNDS,
    REASON_OUTSIDE_WINDOW,
    REASON_SEGMENT_NOT_IN_PLAYLIST,
    STATUS_ERROR,
    STATUS_GAP,
    STATUS_OK,
    align_segment,
)

utc = datetime.timezone.utc  # noqa: UP017 - datetime.UTC needs Python 3.11
EPOCH = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=utc)

FIXTURE_DIR = abspath(join(dirname(__file__), "playlists", "alignment"))


def load_fixture(name):
    return m3u8.load(join(FIXTURE_DIR, name))


@pytest.fixture
def playlists_():
    return {
        "video_full": load_fixture("video_full.m3u8"),
        "video_early": load_fixture("video_early.m3u8"),
        "video_late": load_fixture("video_late.m3u8"),
        "video_gap": load_fixture("video_gap.m3u8"),
        "audio_full": load_fixture("audio_full.m3u8"),
        "audio_early": load_fixture("audio_early.m3u8"),
        "audio_late": load_fixture("audio_late.m3u8"),
        "audio_mid": load_fixture("audio_mid.m3u8"),
    }


# ---------------------------------------------------------------------------
# fixture sanity checks
# ---------------------------------------------------------------------------


def test_fixtures_model_asynchronous_cuts_and_sliding_windows(playlists_):
    video = playlists_["video_full"]
    audio = playlists_["audio_full"]

    # 10s video segments vs 6s audio segments, different sequence numbering
    assert len(video.segments) == 8
    assert len(audio.segments) == 13
    assert video.media_sequence == 900
    assert audio.media_sequence == 4000
    assert [s.duration for s in video.segments[:2]] == [10.0, 15.0]
    assert audio.segments[2].duration == 13.0

    # the video rendition carries a discontinuity tag, the audio one does not
    assert [s.discontinuity for s in video.segments] == [
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert not any(s.discontinuity for s in audio.segments)

    # sparse PDTs: most video segments must rely on extrapolation
    assert [s.program_date_time is not None for s in video.segments] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert [s.program_date_time is not None for s in audio.segments] == [
        True,
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]

    # the GAP segment is preserved
    assert [bool(s.gap_tag) for s in video.segments] == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        False,
    ]

    # sliding windows cover different ranges and keep their own sequence ids
    assert playlists_["video_late"].media_sequence == 902
    assert playlists_["audio_late"].media_sequence == 4004
    assert playlists_["audio_mid"].media_sequence == 4008
    assert len(playlists_["audio_mid"].segments) == 5
    assert not any(
        s.program_date_time is not None for s in playlists_["audio_mid"].segments
    )


# ---------------------------------------------------------------------------
# happy path: different segment durations, asynchronous cuts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_name,source_index,offset,target_name,expected_uri,expected_offset",
    [
        # video 10s segments -> audio 6s segments, first region
        ("video_full", 0, 0.0, "audio_full", "audio_00.ts", 0.0),
        ("video_full", 0, 7.0, "audio_full", "audio_01.ts", 1.0),
        # audio -> video landing on the first post-discontinuity segments
        ("audio_full", 2, 0.0, "video_full", "video_01.ts", 2.0),
        ("audio_full", 2, 1.0, "video_full", "video_01.ts", 3.0),
        ("video_full", 1, 14.0, "audio_full", "audio_02.ts", 12.0),
        # half-open ranges: a time exactly on a boundary starts the next seg
        ("audio_full", 2, 13.0, "video_full", "video_02.ts", 0.0),
        ("video_full", 2, 3.0, "audio_full", "audio_03.ts", 3.0),
        ("audio_full", 5, 0.0, "video_full", "video_03.ts", 2.0),
        ("audio_full", 8, 4.0, "video_full", "video_05.ts", 4.0),
        ("audio_full", 12, 0.0, "video_full", "video_07.ts", 4.0),
        # offset equal to the source segment duration lands on the final
        # target segment's end, which is still inside its range
        ("video_full", 7, 10.0, "audio_full", "audio_12.ts", 6.0),
    ],
)
def test_aligns_corresponding_program_positions(
    playlists_,
    source_name,
    source_index,
    offset,
    target_name,
    expected_uri,
    expected_offset,
):
    result = align_segment(
        playlists_[source_name],
        source_index,
        playlists_[target_name],
        offset=offset,
    )
    assert result.status == STATUS_OK
    assert result.reason is None
    assert result.target_segment.uri == expected_uri
    assert result.offset == pytest.approx(expected_offset)
    assert result.ok


def test_result_exposes_media_sequence_and_program_clock(playlists_):
    result = align_segment(
        playlists_["video_full"], 0, playlists_["audio_full"], offset=7.0
    )
    assert result.target_index == 1
    assert result.target_media_sequence == 4001
    # program time 7s is before the discontinuity, no wall clock jump yet
    assert result.program_date_time == EPOCH + datetime.timedelta(seconds=7)

    result = align_segment(
        playlists_["video_full"], 2, playlists_["audio_full"], offset=3.0
    )
    # program time 28s is after the discontinuity: wall clock gained 5s
    assert result.program_date_time == EPOCH + datetime.timedelta(seconds=33)


def test_align_segment_accepts_segment_object_and_media_sequence(playlists_):
    video = playlists_["video_full"]
    result = align_segment(
        video, video.segments[3], playlists_["audio_full"], offset=2.0
    )
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "audio_05.ts"

    # an object with a matching media sequence (e.g. kept across a reload)
    # resolves even though it is not the same python object
    foreign = m3u8.Segment(uri="stale.ts", duration=10, media_sequence=903)
    result = align_segment(video, foreign, playlists_["audio_full"], offset=2.0)
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "audio_05.ts"


def test_symmetric_alignments_round_trip(playlists_):
    # the program clock reached from either side must be identical
    forward = align_segment(
        playlists_["video_full"], 4, playlists_["audio_full"], offset=3.5
    )
    backward = align_segment(
        playlists_["audio_full"],
        forward.target_index,
        playlists_["video_full"],
        offset=forward.offset,
    )
    assert forward.program_date_time == backward.program_date_time
    assert backward.target_segment.uri == "video_04.ts"
    assert backward.offset == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# matching error: extrapolation and measured clock drift
# ---------------------------------------------------------------------------


def test_error_is_zero_on_explicit_anchors(playlists_):
    # video segment 1 and audio segment 2 both carry an explicit PDT
    result = align_segment(
        playlists_["video_full"], 1, playlists_["audio_full"], offset=3.0
    )
    assert result.error == 0.0


def test_error_grows_with_extrapolation_distance(playlists_):
    # audio segment 1 is one 6s target-duration step away from its anchor
    result = align_segment(
        playlists_["video_full"], 0, playlists_["audio_full"], offset=7.0
    )
    # 6s of target extrapolation plus the 5s drift measured between the
    # audio PDT anchors (the discontinuity wall-clock jump)
    assert result.error == pytest.approx(11.0)


def test_extrapolation_bound_can_be_tightened():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
s0.ts
#EXTINF:5,
s1.ts
#EXTINF:5,
s2.ts
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:16+00:00
#EXTINF:5,
s3.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
t0.ts
#EXTINF:5,
t1.ts
#EXTINF:5,
t2.ts
"""
    )
    # program time 10s lands on target segment t2. The source segment is two
    # target-duration steps from its anchor (2 * 5s) plus 1s measured drift;
    # the target side adds one more 5s step.
    result = align_segment(source, 2, target, offset=0.0)
    assert result.target_segment.uri == "t2.ts"
    assert result.error == pytest.approx(16.0)

    result = align_segment(source, 2, target, offset=0.0, extrapolation_bound=0.5)
    assert result.error == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# sliding windows
# ---------------------------------------------------------------------------


def test_overlapping_sliding_windows_align(playlists_):
    result = align_segment(
        playlists_["video_late"], 1, playlists_["audio_late"], offset=2.0
    )
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "audio_05.ts"
    assert result.offset == pytest.approx(0.0)
    assert result.target_media_sequence == 4005


def test_position_before_target_window_is_outside(playlists_):
    # video_late starts at program time 25s, audio_late only at 31s
    result = align_segment(
        playlists_["video_late"], 0, playlists_["audio_late"], offset=3.0
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_OUTSIDE_WINDOW
    assert result.target_segment is None


def test_window_without_any_pdt_has_no_common_anchor(playlists_):
    result = align_segment(
        playlists_["video_full"], 4, playlists_["audio_mid"], offset=2.0
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_NO_COMMON_ANCHOR

    result = align_segment(playlists_["audio_mid"], 0, playlists_["video_full"])
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_NO_COMMON_ANCHOR


def test_early_windows_share_the_first_anchor(playlists_):
    result = align_segment(
        playlists_["video_early"], 0, playlists_["audio_early"], offset=4.0
    )
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "audio_00.ts"
    assert result.offset == pytest.approx(4.0)
    assert result.error == 0.0


# ---------------------------------------------------------------------------
# GAP segments
# ---------------------------------------------------------------------------


def test_target_gap_segment_is_reported_with_status_gap(playlists_):
    # program time 69s falls into video segment 6 which carries EXT-X-GAP
    result = align_segment(
        playlists_["audio_full"], 10, playlists_["video_full"], offset=2.0
    )
    assert result.status == STATUS_GAP
    assert result.reason is None
    assert result.target_segment.uri == "video_06.ts"
    assert result.offset == pytest.approx(4.0)
    assert result.ok  # GAP results still carry a usable position


def test_gap_window_reaches_the_gap_segment_by_extrapolation(playlists_):
    result = align_segment(
        playlists_["audio_full"], 10, playlists_["video_gap"], offset=2.0
    )
    assert result.status == STATUS_GAP
    assert result.target_segment.uri == "video_06.ts"
    assert result.target_index == 2


def test_aligning_from_a_gap_segment_still_works(playlists_):
    result = align_segment(
        playlists_["video_full"], 6, playlists_["audio_full"], offset=3.0
    )
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "audio_10.ts"
    assert result.offset == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# discontinuity handling
# ---------------------------------------------------------------------------


def test_wall_clock_hole_around_discontinuity_is_reported(playlists_):
    # the audio PDT jumps 5s at the discontinuity while no tag marks it, so
    # wall-clock times 12-17s are uncovered; video t=10 maps to wall time 15s
    result = align_segment(
        playlists_["video_full"], 1, playlists_["audio_full"], offset=0.0
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_CROSS_DISCONTINUITY

    # the same hole is reached from the audio side at wall-clock time 16s
    result = align_segment(
        playlists_["audio_full"], 1, playlists_["video_full"], offset=5.0
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_CROSS_DISCONTINUITY


def test_positions_on_each_side_of_discontinuity_align(playlists_):
    before = align_segment(
        playlists_["video_full"], 0, playlists_["audio_full"], offset=9.9
    )
    assert before.status == STATUS_OK
    assert before.target_segment.uri == "audio_01.ts"
    assert before.offset == pytest.approx(3.9)

    after = align_segment(
        playlists_["audio_full"], 2, playlists_["video_full"], offset=0.0
    )
    assert after.status == STATUS_OK
    assert after.target_segment.uri == "video_01.ts"
    assert after.offset == pytest.approx(2.0)


def test_matching_discontinuity_pdt_on_both_sides_maps_boundary():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
s0.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:10+00:00
#EXTINF:5,
s1.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:3
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:2.5,
t0.ts
#EXTINF:2.5,
t1.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:10+00:00
#EXTINF:2.5,
t2.ts
#EXTINF:2.5,
t3.ts
"""
    )
    result = align_segment(source, 1, target, offset=0.0)
    assert result.status == STATUS_OK
    assert result.target_segment.uri == "t2.ts"
    assert result.offset == 0.0
    assert result.program_date_time == EPOCH + datetime.timedelta(seconds=10)


def test_discontinuity_without_following_pdt_cannot_be_crossed():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
s0.ts
#EXTINF:5,
s1.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
t0.ts
#EXT-X-DISCONTINUITY
#EXTINF:5,
t1.ts
#EXTINF:5,
t2.ts
"""
    )
    # program time 7s needs the unanchored post-discontinuity target segment
    result = align_segment(source, 1, target, offset=2.0)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_CROSS_DISCONTINUITY


# ---------------------------------------------------------------------------
# clock going backwards
# ---------------------------------------------------------------------------


def test_backwards_clock_in_source_is_rejected():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
s0.ts
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:03+00:00
#EXTINF:5,
s1.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
t0.ts
"""
    )
    result = align_segment(source, 1, target)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_CLOCK_BACKWARDS


def test_backwards_clock_in_target_is_rejected():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:10,
s0.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
t0.ts
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:03+00:00
#EXTINF:5,
t1.ts
"""
    )
    result = align_segment(source, 0, target, offset=6.0)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_CLOCK_BACKWARDS


# ---------------------------------------------------------------------------
# remaining error cases
# ---------------------------------------------------------------------------


def test_playlists_without_pdt_have_no_common_anchor():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXTINF:5,
s0.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXTINF:5,
t0.ts
"""
    )
    result = align_segment(source, 0, target)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_NO_COMMON_ANCHOR


def test_unanchored_segments_before_first_target_pdt_are_unresolvable():
    source = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+00:00
#EXTINF:5,
s0.ts
"""
    )
    target = m3u8.loads(
        """
#EXTM3U
#EXT-X-TARGETDURATION:5
#EXTINF:5,
old.ts
#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:10+00:00
#EXTINF:5,
new.ts
"""
    )
    result = align_segment(source, 0, target, offset=2.0)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_NO_COMMON_ANCHOR


def test_offset_out_of_bounds(playlists_):
    result = align_segment(
        playlists_["video_full"], 0, playlists_["audio_full"], offset=10.5
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_OFFSET_OUT_OF_BOUNDS

    result = align_segment(
        playlists_["video_full"], 0, playlists_["audio_full"], offset=-0.1
    )
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_OFFSET_OUT_OF_BOUNDS


def test_empty_playlists():
    empty = m3u8.M3U8("#EXTM3U")
    full = load_fixture("video_full.m3u8")

    result = align_segment(empty, 0, full)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_EMPTY_PLAYLIST

    result = align_segment(full, 0, empty)
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_EMPTY_PLAYLIST


def test_source_segment_not_in_playlist(playlists_):
    result = align_segment(playlists_["video_full"], 99, playlists_["audio_full"])
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_SEGMENT_NOT_IN_PLAYLIST

    # a segment object coming from another playlist cannot resolve either
    foreign = playlists_["audio_full"].segments[0]
    result = align_segment(playlists_["video_full"], foreign, playlists_["audio_full"])
    assert result.status == STATUS_ERROR
    assert result.reason == REASON_SEGMENT_NOT_IN_PLAYLIST
