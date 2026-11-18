# Copyright 2014 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.
"""
Cross-manifest timeline alignment.

The same live program can be published as several media playlists (for example
a video rendition and an audio rendition) whose segments have different
durations, are cut asynchronously and slide independently. Switching between
those playlists requires finding the same program position on both sides.

HLS exposes the program clock through ``EXT-X-PROGRAM-DATE-TIME`` (PDT) tags
and resets it on ``EXT-X-DISCONTINUITY`` boundaries. This module builds a
program-clock timeline for every playlist, extrapolates the clock across
segments that do not carry their own PDT and maps a source segment plus an
in-segment offset onto the target playlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from m3u8.model import M3U8, Segment

#: The exact program position was found on an available target segment.
STATUS_OK = "ok"
#: The program position was found but the target segment carries an
#: ``EXT-X-GAP`` tag, i.e. the media is missing on the server.
STATUS_GAP = "gap"
#: The position could not be mapped; see the reason constants below.
STATUS_ERROR = "error"

REASON_EMPTY_PLAYLIST = "empty_playlist"
REASON_NO_COMMON_ANCHOR = "no_common_anchor"
REASON_CLOCK_BACKWARDS = "clock_backwards"
REASON_CROSS_DISCONTINUITY = "cross_discontinuity"
REASON_OUTSIDE_WINDOW = "outside_window"
REASON_OFFSET_OUT_OF_BOUNDS = "offset_out_of_bounds"
REASON_SEGMENT_NOT_IN_PLAYLIST = "segment_not_in_playlist"

# Floating point tolerance when comparing program clocks.
_EPSILON_SECONDS = 1e-9
_EPSILON = timedelta(seconds=_EPSILON_SECONDS)


@dataclass
class AlignmentResult:
    """
    Outcome of :func:`align_segment`.

    Attributes:

    `status`
      One of :data:`STATUS_OK`, :data:`STATUS_GAP` or :data:`STATUS_ERROR`.
      A GAP result still carries the target segment and offset, the media
      itself is just unavailable.

    `reason`
      ``None`` on success, otherwise one of the ``REASON_*`` constants
      explaining why the mapping failed.

    `source_segment` / `target_segment`
      The involved :class:`~m3u8.model.Segment` objects. ``target_segment``
      is ``None`` on error.

    `target_index` / `target_media_sequence`
      Position of the target segment inside the target playlist and its
      media sequence number.

    `offset`
      Offset in seconds from the start of the target segment.

    `program_date_time`
      Absolute program clock (timezone aware datetime) of the requested
      source position.

    `error`
      Estimated worst-case mapping error in seconds, i.e. how far the real
      program position may be from the returned one. It combines clock drift
      measured between explicit PDT anchors and the uncertainty of
      extrapolating the clock over segments without PDT.

    `message`
      Human readable explanation, mainly useful for error results.
    """

    status: str
    reason: str | None = None
    source_segment: Segment | None = None
    target_segment: Segment | None = None
    target_index: int | None = None
    target_media_sequence: int | None = None
    offset: float | None = None
    program_date_time: datetime | None = None
    error: float | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_OK, STATUS_GAP)


class _TimelineEntry:
    """A segment placed on the program clock."""

    def __init__(
        self,
        index: int,
        segment: Segment,
        region: int,
        start: datetime | None,
        explicit: bool,
        clock_backwards: bool,
    ) -> None:
        self.index = index
        self.segment = segment
        self.region = region
        self.start = start
        self.explicit = explicit
        self.clock_backwards = clock_backwards
        self.duration = float(segment.duration or 0.0)
        self.gap = bool(segment.gap_tag)

    @property
    def end(self) -> datetime | None:
        if self.start is None:
            return None
        return self.start + timedelta(seconds=self.duration)


class _Region:
    """A continuous run of segments between discontinuity boundaries."""

    def __init__(self, region_id: int, target_duration: float | None) -> None:
        self.id = region_id
        self.entries: list[_TimelineEntry] = []
        # (entry index, program clock) for every explicit PDT anchor
        self.anchors: list[tuple[int, datetime]] = []
        self.target_duration = float(target_duration) if target_duration else 0.0
        self.drift = 0.0


def _build_timeline(playlist: M3U8) -> list[_TimelineEntry]:
    """
    Assign a program clock to every segment.

    The clock comes from an explicit PDT when one is present and is otherwise
    extrapolated from the previous segment inside the same region. Clocks are
    *not* extrapolated across discontinuity boundaries: the segments after a
    discontinuity stay unanchored until the next explicit PDT. An explicit
    PDT that moves backwards without a discontinuity tag poisons the rest of
    the timeline.
    """
    entries: list[_TimelineEntry] = []
    region = 0
    previous: _TimelineEntry | None = None
    broken = False

    for index, segment in enumerate(playlist.segments):
        if segment.discontinuity:
            region += 1

        start = None
        explicit = False
        backwards = False

        if segment.program_date_time is not None:
            start = segment.program_date_time
            explicit = True
            if (
                not broken
                and previous is not None
                and previous.start is not None
                and previous.end is not None
                and not segment.discontinuity
                and start < previous.end
            ):
                # The wall clock jumped backwards inside one continuous region.
                broken = True
                backwards = True
        elif (
            not broken
            and previous is not None
            and previous.start is not None
            and previous.region == region
        ):
            start = previous.end

        entry = _TimelineEntry(
            index=index,
            segment=segment,
            region=region,
            start=start,
            explicit=explicit,
            clock_backwards=backwards or broken,
        )
        entries.append(entry)
        previous = entry

    return entries


def _build_regions(entries: list[_TimelineEntry], playlist: M3U8) -> dict[int, _Region]:
    regions: dict[int, _Region] = {}
    for entry in entries:
        region = regions.get(entry.region)
        if region is None:
            region = _Region(entry.region, playlist.target_duration)
            regions[entry.region] = region
        region.entries.append(entry)
        if entry.explicit and entry.start is not None:
            region.anchors.append((entry.index, entry.start))

    for region in regions.values():
        region.drift = _measure_drift(region)
    return regions


def _measure_drift(region: _Region) -> float:
    """
    Largest deviation between the clock advertised by consecutive PDT anchors
    and the clock obtained by adding the intervening EXTINF durations.
    """
    drift = 0.0
    anchors = region.anchors
    # itertools.pairwise() is only available from Python 3.10
    for (index_a, clock_a), (index_b, clock_b) in zip(anchors, anchors[1:]):  # noqa: RUF007
        nominal = sum(
            entry.duration
            for entry in region.entries
            if index_a <= entry.index < index_b
        )
        observed = (clock_b - clock_a).total_seconds()
        drift = max(drift, abs(observed - nominal))
    return drift


def _uncertainty(
    region: _Region,
    entry: _TimelineEntry,
    extrapolation_bound: float | None,
) -> float | None:
    """
    Worst-case clock error for ``entry`` inside ``region`` in seconds.

    Explicit PDT anchors are exact. For other segments the per-segment
    duration tolerance (the playlist target duration by default) is charged
    once per segment separating the position from the nearest anchor, plus
    the drift observed between the anchors of the region.
    """
    if entry.explicit:
        return 0.0
    if not region.anchors:
        return None

    bound = (
        float(extrapolation_bound)
        if extrapolation_bound is not None
        else region.target_duration
    )

    left = [anchor for anchor in region.anchors if anchor[0] <= entry.index]
    right = [anchor for anchor in region.anchors if anchor[0] >= entry.index]
    distances = []
    if left:
        distances.append(entry.index - left[-1][0])
    if right:
        distances.append(right[0][0] - entry.index)
    segments_to_anchor = min(distances)

    return bound * segments_to_anchor + region.drift


def _resolve_source_entry(
    entries: list[_TimelineEntry], source_segment: Segment | int
) -> tuple[_TimelineEntry | None, str | None]:
    if isinstance(source_segment, int):
        if 0 <= source_segment < len(entries):
            return entries[source_segment], None
        return None, REASON_SEGMENT_NOT_IN_PLAYLIST

    # Identity first: the segment usually comes from the same parsed object.
    for entry in entries:
        if entry.segment is source_segment:
            return entry, None

    # Fallback for segments held across a playlist reload: the object is new
    # but its media sequence number still identifies it uniquely inside the
    # playlist.
    sequence = getattr(source_segment, "media_sequence", None)
    if sequence is not None:
        for entry in entries:
            if entry.segment.media_sequence == sequence:
                return entry, None
    return None, REASON_SEGMENT_NOT_IN_PLAYLIST


def _locate_target(
    entries: list[_TimelineEntry], program_time: datetime
) -> tuple[_TimelineEntry | None, str | None]:
    """Find the target segment whose program-clock range contains the time."""
    if not entries:
        return None, REASON_EMPTY_PLAYLIST

    anchored = [entry for entry in entries if entry.start is not None]
    if not anchored:
        return None, REASON_NO_COMMON_ANCHOR

    first = anchored[0]
    last = anchored[-1]

    if program_time < first.start:
        if first.index == 0:
            return None, REASON_OUTSIDE_WINDOW
        # The playlist kept segments older than its first anchor: their
        # program clock cannot be established.
        return None, REASON_NO_COMMON_ANCHOR

    # Segments use the half-open range [start, end): a program time exactly
    # on a boundary is returned at the start of the next segment, which is a
    # clean switch point. Only the final segment may contain its own end.
    for position, entry in enumerate(anchored):
        if entry.clock_backwards:
            return None, REASON_CLOCK_BACKWARDS
        end = entry.end
        is_last = position == len(anchored) - 1
        if entry.start - _EPSILON <= program_time and (
            program_time < end - _EPSILON
            or (is_last and program_time <= end + _EPSILON)
        ):
            return entry, None

    # The time is not covered by any segment.
    if program_time > last.end + _EPSILON:
        if last.index < len(entries) - 1:
            # Segments exist after the final anchor but their program clock
            # cannot be established, usually because a discontinuity was not
            # followed by a new PDT tag.
            following = entries[last.index + 1]
            if following.region != last.region:
                return None, REASON_CROSS_DISCONTINUITY
            if following.clock_backwards or last.clock_backwards:
                return None, REASON_CLOCK_BACKWARDS
            return None, REASON_NO_COMMON_ANCHOR
        return None, REASON_OUTSIDE_WINDOW

    # The time falls into a hole between two anchored segments.
    previous = anchored[0]
    for entry in anchored[1:]:
        if previous.end + _EPSILON < program_time < entry.start - _EPSILON:
            if entry.region != previous.region:
                return None, REASON_CROSS_DISCONTINUITY
            # A PDT jump without a discontinuity leaves an uncovered hole.
            return None, REASON_CROSS_DISCONTINUITY
        previous = entry

    return None, REASON_CROSS_DISCONTINUITY


def _error_result(
    reason: str,
    message: str,
    source_segment: Segment | None = None,
) -> AlignmentResult:
    return AlignmentResult(
        status=STATUS_ERROR,
        reason=reason,
        message=message,
        source_segment=source_segment,
    )


def align_segment(
    source: M3U8,
    source_segment: Segment | int,
    target: M3U8,
    offset: float = 0.0,
    extrapolation_bound: float | None = None,
) -> AlignmentResult:
    """
    Map a position in ``source`` onto the equivalent position in ``target``.

    Parameters:

    `source` / `target`
      The two parsed media playlists (:class:`~m3u8.M3U8` objects) of the
      same program, for instance a video and an audio rendition.

    `source_segment`
      The segment to align from: either a :class:`~m3u8.model.Segment`
      belonging to ``source`` or its zero based segment index.

    `offset`
      Offset in seconds from the start of ``source_segment``. Must be within
      the segment duration.

    `extrapolation_bound`
      Assumed worst-case per-segment duration error used while extrapolating
      the program clock over segments without their own PDT. Defaults to the
      playlist ``EXT-X-TARGETDURATION``, which is a conservative bound.

    Returns an :class:`AlignmentResult` with the target segment, the offset
    inside it, the absolute program time and an estimated mapping error.
    Distinct failure modes (no common PDT anchor, a backwards program clock,
    crossing a discontinuity, a GAP-only position, a position outside the
    sliding window or an out of range offset) are reported through the
    result ``status``/``reason`` instead of raising.
    """
    offset = float(offset)

    if not source.segments:
        return _error_result(
            REASON_EMPTY_PLAYLIST, "the source playlist has no segments"
        )
    if not target.segments:
        return _error_result(
            REASON_EMPTY_PLAYLIST,
            "the target playlist has no segments",
            source_segment
            if isinstance(source_segment, Segment)
            else _segment_at(source, source_segment),
        )

    source_entries = _build_timeline(source)
    source_entry, resolve_reason = _resolve_source_entry(source_entries, source_segment)
    if source_entry is None:
        return _error_result(
            resolve_reason,
            "the source segment does not belong to the source playlist",
            _segment_at(source, source_segment)
            if isinstance(source_segment, int)
            else source_segment,
        )

    if offset < -_EPSILON_SECONDS or offset > source_entry.duration + _EPSILON_SECONDS:
        return _error_result(
            REASON_OFFSET_OUT_OF_BOUNDS,
            f"offset {offset:.3f}s is outside the source segment "
            f"duration {source_entry.duration:.3f}s",
            source_segment=source_entry.segment,
        )

    if source_entry.start is None:
        return _error_result(
            REASON_NO_COMMON_ANCHOR,
            "the source segment cannot be placed on the program clock: "
            "no EXT-X-PROGRAM-DATE-TIME anchor reaches it",
            source_segment=source_entry.segment,
        )
    if source_entry.clock_backwards:
        return _error_result(
            REASON_CLOCK_BACKWARDS,
            "an EXT-X-PROGRAM-DATE-TIME tag moved backwards without a "
            "discontinuity before the source segment",
            source_segment=source_entry.segment,
        )

    program_time = source_entry.start + timedelta(seconds=offset)

    target_entries = _build_timeline(target)
    target_entry, target_reason = _locate_target(target_entries, program_time)
    if target_entry is None:
        return _error_result(
            target_reason,
            _failure_message(target_reason, program_time),
            source_segment=source_entry.segment,
        )

    source_regions = _build_regions(source_entries, source)
    target_regions = _build_regions(target_entries, target)
    source_error = _uncertainty(
        source_regions[source_entry.region], source_entry, extrapolation_bound
    )
    target_error = _uncertainty(
        target_regions[target_entry.region], target_entry, extrapolation_bound
    )
    if source_error is None or target_error is None:
        return _error_result(
            REASON_NO_COMMON_ANCHOR,
            "the two playlists do not share an anchored program clock",
            source_segment=source_entry.segment,
        )

    target_offset = (program_time - target_entry.start).total_seconds()
    # Boundary comparisons may leave a rounding-sized negative offset.
    if abs(target_offset) < _EPSILON_SECONDS:
        target_offset = 0.0

    status = STATUS_GAP if target_entry.gap else STATUS_OK
    return AlignmentResult(
        status=status,
        reason=None,
        source_segment=source_entry.segment,
        target_segment=target_entry.segment,
        target_index=target_entry.index,
        target_media_sequence=target_entry.segment.media_sequence,
        offset=target_offset,
        program_date_time=program_time,
        error=source_error + target_error,
        message=None,
    )


def _segment_at(playlist: M3U8, index: int) -> Segment | None:
    if isinstance(index, int) and 0 <= index < len(playlist.segments):
        return playlist.segments[index]
    return None


def _failure_message(reason: str, program_time: datetime) -> str:
    messages = {
        REASON_NO_COMMON_ANCHOR: "no shared EXT-X-PROGRAM-DATE-TIME anchor "
        f"covers program time {program_time}",
        REASON_CLOCK_BACKWARDS: "the target program clock runs backwards at "
        f"program time {program_time}",
        REASON_CROSS_DISCONTINUITY: f"program time {program_time} falls into a "
        "discontinuity gap with no target segment covering it",
        REASON_OUTSIDE_WINDOW: f"program time {program_time} is outside the "
        "target sliding window",
        REASON_EMPTY_PLAYLIST: "the target playlist has no segments",
    }
    return messages.get(reason, reason)
