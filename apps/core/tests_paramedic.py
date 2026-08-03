"""The driver/paramedic split, staff profiles, and the demo-account list."""
from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.enums import VehicleStatus
from apps.core.profiles import StaffProfile
from apps.core.roles import Role, canonical, has_role, user_roles
from apps.fleet.models import EmergencyVehicle


def make(username: str, *roles: str) -> User:
    user = User.objects.create_user(username, password="pw", first_name="Test", last_name="User")
    for role in roles:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


class RoleSplitTests(TestCase):
    def test_paramedic_is_its_own_role(self):
        medic = make("medic", Role.PARAMEDIC)
        self.assertIn(Role.PARAMEDIC, user_roles(medic))
        self.assertNotIn(Role.AMBULANCE, user_roles(medic))

    def test_the_legacy_paramedics_group_still_maps_to_the_driver_role(self):
        """It was an alias of AMBULANCE before the split and must stay one.

        Silently re-pointing it at the new role would move every upgraded
        deployment's drivers into the paramedic portal.
        """
        self.assertEqual(canonical("paramedics"), Role.AMBULANCE)

    def test_a_driver_is_not_a_paramedic_and_vice_versa(self):
        driver = make("drv", Role.AMBULANCE)
        medic = make("med", Role.PARAMEDIC)
        self.assertFalse(has_role(driver, Role.PARAMEDIC))
        self.assertFalse(has_role(medic, Role.AMBULANCE))

    def test_a_superuser_still_holds_every_role(self):
        root = User.objects.create_superuser("root", password="pw")
        self.assertIn(Role.PARAMEDIC, user_roles(root))
        self.assertIn(Role.AMBULANCE, user_roles(root))


class DriverOnlyActionTests(TestCase):
    """Requirement: only the driver selects the ambulance and opens the shift."""

    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-PM", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = make("pmdriver", Role.AMBULANCE)
        self.medic = make("pmmedic", Role.PARAMEDIC)

    def test_a_paramedic_cannot_list_selectable_ambulances(self):
        response = client_for(self.medic).get("/api/v1/fleet/shifts/selectable-vehicles/")
        self.assertEqual(response.status_code, 403)

    def test_a_paramedic_cannot_open_a_shift(self):
        response = client_for(self.medic).post(
            "/api/v1/fleet/shifts/open/",
            {"vehicle_callsign": "AMB-PM", "paramedic_username": self.medic.username},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_driver_can_do_both(self):
        listing = client_for(self.driver).get("/api/v1/fleet/shifts/selectable-vehicles/")
        self.assertEqual(listing.status_code, 200)

        opened = client_for(self.driver).post(
            "/api/v1/fleet/shifts/open/",
            {"vehicle_callsign": "AMB-PM", "paramedic_username": self.medic.username},
            format="json",
        )
        self.assertEqual(opened.status_code, 201)

    def test_the_paramedic_can_still_accept_what_the_driver_opened(self):
        shift_id = client_for(self.driver).post(
            "/api/v1/fleet/shifts/open/",
            {"vehicle_callsign": "AMB-PM", "paramedic_username": self.medic.username},
            format="json",
        ).data["id"]

        response = client_for(self.medic).post(f"/api/v1/fleet/shifts/{shift_id}/accept/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "active")

    def test_the_crew_directory_offers_paramedics_not_the_caller(self):
        response = client_for(self.driver).get("/api/v1/fleet/shifts/crew/")
        usernames = {p["username"] for p in response.data["crew"]}
        self.assertIn("pmmedic", usernames)
        self.assertNotIn("pmdriver", usernames)


class ProfileTests(TestCase):
    def setUp(self):
        self.user = make("profileuser", Role.PARAMEDIC)

    def test_a_profile_is_created_on_first_read(self):
        response = client_for(self.user).get("/api/v1/auth/profile/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(StaffProfile.objects.filter(user=self.user).exists())
        self.assertTrue(response.data["is_paramedic"])

    def test_a_user_may_edit_their_own_contact_details(self):
        response = client_for(self.user).patch(
            "/api/v1/auth/profile/", {"phone": "+91 90000 00001"}, format="json"
        )
        self.assertEqual(response.data["phone"], "+91 90000 00001")

    def test_roster_fields_are_not_writable_by_their_holder(self):
        """Staff id and qualification are set by whoever runs the roster."""
        StaffProfile.objects.create(user=self.user, staff_id="PM-0001")
        client_for(self.user).patch(
            "/api/v1/auth/profile/",
            {"staff_id": "PM-9999", "qualification": "Consultant"},
            format="json",
        )
        profile = StaffProfile.objects.get(user=self.user)
        self.assertEqual(profile.staff_id, "PM-0001")
        self.assertEqual(profile.qualification, "")

    def test_the_endpoint_has_no_user_id_so_cannot_target_anyone_else(self):
        other = make("someoneelse", Role.PARAMEDIC)
        StaffProfile.objects.create(user=other, phone="secret")
        response = client_for(self.user).get("/api/v1/auth/profile/")
        self.assertEqual(response.data["username"], "profileuser")
        self.assertNotEqual(response.data["phone"], "secret")

    def test_a_non_image_upload_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = client_for(self.user).post(
            "/api/v1/auth/profile/avatar/",
            {"avatar": SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_anonymous_callers_get_nothing(self):
        self.assertEqual(APIClient().get("/api/v1/auth/profile/").status_code, 401)


class DemoAccountTests(TestCase):
    def setUp(self):
        make("paramedic", Role.PARAMEDIC)
        make("driver", Role.AMBULANCE)

    def test_the_list_is_public_so_the_login_page_can_read_it(self):
        response = self.client.get("/api/v1/auth/demo-accounts/")
        self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=True)
    def test_seeded_accounts_are_listed_with_credentials(self):
        payload = self.client.get("/api/v1/auth/demo-accounts/").json()
        self.assertTrue(payload["available"])
        by_username = {a["username"]: a for a in payload["accounts"]}
        self.assertIn("paramedic", by_username)
        self.assertEqual(by_username["paramedic"]["password"], "sevps-paramedic")
        self.assertTrue(by_username["paramedic"]["is_paramedic"])
        self.assertFalse(by_username["driver"]["is_paramedic"])

    @override_settings(DEBUG=False)
    def test_nothing_is_served_in_production(self):
        """The passwords are published in the repo; a live deployment is not."""
        payload = self.client.get("/api/v1/auth/demo-accounts/").json()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["accounts"], [])
