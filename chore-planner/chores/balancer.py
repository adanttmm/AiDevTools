"""Fair-share chore balancer.

Pipeline for a single target week (Mon-Sun):

1. ``find_due`` - project each chore forward from its last occurrence and emit
   the instances whose feasible window overlaps the week, each with a date
   range ``[earliest, latest]``.
2. ``assign_people`` - longest-processing-time-first greedy: hand each due
   instance to the person with the lowest running load, where load is seeded
   from the fairness ledger so past imbalance is corrected.
3. ``spread_dates`` - place every instance on a concrete date inside its range,
   avoiding day-level clustering and respecting each chore's own spacing.
4. ``generate_week`` - the Django-aware orchestrator: reads the models, runs
   the three pure steps, writes the result in one transaction, and refreshes
   the fairness ledger (recomputed from history, so it is idempotent).

The three pure steps take primitives only and never touch the ORM, so they
can be unit-tested without a database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

# Safety valve: never emit more than this many instances of one chore in a week.
MAX_PER_WEEK = 7
# How many past weeks the fairness ledger replays. Bounds runtime and stops
# "ancient debt" from dominating once the tool has run for a while.
LEDGER_LOOKBACK_WEEKS = 8

INF = float("inf")


@dataclass(frozen=True)
class DueInstance:
    chore_id: int
    effort: int
    interval_min_days: int
    earliest: date
    latest: date


@dataclass(frozen=True)
class PlannedInstance:
    chore_id: int
    person_id: int
    date: date


# --------------------------------------------------------------------------- #
# Step 0: week arithmetic
# --------------------------------------------------------------------------- #
def week_bounds(any_day: date) -> tuple[date, date]:
    """Return the Monday and Sunday of the ISO week containing ``any_day``."""
    monday = any_day - timedelta(days=any_day.weekday())
    return monday, monday + timedelta(days=6)


# --------------------------------------------------------------------------- #
# Step 1: due detection
# --------------------------------------------------------------------------- #
def find_due(
    *,
    chore_id: int,
    effort: int,
    interval_min_days: int,
    interval_max_days: int,
    last_done: date,
    pinned_dates: list[date],
    week_start: date,
    week_end: date,
) -> list[DueInstance]:
    """Project one chore forward and return the instances it needs this week.

    ``last_done`` is the date of the most recent occurrence before the week
    (its ``anchor_date`` or the newest prior scheduled instance). ``pinned_dates``
    are pinned occurrences already sitting inside the week; each one consumes an
    occurrence slot instead of producing a new :class:`DueInstance`.

    Scheduling is "lazy": after each occurrence the cursor advances to that
    occurrence's clamped deadline, so a chore recurs at the slow end of its
    interval and an overdue chore catches up one step at a time rather than
    piling onto the first day.
    """
    queue = sorted(d for d in pinned_dates if week_start <= d <= week_end)
    cursor = last_done
    due: list[DueInstance] = []
    slots = 0

    while slots < MAX_PER_WEEK:
        earliest = cursor + timedelta(days=interval_min_days)
        if earliest > week_end:
            break
        deadline = cursor + timedelta(days=interval_max_days)

        if queue and queue[0] <= max(deadline, week_end):
            # A pinned instance already covers this occurrence.
            cursor = queue.pop(0)
            slots += 1
            continue

        lo = max(week_start, earliest)
        hi = min(week_end, deadline)
        if hi < lo:
            # Overdue: the deadline fell before the week started. Catch up now.
            lo = hi = week_start

        due.append(
            DueInstance(
                chore_id=chore_id,
                effort=effort,
                interval_min_days=interval_min_days,
                earliest=lo,
                latest=hi,
            )
        )
        slots += 1
        cursor = hi

    return due


# --------------------------------------------------------------------------- #
# Step 2: people assignment (LPT, ledger-seeded, capacity-aware)
# --------------------------------------------------------------------------- #
def assign_people(
    dues: list[DueInstance],
    person_ids: list[int],
    load0: dict[int, float],
    assigned0: dict[int, int],
    capacity: dict[int, int],
) -> dict[int, int]:
    """Return ``{index into dues -> person_id}``.

    ``load0`` is the fairness-adjusted starting load per person (``-ledger`` plus
    any pinned minutes). ``assigned0`` is the actual starting minutes per person
    (pinned minutes), checked against ``capacity`` (``person_id -> minutes``;
    missing = unlimited).
    """
    load = dict(load0)
    assigned = dict(assigned0)
    order = sorted(range(len(dues)), key=lambda i: (-dues[i].effort, i))
    result: dict[int, int] = {}

    for i in order:
        eff = dues[i].effort
        eligible = [
            p for p in person_ids if assigned[p] + eff <= capacity.get(p, INF)
        ]
        pool = eligible or person_ids
        chosen = min(pool, key=lambda p: (load[p], assigned[p], p))
        result[i] = chosen
        load[chosen] += eff
        assigned[chosen] += eff

    return result


# --------------------------------------------------------------------------- #
# Step 3: date spreading
# --------------------------------------------------------------------------- #
def spread_dates(
    dues: list[DueInstance],
    assignment: dict[int, int],
    day_load0: dict[tuple[int, date], int],
) -> list[PlannedInstance]:
    """Place each due instance on a concrete date within ``[earliest, latest]``.

    Picks, per instance, the date that currently carries the least load for that
    person (tie-break: earliest date), after filtering to dates that keep the
    chore's own ``interval_min_days`` spacing when that is still possible.
    """
    day_load: dict[tuple[int, date], float] = defaultdict(float)
    day_load.update(day_load0)
    chore_last: dict[int, date] = {}
    planned: list[PlannedInstance] = []

    order = sorted(
        range(len(dues)),
        key=lambda i: (dues[i].earliest, dues[i].latest, -dues[i].effort, i),
    )
    for i in order:
        d = dues[i]
        person = assignment[i]
        span = (d.latest - d.earliest).days
        candidates = [d.earliest + timedelta(days=k) for k in range(span + 1)]

        prev = chore_last.get(d.chore_id)
        if prev is not None:
            spaced = [
                c for c in candidates if (c - prev).days >= d.interval_min_days
            ]
            if spaced:
                candidates = spaced

        best = min(candidates, key=lambda c: (day_load[(person, c)], c))
        planned.append(
            PlannedInstance(chore_id=d.chore_id, person_id=person, date=best)
        )
        day_load[(person, best)] += d.effort
        chore_last[d.chore_id] = best

    return planned


# --------------------------------------------------------------------------- #
# Step 4: Django-aware orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class PersonResult:
    person_id: int
    name: str
    instances: int
    minutes: int
    ledger_balance: int


@dataclass
class GenerateResult:
    week_start: date
    week_end: date
    total_minutes: int
    people: list[PersonResult]
    created: int


def _weights(people) -> dict[int, int]:
    """Weight people by capacity when everyone has one, else weight equally."""
    if people and all(p.weekly_capacity_minutes for p in people):
        return {p.id: p.weekly_capacity_minutes for p in people}
    return {p.id: 1 for p in people}


def _replay_ledger(person_ids, weights, first_week_start, up_to_exclusive):
    """Recompute each person's fairness balance from scheduled history.

    balance += (fair share of that week's minutes) - (minutes actually assigned).
    Positive balance = under-served = should receive more work next.
    """
    from .models import ScheduledInstance

    balance = {p: 0.0 for p in person_ids}
    wsum = sum(weights.values()) or 1
    week = max(first_week_start, up_to_exclusive - timedelta(weeks=LEDGER_LOOKBACK_WEEKS))
    while week < up_to_exclusive:
        rows = ScheduledInstance.objects.filter(
            date__range=(week, week + timedelta(days=6))
        ).select_related("chore")
        assigned = {p: 0 for p in person_ids}
        for r in rows:
            if r.person_id in assigned:
                assigned[r.person_id] += r.chore.effort_minutes
        total = sum(assigned.values())
        for p in person_ids:
            balance[p] += total * weights[p] / wsum - assigned[p]
        week += timedelta(weeks=1)
    return balance


def generate_week(target: date | None = None) -> GenerateResult:
    """Balance and persist the schedule for the week containing ``target``.

    Non-pinned instances in the target week are replaced; pinned ones are kept
    and counted toward load, capacity and the ledger. Safe to run repeatedly:
    the ledger is recomputed from history each time, never incremented in place.
    """
    from django.db import transaction
    from django.utils import timezone

    from .models import Chore, FairnessLedger, Person, ScheduledInstance

    week_start, week_end = week_bounds(target or timezone.localdate())

    people = list(Person.objects.all())
    if not people:
        raise ValueError("No people defined - add at least one Person first.")
    person_ids = [p.id for p in people]
    names = {p.id: p.name for p in people}
    capacity = {p.id: p.weekly_capacity_minutes for p in people if p.weekly_capacity_minutes}
    weights = _weights(people)

    # Pinned instances already sitting in the target week.
    pinned = list(
        ScheduledInstance.objects.filter(
            date__range=(week_start, week_end), pinned=True
        ).select_related("chore")
    )
    pinned_minutes: dict[int, int] = defaultdict(int)
    pinned_day_load: dict[tuple[int, date], int] = defaultdict(int)
    pinned_dates: dict[int, list[date]] = defaultdict(list)
    for inst in pinned:
        pinned_minutes[inst.person_id] += inst.chore.effort_minutes
        pinned_day_load[(inst.person_id, inst.date)] += inst.chore.effort_minutes
        pinned_dates[inst.chore_id].append(inst.date)

    # Due detection per active chore.
    chores = list(Chore.objects.filter(active=True))
    effort_by_chore = {c.id: c.effort_minutes for c in chores}
    dues: list[DueInstance] = []
    for chore in chores:
        prior = (
            ScheduledInstance.objects.filter(chore=chore, date__lt=week_start)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        last_done = max(chore.anchor_date, prior) if prior else chore.anchor_date
        dues.extend(
            find_due(
                chore_id=chore.id,
                effort=chore.effort_minutes,
                interval_min_days=chore.interval_min_days,
                interval_max_days=chore.interval_max_days,
                last_done=last_done,
                pinned_dates=pinned_dates.get(chore.id, []),
                week_start=week_start,
                week_end=week_end,
            )
        )

    # Assignment + date spreading.
    first_instance = (
        ScheduledInstance.objects.order_by("date").values_list("date", flat=True).first()
    )
    start_balance = (
        _replay_ledger(
            person_ids, weights, week_bounds(first_instance)[0], week_start
        )
        if first_instance
        else {p: 0.0 for p in person_ids}
    )
    load0 = {p: -start_balance[p] + pinned_minutes[p] for p in person_ids}
    assigned0 = {p: pinned_minutes[p] for p in person_ids}
    assignment = assign_people(dues, person_ids, load0, assigned0, capacity)
    planned = spread_dates(dues, assignment, pinned_day_load)

    # Persist.
    with transaction.atomic():
        ScheduledInstance.objects.filter(
            date__range=(week_start, week_end), pinned=False
        ).delete()
        ScheduledInstance.objects.bulk_create(
            ScheduledInstance(
                chore_id=pl.chore_id, person_id=pl.person_id, date=pl.date
            )
            for pl in planned
        )

        assigned_total: dict[int, int] = defaultdict(int)
        for pl in planned:
            assigned_total[pl.person_id] += effort_by_chore[pl.chore_id]
        for pid in person_ids:
            assigned_total[pid] += pinned_minutes[pid]
        week_total = sum(assigned_total.values())

        wsum = sum(weights.values()) or 1
        results = []
        counts: dict[int, int] = defaultdict(int)
        for pl in planned:
            counts[pl.person_id] += 1
        for inst in pinned:
            counts[inst.person_id] += 1
        for p in people:
            fair = week_total * weights[p.id] / wsum
            new_balance = round(start_balance[p.id] + fair - assigned_total[p.id])
            ledger, _ = FairnessLedger.objects.get_or_create(person_id=p.id)
            ledger.balance_minutes = new_balance
            ledger.save(update_fields=["balance_minutes"])
            results.append(
                PersonResult(
                    person_id=p.id,
                    name=names[p.id],
                    instances=counts[p.id],
                    minutes=assigned_total[p.id],
                    ledger_balance=new_balance,
                )
            )

    results.sort(key=lambda r: r.name)
    return GenerateResult(
        week_start=week_start,
        week_end=week_end,
        total_minutes=week_total,
        people=results,
        created=len(planned),
    )
