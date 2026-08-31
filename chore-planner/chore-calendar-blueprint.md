# Household Chore Planner — Project Blueprint

_Draft. Last updated: 2026-08-31. Stack: **Django**._

A tool for managing household chores and publishing them to calendars as
per-person ICS feeds. A single admin defines people and chores; the tool
auto-balances the workload by estimated time and generates subscribable
calendar feeds.

---

## 1. Scope (locked)

| Dimension          | Decision                                                             |
| ------------------ | ------------------------------------------------------------------- |
| Interface          | Django web app, single admin (you)                                 |
| Household members  | Model rows, not auth users — no login                              |
| Assignment         | Auto-balance by **estimated minutes**, weekly window               |
| Chore timing       | **Flexible interval** — e.g. "every 3–5 days"; tool picks who + when |
| Output             | **One ICS feed per person**, pushed to a static host               |
| Completion         | Publish-only — a planner, not a tracker                            |
| Stack              | Django, runs locally, pushes generated `.ics` files out           |
| Availability       | Not modeled (hand-edit if someone is away)                         |

---

## 2. Architecture

```
┌──────────────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Django admin +      │────▶│  DB (SQLite) │◀────│  balancer.py    │
│  dashboard view      │     │  Person      │     │  1. find due    │
│  - CRUD people/chores│     │  Chore       │     │     instances   │
│  - "Regenerate" btn  │     │  Scheduled-  │     │  2. LPT assign  │
│    (admin action)    │     │   Instance   │     │     by minutes  │
│  - upcoming schedule │     │  Fairness-   │     │  3. spread dates│
└──────────────────────┘     │   Ledger     │     └─────────────────┘
                             └──────┬───────┘
        manage.py generate ─────────┤
        manage.py publish ──────────┤
                                    ▼
                          ┌───────────────────┐     ┌──────────────────┐
                          │ calendar_feed.py  │────▶│ feeds/<name>.ics  │
                          │ (icalendar)       │     └────────┬─────────┘
                          └───────────────────┘              ▼
                                                    ┌──────────────────┐
                                                    │ publisher.py      │
                                                    │ → GitHub Pages /  │
                                                    │   S3 / Netlify    │
                                                    └──────────────────┘
```

Everything runs on the admin's machine. `python manage.py generate` and
`python manage.py publish` are management commands (run by hand or a local
cron / systemd timer). The Django admin is the CRUD UI; a small custom
dashboard view shows the upcoming schedule and exposes a "Regenerate"
button.

> Django can also serve the feeds directly from a view
> (`/feed/<token>.ics`) if the app is ever hosted. The push-to-static-host
> model matches the "runs locally" decision; keep both paths in mind.

---

## 3. Data model (Django models)

`chores/models.py`:

- **Person**
  - `name: CharField`
  - `weekly_capacity_minutes: PositiveIntegerField(null=True)` — optional;
    `null` = equal share
  - `feed_token: CharField(unique=True)` — unguessable, used in the feed URL
    (auto-generated with `secrets.token_urlsafe`)
- **Chore**
  - `name: CharField`
  - `effort_minutes: PositiveIntegerField`
  - `interval_min_days: PositiveIntegerField`
  - `interval_max_days: PositiveIntegerField`
  - `notes: TextField(blank=True)`
  - `active: BooleanField(default=True)`
  - `anchor_date: DateField` — last done / start date for interval math
- **ScheduledInstance**
  - `chore: ForeignKey(Chore, on_delete=PROTECT)`
  - `person: ForeignKey(Person, on_delete=PROTECT)`
  - `date: DateField`
  - `pinned: BooleanField(default=False)` — survives regeneration
  - `uid: CharField(unique=True)` — stable ICS UID
    (`instance-<uuid>@household`)
  - `sequence: PositiveIntegerField(default=0)` — bumped on change
  - `Meta.indexes` on `(person, date)` and `(chore, date)`
- **FairnessLedger** _(optional, recommended)_
  - `person: OneToOneField(Person)`
  - `balance_minutes: IntegerField(default=0)` — running credit/debt

Django migrations manage schema. Register all four models in
`chores/admin.py` with list filters on `active`, `person`, `date`.

---

## 4. The balancer (core algorithm)

`chores/balancer.py` — a plain module, no Django-specific code beyond ORM
queries passed in or done at the edges. Run weekly for the target week
(Mon–Sun):

1. **Due detection.** For each active chore, from its last scheduled date +
   `interval_min/max`, compute the window its next instance must fall in. If
   that window intersects the target week, emit an instance with a feasible
   date range (clamped to the week). Short intervals can emit 2+ instances
   per week.
2. **Assignment.** Longest-processing-time-first greedy: sort instances by
   `effort_minutes` descending; assign each to the person with the lowest
   running total (seeded from the ledger if used). For a household
   (<10 people, dozens of instances) this lands within a few minutes of
   optimal — no solver needed.
3. **Date spreading.** Within each person's assigned instances, place dates
   to (a) respect each chore's spacing vs its previous instance and
   (b) avoid piling multiple chores on one evening.
4. **Persist.** Inside a `transaction.atomic()` block: delete non-pinned
   future instances for the week, `bulk_create` the new ones, update the
   ledger. Keep the run deterministic (seed `random` from the week's ISO
   date) so re-running without data changes is stable.

**Manual override:** admin sets `pinned=True` on an instance (or edits its
person/date) in the Django admin; regeneration leaves pinned rows alone and
balances around them.

Invoked by the `generate` management command and by the dashboard's
"Regenerate" button (which calls the same function).

---

## 5. ICS feeds

`chores/calendar_feed.py`:

- One `VCALENDAR` per person → `feeds/<slug(name)>.ics`, containing that
  person's instances across the published horizon (suggest **2 weeks**
  ahead, since subscribed calendars refresh only every ~8–24h).
- Each `VEVENT`: stable `UID` from `ScheduledInstance.uid`, all-day or a
  default time block, `SUMMARY` = chore name, `DESCRIPTION` = effort +
  notes, optional `VALARM` reminder, `SEQUENCE` from the model field so
  calendars update instead of duplicating.
- `publisher.py` copies the files to a static host. Feed URL carries the
  person's `feed_token` so it is not publicly guessable.

---

## 6. Stack

- Python 3.12, `uv` + `pyproject.toml`
- **Django 5.x** — admin for CRUD, one custom dashboard view, management
  commands for `generate` / `publish` / `seed`
- **DB**: SQLite (Django default)
- **ICS**: `icalendar`
- **Dates**: `python-dateutil`
- **Frontend**: Django templates; `django-htmx` optional for the
  "Regenerate" button without a full page reload
- **Publisher deps**: `boto3` (S3) or shell `git` (GitHub Pages) — pick per
  decision #2
- **Tests**: Django test runner (or `pytest-django`)

### Project structure

```
chore-planner/
  manage.py
  pyproject.toml
  config/                    # Django project package
    settings.py  urls.py  wsgi.py  asgi.py
  chores/                    # main app
    admin.py                 # register Person, Chore, ScheduledInstance, Ledger
    models.py
    balancer.py              # due detection + LPT + date spread
    calendar_feed.py         # icalendar builders
    publisher.py             # push .ics to static host
    views.py                 # dashboard + regenerate action
    urls.py
    templates/chores/
    migrations/
    management/commands/
      generate.py            # python manage.py generate [--week YYYY-Www]
      publish.py             # python manage.py publish
      seed.py                # python manage.py seed  (demo data)
    tests/
  feeds/                     # generated .ics output (gitignored)
```

---

## 7. Build phases

0. **Scaffold** — `django-admin startproject config .`, `startapp chores`,
   add models + first migration, `seed` command with demo people/chores
1. **Balancer core** — `balancer.py` + tests; `generate` command writes
   `ScheduledInstance` rows and prints the plan
2. **ICS generation** — `calendar_feed.py`, per-person files, stable UIDs;
   verify a file imports cleanly into Google/Apple Calendar
3. **Publisher** — `publisher.py` + `publish` command; end-to-end
   `manage.py generate && manage.py publish`
4. **Web UI** — tune Django admin (list displays, filters, inline pinning);
   dashboard view with upcoming schedule + "Regenerate" button
5. **Polish** — `VALARM` reminders, `FairnessLedger` carry-over, data
   export/backup command

Django admin covers most of Phase 4 out of the box — the only custom UI is
the dashboard and the regenerate trigger.

---

## 8. Open decisions

1. **Event time** — all-day events, one fixed daily block (e.g. 18:00–19:00),
   or a preferred time per chore?
2. **Publish target** — private GitHub repo + Pages, S3, Netlify drop, or
   something on the home network (Tailscale)? Determines `publisher.py` deps.
3. **Fairness ledger** — strict per-week, or carry week-to-week credit/debt
   for genuine long-run fairness? (Recommendation: use the ledger.)
4. **Multiple instances per week** for short intervals (every 3–5 days ≈
   1.6×/week) — wanted, or cap at one per chore per week?
5. **First-run anchor** — OK to seed each chore with an `anchor_date` so the
   first schedule is not lopsided?
6. **Chore constraints** — any chore a given person cannot do, or that must
   land on a specific weekday despite being "flexible"? (Would add an
   `excluded_people` M2M and/or `fixed_weekday` field.)
7. **Reminder lead time** — none, morning-of, or day-before?
8. **Household size** — how many people? (Confirms greedy is fine; it is for
   under ~10.)

---

## Notes

- Google Calendar / Gmail connectors are not required for this design (ICS
  feed chosen). If Google Calendar API publishing is ever wanted, that
  connector must be authorized in claude.ai connector settings.
- Subscribed ICS feeds are pull-based: expect up to ~8–24h propagation
  delay in Google/Apple calendars. Publish a horizon well ahead of that.
- Single admin → Django admin login (a superuser) is the only auth needed.
  Household members are plain `Person` rows, never `auth.User`.
