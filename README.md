# DDP FHIR Debug Tools

Kleines Hilfsskript zum Nachrechnen von `infl.cumulative.gender` gegen einen FHIR-Server.

Das Skript bildet die DDP-v0.5.7-Logik fuer Influenza nach:

- Influenza-Observations per LOINC ab `2022-09-01`
- Influenza-Conditions per ICD `J10.0`, `J10.1`, `J10.8`, `J09`
- Patient- und Encounter-Retrieval wie im DDP
- positives Encounter-Flagging ueber `Encounter.identifier` Slice `VN`
- optionaler Condition-Link ueber `Encounter.diagnosis.condition`
- Zaehlen von `Male`, `Female`, `Diverse` ueber positiv markierte Einrichtungskontakte

## Nutzung

In `ddp_cum_items.py` oben diese Werte setzen:

```python
FHIR_BASE_URL = "http://localhost:8080/fhir"
FHIR_USER = "user"
FHIR_PASSWORD = "password"
```

Dann ausfuehren:

```powershell
python .\ddp_cum_items.py
```

## Wichtige Schalter

```python
BATCH_SIZE = 500
ID_CHUNK_SIZE = 50
USE_POST_FOR_ID_SEARCH = True
USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS = True
FILTER_PATIENT_RETRIEVAL = True
MIMIC_DDP_OBS_INTERPRETATION_REMOVAL = True
```

`USE_POST_FOR_ID_SEARCH = True` verhindert zu lange URLs bei Suchen wie `Encounter?subject=id1,id2,...`.

`USE_ENCOUNTER_DIAGNOSIS_FOR_CONDITIONS = True` ist wichtig, wenn `Condition.encounter` fehlt und die Zuordnung ueber `Encounter.diagnosis.condition` erfolgt.

`MIMIC_DDP_OBS_INTERPRETATION_REMOVAL = True` bildet nach, dass der DDP bei FHIR-Observation-Retrieval aktuell `Observation.interpretation` entfernt. Zum Gegencheck kann der Wert auf `False` gesetzt werden.

## Debug-Ausgabe

Der JSON-Output enthaelt neben dem Item auch Debug-Zaehler, u.a.:

- `conditions_linked_via_encounter_diagnosis`
- `positive_encounter_ids_loaded`
- `positive_encounter_ids_missing_in_loaded_encounters`
- `encounters_without_vn_identifier`
- `positive_conditions_without_any_encounter_link`
- `flagged_facility_contacts_by_vn`
- `positive_patient_ids_counted_by_gender_item`

Damit laesst sich nachvollziehen, an welcher DDP-Logik Patienten verloren gehen.
