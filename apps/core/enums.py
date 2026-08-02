"""Domain vocabulary shared by every SEVPS layer.

Keeping these in one place means the paramedic app, the signal controller, the
hospital dashboard and the analytics warehouse all speak the same language.
"""
from django.db import models


class VehicleType(models.TextChoices):
    AMBULANCE = "ambulance", "Ambulance"
    FIRE_ENGINE = "fire_engine", "Fire Engine"
    POLICE = "police", "Police Vehicle"
    DISASTER_RESPONSE = "disaster", "Disaster Response Unit"


class VehicleStatus(models.TextChoices):
    OFFLINE = "offline", "Offline"
    AVAILABLE = "available", "Available"
    DISPATCHED = "dispatched", "Dispatched - en route to scene"
    ON_SCENE = "on_scene", "On scene"
    TRANSPORTING = "transporting", "Transporting patient"
    AT_HOSPITAL = "at_hospital", "At hospital - handover"
    RETURNING = "returning", "Returning to base"
    OUT_OF_SERVICE = "out_of_service", "Out of service"


class EmergencyCategory(models.TextChoices):
    """Categories the paramedic picks in the app (Layer 5, table in §4)."""

    CARDIAC = "cardiac", "Heart Attack / Cardiac Arrest"
    STROKE = "stroke", "Stroke"
    BURN = "burn", "Burn"
    TRAUMA = "trauma", "Trauma / Major Accident"
    POISONING = "poisoning", "Poisoning"
    RESPIRATORY = "respiratory", "Respiratory Distress"
    OBSTETRIC = "obstetric", "Obstetric Emergency"
    PEDIATRIC = "pediatric", "Paediatric Emergency"
    FIRE_RESCUE = "fire_rescue", "Fire / Rescue"
    TRANSFER = "transfer", "Non-critical Transfer"
    UNKNOWN = "unknown", "Undetermined"


class PriorityLevel(models.IntegerChoices):
    """Layer 6 severity ladder - drives lights, siren and signal priority."""

    CRITICAL = 1, "Level 1 - Critical Emergency"
    HIGH = 2, "Level 2 - High Emergency"
    MODERATE = 3, "Level 3 - Moderate Emergency"
    NON_CRITICAL = 4, "Level 4 - Non-Critical Transport"


class SirenMode(models.TextChoices):
    CONTINUOUS = "continuous", "Continuous high-priority siren"
    INTERMITTENT = "intermittent", "Intermittent siren"
    BURST = "burst", "Short bursts at junctions only"
    OFF = "off", "Silent"


class LightPattern(models.TextChoices):
    MAX_INTENSITY = "max", "Maximum-intensity flashing"
    STANDARD = "standard", "Standard emergency lights"
    FLASHING = "flashing", "Flashing lights"
    OFF = "off", "Lights off"


class TripStage(models.TextChoices):
    """Lifecycle of one emergency response."""

    CREATED = "created", "Created"
    TO_SCENE = "to_scene", "En route to scene"
    ON_SCENE = "on_scene", "On scene - patient assessment"
    TO_HOSPITAL = "to_hospital", "En route to hospital"
    ARRIVED = "arrived", "Arrived at hospital"
    HANDOVER = "handover", "Patient handover complete"
    CANCELLED = "cancelled", "Cancelled"


class SignalPhase(models.TextChoices):
    RED = "red", "Red"
    AMBER = "amber", "Amber"
    GREEN = "green", "Green"
    FLASHING_AMBER = "flashing_amber", "Flashing amber"
    OFF = "off", "Dark / manual control"


class PreemptionState(models.TextChoices):
    PLANNED = "planned", "Planned"
    ARMED = "armed", "Armed - clearance running"
    ACTIVE = "active", "Active - corridor green"
    RELEASED = "released", "Released - normal timing restored"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed / controller unreachable"


class RoadEventType(models.TextChoices):
    ACCIDENT = "accident", "Accident"
    CLOSURE = "closure", "Road closure"
    CONSTRUCTION = "construction", "Construction"
    CONGESTION = "congestion", "Heavy congestion"
    PUBLIC_EVENT = "public_event", "Public gathering / event"
    WATERLOGGING = "waterlogging", "Waterlogging"
    ILLEGAL_PARKING = "illegal_parking", "Illegal parking"
    LANE_OBSTRUCTION = "lane_obstruction", "Lane obstruction"
    BLOCKAGE = "blockage", "Road blockage"


class EventSource(models.TextChoices):
    COMPUTER_VISION = "cv", "Computer vision"
    OPERATOR = "operator", "Traffic operator"
    CROWDSOURCE = "crowd", "Crowdsourced report"
    PROVIDER = "provider", "External traffic provider"
    PREDICTED = "predicted", "AI prediction"
    SIMULATION = "simulation", "Simulation"


class CongestionLevel(models.TextChoices):
    FREE = "free", "Free flow"
    LIGHT = "light", "Light"
    MODERATE = "moderate", "Moderate"
    HEAVY = "heavy", "Heavy"
    JAM = "jam", "Standstill"

    @staticmethod
    def from_ratio(ratio: float) -> str:
        """Map speed ratio (current / free-flow) to a level."""
        if ratio >= 0.85:
            return CongestionLevel.FREE
        if ratio >= 0.65:
            return CongestionLevel.LIGHT
        if ratio >= 0.45:
            return CongestionLevel.MODERATE
        if ratio >= 0.20:
            return CongestionLevel.HEAVY
        return CongestionLevel.JAM


class RoadClass(models.TextChoices):
    """OSM-derived highway classes, ordered fastest to slowest."""

    MOTORWAY = "motorway", "Motorway"
    TRUNK = "trunk", "Trunk"
    PRIMARY = "primary", "Primary"
    SECONDARY = "secondary", "Secondary"
    TERTIARY = "tertiary", "Tertiary"
    RESIDENTIAL = "residential", "Residential"
    SERVICE = "service", "Service road"


#: Free-flow design speeds (km/h) used when a segment has no observed speed.
FREE_FLOW_KMH: dict[str, float] = {
    RoadClass.MOTORWAY: 80.0,
    RoadClass.TRUNK: 60.0,
    RoadClass.PRIMARY: 50.0,
    RoadClass.SECONDARY: 40.0,
    RoadClass.TERTIARY: 35.0,
    RoadClass.RESIDENTIAL: 25.0,
    RoadClass.SERVICE: 15.0,
}

#: How much faster a priority vehicle moves than general traffic on the same
#: link once a green corridor is in force (right-of-way + cleared lane).
EMERGENCY_SPEED_ADVANTAGE: dict[int, float] = {
    PriorityLevel.CRITICAL: 1.45,
    PriorityLevel.HIGH: 1.30,
    PriorityLevel.MODERATE: 1.15,
    PriorityLevel.NON_CRITICAL: 1.00,
}


class AlertChannel(models.TextChoices):
    MOBILE_APP = "mobile_app", "SEVPS mobile application"
    NAVIGATION = "navigation", "Navigation app integration"
    VMS_BOARD = "vms", "Digital road display board"
    CITY_DISPLAY = "city_display", "Smart city information display"
    RADIO = "radio", "Traffic control radio"


class HospitalFacility(models.TextChoices):
    """Capabilities a hospital may hold - the right-hand side of §4's table."""

    CARDIAC_ICU = "cardiac_icu", "Cardiac ICU"
    CATH_LAB = "cath_lab", "Catheterisation Lab"
    NEUROLOGY = "neurology", "Neurology"
    CT_SCAN = "ct_scan", "CT Scan"
    MRI = "mri", "MRI"
    BURN_UNIT = "burn_unit", "Burn Unit"
    TRAUMA_CENTER = "trauma_center", "Trauma Center"
    TOXICOLOGY = "toxicology", "Toxicology"
    ICU = "icu", "General ICU"
    OPERATION_THEATRE = "operation_theatre", "Operation Theatre"
    BLOOD_BANK = "blood_bank", "Blood Bank"
    VENTILATOR = "ventilator", "Ventilator support"
    DIALYSIS = "dialysis", "Dialysis"
    NICU = "nicu", "Neonatal ICU"
    PICU = "picu", "Paediatric ICU"
    OBSTETRICS = "obstetrics", "Obstetrics / Labour ward"
    STROKE_UNIT = "stroke_unit", "Stroke Unit"
    EMERGENCY_DEPT = "emergency_dept", "Emergency Department"
