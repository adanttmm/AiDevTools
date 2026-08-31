import secrets
import uuid

from django.core.exceptions import ValidationError
from django.db import models


def _make_feed_token() -> str:
    return secrets.token_urlsafe(16)


def _make_instance_uid() -> str:
    return f"instance-{uuid.uuid4()}@household"


class Person(models.Model):
    name = models.CharField(max_length=100, unique=True)
    weekly_capacity_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Minutes of chores per week this person can take. "
        "Leave blank for an equal share of the total.",
    )
    feed_token = models.CharField(
        max_length=64,
        unique=True,
        default=_make_feed_token,
        help_text="Unguessable slug used in this person's ICS feed URL.",
    )

    class Meta:
        verbose_name_plural = "people"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Chore(models.Model):
    name = models.CharField(max_length=150)
    effort_minutes = models.PositiveIntegerField(
        help_text="Estimated time to do this chore once. Drives fair-share balancing."
    )
    interval_min_days = models.PositiveIntegerField(
        help_text="Minimum days between occurrences."
    )
    interval_max_days = models.PositiveIntegerField(
        help_text="Maximum days between occurrences."
    )
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    anchor_date = models.DateField(
        help_text="Date this chore was last done (or should start counting from). "
        "The next occurrence is scheduled interval_min..max days after this."
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(interval_min_days__lte=models.F("interval_max_days")),
                name="chore_interval_min_lte_max",
            ),
            models.CheckConstraint(
                condition=models.Q(interval_min_days__gte=1),
                name="chore_interval_min_at_least_1",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if (
            self.interval_min_days
            and self.interval_max_days
            and self.interval_min_days > self.interval_max_days
        ):
            raise ValidationError(
                {"interval_min_days": "Minimum interval cannot exceed the maximum."}
            )


class ScheduledInstance(models.Model):
    chore = models.ForeignKey(Chore, on_delete=models.PROTECT, related_name="instances")
    person = models.ForeignKey(
        Person, on_delete=models.PROTECT, related_name="instances"
    )
    date = models.DateField()
    pinned = models.BooleanField(
        default=False,
        help_text="Pinned instances are kept as-is when the schedule is regenerated.",
    )
    uid = models.CharField(max_length=100, unique=True, default=_make_instance_uid)
    sequence = models.PositiveIntegerField(
        default=0, help_text="Bumped whenever this instance changes, for ICS updates."
    )

    class Meta:
        ordering = ["date", "chore__name"]
        indexes = [
            models.Index(fields=["person", "date"]),
            models.Index(fields=["chore", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.chore} — {self.person} on {self.date:%Y-%m-%d}"


class FairnessLedger(models.Model):
    person = models.OneToOneField(
        Person, on_delete=models.CASCADE, related_name="ledger"
    )
    balance_minutes = models.IntegerField(
        default=0,
        help_text="Running credit (negative) / debt (positive) carried between weeks "
        "so past imbalance is corrected going forward.",
    )

    def __str__(self) -> str:
        return f"{self.person}: {self.balance_minutes:+d} min"
