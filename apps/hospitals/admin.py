from django.contrib import admin

from apps.hospitals.models import (
    EmergencyRule,
    Hospital,
    HospitalAlert,
    HospitalCapability,
    HospitalCapacity,
    HospitalRecommendationLog,
)


class HospitalCapabilityInline(admin.TabularInline):
    model = HospitalCapability
    extra = 1


class HospitalCapacityInline(admin.StackedInline):
    model = HospitalCapacity
    extra = 0
    can_delete = False


@admin.register(Hospital)
class HospitalAdmin(admin.ModelAdmin):
    list_display = (
        "name", "code", "city", "is_active", "is_trauma_designated",
        "is_on_diversion", "quality_index",
    )
    list_filter = ("city", "is_active", "is_trauma_designated", "is_on_diversion")
    search_fields = ("name", "code")
    inlines = [HospitalCapabilityInline, HospitalCapacityInline]


@admin.register(EmergencyRule)
class EmergencyRuleAdmin(admin.ModelAdmin):
    list_display = (
        "display_name", "category", "default_priority_level",
        "requires_icu", "golden_window_min", "time_critical", "is_active",
    )
    list_filter = ("default_priority_level", "requires_icu", "time_critical", "is_active")
    search_fields = ("display_name", "category")


@admin.register(HospitalCapacity)
class HospitalCapacityAdmin(admin.ModelAdmin):
    list_display = (
        "hospital", "emergency_beds_available", "icu_beds_available",
        "patients_waiting", "doctors_on_duty", "reported_at",
    )
    raw_id_fields = ("hospital",)


@admin.register(HospitalAlert)
class HospitalAlertAdmin(admin.ModelAdmin):
    list_display = ("hospital", "trip", "emergency_category", "priority_level", "eta", "acknowledged_at")
    list_filter = ("emergency_category", "priority_level")
    raw_id_fields = ("hospital", "trip")


@admin.register(HospitalRecommendationLog)
class HospitalRecommendationLogAdmin(admin.ModelAdmin):
    list_display = ("trip", "emergency_category", "recommended", "chosen", "created_at")
    list_filter = ("emergency_category",)
    raw_id_fields = ("trip", "recommended", "chosen")
