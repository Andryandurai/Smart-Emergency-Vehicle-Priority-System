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


class VehicleReleaseTests(TestCase):
    """Signing off hands the ambulance back to the next driver.

    The shift used to close and the vehicle stay wherever the last job left
    it - ``at_hospital``, ``returning``. Nothing moved it back, so a crewless
    ambulance was permanently absent from the takeover picker and, in
    practice, retired from the fleet.
    """

    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-REL", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("d_release")
        self.paramedic = crew_member("p_release")

    def _live_shift(self) -> CrewShift:
        shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic,
            status=ShiftStatus.ACTIVE,
        )
        EquipmentCheck.objects.create(shift=shift)
        return shift

    def test_ending_a_shift_returns_the_ambulance_to_the_pool(self):
        shift = self._live_shift()
        self.vehicle.status = VehicleStatus.AT_HOSPITAL
        self.vehicle.save(update_fields=["status"])

        response = client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift.id}/end/")
        self.assertEqual(response.status_code, 200)

        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.status, VehicleStatus.AVAILABLE)

    def test_the_released_ambulance_is_offered_to_the_next_driver(self):
        shift = self._live_shift()
        self.vehicle.status = VehicleStatus.RETURNING
        self.vehicle.save(update_fields=["status"])

        # Nothing to take over while the crew is still signed on.
        picker = client_for(self.driver).get("/api/v1/fleet/shifts/selectable-vehicles/")
        self.assertNotIn(
            self.vehicle.callsign, [v["callsign"] for v in picker.data["vehicles"]]
        )

        client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift.id}/end/")

        picker = client_for(self.driver).get("/api/v1/fleet/shifts/selectable-vehicles/")
        self.assertIn(
            self.vehicle.callsign, [v["callsign"] for v in picker.data["vehicles"]]
        )

    def test_an_ambulance_still_carrying_a_patient_is_not_released(self):
        """A crew signing off does not make a live response go away."""
        from apps.core.enums import TripStage
        from apps.dispatch.models import EmergencyTrip

        shift = self._live_shift()
        EmergencyTrip.objects.create(
            vehicle=self.vehicle, emergency_category="cardiac",
            stage=TripStage.TO_HOSPITAL,
        )
        self.vehicle.status = VehicleStatus.TRANSPORTING
        self.vehicle.save(update_fields=["status"])

        client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift.id}/end/")

        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.status, VehicleStatus.TRANSPORTING)

    def test_an_offline_vehicle_is_not_declared_fit_by_a_shift_ending(self):
        """OFFLINE means the onboard unit is silent - not a state to overrule."""
        shift = self._live_shift()
        self.vehicle.status = VehicleStatus.OFFLINE
        self.vehicle.save(update_fields=["status"])

        client_for(self.driver).post(f"/api/v1/fleet/shifts/{shift.id}/end/")

        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.status, VehicleStatus.OFFLINE)


class RosterStatusTests(TestCase):
    """The crew boards' live status ladder, derived not stored."""

    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-ROST", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("d_roster")
        self.paramedic = User.objects.create_user("p_roster", password="pw")
        group, _ = Group.objects.get_or_create(name=Role.PARAMEDIC)
        self.paramedic.groups.add(group)

    def _roster(self) -> dict:
        response = client_for(self.driver).get("/api/v1/fleet/shifts/roster/")
        self.assertEqual(response.status_code, 200)
        return response.data

    def _driver_row(self) -> dict:
        return next(
            row for row in self._roster()["drivers"]
            if row["username"] == self.driver.username
        )

    def test_a_driver_with_no_shift_is_off_duty(self):
        row = self._driver_row()
        self.assertTrue(row["off_duty"])
        self.assertEqual(row["status"], "Off Duty")

    def test_a_driver_mid_takeover_is_not_filed_as_off_duty(self):
        """Vehicle claimed, inspection running. At work, not on the road."""
        CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, status=ShiftStatus.DRAFT
        )
        row = self._driver_row()
        self.assertFalse(row["off_duty"])
        self.assertFalse(row["on_duty"])
        self.assertEqual(row["status"], "On Duty")

    def test_a_crewed_driver_with_no_job_is_available(self):
        CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic,
            status=ShiftStatus.ACTIVE,
        )
        self.assertEqual(self._driver_row()["status"], "Available")

    def test_the_status_follows_the_trip_stage(self):
        from apps.core.enums import TripStage
        from apps.dispatch.models import EmergencyTrip

        CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic,
            status=ShiftStatus.ACTIVE,
        )
        trip = EmergencyTrip.objects.create(
            vehicle=self.vehicle, emergency_category="cardiac", stage=TripStage.TO_SCENE
        )
        self.assertEqual(self._driver_row()["status"], "On Route")

        trip.stage = TripStage.ON_SCENE
        trip.save(update_fields=["stage"])
        self.assertEqual(self._driver_row()["status"], "On Scene")

        trip.stage = TripStage.TO_HOSPITAL
        trip.save(update_fields=["stage"])
        self.assertEqual(self._driver_row()["status"], "Transporting")

    def test_the_paramedic_on_scene_with_an_assessed_patient_is_treating(self):
        """Same trip, two seats, two jobs - the driver is not treating anyone."""
        from apps.core.enums import TripStage
        from apps.dispatch.models import EmergencyTrip

        CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic,
            status=ShiftStatus.ACTIVE,
        )
        EmergencyTrip.objects.create(
            vehicle=self.vehicle, emergency_category="cardiac", stage=TripStage.ON_SCENE
        )
        roster = self._roster()
        medic = next(
            row for row in roster["paramedics"]
            if row["username"] == self.paramedic.username
        )
        self.assertEqual(medic["status"], "Treating Patient")
        self.assertEqual(self._driver_row()["status"], "On Scene")

    def test_a_driver_who_has_just_signed_off_reads_shift_ended(self):
        shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic,
            status=ShiftStatus.ACTIVE,
        )
        shift.end()
        row = self._driver_row()
        self.assertTrue(row["off_duty"])
        self.assertEqual(row["status"], "Shift Ended")
