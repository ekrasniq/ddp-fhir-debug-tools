#!/usr/bin/env python3
"""Trace which influenza Conditions can drive infl.cumulative.gender.

The script intentionally ignores influenza Observations and follows the DDP-like
Condition -> Encounter -> VN -> facility-contact path.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json

import ddp_cum_items as cum


# Set these values for your FHIR endpoint.
FHIR_BASE_URL = "http://localhost:8080/fhir"
FHIR_USER = "user"
FHIR_PASSWORD = "password"

BATCH_SIZE = 500
ID_CHUNK_SIZE = 50
USE_POST_FOR_ID_SEARCH = True
FILTER_CONDITIONS_BY_RECORDED_DATE = True
MAX_REJECTED_EXAMPLES = 50


def main() -> None:
    configure_cumulative_module()

    bundle_resources = list(
        cum.search(
            "Condition",
            {
                "code": ",".join(cum.INFLUENZA_ICD_CODES),
                "_revinclude": "Encounter:diagnosis",
                "_pretty": "false",
                "_count": str(BATCH_SIZE),
            },
            use_post=False,
        )
    )
    raw_conditions = [
        r for r in bundle_resources if r.get("resourceType", "Condition") == "Condition"
    ]
    revinclude_encounters = [
        r for r in bundle_resources if r.get("resourceType") == "Encounter"
    ]
    conditions = [
        c
        for c in raw_conditions
        if cum.is_influenza_condition(c)
        and (
            not FILTER_CONDITIONS_BY_RECORDED_DATE
            or recorded_date_at_or_after(c, cum.INFLUENZA_START_DATE)
        )
    ]

    revinclude_links = link_conditions_from_revinclude(conditions, revinclude_encounters)
    positive_patient_ids = cum.clean_set(cum.ref_id(c.get("subject")) for c in conditions)
    patients = cum.fetch_by_chunks("Patient", "_id", sorted(positive_patient_ids))
    patient_by_id = {p.get("id"): p for p in patients if p.get("id")}

    encounters = cum.fetch_by_chunks(
        "Encounter",
        "subject",
        sorted(positive_patient_ids),
        {
            "_count": str(BATCH_SIZE),
            "date": "ge" + cum.INFLUENZA_START_DATE + "T00:00:00",
        },
    )
    encounters = [e for e in encounters if cum.is_valid_encounter(e)]
    encounter_by_id = {e.get("id"): e for e in encounters if e.get("id")}
    facility_contacts_by_vn = group_facility_contacts_by_vn(encounters)

    trace_items = [
        trace_condition(
            condition,
            revinclude_links,
            encounter_by_id,
            facility_contacts_by_vn,
            patient_by_id,
        )
        for condition in conditions
    ]

    recognized_conditions = [item for item in trace_items if item["resolved_encounter_id"]]
    counted_conditions = [item for item in trace_items if item["counted_patient"]]
    rejected_conditions = [item for item in trace_items if not item["counted_patient"]]
    counted_patients = build_counted_patients(counted_conditions)
    ddp_like_gender = count_gender(counted_patients)

    output = {
        "summary": {
            "observations_used": False,
            "condition_bundle_resources_raw": len(bundle_resources),
            "conditions_raw": len(raw_conditions),
            "conditions_filtered_out_by_recorded_date": len(raw_conditions) - len(conditions),
            "conditions_after_date_filter": len(conditions),
            "condition_revinclude_encounters_raw": len(revinclude_encounters),
            "revinclude_linked_conditions": len(revinclude_links),
            "positive_condition_patient_ids": len(positive_patient_ids),
            "patients_loaded": len(patients),
            "encounters_loaded_valid_status": len(encounters),
            "facility_contacts_loaded": sum(1 for e in encounters if cum.is_facility_contact(e)),
            "recognized_conditions_total": len(recognized_conditions),
            "counted_conditions_total": len(counted_conditions),
            "counted_patients_total": sum(ddp_like_gender.values()),
            "link_source_counts": dict(Counter(item["link_source"] for item in trace_items)),
            "reject_reason_counts": dict(
                Counter(
                    item["reject_reason"]
                    for item in rejected_conditions
                    if item["reject_reason"]
                )
            ),
            "filter_conditions_by_recorded_date": FILTER_CONDITIONS_BY_RECORDED_DATE,
            "influenza_start_date": cum.INFLUENZA_START_DATE,
            "influenza_icd_codes": list(cum.INFLUENZA_ICD_CODES),
        },
        "ddp_like_gender": ddp_like_gender,
        "recognized_conditions": recognized_conditions,
        "counted_conditions": counted_conditions,
        "counted_patients": counted_patients,
        "rejected_condition_examples": rejected_conditions[:MAX_REJECTED_EXAMPLES],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def configure_cumulative_module() -> None:
    cum.FHIR_BASE_URL = FHIR_BASE_URL
    cum.FHIR_USER = FHIR_USER
    cum.FHIR_PASSWORD = FHIR_PASSWORD
    cum.BATCH_SIZE = BATCH_SIZE
    cum.ID_CHUNK_SIZE = ID_CHUNK_SIZE
    cum.USE_POST_FOR_ID_SEARCH = USE_POST_FOR_ID_SEARCH
    cum.DISEASE_START_DATE = cum.INFLUENZA_START_DATE
    cum.DISEASE_ICD_CODES = list(cum.INFLUENZA_ICD_CODES)


def recorded_date_at_or_after(condition: dict, start_date: str) -> bool:
    recorded = condition.get("recordedDate")
    return bool(recorded) and recorded[:10] >= start_date


def link_conditions_from_revinclude(
    conditions: list[dict], encounters: list[dict]
) -> dict[str, list[str]]:
    condition_ids = {c.get("id") for c in conditions if c.get("id")}
    linked: dict[str, list[str]] = defaultdict(list)

    for encounter in encounters:
        encounter_id = encounter.get("id")
        if not encounter_id:
            continue
        for diagnosis in encounter.get("diagnosis", []):
            condition_id = cum.ref_id(diagnosis.get("condition"))
            if condition_id in condition_ids:
                linked[condition_id].append(encounter_id)
    return dict(linked)


def group_facility_contacts_by_vn(encounters: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for encounter in encounters:
        vn = cum.visit_number(encounter)
        if vn and cum.is_facility_contact(encounter):
            grouped[vn].append(encounter)
    return dict(grouped)


def trace_condition(
    condition: dict,
    revinclude_links: dict[str, list[str]],
    encounter_by_id: dict[str, dict],
    facility_contacts_by_vn: dict[str, list[dict]],
    patient_by_id: dict[str, dict],
) -> dict:
    condition_id = condition.get("id")
    patient_id = cum.ref_id(condition.get("subject"))
    link_source, resolved_encounter_id = resolve_condition_link(condition, revinclude_links)
    encounter = encounter_by_id.get(resolved_encounter_id) if resolved_encounter_id else None
    vn = cum.visit_number(encounter) if encounter else None
    flagged_facility_contacts = facility_contacts_by_vn.get(vn, []) if vn else []
    counted_gender = gender_label(patient_by_id.get(patient_id))

    reject_reason = None
    counted_patient = False
    if not resolved_encounter_id:
        reject_reason = "no_encounter_link"
    elif not encounter:
        reject_reason = "encounter_id_not_loaded"
    elif not vn:
        reject_reason = "encounter_without_vn"
    elif not flagged_facility_contacts:
        reject_reason = "no_flagged_facility_contact"
    elif not counted_gender:
        reject_reason = "patient_not_counted_by_gender"
    else:
        counted_patient = True

    return {
        "condition_id": condition_id,
        "patient_id": patient_id,
        "recorded_date": condition.get("recordedDate"),
        "icd_codes": condition_icd_codes(condition),
        "condition_encounter_raw": condition.get("encounter"),
        "link_source": link_source,
        "revinclude_encounter_ids": revinclude_links.get(condition_id, []),
        "resolved_encounter_id": resolved_encounter_id,
        "resolved_encounter_status": encounter.get("status") if encounter else None,
        "resolved_encounter_class": (encounter.get("class") or {}).get("code")
        if encounter
        else None,
        "resolved_encounter_period_start": (encounter.get("period") or {}).get("start")
        if encounter
        else None,
        "visit_number": vn,
        "flagged_facility_contact_ids": [
            e.get("id") for e in flagged_facility_contacts if e.get("id")
        ],
        "counted_patient": counted_patient,
        "counted_gender": counted_gender if counted_patient else None,
        "reject_reason": reject_reason,
    }


def resolve_condition_link(
    condition: dict, revinclude_links: dict[str, list[str]]
) -> tuple[str, str | None]:
    encounter_ref = condition.get("encounter") or {}
    reference = encounter_ref.get("reference")
    if reference:
        return "direct_condition_encounter_reference", cum.ref_id(encounter_ref)

    identifier_value = (encounter_ref.get("identifier") or {}).get("value")
    if identifier_value:
        return "direct_condition_encounter_identifier", identifier_value

    linked_encounters = revinclude_links.get(condition.get("id"), [])
    if linked_encounters:
        # The DDP can overwrite Condition.encounter while iterating returned encounters.
        return "revinclude_encounter_diagnosis", linked_encounters[-1]

    return "no_encounter_link", None


def condition_icd_codes(condition: dict) -> list[str]:
    return [
        coding.get("code")
        for coding in (condition.get("code") or {}).get("coding", [])
        if coding.get("system") == cum.ICD_SYSTEM and coding.get("code")
    ]


def gender_label(patient: dict | None) -> str | None:
    if cum.matches_gender(patient, "male"):
        return "Male"
    if cum.matches_gender(patient, "female"):
        return "Female"
    if cum.matches_gender(patient, "diverse"):
        return "Diverse"
    return None


def build_counted_patients(counted_conditions: list[dict]) -> list[dict]:
    by_patient: dict[str, dict] = {}
    for item in counted_conditions:
        patient_id = item["patient_id"]
        if not patient_id:
            continue
        patient = by_patient.setdefault(
            patient_id,
            {
                "patient_id": patient_id,
                "gender": item["counted_gender"],
                "condition_ids": set(),
                "resolved_encounter_ids": set(),
                "visit_numbers": set(),
                "flagged_facility_contact_ids": set(),
            },
        )
        add_if_present(patient["condition_ids"], item["condition_id"])
        add_if_present(patient["resolved_encounter_ids"], item["resolved_encounter_id"])
        add_if_present(patient["visit_numbers"], item["visit_number"])
        patient["flagged_facility_contact_ids"].update(item["flagged_facility_contact_ids"])

    result = []
    for patient in by_patient.values():
        result.append(
            {
                "patient_id": patient["patient_id"],
                "gender": patient["gender"],
                "condition_ids": sorted(patient["condition_ids"]),
                "resolved_encounter_ids": sorted(patient["resolved_encounter_ids"]),
                "visit_numbers": sorted(patient["visit_numbers"]),
                "flagged_facility_contact_ids": sorted(patient["flagged_facility_contact_ids"]),
            }
        )
    return sorted(result, key=lambda x: x["patient_id"])


def count_gender(counted_patients: list[dict]) -> dict[str, int]:
    counts = {"Male": 0, "Female": 0, "Diverse": 0}
    for patient in counted_patients:
        gender = patient.get("gender")
        if gender in counts:
            counts[gender] += 1
    return counts


def add_if_present(values: set[str], value: str | None) -> None:
    if value:
        values.add(value)


if __name__ == "__main__":
    main()
