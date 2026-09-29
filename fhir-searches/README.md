# FHIR-Suchabfragen

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
