from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from chores.models import Chore, FairnessLedger, Person

PEOPLE = ["Alex", "Sam", "Robin"]

# (name, effort_minutes, interval_min_days, interval_max_days)
CHORES = [
    ("Wash the dishes", 20, 1, 2),
    ("Take out the trash", 5, 3, 4),
    ("Vacuum living areas", 25, 4, 7),
    ("Clean the bathroom", 30, 6, 8),
    ("Mop the kitchen floor", 20, 7, 10),
    ("Water the plants", 10, 3, 5),
    ("Grocery run", 45, 6, 8),
    ("Wipe kitchen counters", 10, 1, 2),
    ("Change bed linens", 15, 10, 14),
    ("Tidy shared spaces", 15, 2, 3),
]


class Command(BaseCommand):
    help = "Load demo people and chores for development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing people/chores/instances before seeding.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            from chores.models import ScheduledInstance

            ScheduledInstance.objects.all().delete()
            FairnessLedger.objects.all().delete()
            Chore.objects.all().delete()
            Person.objects.all().delete()
            self.stdout.write("Cleared existing data.")

        today = timezone.localdate()

        for name in PEOPLE:
            person, created = Person.objects.get_or_create(name=name)
            FairnessLedger.objects.get_or_create(person=person)
            self.stdout.write(f"{'+ ' if created else '= '}person {name}")

        for i, (name, effort, imin, imax) in enumerate(CHORES):
            # Stagger anchor dates so the first generated week isn't lopsided.
            anchor = today - timedelta(days=(i % max(1, imax)))
            chore, created = Chore.objects.get_or_create(
                name=name,
                defaults=dict(
                    effort_minutes=effort,
                    interval_min_days=imin,
                    interval_max_days=imax,
                    anchor_date=anchor,
                ),
            )
            self.stdout.write(f"{'+ ' if created else '= '}chore {name}")

        self.stdout.write(self.style.SUCCESS("Seed complete."))
