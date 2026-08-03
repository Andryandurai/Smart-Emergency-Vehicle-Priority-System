"""Staff profiles - who a user is, as opposed to what they may do.

Roles answer "what can this account do". This answers "who is holding it":
the name a colleague would use, a contact number, a qualification, and a
photograph. A paramedic accepting a shift request needs to recognise the
driver asking, and a hospital taking a handover needs a name to write down.

Kept apart from :class:`~django.contrib.auth.models.User` rather than swapped
in as a custom user model, because the project is well past the point where
changing AUTH_USER_MODEL is a safe migration.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


def avatar_path(instance, filename: str) -> str:
    """Stored under the user id, so a re-upload replaces rather than piles up."""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return f"avatars/{instance.user_id}.{suffix}"


class StaffProfile(TimeStampedModel):
    """Identity details for an operational account."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    #: Service identity number - what appears on a duty roster.
    staff_id = models.CharField(max_length=32, blank=True, db_index=True)
    phone = models.CharField(max_length=32, blank=True)
    #: Free text rather than choices: qualification frameworks differ by state
    #: and a dropdown that cannot express a real crew member's grade just
    #: means the field gets filled with the nearest wrong answer.
    qualification = models.CharField(max_length=120, blank=True)
    base_station = models.CharField(max_length=140, blank=True)
    blood_group = models.CharField(max_length=8, blank=True)
    emergency_contact = models.CharField(max_length=140, blank=True)

    avatar = models.ImageField(upload_to=avatar_path, blank=True, null=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self) -> str:
        return f"profile: {self.user.get_username()}"

    @property
    def display_name(self) -> str:
        return self.user.get_full_name() or self.user.get_username()

    def as_payload(self, request=None) -> dict:
        url = None
        if self.avatar:
            url = self.avatar.url
            if request is not None:
                url = request.build_absolute_uri(url)
        return {
            "username": self.user.get_username(),
            "name": self.display_name,
            "first_name": self.user.first_name,
            "last_name": self.user.last_name,
            "email": self.user.email,
            "staff_id": self.staff_id,
            "phone": self.phone,
            "qualification": self.qualification,
            "base_station": self.base_station,
            "blood_group": self.blood_group,
            "emergency_contact": self.emergency_contact,
            "avatar_url": url,
        }
