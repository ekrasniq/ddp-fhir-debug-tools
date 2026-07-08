#!/usr/bin/env python3
"""Print distinct case numbers for patients with influenza Conditions.

The script reads influenza Conditions, loads the patients' Encounters, and extracts
case numbers from Encounter.identifier VN and Encounter.account.
"""

from __future__ import annotations

from collections import defaultdict
import json

import ddp_cum_items as cum


# Set these values for your FHIR endpoint.
FHIR_BASE_URL = "http://localhost:8080/fhir"
FHIR_USER = "user"
FHIR_PASSWORD = "password"

BATCH_SIZE = 500
ID_CHUNK_SIZE = 50
USE_POST_FOR_ID_SEARCH = True

# Use "," for comma-separated output or " " for space-separated output.
OUTPUT_SEPARATOR = ","

FILTER_CONDITIONS_BY_RECORDED_DATE = True
FILTER_ENCOUNTERS_BY_DATE = True
VALID_ENCOUNTER_STATUS_ONLY = True
ONLY_FACILITY_CONTACTS = True

# Account resources are fetched if Encounter.account.reference points to Account/<id>.
FETCH_ACCOUNT_RESOURCES = True
USE_ACCOUNT_REFERENCE_ID = True
USE_ACCOUNT_DISPLAY = False

# If true, prints JSON with source details instead of only the case-number line.
INCLUDE_DIAGNOSTICS = False


def main() -> None:
    configure_cumulative_module()

    conditions = fetch_influenza_conditions()
    patient_ids = cum.clean_set(cum.ref_id(c.get("subject")) for c in conditions)
    encounters = fetch_patient_encounters(patient_ids)
    account_by_id = fetch_accounts_by_encounter_refs(encounters) if FETCH_ACCOUNT_RESOURCES else {}

    case_number_details = collect_case_numbers(encounters, account_by_id)
    case_numbers = sorted(case_number_details)

    if INCLUDE_DIAGNOSTICS:
        print(
            json.dumps(
                {
                    "summary": {
                        "conditions": len(conditions),
                        "influenza_patient_ids": len(patient_ids),
                        "encounters_loaded": len(encounters),
                        "account_resources_loaded": len(account_by_id),
                        "case_numbers": len(case_numbers),
                        "output_separator": OUTPUT_SEPARATOR,
                        "filter_conditions_by_recorded_date": FILTER_CONDITIONS_BY_RECORDED_DATE,
                        "filter_encounters_by_date": FILTER_ENCOUNTERS_BY_DATE,
                        "valid_encounter_status_only": VALID_ENCOUNTER_STATUS_ONLY,
                        "only_facility_contacts": ONLY_FACILITY_CONTACTS,
                        "influenza_start_date": cum.INFLUENZA_START_DATE,
                        "influenza_icd_codes": list(cum.INFLUENZA_ICD_CODES),
                    },
                    "case_numbers": case_numbers,
                    "case_number_details": [
                        {
                            "case_number": case_number,
                            "sources": sorted(details["sources"]),
                            "patient_ids": sorted(details["patient_ids"]),
                            "encounter_ids": sorted(details["encounter_ids"]),
                        }
                        for case_number, details in sorted(case_number_details.items())
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    print(OUTPUT_SEPARATOR.join(case_numbers))


def configure_cumulative_module() -> None:
    cum.FHIR_BASE_URL = FHIR_BASE_URL
    cum.FHIR_USER = FHIR_USER
    cum.FHIR_PASSWORD = FHIR_PASSWORD
    cum.BATCH_SIZE = BATCH_SIZE
    cum.ID_CHUNK_SIZE = ID_CHUNK_SIZE
    cum.USE_POST_FOR_ID_SEARCH = USE_POST_FOR_ID_SEARCH
    cum.DISEASE_START_DATE = cum.INFLUENZA_START_DATE
    cum.DISEASE_ICD_CODES = list(cum.INFLUENZA_ICD_CODES)


def fetch_influenza_conditions() -> list[dict]:
    params = {
        "code": ",".join(cum.INFLUENZA_ICD_CODES),
        "_pretty": "false",
        "_count": str(BATCH_SIZE),
    }
    if FILTER_CONDITIONS_BY_RECORDED_DATE:
        params["recorded-date"] = "ge" + cum.INFLUENZA_START_DATE

    return [
        condition
        for condition in cum.search("Condition", params)
        if cum.is_influenza_condition(condition)
    ]


def fetch_patient_encounters(patient_ids: set[str]) -> list[dict]:
    extra = {"_count": str(BATCH_SIZE)}
    if FILTER_ENCOUNTERS_BY_DATE:
        extra["date"] = "ge" + cum.INFLUENZA_START_DATE + "T00:00:00"

    encounters = cum.fetch_by_chunks("Encounter", "subject", sorted(patient_ids), extra)
    if VALID_ENCOUNTER_STATUS_ONLY:
        encounters = [e for e in encounters if cum.is_valid_encounter(e)]
    if ONLY_FACILITY_CONTACTS:
        encounters = [e for e in encounters if cum.is_facility_contact(e)]
    return encounters


def fetch_accounts_by_encounter_refs(encounters: list[dict]) -> dict[str, dict]:
    account_ids = sorted(
        {
            account_id
            for encounter in encounters
            for account_id in account_reference_ids(encounter)
            if account_id
        }
    )
    accounts = cum.fetch_by_chunks("Account", "_id", account_ids)
    return {account.get("id"): account for account in accounts if account.get("id")}


def collect_case_numbers(
    encounters: list[dict], account_by_id: dict[str, dict]
) -> dict[str, dict[str, set[str]]]:
    details: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"sources": set(), "patient_ids": set(), "encounter_ids": set()}
    )

    for encounter in encounters:
        patient_id = cum.ref_id(encounter.get("subject"))
        encounter_id = encounter.get("id")
        for case_number, source in case_number_candidates(encounter, account_by_id):
            if not case_number:
                continue
            details[case_number]["sources"].add(source)
            add_if_present(details[case_number]["patient_ids"], patient_id)
            add_if_present(details[case_number]["encounter_ids"], encounter_id)

    return dict(details)


def case_number_candidates(
    encounter: dict, account_by_id: dict[str, dict]
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    vn = normalize(cum.visit_number(encounter))
    if vn:
        candidates.append((vn, "encounter_identifier_vn"))

    for account_ref in encounter.get("account", []):
        identifier_value = normalize((account_ref.get("identifier") or {}).get("value"))
        if identifier_value:
            candidates.append((identifier_value, "encounter_account_identifier"))

        reference_id = normalize(cum.ref_id(account_ref))
        if reference_id and USE_ACCOUNT_REFERENCE_ID:
            candidates.append((reference_id, "encounter_account_reference"))

        display = normalize(account_ref.get("display"))
        if display and USE_ACCOUNT_DISPLAY:
            candidates.append((display, "encounter_account_display"))

        account = account_by_id.get(reference_id)
        if account:
            candidates.extend(account_identifier_candidates(account))

    return candidates


def account_identifier_candidates(account: dict) -> list[tuple[str, str]]:
    return [
        (value, "account_resource_identifier")
        for value in (
            normalize(identifier.get("value"))
            for identifier in account.get("identifier", [])
        )
        if value
    ]


def account_reference_ids(encounter: dict) -> list[str]:
    return [
        account_id
        for account_id in (
            normalize(cum.ref_id(account_ref)) for account_ref in encounter.get("account", [])
        )
        if account_id
    ]


def normalize(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def add_if_present(values: set[str], value: str | None) -> None:
    if value:
        values.add(value)


if __name__ == "__main__":
    main()
