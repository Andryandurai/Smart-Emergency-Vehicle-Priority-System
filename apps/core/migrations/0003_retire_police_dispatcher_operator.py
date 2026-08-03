"""Remove the traffic police, dispatcher and operator roles.

SEVPS is operated by administrators, ambulance crews and hospitals. The three
retired roles had no holder outside the demo seed, and every authority they
carried - signal override, corridor release, opening and cancelling responses
- is already held by the administrator role.

What this does, and does not, delete:

* the ``traffic_police``, ``dispatchers`` and legacy ``operators`` groups go;
* the seeded ``police`` / ``dispatcher`` / ``operator`` demo accounts go with
  them, because they exist only to demonstrate roles that no longer exist -
  *unless* something still references them, in which case they are
  deactivated instead. ``CrewShift.driver`` is PROTECT, and destroying a
  shift record to tidy up a login is the wrong way round: the roster is the
  audit trail for who was on which ambulance, and it outranks the account;
* any *other* account that happened to hold one of those groups is kept and
  simply loses the group. Deleting a real person's login as a side effect of
  a role change would be the wrong trade, and they can be re-granted a
  surviving role by an administrator.

Nothing in the permission layer needs to change: ``IsTrafficPolice`` and
``IsDispatcher`` derive their required roles from the registry, which now
resolves both to administrators alone.
"""
from django.db import migrations
from django.utils.crypto import get_random_string

RETIRED_GROUPS = ("traffic_police", "dispatchers", "operators")
#: Seeded accounts that exist purely to demonstrate a retired role.
RETIRED_DEMO_USERS = ("police", "dispatcher", "operator")


def retire(apps, schema_editor):
    from django.db.models import ProtectedError

    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")

    for user in User.objects.filter(
        username__in=RETIRED_DEMO_USERS, is_superuser=False
    ):
        try:
            user.delete()
        except ProtectedError:
            # Referenced by a crew shift or another protected record. Retire
            # the account without touching what points at it: no groups, no
            # password that works, no way to sign in - but the shift history
            # that names them stays readable.
            user.groups.clear()
            user.is_active = False
            # Historical models carry fields, not methods, so there is no
            # set_unusable_password() here. Django's convention is that a
            # password beginning with "!" can never match a hash.
            user.password = f"!retired{get_random_string(32)}"
            user.save(update_fields=["is_active", "password"])

    Group.objects.filter(name__in=RETIRED_GROUPS).delete()


def restore_is_not_possible(apps, schema_editor):
    """One-way.

    Recreating an empty group is trivial; recreating the accounts and the
    membership that was in it is not, because that information is gone. Left
    as a no-op so the migration can still be unapplied without erroring.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(retire, restore_is_not_possible),
    ]
