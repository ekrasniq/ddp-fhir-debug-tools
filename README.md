# DDP FHIR Debug Tools

Kleine Hilfsskripte zum Nachrechnen von Influenza-DDP-Items gegen einen FHIR-Server.

`ddp_cum_items.py` bildet die DDP-v0.5.7-Logik fuer `infl.cumulative.gender` nach:

- Influenza-Observations per LOINC ab `2022-09-01`
- Influenza-Conditions per ICD `J10.0`, `J10.1`, `J10.8`, `J09`
- Patient- und Encounter-Retrieval wie im DDP
- positives Encounter-Flagging ueber `Encounter.identifier` Slice `VN`
- optionaler Condition-Link ueber `Encounter.diagnosis.condition`
- Zaehlen von `Male`, `Female`, `Diverse` ueber positiv markierte Einrichtungskontakte

`ddp_infl_maxtreatment_items.py` nutzt dieselbe Influenza-Kohorte und gibt die
Maxtreatment-Items als DDP-aehnliche `DiseaseDataItem`-Liste aus, also mit
`itemname`, `itemtype` und `data`.

`ddp_covid_items.py` nutzt dieselbe Maxtreatment-Logik fuer COVID und gibt
`cumulative.gender` plus die COVID-Maxtreatment-Items aus. COVID-Itemnamen sind
wie im DDP nicht mit `infl.` geprefixt.

## Nutzung

Im jeweiligen Skript oben diese Werte setzen:

```python
FHIR_BASE_URL = "http://localhost:8080/fhir"
FHIR_USER = "user"
FHIR_PASSWORD = "password"
```

Dann ausfuehren:

```powershell
python .\ddp_cum_items.py
```

Fuer Maxtreatment:

```powershell
python .\ddp_infl_maxtreatment_items.py
```

Fuer COVID cumulative gender und Maxtreatment:

```powershell
python .\ddp_covid_items.py
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

## Maxtreatment-Schalter

In `ddp_infl_maxtreatment_items.py` und `ddp_covid_items.py` sind zusaetzlich diese DDP-Defaults gesetzt:

```python
USE_PART_OF_INSTEAD_OF_IDENTIFIER = False
USE_ICU_UNDIFFERENTIATED = False
CHECK_PROCEDURES_ICU_STAYS = True
DDP_DEBUG = False
INCLUDE_DIAGNOSTICS = False
ICU_SERVICE_PROVIDER_IDS = set()
ADDITIONAL_ICU_LOCATION_IDS = set()
```

`ICU_SERVICE_PROVIDER_IDS` entspricht `global.service-provider-identifier-of-icu-locations`.
Wenn der DDP ICU ueber `Encounter.serviceProvider` erkennt, hier dieselben Werte eintragen.

`ADDITIONAL_ICU_LOCATION_IDS` ist nur ein Debug-Override, falls Location-Ressourcen nicht per
`Location?_id=...` geladen werden koennen.

Mit `DDP_DEBUG = True` werden zusaetzlich die DDP-Debug-Items `*.debug` erzeugt.
Mit `INCLUDE_DIAGNOSTICS = True` wird die Ausgabe in `{ "data_items": ..., "diagnostics": ... }`
gepackt und enthaelt die internen Zwischenzaehler.

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

Beim Maxtreatment-Skript sind diese Felder nur sichtbar, wenn `INCLUDE_DIAGNOSTICS = True`
gesetzt ist:

- `ddp_would_exclude_maxtreatment_items`
- `supply_contacts_loaded_inpatient_or_shortstay`
- `locations_loaded`
- `dummy_icu_location_available`
- `flagged_facility_contacts_by_vn`
- `flagged_facility_contacts_missing_period_start`
- `icu_supply_contacts_positive`
- `procedures_after_icu_ward_filter`
- `counts_before_final_duplicate_removal`
- `debug_case_ids_by_treatmentlevel`
