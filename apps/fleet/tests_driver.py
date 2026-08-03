"""Driver module: readiness gating, emergency skip, breakdown transfer."""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.enums import (
    BreakdownState,
    MaintenanceState,
    TransferOfferState,
    TripStage,
    VehicleReadiness,
    VehicleStatus,
)
from apps.core.roles import Role
from apps.dispatch.models import EmergencyTrip
from apps.fleet.crew import EQUIPMENT_CATALOGUE, CrewShift, EquipmentCheck
from apps.fleet.maintenance import BreakdownEvent, MaintenanceReport, TransferOffer
from apps.fleet.models import EmergencyVehicle


def crew_member(username: str, *roles) -> User:
    user = User.objects.create_user(username, password="pw")
    for role in roles or (Role.AMBULANCE,):
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def answers(present=True, **overrides) -> dict:
    data = {item["code"]: {"present": present, "note": ""} for item in EQUIPMENT_CATALOGUE}
    data.update(overrides)
    return data


class ReadinessCatalogueTests(TestCase):
    def test_every_item_declares_a_group_and_a_failure_reason(self):
        """A failed item must be able to name the fault it implies."""
        for item in EQUIPMENT_CATALOGUE:
            self.assertIn("group", item, item["code"])
            self.assertIn("failure_reason", item, item["code"])
            self.assertIn("critical", item, item["code"])

    def test_the_three_requested_categories_are_present(self):
        groups = {item["group"] for item in EQUIPMENT_CATALOGUE}
        self.assertEqual(
            groups, {"Vehicle Inspection", "Medical Equipment", "General"}
        )

    def test_general_items_are_not_critical(self):
        """A lapsed photocopy must not leave a cardiac arrest without a vehicle."""
        general = [i for i in EQUIPMENT_CATALOGUE if i["group"] == "General"]
        self.assertTrue(general)
        self.assertTrue(all(not i["critical"] for i in general))


class ReadinessGateTests(TestCase):
    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-RDY", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("d_ready")
        self.paramedic = crew_member("p_ready")
        self.shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic
        )
        self.shift.accept()
        EquipmentCheck.objects.create(shift=self.shift)
        self.url = f"/api/v1/fleet/shifts/{self.shift.id}/checklist/"

    def test_a_complete_pass_marks_the_vehicle_ready(self):
        response = client_for(self.driver).post(self.url, {"items": answers()}, format="json")
        self.assertEqual(response.data["readiness"], VehicleReadiness.READY)
        self.assertTrue(response.data["may_dispatch"])
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.readiness, VehicleReadiness.READY)

    def test_a_failed_critical_item_grounds_the_vehicle(self):
        """The point of the checklist: it prevents a dispatch, not just logs one."""
        items = answers(brakes={"present": False, "note": "spongy pedal"})
        response = client_for(self.driver).post(self.url, {"items": items}, format="json")

        self.assertEqual(response.data["readiness"], VehicleReadiness.NOT_READY)
        self.assertFalse(response.data["may_dispatch"])
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.readiness, VehicleReadiness.NOT_READY)

    def test_a_failed_critical_item_raises_a_maintenance_report(self):
        items = answers(brakes={"present": False, "note": "spongy pedal"})
        response = client_for(self.driver).post(self.url, {"items": items}, format="json")

        report = MaintenanceReport.objects.get(vehicle=self.vehicle)
        self.assertIn("brakes", report.reasons)
        self.assertIn("brakes", report.failed_items)
        self.assertTrue(report.from_inspection)
        self.assertEqual(report.reported_by, self.driver)
        self.assertIn("spongy pedal", report.remarks)
        self.assertIsNotNone(report.created_at)
        self.assertEqual(response.data["maintenance_report"]["id"], report.id)

    def test_a_non_critical_failure_does_not_ground_the_vehicle(self):
        items = answers(cleanliness={"present": False, "note": "cabin needs a wipe"})
        response = client_for(self.driver).post(self.url, {"items": items}, format="json")
        self.assertEqual(response.data["readiness"], VehicleReadiness.READY)
        self.assertTrue(response.data["may_dispatch"])

    def test_correcting_the_answer_updates_one_report_rather_than_stacking_them(self):
        """Three corrections must leave one accurate fault, not three."""
        client = client_for(self.driver)
        client.post(self.url, {"items": answers(brakes={"present": False})}, format="json")
        client.post(
            self.url,
            {"items": {"brakes": {"present": False}, "tyres": {"present": False}}},
            format="json",
        )
        self.assertEqual(MaintenanceReport.objects.filter(vehicle=self.vehicle).count(), 1)
        report = MaintenanceReport.objects.get(vehicle=self.vehicle)
        self.assertEqual(set(report.reasons), {"brakes", "tyres"})

    def test_re_inspection_clears_the_fault_and_returns_the_vehicle(self):
        client = client_for(self.driver)
        client.post(self.url, {"items": answers(brakes={"present": False})}, format="json")
        response = client.post(self.url, {"items": answers()}, format="json")

        self.assertEqual(response.data["readiness"], VehicleReadiness.READY)
        self.assertEqual(
            MaintenanceReport.objects.get(vehicle=self.vehicle).state,
            MaintenanceState.RESOLVED,
        )

    def test_a_grounded_vehicle_is_not_offered_to_dispatch(self):
        client_for(self.driver).post(
            self.url, {"items": answers(engine_status={"present": False})}, format="json"
        )
        self.assertNotIn(
            self.vehicle, list(EmergencyVehicle.objects.dispatchable())
        )
        # Still 'deployable' - it is idle at the station. The two questions
        # are different, and that is the point of separating them.
        self.assertIn(self.vehicle, list(EmergencyVehicle.objects.deployable()))

    def test_a_grounded_vehicle_is_not_selectable_for_takeover(self):
        client_for(self.driver).post(
            self.url, {"items": answers(tyres={"present": False})}, format="json"
        )
        self.assertNotIn(
            self.vehicle, list(EmergencyVehicle.objects.selectable_for_takeover())
        )


class EmergencySkipTests(TestCase):
    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-SKIP", latitude=13.0, longitude=80.0,
            status=VehicleStatus.AVAILABLE,
        )
        self.driver = crew_member("d_skip")
        self.paramedic = crew_member("p_skip")
        self.shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=self.driver, paramedic=self.paramedic
        )
        self.shift.accept()
        EquipmentCheck.objects.create(shift=self.shift)
        self.url = f"/api/v1/fleet/shifts/{self.shift.id}/checklist/skip/"

    def test_skipping_makes_the_vehicle_temporarily_ready_and_dispatchable(self):
        response = client_for(self.driver).post(
            self.url, {"reason": "Cardiac call while boarding"}, format="json"
        )
        self.assertEqual(response.data["readiness"], VehicleReadiness.TEMPORARILY_READY)
        self.assertTrue(response.data["may_dispatch"])
        self.assertIn(self.vehicle, list(EmergencyVehicle.objects.dispatchable()))

    def test_a_skipped_check_is_still_outstanding(self):
        response = client_for(self.driver).post(
            self.url, {"reason": "emergency"}, format="json"
        )
        self.assertTrue(response.data["skipped"])
        self.assertTrue(response.data["is_outstanding"])

    def test_the_skip_is_refused_once_the_vehicle_is_on_a_call(self):
        """It exists for the moment before a dispatch, not as an open deferral."""
        EmergencyTrip.objects.create(
            vehicle=self.vehicle, emergency_category="cardiac",
            stage=TripStage.TO_HOSPITAL,
        )
        response = client_for(self.driver).post(
            self.url, {"reason": "too late"}, format="json"
        )
        self.assertEqual(response.status_code, 409)

    def test_completing_the_check_afterwards_promotes_it_to_ready(self):
        client = client_for(self.driver)
        client.post(self.url, {"reason": "emergency"}, format="json")
        response = client.post(
            f"/api/v1/fleet/shifts/{self.shift.id}/checklist/",
            {"items": answers()}, format="json",
        )
        self.assertEqual(response.data["readiness"], VehicleReadiness.READY)
        self.assertFalse(response.data["skipped"])


class BreakdownTransferTests(TestCase):
    def setUp(self):
        self.broken = EmergencyVehicle.objects.create(
            callsign="AMB-BRK", latitude=13.00, longitude=80.00,
            status=VehicleStatus.TRANSPORTING, readiness=VehicleReadiness.READY,
        )
        self.near = EmergencyVehicle.objects.create(
            callsign="AMB-NEAR", latitude=13.005, longitude=80.005,
            status=VehicleStatus.AVAILABLE, readiness=VehicleReadiness.READY,
        )
        self.also_near = EmergencyVehicle.objects.create(
            callsign="AMB-ALSO", latitude=13.006, longitude=80.006,
            status=VehicleStatus.AVAILABLE, readiness=VehicleReadiness.READY,
        )
        self.grounded = EmergencyVehicle.objects.create(
            callsign="AMB-DEAD", latitude=13.001, longitude=80.001,
            status=VehicleStatus.AVAILABLE, readiness=VehicleReadiness.NOT_READY,
        )

        self.driver = crew_member("d_brk")
        self.rescuer = crew_member("d_near")
        self.rescuer2 = crew_member("d_also")
        medic = crew_member("p_brk")

        for vehicle, driver in (
            (self.broken, self.driver), (self.near, self.rescuer),
            (self.also_near, self.rescuer2),
        ):
            shift = CrewShift.objects.create(
                vehicle=vehicle, driver=driver, paramedic=medic
            )
            shift.accept()

        self.trip = EmergencyTrip.objects.create(
            vehicle=self.broken, emergency_category="cardiac",
            stage=TripStage.TO_HOSPITAL, priority_level=1,
            destination_latitude=13.05, destination_longitude=80.05,
        )

    def _declare(self):
        return client_for(self.driver).post(
            "/api/v1/fleet/breakdowns/declare/",
            {
                "vehicle_callsign": self.broken.callsign,
                "reasons": ["engine"],
                "remarks": "Engine cut out on Mount Road",
            },
            format="json",
        )

    def test_declaring_a_breakdown_offers_it_to_nearby_crews(self):
        response = self._declare()
        self.assertEqual(response.status_code, 201)
        offered = {o["vehicle"] for o in response.data["offers"]}
        self.assertIn("AMB-NEAR", offered)
        self.assertIn("AMB-ALSO", offered)

    def test_a_grounded_ambulance_is_never_offered_a_transfer(self):
        self._declare()
        offered = set(
            TransferOffer.objects.values_list("vehicle__callsign", flat=True)
        )
        self.assertNotIn("AMB-DEAD", offered)

    def test_the_broken_ambulance_is_taken_out_of_service(self):
        self._declare()
        self.broken.refresh_from_db()
        self.assertEqual(self.broken.status, VehicleStatus.OUT_OF_SERVICE)
        self.assertEqual(self.broken.readiness, VehicleReadiness.MAINTENANCE)

    def test_a_breakdown_carries_the_clinical_picture_to_the_offered_crew(self):
        """A crew deciding whether to take over needs to know what they are taking."""
        response = self._declare()
        self.assertEqual(response.data["priority_level"], 1)
        self.assertEqual(response.data["emergency_category"], "cardiac")
        self.assertEqual(response.data["reference"], self.trip.reference)

    def test_a_breakdown_also_raises_a_maintenance_report(self):
        self._declare()
        self.assertTrue(
            MaintenanceReport.objects.filter(vehicle=self.broken).exists()
        )

    def test_declaring_without_a_patient_on_board_is_refused(self):
        """A breakdown is specifically a failure with a patient aboard."""
        self.trip.stage = TripStage.HANDOVER
        self.trip.save(update_fields=["stage"])
        response = self._declare()
        self.assertEqual(response.status_code, 409)

    def test_only_the_signed_on_driver_may_declare(self):
        stranger = crew_member("d_stranger")
        response = client_for(stranger).post(
            "/api/v1/fleet/breakdowns/declare/",
            {"vehicle_callsign": self.broken.callsign, "reasons": ["engine"]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_accepting_moves_the_patient_onto_the_replacement(self):
        breakdown_id = self._declare().data["id"]
        response = client_for(self.rescuer).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/accept/",
            {"vehicle_callsign": self.near.callsign}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], BreakdownState.TRANSFER_ACCEPTED)

        self.trip.refresh_from_db()
        self.assertEqual(self.trip.vehicle_id, self.near.id)
        # The reference survives - the clinical record and the hospital's
        # notification both hang off it.
        self.assertEqual(self.trip.reference, EmergencyTrip.objects.get(pk=self.trip.pk).reference)

    def test_only_one_crew_can_win_the_transfer(self):
        """Two crews pressing Accept in the same second: exactly one wins."""
        breakdown_id = self._declare().data["id"]
        first = client_for(self.rescuer).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/accept/",
            {"vehicle_callsign": self.near.callsign}, format="json",
        )
        second = client_for(self.rescuer2).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/accept/",
            {"vehicle_callsign": self.also_near.callsign}, format="json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

        breakdown = BreakdownEvent.objects.get(pk=breakdown_id)
        self.assertEqual(breakdown.replacement_vehicle_id, self.near.id)

    def test_losing_crews_have_their_offers_withdrawn_not_left_hanging(self):
        breakdown_id = self._declare().data["id"]
        client_for(self.rescuer).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/accept/",
            {"vehicle_callsign": self.near.callsign}, format="json",
        )
        other = TransferOffer.objects.get(
            breakdown_id=breakdown_id, vehicle=self.also_near
        )
        self.assertEqual(other.state, TransferOfferState.WITHDRAWN)

    def test_a_vehicle_that_was_not_offered_cannot_accept(self):
        breakdown_id = self._declare().data["id"]
        outsider = EmergencyVehicle.objects.create(
            callsign="AMB-FAR", latitude=14.5, longitude=81.5,
            status=VehicleStatus.AVAILABLE, readiness=VehicleReadiness.READY,
        )
        response = client_for(self.rescuer).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/accept/",
            {"vehicle_callsign": outsider.callsign}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_rejecting_records_who_declined_and_why(self):
        """'Nobody came' and 'nobody was asked' are different findings."""
        breakdown_id = self._declare().data["id"]
        response = client_for(self.rescuer).post(
            f"/api/v1/fleet/breakdowns/{breakdown_id}/reject/",
            {"vehicle_callsign": self.near.callsign, "reason": "Already on a call"},
            format="json",
        )
        self.assertEqual(response.data["state"], TransferOfferState.REJECTED)
        offer = TransferOffer.objects.get(breakdown_id=breakdown_id, vehicle=self.near)
        self.assertEqual(offer.responded_by, self.rescuer)
        self.assertIn("Already on a call", offer.reject_reason)

    def test_offers_endpoint_shows_a_crew_what_is_waiting_for_them(self):
        self._declare()
        response = client_for(self.rescuer).get("/api/v1/fleet/breakdowns/offers/")
        self.assertEqual(len(response.data["offers"]), 1)
        self.assertEqual(response.data["offers"][0]["breakdown"]["vehicle"], "AMB-BRK")


class FleetBoardTests(TestCase):
    def setUp(self):
        self.admin = crew_member("boardadmin", Role.ADMIN)
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-BOARD", registration="TN 01 XX 0001",
            latitude=13.0, longitude=80.0, status=VehicleStatus.AVAILABLE,
        )
        driver = crew_member("d_board")
        medic = crew_member("p_board")
        self.shift = CrewShift.objects.create(
            vehicle=self.vehicle, driver=driver, paramedic=medic
        )
        self.shift.accept()
        EquipmentCheck.objects.create(shift=self.shift)

    def test_the_board_reports_every_column_the_dashboard_renders(self):
        response = client_for(self.admin).get("/api/v1/fleet/board/")
        row = next(r for r in response.data["vehicles"] if r["callsign"] == "AMB-BOARD")
        for field in (
            "registration", "latitude", "longitude", "driver_name", "paramedic_name",
            "status_display", "readiness_display", "inspection_status",
            "current_emergency", "shift_status_display", "updated_at",
        ):
            self.assertIn(field, row)

    def test_the_board_names_the_crew_on_duty(self):
        response = client_for(self.admin).get("/api/v1/fleet/board/")
        row = next(r for r in response.data["vehicles"] if r["callsign"] == "AMB-BOARD")
        self.assertEqual(row["driver_name"], "d_board")
        self.assertEqual(row["paramedic_name"], "p_board")
        self.assertEqual(row["shift_status"], "active")

    def test_the_summary_counts_vehicles_owing_an_inspection(self):
        response = client_for(self.admin).get("/api/v1/fleet/board/")
        self.assertGreaterEqual(response.data["summary"]["inspection_pending"], 1)

    def test_an_administrator_can_override_a_grounded_vehicle(self):
        self.vehicle.readiness = VehicleReadiness.NOT_READY
        self.vehicle.save(update_fields=["readiness"])

        response = client_for(self.admin).post(
            f"/api/v1/fleet/vehicles/{self.vehicle.callsign}/readiness/",
            {"readiness": VehicleReadiness.READY, "note": "Fault cleared at roadside"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.readiness, VehicleReadiness.READY)

    def test_a_driver_cannot_override_readiness(self):
        driver = crew_member("d_nope")
        response = client_for(driver).post(
            f"/api/v1/fleet/vehicles/{self.vehicle.callsign}/readiness/",
            {"readiness": VehicleReadiness.READY}, format="json",
        )
        self.assertEqual(response.status_code, 403)
