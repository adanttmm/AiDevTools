from django.shortcuts import render


def dashboard(request):
    """Placeholder dashboard. Will show the upcoming schedule and a
    "Regenerate" button once the balancer lands (build phase 4)."""
    return render(request, "chores/dashboard.html", {})
