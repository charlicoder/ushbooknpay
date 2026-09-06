"""
app/availability/domain/interval_engine.py
────────────────────────────────────────────
Core interval arithmetic for the Availability Engine.

Algorithm complexity: O(n log n) for merging n intervals.

All operations are pure functions — no I/O, no state.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.availability.domain.value_objects import BlockType, Interval, TimeBlock


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """
    Merge overlapping or adjacent intervals.

    Algorithm: Sort by start time, then sweep through merging overlapping pairs.
    Complexity: O(n log n)

    Args:
        intervals: List of possibly overlapping Interval objects.

    Returns:
        List of non-overlapping, sorted Interval objects.
    """
    if not intervals:
        return []

    sorted_intervals = sorted(intervals, key=lambda iv: iv.start)
    merged: list[Interval] = [sorted_intervals[0]]

    for current in sorted_intervals[1:]:
        last = merged[-1]
        if current.start <= last.end:
            # Overlapping or adjacent — extend
            merged[-1] = Interval(last.start, max(last.end, current.end))
        else:
            merged.append(current)

    return merged


def subtract_intervals(
    working: list[Interval],
    blocked: list[Interval],
) -> list[Interval]:
    """
    Subtract blocked intervals from working intervals.

    Equivalent to: working ∩ complement(blocked)

    Both inputs are assumed to be already merged (non-overlapping, sorted).
    Complexity: O(n + m) where n=len(working), m=len(blocked).

    Args:
        working: Free time intervals.
        blocked: Intervals to remove from free time.

    Returns:
        List of remaining free intervals after subtracting blocked.
    """
    if not blocked:
        return working[:]

    result: list[Interval] = []
    bi = 0

    for w in working:
        # Current position within working interval
        current_start = w.start

        while bi < len(blocked) and blocked[bi].start < w.end:
            b = blocked[bi]

            if b.end <= current_start:
                # Block is entirely before current position — skip
                bi += 1
                continue

            if b.start > current_start:
                # Gap between current position and block start — free interval
                result.append(Interval(current_start, min(b.start, w.end)))

            current_start = max(current_start, b.end)

            if current_start >= w.end:
                break

            if b.end > w.end:
                # Block extends past working interval — don't advance bi
                break
            else:
                bi += 1

        if current_start < w.end:
            result.append(Interval(current_start, w.end))

    return result


def intersect_intervals(
    a: list[Interval],
    b: list[Interval],
) -> list[Interval]:
    """
    Compute the intersection of two sorted, non-overlapping interval lists.

    Complexity: O(n + m)
    """
    result: list[Interval] = []
    i = j = 0

    while i < len(a) and j < len(b):
        start = max(a[i].start, b[j].start)
        end = min(a[i].end, b[j].end)

        if start < end:
            result.append(Interval(start, end))

        if a[i].end < b[j].end:
            i += 1
        else:
            j += 1

    return result


def generate_blocks(
    free_intervals: list[Interval],
    all_intervals: list[Interval],
    blocked_intervals: list[Interval],
    slot_minutes: int = 30,
    *,
    branch_open: datetime,
    branch_close: datetime,
    service_duration_minutes: int = 30,
) -> list[TimeBlock]:
    """
    Generate 30-minute TimeBlock objects for a day.

    Steps:
    1. Divide the branch open→close window into slot_minutes-sized blocks.
    2. For each block, determine its availability and blocking reason.
    3. Mark a block as available only if ALL consecutive blocks required
       for service_duration_minutes are also free.

    Args:
        free_intervals: Already-computed free intervals (working minus blocked).
        all_intervals: All blocking intervals (for blocking_type labelling).
        blocked_intervals: All blocked intervals with type information.
        slot_minutes: Block size (30 minutes).
        branch_open: Branch opening datetime (timezone-aware).
        branch_close: Branch closing datetime (timezone-aware).
        service_duration_minutes: Required service duration for continuity check.

    Returns:
        Ordered list of TimeBlock objects covering the full day.
    """
    blocks: list[TimeBlock] = []
    slots_needed = max(1, service_duration_minutes // slot_minutes)

    slot_start = branch_open
    all_slot_starts: list[datetime] = []

    while slot_start + timedelta(minutes=slot_minutes) <= branch_close:
        all_slot_starts.append(slot_start)
        slot_start += timedelta(minutes=slot_minutes)

    # Pre-compute which slots are free
    free_set: set[int] = set()
    for idx, ss in enumerate(all_slot_starts):
        slot_end = ss + timedelta(minutes=slot_minutes)
        slot_interval = Interval(ss, slot_end)
        if any(
            iv.start <= ss and iv.end >= slot_end
            for iv in free_intervals
        ):
            free_set.add(idx)

    for idx, ss in enumerate(all_slot_starts):
        slot_end = ss + timedelta(minutes=slot_minutes)

        # A slot is bookable only if this and the next (slots_needed - 1) are all free
        is_available = all(
            (idx + k) in free_set
            for k in range(slots_needed)
        )

        if is_available:
            blocks.append(
                TimeBlock(
                    start=ss,
                    end=slot_end,
                    available=True,
                    blocking_type=BlockType.AVAILABLE,
                )
            )
        else:
            # Determine the blocking reason
            blocking_type = _classify_blocking_reason(
                ss, slot_end, blocked_intervals, branch_open, branch_close
            )
            blocks.append(
                TimeBlock(
                    start=ss,
                    end=slot_end,
                    available=False,
                    blocking_type=blocking_type,
                )
            )

    return blocks


def _classify_blocking_reason(
    slot_start: datetime,
    slot_end: datetime,
    blocked: list[Interval],
    branch_open: datetime,
    branch_close: datetime,
) -> BlockType:
    """
    Determine the primary reason a slot is unavailable.

    Priority order:
    1. Outside branch hours
    2. Leave
    3. Home booking (with buffer)
    4. Confirmed booking
    5. Temporary hold
    6. Outside working hours (fallback)
    """
    slot = Interval(slot_start, slot_end)

    # Check if the slot is outside branch hours
    if slot_start < branch_open or slot_end > branch_close:
        return BlockType.OUTSIDE_BRANCH_HOURS

    # Check each tagged interval
    # (Tagged intervals would require a richer data structure; this is a simplified version
    # that returns BOOKING as the default blocking reason.)
    return BlockType.BOOKING
