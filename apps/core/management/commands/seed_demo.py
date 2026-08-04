"""Build a complete, runnable SEVPS deployment for a city.

Generates a routable road grid with arterial/secondary/residential hierarchy,
signalised junctions, camera coverage, a hospital network with realistic
capability mixes, an ambulance fleet, roadside display boards and an accident
archive - everything the six layers need to actually run.

The synthetic network is deliberately *irregular*: pure grids make routing
look better than it is because every detour is equivalent.  Streets here are
jittered, a few links are one-way, and speeds differ by road class, so route
choice is a real decision.

    python manage.py seed_demo
    python manage.py seed_demo --reset --grid 14
"""
from __future__ import annotations

import math
import random

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.alerts.models import DisplayBoard
from apps.core.enums import (
    AlertChannel,
    HospitalFacility as HF,
    RoadClass,
    VehicleOwnership,
    VehicleStatus,
    VehicleType,
)
from apps.fleet.models import EmergencyVehicle, Station
from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
from apps.hospitals.rules import seed_rules
from apps.network.models import (
    AccidentRecord,
    CameraFeed,
    Intersection,
    RoadSegment,
    TrafficSignal,
)

# Chennai city centre.
CENTRE_LAT, CENTRE_LON = 13.0604, 80.2496

ARTERIAL_NAMES = [
    "Anna Salai", "EVR Periyar Salai", "Poonamallee High Road", "GST Road",
    "OMR - Rajiv Gandhi Salai", "ECR - East Coast Road", "Sardar Patel Road",
    "Nungambakkam High Road", "Mount Road", "Jawaharlal Nehru Road",
]
CROSS_NAMES = [
    "Cathedral Road", "TTK Road", "Kamarajar Salai", "Santhome High Road",
    "Dr Radhakrishnan Salai", "Harrington Road", "Bazullah Road",
    "Sterling Road", "Casa Major Road", "Pantheon Road", "Greams Road",
    "College Road",
]

HOSPITALS = [
    {
        "code": "RGGGH", "name": "Rajiv Gandhi Government General Hospital",
        "offset": (0.008, -0.010), "trauma": True, "quality": 0.86,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.TRAUMA_CENTER, HF.CARDIAC_ICU, HF.CATH_LAB,
            HF.NEUROLOGY, HF.CT_SCAN, HF.MRI, HF.ICU, HF.OPERATION_THEATRE,
            HF.BLOOD_BANK, HF.VENTILATOR, HF.TOXICOLOGY, HF.BURN_UNIT,
            HF.STROKE_UNIT, HF.OBSTETRICS, HF.PICU, HF.NICU, HF.DIALYSIS,
        ],
        "capacity": (60, 26, 30, 12, 14, 6, 18, 9),
    },
    {
        "code": "APOLLO", "name": "Apollo Hospitals Greams Road",
        "offset": (0.002, 0.004), "trauma": True, "quality": 0.94,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.CARDIAC_ICU, HF.CATH_LAB, HF.NEUROLOGY,
            HF.CT_SCAN, HF.MRI, HF.ICU, HF.OPERATION_THEATRE, HF.BLOOD_BANK,
            HF.VENTILATOR, HF.TRAUMA_CENTER, HF.STROKE_UNIT, HF.DIALYSIS,
        ],
        "capacity": (34, 11, 22, 6, 10, 4, 9, 7),
    },
    {
        "code": "KAUVERY", "name": "Kauvery Hospital Alwarpet",
        "offset": (-0.018, 0.002), "trauma": False, "quality": 0.88,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.CARDIAC_ICU, HF.CATH_LAB, HF.ICU,
            HF.OPERATION_THEATRE, HF.CT_SCAN, HF.VENTILATOR, HF.BLOOD_BANK,
        ],
        "capacity": (22, 8, 14, 4, 6, 3, 6, 5),
    },
    {
        "code": "STANLEY", "name": "Government Stanley Medical College Hospital",
        "offset": (0.021, -0.006), "trauma": True, "quality": 0.80,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.TRAUMA_CENTER, HF.BURN_UNIT, HF.ICU,
            HF.OPERATION_THEATRE, HF.BLOOD_BANK, HF.CT_SCAN, HF.VENTILATOR,
            HF.TOXICOLOGY, HF.OBSTETRICS,
        ],
        "capacity": (44, 18, 24, 9, 11, 5, 13, 7),
    },
    {
        "code": "MIOT", "name": "MIOT International Manapakkam",
        "offset": (-0.030, -0.055), "trauma": True, "quality": 0.91,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.TRAUMA_CENTER, HF.CARDIAC_ICU, HF.CATH_LAB,
            HF.NEUROLOGY, HF.CT_SCAN, HF.MRI, HF.ICU, HF.OPERATION_THEATRE,
            HF.BLOOD_BANK, HF.VENTILATOR, HF.DIALYSIS, HF.STROKE_UNIT,
        ],
        "capacity": (30, 13, 20, 7, 9, 4, 7, 6),
    },
    {
        "code": "SRMC", "name": "Sri Ramachandra Medical Centre Porur",
        "offset": (-0.008, -0.078), "trauma": False, "quality": 0.87,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.NEUROLOGY, HF.CT_SCAN, HF.MRI, HF.ICU,
            HF.OPERATION_THEATRE, HF.BLOOD_BANK, HF.VENTILATOR, HF.NICU,
            HF.PICU, HF.OBSTETRICS, HF.STROKE_UNIT, HF.DIALYSIS,
        ],
        "capacity": (28, 12, 18, 6, 8, 4, 8, 6),
    },
    {
        "code": "EGA", "name": "Egmore Children's Hospital",
        "offset": (0.011, 0.001), "trauma": False, "quality": 0.83,
        "facilities": [HF.EMERGENCY_DEPT, HF.PICU, HF.NICU, HF.VENTILATOR, HF.OPERATION_THEATRE],
        "capacity": (20, 9, 12, 5, 6, 2, 5, 4),
    },
    {
        "code": "GKNM", "name": "Global Health City Perumbakkam",
        "offset": (-0.062, 0.012), "trauma": True, "quality": 0.90,
        "facilities": [
            HF.EMERGENCY_DEPT, HF.TRAUMA_CENTER, HF.CARDIAC_ICU, HF.CATH_LAB,
            HF.ICU, HF.OPERATION_THEATRE, HF.CT_SCAN, HF.VENTILATOR,
            HF.TOXICOLOGY, HF.BLOOD_BANK,
        ],
        "capacity": (26, 10, 16, 6, 7, 3, 6, 5),
    },
]

# (callsign, type, operator, is_als, ownership, registration)
# Registrations are fixed rather than random so a demo screenshot, a bug report
# and a test all name the same vehicle.
#
# Ten of them, because this is the pool a driver picks from at the start of a
# shift and a picker with two entries does not demonstrate a choice. The three
# demonstration units below are deliberately *not* in this list.
AMBULANCES = [
    ("AMB-101", VehicleType.AMBULANCE, "108 Emergency Services", True,
     VehicleOwnership.GOVERNMENT, "TN 01 AE 4501"),
    ("AMB-102", VehicleType.AMBULANCE, "108 Emergency Services", True,
     VehicleOwnership.GOVERNMENT, "TN 01 AE 4502"),
    ("AMB-103", VehicleType.AMBULANCE, "108 Emergency Services", False,
     VehicleOwnership.GOVERNMENT, "TN 01 AE 4503"),
    ("AMB-104", VehicleType.AMBULANCE, "Apollo Hospitals", True,
     VehicleOwnership.PRIVATE_HOSPITAL, "TN 07 BK 8811"),
    ("AMB-105", VehicleType.AMBULANCE, "Kauvery Hospital", False,
     VehicleOwnership.PRIVATE_HOSPITAL, "TN 07 BK 8812"),
    ("AMB-106", VehicleType.AMBULANCE, "GVK EMRI", True,
     VehicleOwnership.PRIVATE_SERVICE, "TN 22 CM 3390"),
    ("AMB-107", VehicleType.AMBULANCE, "108 Emergency Services", False,
     VehicleOwnership.GOVERNMENT, "TN 01 AE 4504"),
    ("AMB-108", VehicleType.AMBULANCE, "Rela Institute", True,
     VehicleOwnership.PRIVATE_HOSPITAL, "TN 07 BK 8813"),
    ("AMB-109", VehicleType.AMBULANCE, "Red Cross Society", False,
     VehicleOwnership.NGO, "TN 09 RC 1201"),
    ("AMB-110", VehicleType.AMBULANCE, "MIOT International", True,
     VehicleOwnership.PRIVATE_HOSPITAL, "TN 07 BK 8814"),
]

#: Permanently-running demonstration units.
#:
#: Kept on a rolling synthetic response by `apps.dispatch.journey` so that
#: anyone opening the platform sees ambulances actually moving, corridors
#: arming and ETAs counting down without first having to start a simulation.
#: Numbered in a separate 2xx series and excluded from the takeover picker, so
#: nobody mistakes one for a vehicle they can sign on to.
DEMO_AMBULANCES = [
    ("AMB-201", VehicleType.AMBULANCE, "SEVPS Demonstration", True,
     VehicleOwnership.GOVERNMENT, "TN 01 DM 2001"),
    ("AMB-202", VehicleType.AMBULANCE, "SEVPS Demonstration", False,
     VehicleOwnership.PRIVATE_SERVICE, "TN 01 DM 2002"),
    ("AMB-203", VehicleType.AMBULANCE, "SEVPS Demonstration", True,
     VehicleOwnership.PRIVATE_HOSPITAL, "TN 01 DM 2003"),
]


class Command(BaseCommand):
    help = "Seed a complete demonstration deployment (network, hospitals, fleet)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing data first.")
        parser.add_argument("--grid", type=int, default=12, help="Grid size (N x N nodes).")
        parser.add_argument("--spacing", type=float, default=420.0, help="Metres between nodes.")
        parser.add_argument("--seed", type=int, default=20260802, help="RNG seed.")
        parser.add_argument("--accidents", type=int, default=260, help="Historical accidents.")
        parser.add_argument("--city", type=str, default="Chennai")

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        city = options["city"]

        if options["reset"]:
            self._reset()

        created, updated = seed_rules()
        self.stdout.write(self.style.SUCCESS(f"Rule base: {created} created, {updated} updated"))

        nodes = self._build_nodes(rng, options["grid"], options["spacing"], city)
        segments = self._build_segments(rng, nodes, options["grid"])
        signals = self._build_signals(rng, nodes, options["grid"])
        cameras = self._build_cameras(rng, segments)
        boards = self._build_boards(rng, segments)
        hospitals = self._build_hospitals(rng)
        stations, vehicles = self._build_fleet(rng, nodes)
        accidents = self._build_accidents(rng, nodes, options["accidents"])
        self._seed_live_speeds(rng, segments)

        from apps.brain import graph as graph_mod

        graph_mod.invalidate()

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    [
                        "",
                        "SEVPS demonstration data ready:",
                        f"  intersections     {len(nodes)}",
                        f"  road segments     {len(segments)} (directed)",
                        f"  traffic signals   {len(signals)}",
                        f"  cameras           {len(cameras)}",
                        f"  display boards    {len(boards)}",
                        f"  hospitals         {len(hospitals)}",
                        f"  stations          {len(stations)}",
                        f"  vehicles          {len(vehicles)}",
                        f"  accident records  {accidents}",
                        "",
                        "Next:  python manage.py simulate --trips 3",
                        "Then open http://127.0.0.1:8000/",
                    ]
                )
            )
        )

    # -- reset --------------------------------------------------------------
    def _reset(self):
        from apps.analytics.models import DailyMetric, Hotspot
        from apps.alerts.models import DriverAlert, DriverDevice
        from apps.dispatch.models import EmergencyTrip
        from apps.fleet.models import VehicleTelemetry
        from apps.hospitals.models import HospitalAlert, HospitalRecommendationLog
        from apps.network.models import RoadEvent, TrafficObservation, TrafficProfile

        for model in (
            DriverAlert, DriverDevice, HospitalAlert, HospitalRecommendationLog,
            VehicleTelemetry, EmergencyTrip, TrafficObservation, TrafficProfile,
            RoadEvent, AccidentRecord, CameraFeed, DisplayBoard, TrafficSignal,
            RoadSegment, Intersection, HospitalCapability, HospitalCapacity,
            Hospital, EmergencyVehicle, Station, DailyMetric, Hotspot,
        ):
            deleted, _ = model.objects.all().delete()
            if deleted:
                self.stdout.write(f"  cleared {deleted:>6} {model.__name__}")

    # -- network ------------------------------------------------------------
    def _build_nodes(self, rng, size, spacing_m, city) -> dict:
        dlat = spacing_m / 111_320.0
        dlon = spacing_m / (111_320.0 * math.cos(math.radians(CENTRE_LAT)))
        half = size // 2

        nodes = {}
        rows = []
        for i in range(size):
            for j in range(size):
                # Jitter breaks the perfect grid so route choice is non-trivial.
                lat = CENTRE_LAT + (i - half) * dlat + rng.uniform(-0.22, 0.22) * dlat
                lon = CENTRE_LON + (j - half) * dlon + rng.uniform(-0.22, 0.22) * dlon
                is_major = (i % 3 == 0) or (j % 3 == 0)
                name = (
                    f"{ARTERIAL_NAMES[i % len(ARTERIAL_NAMES)]} / {CROSS_NAMES[j % len(CROSS_NAMES)]}"
                    if is_major
                    else f"Junction {i}-{j}"
                )
                rows.append(
                    Intersection(
                        latitude=round(lat, 6),
                        longitude=round(lon, 6),
                        name=name,
                        city=city,
                        is_signalised=is_major and rng.random() < 0.75,
                        # Signalised junctions cost time even without preemption.
                        base_delay_s=round(rng.uniform(14, 34), 1) if is_major else round(rng.uniform(2, 7), 1),
                    )
                )
        Intersection.objects.bulk_create(rows, batch_size=500)

        stored = list(Intersection.objects.order_by("id"))
        index = 0
        for i in range(size):
            for j in range(size):
                nodes[(i, j)] = stored[index]
                index += 1
        return nodes

    def _build_segments(self, rng, nodes, size) -> list:
        def classify(i, j, horizontal):
            axis = i if horizontal else j
            if axis % 6 == 0:
                return RoadClass.PRIMARY, rng.randint(3, 4)
            if axis % 3 == 0:
                return RoadClass.SECONDARY, rng.randint(2, 3)
            return RoadClass.RESIDENTIAL, 2

        from apps.core.enums import FREE_FLOW_KMH
        from apps.core.geo import haversine_m

        segments = []
        for (i, j), node in nodes.items():
            for di, dj, horizontal in ((0, 1, True), (1, 0, False)):
                neighbour = nodes.get((i + di, j + dj))
                if neighbour is None:
                    continue

                road_class, lanes = classify(i, j, horizontal)
                free_flow = FREE_FLOW_KMH[road_class] * rng.uniform(0.9, 1.1)
                name = (
                    ARTERIAL_NAMES[i % len(ARTERIAL_NAMES)]
                    if horizontal
                    else CROSS_NAMES[j % len(CROSS_NAMES)]
                )
                length = max(40.0, haversine_m(node.latitude, node.longitude, neighbour.latitude, neighbour.longitude))

                # ~8% of residential links are one-way, as in a real city.
                one_way = road_class == RoadClass.RESIDENTIAL and rng.random() < 0.08
                pairs = [(node, neighbour)] if one_way else [(node, neighbour), (neighbour, node)]

                for a, b in pairs:
                    mid_lat = (a.latitude + b.latitude) / 2
                    mid_lon = (a.longitude + b.longitude) / 2
                    segments.append(
                        RoadSegment(
                            from_node=a,
                            to_node=b,
                            name=name,
                            road_class=road_class,
                            length_m=round(length, 1),
                            lanes=lanes,
                            free_flow_kmh=round(free_flow, 1),
                            geometry=[[a.latitude, a.longitude], [b.latitude, b.longitude]],
                            latitude=round(mid_lat, 6),
                            longitude=round(mid_lon, 6),
                            allows_contraflow=road_class in {RoadClass.PRIMARY, RoadClass.SECONDARY},
                        )
                    )
        RoadSegment.objects.bulk_create(segments, batch_size=800)
        return segments

    def _build_signals(self, rng, nodes, size) -> list:
        signalised = [n for n in nodes.values() if n.is_signalised]
        signals = [
            TrafficSignal(
                intersection=node,
                controller_id=f"TSC-{node.id:05d}",
                controller_type="simulated",
                cycle_seconds=rng.choice([90, 110, 120, 150]),
                phase_plan={
                    "north_south": {"green": rng.randint(28, 46), "amber": 3},
                    "east_west": {"green": rng.randint(28, 46), "amber": 3},
                },
                supports_preemption=rng.random() < 0.92,
                min_recovery_s=rng.choice([30, 45, 60]),
                is_online=True,
                last_heartbeat=timezone.now(),
            )
            for node in signalised
        ]
        TrafficSignal.objects.bulk_create(signals, batch_size=500)
        return signals

    def _build_cameras(self, rng, segments) -> list:
        stored = list(RoadSegment.objects.exclude(road_class=RoadClass.RESIDENTIAL))
        chosen = rng.sample(stored, k=min(len(stored), max(12, len(stored) // 8)))
        cameras = [
            CameraFeed(
                name=f"CAM-{index + 1:03d} {segment.name}",
                segment=segment,
                intersection=segment.to_node,
                latitude=segment.latitude,
                longitude=segment.longitude,
                heading_deg=round(rng.uniform(0, 360), 1),
                stream_url="",
                is_active=True,
            )
            for index, segment in enumerate(chosen)
        ]
        CameraFeed.objects.bulk_create(cameras, batch_size=200)
        return cameras

    def _build_boards(self, rng, segments) -> list:
        stored = list(RoadSegment.objects.filter(road_class__in=[RoadClass.PRIMARY, RoadClass.SECONDARY]))
        chosen = rng.sample(stored, k=min(len(stored), 22))
        boards = [
            DisplayBoard(
                code=f"VMS-{index + 1:03d}",
                name=f"{segment.name} approach",
                channel=AlertChannel.VMS_BOARD if index % 3 else AlertChannel.CITY_DISPLAY,
                segment=segment,
                latitude=segment.latitude,
                longitude=segment.longitude,
                facing_deg=round(rng.uniform(0, 360), 1),
                is_active=True,
            )
            for index, segment in enumerate(chosen)
        ]
        DisplayBoard.objects.bulk_create(boards, batch_size=100)
        return boards

    # -- hospitals ----------------------------------------------------------
    def _build_hospitals(self, rng) -> list:
        created = []
        for spec in HOSPITALS:
            hospital, _ = Hospital.objects.update_or_create(
                code=spec["code"],
                defaults={
                    "name": spec["name"],
                    "city": "Chennai",
                    "latitude": round(CENTRE_LAT + spec["offset"][0], 6),
                    "longitude": round(CENTRE_LON + spec["offset"][1], 6),
                    "is_trauma_designated": spec["trauma"],
                    "quality_index": spec["quality"],
                    "emergency_phone": f"044-2{rng.randint(1000000, 9999999)}",
                    "address": "Chennai, Tamil Nadu",
                    "is_active": True,
                },
            )
            HospitalCapability.objects.filter(hospital=hospital).delete()
            HospitalCapability.objects.bulk_create(
                [
                    HospitalCapability(
                        hospital=hospital,
                        facility=facility,
                        is_available=True,
                        units=rng.randint(1, 3),
                    )
                    for facility in spec["facilities"]
                ]
            )
            ed_total, ed_free, icu_total, icu_free, vents, ots, waiting, doctors = spec["capacity"]
            HospitalCapacity.objects.update_or_create(
                hospital=hospital,
                defaults={
                    "emergency_beds_total": ed_total,
                    "emergency_beds_available": ed_free,
                    "icu_beds_total": icu_total,
                    "icu_beds_available": icu_free,
                    "ventilators_available": vents,
                    "operation_theatres_free": ots,
                    "patients_waiting": waiting,
                    "doctors_on_duty": doctors,
                    "reported_at": timezone.now(),
                },
            )
            created.append(hospital)
        return created

    # -- fleet --------------------------------------------------------------
    def _build_fleet(self, rng, nodes) -> tuple[list, list]:
        node_list = list(nodes.values())
        stations = []
        for index, (name, code) in enumerate(
            [
                ("Chennai Central Ambulance Base", "STN-CEN"),
                ("Kilpauk Emergency Station", "STN-KLP"),
                ("Adyar Response Station", "STN-ADY"),
                ("Guindy Fire Station", "STN-GDY"),
            ]
        ):
            node = node_list[rng.randrange(len(node_list))]
            station, _ = Station.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "city": "Chennai",
                    "latitude": node.latitude,
                    "longitude": node.longitude,
                    "contact_number": f"044-2{rng.randint(1000000, 9999999)}",
                },
            )
            stations.append(station)

        vehicles = []
        for is_demo, roster in ((False, AMBULANCES), (True, DEMO_AMBULANCES)):
            for callsign, vtype, operator, is_als, ownership, registration in roster:
                node = node_list[rng.randrange(len(node_list))]
                vehicle, _ = EmergencyVehicle.objects.update_or_create(
                    callsign=callsign,
                    defaults={
                        "vehicle_type": vtype,
                        "ownership": ownership,
                        "operator": operator,
                        "home_station": rng.choice(stations),
                        "status": VehicleStatus.AVAILABLE,
                        "latitude": node.latitude,
                        "longitude": node.longitude,
                        "heading_deg": round(rng.uniform(0, 360), 1),
                        "speed_kmh": 0.0,
                        "last_seen_at": timezone.now(),
                        "is_als": is_als,
                        "is_demo": is_demo,
                        "crew_size": rng.choice([2, 2, 3]),
                        "registration": registration,
                        "equipment": ["defibrillator", "oxygen", "spine board"]
                        + (["ventilator", "infusion pump"] if is_als else []),
                    },
                )
                vehicles.append(vehicle)
        return stations, vehicles

    # -- history ------------------------------------------------------------
    def _build_accidents(self, rng, nodes, count) -> int:
        """Concentrate accidents on a few junctions so hotspots are real."""
        node_list = list(nodes.values())
        blackspots = rng.sample(node_list, k=max(4, len(node_list) // 25))
        now = timezone.now()

        records = []
        for _ in range(count):
            if rng.random() < 0.62:
                node = rng.choice(blackspots)
                spread = 0.0009
            else:
                node = rng.choice(node_list)
                spread = 0.004
            records.append(
                AccidentRecord(
                    occurred_at=now - timezone.timedelta(
                        days=rng.uniform(0, 180), hours=rng.uniform(0, 24)
                    ),
                    latitude=round(node.latitude + rng.uniform(-spread, spread), 6),
                    longitude=round(node.longitude + rng.uniform(-spread, spread), 6),
                    severity=rng.choices([1, 2, 3], weights=[0.55, 0.34, 0.11])[0],
                    casualties=rng.choices([0, 1, 2, 3], weights=[0.45, 0.34, 0.15, 0.06])[0],
                    intersection=node,
                    description=rng.choice(
                        [
                            "Two-wheeler collision", "Rear-end collision at signal",
                            "Pedestrian struck", "Auto-rickshaw overturned",
                            "Lane-change collision", "Signal jump collision",
                        ]
                    ),
                )
            )
        AccidentRecord.objects.bulk_create(records, batch_size=500)
        return len(records)

    def _seed_live_speeds(self, rng, segments) -> None:
        """Give every segment a plausible current speed so routing is live."""
        from apps.brain.congestion import default_speed_factor

        base_factor = default_speed_factor(timezone.localtime())
        stored = list(RoadSegment.objects.all())
        for segment in stored:
            factor = max(0.12, min(1.15, base_factor * rng.uniform(0.65, 1.30)))
            segment.apply_speed(segment.design_speed_kmh * factor, save=False)
        RoadSegment.objects.bulk_update(
            stored,
            ["current_speed_kmh", "congestion_level", "congestion_index", "speed_updated_at"],
            batch_size=800,
        )
