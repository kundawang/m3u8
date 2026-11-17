import pytest

import m3u8
from m3u8 import (
    DuplicateURIError,
    IncompletePartError,
    InvalidDurationError,
    LowLatencyWindow,
    PublishOrderError,
    WindowTooSmallError,
)

PARTS_PER_SEGMENT = 4
PART_DURATION = 0.5
SEGMENT_DURATION = 2.0


def publish_segment(window, index, parts=PARTS_PER_SEGMENT, next_uri=None):
    """Publish one full segment part by part and return the playlist texts
    observed right after each part was completed."""
    snapshots = []
    first_part = f"seg{index}.part0.m4s"
    if window.preload_hint_uri != first_part:
        window.start_part(first_part)
    for part_index in range(parts):
        following = (
            f"seg{index}.part{part_index + 1}.m4s" if part_index + 1 < parts else None
        )
        window.complete_part(PART_DURATION, next_uri=following)
        snapshots.append(window.dumps())
    window.complete_segment(f"seg{index}.m4s", SEGMENT_DURATION, next_uri=next_uri)
    return snapshots


def test_three_segments_publish_parts_hints_and_trims_window():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=2
    )

    # --- segment 0: every part refreshes the playlist and hints the next ---
    snapshots = publish_segment(window, 0, next_uri="seg1.part0.m4s")
    for part_index, text in enumerate(snapshots):
        assert "#EXT-X-PART-INF:PART-TARGET=0.5" in text
        assert (
            "#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,"
            "HOLD-BACK=6,PART-HOLD-BACK=1.5" in text
        )
        # parts published so far show up as EXT-X-PART, newest last
        for published in range(part_index + 1):
            assert f'#EXT-X-PART:DURATION=0.5,URI="seg0.part{published}.m4s"' in text
        # the hint always announces the piece being written next; once the
        # last part of the segment is done nothing is being written yet
        if part_index + 1 < PARTS_PER_SEGMENT:
            assert (
                f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="seg0.part{part_index + 1}.m4s"'
                in text
            )
        else:
            assert "#EXT-X-PRELOAD-HINT" not in text
        # no complete segment yet
        assert "#EXTINF" not in text
        assert "#EXT-X-MEDIA-SEQUENCE" not in text  # still 0, the default

    # --- segments 1 and 2: the window slides and the sequence advances ---
    publish_segment(window, 1, next_uri="seg2.part0.m4s")
    text = window.dumps()
    assert "seg0.m4s" in text and "seg1.m4s" in text
    assert "#EXT-X-MEDIA-SEQUENCE" not in text  # nothing trimmed yet

    publish_segment(window, 2, next_uri="seg3.part0.m4s")
    text = window.dumps()
    # window of 2 keeps only the two newest complete segments
    assert "#EXT-X-MEDIA-SEQUENCE:1" in text
    assert "seg0.m4s" not in text
    assert "seg0.part0.m4s" not in text
    assert "#EXTINF:2,\nseg1.m4s" in text
    assert "#EXTINF:2,\nseg2.m4s" in text
    # complete segments keep their parts, newest segment first in order
    assert text.index('URI="seg1.part0.m4s"') < text.index("#EXTINF:2,\nseg1.m4s")
    for part_index in range(PARTS_PER_SEGMENT):
        assert f'URI="seg2.part{part_index}.m4s"' in text
    # hint points at the first part of the upcoming fourth segment
    assert '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="seg3.part0.m4s"' in text
    assert window.media_sequence == 1

    # --- parts of the open segment are published before its EXTINF ---
    window.complete_part(PART_DURATION, next_uri="seg3.part1.m4s")
    window.complete_part(PART_DURATION, next_uri="seg3.part2.m4s")
    text = window.dumps()
    assert text.index('URI="seg3.part0.m4s"') < text.index('URI="seg3.part1.m4s"')
    assert "seg3.m4s\n" not in text
    assert '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="seg3.part2.m4s"' in text


def test_playlist_round_trips_through_parser():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=2
    )
    publish_segment(window, 0, next_uri="seg1.part0.m4s")
    publish_segment(window, 1, next_uri="seg2.part0.m4s")
    publish_segment(window, 2, next_uri="seg3.part0.m4s")
    window.complete_part(PART_DURATION, independent=True)

    parsed = m3u8.loads(window.dumps())

    assert parsed.media_sequence == 1
    assert parsed.target_duration == SEGMENT_DURATION
    assert parsed.part_inf.part_target == PART_DURATION
    assert parsed.server_control.can_block_reload == "YES"
    assert parsed.server_control.hold_back == 6.0
    assert parsed.server_control.part_hold_back == 1.5
    assert parsed.preload_hint is None  # no part currently being written

    # two complete segments plus the still-open one carrying a single part
    assert len(parsed.segments) == 3
    first, second, open_segment = parsed.segments
    assert (first.uri, first.duration) == ("seg1.m4s", SEGMENT_DURATION)
    assert (second.uri, second.duration) == ("seg2.m4s", SEGMENT_DURATION)
    assert [part.uri for part in second.parts] == [
        f"seg2.part{i}.m4s" for i in range(PARTS_PER_SEGMENT)
    ]
    assert open_segment.uri is None
    assert [part.uri for part in open_segment.parts] == ["seg3.part0.m4s"]
    assert open_segment.parts[0].independent == "YES"

    # re-serializing the parsed playlist is stable
    assert m3u8.loads(parsed.dumps()).dumps() == parsed.dumps()


def test_preload_hint_round_trips_while_part_is_open():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=3
    )
    window.start_part("seg0.part0.m4s")
    parsed = m3u8.loads(window.dumps())
    assert parsed.preload_hint.hint_type == "PART"
    assert parsed.preload_hint.uri == "seg0.part0.m4s"


def test_discontinuity_survives_until_its_segment_is_trimmed():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=2
    )
    window.add_discontinuity()
    publish_segment(window, 0, next_uri="seg1.part0.m4s")

    text = window.dumps()
    assert "#EXT-X-DISCONTINUITY\n" in text
    assert "#EXT-X-DISCONTINUITY-SEQUENCE" not in text  # still 0
    assert text.index("#EXT-X-DISCONTINUITY") < text.index('URI="seg0.part0.m4s"')

    publish_segment(window, 1, next_uri="seg2.part0.m4s")
    publish_segment(window, 2, next_uri="seg3.part0.m4s")

    # seg0 carried the discontinuity out of the window
    text = window.dumps()
    assert "#EXT-X-DISCONTINUITY\n" not in text
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in text
    parsed = m3u8.loads(text)
    assert parsed.discontinuity_sequence == 1
    assert parsed.media_sequence == 1


def test_endlist_closes_the_window():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=3
    )
    publish_segment(window, 0)
    window.end()

    text = window.dumps()
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert "#EXT-X-PRELOAD-HINT" not in text
    assert m3u8.loads(text).is_endlist

    with pytest.raises(PublishOrderError):
        window.start_part("seg1.part0.m4s")
    with pytest.raises(PublishOrderError):
        window.complete_segment("seg1.m4s", SEGMENT_DURATION)
    with pytest.raises(PublishOrderError):
        window.end()


def test_incomplete_part_blocks_further_events():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=3
    )
    window.start_part("seg0.part0.m4s")

    with pytest.raises(IncompletePartError):
        window.start_part("seg0.part1.m4s")
    with pytest.raises(IncompletePartError):
        window.complete_segment("seg0.m4s", SEGMENT_DURATION)
    with pytest.raises(IncompletePartError):
        window.end()

    # the window is untouched and the part can still be completed
    window.complete_part(PART_DURATION)
    assert [part.uri for part in window.current_parts] == ["seg0.part0.m4s"]


def test_duplicate_and_regressing_uris_are_rejected():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=2
    )
    window.start_part("seg0.part0.m4s")
    with pytest.raises(DuplicateURIError):
        window.start_part("seg0.part0.m4s")  # already the pending hint
    window.complete_part(PART_DURATION, next_uri="seg0.part1.m4s")
    with pytest.raises(DuplicateURIError):
        window.start_part("seg0.part0.m4s")  # already published
    window.complete_part(PART_DURATION)
    window.complete_segment("seg0.m4s", SEGMENT_DURATION)

    publish_segment(window, 1)
    publish_segment(window, 2)  # pushes seg0 out of the window
    assert "seg0.m4s" not in window.dumps()

    # URIs must not go backwards either: trimmed URIs stay reserved
    with pytest.raises(DuplicateURIError):
        window.start_part("seg0.part0.m4s")
    window.start_part("seg3.part0.m4s")
    window.complete_part(PART_DURATION)
    with pytest.raises(DuplicateURIError):
        window.complete_segment("seg0.m4s", SEGMENT_DURATION)


def test_invalid_durations_are_rejected():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=3
    )
    window.start_part("seg0.part0.m4s")
    for bad in (0, -0.5, "0.5", None, float("inf"), float("nan"), True):
        with pytest.raises(InvalidDurationError):
            window.complete_part(bad)
    with pytest.raises(InvalidDurationError):
        window.complete_part(0.6)  # longer than PART-TARGET
    window.complete_part(PART_DURATION)

    with pytest.raises(InvalidDurationError):
        window.complete_segment("seg0.m4s", 2.5)  # longer than TARGETDURATION
    with pytest.raises(InvalidDurationError):
        window.complete_segment("seg0.m4s", 0.4)  # shorter than its parts
    window.complete_segment("seg0.m4s", SEGMENT_DURATION)

    with pytest.raises(InvalidDurationError):
        LowLatencyWindow(target_duration=0, part_target=PART_DURATION)
    with pytest.raises(InvalidDurationError):
        LowLatencyWindow(
            target_duration=SEGMENT_DURATION, part_target=3.0
        )  # part target above segment target


def test_window_must_hold_minimum_segments():
    with pytest.raises(WindowTooSmallError):
        LowLatencyWindow(
            target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=1
        )
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=2
    )
    assert window.window_size == 2


def test_out_of_order_events_are_rejected():
    window = LowLatencyWindow(
        target_duration=SEGMENT_DURATION, part_target=PART_DURATION, window_size=3
    )
    with pytest.raises(PublishOrderError):
        window.complete_part(PART_DURATION)  # nothing started
    with pytest.raises(PublishOrderError):
        window.complete_segment("seg0.m4s", SEGMENT_DURATION)  # no parts yet

    window.start_part("seg0.part0.m4s")
    with pytest.raises(PublishOrderError):
        window.add_discontinuity()  # a part is already open
    window.complete_part(PART_DURATION)
    with pytest.raises(PublishOrderError):
        window.add_discontinuity()  # segment already has published parts
    window.complete_segment("seg0.m4s", SEGMENT_DURATION)

    window.start_part("seg1.part0.m4s")
    window.complete_part(PART_DURATION)
    with pytest.raises(PublishOrderError):
        window.end()  # segment left incomplete


def test_server_control_parameters_are_consistent_and_overridable():
    window = LowLatencyWindow(
        target_duration=4, part_target=1.0, window_size=3, can_block_reload=False
    )
    text = window.dumps()
    assert "CAN-BLOCK-RELOAD" not in text
    assert "HOLD-BACK=12" in text
    assert "PART-HOLD-BACK=3" in text

    custom = LowLatencyWindow(
        target_duration=4,
        part_target=1.0,
        window_size=3,
        hold_back=8.0,
        part_hold_back=2.5,
        can_skip_until=24.0,
    )
    parsed = m3u8.loads(custom.dumps())
    assert parsed.server_control.hold_back == 8.0
    assert parsed.server_control.part_hold_back == 2.5
    assert parsed.server_control.can_skip_until == 24.0
