# Copyright 2014 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.
"""Low-latency (LL-HLS) sliding-window live playlist constructor.

Implements the "publish while writing" workflow for live streams: every
completed partial segment refreshes the media playlist and moves the
``EXT-X-PRELOAD-HINT`` to the next part being encoded, while completed
segments slide a fixed-size window forward, keeping the media sequence
and discontinuity sequence consistent.

Typical usage::

    window = LowLatencyWindow(target_duration=2, part_target=0.5, window_size=3)
    window.start_part("seg0.part0.m4s")
    window.complete_part(0.5, next_uri="seg0.part1.m4s")
    ...
    window.complete_segment("seg0.m4s", 2.0, next_uri="seg1.part0.m4s")
    ...
    window.end()
    playlist_text = window.dumps()

Every event mutates the window state; ``playlist()``/``dumps()`` always
reflect the playlist a client would fetch at that instant, and the output
round-trips through ``m3u8.loads``.
"""

import math

from m3u8.model import (
    M3U8,
    PartialSegment,
    PartialSegmentList,
    PartInformation,
    PreloadHint,
    Segment,
    SegmentList,
    ServerControl,
)

#: Minimum number of complete segments the sliding window must hold.
#: LL-HLS clients need enough complete media on screen to survive a
#: playlist reload, so a window smaller than this is rejected.
MIN_WINDOW_SEGMENTS = 2

#: Tolerance used when comparing part/segment durations.
_DURATION_EPSILON = 1e-6

#: LL-HLS requires at least protocol version 9.
LL_HLS_VERSION = 9


class LiveWindowError(Exception):
    """Base class for sliding-window construction errors."""


class IncompletePartError(LiveWindowError):
    """A part was started but not completed before the next event."""


class DuplicateURIError(LiveWindowError):
    """A URI that was already published (even if since trimmed) was reused."""


class InvalidDurationError(LiveWindowError):
    """A duration is not a positive finite number or exceeds its target."""


class WindowTooSmallError(LiveWindowError):
    """The sliding window cannot hold the minimum number of segments."""


class PublishOrderError(LiveWindowError):
    """Events were published out of order."""


def _validate_duration(value, limit, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidDurationError(f"{what} duration must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise InvalidDurationError(
            f"{what} duration must be a positive finite number, got {value!r}"
        )
    if limit is not None and value > limit + _DURATION_EPSILON:
        raise InvalidDurationError(
            f"{what} duration {value} exceeds its target {limit}"
        )
    return float(value)


class LowLatencyWindow:
    """Incrementally builds an LL-HLS live media playlist.

    Parameters:

     `target_duration`
       value for EXT-X-TARGETDURATION; no segment may be longer

     `part_target`
       value for EXT-X-PART-INF:PART-TARGET; no part may be longer

     `window_size`
       number of complete segments kept in the playlist; older segments
       are trimmed and the media sequence is advanced. Must be at least
       ``MIN_WINDOW_SEGMENTS``

     `media_sequence` / `discontinuity_sequence`
       starting sequence numbers (e.g. when continuing a previous window)

     `can_block_reload`, `hold_back`, `part_hold_back`, `can_skip_until`
       EXT-X-SERVER-CONTROL parameters. ``hold_back`` defaults to
       3 * ``target_duration`` and ``part_hold_back`` to 3 * ``part_target``,
       per the LL-HLS recommendations

     `version`
       EXT-X-VERSION, defaults to 9 (the minimum for LL-HLS)
    """

    def __init__(
        self,
        target_duration,
        part_target,
        window_size=3,
        media_sequence=0,
        discontinuity_sequence=0,
        can_block_reload=True,
        hold_back=None,
        part_hold_back=None,
        can_skip_until=None,
        version=LL_HLS_VERSION,
    ):
        self.target_duration = _validate_duration(target_duration, None, "target")
        self.part_target = _validate_duration(
            part_target, target_duration, "part target"
        )
        if window_size < MIN_WINDOW_SEGMENTS:
            raise WindowTooSmallError(
                f"window_size must hold at least {MIN_WINDOW_SEGMENTS} "
                f"complete segments, got {window_size!r}"
            )
        self.window_size = window_size
        self.media_sequence = media_sequence
        self.discontinuity_sequence = discontinuity_sequence
        self.can_block_reload = "YES" if can_block_reload else None
        self.hold_back = hold_back if hold_back is not None else 3 * target_duration
        self.part_hold_back = (
            part_hold_back if part_hold_back is not None else 3 * part_target
        )
        self.can_skip_until = can_skip_until
        self.version = version

        self._segments = []
        self._current_parts = []
        self._current_discontinuity = False
        self._pending_part_uri = None
        self._published_uris = set()
        self._ended = False

    @property
    def is_ended(self):
        """True once ``end()`` has been called."""
        return self._ended

    @property
    def segments(self):
        """Complete segments currently inside the window."""
        return list(self._segments)

    @property
    def current_parts(self):
        """Parts already published for the still-open segment."""
        return list(self._current_parts)

    @property
    def preload_hint_uri(self):
        """URI the preload hint currently points at, or None."""
        return self._pending_part_uri

    def _ensure_live(self):
        if self._ended:
            raise PublishOrderError(
                "playlist was already ended with EXT-X-ENDLIST; "
                "no further events may be published"
            )

    def _check_new_uri(self, uri):
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"uri must be a non-empty string, got {uri!r}")
        if uri == self._pending_part_uri or uri in self._published_uris:
            raise DuplicateURIError(
                f"uri {uri!r} was already published; published URIs must not "
                "repeat or go backwards"
            )

    def start_part(self, uri):
        """Open a new partial segment for the current segment.

        From this point on the playlist's EXT-X-PRELOAD-HINT points at
        ``uri`` until ``complete_part()`` publishes it.
        """
        self._ensure_live()
        self._check_new_uri(uri)
        if self._pending_part_uri is not None:
            raise IncompletePartError(
                f"part {self._pending_part_uri!r} was started but not completed"
            )
        self._pending_part_uri = uri
        return self

    def complete_part(
        self, duration, independent=None, byterange=None, gap=None, next_uri=None
    ):
        """Publish the part opened by ``start_part()`` as an EXT-X-PART.

        If ``next_uri`` is given, the next part is opened immediately so the
        preload hint keeps pointing at the resource being written.
        """
        self._ensure_live()
        if self._pending_part_uri is None:
            raise PublishOrderError(
                "no part in progress; call start_part() before complete_part()"
            )
        duration = _validate_duration(duration, self.part_target, "part")
        if independent is True:
            independent = "YES"
        part = PartialSegment(
            base_uri=None,
            uri=self._pending_part_uri,
            duration=duration,
            independent=independent,
            byterange=byterange,
            gap=gap,
        )
        self._published_uris.add(self._pending_part_uri)
        self._pending_part_uri = None
        self._current_parts.append(part)
        if next_uri is not None:
            self.start_part(next_uri)
        return self

    def complete_segment(self, uri, duration, next_uri=None):
        """Close the current segment and publish it as EXTINF + URI.

        All parts published since the previous ``complete_segment()`` call
        become this segment's EXT-X-PART tags. The window is then trimmed:
        segments that fall off are removed, the media sequence advances, and
        the discontinuity sequence advances for each trimmed segment that
        carried a discontinuity. If ``next_uri`` is given, the first part of
        the following segment is opened immediately.
        """
        self._ensure_live()
        if self._pending_part_uri is not None:
            raise IncompletePartError(
                f"part {self._pending_part_uri!r} was started but not completed; "
                "finish it before completing the segment"
            )
        if not self._current_parts:
            raise PublishOrderError(
                "cannot complete a segment before publishing at least one part"
            )
        duration = _validate_duration(duration, self.target_duration, "segment")
        parts_duration = sum(part.duration for part in self._current_parts)
        if duration < parts_duration - _DURATION_EPSILON:
            raise InvalidDurationError(
                f"segment duration {duration} is shorter than its published "
                f"parts ({parts_duration})"
            )
        self._check_new_uri(uri)
        segment = Segment(
            uri=uri, duration=duration, discontinuity=self._current_discontinuity
        )
        segment.parts = PartialSegmentList(self._current_parts)
        self._segments.append(segment)
        self._published_uris.add(uri)
        self._current_parts = []
        self._current_discontinuity = False
        self._trim()
        if next_uri is not None:
            self.start_part(next_uri)
        return self

    def add_discontinuity(self):
        """Mark the next segment with EXT-X-DISCONTINUITY.

        Must be called between segments: once a part of the next segment has
        been opened or published, the discontinuity can no longer be placed
        before it.
        """
        self._ensure_live()
        if self._pending_part_uri is not None or self._current_parts:
            raise PublishOrderError(
                "discontinuity must be declared between segments, before any "
                "part of the next segment is published"
            )
        self._current_discontinuity = True
        return self

    def end(self):
        """Append EXT-X-ENDLIST; no further events may be published."""
        self._ensure_live()
        if self._pending_part_uri is not None:
            raise IncompletePartError(
                f"part {self._pending_part_uri!r} was started but not completed; "
                "finish it before ending the playlist"
            )
        if self._current_parts:
            raise PublishOrderError(
                "cannot end the playlist with an incomplete segment; call "
                "complete_segment() first"
            )
        self._ended = True
        return self

    def _trim(self):
        while len(self._segments) > self.window_size:
            dropped = self._segments.pop(0)
            self.media_sequence += 1
            if dropped.discontinuity:
                self.discontinuity_sequence += 1

    def playlist(self):
        """Build an ``M3U8`` object reflecting the current window state."""
        playlist = M3U8()
        playlist.media_sequence = self.media_sequence
        playlist.discontinuity_sequence = self.discontinuity_sequence
        playlist.target_duration = self.target_duration
        playlist.version = self.version
        playlist.server_control = ServerControl(
            can_block_reload=self.can_block_reload,
            hold_back=self.hold_back,
            part_hold_back=self.part_hold_back,
            can_skip_until=self.can_skip_until,
        )
        playlist.part_inf = PartInformation(part_target=self.part_target)

        segments = list(self._segments)
        if self._current_parts or self._current_discontinuity:
            open_segment = Segment(
                uri=None, duration=None, discontinuity=self._current_discontinuity
            )
            open_segment.parts = PartialSegmentList(list(self._current_parts))
            segments.append(open_segment)
        playlist.segments = SegmentList(segments)

        if self._pending_part_uri is not None:
            playlist.preload_hint = PreloadHint(
                type="PART", base_uri=None, uri=self._pending_part_uri
            )
        playlist.is_endlist = self._ended
        return playlist

    def dumps(self, **kwargs):
        """Serialize the current playlist state to a string."""
        return self.playlist().dumps(**kwargs)
