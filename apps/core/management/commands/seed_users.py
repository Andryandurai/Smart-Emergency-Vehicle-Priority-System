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
    # --- Paramedics -------------------------------------------------------
    # Several, because a shift is a pairing and a driver has to be able to
    # pick *which* paramedic they are crewing with. One demo paramedic makes
    # the crew directory a formality rather than a choice.
    {
        "username": "paramedic",
        "password": "sevps-paramedic",
        "email": "paramedic@sevps.local",
        "first_name": "Anita",
        "last_name": "Raman",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.PARAMEDIC],
        "profile": {
            "staff_id": "PM-1041",
            "qualification": "Advanced Life Support Paramedic",
            "base_station": "Chennai Central Ambulance Base",
            "phone": "+91 98400 11041",
            "blood_group": "O+",
            "emergency_contact": "R. Raman +91 98400 22041",
        },
    },
    {
        "username": "paramedic2",
        "password": "sevps-paramedic2",
        "email": "paramedic2@sevps.local",
        "first_name": "Vikram",
        "last_name": "Iyer",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.PARAMEDIC],
        "profile": {
            "staff_id": "PM-1058",
            "qualification": "Emergency Medical Technician - Advanced",
            "base_station": "Adyar Response Station",
            "phone": "+91 98400 11058",
            "blood_group": "B+",
            "emergency_contact": "S. Iyer +91 98400 22058",
        },
    },
    {
        "username": "paramedic3",
        "password": "sevps-paramedic3",
        "email": "paramedic3@sevps.local",
        "first_name": "Fathima",
        "last_name": "Basheer",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.PARAMEDIC],
        "profile": {
            "staff_id": "PM-1073",
            "qualification": "Critical Care Paramedic",
            "base_station": "Kilpauk Emergency Station",
            "phone": "+91 98400 11073",
            "blood_group": "A-",
            "emergency_contact": "N. Basheer +91 98400 22073",
        },
    },
    {
        "username": "paramedic4",
        "password": "sevps-paramedic4",
        "email": "paramedic4@sevps.local",
        "first_name": "Joseph",
        "last_name": "Fernandes",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.PARAMEDIC],
        "profile": {
            "staff_id": "PM-1090",
            "qualification": "Emergency Medical Technician - Basic",
            "base_station": "Guindy Fire Station",
            "phone": "+91 98400 11090",
            "blood_group": "AB+",
            "emergency_contact": "M. Fernandes +91 98400 22090",
        },
    },
    {
        # A second crew account, because a shift takeover is a handshake
        # between two people and cannot be demonstrated - or tested against a
        # running system - with only one.  Driver and paramedic are seats on a
        # shift rather than separate roles: the same person drives on Monday
        # and attends on Tuesday.
        "username": "driver",
        "password": "sevps-driver",
        "email": "driver@sevps.local",
        "first_name": "Suresh",
        "last_name": "Kumar",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.AMBULANCE],
        "profile": {
            "staff_id": "DR-2014",
            "qualification": "Emergency Vehicle Operator - Class A",
            "base_station": "Chennai Central Ambulance Base",
            "phone": "+91 98400 32014",
            "blood_group": "O-",
            "emergency_contact": "L. Kumar +91 98400 42014",
        },
    },
    {
        "username": "driver2",
        "password": "sevps-driver2",
        "email": "driver2@sevps.local",
        "first_name": "Mohan",
        "last_name": "Rajan",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.AMBULANCE],
        "profile": {
            "staff_id": "DR-2031",
            "qualification": "Emergency Vehicle Operator - Class A",
            "base_station": "Adyar Response Station",
            "phone": "+91 98400 32031",
            "blood_group": "B-",
            "emergency_contact": "P. Rajan +91 98400 42031",
        },
    },
    {
        "username": "driver3",
        "password": "sevps-driver3",
        "email": "driver3@sevps.local",
        "first_name": "Karthik",
        "last_name": "Selvam",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.AMBULANCE],
        "profile": {
            "staff_id": "DR-2047",
            "qualification": "Emergency Vehicle Operator - Class B",
            "base_station": "Kilpauk Emergency Station",
            "phone": "+91 98400 32047",
            "blood_group": "A+",
            "emergency_contact": "D. Selvam +91 98400 42047",
        },
    },
    {
        "username": "driver4",
        "password": "sevps-driver4",
        "email": "driver4@sevps.local",
        "first_name": "Imran",
        "last_name": "Sheikh",
        "is_staff": False,
        "is_superuser": False,
        "groups": [Role.AMBULANCE],
        "profile": {
            "staff_id": "DR-2062",
            "qualification": "Emergency Vehicle Operator - Class A",
            "base_station": "Guindy Fire Station",
            "phone": "+91 98400 32062",
            "blood_group": "O+",
            "emergency_contact": "H. Sheikh +91 98400 42062",
        },
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

            # Identity details. Updated rather than only created, so editing
            # the spec and re-seeding actually corrects the roster - but the
            # avatar is never touched, because a crew member who uploaded
            # their own photograph should not lose it to a seed run.
            if spec.get("profile"):
                from apps.core.profiles import StaffProfile

                StaffProfile.objects.update_or_create(
                    user=user, defaults=spec["profile"]
                )

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
