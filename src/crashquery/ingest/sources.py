"""STATS19 source definitions.

DfT publishes one CSV per table per year at a predictable URL. Files are coded
integers — the meaning of `accident_severity = 1` lives in a separate data
guide, not in the data. That separation is deliberate here: it is the whole
reason the agent needs a metadata tool rather than just a schema dump.

Licence: Open Government Licence v3.0.
Landing page: https://www.gov.uk/government/statistics/road-safety-data
"""

from __future__ import annotations

BASE_URL = "https://data.dft.gov.uk/road-accidents-safety-data"

# DfT renamed "accident" to "collision" in the 2022 release. Older years use
# the accident-* filenames, so pass the year through this.
TABLES = ("collision", "vehicle", "casualty")

# DfT renamed accident → collision in the file names and again in the
# column names (accident_index → collision_index). The rest of the agent
# still speaks the older, documented names, so we normalise on ingest.
HEADER_ALIASES = {
    "collision_index": "accident_index",
    "collision_year": "accident_year",
    "collision_ref_no": "accident_reference",
    "collision_severity": "accident_severity",
}


def csv_filename(table: str, year: int) -> str:
    return f"dft-road-casualty-statistics-{table}-{year}.csv"


def csv_url(table: str, year: int) -> str:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    return f"{BASE_URL}/{csv_filename(table, year)}"


def candidate_csv_urls(table: str, year: int) -> list[str]:
    """Return URL fallbacks. Older years used `accident` instead of `collision`."""
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    names = [table]
    if table == "collision":
        names.append("accident")
    return [f"{BASE_URL}/dft-road-casualty-statistics-{name}-{year}.csv" for name in names]


def canonical_column(name: str) -> str:
    return HEADER_ALIASES.get(name.strip().lower(), name.strip().lower())


# --------------------------------------------------------------------------
# Type overlay
# --------------------------------------------------------------------------
# The loader reads the real CSV header rather than trusting a hardcoded DDL —
# STATS19 columns have shifted across releases and a hardcoded schema breaks
# silently on a year you didn't test. Anything not listed here lands as TEXT,
# which is safe and easy to cast later.

COLUMN_TYPES: dict[str, str] = {
    # keys / identity
    "accident_index": "TEXT",
    "collision_index": "TEXT",
    "accident_reference": "TEXT",
    "collision_ref_no": "TEXT",
    "accident_year": "SMALLINT",
    "collision_year": "SMALLINT",
    "vehicle_reference": "INTEGER",
    "casualty_reference": "INTEGER",
    # collision facts
    "number_of_vehicles": "INTEGER",
    "number_of_casualties": "INTEGER",
    "speed_limit": "INTEGER",
    "longitude": "DOUBLE PRECISION",
    "latitude": "DOUBLE PRECISION",
    "location_easting_osgr": "INTEGER",
    "location_northing_osgr": "INTEGER",
    "date": "TEXT",  # dd/mm/yyyy in source; converted in the typed view
    "time": "TEXT",
    # coded categoricals — kept as INTEGER so the lookup join is explicit
    "accident_severity": "SMALLINT",
    "collision_severity": "SMALLINT",
    "casualty_severity": "SMALLINT",
    "day_of_week": "SMALLINT",
    "road_type": "SMALLINT",
    "junction_detail": "SMALLINT",
    "junction_control": "SMALLINT",
    "light_conditions": "SMALLINT",
    "weather_conditions": "SMALLINT",
    "road_surface_conditions": "SMALLINT",
    "urban_or_rural_area": "SMALLINT",
    "first_road_class": "SMALLINT",
    "second_road_class": "SMALLINT",
    "pedestrian_crossing_human_control": "SMALLINT",
    "pedestrian_crossing_physical_facilities": "SMALLINT",
    "special_conditions_at_site": "SMALLINT",
    "carriageway_hazards": "SMALLINT",
    "did_police_officer_attend_scene_of_accident": "SMALLINT",
    # vehicle
    "vehicle_type": "SMALLINT",
    "vehicle_manoeuvre": "SMALLINT",
    "junction_location": "SMALLINT",
    "skidding_and_overturning": "SMALLINT",
    "first_point_of_impact": "SMALLINT",
    "journey_purpose_of_driver": "SMALLINT",
    "sex_of_driver": "SMALLINT",
    "age_of_driver": "INTEGER",
    "age_band_of_driver": "SMALLINT",
    "engine_capacity_cc": "INTEGER",
    "age_of_vehicle": "INTEGER",
    "driver_imd_decile": "SMALLINT",
    "propulsion_code": "SMALLINT",
    # casualty
    "casualty_class": "SMALLINT",
    "sex_of_casualty": "SMALLINT",
    "age_of_casualty": "INTEGER",
    "age_band_of_casualty": "SMALLINT",
    "casualty_type": "SMALLINT",
    "pedestrian_location": "SMALLINT",
    "pedestrian_movement": "SMALLINT",
    "car_passenger": "SMALLINT",
    "bus_or_coach_passenger": "SMALLINT",
    "casualty_imd_decile": "SMALLINT",
}

DEFAULT_TYPE = "TEXT"


def column_type(name: str) -> str:
    key = canonical_column(name)
    return COLUMN_TYPES.get(key, DEFAULT_TYPE)


# Human-readable table comments. These get written into Postgres as COMMENT ON,
# so `describe_table` returns them and the agent gets grain information — which
# is the single most common thing agents get wrong on this schema.
TABLE_COMMENTS = {
    "collisions": (
        "One row per reported road collision in Great Britain. GRAIN: collision. "
        "Counting rows here counts collisions, not casualties."
    ),
    "vehicles": (
        "One row per vehicle involved in a collision. GRAIN: vehicle. "
        "Joins to collisions on accident_index. A collision with 3 vehicles has "
        "3 rows here, so joining to collisions fans out the collision columns."
    ),
    "casualties": (
        "One row per person injured. GRAIN: casualty. Joins to collisions on "
        "accident_index and to vehicles on (accident_index, vehicle_reference). "
        "Fatality counts come from THIS table via casualty_severity = 1, not "
        "from collisions.accident_severity, which describes the worst outcome "
        "of the whole collision."
    ),
    "code_lookups": (
        "Decodes the integer codes used throughout the other tables. "
        "One row per (table_name, field_name, code). Always consult this before "
        "interpreting or filtering a coded column."
    ),
}
