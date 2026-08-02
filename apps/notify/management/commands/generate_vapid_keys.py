"""Create the VAPID keypair Web Push needs.

Run once per deployment. Rerunning with --force invalidates every existing
subscription, because a push service binds a subscription to the key that
created it — so the command refuses by default rather than making that
irreversible for a typo.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.notify import vapid


class Command(BaseCommand):
    help = "Generate the VAPID keypair used to sign Web Push messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Overwrite an existing key. Invalidates every push subscription.",
        )
        parser.add_argument(
            "--print-only", action="store_true",
            help="Print a keypair for injection as env vars; writes nothing to disk.",
        )

    def handle(self, *args, **options):
        if options["print_only"]:
            private, public = vapid.generate_keypair()
            self.stdout.write(self.style.MIGRATE_HEADING("Set these on the server:\n"))
            self.stdout.write(f'SEVPS_VAPID_PRIVATE_KEY="{private.replace(chr(10), chr(92) + "n")}"')
            self.stdout.write(f"SEVPS_VAPID_PUBLIC_KEY={public}")
            self.stdout.write(
                "\nSEVPS_VAPID_SUBJECT must also be set to a contactable "
                "mailto: or https: URI."
            )
            return

        try:
            path, public = vapid.write_keypair(overwrite=options["force"])
        except FileExistsError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"VAPID private key written to {path}"))
        self.stdout.write(f"Public key: {public}")
        if options["force"]:
            self.stdout.write(
                self.style.WARNING(
                    "Existing push subscriptions are now invalid. Every user must "
                    "re-grant notification permission."
                )
            )
        if not vapid.claims_configured():
            self.stdout.write(
                self.style.WARNING(
                    "SEVPS_VAPID_SUBJECT is not set. Push services require a "
                    "contactable mailto: or https: URI; pushes will fail without it."
                )
            )
