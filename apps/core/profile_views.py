"""Staff profile REST surface, and the demo-account list for the login page."""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsAuthenticatedRole, PublicRead
from apps.core.profiles import StaffProfile
from apps.core.roles import ROLES, Role, canonical, user_roles

User = get_user_model()

#: Fields a user may edit about themselves. Deliberately short: a staff id or
#: a qualification is set by whoever runs the roster, not by the holder.
EDITABLE = ("phone", "blood_group", "emergency_contact")

#: Maximum avatar upload. Large enough for a phone photo, small enough that a
#: crew on a patchy connection is not stuck uploading during a shift start.
MAX_AVATAR_BYTES = 4 * 1024 * 1024


class ProfileUpdateSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True, max_length=32)
    blood_group = serializers.CharField(required=False, allow_blank=True, max_length=8)
    emergency_contact = serializers.CharField(
        required=False, allow_blank=True, max_length=140
    )


class MyProfileView(APIView):
    """The signed-in user's own profile.

    Self-service only - there is no user id in the URL, so this endpoint
    cannot be pointed at somebody else's record however it is called.
    """

    permission_classes = [IsAuthenticatedRole]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def _profile(self, user) -> StaffProfile:
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self._profile(request.user)
        roles = sorted(user_roles(request.user))
        return Response(
            {
                **profile.as_payload(request),
                "roles": roles,
                "role_labels": [ROLES[r].label for r in roles if r in ROLES],
                "is_paramedic": Role.PARAMEDIC in roles,
                "is_driver": Role.AMBULANCE in roles,
            }
        )

    def patch(self, request):
        profile = self._profile(request.user)
        serializer = ProfileUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(profile, field, value)
        profile.save()
        return Response(profile.as_payload(request))


class MyAvatarView(APIView):
    """Upload or clear the signed-in user's profile picture."""

    permission_classes = [IsAuthenticatedRole]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get("avatar")
        if upload is None:
            return Response(
                {"detail": "No image was uploaded."}, status=status.HTTP_400_BAD_REQUEST
            )
        if upload.size > MAX_AVATAR_BYTES:
            return Response(
                {
                    "detail": (
                        f"Image is too large ({upload.size // 1024} kB). "
                        f"Maximum is {MAX_AVATAR_BYTES // 1024} kB."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not (upload.content_type or "").startswith("image/"):
            return Response(
                {"detail": "That file is not an image."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        profile, _ = StaffProfile.objects.get_or_create(user=request.user)
        # Replace rather than accumulate - avatar_path keys on the user id, so
        # the old file is overwritten and storage does not grow per upload.
        profile.avatar = upload
        profile.save(update_fields=["avatar", "updated_at"])
        return Response(profile.as_payload(request))

    def delete(self, request):
        profile, _ = StaffProfile.objects.get_or_create(user=request.user)
        profile.avatar.delete(save=False)
        profile.avatar = None
        profile.save(update_fields=["avatar", "updated_at"])
        return Response(profile.as_payload(request))


class DemoAccountsView(APIView):
    """Sign-in credentials for the seeded demo accounts.

    Public, and only in DEBUG. These passwords are already published in
    ``seed_users.py`` and the command refuses to create them when DEBUG is
    off, so serving them here adds no exposure that the repository does not
    already have - but returning them from a production deployment would, so
    it does not.
    """

    permission_classes = [PublicRead]

    def get_permissions(self):
        return [AllowAny()]

    def get(self, request):
        if not settings.DEBUG:
            return Response({"accounts": [], "available": False})

        from apps.core.management.commands.seed_users import DEMO_USERS

        accounts = []
        for spec in DEMO_USERS:
            user = User.objects.filter(username=spec["username"]).first()
            if user is None:
                continue
            # Actual group membership, not `user_roles`, which grants a
            # superuser every role - correct for authorisation, wrong for a
            # sign-in list, where it would file the administrator under
            # "Paramedic logins".
            roles = sorted(
                canonical(name)
                for name in user.groups.values_list("name", flat=True)
            )
            profile = getattr(user, "profile", None)
            accounts.append(
                {
                    "username": spec["username"],
                    "password": spec["password"],
                    "name": user.get_full_name() or spec["username"],
                    "roles": roles,
                    "role_labels": [ROLES[r].label for r in roles if r in ROLES],
                    "is_paramedic": Role.PARAMEDIC in roles,
                    "is_driver": Role.AMBULANCE in roles,
                    "staff_id": profile.staff_id if profile else "",
                    "qualification": profile.qualification if profile else "",
                    "base_station": profile.base_station if profile else "",
                }
            )
        return Response({"accounts": accounts, "available": True})
