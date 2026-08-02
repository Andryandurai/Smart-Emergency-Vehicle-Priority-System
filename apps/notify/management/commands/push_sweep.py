"""Retire push subscriptions that can no longer be delivered to.

Worth running on a schedule. Dead endpoints are not free: the fan-out is
synchronous, so every retired browser that stays in the table adds a doomed
HTTPS request — with its timeout — to every notification it would have
matched. A few hundred of those turn a 200 ms fan-out into a stall.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.notify.models import PushSubscription
from apps.notify.service import prune_dead_subscriptions


class Command(BaseCommand):
    help = "Deactivate push subscriptions that are stale or repeatedly failing."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=60,
                            help="Inactivity threshold in days (default: 60).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        active = PushSubscription.objects.active().count()
        if options["dry_run"]:
            from django.utils import timezone

            cutoff = timezone.now() - timezone.timedelta(days=options["days"])
            count = PushSubscription.objects.filter(
                is_active=True, last_success_at__isnull=False, last_success_at__lt=cutoff
            ).count()
            self.stdout.write(f"{count} of {active} active subscriptions would be retired.")
            return

        pruned = prune_dead_subscriptions(options["days"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Retired {pruned} stale subscriptions; "
                f"{PushSubscription.objects.active().count()} remain active."
            )
        )
