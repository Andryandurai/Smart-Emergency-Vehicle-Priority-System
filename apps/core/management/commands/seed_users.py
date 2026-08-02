"""Create the demo role accounts and the groups the permission classes use.

SEVPS separates three duties, and :mod:`apps.core.permissions` enforces the
split by group membership. Those groups have to exist before the split means
anything, so this command creates them alongside one demo account each.

    python manage.py seed_users

The passwords are deliberately obvious and printed to the console: these are
local pilot credentials, not secrets. The command refuses to run with
DEBUG off unless explicitly forced, so a production deployment cannot acquire
a known-password account by accident.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.roles import ALIASES, ROLES, Role

DEMO_USERS = [
    {
        "username": "admin",
        "password": "sevps-admin",
        "email": "admin@sevps.local",
        "first_name": "System",
        "last_name": "Administrator",
        "is_staff": True,
        "is_superuser": True,
        "groups": [Role.ADMIN],
    },
    {
        "username": "police",
        "password": "sevps-police",
        "email": "police@sevps.local",
        "first_name": "Traffic",
        "last_name": "Police",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.TRAFFIC_POLICE],
    },
    {
        "username": "dispatcher",
        "password": "sevps-dispatcher",
        "email": "dispatcher@sevps.local",
        "first_name": "Emergency",
        "last_name": "Dispatcher",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.DISPATCHER],
    },
    {
        "username": "paramedic",
        "password": "sevps-paramedic",
        "email": "paramedic@sevps.local",
        "first_name": "Ambulance",
        "last_name": "Crew",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.AMBULANCE],
    },
    {
        "username": "hospital",
        "password": "sevps-hospital",
        "email": "hospital@sevps.local",
        "first_name": "Emergency",
        "last_name": "Department",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.HOSPITAL],
    },
    {
        "username": "public",
        "password": "sevps-public",
        "email": "public@sevps.local",
        "first_name": "Road",
        "last_name": "User",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.PUBLIC],
    },
    # Retained so the pre-RBAC demo account keeps working after upgrade; it
    # maps onto the traffic police role via the legacy `operators` alias.
    {
        "username": "operator",
        "password": "sevps-operator",
        "email": "operator@sevps.local",
        "first_name": "Control",
        "last_name": "Room",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.TRAFFIC_POLICE],
    },
]


class Command(BaseCommand):
    help = "Create SEVPS role groups and demo accounts for each role."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Allow seeding known-password accounts even when DEBUG is off.",
        )
        parser.add_argument(
            "--keep-passwords", action="store_true",
            help="Create missing accounts but leave existing passwords untouched.",
        )
        parser.add_argument(
            "--reset-roles", action="store_true",
            help=(
                "Force existing accounts to exactly the privileges listed here, "
                "including REMOVING staff/superuser rights they already hold. "
                "Use to clean up an account that drifted above its documented role."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to create accounts with published passwords while DEBUG is off.\n"
                "Create real accounts with `createsuperuser`, or pass --force if this is "
                "an isolated pilot environment."
            )

        for key, spec in ROLES.items():
            group, created = Group.objects.get_or_create(name=key)
            self.stdout.write(
                f"  {'created' if created else 'exists '} group {group.name:<18} {spec.description}"
            )

        # Legacy groups from before the role registry are migrated in place:
        # their members are moved onto the canonical role and the old group is
        # left behind (empty) rather than deleted, so a rollback is possible.
        for legacy, canonical_key in ALIASES.items():
            old = Group.objects.filter(name=legacy).first()
            if old is None:
                continue
            target = Group.objects.get(name=canonical_key)
            moved = 0
            for user in old.user_set.all():
                user.groups.add(target)
                user.groups.remove(old)
                moved += 1
            if moved:
                self.stdout.write(
                    self.style.WARNING(
                        f"  migrated {moved} user(s) from legacy group "
                        f"{legacy!r} -> {canonical_key!r}"
                    )
                )

        self.stdout.write("")
        rows = []
        for spec in DEMO_USERS:
            user, created = User.objects.get_or_create(
                username=spec["username"],
                defaults={
                    "email": spec["email"],
                    "first_name": spec["first_name"],
                    "last_name": spec["last_name"],
                    "is_staff": spec["is_staff"],
                    "is_superuser": spec["is_superuser"],
                },
            )

            # Never *silently* strip privileges from an account that already
            # has them; demoting a live administrator as a side effect of a
            # seed command would be a nasty surprise. --reset-roles makes it
            # an explicit, requested action.
            if options["reset_roles"]:
                if (user.is_staff and not spec["is_staff"]) or (
                    user.is_superuser and not spec["is_superuser"]
                ):
                    self.stdout.write(
                        self.style.WARNING(
                            f"  demoting {user.username!r} to its documented role "
                            f"(was staff={user.is_staff} superuser={user.is_superuser})"
                        )
                    )
                user.is_staff = spec["is_staff"]
                user.is_superuser = spec["is_superuser"]
            else:
                user.is_staff = user.is_staff or spec["is_staff"]
                user.is_superuser = user.is_superuser or spec["is_superuser"]

            if created or not options["keep_passwords"]:
                user.set_password(spec["password"])
            user.save()

            user.groups.set(Group.objects.filter(name__in=spec["groups"]))
            rows.append((spec, user, created))

        self._report(rows, options["keep_passwords"])

    def _report(self, rows, keep_passwords: bool):
        width = max(len(s["username"]) for s, _, _ in rows)
        self.stdout.write(self.style.SUCCESS("SEVPS demo accounts\n"))
        self.stdout.write(f"  {'USERNAME'.ljust(width)}  {'PASSWORD'.ljust(20)}  ROLE")
        self.stdout.write(f"  {'-' * width}  {'-' * 20}  {'-' * 56}")
        for spec, user, created in rows:
            password = "(unchanged)" if keep_passwords and not created else spec["password"]
            # The role label comes from the registry rather than being repeated
            # here, so the table can never drift from what was actually granted.
            role_label = (
                ", ".join(ROLES[g].label for g in spec["groups"] if g in ROLES) or "no role"
            )
            self.stdout.write(
                f"  {spec['username'].ljust(width)}  {password.ljust(20)}  {role_label}"
            )

        self.stdout.write("")
        self.stdout.write("  Sign in at  /login/   (dashboards)")
        self.stdout.write("  Admin at    /admin/   (admin + superuser only)")
        self.stdout.write(
            self.style.WARNING(
                "\n  These passwords are published in the source. Change them before any "
                "deployment that is reachable beyond your own machine."
            )
        )
