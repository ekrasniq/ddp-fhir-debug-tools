# COVID: ueber 3.000 Patienten in FHIR gegen 61 im DDP

Die neuen Python-Skripte untersuchen ausschliesslich die Komponente
`cumulative.maxtreatmentlevel.data.ICU_undifferentiated`. Sie erkennen ICU wie
angefordert ueber **Encounter.type mit
`http://fhir.de/CodeSystem/kontaktart-de|intensivstationaer`**, auch wenn eine
Location vorhanden ist. Outpatient und Normal_ward werden nicht berechnet.

| Datei | Hierarchie-Zuordnung |
| --- | --- |
| `ddp_covid_icu_without_partof.py` | EK/VK ueber die erste Aufnahmenummer mit `identifier.type=VN` |
| `ddp_covid_icu_with_partof.py` | dieselbe VN-Zuordnung, zusaetzlich `VK.partOf -> AK.partOf -> EK` |
| `ddp_covid_icu_debug.py` | beide Varianten auf einem gemeinsamen eingelesenen Datenstand; empfohlen |

Der DDP nimmt ICU im Original normalerweise aus Location-Daten; die Kontaktart
ist nur ein Fallback, wenn keine Location vorhanden ist. Der explizite
Kontaktart-Vergleich hier isoliert die Fall-, Referenz- und Positivitaetslogik.
Er ist deshalb keine Zusicherung, dass das Skript exakt die 61 des laufenden
Images ausgeben wird. Ein hoeherer Skriptwert kann auch auf den nativen
ICU-/Location-Pfad des DDP hinweisen.

## Voraussetzungen und Ausfuehrung im Kliniknetz

Python **3.10 oder neuer**, Standardbibliothek; kein `pip install` erforderlich.
Das Skript muss aus diesem Repository ausgefuehrt werden, weil es die bestehende
COVID-LOINC-Konfiguration importiert. Zugriff auf den Blaze-FHIR-Endpunkt und
lokaler Speicherplatz fuer SQLite-Tabellen und temporaere Join-Tabellen sind
erforderlich. Codex hat den Lauf nicht gegen das Kliniksystem ausgefuehrt.

`FHIR_BASE_URL` durch die interne FHIR-Basis-URL ersetzen (einschliesslich `/fhir`,
falls der Server diesen Pfad verwendet). Fuer Basic Auth `FHIR_USER` lokal setzen;
das Passwort wird verdeckt abgefragt. Alternativ `FHIR_PASSWORD` lokal setzen.
Bearer Auth verwendet `FHIR_BEARER_TOKEN`. Credentials sind weder CLI-Argumente
noch Bestandteil der Ausgabe und gehoeren nicht in Git.

Beispiel fuer Linux, mit Platzhaltern:

```bash
export FHIR_BASE_URL='https://FHIR-SERVER.EXAMPLE/fhir'
export FHIR_USER='FHIR-BENUTZER'
python3 ddp_covid_icu_debug.py --auth basic --page-size 500 --patient-batch-size 25 > icu-comparison.json
```

Oder Windows/PowerShell:

```powershell
$env:FHIR_BASE_URL = 'https://FHIR-SERVER.EXAMPLE/fhir'
$env:FHIR_USER = 'FHIR-BENUTZER'
python .\ddp_covid_icu_debug.py --auth basic --page-size 500 --patient-batch-size 25 > icu-comparison.json
```

Die beiden separaten Einstiegspunkte unterstuetzen dieselben Optionen:

```bash
python3 ddp_covid_icu_without_partof.py --auth basic > icu-without-partof.json
python3 ddp_covid_icu_with_partof.py --auth basic > icu-with-partof.json
```

Separate Laeufe laden jeweils erneut; mit dem gemeinsamen Einstiegspunkt wird
nur einmal geladen. Bei anonymem internem Zugriff `--auth none` verwenden.

## Paging, Batches und Wirkung

- `--page-size 500`: maximal angeforderte Ressourcen pro FHIR-Seite. Jede Seite
  wird sofort in kompakte SQLite-Zeilen umgesetzt; ganze FHIR-Ressourcenlisten
  werden nicht fuer den gesamten Lauf im RAM gesammelt.
- `--patient-batch-size 25`: Patienten-IDs pro Patient-/Encounter-Suche. Die
  Batches laufen nacheinander. Observation- und Condition-Suchen werden ebenfalls
  vollstaendig seitenweise abgearbeitet.
- Ressourcen werden serverseitig lesend mit GET oder **POST `/_search`** gesucht.
  Ein POST auf `/_search` erzeugt keine FHIR-Ressourcen.
- `Bundle.link[relation=next]` wird unveraendert verfolgt; relative URLs werden
  aufgeloest. Auch nach POST erfolgt Paging mit GET. `Bundle.total` wird nicht
  benoetigt. Das passt zum [Blaze Search-Type API](https://blaze-server.org/api/interaction/search-type.html)
  und zu [Blaze-Paging](https://github.com/samply/blaze/blob/main/docs/api.md#paging-sessions).
- Es werden Encounter der aus COVID-Quellen ermittelten Patienten plus der
  FHIR-Vergleichskohorte geladen. Kein unbeschraenkter `Encounter`-Gesamtexport
  ueber die 31 Millionen Ressourcen.
- AK/EK werden per indizierten SQLite-Joins zugeordnet. Keine wiederholte Suche
  in allen AK/EK fuer jeden VK; keine angehaeuften Identifier-Kopien am EK.
- SQLite schreibt nur lokal. Standardmaessig wird eine neue Arbeitsdatei im
  Betriebssystem-Tempverzeichnis angelegt und beim Ende entfernt. Fuer einen
  festgelegten Ablageort `--work-db /LOKALER/PFAD/icu-work.sqlite` verwenden.
  `--keep-work-db` behaelt sie, auch bei einem abgebrochenen Lauf. Existierende
  Dateien werden nicht ueberschrieben; die Arbeitsdatei ist kein Resume-Cache.
- Fortschritt kommt auf stderr, der fertige aggregierte JSON-Bericht auf stdout.
  Bei HTTP-/Paging-Fehlern wird abgebrochen und kein fertiger Teilzaehler ausgegeben.
- `--timeout 120` ist das Timeout pro HTTP-Anfrage. TLS-Pruefung bleibt aktiv.
  Paging/Redirects auf einen anderen Host werden abgelehnt. Bei falschen absoluten
  Blaze-Links `BASE_URL` bzw. die Proxy-Konfiguration pruefen.

Die Arbeitsdatenbank enthaelt interne Patienten-, Encounter- und Referenz-IDs.
Sie bleibt im Kliniknetz. JSON-Berichte enthalten standardmaessig nur Zaehler und
Konfiguration, keine Patienten-/Fall-IDs. `*.sqlite`, `*.json`, `*.csv` und Logs
sind per `.gitignore` vom normalen Commit ausgeschlossen; keine klinischen
Ergebnisse in Git veroeffentlichen.

## Die entscheidenden Ausgabefelder

`baseline_patient_query_count` ist die seitenweise ermittelte Anzahl eindeutiger
Patienten der bestehenden [FHIR-Abfrage](../fhir-searches/covid-icu-patient-count.http):
positiver Befund mit einem der fuenf LOINCs **und irgendein** intensivstationaerer
Encounter desselben Patienten. Status, Datum, Klasse, Kontaktebene und derselbe
Fall werden dort nicht verlangt.

Jeder `runs.<LOINC-Profil>.<Modus>` enthaelt:

| Feld | Bedeutung |
| --- | --- |
| `data_item.data.ICU_undifferentiated` | eindeutige Patienten nach der nachgebildeten Itemlogik |
| `baseline_counted` | davon Patienten aus der ueber-3.000-Vergleichskohorte |
| `baseline_not_counted` | Patienten dieser Kohorte, die bei der Itemlogik fehlen |
| `counted_outside_baseline` | zusaetzliche Patienten, z.B. ueber U07.1 oder andere PCR-LOINCs |
| `baseline_exclusive_loss_reasons` | erste nicht erfuellte Pruefung je Baseline-Patient; inklusive `counted` summiert sich dies zur Baseline |
| `diagnostics` | Encounter-/VK-/EK-Zaehler, VN-Probleme und fehlende Hierarchie-Verknuepfungen |

Die Gesamtdifferenz nicht einfach als `Baseline - Item-Gesamtzahl` interpretieren:
der DDP kann auch Patienten ausserhalb der laborbasierten Baseline zaehlen.
`baseline_not_counted` ist der korrekte Verlustzaehler.

Die exklusiven Verlustgruende werden in dieser Reihenfolge vergeben:

1. `patient_resource_not_loaded`: Patient-Ressource fehlt.
2. `no_source_for_ddp_patient_retrieval`: keine Quelle fuer die DDP-Patientenauswahl
   unter der eingestellten LOINC-/Status-/Suchdatums-Konfiguration.
3. `no_positive_source_after_loinc_status_date_filters`: keine gueltige positive
   PCR-Observation bzw. U07.1-Condition nach den Positivitaetspruefungen.
4. `positive_source_encounter_missing_or_filtered`: positive Quelle ohne ladbaren,
   gueltigen Encounter; z.B. fehlende Observation.encounter, ungueltiger Status,
   fehlendes period.start oder Beginn vor dem Stichtag.
5. `positive_source_encounter_without_vn`: referenzierter Encounter vorhanden,
   aber keine verwendbare Aufnahmenummer, auch nach moeglicher partOf-Ergaenzung.
6. `no_valid_inpatient_icu_vk`: kein gueltiger stationaerer/SS-ICU-VK mit Kontaktebene.
7. `icu_vk_without_ek_mapping`: ICU-VK existiert, aber keine EK-Zuordnung.
8. `icu_vk_not_in_positive_visit_number`: ICU-VK gehoert nicht zu einer positiv
   markierten VN, z.B. COVID und ICU in unterschiedlichen Faellen oder abweichende VNs.
9. `mapped_ek_not_positive_inpatient`: zugeordneter EK ist nicht positiv und stationaer/SS.

Dies sind priorisierte Diagnosen; ein Patient kann mehrere Probleme haben.
Die Zwischenzaehler sind keine Aussage, dass genau eine Ursache allein verantwortlich ist.

`partof_comparison` zeigt die Patienten, die nur mit, nur ohne oder in beiden
Modi gezaehlt werden. `loinc_comparison` zeigt Gewinne/Verluste durch die ergaenzte
LOINC-Liste. `baseline_source_checks` prueft die Baseline anhand der tatsaechlich
geladenen Befunde und ICU-Kontakte gegen; Warnungen dort koennen auf andere
Berechtigungen, ignorierte Suchparameter oder Importe waehrend des Laufs hinweisen.

## LOINC-Profile und DDP-Konfiguration

Die wiederkehrenden lokalen COVID-LOINCs sind **94640-0, 94306-8, 96763-8,
94500-6, 94558-4**. Positive valueCodeableConcept-Codes sind SNOMED CT
10828004, 260373001 und 52101004.

Jeder Modus wird mit zwei Profilen berechnet:

- `ddp_configured`: PCR-Liste aus dem bestehenden COVID-Skript, standardmaessig
  upstream v0.5.7. `94558-4` fehlt dort.
- `ddp_plus_query_loincs`: dieselbe Liste, ergaenzt um alle fuenf lokalen LOINCs.

Wenn euer laufender DDP eine andere PCR-Liste verwendet, mit
`--ddp-pcr-loincs 'CODE1,CODE2,...'` genau diese einstellen. Die tatsaechliche Liste
steht im JSON. Der zweite Lauf veraendert keine Konfiguration des DDP.

Diese Optionen muessen zur **laufenden** DDP-Konfiguration passen:

| Skriptoption | DDP-Einstellung | Skriptstandard |
| --- | --- | --- |
| `--filter-patient-retrieval` / `--no-filter-patient-retrieval` | `fhir.search.filter-patient-retrieval` | true |
| `--filter-resources-by-date` / `--no-filter-resources-by-date` | `fhir.search.filter-resources-by-date` | true |
| `--condition-link-mode revinclude` | `fhir.search.use-encounter-condition-reference=true` | direct / false |
| kein `--keep-interpretation` | FHIR-Observation.interpretation wird entfernt | Entfernung nachgebildet |

Beide Filter sind im Java-Code von `SearchConfiguration` standardmaessig **true**,
auch wenn die kommentierten application.yaml-Beispiele false zeigen. Bei
`--no-filter-patient-retrieval` werden auch Patienten aus negativen DDP-PCR-Befunden
geladen. Das kann die Datenmenge stark erhoehen; nur setzen, wenn dies dem laufenden
DDP entspricht oder gezielt als Gegencheck gewuenscht ist. Jede Einstellung steht
im Bericht.

## Nachgebildete Besonderheiten und Grenzen

Referenzmodell sind die lokal geprueften Upstream-Repositories bei **v0.5.7**:

- [VK/EK-Zuordnung und ICU-Map](https://github.com/mwtek/dashboardlogic/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/dashboardlogic/logic/DiseaseResultFunctionality.java)
- [Positiv-Flagging](https://github.com/mwtek/dashboardlogic/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/dashboardlogic/logic/DiseaseDetectionManagement.java)
- [CumulativeMaxTreatmentLevel](https://github.com/mwtek/dashboardlogic/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/dashboardlogic/logic/cumulative/maxtreatmentlevel/CumulativeMaxTreatmentLevel.java)
- [Datums-/Statusfilter und Item-Ausgabe](https://github.com/mwtek/dashboardlogic/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/dashboardlogic/DataItemGenerator.java)
- [MiiEncounter/VN-Zugriff](https://github.com/mwtek/utilities/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/utilities/fhir/resources/MiiEncounter.java)
- [FHIR-Retrieval und Condition-Verknuepfung](https://github.com/mwtek/dashboarddataprocessor/blob/v0.5.7/src/main/java/de/ukbonn/mwtek/dashboard/misc/ResourceHandler.java)

Das Heidelberger Image `V0.5.7-update.4` konnte nicht gegen seine Build-Quellen
verifiziert werden. Der Bericht behauptet deshalb keine exakte Image-Reproduktion.

Nachgebildet werden insbesondere:

- Encounter `finished`/`in-progress`, `period.start >= 27.01.2020`; Observation
  `effectiveDateTime > Stichtag`. Standardstichtag ist Mitternacht Europe/Berlin
  im Januar (`+01:00`); `--cutoff` erlaubt eine andere DDP-Zeitzone.
- Blaze-Suchdaten ohne Zeitzone werden als UTC betrachtet. Fuer die optionale
  initiale Suchdatumspruefung wird `2020-01-27T00:00:00Z` verwendet. Ressourcen
  werden fuer die Verlustanalyse dennoch ohne Suchdatumsfilter geladen.
- Die erste identifier.type-Codierung VN wird verwendet, nicht beliebige
  `identifier.value`, `Encounter.account` oder identifier-only Reference-Werte.
- `partOf` ist zusaetzlich zur VN-Zuordnung aktiv und ueberschreibt die direkte
  EK-Zuordnung, wenn die zweistufige Kette aufgeloest werden kann. Ein direkter
  `VK.partOf -> EK`-Pfad wird nicht als zweistufige Kette akzeptiert.
- Synthetische VNs ergaenzen fehlende Identifier, ersetzen aber vorhandene/cached
  VNs nicht. Auch ein erster typisierter VN ohne value kann den neuen Identifier
  blockieren. `partof_links_with_different_effective_vns` macht dies sichtbar.
- Fehlendes Encounter.type gilt im DDP als jede Kontaktebene; dieses Verhalten
  wird nachgebildet. ICU verlangt dennoch die konkrete Kontaktart-Codierung.
- Die positive Markierung propagiert ueber VN, nicht allein ueber Patient.subject
  oder die geometrische Existenz einer partOf-Kette.
- Positive ICU-VK und positiv markierter, stationaerer/SS-EK sind beide notwendig.
  Pro Patient wird fuer den kumulativen Wert nur einmal gezaehlt. Bei optionalen
  Detaildaten wird der frueheste gezaehlte EK nach period.start ausgegeben.
- Bei Condition-revinclude werden Zuordnungen wie im Retrieval-Code auch bei
  vorhandenem Condition.encounter ueberschrieben. Der lokale Condition-Datumsfilter
  von v0.5.7 ersetzt seine Liste nicht; dieser Effekt wird bewusst beibehalten.
- Keine Beatmungs-/ECMO-Procedures: der ICU-undifferentiated-Controller ueberspringt
  deren Retrieval. Die 12-Tage-Erweiterung setzt in diesem Stand kein zusaetzliches
  POSITIVE-Flag und aendert den hier berechneten ICU-Zaehler nicht.

Bei mehreren EK mit derselben VN ist die DDP-Auswahl von der Iterationsreihenfolge
abhaengig. Das Skript waehlt deterministisch die kleinste EK-ID und warnt; solche
Ergebnisse sind keine exakte Reproduktion. Patientenuebergreifende Zuordnungen
werden ebenfalls gezaehlt. Absolute und versionierte FHIR-Referenzen werden auf
Ressourcen-IDs normalisiert; identifier-only oder contained References nicht.
effectivePeriod wird als nicht unterstuetzt diagnostiziert (der Java-Pfad erwartet
effectiveDateTime). Es erfolgt keine Pruefung der zeitlichen Ueberlappung von Test
und ICU-Aufenthalt ueber die gemeinsame Fall-Zuordnung hinaus.

Die Baseline-Abfrage, Quellabfragen und Patientenbatches sind verschiedene
Blaze-Suchsitzungen. Innerhalb jeder Sitzung ist das Paging stabil; alle Sitzungen
zusammen sind kein atomarer Gesamt-Snapshot. Fuer einen gut vergleichbaren Lauf
ein Zeitfenster ohne parallelen Import verwenden.

## Optionale fallbezogene Details und Pruefstatus

```bash
python3 ddp_covid_icu_debug.py --auth basic --details-csv /LOKALER/PFAD/icu-details.csv > icu-comparison.json
```

Die CSV wird neu angelegt und enthaelt pro Baseline-Patient, LOINC-Profil und Modus
die ID, den Verlustgrund, Beispielreferenzen und gegebenenfalls den fruehesten
gezaehlten ICU-EK. Die Beispielreferenzen sind repraesentative erste IDs, keine
Zusicherung, dass sie zum selben Fall gehoeren. Existierende CSVs werden abgelehnt.
Bei Abbruch kann die CSV unvollstaendig sein; nur ein erfolgreicher Lauf mit
Exitcode 0 liefert einen vollstaendigen Bericht.

Verifiziert mit synthetischen Unit-/Pipeline-Tests und einem lokalen HTTP-Testserver:

```bash
python3 -m unittest discover -s tests -v
```

Getestet sind VN- und partOf-Faelle, Patienten-Deduplikation, Status/Datum/Klasse/
Kontaktebene, unterschiedliche COVID-/ICU-Faelle, fehlende Referenzen, vorhandene
Location bei Kontaktart-ICU, LOINC-Differenz, U07.1 ausserhalb der Baseline, doppelte
VNs, revinclude-Ueberschreibung und POST/GET-Paging ohne Bundle.total. Kein Test
verwendet reale Patienten oder den Klinikserver.
