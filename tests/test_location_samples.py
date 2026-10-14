"""Static HTTP checks and synthetic SQLite selection; no clinical connections."""

from pathlib import Path
import sqlite3
import unittest
from urllib.parse import parse_qs, urlsplit

import ddp_covid_icu_debug as debug
from test_covid_icu_debug import encounter


SEARCHES = Path(__file__).resolve().parents[1] / "fhir-searches"


class LocationSampleTests(unittest.TestCase):
    def test_icu_vk_query_uses_and_between_level_and_contactart(self):
        content = (SEARCHES / "icu-vk-encounter-sample.http").read_text(encoding="utf-8")
        requests = [line for line in content.splitlines() if line.startswith("GET ")]
        self.assertEqual(1, len(requests))
        parsed = urlsplit(requests[0][4:].replace("{{fhirBaseUrl}}", "https://example.invalid/fhir"))
        self.assertEqual("/fhir/Encounter", parsed.path)
        query = parse_qs(parsed.query)
        self.assertEqual([debug.CONTACT_LEVEL + "|versorgungsstellenkontakt",
                          debug.CONTACT_ART + "|intensivstationaer"], query["type"])
        self.assertEqual(["20"], query["_count"])
        self.assertEqual(["id,type,location"], query["_elements"])
        self.assertEqual({"type", "_count", "_elements"}, set(query))
        self.assertIn("Prefer: handling=strict", content)

    def test_http_requests_are_small_strict_read_only_samples(self):
        content = (SEARCHES / "encounter-location-sample.http").read_text(encoding="utf-8")
        requests = [line for line in content.splitlines() if line.startswith("GET ")]
        self.assertEqual(3, len(requests))
        self.assertEqual(3, content.count("Prefer: handling=strict"))
        params = []
        for request in requests:
            parsed = urlsplit(request[4:].replace("{{fhirBaseUrl}}", "https://example.invalid/fhir"))
            self.assertEqual("/fhir/Encounter", parsed.path)
            query = parse_qs(parsed.query)
            self.assertEqual(["20"], query["_count"])
            self.assertEqual(["id,type,location"], query["_elements"])
            self.assertNotIn("_total", query)
            self.assertNotIn("_summary", query)
            params.append(query)
        self.assertEqual([debug.CONTACT_ART + "|intensivstationaer"], params[0]["type"])
        self.assertNotIn("location:missing", params[0])
        self.assertEqual(["{{encounterIdsWithLocation}}"], params[1]["_id"])
        self.assertEqual(["false"], params[2]["location:missing"])

    def test_snapshot_selects_location_backbones_without_literal_ids(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript(debug.SCHEMA)
            args = debug.parser().parse_args([])
            db.execute("INSERT INTO source_patients VALUES ('p',1,1,1,1)")
            for eid, location in (
                ("a-physical-type", [{"physicalType": {"coding": [{"code": "wa"}]}}]),
                ("b-identifier", [{"location": {"identifier": {"value": "synthetic"}}}]),
                ("c-literal", [{"location": {"reference": "Location/synthetic"}}]),
                ("d-absent", None),
                ("e-empty", [{}]),
            ):
                resource = encounter(eid, "p", "versorgungsstellenkontakt", "vn", icu=True)
                if location is None:
                    resource.pop("location", None)
                else:
                    resource["location"] = location
                db.execute("INSERT INTO encounters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           debug.encounter_row(resource, debug.timestamp(args.cutoff)))
                debug.stage_native_encounter(db, resource, args)
            query = (SEARCHES / "encounter-location-snapshot-sample.sql").read_text(encoding="utf-8")
            before = db.total_changes
            db.execute("PRAGMA query_only=ON")
            self.assertEqual([("a-physical-type",), ("b-identifier",)], db.execute(query).fetchall())
            self.assertEqual(before, db.total_changes)


if __name__ == "__main__":
    unittest.main()
