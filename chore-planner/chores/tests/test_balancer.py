from datetime import date

from django.test import TestCase

from chores.balancer import (
    DueInstance,
    assign_people,
    find_due,
    generate_week,
    spread_dates,
    week_bounds,
)
from chores.models import Chore, FairnessLedger, Person, ScheduledInstance

MON = date(2026, 3, 2)  # a Monday
SUN = date(2026, 3, 8)


class WeekBoundsTests(TestCase):
    def test_bounds_from_any_weekday(self):
        self.assertEqual(week_bounds(date(2026, 3, 5)), (MON, SUN))
        self.assertEqual(week_bounds(MON), (MON, SUN))
        self.assertEqual(week_bounds(SUN), (MON, SUN))


class FindDueTests(TestCase):
    def _find(self, imin, imax, last_done, pinned=None):
        return find_due(
            chore_id=1,
            effort=20,
            interval_min_days=imin,
            interval_max_days=imax,
            last_done=last_done,
            pinned_dates=pinned or [],
            week_start=MON,
            week_end=SUN,
        )

    def test_not_due_when_next_window_is_after_week(self):
        # Done Sunday before the week, cannot recur for 4 days -> earliest is Thu
        # of *next* week only if last_done is late enough; here it is not due.
        self.assertEqual(self._find(10, 14, date(2026, 2, 27)), [])

    def test_single_instance_within_week(self):
        due = self._find(4, 7, date(2026, 2, 26))  # earliest Mar 2, latest Mar 5
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].earliest, MON)
        self.assertEqual(due[0].latest, date(2026, 3, 5))

    def test_short_interval_yields_multiple(self):
        due = self._find(1, 2, date(2026, 3, 1))  # daily-ish
        self.assertGreaterEqual(len(due), 3)

    def test_respects_max_per_week(self):
        due = self._find(1, 1, date(2026, 2, 20))
        self.assertLessEqual(len(due), 7)

    def test_overdue_catches_up_at_week_start(self):
        due = self._find(2, 3, date(2026, 1, 1))
        self.assertEqual(due[0].earliest, MON)
        self.assertEqual(due[0].latest, MON)

    def test_pinned_consumes_an_occurrence(self):
        without = self._find(2, 3, date(2026, 2, 28))
        withpin = self._find(2, 3, date(2026, 2, 28), pinned=[date(2026, 3, 3)])
        self.assertEqual(len(withpin), len(without) - 1)


class AssignPeopleTests(TestCase):
    def test_lpt_balances_minutes(self):
        dues = [DueInstance(1, m, 1, MON, SUN) for m in (60, 40, 30, 20, 10)]
        out = assign_people(dues, [1, 2], {1: 0, 2: 0}, {1: 0, 2: 0}, {})
        load = {1: 0, 2: 0}
        for i, pid in out.items():
            load[pid] += dues[i].effort
        self.assertLessEqual(abs(load[1] - load[2]), 20)

    def test_ledger_debt_pulls_work(self):
        dues = [DueInstance(1, 30, 1, MON, SUN) for _ in range(4)]
        # person 2 is owed 100 minutes -> load0 seeded negative -> gets more
        out = assign_people(dues, [1, 2], {1: 0, 2: -100}, {1: 0, 2: 0}, {})
        got2 = sum(dues[i].effort for i, p in out.items() if p == 2)
        got1 = sum(dues[i].effort for i, p in out.items() if p == 1)
        self.assertGreater(got2, got1)

    def test_capacity_is_respected_when_possible(self):
        dues = [DueInstance(1, 30, 1, MON, SUN) for _ in range(4)]
        out = assign_people(dues, [1, 2], {1: 0, 2: 0}, {1: 0, 2: 0}, {1: 30})
        got1 = sum(dues[i].effort for i, p in out.items() if p == 1)
        self.assertLessEqual(got1, 30)


class SpreadDatesTests(TestCase):
    def test_dates_land_in_range_and_spread(self):
        dues = [DueInstance(c, 20, 1, MON, SUN) for c in range(5)]
        assignment = {i: 1 for i in range(5)}
        planned = spread_dates(dues, assignment, {})
        for p in planned:
            self.assertTrue(MON <= p.date <= SUN)
        self.assertGreaterEqual(len({p.date for p in planned}), 4)

    def test_same_chore_instances_are_spaced(self):
        dues = [
            DueInstance(7, 20, 3, MON, SUN),
            DueInstance(7, 20, 3, MON, SUN),
        ]
        planned = spread_dates(dues, {0: 1, 1: 1}, {})
        gap = abs((planned[0].date - planned[1].date).days)
        self.assertGreaterEqual(gap, 3)


class GenerateWeekTests(TestCase):
    def setUp(self):
        self.people = [
            Person.objects.create(name=n) for n in ("Alex", "Sam", "Robin")
        ]
        for p in self.people:
            FairnessLedger.objects.create(person=p)
        Chore.objects.create(
            name="Dishes",
            effort_minutes=20,
            interval_min_days=1,
            interval_max_days=2,
            anchor_date=date(2026, 3, 1),
        )
        Chore.objects.create(
            name="Vacuum",
            effort_minutes=30,
            interval_min_days=4,
            interval_max_days=7,
            anchor_date=date(2026, 2, 26),
        )
        Chore.objects.create(
            name="Inactive",
            effort_minutes=99,
            interval_min_days=1,
            interval_max_days=2,
            anchor_date=date(2026, 3, 1),
            active=False,
        )

    def test_generates_and_persists(self):
        result = generate_week(date(2026, 3, 4))
        self.assertEqual((result.week_start, result.week_end), (MON, SUN))
        self.assertGreaterEqual(result.created, 4)
        self.assertEqual(
            ScheduledInstance.objects.filter(date__range=(MON, SUN)).count(),
            result.created,
        )
        # inactive chore never scheduled
        self.assertFalse(
            ScheduledInstance.objects.filter(chore__name="Inactive").exists()
        )

    def test_idempotent(self):
        first = generate_week(date(2026, 3, 4))
        second = generate_week(date(2026, 3, 4))
        self.assertEqual(first.created, second.created)
        self.assertEqual(
            ScheduledInstance.objects.filter(date__range=(MON, SUN)).count(),
            second.created,
        )

    def test_pinned_instances_survive_regeneration(self):
        generate_week(date(2026, 3, 4))
        inst = ScheduledInstance.objects.filter(date__range=(MON, SUN)).first()
        inst.pinned = True
        inst.save()
        pinned_uid = inst.uid
        generate_week(date(2026, 3, 4))
        self.assertTrue(ScheduledInstance.objects.filter(uid=pinned_uid).exists())

    def test_no_people_raises(self):
        Person.objects.all().delete()
        with self.assertRaises(ValueError):
            generate_week(date(2026, 3, 4))
