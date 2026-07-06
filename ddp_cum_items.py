#!/usr/bin/env python3
"""Generate one DDP-like cumulative.gender item from FHIR resources.

This module defaults to influenza for backwards compatibility. Use
ddp_infl_cumulative_items.py or ddp_covid_cumulative_items.py as the clearer entry points.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.parse
import urllib.request


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
USE_OBSERVATIONS = True
USE_CONDITIONS = True

INFLUENZA_START_DATE = "2022-09-01"
ICD_SYSTEM = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"
LOINC_SYSTEM = "http://loinc.org"
SNOMED_SYSTEM = "http://snomed.info/sct"
OBS_INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
ENCOUNTER_CONTACT_LEVEL_SYSTEM = "http://fhir.de/CodeSystem/Kontaktebene"
IDENTIFIER_VN_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
GENDER_EXTENSION_URL = "http://fhir.de/StructureDefinition/gender-amtlich-de"

INFLUENZA_ICD_CODES = ["J10.0", "J10.1", "J10.8", "J09"]
INFLUENZA_LOINC_CODES = """
100343-3,100344-1,100973-7,100974-5,101292-1,101293-9,101294-7,101295-4,
101423-2,101424-0,101983-5,29906-5,29907-3,34487-9,38270-5,38271-3,
38272-1,40982-1,44795-3,49520-0,49521-8,49523-4,49524-2,49526-7,
49527-5,49528-3,49531-7,49535-8,50700-4,55464-2,55465-9,57985-4,
60267-2,60494-2,60530-3,60538-6,62462-7,62860-2,68986-9,68987-7,
74785-7,74786-5,76077-7,76078-5,76079-3,76080-1,77026-3,77027-1,
77028-9,80588-7,80589-5,80590-3,80591-1,81428-5,82166-0,82167-8,
82168-6,82169-4,82170-2,85477-8,85478-6,86569-1,86572-5,88193-8,
88195-3,88592-1,88596-2,88599-6,88600-2,92141-1,92142-9,92808-5,
92809-3,92976-0,92977-8,94394-4,94395-1,94396-9,95658-1,99623-1
""".replace("\n", "").replace(" ", "").split(",")

DISEASE_START_DATE = INFLUENZA_START_DATE
DISEASE_ICD_CODES = list(INFLUENZA_ICD_CODES)
DISEASE_POSITIVE_LOINC_CODES = list(INFLUENZA_LOINC_CODES)
DISEASE_RETRIEVAL_LOINC_CODES = list(DISEASE_POSITIVE_LOINC_CODES)
OUTPUT_ITEM_NAME = "infl.cumulative.gender"

POSITIVE_VALUE_CODES = {"10828004", "260373001", "52101004"}
POSITIVE_INTERPRETATION_CODES = {"POS", "DET"}
VALID_ENCOUNTER_STATUS = {"in-progress", "finished"}
INVALID_OBSERVATION_STATUS = {"cancelled", "entered-in-error"}


def main() -> None:
    observations_raw = (
        list(
            search(
                "Observation",
                {
                    "code": ",".join(DISEASE_RETRIEVAL_LOINC_CODES),
                    "_pretty": "false",
                    "_count": str(BATCH_SIZE),
                    **({"date": "ge" + DISEASE_START_DATE} if FILTER_RESOURCES_BY_DATE else {}),
                },
            )
        )
        if USE_OBSERVATIONS
        else []
    )
    observations = [o for o in observations_raw if is_valid_observation(o)]

    conditions = (
        list(
            search(
                "Condition",
                {
                    "code": ",".join(DISEASE_ICD_CODES),
                    "_pretty": "false",
                    "_count": str(BATCH_SIZE),
                    **(
                        {"recorded-date": "ge" + DISEASE_START_DATE}
                        if FILTER_RESOURCES_BY_DATE
                        else {}
                    ),
                },
            )
        )
        if USE_CONDITIONS
        else []
    )

    obs_pids = {ref_id(o.get("subject")) for o in observations}
    condition_pids = {ref_id(c.get("subject")) for c in conditions}
    all_source_pids = clean_set(obs_pids | condition_pids)

    positive_obs_pids = {
        ref_id(o.get("subject"))
        for o in observations
        if is_influenza_observation(o) and is_positive_observation(o)
    }
    positive_condition_pids = {
        ref_id(c.get("subject")) for c in conditions if is_influenza_condition(c)
    }
    positive_source_pids = clean_set(positive_obs_pids | positive_condition_pids)

    patient_ids_for_retrieval = (
        positive_source_pids
        if FILTER_PATIENT_RETRIEVAL and positive_source_pids
        else all_source_pids
    )

    patients = fetch_by_chunks("Patient", "_id", sorted(patient_ids_for_retrieval))
    patient_by_id = {p.get("id"): p for p in patients if p.get("id")}

    encounters = fetch_by_chunks(
        "Encounter",
        "subject",
        sorted(patient_ids_for_retrieval),
        {
            "_count": str(BATCH_SIZE),
            **(
                {"date": "ge" + DISEASE_START_DATE + "T00:00:00"}
                if FILTER_RESOURCES_BY_DATE
                else {}
            ),
        },
    )
    encounters = [e for e in encounters if is_valid_encounter(e)]
    encounter_by_id = {e.get("id"): e for e in encounters if e.get("id")}
    condition_encounter_by_diagnosis = (
        link_conditions_to_encounters_by_diagnosis(conditions, encounters)
        if USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS
        else {}
    )

    positive_encounter_ids_from_obs = {
        ref_id(o.get("encounter"))
        for o in observations
        if is_influenza_observation(o) and is_positive_observation(o)
    }
    positive_encounter_ids_from_conditions = {
        condition_encounter_id(c, condition_encounter_by_diagnosis)
        for c in conditions
        if is_influenza_condition(c)
    }
    positive_encounter_ids = clean_set(
        positive_encounter_ids_from_obs | positive_encounter_ids_from_conditions
    )

    positive_visit_numbers = {
        visit_number(encounter_by_id[enc_id])
        for enc_id in positive_encounter_ids
        if enc_id in encounter_by_id and visit_number(encounter_by_id[enc_id])
    }

    flagged_encounters = [
        e for e in encounters if visit_number(e) and visit_number(e) in positive_visit_numbers
    ]
    flagged_facility_contacts = [e for e in flagged_encounters if is_facility_contact(e)]
    positive_pids = clean_set(ref_id(e.get("subject")) for e in flagged_facility_contacts)

    pids_by_gender = {
        "Male": sorted(pid for pid in positive_pids if matches_gender(patient_by_id.get(pid), "male")),
        "Female": sorted(
            pid for pid in positive_pids if matches_gender(patient_by_id.get(pid), "female")
        ),
        "Diverse": sorted(
            pid for pid in positive_pids if matches_gender(patient_by_id.get(pid), "diverse")
        ),
    }

    output = {
        OUTPUT_ITEM_NAME: {gender: len(pids) for gender, pids in pids_by_gender.items()},
        "debug": {
            "observations_raw": len(observations_raw),
            "observations_after_status_filter": len(observations),
            "conditions_raw": len(conditions),
            "source_patient_ids_all": len(all_source_pids),
            "source_patient_ids_positive": len(positive_source_pids),
            "patients_loaded": len(patients),
            "encounters_loaded_valid_status": len(encounters),
            "facility_contacts_loaded": sum(1 for e in encounters if is_facility_contact(e)),
            "positive_observation_patient_ids": len(clean_set(positive_obs_pids)),
            "positive_condition_patient_ids": len(clean_set(positive_condition_pids)),
            "positive_observation_encounter_ids": len(clean_set(positive_encounter_ids_from_obs)),
            "positive_condition_encounter_ids": len(
                clean_set(positive_encounter_ids_from_conditions)
            ),
            "conditions_linked_via_encounter_diagnosis": len(condition_encounter_by_diagnosis),
            "positive_encounter_ids_total": len(positive_encounter_ids),
            "positive_encounter_ids_loaded": sum(1 for x in positive_encounter_ids if x in encounter_by_id),
            "positive_encounter_ids_missing_in_loaded_encounters": len(
                [x for x in positive_encounter_ids if x not in encounter_by_id]
            ),
            "positive_visit_numbers": len(clean_set(positive_visit_numbers)),
            "flagged_encounters_by_vn": len(flagged_encounters),
            "flagged_facility_contacts_by_vn": len(flagged_facility_contacts),
            "positive_patient_ids_counted_by_gender_item": len(positive_pids),
            "positive_observations_without_encounter": sum(
                1
                for o in observations
                if is_influenza_observation(o) and is_positive_observation(o) and not ref_id(o.get("encounter"))
            ),
            "positive_conditions_without_encounter": sum(
                1 for c in conditions if is_influenza_condition(c) and not ref_id(c.get("encounter"))
            ),
            "positive_conditions_without_any_encounter_link": sum(
                1
                for c in conditions
                if is_influenza_condition(c)
                and not condition_encounter_id(c, condition_encounter_by_diagnosis)
            ),
            "encounters_without_vn_identifier": sum(1 for e in encounters if not visit_number(e)),
            "mimic_ddp_obs_interpretation_removal": MIMIC_DDP_OBS_INTERPRETATION_REMOVAL,
            "filter_patient_retrieval": FILTER_PATIENT_RETRIEVAL,
            "batch_size": BATCH_SIZE,
            "id_chunk_size": ID_CHUNK_SIZE,
            "use_post_for_id_search": USE_POST_FOR_ID_SEARCH,
            "use_encounter_diagnosis_for_conditions": USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS,
            "use_observations": USE_OBSERVATIONS,
            "use_conditions": USE_CONDITIONS,
            "disease_positive_loinc_codes": len(DISEASE_POSITIVE_LOINC_CODES),
            "disease_retrieval_loinc_codes": len(DISEASE_RETRIEVAL_LOINC_CODES),
            "disease_icd_codes": len(DISEASE_ICD_CODES),
        },
        "debug_patient_ids": pids_by_gender,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def search(resource_type: str, params: dict[str, str], use_post: bool = False):
    url = make_search_url(resource_type, use_post)
    body = urllib.parse.urlencode(params, safe=",:").encode("utf-8") if use_post else None
    if not use_post:
        url = url + "?" + urllib.parse.urlencode(params, safe=",:")

    while url:
        bundle = get_json(url, body)
        body = None
        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                yield resource
        url = next_url(bundle)


def fetch_by_chunks(
    resource_type: str, search_param: str, ids: list[str], extra: dict[str, str] | None = None
) -> list[dict]:
    resources: list[dict] = []
    for chunk in chunks(ids, ID_CHUNK_SIZE):
        if not chunk:
            continue
        params = {search_param: ",".join(chunk), "_count": str(BATCH_SIZE)}
        if extra:
            params.update(extra)
        resources.extend(search(resource_type, params, use_post=USE_POST_FOR_ID_SEARCH))
    return resources


def get_json(url: str, body: bytes | None = None) -> dict:
    token = base64.b64encode(f"{FHIR_USER}:{FHIR_PASSWORD}".encode("utf-8")).decode("ascii")
    headers = {
        "Accept": "application/fhir+json",
        "Authorization": f"Basic {token}",
    }
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"FHIR request failed: {url}\n{exc}", file=sys.stderr)
        raise


def make_search_url(resource_type: str, use_post: bool) -> str:
    base = FHIR_BASE_URL.rstrip("/") + "/" + resource_type
    return base + "/_search" if use_post else base


def next_url(bundle: dict) -> str | None:
    for link in bundle.get("link", []):
        if link.get("relation") == "next":
            return link.get("url")
    return None


def is_valid_observation(obs: dict) -> bool:
    status = obs.get("status")
    return bool(status) and status not in INVALID_OBSERVATION_STATUS


def is_valid_encounter(encounter: dict) -> bool:
    return encounter.get("status") in VALID_ENCOUNTER_STATUS


def is_influenza_observation(obs: dict) -> bool:
    return has_code(obs.get("code"), LOINC_SYSTEM, set(DISEASE_POSITIVE_LOINC_CODES))


def is_influenza_condition(condition: dict) -> bool:
    return has_code(condition.get("code"), ICD_SYSTEM, set(DISEASE_ICD_CODES))


def condition_encounter_id(condition: dict, linked_encounters: dict[str, str]) -> str | None:
    return ref_id(condition.get("encounter")) or linked_encounters.get(condition.get("id"))


def link_conditions_to_encounters_by_diagnosis(
    conditions: list[dict], encounters: list[dict]
) -> dict[str, str]:
    conditions_needing_assignment = {
        c.get("id")
        for c in conditions
        if c.get("id") and not ref_id(c.get("encounter")) and is_influenza_condition(c)
    }
    remaining = set(conditions_needing_assignment)
    linked: dict[str, str] = {}

    for encounter in encounters:
        encounter_id = encounter.get("id")
        if not encounter_id or not is_facility_contact(encounter):
            continue
        for diagnosis in encounter.get("diagnosis", []):
            cond_id = ref_id(diagnosis.get("condition"))
            if not cond_id or cond_id not in remaining:
                continue
            linked[cond_id] = encounter_id
            remaining.remove(cond_id)
            if not remaining:
                return linked
    return linked


def is_positive_observation(obs: dict) -> bool:
    if has_code(obs.get("valueCodeableConcept"), SNOMED_SYSTEM, POSITIVE_VALUE_CODES):
        return True
    if MIMIC_DDP_OBS_INTERPRETATION_REMOVAL:
        return False
    return not has_any_value(obs) and any(
        has_code(cc, OBS_INTERPRETATION_SYSTEM, POSITIVE_INTERPRETATION_CODES)
        for cc in obs.get("interpretation", [])
    )


def has_any_value(resource: dict) -> bool:
    return any(k.startswith("value") and v is not None for k, v in resource.items())


def has_code(codeable: dict | None, system: str, codes: set[str]) -> bool:
    if not codeable:
        return False
    return any(c.get("system") == system and c.get("code") in codes for c in codeable.get("coding", []))


def ref_id(reference: dict | None) -> str | None:
    if not reference:
        return None
    text = reference.get("reference")
    if text:
        if text.endswith("/"):
            return None
        return text.rsplit("/", 1)[-1]
    identifier = reference.get("identifier") or {}
    return identifier.get("value")


def visit_number(encounter: dict) -> str | None:
    for identifier in encounter.get("identifier", []):
        id_type = identifier.get("type") or {}
        if has_code(id_type, IDENTIFIER_VN_TYPE_SYSTEM, {"VN"}):
            return identifier.get("value")
    return None


def is_facility_contact(encounter: dict) -> bool:
    types = encounter.get("type")
    if not types:
        return True
    return any(has_code(t, ENCOUNTER_CONTACT_LEVEL_SYSTEM, {"einrichtungskontakt"}) for t in types)


def matches_gender(patient: dict | None, gender: str) -> bool:
    if not patient or "gender" not in patient:
        return False
    if gender in {"male", "female"}:
        return patient.get("gender", "").lower() == gender
    if patient.get("gender", "").lower() != "other":
        return False
    for ext in (patient.get("_gender") or {}).get("extension", []):
        if ext.get("url") != GENDER_EXTENSION_URL:
            continue
        coding = ext.get("valueCoding") or {}
        if coding.get("code", "").upper() == "D":
            return True
    return False


def clean_set(values) -> set[str]:
    return {x for x in values if x}


def chunks(values: list[str], size: int):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


if __name__ == "__main__":
    main()
