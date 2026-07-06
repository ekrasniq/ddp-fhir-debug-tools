#!/usr/bin/env python3
"""Generate COVID cumulative.gender and maxtreatmentlevel items with DDP-like FHIR logic."""

from __future__ import annotations

import ddp_infl_maxtreatment_items as items


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

COVID_START_DATE = "2020-01-27"
COVID_ICD_CODES = ["U07.1"]

COVID_PCR_LOINC_CODES = """
96957-6,94306-8,94640-0,96765-3,96763-8,96986-5,95409-9,94760-6,
94533-7,95425-5,94766-3,94316-7,97098-8,98132-4,98494-8,94559-2,
95824-9,94639-2,98131-6,98493-0,94534-5,96120-1,96123-5,96091-4,
94314-2,105749-6,94759-8,94845-5,101289-7,105748-8,94767-1,94641-8,
96448-6,96958-4,106617-4,95406-5,94565-9,96797-6,95608-6,94500-6,
94660-8,108180-1,94309-2,96829-7,94756-4,94757-2,94307-6,94308-4
""".replace("\n", "").replace(" ", "").split(",")

COVID_VARIANT_LOINC_CODES = "96895-8,96741-4,100156-9".split(",")


def main() -> None:
    items.FHIR_BASE_URL = FHIR_BASE_URL
    items.FHIR_USER = FHIR_USER
    items.FHIR_PASSWORD = FHIR_PASSWORD
    items.BATCH_SIZE = BATCH_SIZE
    items.ID_CHUNK_SIZE = ID_CHUNK_SIZE
    items.USE_POST_FOR_ID_SEARCH = USE_POST_FOR_ID_SEARCH
    items.USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS = USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS
    items.FILTER_PATIENT_RETRIEVAL = FILTER_PATIENT_RETRIEVAL
    items.FILTER_RESOURCES_BY_DATE = FILTER_RESOURCES_BY_DATE
    items.MIMIC_DDP_OBS_INTERPRETATION_REMOVAL = MIMIC_DDP_OBS_INTERPRETATION_REMOVAL

    items.USE_PART_OF_INSTEAD_OF_IDENTIFIER = USE_PART_OF_INSTEAD_OF_IDENTIFIER
    items.USE_ICU_UNDIFFERENTIATED = USE_ICU_UNDIFFERENTIATED
    items.CHECK_PROCEDURES_ICU_STAYS = CHECK_PROCEDURES_ICU_STAYS
    items.DDP_DEBUG = DDP_DEBUG
    items.INCLUDE_DIAGNOSTICS = INCLUDE_DIAGNOSTICS
    items.ICU_SERVICE_PROVIDER_IDS = set(ICU_SERVICE_PROVIDER_IDS)
    items.ADDITIONAL_ICU_LOCATION_IDS = set(ADDITIONAL_ICU_LOCATION_IDS)

    # COVID item labels are unprefixed in the DDP output, unlike influenza's "infl." labels.
    items.INFL_PREFIX = ""
    items.DISEASE_START_DATE = COVID_START_DATE
    items.DISEASE_ICD_CODES = list(COVID_ICD_CODES)
    items.DISEASE_POSITIVE_LOINC_CODES = list(COVID_PCR_LOINC_CODES)
    items.DISEASE_RETRIEVAL_LOINC_CODES = list(COVID_PCR_LOINC_CODES + COVID_VARIANT_LOINC_CODES)
    items.INCLUDE_CUMULATIVE_GENDER_ITEM = True
    items.KNOWN_CONTEXT = (
        "COVID DDP-like debug run. Retrieval includes COVID PCR and variant LOINCs, "
        "but positive case detection uses PCR LOINCs only, matching the DDP logic."
    )

    items.main()


if __name__ == "__main__":
    main()
