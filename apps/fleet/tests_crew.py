"""Crew takeover, pairing, and the start-of-shift vehicle check."""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.enums import ShiftStatus, VehicleStatus
from apps.core.roles import Role
from apps.fleet.crew import EQUIPMENT_CATALOGUE, CrewShift, EquipmentCheck
from apps.fleet.models import EmergencyVehicle


def crew_member(username: str) -> User:
    user = User.objects.create_user(username, password="pw")
    group, _ = Group.objects.get_or_create(name=Role.AMBULANCE)
    user.groups.add(group)
    return user


def client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


class TakeoverTests(TestCase):
    """The driver opens, the paramedic accepts. Neither alone is a shift."""

    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-TEST", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("driver1")
        self.paramedic = crew_member("medic1")
        self.other = crew_member("medic2")

    def _open(self, client=None, paramedic=None):
        return (client or client_for(self.driver)).post(
            "/api/v1/fleet/shifts/open/",
            {
                "vehicle_callsign": self.vehicle.callsign,
                "paramedic_username": (paramedic or self.paramedic).username,
            },
            format="json",
        )

    def test_a_new_takeover_is_pending_not_live(self):
        response = self._open()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], ShiftStatus.PENDING)
        self.assertFalse(CrewShift.objects.get().is_live)

    def test_the_named_paramedic_can_accept_and_the_shift_goes_live(self):
        shift_id = self._open().data["id"]
        response = client_for(self.paramedic).post(f"/api/v1/fleet/shifts/{shift_id}/accept/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ShiftStatus.ACTIVE)

        shift = CrewShift.objects.get()
        self.assertTrue(shift.is_live)
        self.assertIsNotNone(shift.accepted_at)

    def test_someone_else_cannot_accept_on_the_paramedics_behalf(self):
        """The whole point of the handshake is that it cannot be faked."""
        shift_id = self._open().data["id"]
        response = client_for(self.other).post(f"/api/v1/fleet/shifts/{shift_id}/accept/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CrewShift.objects.get().status, ShiftStatus.PENDING)

    def test_the_driver_cannot_accept_their_own_request(self):
        shift_id = self._open().data["id"]
        response = client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift_id}/accept/")
        self.assertEqual(response.status_code, 403)

    def test_a_driver_cannot_crew_with_themselves(self):
        response = self._open(paramedic=self.driver)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CrewShift.objects.exists())

    def test_a_second_takeover_on_the_same_vehicle_is_refused(self):
        """'Who is on AMB-101 right now' must have exactly one answer."""
        self._open()
        response = self._open(client=client_for(self.other), paramedic=self.paramedic)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CrewShift.objects.count(), 1)

    def test_the_vehicle_is_free_again_once_the_shift_ends(self):
        shift_id = self._open().data["id"]
        client_for(self.paramedic).post(f"/api/v1/fleet/shifts/{shift_id}/accept/")
        client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift_id}/end/")

        response = self._open(client=client_for(self.other), paramedic=self.paramedic)
        self.assertEqual(response.status_code, 201)

    def test_a_declined_takeover_frees_the_vehicle_too(self):
        shift_id = self._open().data["id"]
        declined = client_for(self.paramedic).post(
            f"/api/v1/fleet/shifts/{shift_id}/decline/",
            {"reason": "Not my vehicle today"}, format="json",
        )
        self.assertEqual(declined.status_code, 200)
        self.assertEqual(declined.data["status"], ShiftStatus.DECLINED)
        self.assertEqual(self._open().status_code, 201)

    def test_mine_shows_the_paramedic_what_is_waiting_for_them(self):
        self._open()
        response = client_for(self.paramedic).get("/api/v1/fleet/shifts/mine/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["awaiting_my_acceptance"]), 1)

    def test_the_crew_directory_only_lists_ambulance_role_users(self):
        outsider = User.objects.create_user("clerk", password="pw")
        group, _ = Group.objects.get_or_create(name=Role.HOSPITAL)
        outsider.groups.add(group)

        response = client_for(self.driver).get("/api/v1/fleet/shifts/crew/")
        usernames = {person["username"] for person in response.data["crew"]}
        self.assertIn("medic1", usernames)
        self.assertNotIn("clerk", usernames)


class EquipmentCheckTests(TestCase):
    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-CHECK", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("driver2")
        self.paramedic = crew_member("medic3")
        self.shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic
        )
        self.check = EquipmentCheck.objects.create(shift=self.shift)
        self.url = f"/api/v1/fleet/shifts/{self.shift.id}/checklist/"

    def _answer_all(self, present=True):
        return {
            item["code"]: {"present": present, "note": ""} for item in EQUIPMENT_CATALOGUE
        }

    def test_a_fresh_check_is_outstanding(self):
        self.assertTrue(self.check.is_outstanding)
        self.assertFalse(self.check.is_complete)

    def test_partial_answers_are_merged_not_replaced(self):
        """A crew ticking items as they walk the vehicle must not lose them."""
        client = client_for(self.driver)
        client.post(self.url, {"items": {"defibrillator": {"present": True}}}, format="json")
        response = client.post(
            self.url, {"items": {"oxygen_cylinder": {"present": True}}}, format="json"
        )
        self.assertEqual(response.data["answered"], 2)

    def test_answering_every_item_completes_the_check(self):
        response = client_for(self.driver).post(
            self.url, {"items": self._answer_all()}, format="json"
        )
        self.assertTrue(response.data["is_complete"])
        self.assertFalse(response.data["is_outstanding"])
        self.assertIsNotNone(response.data["completed_at"])

    def test_missing_critical_equipment_is_called_out_separately(self):
        items = self._answer_all()
        items["defibrillator"] = {"present": False, "note": "sent for service"}
        items["thermometer"] = {"present": False, "note": ""}

        response = client_for(self.driver).post(self.url, {"items": items}, format="json")
        self.assertIn("defibrillator", response.data["missing_critical"])
        self.assertNotIn("thermometer", response.data["missing_critical"])
        self.assertIn("thermometer", response.data["missing"])

    def test_emergency_skip_records_a_reason_and_stays_outstanding(self):
        """A skip is a deferral, not a waiver - that distinction is the point."""
        response = client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{self.shift.id}/checklist/skip/",
            {"reason": "Cardiac call received while boarding"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["skipped"])
        self.assertTrue(response.data["is_outstanding"])
        self.assertIsNotNone(response.data["skipped_at"])
        self.assertIn("Cardiac call", response.data["skip_reason"])

    def test_a_skip_requires_a_reason(self):
        response = client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{self.shift.id}/checklist/skip/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_completing_later_clears_the_skip(self):
        client = client_for(self.paramedic)
        client.post(
            f"/api/v1/fleet/shifts/{self.shift.id}/checklist/skip/",
            {"reason": "emergency"}, format="json",
        )
        response = client.post(self.url, {"items": self._answer_all()}, format="json")
        self.assertFalse(response.data["skipped"])
        self.assertFalse(response.data["is_outstanding"])

    def test_a_stranger_cannot_fill_in_someone_elses_vehicle_check(self):
        stranger = crew_member("medic4")
        response = client_for(stranger).post(
            self.url, {"items": {"defibrillator": {"present": True}}}, format="json"
        )
        self.assertEqual(response.status_code, 403)


class ThreeStepTakeoverTests(TestCase):
    """Claim the vehicle, inspect it, *then* call a paramedic.

    The ordering is the point. A paramedic summoned to an ambulance that
    turns out to have failed brakes is the one person the driver most needs
    still available, so nobody is called until the vehicle is known to be fit.
    """

    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-3STEP", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("d_3step")
        self.paramedic = crew_member("p_3step")

    def _claim(self, client=None):
        return (client or client_for(self.driver)).post(
            "/api/v1/fleet/shifts/claim/",
            {"vehicle_callsign": self.vehicle.callsign}, format="json",
        )

    def _answer(self, shift_id, **overrides):
        items = {i["code"]: {"present": True, "note": ""} for i in EQUIPMENT_CATALOGUE}
        items.update(overrides)
        return client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/checklist/", {"items": items}, format="json"
        )

    def test_claiming_a_vehicle_names_nobody_yet(self):
        response = self._claim()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], ShiftStatus.DRAFT)
        self.assertIsNone(response.data["paramedic"])

    def test_claiming_reserves_the_vehicle_against_a_second_driver(self):
        self._claim()
        other = crew_member("d_3step2")
        self.assertEqual(self._claim(client_for(other)).status_code, 409)

    def test_a_claimed_vehicle_creates_its_checklist(self):
        shift_id = self._claim().data["id"]
        self.assertTrue(EquipmentCheck.objects.filter(shift_id=shift_id).exists())

    def test_requesting_a_paramedic_moves_the_shift_to_pending(self):
        shift_id = self._claim().data["id"]
        self._answer(shift_id)
        response = client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/request-paramedic/",
            {"paramedic_username": self.paramedic.username}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ShiftStatus.PENDING)
        self.assertEqual(response.data["paramedic_detail"]["username"], "p_3step")

    def test_a_grounded_vehicle_cannot_have_a_paramedic_called_to_it(self):
        shift_id = self._claim().data["id"]
        self._answer(shift_id, brakes={"present": False, "note": "spongy"})

        response = client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/request-paramedic/",
            {"paramedic_username": self.paramedic.username}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("brakes", response.data["missing_critical"])
        self.assertEqual(CrewShift.objects.get(pk=shift_id).status, ShiftStatus.DRAFT)

    def test_an_emergency_skip_still_lets_the_driver_call_a_paramedic(self):
        """The skip exists so the ambulance can roll; it must not block crewing."""
        shift_id = self._claim().data["id"]
        client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/checklist/skip/",
            {"reason": "cardiac call while boarding"}, format="json",
        )
        response = client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/request-paramedic/",
            {"paramedic_username": self.paramedic.username}, format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_only_the_claiming_driver_may_send_the_request(self):
        shift_id = self._claim().data["id"]
        stranger = crew_member("d_3step3")
        response = client_for(stranger).post(
            f"/api/v1/fleet/shifts/{shift_id}/request-paramedic/",
            {"paramedic_username": self.paramedic.username}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_paramedic_cannot_claim_a_vehicle(self):
        medic = User.objects.create_user("pm_only", password="pw")
        group, _ = Group.objects.get_or_create(name=Role.PARAMEDIC)
        medic.groups.add(group)
        self.assertEqual(self._claim(client_for(medic)).status_code, 403)

    def test_the_full_three_steps_end_with_a_live_shift(self):
        shift_id = self._claim().data["id"]
        self._answer(shift_id)
        client_for(self.driver).post(
            f"/api/v1/fleet/shifts/{shift_id}/request-paramedic/",
            {"paramedic_username": self.paramedic.username}, format="json",
        )
        accepted = client_for(self.paramedic).post(
            f"/api/v1/fleet/shifts/{shift_id}/accept/"
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.data["status"], ShiftStatus.ACTIVE)
