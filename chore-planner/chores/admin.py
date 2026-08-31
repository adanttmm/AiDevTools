from django.contrib import admin

from .models import Chore, FairnessLedger, Person, ScheduledInstance


class LedgerInline(admin.StackedInline):
    model = FairnessLedger
    can_delete = False
    extra = 0


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("name", "weekly_capacity_minutes", "feed_token")
    search_fields = ("name",)
    readonly_fields = ("feed_token",)
    inlines = [LedgerInline]


@admin.register(Chore)
class ChoreAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "effort_minutes",
        "interval_min_days",
        "interval_max_days",
        "active",
        "anchor_date",
    )
    list_filter = ("active",)
    search_fields = ("name",)
    list_editable = ("active",)


@admin.register(ScheduledInstance)
class ScheduledInstanceAdmin(admin.ModelAdmin):
    list_display = ("date", "chore", "person", "pinned", "sequence")
    list_filter = ("pinned", "person", "date", "chore")
    search_fields = ("chore__name", "person__name")
    list_editable = ("pinned",)
    date_hierarchy = "date"
    autocomplete_fields = ("chore", "person")
    readonly_fields = ("uid",)


@admin.register(FairnessLedger)
class FairnessLedgerAdmin(admin.ModelAdmin):
    list_display = ("person", "balance_minutes")
