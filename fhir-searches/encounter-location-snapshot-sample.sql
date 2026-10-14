-- Ausschliesslich SELECT auf dem lokalen NEW-format Debug-Snapshot.
-- Datenbank im SQLite-Client READ-ONLY oeffnen; keine Klinik-/OMOP-Datenbank!
-- Voraussetzung: abgeschlossener nativer Lauf von ddp_covid_icu_gitops_test.py.
-- Liefert maximal 20 VK-IDs, deren location-Element im Snapshot nichtleer war,
-- fuer die aber KEINE vom Debugger verwertbare Location-Referenz-ID vorlag.
-- IDs nur lokal in encounter-location-sample.http einsetzen, nicht veroeffentlichen.
SELECT e.id AS encounter_id
FROM encounters e
JOIN encounter_native n ON n.id = e.id
JOIN source_patients p ON p.pid = e.pid
WHERE p.configured_positive = 1
  AND (e.level & 4) > 0
  AND e.class_code IN ('IMP', 'stationaer', 'SS')
  AND e.status_ok = 1
  AND e.date_ok = 1
  AND n.server_date_ok = 1
  AND e.icu = 1
  AND e.has_location = 1
  AND NOT EXISTS (
    SELECT 1 FROM encounter_locations r
    WHERE r.enc = e.id AND r.lid IS NOT NULL
  )
ORDER BY e.id
LIMIT 20;
