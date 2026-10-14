# FHIR-Suchabfragen

## Intensivstationaere Versorgungsstellenkontakte

[`icu-vk-encounter-sample.http`](icu-vk-encounter-sample.http) liefert eine Seite
mit maximal 20 Encounter-Ressourcen, die beide Kriterien erfuellen:
Kontaktstufe `http://fhir.de/CodeSystem/Kontaktebene|versorgungsstellenkontakt`
und Kontaktart `http://fhir.de/CodeSystem/kontaktart-de|intensivstationaer`.
Die zwei `type`-Parameter werden gemaess
[FHIR R4](https://hl7.org/fhir/R4/search.html#combining) mit UND verknuepft;
eine Komma-Liste wuerde stattdessen ODER bedeuten.

`fhirBaseUrl` und Authentifizierung lokal setzen. Die Anfrage ist ausschliesslich
lesend und gibt `id,type,location` zur Inspektion aus, ohne COVID-, Datums-,
Status- oder Location-Existenzfilter. Kein exakter Gesamtzaehler wird angefordert.
Keine Zufallsstichprobe; die Treffer duerfen ein Location-Element besitzen oder
nicht. Syntaktisch getestet, nicht gegen Klinik-Blaze ausgefuehrt. Ergebnisse
bleiben im Kliniknetz; nur anonymisierte Location-Strukturen teilen.

## Encounter-Location-Stichprobe fuer Blaze

[`encounter-location-sample.http`](encounter-location-sample.http) enthaelt drei
getrennt auszufuehrende, ausschliesslich lesende Anfragen. `fhirBaseUrl` und
Authentifizierung lokal setzen. `_count=20` begrenzt jede Seite; keinen Vollabruf
oder exakten Gesamtzaehler anfordern. Es ist keine Zufallsstichprobe.

1. **ICU-Rohdaten:** 20 Kontakte mit Kontaktart `intensivstationaer`, Status
   `finished`/`in-progress`, nur `id,type,location`. Ohne Location-Existenzfilter:
   das tatsaechliche `entry[].resource.location` im Ergebnis ansehen.
2. **Gezielt aus dem Snapshot:**
   [`encounter-location-snapshot-sample.sql`](encounter-location-snapshot-sample.sql)
   auf der lokalen `icu-native-work.sqlite` read-only ausfuehren, z.B. in einem
   SQLite-Browser. Die Datei muss aus einem abgeschlossenen nativen Debug-Lauf
   stammen. Die maximal 20 Encounter-IDs lokal als Komma-Liste in
   `encounterIdsWithLocation` einsetzen und Anfrage 2 starten. Die SQL-Abfrage
   waehlt ICU-VK mit nichtleerem Location-Element, aber ohne vom Debugger
   verwertbare Location-ID. Aktuelle FHIR-Daten koennen inzwischen abweichen.
3. **Optionaler Standardfilter-Test:** `location:missing=false` mit
   `Prefer: handling=strict`. Die aktuelle
   [Blaze-Suchdokumentation](https://blaze-server.org/api/interaction/search-type.html#implemented-modifiers)
   listet `:missing` nicht als implementiert. Ein HTTP 400/OperationOutcome ist
   deshalb kein Nachweis fuer fehlende Locations. Ohne strikte Fehlerbehandlung
   kann Blaze nicht unterstuetzte Parameter ignorieren. Die installierte
   Klinikversion wurde nicht getestet.

Wichtig ist der Unterschied zwischen dem aeusseren `Encounter.location` (einem
BackboneElement mit z.B. `physicalType` oder `period`), der darin liegenden
Referenz `Encounter.location[].location` und einer separaten `Location`-Ressource.
Der [R4-Suchparameter `location`](https://hl7.org/fhir/R4/encounter.html#search)
bezieht sich auf die Referenz, nicht auf das gesamte aeussere Element.
Ein nichtleeres aeusseres Element kann den DDP-Kontaktart-Fallback verhindern,
obwohl es keine verwertbare Location-Referenz gibt. Diese Mappingsituation ist
zu pruefen, nicht allein aus den Zaehlern bewiesen. Siehe auch
[native DDP-Logik](../docs/covid-icu-native-next-run.md#verifizierte-modellbesonderheiten).

Nur das anonymisierte `location`-Array teilen, nicht komplette Encounter,
IDs oder Credentials. Keine Resultate in Git ablegen. Syntax und SQL-Selektion
werden synthetisch getestet; keine Anfrage gegen Klinik-Blaze ausgefuehrt.

## COVID-Patient:innen mit intensivstationaerer Versorgung

Die ausfuehrbare HTTP-Abfrage steht in
[`covid-icu-patient-count.http`](covid-icu-patient-count.http).

Sie sucht auf `Patient`, damit `Bundle.total` die Anzahl eindeutiger Patient:innen enthaelt.
Ein Patient muss

1. mindestens eine Observation mit einem der COVID-LOINC-Codes und einem positiven
   `valueCodeableConcept` besitzen und
2. von mindestens einem Encounter mit der Kontaktart `intensivstationaer` referenziert werden.

Die Observation-Bedingung verwendet den R4-Composite-Suchparameter
`code-value-concept`. Dadurch muessen COVID-Testcode und positives Ergebnis in derselben
Observation stehen. Mehrere getrennte `_has:Observation`-Parameter waeren hier fachlich
unsicher, weil FHIR sie unabhaengig auswertet.

### Wiederkehrende COVID-LOINC-Codes

- `94640-0`
- `94306-8`
- `96763-8`
- `94500-6`
- `94558-4`

Positive Laborergebnisse werden wie im DDP ueber folgende SNOMED-CT-Codes erkannt:

- `10828004`
- `260373001`
- `52101004`

Die ICU-Kontaktart ist:

```text
http://fhir.de/CodeSystem/kontaktart-de|intensivstationaer
```

### Bedeutung und Grenze der Abfrage

Die Verknuepfung erfolgt auf Patientenebene, weil sowohl `Observation.subject` als auch
der ICU-Versorgungsstellenkontakt `Encounter.subject` auf den Patienten zeigen. Damit lautet
die Aussage: "Patient hatte einen positiven COVID-Laborbefund und hatte einen
intensivstationaeren Kontakt".

Die Abfrage erzwingt nicht, dass der positive Befund und der ICU-Kontakt zum selben
Einrichtungskontakt gehoeren oder sich zeitlich ueberlappen. Fuer diese strengere Aussage muss
die Hierarchie `Observation.encounter -> Einrichtungskontakt <- Abteilungskontakt <-
Versorgungsstellenkontakt` mehrstufig ausgewertet und danach nach Patienten-ID dedupliziert
werden; eine portable einzelne FHIR-R4-Search liefert dafuer keinen Distinct-Patient-Count.

### Ausfuehrung und Wirkung

- `fhirBaseUrl` durch die interne FHIR-Basis-URL ersetzen.
- Authentifizierung lokal entsprechend dem Zielserver ergaenzen; keine Zugangsdaten committen.
- Die Anfrage ist rein lesend.
- Der Zaehler steht in `Bundle.total`.
- `_summary=count` verhindert die Rueckgabe der Patient-Ressourcen.
- `_total=accurate` bittet den Server um eine exakte Anzahl; ein Server darf diesen Hinweis
  gemaess FHIR R4 ignorieren.
- Der Server muss Reverse Chaining (`_has`) und den Observation-Composite-Parameter
  `code-value-concept` unterstuetzen.
