"""Synthetic-only regression tests; never contact a clinical server."""

from contextlib import redirect_stderr, redirect_stdout
from collections import Counter
import io
import json
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ddp_covid_icu_debug as debug
from ddp_covid_icu_gitops_test import DEFAULTS as GITOPS_DEFAULTS


def coding(system, code):
    return {"coding": [{"system": system, "code": code}]}


def encounter(eid, pid, level, vn=None, parent=None, icu=False,
              status="finished", start="2021-01-01T12:00:00+01:00", cls="IMP"):
    result = {"resourceType": "Encounter", "id": eid,
              "subject": {"reference": "Patient/" + pid}, "status": status,
              "class": {"code": cls}, "period": {"start": start} if start else {},
              "type": [coding(debug.CONTACT_LEVEL, level)]}
    if vn is not None:
        result["identifier"] = [{"type": coding(debug.VN_SYSTEM, "VN"), "value": vn}]
    if parent:
        result["partOf"] = {"reference": "Encounter/" + parent}
    if icu:
        result["type"].append(coding(debug.CONTACT_ART, "intensivstationaer"))
        # ICU via contactart must work even if a non-ICU Location already exists.
        result["location"] = [{"location": {"reference": "Location/ordinary-ward"}}]
    return result


class ModelFixture(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(debug.SCHEMA)
        self.args = debug.parser().parse_args([])
        self.db.executescript("CREATE VIEW retrieved_observations AS SELECT * FROM observations WHERE status_ok=1;"
                             "CREATE VIEW retrieved_conditions AS SELECT * FROM conditions;")

    def tearDown(self):
        self.db.close()

    def patient(self, pid, baseline=True):
        self.db.execute("INSERT OR IGNORE INTO loaded_patients VALUES (?)", (pid,))
        self.db.execute("INSERT OR REPLACE INTO source_patients VALUES (?,1,1,1,1)", (pid,))
        if baseline:
            self.db.execute("INSERT OR IGNORE INTO baseline VALUES (?)", (pid,))

    def add_enc(self, resource):
        self.db.execute("INSERT INTO encounters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        debug.encounter_row(resource, debug.timestamp(self.args.cutoff)))

    def obs(self, oid, pid, enc, configured=1, date_ok=1, status_ok=1):
        self.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (oid, pid, enc, configured, 1, configured, status_ok, date_ok, 1, 1, 1))

    def chain(self, pid, ek_vn=None, vk_vn=None, **vk_options):
        self.patient(pid)
        self.add_enc(encounter(pid + '-ek', pid, "einrichtungskontakt", ek_vn))
        self.add_enc(encounter(pid + '-ak', pid, "abteilungskontakt", ek_vn, pid + '-ek'))
        self.add_enc(encounter(pid + '-vk', pid, "versorgungsstellenkontakt", vk_vn,
                               pid + '-ak', icu=True, **vk_options))
        self.obs(pid + '-obs', pid, pid + '-ek')

    def run_model(self, mode="without_partof", profile="ddp_configured"):
        return debug.analyze(self.db, self.args, profile, mode)


class ModelTests(ModelFixture):
    def test_same_vn_deduplicates_patients_and_accepts_contactart_with_location(self):
        self.chain("a", "case-a", "case-a")
        self.add_enc(encounter("another-vk", "a", "versorgungsstellenkontakt", "case-a", "a-ak", icu=True))
        report = self.run_model()
        self.assertEqual(1, report["data_item"]["data"]["ICU_undifferentiated"])
        self.assertEqual(2, report["diagnostics"]["positive_icu_vk"])
        self.assertEqual(2, report["diagnostics"]["icu_vk_with_location_still_counted_by_contactart"])

    def test_missing_vns_rescued_only_by_partof_on_identical_resources(self):
        self.chain("a")
        self.assertEqual(0, self.run_model()["baseline_counted"])
        self.assertEqual(1, self.run_model("with_partof")["baseline_counted"])
        self.assertEqual(1, debug.compare_modes(self.db, "ddp_configured")["only_with_partof"])
        self.assertIsNone(self.db.execute("SELECT vn FROM encounters WHERE id='a-ek'").fetchone()[0])

    def test_existing_different_vns_are_not_replaced_by_synthetic_identifier(self):
        self.chain("a", "ek-vn", "vk-vn")
        report = self.run_model("with_partof")
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(1, report["diagnostics"]["partof_links_with_different_effective_vns"])
        self.assertEqual(1, report["baseline_exclusive_loss_reasons"]["icu_vk_not_in_positive_visit_number"])

    def test_empty_first_typed_vn_blocks_appended_identifier(self):
        self.chain("a", "", "")
        report = self.run_model("with_partof")
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(1, report["baseline_exclusive_loss_reasons"]["positive_source_encounter_without_vn"])

    def test_patient_level_covid_and_icu_from_different_cases_do_not_count(self):
        self.chain("a", "covid-case", "icu-case")
        self.add_enc(encounter("icu-ek", "a", "einrichtungskontakt", "icu-case"))
        report = self.run_model()
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(1, report["baseline_exclusive_loss_reasons"]["icu_vk_not_in_positive_visit_number"])

    def test_query_loinc_missing_from_default_can_explain_difference(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE observations SET configured=0, retrieval_code=0")
        self.db.execute("UPDATE source_patients SET configured_all=0,configured_positive=0")
        self.assertEqual(0, self.run_model()["baseline_counted"])
        self.assertEqual(1, self.run_model(profile="ddp_plus_query_loincs")["baseline_counted"])

    def test_encounter_status_start_class_and_contact_level_filters(self):
        self.chain("invalid-status", "s", "s", status="cancelled")
        self.chain("missing-date", "d", "d", start=None)
        self.chain("outpatient-vk", "c", "c", cls="AMB")
        self.chain("wrong-level", "l", "l")
        self.db.execute("UPDATE encounters SET level=0 WHERE id='wrong-level-vk'")
        report = self.run_model("with_partof")
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(4, report["baseline_exclusive_loss_reasons"]["no_valid_inpatient_icu_vk"])
        self.assertEqual(4, sum(report["baseline_exclusive_loss_reasons"].values()))

    def test_observation_status_date_and_missing_encounter_are_separate_losses(self):
        self.chain("invalid", "a", "a")
        self.chain("old", "b", "b")
        self.chain("unlinked", "c", "c")
        self.db.execute("UPDATE observations SET status_ok=0 WHERE pid='invalid'")
        self.db.execute("UPDATE observations SET date_ok=0 WHERE pid='old'")
        self.db.execute("UPDATE observations SET enc=NULL WHERE pid='unlinked'")
        report = self.run_model()
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(2, report["baseline_exclusive_loss_reasons"]["no_positive_source_after_loinc_status_date_filters"])
        self.assertEqual(1, report["baseline_exclusive_loss_reasons"]["positive_source_encounter_missing_or_filtered"])

    def test_direct_vk_to_ek_is_not_the_ddp_two_step_chain(self):
        self.chain("a")
        self.db.execute("UPDATE encounters SET partof='a-ek' WHERE id='a-vk'")
        report = self.run_model("with_partof")
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(1, report["diagnostics"]["icu_vk_partof_direct_to_ek_instead_of_ak"])

    def test_outpatient_ek_is_excluded_even_with_positive_icu_vk(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE encounters SET class_code='AMB' WHERE id='a-ek'")
        report = self.run_model()
        self.assertEqual(0, report["baseline_counted"])
        self.assertEqual(1, report["baseline_exclusive_loss_reasons"]["mapped_ek_not_positive_inpatient"])

    def test_u071_only_patients_are_reported_outside_lab_baseline(self):
        self.chain("a", "same", "same")
        self.db.execute("DELETE FROM baseline")
        self.db.execute("DELETE FROM observations")
        self.args.filter_resources_by_date = False
        # v0.5.7's local condition date filter does not remove its list entries.
        self.db.execute("INSERT INTO conditions VALUES ('cond','a','a-ek',1,0)")
        report = self.run_model()
        self.assertEqual(1, report["counted_outside_baseline"])
        self.assertEqual(1, report["data_item"]["data"]["ICU_undifferentiated"])

    def test_duplicate_vns_and_cross_patient_links_are_visible(self):
        self.chain("a", "shared", "shared")
        self.chain("z", "shared", "shared")
        report = self.run_model()
        self.assertEqual(1, report["diagnostics"]["vn_groups_with_multiple_ek"])
        self.assertEqual(1, report["diagnostics"]["cross_patient_mappings"])

    def test_reference_and_date_normalization(self):
        self.assertEqual("abc", debug.ref_id({"reference": "https://example.org/fhir/Encounter/abc/_history/5"}))
        self.assertIsNone(debug.ref_id({"identifier": {"value": "case-number"}}))
        self.assertIsNone(debug.ref_id({"reference": "#contained"}))
        self.assertEqual(debug.timestamp("2021-01-01T11:00:00Z"), debug.timestamp("2021-01-01T12:00:00+01:00"))


class NativeModelTests(ModelFixture):
    def add_enc(self, resource):
        super().add_enc(resource)
        debug.stage_native_encounter(self.db, resource, self.args)

    def location(self, code="ICU", physical="wa", **overrides):
        resource = {"resourceType": "Location", "id": "ordinary-ward", "status": "inactive",
                    "physicalType": coding("any-system", physical), "type": [coding("any-system", code)]}
        resource.update(overrides)
        self.db.execute("INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?,?)", debug.location_row(resource))

    def native(self):
        return debug.analyze(self.db, self.args, "ddp_configured", "without_partof", "native")

    def test_existing_non_icu_location_blocks_contactart_fallback(self):
        self.chain("a", "same", "same")
        self.location(code="GEN")
        self.assertEqual(1, self.run_model()["patient_count"])
        report = self.native()
        self.assertEqual(0, report["patient_count"])
        self.assertEqual({"location_type_not_icu": 1}, report["diagnostics"]["positive_contactart_icu_vk_rejection_reasons"])
        self.assertEqual(1, debug.compare_detection(self.db, "ddp_configured", "without_partof")["only_contactart"])

    def test_recognized_location_counts_historical_icu_without_system_check(self):
        self.chain("a", "same", "same")
        self.location()
        report = self.native()
        self.assertEqual(1, report["patient_count"])
        self.assertEqual(1, report["diagnostics"]["native_icu_via_real_location_vk"])

    def test_absent_location_resource_is_diagnosed_and_item_gate_is_not_zero(self):
        self.chain("a", "same", "same")
        report = self.native()
        self.assertIsNone(report["data_item"])
        self.assertFalse(report["diagnostics"]["native_item_locations_gate_satisfied"])
        self.assertEqual({"location_not_loaded": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])

    def test_physical_type_room_or_missing_does_not_count(self):
        self.chain("a", "same", "same")
        self.location(physical="ro")
        report = self.native()
        self.assertEqual(0, report["patient_count"])
        self.assertEqual({"physical_type_not_ward": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])

    def test_missing_physical_type_is_reported_separately(self):
        self.chain("a", "same", "same")
        self.location(physicalType={})
        report = self.native()
        self.assertEqual({"location_missing_physical_type": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])

    def test_later_type_concept_matches_any_check_but_native_early_return_rejects(self):
        self.chain("a", "same", "same")
        self.location(type=[coding("any", "GEN"), coding("any", "ICU")])
        report = self.native()
        self.assertEqual(0, report["patient_count"])
        self.assertEqual({"icu_code_only_in_later_type_concept": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])
        self.assertTrue(debug.ddp_first_coded_concept([{"coding": [{}]}, coding("any", "ICU")], {"ICU"}))

    def test_contactart_without_location_creates_registered_dummy(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE encounters SET has_location=0 WHERE id='a-vk'")
        self.db.execute("DELETE FROM encounter_locations WHERE enc='a-vk'")
        report = self.native()
        self.assertEqual(1, report["patient_count"])
        self.assertTrue(report["diagnostics"]["dummy_icu_location_available"])
        self.assertEqual(1, report["diagnostics"]["native_icu_via_contactart_fallback_vk"])

    def test_native_location_counts_even_without_intensive_contactart(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE encounters SET icu=0 WHERE id='a-vk'")
        self.db.execute("DELETE FROM baseline")
        self.location()
        self.assertEqual(0, self.run_model()["patient_count"])
        self.assertEqual(1, self.native()["counted_outside_baseline"])

    def test_missing_literal_reference_does_not_trigger_fallback(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE encounter_locations SET lid=NULL WHERE enc='a-vk'")
        report = self.native()
        self.assertEqual({"no_literal_location_reference": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])

    def test_duplicate_contactart_codes_block_exact_fallback(self):
        self.chain("a", "same", "same")
        self.db.execute("UPDATE encounters SET has_location=0 WHERE id='a-vk'")
        self.db.execute("UPDATE encounter_native SET exact_contactart=0 WHERE id='a-vk'")
        self.db.execute("DELETE FROM encounter_locations WHERE enc='a-vk'")
        report = self.native()
        self.assertEqual(0, report["patient_count"])
        self.assertEqual({"contactart_fallback_not_exact": 1}, report["diagnostics"]["contactart_icu_vk_rejection_reasons"])

    def test_configured_service_provider_fallback(self):
        self.args.icu_service_provider_ids = "synthetic-icu-provider"
        self.patient("a", baseline=False)
        self.add_enc(encounter("a-ek", "a", "einrichtungskontakt", "same"))
        vk = encounter("a-vk", "a", "versorgungsstellenkontakt", "same")
        vk["serviceProvider"] = {"reference": "Organization/synthetic-icu-provider"}
        self.add_enc(vk)
        self.obs("obs", "a", "a-ek")
        report = self.native()
        self.assertEqual(1, report["patient_count"])
        self.assertEqual(1, report["diagnostics"]["native_icu_via_service_provider_fallback_vk"])

    def test_ambiguous_vn_selection_reports_bounds_instead_of_exact_claim(self):
        self.chain("a", "same", "same")
        self.location()
        self.add_enc(encounter("a-0-ambulant", "a", "einrichtungskontakt", "same", cls="AMB"))
        report = self.native()
        self.assertEqual(0, report["patient_count"])
        bounds = report["diagnostics"]["vn_selection_bounds"]
        self.assertEqual(0, bounds["guaranteed_patients_lower_bound"])
        self.assertEqual(1, bounds["possible_patients_upper_bound"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                requests.append(("POST", self.path, body, self.headers.get("Authorization")))
                self.send_bundle({"resourceType": "Bundle", "type": "searchset", "entry": [
                    {"resource": {"resourceType": "Patient", "id": "p1"}}],
                    "link": [{"relation": "next", "url": "/fhir/__page?page=2"}]})

            def do_GET(self):
                requests.append(("GET", self.path, "", self.headers.get("Authorization")))
                if "bad" in self.path:
                    bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [
                        {"resource": {"resourceType": "OperationOutcome", "issue": [
                            {"severity": "warning", "code": "not-supported"}]}}]}
                elif "cross" in self.path:
                    bundle = {"resourceType": "Bundle", "type": "searchset", "link": [
                        {"relation": "next", "url": "https://other.example/fhir/__page"}]}
                else:
                    bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [
                        {"resource": {"resourceType": "Patient", "id": "p2"}}]}
                self.send_bundle(bundle)

            def send_bundle(self, bundle):
                data = json.dumps(bundle).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = debug.FhirClient(f"http://127.0.0.1:{self.server.server_port}/fhir",
                                       {"Authorization": "Bearer synthetic-test-token"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_blaze_style_post_and_relative_get_paging_without_bundle_total(self):
        with redirect_stderr(io.StringIO()):
            pages = list(self.client.pages("Patient", {"_id": "p1,p2", "_count": "1"}))
        self.assertEqual(2, len(pages))
        self.assertEqual(["POST", "GET"], [r[0] for r in self.requests])
        self.assertEqual("/fhir/Patient/_search", self.requests[0][1])
        self.assertEqual("p1,p2", urllib.parse.parse_qs(self.requests[0][2])["_id"][0])
        self.assertEqual("Bearer synthetic-test-token", self.requests[1][3])

    def test_search_warning_is_not_silently_counted(self):
        with self.assertRaisesRegex(RuntimeError, "OperationOutcome"):
            list(self.client.pages("bad", {}, force_get=True))

    def test_cross_origin_paging_does_not_forward_credentials(self):
        with redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, "Cross-origin paging"):
            list(self.client.pages("cross", {}, force_get=True))
        self.assertEqual(1, len(self.requests))


class PipelineTests(unittest.TestCase):
    def test_end_to_end_batched_ingest_and_all_four_comparisons(self):
        class SyntheticClient:
            def __init__(self):
                self.encounter_batches = []

            def pages(self, kind, params, force_get=False):
                if kind == "Patient":
                    if "_id" not in params:
                        values = [{"resourceType": "Patient", "id": "a"},
                                  {"resourceType": "Patient", "id": "b"}]
                        self_test.assertIn("_has:Observation:patient:code-value-concept", params)
                        self_test.assertEqual(15, len(params["_has:Observation:patient:code-value-concept"].split(",")))
                    else:
                        values = [{"resourceType": "Patient", "id": pid} for pid in params["_id"].split(",")]
                elif kind == "Observation":
                    values = [{"resourceType": "Observation", "id": pid + "-obs", "status": "final",
                               "subject": {"reference": "Patient/" + pid},
                               "encounter": {"reference": "Encounter/" + pid + "-ek"},
                               "effectiveDateTime": "2021-01-01T12:00:00Z",
                               "code": coding(debug.LOINC, code),
                               "valueCodeableConcept": coding(debug.SNOMED, "260373001")}
                              for pid, code in (("a", "94500-6"), ("b", "94558-4"))]
                elif kind == "Condition":
                    values = [{"resourceType": "Condition", "id": "c-condition",
                               "subject": {"reference": "Patient/c"},
                               "encounter": {"reference": "Encounter/c-ek"},
                               "code": coding(debug.ICD, "U07.1"), "recordedDate": "2021-01-01"}]
                elif kind == "Encounter":
                    ids = params["subject"].split(",")
                    self.encounter_batches.append(ids)
                    values = []
                    for pid in ids:
                        vn = None if pid == "a" else pid + "-vn"
                        values.extend((encounter(pid + "-ek", pid, "einrichtungskontakt", vn),
                                       encounter(pid + "-ak", pid, "abteilungskontakt", vn, pid + "-ek"),
                                       encounter(pid + "-vk", pid, "versorgungsstellenkontakt", vn,
                                                 pid + "-ak", icu=True)))
                else:
                    raise AssertionError(kind)
                # Many small pages; no Bundle.total, and no Location/Procedure requests.
                for value in values:
                    yield {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": value}]}

        self_test = self
        client = SyntheticClient()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(debug, "FhirClient", return_value=client), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            debug.main(["--fhir-base-url", "http://synthetic.example/fhir", "--auth", "none",
                        "--page-size", "1", "--patient-batch-size", "1",
                        "--details-csv", directory + "/details.csv"])
            with open(directory + "/details.csv", encoding="utf-8") as details:
                self.assertEqual(9, len(details.readlines()))  # header + 2 baseline patients * 4 runs
        report = json.loads(output.getvalue())
        self.assertEqual(2, report["baseline_patient_query_count"])
        self.assertEqual([["a"], ["b"], ["c"]], client.encounter_batches)
        self.assertEqual(9, report["retrieval"]["encounters_distinct"])
        expected = {("ddp_configured", "without_partof"): 1,
                    ("ddp_configured", "with_partof"): 2,
                    ("ddp_plus_query_loincs", "without_partof"): 2,
                    ("ddp_plus_query_loincs", "with_partof"): 3}
        for (profile, mode), count in expected.items():
            self.assertEqual(count, report["runs"][profile][mode]["data_item"]["data"]["ICU_undifferentiated"])
            self.assertEqual(2, sum(report["runs"][profile][mode]["baseline_exclusive_loss_reasons"].values()))
        self.assertEqual(1, report["partof_comparison"]["ddp_configured"]["only_with_partof"])
        self.assertEqual(1, report["loinc_comparison"]["with_partof"]["additional_counted_with_query_loincs"])
        self.assertNotIn("Patient/a", output.getvalue())
        self.assertNotIn("a-ek", output.getvalue())

    def test_revinclude_assignment_overwrites_existing_condition_encounter(self):
        condition = {"resourceType": "Condition", "id": "c", "code": coding(debug.ICD, "U07.1"),
                     "subject": {"reference": "Patient/p"}, "encounter": {"reference": "Encounter/old"}}
        included = {"resourceType": "Encounter", "id": "new",
                    "diagnosis": [{"condition": {"reference": "Condition/c"}}]}

        class SyntheticClient:
            def pages(self, kind, params):
                if kind == "Condition":
                    self_test.assertEqual("Encounter:diagnosis", params["_revinclude"])
                    yield {"entry": [{"resource": condition}, {"resource": included}]}

        self_test = self
        args = debug.parser().parse_args(["--condition-link-mode", "revinclude", "--filter-resources-by-date"])
        with sqlite3.connect(":memory:") as db, redirect_stderr(io.StringIO()):
            db.executescript(debug.SCHEMA)
            debug.load_sources(db, SyntheticClient(), args, Counter())
            self.assertEqual("new", db.execute("SELECT enc FROM conditions").fetchone()[0])
            self.assertEqual(1, debug.scalar(db, "SELECT COUNT(*) FROM retrieved_conditions"))


class NativePipelineTests(unittest.TestCase):
    def fixture_client(self):
        outer = self

        class Client:
            def __init__(self):
                self.calls = []

            def pages(self, kind, params, force_get=False):
                self.calls.append((kind, params))
                if kind == "Patient":
                    ids = params.get("_id", "a,b,c,d,e,f").split(",")
                    values = [{"resourceType": "Patient", "id": pid} for pid in ids]
                elif kind == "Observation":
                    outer.assertEqual(set(params["code"].split(",")), debug.QUERY_LOINCS | {"96895-8", "96741-4"})
                    values = [{"resourceType": "Observation", "id": pid + "-obs", "status": "final",
                               "subject": {"reference": "Patient/" + pid},
                               "encounter": {"reference": "Encounter/" + pid + "-ek"},
                               "effectiveDateTime": "2021-01-01T12:00:00Z",
                               "code": coding(debug.LOINC, "94558-4"),
                               "valueCodeableConcept": coding(debug.SNOMED, "260373001")}
                              for pid in "abcdefg"]
                elif kind == "Condition":
                    outer.assertEqual("Encounter:diagnosis", params["_revinclude"])
                    values = [{"resourceType": "Condition", "id": "condition-h",
                               "subject": {"reference": "Patient/h"}, "code": coding(debug.ICD, "U07.1")},
                              {"resourceType": "Encounter", "id": "h-ek",
                               "diagnosis": [{"condition": {"reference": "Condition/condition-h"}}]}]
                elif kind == "Encounter":
                    values = []
                    for pid in params["subject"].split(","):
                        ek = encounter(pid + "-ek", pid, "einrichtungskontakt", pid + "-vn")
                        vk = encounter(pid + "-vk", pid, "versorgungsstellenkontakt", pid + "-vn", icu=pid in "abcdef")
                        vk["location"] = [{"location": {"reference": "Location/loc-" + pid}}]
                        if pid == "c":
                            vk.pop("location")
                        values.extend((ek, vk))
                elif kind == "Location":
                    values = []
                    for lid in params["_id"].split(","):
                        pid = lid.split("-")[-1]
                        if pid == "d":
                            continue
                        resource = {"resourceType": "Location", "id": lid,
                                    "physicalType": coding("any", "ro" if pid == "e" else "wa"),
                                    "type": [coding("any", "GEN" if pid in "bf" else "ICU")]}
                        if pid == "f":
                            resource["type"].append(coding("any", "ICU"))
                        values.append(resource)
                else:
                    raise AssertionError(kind)
                for resource in values:
                    yield {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": resource}]}

        return Client()

    def run_fixture(self, directory):
        client = self.fixture_client()
        output = io.StringIO()
        path = directory + "/native.sqlite"
        with patch.object(debug, "FhirClient", return_value=client), redirect_stdout(output), redirect_stderr(io.StringIO()):
            debug.main(["--fhir-base-url", "http://synthetic.example/fhir", "--auth", "none",
                        "--work-db", path, "--keep-work-db", "--page-size", "1",
                        "--patient-batch-size", "2", "--location-batch-size", "2"], defaults=GITOPS_DEFAULTS)
        return client, json.loads(output.getvalue()), path

    def test_gitops_preset_native_contactart_and_location_loss_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            client, report, path = self.run_fixture(directory)
            self.assertTrue(Path(path).is_file())
        self.assertEqual(6, report["baseline_patient_query_count"])
        self.assertEqual("revinclude", report["configuration"]["condition_link_mode"])
        self.assertEqual(sorted(debug.QUERY_LOINCS), report["configuration"]["ddp_pcr_loincs"])
        self.assertEqual("native", report["configuration"]["primary_icu_detection"])
        run = report["runs"]["ddp_configured"]["without_partof"]
        self.assertEqual(4, run["patient_count"])
        self.assertEqual(6, run["contactart_countercheck"]["patient_count"])
        self.assertEqual(2, run["baseline_counted"])
        self.assertEqual(2, run["counted_outside_baseline"])
        self.assertEqual({"both_detections": 2, "only_contactart": 4, "only_native": 2},
                         report["icu_detection_comparison"]["ddp_configured"]["without_partof"])
        self.assertEqual({"location_type_not_icu": 1, "location_not_loaded": 1,
                          "physical_type_not_ward": 1, "icu_code_only_in_later_type_concept": 1},
                         run["diagnostics"]["positive_contactart_icu_vk_rejection_reasons"])
        self.assertEqual(-61, report["ddp_count_comparison"]["runs"]["ddp_configured"]["without_partof"]["delta"])
        self.assertTrue(report["loinc_comparison"]["without_partof"]["profiles_equivalent"])
        self.assertTrue(all(len(params["_id"].split(",")) <= 2 for kind, params in client.calls if kind == "Location"))

    def test_completed_snapshot_reuse_is_read_only_and_makes_no_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            client, initial, path = self.run_fixture(directory)
            before = Path(path).read_bytes()
            output = io.StringIO()
            with patch.object(debug, "FhirClient", side_effect=AssertionError("No network allowed")), \
                    redirect_stdout(output), redirect_stderr(io.StringIO()):
                debug.main(["--fhir-base-url", "http://synthetic.example/fhir", "--reuse-work-db", path], defaults=GITOPS_DEFAULTS)
            replay = json.loads(output.getvalue())
            self.assertEqual(initial["runs"], replay["runs"])
            self.assertTrue(replay["retrieval"]["reused_snapshot"])
            self.assertEqual(before, Path(path).read_bytes())
            output = io.StringIO()
            with patch.object(debug, "FhirClient", side_effect=AssertionError("No network allowed")), \
                    redirect_stdout(output), redirect_stderr(io.StringIO()):
                debug.main(["--fhir-base-url", "http://synthetic.example/fhir", "--reuse-work-db", path,
                            "--mode", "both"], defaults=GITOPS_DEFAULTS)
            both = json.loads(output.getvalue())
            self.assertEqual({"both_modes": 4, "only_with_partof": 0, "only_without_partof": 0},
                             both["partof_comparison"]["ddp_configured"])
            self.assertEqual(before, Path(path).read_bytes())

    def test_snapshot_with_mismatched_codes_is_rejected_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, path = self.run_fixture(directory)
            with redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "settings differ"):
                debug.main(["--fhir-base-url", "http://synthetic.example/fhir", "--reuse-work-db", path,
                            "--ddp-pcr-loincs", "94500-6"], defaults=GITOPS_DEFAULTS)
            self.assertTrue(Path(path).is_file())


if __name__ == "__main__":
    unittest.main()
