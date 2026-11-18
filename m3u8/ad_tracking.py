# Copyright 2024 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.

"""
Ad break consolidation for live playlists.

Live ad signaling commonly arrives as a pair of ``EXT-X-DATERANGE`` tags
sharing the same ``ID``: an opening tag carrying ``SCTE35-OUT`` (or a
``START-DATE``) and, several playlist refreshes later, a closing tag carrying
``SCTE35-IN`` (or an ``END-DATE``/``DURATION``).  This module joins those
fragments into time-ordered :class:`AdBreak` intervals and can incrementally
merge successive versions of the same live playlist without producing
duplicates.
"""

from datetime import datetime, timedelta, timezone

from m3u8.parser import cast_date_time

# DATERANGE attributes that identify an ad (splice) marker
_AD_MARKERS = frozenset(("scte35_out", "scte35_in", "scte35_cmd"))


def _parse_date(value):
    """Parse an ISO-8601 DATERANGE date, returning ``None`` when absent."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return cast_date_time(value)


class AdBreak:
    """
    A consolidated advertising interval built from one or more
    ``EXT-X-DATERANGE`` tags sharing the same source ``ID``.

    Attributes:

    `id`
      the source ``EXT-X-DATERANGE`` ID

    `class_`
      the DATERANGE ``CLASS`` when present

    `start_date`
      ``datetime`` parsed from the opening tag's ``START-DATE``

    `end_date`
      ``datetime`` parsed from the closing tag's ``END-DATE`` (or computed
      from ``START-DATE`` + ``DURATION``); ``None`` while the break is open

    `duration`
      closing ``DURATION`` in seconds when present

    `planned_duration`
      ``PLANNED-DURATION`` from the opening tag in seconds when present

    `end_on_next`
      ``"YES"`` when the break carries ``END-ON-NEXT``

    `start_marker`
      the raw :class:`~m3u8.model.DateRange` tag that opened the break
      (carrying ``SCTE35-OUT`` or ``START-DATE``)

    `end_marker`
      the raw :class:`~m3u8.model.DateRange` tag that closed the break
      (carrying ``SCTE35-IN`` / ``END-DATE`` / ``DURATION``); ``None`` while
      the break is open

    `tags`
      every source :class:`~m3u8.model.DateRange` tag contributing to this
      break, in arrival order

    `conflicts`
      list of human readable strings describing contradictory input that was
      detected (duplicate conflicting ID, end before start, overlapping
      breaks...).  A break with conflicts is still emitted.
    """

    def __init__(
        self,
        id,
        start_date=None,
        end_date=None,
        duration=None,
        planned_duration=None,
        class_=None,
        end_on_next=None,
        start_marker=None,
        end_marker=None,
        tags=None,
        conflicts=None,
    ):
        self.id = id
        self.class_ = class_
        self.start_date = start_date
        self.end_date = end_date
        self.duration = duration
        self.planned_duration = planned_duration
        self.end_on_next = end_on_next
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.tags = list(tags) if tags else []
        self.conflicts = list(conflicts) if conflicts else []

    @property
    def is_open(self):
        """``True`` while no closing marker has been observed."""
        return self.end_date is None and not self.duration

    @property
    def scte35_out(self):
        return self.start_marker.scte35_out if self.start_marker else None

    @property
    def scte35_in(self):
        return self.end_marker.scte35_in if self.end_marker else None

    def __repr__(self):
        state = "open" if self.is_open else "closed"
        return f"<AdBreak {self.id!r} {state} {self.start_date}..{self.end_date}>"

    def __eq__(self, other):
        if not isinstance(other, AdBreak):
            return NotImplemented
        return (
            self.id == other.id
            and self.start_date == other.start_date
            and self.end_date == other.end_date
            and self.duration == other.duration
        )

    def __hash__(self):
        return hash((self.id, self.start_date, self.end_date, self.duration))


class AdBreakList(list):
    """A time-ordered collection of :class:`AdBreak` objects."""

    @property
    def open(self):
        return [ad for ad in self if ad.is_open]

    @property
    def closed(self):
        return [ad for ad in self if not ad.is_open]

    def by_id(self, id):
        for ad in self:
            if ad.id == id:
                return ad
        return None


class AdBreakTracker:
    """
    Incrementally consolidates ad breaks across successive versions of the
    same live stream.

    Feed every downloaded playlist (an :class:`~m3u8.M3U8` object) to
    :meth:`merge`; the tracker deduplicates repeated tags, completes open
    breaks when a late closing marker arrives, tolerates playlist rollback,
    and keeps the result sorted by start time.

    The same instance must only be used for one stream (one rendition URL).
    """

    def __init__(self):
        self._breaks = {}  # id -> AdBreak
        # identity of every tag already consumed, so re-sent tags in sliding
        # windows are not processed twice
        self._seen_tags = set()
        self._order = []  # ids in first-seen order as a stable tiebreaker
        self.conflicts = []

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def merge(self, playlist):
        """
        Merge all ad-related ``EXT-X-DATERANGE`` tags from *playlist* (an
        :class:`~m3u8.M3U8` instance) into the tracked state.  Returns the
        sorted :class:`AdBreakList` for convenience.
        """
        for tag in self._iter_dateranges(playlist):
            if not self._is_ad_marker(tag):
                continue
            identity = self._tag_identity(tag)
            if identity in self._seen_tags:
                continue
            self._seen_tags.add(identity)
            self._ingest(tag)

        self._resolve_end_on_next()
        self._detect_overlaps()
        return self.breaks

    @property
    def breaks(self):
        """Sorted :class:`AdBreakList` of all known ad breaks."""
        ordered = sorted(
            self._breaks.values(),
            key=lambda ad: (
                ad.start_date is None,
                ad.start_date or _EPOCH,
                self._order.index(ad.id) if ad.id in self._order else 0,
            ),
        )
        return AdBreakList(ordered)

    # ------------------------------------------------------------------ #
    # tag discovery / classification
    # ------------------------------------------------------------------ #
    @staticmethod
    def _iter_dateranges(playlist):
        for segment in playlist.segments:
            yield from segment.dateranges
            for part in getattr(segment, "parts", []) or []:
                yield from part.dateranges

    @staticmethod
    def _is_ad_marker(tag):
        if any(getattr(tag, attr, None) for attr in _AD_MARKERS):
            return True
        # plain DATERANGE with explicit start (and optional end) timing
        return bool(getattr(tag, "start_date", None))

    @staticmethod
    def _tag_identity(tag):
        return (
            tag.id,
            tag.start_date,
            tag.end_date,
            tag.duration,
            tag.scte35_out,
            tag.scte35_in,
            tag.scte35_cmd,
        )

    # ------------------------------------------------------------------ #
    # core state machine
    # ------------------------------------------------------------------ #
    def _ingest(self, tag):
        start = _parse_date(tag.start_date)
        end = _parse_date(tag.end_date)
        role = self._tag_role(tag)

        existing = self._breaks.get(tag.id)

        if existing is None:
            if role == "end":
                # closing marker with no opening marker: clock anchor for the
                # start is missing, keep an open-ended break anchored on end
                ad = AdBreak(
                    id=tag.id,
                    class_=tag.class_,
                    end_date=end,
                    duration=tag.duration,
                    end_marker=tag,
                    tags=[tag],
                )
                if start is None:
                    self._conflict(
                        ad,
                        f"closing DATERANGE {tag.id!r} has no START-DATE and "
                        "no earlier opening tag",
                    )
                else:
                    ad.start_date = start
                self._store(tag.id, ad)
                return

            ad = AdBreak(
                id=tag.id,
                class_=tag.class_,
                start_date=start,
                planned_duration=tag.planned_duration,
                end_on_next=tag.end_on_next,
                start_marker=tag,
                tags=[tag],
            )
            if end is not None:
                ad.end_date = end
                ad.end_marker = tag
            elif tag.duration is not None and start is not None:
                ad.end_date = start + timedelta(seconds=tag.duration)
                ad.duration = tag.duration
                ad.end_marker = tag
            self._validate_ordering(ad, tag)
            self._store(tag.id, ad)
            return

        # ID already known: merge the fragment, guarding against conflicts
        self._merge_fragment(existing, tag, role, start, end)

    def _merge_fragment(self, ad, tag, role, start, end):
        ad.tags.append(tag)

        if tag.class_ and not ad.class_:
            ad.class_ = tag.class_
        if tag.planned_duration is not None and ad.planned_duration is None:
            ad.planned_duration = tag.planned_duration
        if tag.end_on_next and not ad.end_on_next:
            ad.end_on_next = tag.end_on_next

        if role == "start":
            # duplicate opening tag with different timing is a conflicting ID
            if (
                start is not None
                and ad.start_date is not None
                and start != ad.start_date
            ):
                self._conflict(
                    ad,
                    f"duplicate DATERANGE ID {tag.id!r} with conflicting "
                    f"START-DATE ({start} != {ad.start_date})",
                )
            elif ad.start_date is None and start is not None:
                ad.start_date = start
                ad.start_marker = ad.start_marker or tag
            if ad.scte35_out is None and tag.scte35_out:
                ad.start_marker = tag
            return

        # role == "end": complete the open break
        if tag.duration is not None:
            ad.duration = tag.duration
        if end is None and tag.duration is not None and ad.start_date is not None:
            end = ad.start_date + timedelta(seconds=tag.duration)

        if ad.start_date is not None and end is not None:
            self._validate_ordering(ad, tag, end=end)

        if ad.end_date is not None and end is not None and end != ad.end_date:
            self._conflict(
                ad,
                f"duplicate DATERANGE ID {tag.id!r} with conflicting end "
                f"({end} != {ad.end_date})",
            )
        if end is not None and ad.end_date is None:
            ad.end_date = end
        if ad.end_marker is None:
            ad.end_marker = tag

    def _store(self, id_, ad):
        if id_ not in self._breaks:
            self._order.append(id_)
        self._breaks[id_] = ad

    def _conflict(self, ad, message):
        ad.conflicts.append(message)
        if message not in self.conflicts:
            self.conflicts.append(message)

    def _validate_ordering(self, ad, tag, end=None):
        end = ad.end_date if end is None else end
        if ad.start_date is not None and end is not None and end < ad.start_date:
            self._conflict(
                ad,
                f"DATERANGE {tag.id!r} END-DATE {end} precedes START-DATE "
                f"{ad.start_date}; end marker retained",
            )

    @staticmethod
    def _tag_role(tag):
        # an explicit SCTE35-IN / END-DATE tag closes a break;
        # SCTE35-OUT / SCTE35-CMD / START-DATE opens it. A tag carrying both
        # a START-DATE and timing (DURATION/END-DATE) is self-contained and
        # treated as an opening (start) marker.
        if tag.scte35_in:
            return "end"
        if tag.scte35_out or tag.scte35_cmd:
            return "start"
        if tag.start_date:
            return "start"
        if tag.end_date or tag.duration:
            return "end"
        return "start"

    # ------------------------------------------------------------------ #
    # END-ON-NEXT / overlap resolution
    # ------------------------------------------------------------------ #
    def _resolve_end_on_next(self):
        """An END-ON-NEXT break ends when the next break of the same class
        with a START-DATE begins."""
        for ad in self._breaks.values():
            if not ad.end_on_next or ad.end_date is not None:
                continue
            candidates = [
                other
                for other in self._breaks.values()
                if other is not ad
                and other.class_ == ad.class_
                and other.start_date is not None
                and (ad.start_date is None or other.start_date >= ad.start_date)
            ]
            if candidates:
                nxt = min(candidates, key=lambda o: o.start_date)
                # the end is derived from the next break per END-ON-NEXT;
                # end_marker stays None since no closing tag was observed
                ad.end_date = nxt.start_date

    def _detect_overlaps(self):
        timed = [
            ad
            for ad in self._breaks.values()
            if ad.start_date is not None
            and ad.end_date is not None
            and not ad.end_on_next
        ]
        timed.sort(key=lambda ad: ad.start_date)
        for earlier, later in zip(timed, timed[1:], strict=False):
            if later.start_date < earlier.end_date:
                msg = (
                    f"overlapping ad breaks {earlier.id!r} and {later.id!r} "
                    f"({later.start_date} < {earlier.end_date})"
                )
                for ad in (earlier, later):
                    if msg not in ad.conflicts:
                        ad.conflicts.append(msg)
                if msg not in self.conflicts:
                    self.conflicts.append(msg)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
