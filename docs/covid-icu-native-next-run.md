# Naechster Lauf: nativer DDP gegen Kontaktart-ICU

Einstieg: **`ddp_covid_icu_gitops_test.py`**. Python 3.10+, Standardbibliothek;
das komplette Repository im Kliniknetz verwenden. FHIR-Zugriff ist ausschliesslich
lesend (GET/POST `/_search`); nur lokale Arbeits-/Ergebnisdateien werden geschrieben.
Der Lauf aendert weder FHIR-Ressourcen noch GitOps/Kubernetes und startet keinen DDP neu.

Die Voreinstellungen entsprechen der am 12.10.2026 mit Kustomize lokal geprueften
Konfiguration von `numcodex-dashboarddataprocessor-test`:

| Einstellung | Voreinstellung |
| --- | --- |
| PCR-LOINCs | 94640-0, 94306-8, 96763-8, 94500-6, 94558-4 |
| Variant-LOINCs fuer Retrieval | 96895-8, 96741-4 plus die fuenf PCR-Codes |
| Conditions | `_revinclude=Encounter:diagnosis` |
| partOf | false; VN-Zuordnung |
| ICU | nativer Location-Pfad und Kontaktart-Gegenprobe |
| FHIR-Seite / Location-Batch | 150 Ressourcen / 150 IDs |
| Patienten pro Encounter-Suche | 25 |
| Patienten-/Suchdatumsfilter | true, entsprechend den Java-Defaults |
| berichteter DDP-Vergleichswert | 65 |

Alle Voreinstellungen lassen sich per CLI uebersteuern und stehen im JSON-Bericht.
65 ist ein manueller Referenzwert, kein erzwungenes Skriptergebnis; mit
`--reference-ddp-count` bei Bedarf aktualisieren. Interner Endpunkt und Credentials
sind keine Voreinstellungen.

## Im Kliniknetz starten

Den Folge-Commit ueber Git holen. Dann die lokale FHIR-Adresse und Authentifizierung
wie im vorherigen Lauf setzen; Platzhalter durch eure Werte ersetzen.
Bei Basic Auth wird das Passwort verdeckt abgefragt, falls `FHIR_PASSWORD` nicht
lokal gesetzt ist.

```bash
export FHIR_BASE_URL='https://FHIR-SERVER.EXAMPLE/fhir'
export FHIR_USER='FHIR-BENUTZER'
python3 ddp_covid_icu_gitops_test.py --auth basic --work-db icu-native-work.sqlite --keep-work-db > icu-native-comparison.json
```

PowerShell:

```powershell
$env:FHIR_BASE_URL = 'https://FHIR-SERVER.EXAMPLE/fhir'
$env:FHIR_USER = 'FHIR-BENUTZER'
python .\ddp_covid_icu_gitops_test.py --auth basic --work-db icu-native-work.sqlite --keep-work-db > icu-native-comparison.json
```

Bei anonymem Zugriff `--auth none`; fuer Bearer Auth lokal `FHIR_BEARER_TOKEN`
setzen und `--auth bearer` verwenden. Credentials nicht in Git schreiben.
Existierende Arbeitsdatenbanken/Detail-CSVs werden nicht ueberschrieben;
fuer einen neuen Download neue Namen waehlen.

Seiten, Patienten- und Location-Batches laufen nacheinander. Es werden referenzierte
Location-IDs geladen, kein Location-Gesamtexport. Gespeichert werden nur kompakte
Prueffelder/Referenzen in SQLite, keine kompletten Ressourcenlisten im RAM.
Beide ICU-Erkennungen rechnen auf einem gemeinsamen eingelesenen Datenstand.

## Die entscheidenden Ausgabefelder

Der native Patientenzaehler steht unter:

```text
runs.ddp_configured.without_partof.data_item.data.ICU_undifferentiated
```

Die Kontaktart-Gegenprobe mit exakt derselben Quellkonfiguration steht unter:

```text
runs.ddp_configured.without_partof.contactart_countercheck.data_item.data.ICU_undifferentiated
```

`ddp_count_comparison` zeigt die Differenz zur 65.
`icu_detection_comparison.ddp_configured.without_partof` zeigt Patienten nur bei
Kontaktart, nur beim nativen Pfad oder bei beiden. `baseline_patient_query_count`
bleibt die bisherige Patienten-FHIR-Abfrage (zuletzt 3.841).

Unter `runs.ddp_configured.without_partof.diagnostics`:

| Feld | Bedeutung |
| --- | --- |
| `native_icu_via_real_location_vk` | VK mit geladener, vom DDP erkannter ICU-Location |
| `native_icu_via_contactart_fallback_vk` | VK ohne Location, ueber exakte Kontaktart erkannt |
| `contactart_icu_vk_rejected_by_native` | Kontaktart-ICU-VK, die der native Pfad verwirft |
| `contactart_icu_vk_rejection_reasons` | priorisierte Location-Verlustgruende fuer diese VK |
| `positive_contactart_icu_vk_rejection_reasons` | dieselben Gruende, nur fuer bereits COVID-positive VK |
| `positive_inpatient_icu_ek` | passende positive stationaere/SS-EK vor Patienten-Deduplikation |
| `vn_selection_bounds` | konservative Patienten-Unter-/Obergrenze wegen mehrdeutiger VN-Zuordnung |

Die Location-Gruende sind `no_literal_location_reference`, `location_not_loaded`,
`location_missing_physical_type`, `physical_type_not_ward`,
`icu_code_only_in_later_type_concept`, `location_type_not_icu` und
`contactart_fallback_not_exact`. Sie zaehlen VK, nicht Patienten. Bei mehreren
Problemen wird der erste passende Grund vergeben. Die allgemeinen
`baseline_exclusive_loss_reasons` bleiben patientenbezogen.

Wenn ueberhaupt keine verwendbaren Location-/Dummy-Ressourcen vorhanden sind,
stehen `native_item_locations_gate_satisfied=false` und `data_item=null` im Bericht.
Das bedeutet Item-Ausschluss, keinen bestaetigten Patientenzaehler von null.

## Verifizierte Modellbesonderheiten

Die relevanten Source-Archive wurden aus dem oeffentlichen Build-Tag
`V0.5.7-update.4`, Commit `42185bbe131773f1407f27f49f2fd5c50abb4536`, gelesen:
[Build-Repository](https://git.med.uni-heidelberg.de/imi/public/medic/infrastructure/external-docker-images/num-codex-dashboard-data-processor/-/tree/V0.5.7-update.4).
Der laufende Pod/Registry-Digest wurde nicht geprueft.

- Location verlangt `physicalType=wa` und `type=ICU/PEDICU/PEDNICU`. Der Java-Helfer
  prueft dabei die Codes ohne System-URI und entscheidet bereits am ersten
  CodeableConcept mit nichtleeren Codings. Spaetere ICU-Codes koennen verloren gehen.
- Die Kontaktart erzeugt nur ohne vorhandene Location eine virtuelle ICU-Location.
  Eine unpassende oder nicht geladene Location aktiviert diesen Fallback nicht.
- Der Kontaktart-Helfer haengt Codes zusammen und verlangt exakt `intensivstationaer`.
  Doppelte/mehrere Kontaktart-Codes koennen den Fallback blockieren.
- Service-provider-Fallback ist in diesem Preset deaktiviert. Optional kann
  `--icu-service-provider-ids` die entsprechenden DDP-IDs vorgeben; auch dies greift
  nur ohne Location. Dummy-Registrierung und der Location-Item-Gate werden nachgebildet.
- Historische Location-Perioden bzw. inaktive Locations werden fuer dieses
  kumulative Item nicht wegen fehlender Aktualitaet entfernt.
- Danach gelten weiterhin positive VN, EK-Zuordnung, stationaer/SS und Patienten-
  Deduplikation. Native ICU kann auch Patienten ohne Kontaktart-ICU finden; diese
  stehen gegebenenfalls unter `counted_outside_baseline`.

Mehrere EK pro VN bleiben eine Auswahlunsicherheit: DDP verwendet seine Encounter-
Reihenfolge, das Skript deterministisch die kleinste EK-ID. Deshalb wird gewarnt
und ein konservatives Zahlenintervall ausgegeben. `reference_within_vn_selection_bounds`
zeigt, ob 65 innerhalb dieses Intervalls liegt; dies beweist keine konkrete
DDP-Auswahl. Die Obergrenze ist keine zugesicherte, exakt erreichbare Maximalzahl.

## Auswertung ohne neuen FHIR-Download

Der vorgeschlagene Lauf behaelt den lokalen Snapshot. Beide partOf-Modi koennen
daraus anschliessend ohne Netzwerkzugriff gerechnet werden:

```bash
python3 ddp_covid_icu_gitops_test.py --reuse-work-db icu-native-work.sqlite --mode both > icu-native-both-partof.json
```

`FHIR_BASE_URL` muss gleich bleiben; Credentials werden fuer Snapshot-Replay nicht
benoetigt. Die Datenbank wird read-only geoeffnet, nicht veraendert oder geloescht.
PCR-/Variant-Liste, Condition-Modus, Filter und weitere Retrieval-Einstellungen
muessen zum Snapshot passen. Unvollstaendige/alte Datenbanken werden abgelehnt.
Dies ist kein Download-Resume. Fuer lokale Detaildaten:

```bash
python3 ddp_covid_icu_gitops_test.py --reuse-work-db icu-native-work.sqlite --details-csv icu-native-details.csv > icu-native-details-summary.json
```

SQLite/CSV enthalten interne IDs und bleiben im Kliniknetz. Fuer die Besprechung
reicht die aggregierte JSON-Ausgabe. Keine CSV/Datenbank oder Zugangsdaten in Git
veroeffentlichen; diese Ergebnistypen sind in `.gitignore` ausgeschlossen.

## Pruefstatus

Synthetische Unit-/Pipeline-Tests pruefen die GitOps-Voreinstellungen, den nativen
Pfad, Location-Verlustgruende, blockierte Fallbacks, Patienten nur im nativen Pfad,
VN-Grenzen und unveraenderten Snapshot-Replay. Die alten Kontaktart- und lokalen
HTTP-Paging-Tests bleiben erhalten:

```bash
python3 -m unittest discover -s tests -v
```

Kein Kliniklauf und keine GitOps-/Cluster-Aenderung wurden ausgefuehrt. Bleibt eine
Differenz zu 65, zuerst die Location-Zaehler und VN-Grenzen, dann einen zeitlich
passenden DDP-Export und dessen geladene Ressourcenzaehler vergleichen.
