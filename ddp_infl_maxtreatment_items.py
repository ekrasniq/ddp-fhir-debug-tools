#!/usr/bin/env python3
"""Debug infl.cumulative.maxtreatmentlevel with DDP-like FHIR logic."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json

import ddp_cum_items as cum


# Set these values for your FHIR endpoint.
FHIR_BASE_URL = "http://localhost:8080/fhir"
FHIR_USER = "user"
FHIR_PASSWORD = "password"

BATCH_SIZE = 500
ID_CHUNK_SIZE = 50
USE_POST_FOR_ID_SEARCH = True
USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS = True
FILTER_PATIENT_RETRIEVAL = True
FILTER_RESOURCES_BY_DATE = True
MIMIC_DDP_OBS_INTERPRETATION_REMOVAL = True

# DDP global defaults.
USE_PART_OF_INSTEAD_OF_IDENTIFIER = False
USE_ICU_UNDIFFERENTIATED = False
CHECK_PROCEDURES_ICU_STAYS = True
DDP_DEBUG = False

# Default is the DDP-like DiseaseDataItem list. Set this to True if you also need the
# intermediate counters that explain where cases are lost.
INCLUDE_DIAGNOSTICS = False

# Same idea as global.service-provider-identifier-of-icu-locations.
# Fill with serviceProvider reference IDs or serviceProvider.identifier.value values.
ICU_SERVICE_PROVIDER_IDS: set[str] = set()

# Optional manual override if your Location resources cannot be loaded by _id.
ADDITIONAL_ICU_LOCATION_IDS: set[str] = set()

ICU_DUMMY_ID = "ICU_DUMMY"
TWELVE_DAYS_AFTER_OUTPATIENT = 12
CASE_TYPE_SYSTEM = "http://fhir.de/CodeSystem/KontaktArtDe"
CASE_TYPE_INTENSIVE_STATIONARY = "intensivstationaer"
LOCATION_PHYSICAL_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/location-physical-type"
LOCATION_ROLE_CODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-RoleCode"
LOCATION_WARD_CODE = "wa"
LOCATION_ICU_CODES = {"ICU", "PEDICU", "PEDNICU"}
INPATIENT_CODES = {"IMP", "stationaer"}
OUTPATIENT_CODES = {"AMB", "ambulant"}
SHORT_STAY_CODES = {"SS"}
PROCEDURE_INVALID_STATUS = {"entered-in-error", "not-done"}

PROCEDURE_ECMO_CODES = """
265764009,341939001,127788007,233572003,302497006,233586004,233581009,
182750009,714749008,233573008,233578004,233575001,233588003,233583007,
233594006,72541002,233574002,786452006,786451004,786453001,233579007,
233580005,708932005,427053002,57274006,233577009,233576000,11932001,
698074000,233589006,233590002,708933000,233587008,708930002,233584001,
233585000,715743002,233582002,708934006,14130001000004100,182749009,
438839005,83794005,77257005,448955002,20720000,233564000,233565004,
225067007,19647005
""".replace("\n", "").replace(" ", "").split(",")

PROCEDURE_VENTILATION_CODES = """
26763009,243147009,26763009,1149092001,53950000,243141005,11140008,
428311008,243160003,182687005,286812008,33050008,45851008,55089006,
8948006,45851008,229306004,243180002,243161004,243164007,182687005,
243151006,59427005,286812008,47545007,243142003,37113006,243157005,
243170001,371908008,243163001,243162006,243167000,243166009,243169002,
243168005,243150007,243156001,243148004,243149007,243154003,243155002,
243153009,243152004,286813003,408852001,408853006,286813003,448442005,
405609003,66852000,4764004,243143008,448134000,229308003,34281000175105,
34291000175108,243144002,229312009,425447009,243140006,229313004,425696007,
243184006,74596007,243181003,243183000,243182005,52729008,76777009,
38282001,773454006,281508008,276737004,276732005,243159008,243158000,
243172009,243171002,82433009,447837008,870392006,243146000,429253002,
371907003,398077001,182714002,243136002,315041000,426990007,304577004,
870533002,71786000
""".replace("\n", "").replace(" ", "").split(",")

OUTPATIENT = "Outpatient"
NORMAL_WARD = "Normal_ward"
ICU = "ICU"
ICU_VENTILATION = "ICU_with_ventilation"
ICU_ECMO = "ICU_with_ecmo"
ICU_UNDIFF = "ICU_undifferentiated"
INPATIENT = "Stationary"
ITEMTYPE_AGGREGATED = "aggregated"
ITEMTYPE_LIST = "list"
ITEMTYPE_DEBUG = "debug"
INFL_PREFIX = "infl."


def main() -> None:
    configure_shared_module()

    observations_raw = list(
        cum.search(
            "Observation",
            {
                "code": ",".join(cum.INFLUENZA_LOINC_CODES),
                "_pretty": "false",
                "_count": str(BATCH_SIZE),
                **({"date": "ge" + cum.INFLUENZA_START_DATE} if FILTER_RESOURCES_BY_DATE else {}),
            },
        )
    )
    observations = [o for o in observations_raw if cum.is_valid_observation(o)]

    conditions = list(
        cum.search(
            "Condition",
            {
                "code": ",".join(cum.INFLUENZA_ICD_CODES),
                "_pretty": "false",
                "_count": str(BATCH_SIZE),
                **(
                    {"recorded-date": "ge" + cum.INFLUENZA_START_DATE}
                    if FILTER_RESOURCES_BY_DATE
                    else {}
                ),
            },
        )
    )

    obs_pids = {cum.ref_id(o.get("subject")) for o in observations}
    condition_pids = {cum.ref_id(c.get("subject")) for c in conditions}
    all_source_pids = cum.clean_set(obs_pids | condition_pids)

    positive_obs_pids = {
        cum.ref_id(o.get("subject"))
        for o in observations
        if cum.is_influenza_observation(o) and cum.is_positive_observation(o)
    }
    positive_condition_pids = {
        cum.ref_id(c.get("subject")) for c in conditions if cum.is_influenza_condition(c)
    }
    positive_source_pids = cum.clean_set(positive_obs_pids | positive_condition_pids)

    patient_ids_for_retrieval = (
        positive_source_pids
        if FILTER_PATIENT_RETRIEVAL and positive_source_pids
        else all_source_pids
    )

    patients = cum.fetch_by_chunks("Patient", "_id", sorted(patient_ids_for_retrieval))
    encounters = cum.fetch_by_chunks(
        "Encounter",
        "subject",
        sorted(patient_ids_for_retrieval),
        {
            "_count": str(BATCH_SIZE),
            **(
                {"date": "ge" + cum.INFLUENZA_START_DATE + "T00:00:00"}
                if FILTER_RESOURCES_BY_DATE
                else {}
            ),
        },
    )
    encounters = [e for e in encounters if cum.is_valid_encounter(e)]

    dummy_icu_encounter_ids = apply_dummy_icu_locations(encounters)
    encounter_by_id = {e.get("id"): e for e in encounters if e.get("id")}

    condition_encounter_by_diagnosis = (
        cum.link_conditions_to_encounters_by_diagnosis(conditions, encounters)
        if USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS
        else {}
    )

    location_ids_from_encounters = sorted(
        {loc_id for e in encounters for loc_id in encounter_location_ids(e) if loc_id != ICU_DUMMY_ID}
    )
    locations = cum.fetch_by_chunks("Location", "_id", location_ids_from_encounters)
    location_by_id = {l.get("id"): l for l in locations if l.get("id")}
    dummy_location_available = bool(dummy_icu_encounter_ids or has_any_service_provider(encounters))
    icu_location_ids = {
        loc_id for loc_id, loc in location_by_id.items() if is_icu_location(loc)
    } | ADDITIONAL_ICU_LOCATION_IDS
    if dummy_location_available:
        icu_location_ids.add(ICU_DUMMY_ID)

    facility_contacts = [e for e in encounters if is_facility_contact(e)]
    supply_contacts = [e for e in encounters if is_supply_contact(e) and is_inpatient_or_shortstay(e)]
    department_contacts = [
        e for e in encounters if is_department_contact(e) and is_inpatient_or_shortstay(e)
    ]
    locations_found_for_ddp = bool(locations or dummy_location_available)
    supply_contacts_found_for_ddp = bool(supply_contacts)

    supply_to_facility_map = {}
    supply_map_error = None
    if supply_contacts_found_for_ddp and locations_found_for_ddp:
        supply_to_facility_map = generate_supply_contact_to_facility_contact_map(
            supply_contacts, department_contacts, facility_contacts
        )
        if not supply_to_facility_map:
            supply_map_error = "DDP would throw: no Encounter.identifier VN mapping found."

    positive_encounter_ids_from_obs = {
        cum.ref_id(o.get("encounter"))
        for o in observations
        if cum.is_influenza_observation(o) and cum.is_positive_observation(o)
    }
    positive_encounter_ids_from_conditions = {
        cum.condition_encounter_id(c, condition_encounter_by_diagnosis)
        for c in conditions
        if cum.is_influenza_condition(c)
    }
    positive_encounter_ids = cum.clean_set(
        positive_encounter_ids_from_obs | positive_encounter_ids_from_conditions
    )

    positive_visit_numbers = {
        cum.visit_number(encounter_by_id[enc_id])
        for enc_id in positive_encounter_ids
        if enc_id in encounter_by_id and cum.visit_number(encounter_by_id[enc_id])
    }
    positive_visit_numbers = cum.clean_set(positive_visit_numbers)

    flagged_encounter_ids = {
        e.get("id") for e in encounters if cum.visit_number(e) in positive_visit_numbers
    }
    flagged_encounter_ids = cum.clean_set(flagged_encounter_ids)
    twelve_day_ids = detect_twelve_day_inpatient_encounters(flagged_encounter_ids, encounters)

    procedures_raw = fetch_icu_procedures(sorted(patient_ids_for_retrieval))
    procedures = [p for p in procedures_raw if is_valid_procedure(p)]

    icu_supply_contacts = [
        s for s in supply_contacts if is_icu_case(s, icu_location_ids)
    ]
    procedures_before_icu_filter = procedures
    if CHECK_PROCEDURES_ICU_STAYS:
        icu_facility_ids = {
            facility_contact_id(s, supply_to_facility_map) for s in icu_supply_contacts
        }
        icu_facility_ids = cum.clean_set(icu_facility_ids)
        procedures = [
            p
            for p in procedures
            if procedure_case_id(p) and procedure_case_id(p) in icu_facility_ids
        ]

    positive_facility_contacts = [
        e for e in facility_contacts if e.get("id") in flagged_encounter_ids
    ]
    map_positive_by_class = create_encounter_map_by_class(positive_facility_contacts)
    map_icu = create_icu_map(
        encounters,
        supply_contacts,
        supply_to_facility_map,
        icu_location_ids,
        procedures,
        flagged_encounter_ids,
    )

    current_icu_map = create_current_icu_map(map_icu)
    current_treatmentlevel_lists = create_current_treatmentlevel_lists(
        current_icu_map,
        icu_supply_contacts,
        facility_contacts,
        procedures,
        icu_location_ids,
        flagged_encounter_ids,
        supply_to_facility_map,
    )
    current_maxtreatmentlevel_lists = create_current_maxtreatmentlevel_lists(
        map_icu, facility_contacts, flagged_encounter_ids
    )

    cumulative_outpatient = get_cumulative_by_class(OUTPATIENT, map_positive_by_class, map_icu)
    cumulative_normal_ward = get_cumulative_by_class(INPATIENT, map_positive_by_class, map_icu)
    if USE_ICU_UNDIFFERENTIATED:
        cumulative_icu_undiff = get_cumulative_by_icu_level(ICU_UNDIFF, map_icu)
        counts_before_dedup = treatment_counts(
            cumulative_outpatient, cumulative_normal_ward, icu_undiff=cumulative_icu_undiff
        )
        remove_duplicate_pids_undiff(
            cumulative_outpatient, cumulative_normal_ward, cumulative_icu_undiff
        )
        counts = treatment_counts(
            cumulative_outpatient, cumulative_normal_ward, icu_undiff=cumulative_icu_undiff
        )
        debug_cases = debug_case_ids_by_treatmentlevel(
            {
                OUTPATIENT: cumulative_outpatient,
                NORMAL_WARD: cumulative_normal_ward,
                ICU_UNDIFF: cumulative_icu_undiff,
            }
        )
    else:
        cumulative_icu = get_cumulative_by_icu_level(ICU, map_icu)
        cumulative_vent = get_cumulative_by_icu_level(ICU_VENTILATION, map_icu)
        cumulative_ecmo = get_cumulative_by_icu_level(ICU_ECMO, map_icu)
        counts_before_dedup = treatment_counts(
            cumulative_outpatient,
            cumulative_normal_ward,
            icu=cumulative_icu,
            vent=cumulative_vent,
            ecmo=cumulative_ecmo,
        )
        remove_duplicate_pids(
            cumulative_outpatient,
            cumulative_normal_ward,
            cumulative_icu,
            cumulative_vent,
            cumulative_ecmo,
        )
        counts = treatment_counts(
            cumulative_outpatient,
            cumulative_normal_ward,
            icu=cumulative_icu,
            vent=cumulative_vent,
            ecmo=cumulative_ecmo,
        )
        debug_cases = debug_case_ids_by_treatmentlevel(
            {
                OUTPATIENT: cumulative_outpatient,
                NORMAL_WARD: cumulative_normal_ward,
                ICU: cumulative_icu,
                ICU_VENTILATION: cumulative_vent,
                ICU_ECMO: cumulative_ecmo,
            }
        )

    maxtreatment_items_available = supply_contacts_found_for_ddp and locations_found_for_ddp
    data_items = (
        build_ddp_maxtreatment_items(
            current_treatmentlevel_lists,
            current_maxtreatmentlevel_lists,
            counts,
            debug_cases,
            map_positive_by_class,
            map_icu,
            patients,
        )
        if maxtreatment_items_available
        else []
    )

    diagnostics = {
            "known_context": (
                "ddp_cum_items.py/manual search found far more influenza patients than DDP "
                "infl.cumulative.gender; DDP reported only 6 in the current investigation."
            ),
            "counts_before_final_duplicate_removal": counts_before_dedup,
            "ddp_would_exclude_maxtreatment_items": not (
                supply_contacts_found_for_ddp and locations_found_for_ddp
            ),
            "ddp_exclusion_reason": ddp_exclusion_reason(
                supply_contacts_found_for_ddp, locations_found_for_ddp
            ),
            "supply_mapping_error": supply_map_error,
            "observations_raw": len(observations_raw),
            "observations_after_status_filter": len(observations),
            "conditions_raw": len(conditions),
            "source_patient_ids_all": len(all_source_pids),
            "source_patient_ids_positive": len(positive_source_pids),
            "patients_loaded": len(patients),
            "encounters_loaded_valid_status": len(encounters),
            "facility_contacts_loaded": len(facility_contacts),
            "supply_contacts_loaded_inpatient_or_shortstay": len(supply_contacts),
            "department_contacts_loaded_inpatient_or_shortstay": len(department_contacts),
            "encounters_without_type_treated_as_all_contact_levels": sum(
                1 for e in encounters if not e.get("type")
            ),
            "locations_referenced_by_encounters": len(location_ids_from_encounters),
            "locations_loaded": len(locations),
            "dummy_icu_location_available": dummy_location_available,
            "dummy_icu_encounters_created": len(dummy_icu_encounter_ids),
            "icu_location_ids_effective": len(icu_location_ids),
            "icu_location_ids_effective_sample": sorted(icu_location_ids)[:20],
            "service_provider_ids_configured": sorted(ICU_SERVICE_PROVIDER_IDS),
            "encounters_with_service_provider": sum(1 for e in encounters if e.get("serviceProvider")),
            "supply_to_facility_mappings": len(supply_to_facility_map),
            "supply_contacts_without_facility_mapping": sum(
                1 for s in supply_contacts if not facility_contact_id(s, supply_to_facility_map)
            ),
            "positive_observation_patient_ids": len(cum.clean_set(positive_obs_pids)),
            "positive_condition_patient_ids": len(cum.clean_set(positive_condition_pids)),
            "positive_observation_encounter_ids": len(cum.clean_set(positive_encounter_ids_from_obs)),
            "positive_condition_encounter_ids": len(
                cum.clean_set(positive_encounter_ids_from_conditions)
            ),
            "conditions_linked_via_encounter_diagnosis": len(condition_encounter_by_diagnosis),
            "positive_encounter_ids_total": len(positive_encounter_ids),
            "positive_encounter_ids_loaded": sum(1 for x in positive_encounter_ids if x in encounter_by_id),
            "positive_encounter_ids_missing_in_loaded_encounters": len(
                [x for x in positive_encounter_ids if x not in encounter_by_id]
            ),
            "positive_visit_numbers": len(positive_visit_numbers),
            "flagged_encounters_by_vn": len(flagged_encounter_ids),
            "flagged_facility_contacts_by_vn": len(positive_facility_contacts),
            "flagged_facility_contacts_outpatient": sum(
                1 for e in positive_facility_contacts if is_outpatient(e)
            ),
            "flagged_facility_contacts_inpatient_or_shortstay": sum(
                1 for e in positive_facility_contacts if is_inpatient_or_shortstay(e)
            ),
            "flagged_facility_contacts_missing_period_start": sum(
                1 for e in positive_facility_contacts if not period_start(e)
            ),
            "twelve_day_inpatient_encounters_marked": len(twelve_day_ids),
            "positive_supply_contacts_by_vn": sum(
                1 for s in supply_contacts if s.get("id") in flagged_encounter_ids
            ),
            "icu_supply_contacts_total": len(icu_supply_contacts),
            "icu_supply_contacts_positive": sum(
                1 for s in icu_supply_contacts if s.get("id") in flagged_encounter_ids
            ),
            "facility_contacts_on_icu_from_positive_supply_contacts": len(
                facility_contacts_on_icu(
                    supply_contacts, supply_to_facility_map, icu_location_ids, flagged_encounter_ids
                )
            ),
            "procedures_raw": len(procedures_raw),
            "procedures_after_status_filter": len(procedures_before_icu_filter),
            "procedures_after_icu_ward_filter": len(procedures),
            "procedures_with_encounter_case_id": sum(1 for p in procedures if procedure_case_id(p)),
            "procedures_with_encounter_identifier_only": sum(
                1
                for p in procedures_before_icu_filter
                if not ref_id_reference_only(p.get("encounter"))
                and ((p.get("encounter") or {}).get("identifier") or {}).get("value")
            ),
            "vent_procedures_after_filter": sum(1 for p in procedures if is_vent_procedure(p)),
            "ecmo_procedures_after_filter": sum(1 for p in procedures if is_ecmo_procedure(p)),
            "icu_map_counts": {level: len(items) for level, items in map_icu.items()},
            "map_positive_by_class_counts": {
                OUTPATIENT: len(map_positive_by_class[OUTPATIENT]),
                INPATIENT: len(map_positive_by_class[INPATIENT]),
            },
            "outpatient_candidates_without_period_start": sum(
                1 for e in map_positive_by_class[OUTPATIENT] if not period_start(e)
            ),
            "normal_ward_candidates_without_period_start": sum(
                1
                for e in map_positive_by_class[INPATIENT]
                if is_max_treatmentlevel_normal_ward(map_icu, e) and not period_start(e)
            ),
            "use_part_of_instead_of_identifier": USE_PART_OF_INSTEAD_OF_IDENTIFIER,
            "use_icu_undifferentiated": USE_ICU_UNDIFFERENTIATED,
            "check_procedures_icu_stays": CHECK_PROCEDURES_ICU_STAYS,
            "filter_patient_retrieval": FILTER_PATIENT_RETRIEVAL,
            "mimic_ddp_obs_interpretation_removal": MIMIC_DDP_OBS_INTERPRETATION_REMOVAL,
            "use_post_for_id_search": USE_POST_FOR_ID_SEARCH,
            "batch_size": BATCH_SIZE,
            "id_chunk_size": ID_CHUNK_SIZE,
    }
    output = {"data_items": data_items, "diagnostics": diagnostics} if INCLUDE_DIAGNOSTICS else data_items
    print(json.dumps(output, indent=2, ensure_ascii=False))


def build_ddp_maxtreatment_items(
    current_treatmentlevel_lists: dict[str, list[dict]],
    current_maxtreatmentlevel_lists: dict[str, list[dict]],
    cumulative_counts: dict[str, int],
    cumulative_debug_cases: dict[str, dict[str, list[str]]],
    map_positive_by_class: dict[str, list[dict]],
    map_icu: dict[str, list[dict]],
    patients: list[dict],
) -> list[dict]:
    items = [
        data_item(
            infl_label("current.treatmentlevel"),
            ITEMTYPE_AGGREGATED,
            counts_from_level_lists(current_treatmentlevel_lists, include_outpatient=False),
        )
    ]
    if DDP_DEBUG:
        items.append(
            data_item(
                infl_label("current.treatmentlevel.debug"),
                ITEMTYPE_DEBUG,
                case_id_map_from_level_lists(current_treatmentlevel_lists),
            )
        )

    items.append(
        data_item(
            infl_label("current.maxtreatmentlevel"),
            ITEMTYPE_AGGREGATED,
            counts_from_level_lists(current_maxtreatmentlevel_lists, include_outpatient=False),
        )
    )
    if DDP_DEBUG:
        items.append(
            data_item(
                infl_label("current.maxtreatmentlevel.debug"),
                ITEMTYPE_DEBUG,
                case_id_map_from_level_lists(current_maxtreatmentlevel_lists),
            )
        )

    items.append(
        data_item(
            infl_label("current.age.maxtreatmentlevel.normal_ward"),
            ITEMTYPE_LIST,
            current_max_age_list(
                current_maxtreatmentlevel_lists.get(NORMAL_WARD, []),
                map_positive_by_class,
                patients,
            ),
        )
    )
    if USE_ICU_UNDIFFERENTIATED:
        items.append(
            data_item(
                infl_label("current.age.maxtreatmentlevel.icu_undifferentiated"),
                ITEMTYPE_LIST,
                current_max_age_list(
                    current_maxtreatmentlevel_lists.get(ICU_UNDIFF, []),
                    map_positive_by_class,
                    patients,
                ),
            )
        )
    else:
        for label, level in [
            ("current.age.maxtreatmentlevel.icu", ICU),
            ("current.age.maxtreatmentlevel.icu_with_ventilation", ICU_VENTILATION),
            ("current.age.maxtreatmentlevel.icu_with_ecmo", ICU_ECMO),
        ]:
            items.append(
                data_item(
                    infl_label(label),
                    ITEMTYPE_LIST,
                    current_max_age_list(
                        current_maxtreatmentlevel_lists.get(level, []),
                        map_positive_by_class,
                        patients,
                    ),
                )
            )

    items.append(
        data_item(
            infl_label("cumulative.maxtreatmentlevel"),
            ITEMTYPE_AGGREGATED,
            cumulative_counts,
        )
    )
    if DDP_DEBUG:
        items.append(
            data_item(
                infl_label("cumulative.maxtreatmentlevel.debug"),
                ITEMTYPE_DEBUG,
                cumulative_debug_cases,
            )
        )

    cumulative_age_labels = [
        ("cumulative.age.maxtreatmentlevel.outpatient", OUTPATIENT),
        ("cumulative.age.maxtreatmentlevel.normal_ward", NORMAL_WARD),
    ]
    if USE_ICU_UNDIFFERENTIATED:
        cumulative_age_labels.append(
            ("cumulative.age.maxtreatmentlevel.icu_undifferentiated", ICU_UNDIFF)
        )
    else:
        cumulative_age_labels.extend(
            [
                ("cumulative.age.maxtreatmentlevel.icu", ICU),
                ("cumulative.age.maxtreatmentlevel.icu_with_ventilation", ICU_VENTILATION),
                ("cumulative.age.maxtreatmentlevel.icu_with_ecmo", ICU_ECMO),
            ]
        )
    for item_label, level in cumulative_age_labels:
        items.append(
            data_item(
                infl_label(item_label),
                ITEMTYPE_LIST,
                cumulative_max_age_list(level, map_positive_by_class, map_icu, patients),
            )
        )
    return items


def data_item(itemname: str, itemtype: str, data) -> dict:
    return {"itemname": itemname, "itemtype": itemtype, "data": data}


def infl_label(label: str) -> str:
    return INFL_PREFIX + label


def counts_from_level_lists(levels: dict[str, list[dict]], include_outpatient: bool) -> dict[str, int]:
    keys = [NORMAL_WARD]
    if include_outpatient:
        keys.insert(0, OUTPATIENT)
    if USE_ICU_UNDIFFERENTIATED:
        keys.append(ICU_UNDIFF)
    else:
        keys.extend([ICU, ICU_VENTILATION, ICU_ECMO])
    return {key: len(levels.get(key, [])) for key in keys}


def case_id_map_from_level_lists(levels: dict[str, list[dict]]) -> dict[str, list[str]]:
    return {
        key: [e.get("id") for e in levels.get(key, []) if e.get("id")]
        for key in counts_from_level_lists(levels, include_outpatient=False)
    }


def create_current_icu_map(map_icu: dict[str, list[dict]]) -> dict[str, list[dict]]:
    if USE_ICU_UNDIFFERENTIATED:
        return {ICU_UNDIFF: [e for e in map_icu.get(ICU_UNDIFF, []) if is_active(e)]}
    return {
        ICU: [e for e in map_icu.get(ICU, []) if is_active(e)],
        ICU_VENTILATION: [e for e in map_icu.get(ICU_VENTILATION, []) if is_active(e)],
        ICU_ECMO: [e for e in map_icu.get(ICU_ECMO, []) if is_active(e)],
    }


def create_current_treatmentlevel_lists(
    current_icu_map: dict[str, list[dict]],
    icu_supply_contacts: list[dict],
    facility_contacts: list[dict],
    procedures: list[dict],
    icu_location_ids: set[str],
    flagged_encounter_ids: set[str],
    supply_to_facility_map: dict[str, str],
) -> dict[str, list[dict]]:
    positive_currently_on_icu_supply = [
        s
        for s in icu_supply_contacts
        if s.get("id") in flagged_encounter_ids
        and is_active(s)
        and is_currently_on_icu_ward(s, icu_location_ids)
    ]
    current_icu_facility_ids = cum.clean_set(
        facility_contact_id(s, supply_to_facility_map) for s in positive_currently_on_icu_supply
    )
    active_vent_facility_ids = {
        procedure_case_id(p)
        for p in procedures
        if procedure_case_id(p) and is_vent_procedure(p) and is_active_procedure(p)
    }
    active_ecmo_facility_ids = {
        procedure_case_id(p)
        for p in procedures
        if procedure_case_id(p) and is_ecmo_procedure(p) and is_active_procedure(p)
    }
    active_positive_facility = [
        e
        for e in facility_contacts
        if e.get("id") in flagged_encounter_ids and is_inpatient_or_shortstay(e) and is_active(e)
    ]

    if USE_ICU_UNDIFFERENTIATED:
        return {
            NORMAL_WARD: [
                e for e in active_positive_facility if e.get("id") not in current_icu_facility_ids
            ],
            ICU_UNDIFF: positive_currently_on_icu_supply,
        }

    current_icu_facility = [
        e for e in current_icu_map.get(ICU, []) if e.get("id") in current_icu_facility_ids
    ]
    current_vent = [
        e for e in current_icu_map.get(ICU_VENTILATION, []) if e.get("id") in active_vent_facility_ids
    ]
    current_ecmo = [
        e for e in current_icu_map.get(ICU_ECMO, []) if e.get("id") in active_ecmo_facility_ids
    ]
    return {
        NORMAL_WARD: [
            e
            for e in active_positive_facility
            if e.get("id") not in current_icu_facility_ids
            and e.get("id") not in active_vent_facility_ids
            and e.get("id") not in active_ecmo_facility_ids
        ],
        ICU: [
            e
            for e in current_icu_facility
            if e.get("id") not in active_vent_facility_ids
            and e.get("id") not in active_ecmo_facility_ids
        ],
        ICU_VENTILATION: [e for e in current_vent if e.get("id") not in active_ecmo_facility_ids],
        ICU_ECMO: current_ecmo,
    }


def create_current_maxtreatmentlevel_lists(
    map_icu: dict[str, list[dict]], facility_contacts: list[dict], flagged_encounter_ids: set[str]
) -> dict[str, list[dict]]:
    active_positive_facility = [
        e
        for e in facility_contacts
        if e.get("id") in flagged_encounter_ids and is_inpatient_or_shortstay(e) and is_active(e)
    ]
    if USE_ICU_UNDIFFERENTIATED:
        icu_undiff_ids = {e.get("id") for e in map_icu.get(ICU_UNDIFF, [])}
        return {
            NORMAL_WARD: [e for e in active_positive_facility if e.get("id") not in icu_undiff_ids],
            ICU_UNDIFF: [e for e in active_positive_facility if e.get("id") in icu_undiff_ids],
        }

    icu_ids = {e.get("id") for e in map_icu.get(ICU, [])}
    vent_ids = {e.get("id") for e in map_icu.get(ICU_VENTILATION, [])}
    ecmo_ids = {e.get("id") for e in map_icu.get(ICU_ECMO, [])}
    return {
        NORMAL_WARD: [
            e
            for e in active_positive_facility
            if e.get("id") not in icu_ids | vent_ids | ecmo_ids
        ],
        ICU: [
            e
            for e in active_positive_facility
            if e.get("id") in icu_ids and e.get("id") not in vent_ids | ecmo_ids
        ],
        ICU_VENTILATION: [
            e
            for e in active_positive_facility
            if e.get("id") in vent_ids and e.get("id") not in ecmo_ids
        ],
        ICU_ECMO: [e for e in active_positive_facility if e.get("id") in ecmo_ids],
    }


def current_max_age_list(
    current_max_encounters: list[dict],
    map_positive_by_class: dict[str, list[dict]],
    patients: list[dict],
) -> list[int]:
    patient_ids = {patient_id(e) for e in current_max_encounters}
    pid_admission: dict[str, dict] = {}
    for encounter_list in map_positive_by_class.values():
        for encounter in encounter_list:
            if patient_id(encounter) in patient_ids:
                assign_first_admission_date_to_pid(encounter, pid_admission)
    patient_by_id = {p.get("id"): p for p in patients if p.get("id")}
    ages = [
        age_group_for_patient(patient_by_id.get(pid), encounter)
        for pid, encounter in pid_admission.items()
    ]
    return sorted(age for age in ages if age is not None)


def cumulative_max_age_list(
    treatment_level: str,
    map_positive_by_class: dict[str, list[dict]],
    map_icu: dict[str, list[dict]],
    patients: list[dict],
) -> list[int]:
    outpatient_pids = pid_set(map_positive_by_class.get(OUTPATIENT, []))
    inpatient_pids = pid_set(map_positive_by_class.get(INPATIENT, []))
    icu_undiff_pids = pid_set(map_icu.get(ICU_UNDIFF, [])) if USE_ICU_UNDIFFERENTIATED else set()
    icu_pids = pid_set(map_icu.get(ICU, [])) if not USE_ICU_UNDIFFERENTIATED else set()
    vent_pids = pid_set(map_icu.get(ICU_VENTILATION, [])) if not USE_ICU_UNDIFFERENTIATED else set()
    ecmo_pids = pid_set(map_icu.get(ICU_ECMO, [])) if not USE_ICU_UNDIFFERENTIATED else set()

    encounters_overall = encounter_union(
        map_positive_by_class.get(OUTPATIENT, []),
        map_positive_by_class.get(INPATIENT, []),
        map_icu.get(ICU_UNDIFF, []) if USE_ICU_UNDIFFERENTIATED else [],
        map_icu.get(ICU, []) if not USE_ICU_UNDIFFERENTIATED else [],
        map_icu.get(ICU_VENTILATION, []) if not USE_ICU_UNDIFFERENTIATED else [],
        map_icu.get(ICU_ECMO, []) if not USE_ICU_UNDIFFERENTIATED else [],
    )
    pid_admission: dict[str, dict] = {}
    for encounter in encounters_overall:
        assign_first_admission_date_to_pid(encounter, pid_admission)

    patient_by_id = {p.get("id"): p for p in patients if p.get("id")}
    ages: list[int] = []
    for pid, encounter in pid_admission.items():
        if has_higher_treatment_level(
            pid, treatment_level, inpatient_pids, icu_undiff_pids, icu_pids, vent_pids, ecmo_pids
        ):
            continue
        if is_patient_eligible_for_level(
            pid, treatment_level, outpatient_pids, inpatient_pids, icu_undiff_pids, icu_pids, vent_pids, ecmo_pids
        ):
            age_group = age_group_for_patient(patient_by_id.get(pid), encounter)
            if age_group is not None:
                ages.append(age_group)
    return sorted(ages)


def has_higher_treatment_level(
    patient_id_value: str,
    treatment_level: str,
    inpatient_pids: set[str],
    icu_undiff_pids: set[str],
    icu_pids: set[str],
    vent_pids: set[str],
    ecmo_pids: set[str],
) -> bool:
    has_icu = (
        patient_id_value in icu_undiff_pids
        if USE_ICU_UNDIFFERENTIATED
        else patient_id_value in icu_pids | vent_pids | ecmo_pids
    )
    if treatment_level == OUTPATIENT:
        return patient_id_value in inpatient_pids or has_icu
    if treatment_level == NORMAL_WARD:
        return has_icu
    if treatment_level == ICU:
        return patient_id_value in vent_pids or patient_id_value in ecmo_pids
    if treatment_level == ICU_VENTILATION:
        return patient_id_value in ecmo_pids
    return False


def is_patient_eligible_for_level(
    patient_id_value: str,
    treatment_level: str,
    outpatient_pids: set[str],
    inpatient_pids: set[str],
    icu_undiff_pids: set[str],
    icu_pids: set[str],
    vent_pids: set[str],
    ecmo_pids: set[str],
) -> bool:
    return (
        (treatment_level == OUTPATIENT and patient_id_value in outpatient_pids)
        or (treatment_level == NORMAL_WARD and patient_id_value in inpatient_pids)
        or (treatment_level == ICU and patient_id_value in icu_pids)
        or (treatment_level == ICU_VENTILATION and patient_id_value in vent_pids)
        or (treatment_level == ICU_ECMO and patient_id_value in ecmo_pids)
        or (treatment_level == ICU_UNDIFF and patient_id_value in icu_undiff_pids)
    )


def encounter_union(*encounter_lists: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for encounter_list in encounter_lists:
        for encounter in encounter_list:
            if encounter.get("id"):
                by_id.setdefault(encounter["id"], encounter)
            else:
                no_id.append(encounter)
    return list(by_id.values()) + no_id


def assign_first_admission_date_to_pid(encounter: dict, pid_admission: dict[str, dict]) -> None:
    pid = patient_id(encounter)
    start = period_start(encounter)
    if not pid or not start:
        return
    previous = pid_admission.get(pid)
    if previous is None or start < period_start(previous):
        pid_admission[pid] = encounter


def age_group_for_patient(patient: dict | None, encounter: dict) -> int | None:
    if not patient or not patient.get("birthDate") or not period_start(encounter):
        return None
    birth_date = parse_fhir_date(patient["birthDate"])
    if birth_date is None:
        return None
    age = calculate_age_years(birth_date, period_start(encounter).date())
    return check_age_group(age)


def parse_fhir_date(value: str):
    for candidate in (value, value + "-01" if len(value) == 7 else None, value + "-01-01" if len(value) == 4 else None):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def calculate_age_years(birth_date, case_date) -> int:
    age = case_date.year - birth_date.year
    if (case_date.month, case_date.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def check_age_group(age: int) -> int:
    if age < 0:
        raise ValueError("Age cannot be negative.")
    if age <= 19:
        return 0
    if age >= 90:
        return 90
    if age < 50:
        for start in range(20, 50, 5):
            if age <= start + 4:
                return start
    for start in range(50, 90, 5):
        if age <= start + 4:
            return start
    raise ValueError(f"Unexpected age value: {age}")


def configure_shared_module() -> None:
    cum.FHIR_BASE_URL = FHIR_BASE_URL
    cum.FHIR_USER = FHIR_USER
    cum.FHIR_PASSWORD = FHIR_PASSWORD
    cum.BATCH_SIZE = BATCH_SIZE
    cum.ID_CHUNK_SIZE = ID_CHUNK_SIZE
    cum.USE_POST_FOR_ID_SEARCH = USE_POST_FOR_ID_SEARCH
    cum.USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS = USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS
    cum.FILTER_PATIENT_RETRIEVAL = FILTER_PATIENT_RETRIEVAL
    cum.FILTER_RESOURCES_BY_DATE = FILTER_RESOURCES_BY_DATE
    cum.MIMIC_DDP_OBS_INTERPRETATION_REMOVAL = MIMIC_DDP_OBS_INTERPRETATION_REMOVAL


def fetch_icu_procedures(patient_ids: list[str]) -> list[dict]:
    codes = [f"{cum.SNOMED_SYSTEM}|{code}" for code in PROCEDURE_VENTILATION_CODES + PROCEDURE_ECMO_CODES]
    return cum.fetch_by_chunks(
        "Procedure",
        "subject",
        patient_ids,
        {"code": ",".join(codes), "_count": str(BATCH_SIZE)},
    )


def apply_dummy_icu_locations(encounters: list[dict]) -> set[str]:
    changed: set[str] = set()
    for encounter in encounters:
        if encounter.get("location"):
            continue
        if is_case_type_intensive_stationary(encounter) or service_provider_matches_icu(encounter):
            encounter.setdefault("location", []).append(
                {
                    "location": {"reference": f"Location/{ICU_DUMMY_ID}"},
                    "period": encounter.get("period", {}),
                }
            )
            if encounter.get("id"):
                changed.add(encounter["id"])
    return changed


def generate_supply_contact_to_facility_contact_map(
    supply_contacts: list[dict], department_contacts: list[dict], facility_contacts: list[dict]
) -> dict[str, str]:
    output: dict[str, str] = {}
    facility_by_vn = {
        cum.visit_number(f): f for f in facility_contacts if cum.visit_number(f)
    }
    for supply in supply_contacts:
        facility = facility_by_vn.get(cum.visit_number(supply))
        if facility and supply.get("id") and facility.get("id"):
            output[supply["id"]] = facility["id"]

    if USE_PART_OF_INSTEAD_OF_IDENTIFIER:
        departments_by_id = {d.get("id"): d for d in department_contacts if d.get("id")}
        facilities_by_id = {f.get("id"): f for f in facility_contacts if f.get("id")}
        for supply in supply_contacts:
            department = departments_by_id.get(cum.ref_id(supply.get("partOf")))
            facility = facilities_by_id.get(cum.ref_id((department or {}).get("partOf")))
            if facility and supply.get("id") and facility.get("id"):
                output[supply["id"]] = facility["id"]
                add_visit_number_identifier(supply, facility["id"])
                add_visit_number_identifier(facility, facility["id"])
    return output


def detect_twelve_day_inpatient_encounters(
    flagged_encounter_ids: set[str], encounters: list[dict]
) -> set[str]:
    outpatient_positive = [
        e for e in encounters if e.get("id") in flagged_encounter_ids and is_outpatient(e)
    ]
    inpatient_by_pid: dict[str, list[dict]] = defaultdict(list)
    outpatient_pids = {patient_id(e) for e in outpatient_positive}
    for encounter in encounters:
        if patient_id(encounter) in outpatient_pids and is_inpatient_or_shortstay(encounter):
            inpatient_by_pid[patient_id(encounter)].append(encounter)

    marked: set[str] = set()
    for outpatient in outpatient_positive:
        outpatient_start = period_start(outpatient)
        if not outpatient_start:
            continue
        for inpatient in inpatient_by_pid.get(patient_id(outpatient), []):
            inpatient_start = period_start(inpatient)
            if not inpatient_start or outpatient_start >= inpatient_start:
                continue
            if (inpatient_start.date() - outpatient_start.date()).days <= TWELVE_DAYS_AFTER_OUTPATIENT:
                if inpatient.get("id"):
                    marked.add(inpatient["id"])
    return marked


def create_encounter_map_by_class(positive_facility_contacts: list[dict]) -> dict[str, list[dict]]:
    encounter_map = {OUTPATIENT: [], INPATIENT: []}
    for encounter in positive_facility_contacts:
        if is_inpatient_or_shortstay(encounter):
            encounter_map[INPATIENT].append(encounter)
        elif is_outpatient(encounter):
            encounter_map[OUTPATIENT].append(encounter)
    return encounter_map


def create_icu_map(
    encounters: list[dict],
    supply_contacts: list[dict],
    supply_to_facility_map: dict[str, str],
    icu_location_ids: set[str],
    procedures: list[dict],
    flagged_encounter_ids: set[str],
) -> dict[str, list[dict]]:
    inpatient_positive = [
        e
        for e in encounters
        if e.get("id") in flagged_encounter_ids and is_inpatient_or_shortstay(e)
    ]
    supply_positive = [s for s in supply_contacts if s.get("id") in flagged_encounter_ids]
    contacts_on_icu = facility_contacts_on_icu(
        supply_positive, supply_to_facility_map, icu_location_ids, flagged_encounter_ids
    )

    icu_encounters = [e for e in inpatient_positive if e.get("id") in contacts_on_icu]
    facility_contacts_with_vent = {
        procedure_case_id(p) for p in procedures if procedure_case_id(p) and is_vent_procedure(p)
    }
    facility_contacts_with_ecmo = {
        procedure_case_id(p) for p in procedures if procedure_case_id(p) and is_ecmo_procedure(p)
    }
    vent_encounters = [e for e in inpatient_positive if e.get("id") in facility_contacts_with_vent]
    ecmo_encounters = [e for e in inpatient_positive if e.get("id") in facility_contacts_with_ecmo]

    if USE_ICU_UNDIFFERENTIATED:
        return {ICU_UNDIFF: icu_encounters + vent_encounters + ecmo_encounters}
    return {ICU: icu_encounters, ICU_VENTILATION: vent_encounters, ICU_ECMO: ecmo_encounters}


def get_cumulative_by_class(
    treatment_level: str, map_positive_by_class: dict[str, list[dict]], map_icu: dict[str, list[dict]]
) -> list[dict]:
    if treatment_level == OUTPATIENT:
        pid_set = {
            patient_id(e)
            for values in map_positive_by_class.values()
            for e in values
            if is_outpatient(e)
        }
        return get_first_cases_by_period_start(map_positive_by_class[OUTPATIENT], pid_set)

    if treatment_level == INPATIENT:
        pid_set = {
            patient_id(e)
            for values in map_positive_by_class.values()
            for e in values
            if is_inpatient_or_shortstay(e) and is_max_treatmentlevel_normal_ward(map_icu, e)
        }
        return get_first_cases_by_period_start(map_positive_by_class[INPATIENT], pid_set)

    return []


def get_cumulative_by_icu_level(treatment_level: str, map_icu: dict[str, list[dict]]) -> list[dict]:
    icu_undiff = map_icu.get(ICU_UNDIFF, [])
    if icu_undiff:
        return get_first_disease_positive_encounters(icu_undiff)

    icu_encounters = map_icu.get(ICU, [])
    vent_encounters = map_icu.get(ICU_VENTILATION, [])
    ecmo_encounters = map_icu.get(ICU_ECMO, [])
    vent_pids = {patient_id(e) for e in vent_encounters}
    ecmo_pids = {patient_id(e) for e in ecmo_encounters}

    if treatment_level == ICU:
        return get_first_disease_positive_encounters(
            [e for e in icu_encounters if patient_id(e) not in vent_pids | ecmo_pids]
        )
    if treatment_level == ICU_VENTILATION:
        ecmo_ids = {e.get("id") for e in ecmo_encounters}
        return get_first_disease_positive_encounters(
            [e for e in vent_encounters if e.get("id") not in ecmo_ids]
        )
    if treatment_level == ICU_ECMO:
        return get_first_disease_positive_encounters(ecmo_encounters)
    return []


def get_first_cases_by_period_start(encounters: list[dict], patient_ids: set[str]) -> list[dict]:
    by_pid: dict[str, list[dict]] = defaultdict(list)
    for encounter in encounters:
        if patient_id(encounter) in patient_ids and period_start(encounter):
            by_pid[patient_id(encounter)].append(encounter)
    return [min(items, key=period_start) for items in by_pid.values()]


def get_first_disease_positive_encounters(encounters: list[dict]) -> list[dict]:
    by_pid: dict[str, list[dict]] = defaultdict(list)
    for encounter in encounters:
        if patient_id(encounter):
            by_pid[patient_id(encounter)].append(encounter)
    result = []
    for items in by_pid.values():
        with_start = [e for e in items if period_start(e)]
        if with_start:
            result.append(min(with_start, key=period_start))
    return result


def is_max_treatmentlevel_normal_ward(map_icu: dict[str, list[dict]], encounter: dict) -> bool:
    if ICU_UNDIFF in map_icu:
        return not same_encounter_in_list(encounter, map_icu[ICU_UNDIFF])
    return not (
        same_encounter_in_list(encounter, map_icu.get(ICU_ECMO, []))
        or same_encounter_in_list(encounter, map_icu.get(ICU_VENTILATION, []))
        or same_encounter_in_list(encounter, map_icu.get(ICU, []))
    )


def remove_duplicate_pids(
    outpatient: list[dict],
    normal_ward: list[dict],
    icu: list[dict],
    vent: list[dict],
    ecmo: list[dict],
) -> None:
    pids_outpatient = pid_set(outpatient)
    pids_normal = pid_set(normal_ward)
    pids_icu = pid_set(icu)
    pids_vent = pid_set(vent)
    pids_ecmo = pid_set(ecmo)

    pids_outpatient -= pids_normal | pids_icu | pids_vent | pids_ecmo
    pids_normal -= pids_icu | pids_vent | pids_ecmo
    pids_icu -= pids_vent | pids_ecmo
    pids_vent -= pids_ecmo

    filter_encounters(outpatient, pids_outpatient)
    filter_encounters(normal_ward, pids_normal)
    filter_encounters(icu, pids_icu)
    filter_encounters(vent, pids_vent)
    filter_encounters(ecmo, pids_ecmo)


def remove_duplicate_pids_undiff(outpatient: list[dict], normal_ward: list[dict], icu_undiff: list[dict]) -> None:
    pids_outpatient = pid_set(outpatient)
    pids_normal = pid_set(normal_ward)
    pids_icu_undiff = pid_set(icu_undiff)

    pids_outpatient -= pids_normal | pids_icu_undiff
    pids_normal -= pids_icu_undiff

    filter_encounters(outpatient, pids_outpatient)
    filter_encounters(normal_ward, pids_normal)


def treatment_counts(
    outpatient: list[dict],
    normal_ward: list[dict],
    icu: list[dict] | None = None,
    vent: list[dict] | None = None,
    ecmo: list[dict] | None = None,
    icu_undiff: list[dict] | None = None,
) -> dict[str, int]:
    counts = {OUTPATIENT: len(outpatient), NORMAL_WARD: len(normal_ward)}
    if icu_undiff is not None:
        counts[ICU_UNDIFF] = len(icu_undiff)
    else:
        counts[ICU] = len(icu or [])
        counts[ICU_VENTILATION] = len(vent or [])
        counts[ICU_ECMO] = len(ecmo or [])
    return counts


def debug_case_ids_by_treatmentlevel(levels: dict[str, list[dict]]) -> dict[str, dict[str, list[str]]]:
    output: dict[str, dict[str, list[str]]] = {}
    for level, encounters in levels.items():
        output[level] = defaultdict(list)
        for encounter in encounters:
            output[level][patient_id(encounter)].append(encounter.get("id"))
        output[level] = dict(sorted(output[level].items()))
    return output


def facility_contacts_on_icu(
    supply_contacts: list[dict],
    supply_to_facility_map: dict[str, str],
    icu_location_ids: set[str],
    flagged_encounter_ids: set[str],
) -> set[str]:
    return cum.clean_set(
        facility_contact_id(s, supply_to_facility_map)
        for s in supply_contacts
        if s.get("id") in flagged_encounter_ids and is_icu_case(s, icu_location_ids)
    )


def filter_encounters(encounters: list[dict], valid_pids: set[str]) -> None:
    encounters[:] = [e for e in encounters if patient_id(e) in valid_pids]


def pid_set(encounters: list[dict]) -> set[str]:
    return cum.clean_set(patient_id(e) for e in encounters)


def same_encounter_in_list(encounter: dict, encounters: list[dict]) -> bool:
    return any(e.get("id") == encounter.get("id") for e in encounters)


def patient_id(resource: dict) -> str | None:
    return cum.ref_id(resource.get("subject"))


def facility_contact_id(encounter: dict, supply_to_facility_map: dict[str, str]) -> str | None:
    if is_facility_contact(encounter):
        return encounter.get("id")
    return supply_to_facility_map.get(encounter.get("id"))


def procedure_case_id(procedure: dict) -> str | None:
    return cum.ref_id(procedure.get("encounter"))


def is_valid_procedure(procedure: dict) -> bool:
    status = procedure.get("status")
    return bool(status) and status not in PROCEDURE_INVALID_STATUS


def is_vent_procedure(procedure: dict) -> bool:
    return procedure_code_matches(procedure, set(PROCEDURE_VENTILATION_CODES))


def is_ecmo_procedure(procedure: dict) -> bool:
    return procedure_code_matches(procedure, set(PROCEDURE_ECMO_CODES))


def procedure_code_matches(procedure: dict, codes: set[str]) -> bool:
    codeable = procedure.get("code") or {}
    codings = codeable.get("coding", [])
    snomed_codes = [c.get("code") for c in codings if c.get("system") == cum.SNOMED_SYSTEM]
    if any(code in codes for code in snomed_codes):
        return True
    first_code = codings[0].get("code") if codings else None
    return first_code in codes


def is_facility_contact(encounter: dict) -> bool:
    return is_contact_type(encounter, "einrichtungskontakt")


def is_supply_contact(encounter: dict) -> bool:
    return is_contact_type(encounter, "versorgungsstellenkontakt")


def is_department_contact(encounter: dict) -> bool:
    return is_contact_type(encounter, "abteilungskontakt")


def is_contact_type(encounter: dict, code: str) -> bool:
    types = encounter.get("type")
    if not types:
        return True
    return any(cum.has_code(t, cum.ENCOUNTER_CONTACT_LEVEL_SYSTEM, {code}) for t in types)


def is_inpatient_or_shortstay(encounter: dict) -> bool:
    return class_code(encounter) in INPATIENT_CODES | SHORT_STAY_CODES


def is_outpatient(encounter: dict) -> bool:
    return class_code(encounter) in OUTPATIENT_CODES


def class_code(encounter: dict) -> str | None:
    class_obj = encounter.get("class") or {}
    return class_obj.get("code")


def is_icu_case(encounter: dict, icu_location_ids: set[str]) -> bool:
    return any(loc_id in icu_location_ids for loc_id in encounter_location_ids(encounter))


def encounter_location_ids(encounter: dict) -> list[str]:
    ids = []
    for loc in encounter.get("location", []):
        loc_id = cum.ref_id(loc.get("location"))
        if loc_id:
            ids.append(loc_id)
    return ids


def is_icu_location(location: dict) -> bool:
    if not location.get("type"):
        return False
    if not any(has_any_code(cc, LOCATION_ICU_CODES) for cc in location.get("type", [])):
        return False
    physical_type = location.get("physicalType")
    return has_any_code(physical_type, {LOCATION_WARD_CODE})


def is_case_type_intensive_stationary(encounter: dict) -> bool:
    return any(cum.has_code(t, CASE_TYPE_SYSTEM, {CASE_TYPE_INTENSIVE_STATIONARY}) for t in encounter.get("type", []))


def service_provider_matches_icu(encounter: dict) -> bool:
    service_provider_id = reference_id_or_identifier(encounter.get("serviceProvider"))
    return bool(service_provider_id and service_provider_id in ICU_SERVICE_PROVIDER_IDS)


def has_any_service_provider(encounters: list[dict]) -> bool:
    return any(e.get("serviceProvider") for e in encounters)


def reference_id_or_identifier(reference: dict | None) -> str | None:
    return cum.ref_id(reference)


def ref_id_reference_only(reference: dict | None) -> str | None:
    if not reference:
        return None
    text = reference.get("reference")
    if not text or text.endswith("/"):
        return None
    return text.rsplit("/", 1)[-1]


def add_visit_number_identifier(encounter: dict, value: str) -> None:
    encounter.setdefault("identifier", []).append(
        {
            "type": {
                "coding": [
                    {
                        "system": cum.IDENTIFIER_VN_TYPE_SYSTEM,
                        "code": "VN",
                    }
                ]
            },
            "value": value,
        }
    )


def period_start(encounter: dict) -> datetime | None:
    period = encounter.get("period") or {}
    raw = period.get("start")
    if not raw:
        return None
    return parse_fhir_datetime(raw)


def parse_fhir_datetime(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(value[:10])
        except ValueError:
            return None


def has_any_code(codeable: dict | None, codes: set[str]) -> bool:
    if not codeable:
        return False
    return any(c.get("code") in codes for c in codeable.get("coding", []))


def ddp_exclusion_reason(supply_found: bool, locations_found: bool) -> str | None:
    missing = []
    if not supply_found:
        missing.append("no inpatient/short-stay Versorgungsstellenkontakt")
    if not locations_found:
        missing.append("no Location resources and no dummy ICU location")
    return ", ".join(missing) if missing else None


if __name__ == "__main__":
    main()
