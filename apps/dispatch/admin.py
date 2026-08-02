from django.contrib import admin

from apps.dispatch.models import EmergencyTrip, PriorityDirective, RoutePlan, SignalPreemption


class RoutePlanInline(admin.TabularInline):
    model = RoutePlan
    extra = 0
    fields = ("is_active", "algorithm", "total_distance_m", "total_duration_s", "reason", "computed_at")
    readonly_fields = fields
    show_change_link = True


class PriorityDirectiveInline(admin.TabularInline):
    model = PriorityDirective
    extra = 0
    fields = ("created_at", "priority_level", "previous_level", "siren_mode", "trigger")
    readonly_fields = fields


@admin.register(EmergencyTrip)
class EmergencyTripAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "vehicle", "stage", "emergency_category", "priority_level",
        "destination_hospital", "eta", "created_at",
    )
    list_filter = ("stage", "emergency_category", "priority_level")
    search_fields = ("reference", "vehicle__callsign", "incident_address")
    raw_id_fields = ("vehicle", "destination_hospital")
    readonly_fields = ("reference", "uuid")
    inlines = [RoutePlanInline, PriorityDirectiveInline]
    date_hierarchy = "created_at"


@admin.register(RoutePlan)
class RoutePlanAdmin(admin.ModelAdmin):
    list_display = ("trip", "is_active", "algorithm", "total_distance_m", "total_duration_s", "computed_at")
    list_filter = ("is_active", "algorithm")
    raw_id_fields = ("trip",)


@admin.register(SignalPreemption)
class SignalPreemptionAdmin(admin.ModelAdmin):
    list_display = (
        "signal", "trip", "state", "planned_green_at", "activated_at",
        "released_at", "priority_score",
    )
    list_filter = ("state",)
    raw_id_fields = ("trip", "signal", "yielded_to")


@admin.register(PriorityDirective)
class PriorityDirectiveAdmin(admin.ModelAdmin):
    list_display = (
        "trip", "priority_level", "previous_level", "siren_mode",
        "light_pattern", "trigger", "created_at",
    )
    list_filter = ("priority_level", "siren_mode")
    raw_id_fields = ("trip",)
