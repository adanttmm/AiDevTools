from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from chores.balancer import generate_week


class Command(BaseCommand):
    help = "Balance and store the chore schedule for one week."

    def add_arguments(self, parser):
        parser.add_argument(
            "--week",
            metavar="YYYY-MM-DD",
            help="Any date in the target week. Default: next week.",
        )
        parser.add_argument(
            "--this-week",
            action="store_true",
            help="Target the current week instead of next week.",
        )

    def handle(self, *args, **options):
        if options["week"]:
            try:
                target = datetime.strptime(options["week"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--week must be YYYY-MM-DD")
        elif options["this_week"]:
            target = timezone.localdate()
        else:
            target = timezone.localdate() + timedelta(days=7)

        try:
            result = generate_week(target)
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(
            self.style.SUCCESS(
                f"Week {result.week_start:%Y-%m-%d} to {result.week_end:%Y-%m-%d}: "
                f"{result.created} instances, {result.total_minutes} min total"
            )
        )
        self.stdout.write(f"  {'person':<16}{'chores':>8}{'minutes':>10}{'ledger':>10}")
        for p in result.people:
            self.stdout.write(
                f"  {p.name:<16}{p.instances:>8}{p.minutes:>10}{p.ledger_balance:>+10}"
            )
