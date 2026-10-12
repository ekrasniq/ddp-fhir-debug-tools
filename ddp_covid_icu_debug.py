#!/usr/bin/env python3
"""Read-only, paged COVID ICU comparison; compact FHIR fields are staged on disk.

Python 3.10+, standard library only. See docs/covid-icu-debug.md for the modeled
DDP behavior, optional native Location detection, and clinical usage.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from ddp_covid_maxtreatment_items import COVID_PCR_LOINC_CODES, COVID_VARIANT_LOINC_CODES

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"
CONTACT_LEVEL = "http://fhir.de/CodeSystem/Kontaktebene"
CONTACT_ART = "http://fhir.de/CodeSystem/kontaktart-de"
VN_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
QUERY_LOINCS = {"94640-0", "94306-8", "96763-8", "94500-6", "94558-4"}
POSITIVE_CODES = {"10828004", "260373001", "52101004"}
INPATIENT = {"IMP", "stationaer", "SS"}
LEVELS = {"einrichtungskontakt": 1, "abteilungskontakt": 2, "versorgungsstellenkontakt": 4}
PROFILES = ("ddp_configured", "ddp_plus_query_loincs")
MODES = ("without_partof", "with_partof")
ICU_DUMMY = "ICU_DUMMY"
ICU_LOCATION_CODES = {"ICU", "PEDICU", "PEDNICU"}


def ref_id(reference: dict | None) -> str | None:
    """Literal references only; identifier.value is not a FHIR resource ID."""
    text = (reference or {}).get("reference")
    if not text or text.startswith(("#", "urn:")):
        return None
    parts = urllib.parse.urlsplit(text).path.rstrip("/").split("/")
    if "_history" in parts:
        parts = parts[:parts.index("_history")]
    return parts[-1] or None


def has_code(concept: dict | None, system: str, codes: set[str]) -> bool:
    return any(c.get("system") == system and c.get("code") in codes
               for c in (concept or {}).get("coding", []))


def fhir_nonempty(value) -> bool:
    if isinstance(value, dict):
        return any(fhir_nonempty(v) for v in value.values())
    if isinstance(value, list):
        return any(fhir_nonempty(v) for v in value)
    return value is not None and value != ""


def timestamp(value: str | None) -> str | None:
    """Normalize to UTC so SQLite comparisons also respect timezone offsets."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # FHIR date-only/partial values have no offset; use UTC as Blaze date search does.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def observation_time(resource: dict) -> str | None:
    # v0.5.7 processing accesses effectiveDateTime, not issued or effectivePeriod.
    return timestamp(resource.get("effectiveDateTime"))


def encounter_row(resource: dict, cutoff: str) -> tuple:
    types = resource.get("type") or []
    level = 7 if not types else 0  # Same fallback as MiiEncounter.isContactType.
    for name, bit in LEVELS.items():
        if any(has_code(t, CONTACT_LEVEL, {name}) for t in types):
            level |= bit
    vn, has_vn = None, False
    for identifier in resource.get("identifier", []):
        if has_code(identifier.get("type"), VN_SYSTEM, {"VN"}):
            has_vn, vn = True, identifier.get("value") or None
            break  # DDP uses the FIRST typed VN, even if it has no value.
    start = timestamp((resource.get("period") or {}).get("start"))
    status_ok = resource.get("status") in {"finished", "in-progress"}
    icu = any(has_code(t, CONTACT_ART, {"intensivstationaer"}) for t in types)
    any_icu = any(c.get("code") == "intensivstationaer"
                  for t in types for c in t.get("coding", []))
    return (resource["id"], ref_id(resource.get("subject")),
            (resource.get("class") or {}).get("code"), level, int(icu), int(any_icu),
            int(fhir_nonempty(resource.get("location"))), start, int(status_ok),
            int(bool(start and start >= cutoff)), vn, int(has_vn), ref_id(resource.get("partOf")))


def ddp_first_coded_concept(concepts: list[dict], codes: set[str]) -> bool:
    """v0.5.7-update.4 FhirCodingTools returns at the FIRST concept with coding.

    This intentionally does not fix that early return or require a code system.
    """
    for concept in concepts:
        if fhir_nonempty(concept.get("coding")):
            return any(c.get("code") in codes for c in concept["coding"])
    return False


def location_row(resource: dict) -> tuple:
    types = resource.get("type") or []
    physical = resource.get("physicalType") or {}
    return (resource["id"], int(ddp_first_coded_concept([physical], {"wa"})),
            int(ddp_first_coded_concept(types, ICU_LOCATION_CODES)),
            int(any(c.get("code") in ICU_LOCATION_CODES for t in types for c in t.get("coding", []))),
            int(fhir_nonempty(physical)), int(fhir_nonempty(types)))


def stage_native_encounter(db: sqlite3.Connection, resource: dict, args) -> None:
    """Keep reference IDs and flags, not whole Location/serviceProvider objects."""
    eid = resource["id"]
    contact = "".join(c.get("code") or "" for t in resource.get("type", [])
                      for c in t.get("coding", []) if c.get("system") == CONTACT_ART)
    provider = resource.get("serviceProvider") or {}
    provider_id = ref_id(provider) or (provider.get("identifier") or {}).get("value")
    provider_match = bool(provider_id and provider_id in set(args.icu_service_provider_ids.split(",")) - {""})
    period = resource.get("period") or {}
    start, end = timestamp(period.get("start")), timestamp(period.get("end"))
    server_cutoff = timestamp("2020-01-27T00:00:00Z")
    # Encounter date search compares the period interval, not only period.start.
    server_date_ok = bool(start and (start >= server_cutoff or not end or end >= server_cutoff))
    db.execute("INSERT OR REPLACE INTO encounter_native VALUES (?,?,?,?,?)",
               (eid, int(contact == "intensivstationaer"), int(bool(provider)),
                int(provider_match), int(server_date_ok)))
    db.execute("DELETE FROM encounter_locations WHERE enc=?", (eid,))
    db.executemany("INSERT INTO encounter_locations VALUES (?,?,?)",
                   ((eid, index, ref_id(entry.get("location")))
                    for index, entry in enumerate(resource.get("location", []))))


SCHEMA = """
PRAGMA cache_size=-32768;
PRAGMA temp_store=FILE;
CREATE TABLE baseline (pid TEXT PRIMARY KEY);
CREATE TABLE observations (
 id TEXT PRIMARY KEY, pid TEXT, enc TEXT, configured INTEGER, query_code INTEGER,
 retrieval_code INTEGER, status_ok INTEGER, date_ok INTEGER, server_date_ok INTEGER,
 positive INTEGER, query_positive INTEGER
);
CREATE TABLE conditions (id TEXT PRIMARY KEY, pid TEXT, enc TEXT, code_ok INTEGER, server_date_ok INTEGER);
CREATE TABLE condition_links (cid TEXT PRIMARY KEY, enc TEXT);
CREATE TABLE loaded_patients (pid TEXT PRIMARY KEY);
CREATE TABLE encounters (
 id TEXT PRIMARY KEY, pid TEXT, class_code TEXT, level INTEGER, icu INTEGER,
 icu_any_system INTEGER, has_location INTEGER, start TEXT, status_ok INTEGER,
 date_ok INTEGER, vn TEXT, has_vn INTEGER, partof TEXT
);
CREATE INDEX observations_pid ON observations(pid);
CREATE INDEX conditions_pid ON conditions(pid);
CREATE INDEX encounters_pid ON encounters(pid);
CREATE INDEX encounters_vn ON encounters(vn);
CREATE TABLE source_patients (pid TEXT PRIMARY KEY, configured_all INTEGER,
 configured_positive INTEGER, extended_all INTEGER, extended_positive INTEGER);
CREATE TABLE encounter_native (id TEXT PRIMARY KEY, exact_contactart INTEGER,
 has_service_provider INTEGER, service_provider_match INTEGER, server_date_ok INTEGER);
CREATE TABLE encounter_locations (enc TEXT, position INTEGER, lid TEXT, PRIMARY KEY(enc,position));
CREATE INDEX encounter_locations_lid ON encounter_locations(lid);
CREATE TABLE locations (id TEXT PRIMARY KEY, is_ward INTEGER, first_type_icu INTEGER,
 any_type_icu INTEGER, has_physical_type INTEGER, has_type INTEGER);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
"""


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise RuntimeError("Cross-origin redirect refused; check the FHIR base URL.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def origin(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class FhirClient:
    def __init__(self, base_url: str, headers: dict, timeout: int = 120, use_post: bool = True):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
            raise ValueError("Use an HTTP(S) FHIR base URL without embedded credentials.")
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = {"Accept": "application/fhir+json", "Prefer": "handling=strict", **headers}
        self.timeout, self.use_post = timeout, use_post
        self.opener = urllib.request.build_opener(SafeRedirect())

    def pages(self, resource_type: str, params: dict, force_get: bool = False):
        post = self.use_post and not force_get
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        url = self.base_url + resource_type + ("/_search" if post else "?" + encoded.decode())
        body = encoded if post else None
        previous_url = None
        page = 0
        while url:
            if origin(url) != origin(self.base_url):
                raise RuntimeError("Cross-origin paging URL refused; check Blaze BASE_URL/proxy settings.")
            if url == previous_url:
                raise RuntimeError("Repeated FHIR next link; refusing an incomplete comparison.")
            previous_url = url
            headers = dict(self.headers)
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = urllib.request.Request(url, data=body, headers=headers,
                                             method="POST" if body is not None else "GET")
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    bundle = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"FHIR {resource_type} search failed: HTTP {exc.code}; "
                                   "no partial result is reported.") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"FHIR {resource_type} connection failed ({type(exc).__name__}).") from None
            if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "searchset":
                raise RuntimeError(f"FHIR {resource_type} did not return a searchset Bundle.")
            for entry in bundle.get("entry", []):
                resource = entry.get("resource") or {}
                if resource.get("resourceType") == "OperationOutcome":
                    issues = resource.get("issue", [])
                    if any(i.get("severity") in {"fatal", "error", "warning"} for i in issues):
                        raise RuntimeError("FHIR search returned an OperationOutcome; check search support.")
            page += 1
            yield bundle
            if page == 1 or page % 20 == 0:
                progress(f"{resource_type}: {page} Seiten verarbeitet")
            next_link = next((x.get("url") for x in bundle.get("link", [])
                              if x.get("relation") == "next"), None)
            url = urllib.parse.urljoin(url, next_link) if next_link else None
            body = None  # Blaze next links are opaque GET URLs, also after POST _search.


def resources(bundle: dict):
    for entry in bundle.get("entry", []):
        if entry.get("resource"):
            yield entry["resource"]


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def scalar(db: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return db.execute(sql, params).fetchone()[0]


def load_baseline(db: sqlite3.Connection, client: FhirClient, page_size: int) -> None:
    pairs = ",".join(f"{LOINC}|{loinc}${SNOMED}|{code}"
                     for loinc in sorted(QUERY_LOINCS) for code in sorted(POSITIVE_CODES))
    params = {"_has:Observation:patient:code-value-concept": pairs,
              "_has:Encounter:patient:type": CONTACT_ART + "|intensivstationaer",
              "_count": str(page_size), "_elements": "id"}
    # Same predicates as the user's .http file, but page IDs rather than trust Bundle.total.
    for bundle in client.pages("Patient", params, force_get=True):
        with db:
            db.executemany("INSERT OR IGNORE INTO baseline VALUES (?)",
                           ((r["id"],) for r in resources(bundle)
                            if r.get("resourceType") == "Patient" and r.get("id")))
    progress(f"FHIR-Vergleichsabfrage: {scalar(db, 'SELECT COUNT(*) FROM baseline')} eindeutige Patienten")


def load_sources(db: sqlite3.Connection, client: FhirClient, args, stats: Counter) -> None:
    configured = set(args.ddp_pcr_loincs.split(","))
    retrieval = configured | set(args.ddp_variant_loincs.split(",")) - {""}
    codes = retrieval | QUERY_LOINCS
    params = {"code": ",".join(sorted(codes)), "_count": str(args.page_size)}
    cutoff, server_cutoff = timestamp(args.cutoff), timestamp("2020-01-27T00:00:00Z")
    for bundle in client.pages("Observation", params):
        with db:
            for obs in resources(bundle):
                if obs.get("resourceType") != "Observation" or not obs.get("id"):
                    continue
                stats["observations_received"] += 1
                effective = observation_time(obs)
                valid = bool(obs.get("status")) and obs.get("status") not in {"cancelled", "entered-in-error"}
                positive_value = has_code(obs.get("valueCodeableConcept"), SNOMED, POSITIVE_CODES)
                positive_interpretation = not any(k.startswith("value") and v is not None
                                                 for k, v in obs.items()) and any(
                    has_code(cc, "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                             {"POS", "DET"}) for cc in obs.get("interpretation", []))
                if obs.get("effectivePeriod"):
                    stats["observations_with_effective_period_unsupported_by_ddp_datetime_path"] += 1
                db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (obs["id"], ref_id(obs.get("subject")), ref_id(obs.get("encounter")),
                            int(has_code(obs.get("code"), LOINC, configured)),
                            int(has_code(obs.get("code"), LOINC, QUERY_LOINCS)),
                            int(has_code(obs.get("code"), LOINC, retrieval)), int(valid),
                            int(bool(effective and effective > cutoff)),
                            int(bool(effective and effective >= server_cutoff)),
                            int(positive_value or (args.keep_interpretation and positive_interpretation)),
                            int(positive_value)))
    params = {"code": "U07.1", "_count": str(args.page_size)}
    if args.condition_link_mode == "revinclude":
        params["_revinclude"] = "Encounter:diagnosis"
    # No server date restriction here: diagnostics need to see filtered-out sources too.
    for bundle in client.pages("Condition", params):
        with db:
            for resource in resources(bundle):
                if resource.get("resourceType") == "Condition" and resource.get("id"):
                    recorded = timestamp(resource.get("recordedDate"))
                    db.execute("INSERT OR REPLACE INTO conditions VALUES (?,?,?,?,?)",
                               (resource["id"], ref_id(resource.get("subject")),
                                ref_id(resource.get("encounter")),
                                int(has_code(resource.get("code"), ICD, {"U07.1"})),
                                int(bool(recorded and recorded >= server_cutoff))))
                elif resource.get("resourceType") == "Encounter" and resource.get("id"):
                    for diagnosis in resource.get("diagnosis", []):
                        cid = ref_id(diagnosis.get("condition"))
                        if cid:
                            # v0.5.7 revinclude overwrites even an existing Condition.encounter.
                            db.execute("INSERT OR REPLACE INTO condition_links VALUES (?,?)",
                                       (cid, resource["id"]))
    if args.condition_link_mode == "revinclude":
        db.execute("UPDATE conditions SET enc=(SELECT enc FROM condition_links l WHERE l.cid=conditions.id) "
                   "WHERE id IN (SELECT cid FROM condition_links)")
    obs_server = "AND server_date_ok=1" if args.filter_resources_by_date else ""
    cond_server = ("AND server_date_ok=1" if args.filter_resources_by_date
                   and args.condition_link_mode == "direct" else "")
    db.executescript(f"""
      CREATE VIEW retrieved_observations AS SELECT * FROM observations WHERE status_ok=1 {obs_server};
      CREATE VIEW retrieved_conditions AS SELECT * FROM conditions WHERE 1=1 {cond_server};
      INSERT INTO source_patients
      SELECT pid, MAX(c_all), MAX(c_pos), MAX(e_all), MAX(e_pos) FROM (
        SELECT pid, retrieval_code AS c_all, configured*positive AS c_pos,
          MAX(retrieval_code,query_code) AS e_all, MAX(configured,query_code)*positive AS e_pos
          FROM retrieved_observations
        UNION ALL SELECT pid, 1, code_ok, 1, code_ok FROM retrieved_conditions
      ) WHERE pid IS NOT NULL GROUP BY pid;
    """)
    db.commit()
    progress("COVID-Quellen seitenweise eingelesen")


def load_encounters(db: sqlite3.Connection, client: FhirClient, args, stats: Counter) -> None:
    flag = "extended_positive" if args.filter_patient_retrieval else "extended_all"
    cursor = db.execute(f"SELECT pid FROM source_patients WHERE {flag}=1 "
                        "UNION SELECT pid FROM baseline ORDER BY pid")
    cutoff = timestamp(args.cutoff)
    batch = 0
    while rows := cursor.fetchmany(args.patient_batch_size):
        ids = [row[0] for row in rows]
        patients = []
        for bundle in client.pages("Patient", {"_id": ",".join(ids), "_count": str(args.page_size), "_elements": "id"}):
            with db:
                for patient in resources(bundle):
                    if patient.get("resourceType") == "Patient" and patient.get("id"):
                        patients.append(patient["id"])
                        if patient["id"] not in ids:
                            raise RuntimeError("Patient _id search returned an unrequested ID; check search support.")
                        db.execute("INSERT OR IGNORE INTO loaded_patients VALUES (?)", (patient["id"],))
        if not patients:
            continue
        params = {"subject": ",".join(patients), "_count": str(args.page_size),
                  "_elements": "id,subject,status,class,type,identifier,partOf,period,location,serviceProvider"}
        # Deliberately retrieve invalid/old encounters too, then report their exclusion locally.
        for bundle in client.pages("Encounter", params):
            with db:
                for encounter in resources(bundle):
                    if encounter.get("resourceType") == "Encounter" and encounter.get("id"):
                        stats["encounters_received"] += 1
                        db.execute("INSERT OR REPLACE INTO encounters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   encounter_row(encounter, cutoff))
                        stage_native_encounter(db, encounter, args)
        batch += 1
        progress(f"Encounter-Patientenbatch {batch}: {len(patients)} Patienten; "
                 f"{stats['encounters_received']} Ressourcen verarbeitet")


def load_locations(db: sqlite3.Connection, client: FhirClient, args, stats: Counter) -> None:
    cursor = db.execute("SELECT DISTINCT lid FROM encounter_locations WHERE lid IS NOT NULL ORDER BY lid")
    batch = 0
    while rows := cursor.fetchmany(args.location_batch_size):
        ids = [r[0] for r in rows]
        for bundle in client.pages("Location", {"_id": ",".join(ids), "_count": str(args.page_size),
                                               "_elements": "id,type,physicalType"}):
            with db:
                for location in resources(bundle):
                    if location.get("resourceType") != "Location" or not location.get("id"):
                        continue
                    if location["id"] not in ids:
                        raise RuntimeError("Location _id search returned an unrequested ID.")
                    db.execute("INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?,?)", location_row(location))
                    stats["locations_received"] += 1
        batch += 1
        if batch == 1 or batch % 10 == 0:
            progress(f"Location-Batch {batch} verarbeitet")
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('locations_complete','true')")
    db.commit()


def prepare_native_scope(db: sqlite3.Connection, args, source_flag: str) -> dict:
    """Locations/dummy gate before DataItemGenerator's local start-date filter."""
    server_date = "AND n.server_date_ok=1" if args.filter_resources_by_date else ""
    db.executescript(f"""
      CREATE TEMP TABLE native_retrieved AS SELECT e.id FROM encounters e
        JOIN encounter_native n ON n.id=e.id JOIN source_patients p ON p.pid=e.pid
        WHERE p.{source_flag}=1 AND e.status_ok=1 AND e.start IS NOT NULL AND e.class_code IS NOT NULL {server_date};
      CREATE UNIQUE INDEX temp.native_retrieved_id ON native_retrieved(id);
    """)
    dummy_available = bool(scalar(db, "SELECT EXISTS(SELECT 1 FROM native_retrieved r "
        "JOIN encounters e ON e.id=r.id JOIN encounter_native n ON n.id=r.id WHERE "
        "n.has_service_provider=1 OR (e.has_location=0 AND (n.exact_contactart=1 OR n.service_provider_match=1)) "
        "OR EXISTS(SELECT 1 FROM encounter_locations l WHERE l.enc=r.id AND l.lid=?))", (ICU_DUMMY,)))
    real_locations = scalar(db, "SELECT COUNT(DISTINCT l.id) FROM locations l JOIN encounter_locations r "
                            "ON r.lid=l.id JOIN native_retrieved e ON e.id=r.enc")
    db.executescript(f"""
      CREATE TEMP TABLE native_flags AS SELECT e.id,
        EXISTS(SELECT 1 FROM encounter_locations r JOIN locations l ON l.id=r.lid
          WHERE r.enc=e.id AND l.is_ward=1 AND l.first_type_icu=1) AS real_location,
        (e.has_location=0 AND n.exact_contactart=1 AND {int(dummy_available)}) AS contactart_fallback,
        (e.has_location=0 AND n.exact_contactart=0 AND n.service_provider_match=1 AND {int(dummy_available)}) AS provider_fallback,
        EXISTS(SELECT 1 FROM encounter_locations r WHERE r.enc=e.id AND r.lid='ICU_DUMMY'
          AND {int(dummy_available)}) AS existing_dummy_reference
        FROM effective e JOIN encounter_native n ON n.id=e.id;
      CREATE UNIQUE INDEX temp.native_flags_id ON native_flags(id);
    """)
    return {"dummy_icu_location_available": dummy_available, "locations_retrieved_for_ddp_population": real_locations,
            "native_item_locations_gate_satisfied": bool(real_locations or dummy_available)}


def analyze(db: sqlite3.Connection, args, profile: str, mode: str, detection: str = "contactart") -> dict:
    extended = profile == "ddp_plus_query_loincs"
    code = "MAX(configured,query_code)" if extended else "configured"
    source_flag = ("extended_" if extended else "configured_") + (
        "positive" if args.filter_patient_retrieval else "all")
    encounter_server_filter = "AND COALESCE(n.server_date_ok,1)=1" if args.filter_resources_by_date else ""
    if args.condition_link_mode == "revinclude":
        # In v0.5.7 condition date filtering logs differences but does NOT replace the list.
        condition_filter_note = "revinclude: no recordedDate exclusion, matching v0.5.7 list-retention behavior"
    else:
        condition_filter_note = "direct: optional server recorded-date filter; no additional local date exclusion"
    for table in ("scope", "positive_sources", "partof_links", "vn_facilities", "links", "effective",
                  "positive_vns", "flagged", "icu", "mapped_icu", "positive_icu", "counted", "reasons",
                  "native_retrieved", "native_flags", "native_rejections", "vn_bounds"):
        db.execute(f"DROP TABLE IF EXISTS temp.{table}")
    db.executescript(f"""
      CREATE TEMP TABLE scope AS
        SELECT e.* FROM encounters e JOIN source_patients p ON p.pid=e.pid
        LEFT JOIN encounter_native n ON n.id=e.id
        WHERE p.{source_flag}=1 AND e.status_ok=1 AND e.date_ok=1
          AND e.class_code IS NOT NULL AND e.class_code<>'' {encounter_server_filter};
      CREATE UNIQUE INDEX temp.scope_id ON scope(id);
      CREATE INDEX temp.scope_vn ON scope(vn);
      CREATE INDEX temp.scope_pid ON scope(pid);
      CREATE TEMP TABLE positive_sources AS
        SELECT pid, enc, 'observation' AS source FROM retrieved_observations
          WHERE {code}=1 AND positive=1 AND date_ok=1
        UNION ALL SELECT pid, enc, 'condition' FROM retrieved_conditions WHERE code_ok=1;
      CREATE INDEX temp.positive_sources_pid ON positive_sources(pid);
      CREATE INDEX temp.positive_sources_enc ON positive_sources(enc);
      CREATE TEMP TABLE partof_links AS
        SELECT s.id AS supply, f.id AS facility FROM scope s
        JOIN scope a ON a.id=s.partof AND (a.level & 2)>0 AND a.class_code IN ('IMP','stationaer','SS')
        JOIN scope f ON f.id=a.partof AND (f.level & 1)>0
        WHERE (s.level & 4)>0 AND s.class_code IN ('IMP','stationaer','SS');
      CREATE UNIQUE INDEX temp.partof_supply ON partof_links(supply);
      CREATE INDEX temp.partof_facility ON partof_links(facility);
      CREATE TEMP TABLE vn_facilities AS
        SELECT vn, MIN(id) AS facility FROM scope WHERE (level & 1)>0 AND vn IS NOT NULL GROUP BY vn;
      CREATE UNIQUE INDEX temp.vn_facilities_vn ON vn_facilities(vn);
      CREATE TEMP TABLE links AS
        SELECT s.id AS supply, f.facility, 'vn' AS method FROM scope s JOIN vn_facilities f ON f.vn=s.vn
        WHERE (s.level & 4)>0 AND s.class_code IN ('IMP','stationaer','SS');
      CREATE UNIQUE INDEX temp.links_supply ON links(supply);
    """)
    if mode == "with_partof":
        db.execute("INSERT OR REPLACE INTO links SELECT supply, facility, 'partof' FROM partof_links")
    # Appended synthetic VNs do not replace existing/cached VNs. Empty first typed VN also blocks them.
    synthetic = ("CASE WHEN has_vn=1 THEN vn "
                 "WHEN id IN (SELECT supply FROM partof_links) THEN "
                 "(SELECT facility FROM partof_links p WHERE p.supply=scope.id) "
                 "WHEN id IN (SELECT facility FROM partof_links) THEN id ELSE vn END"
                 if mode == "with_partof" else "vn")
    db.executescript(f"""
      CREATE TEMP TABLE effective AS SELECT scope.*, {synthetic} AS effective_vn FROM scope;
      CREATE UNIQUE INDEX temp.effective_id ON effective(id);
      CREATE INDEX temp.effective_vn ON effective(effective_vn);
      CREATE INDEX temp.effective_pid ON effective(pid);
      CREATE TEMP TABLE positive_vns AS
        SELECT DISTINCT e.effective_vn AS vn FROM effective e JOIN positive_sources p ON p.enc=e.id
        WHERE e.effective_vn IS NOT NULL;
      CREATE UNIQUE INDEX temp.positive_vns_vn ON positive_vns(vn);
      CREATE TEMP TABLE flagged AS
        SELECT e.* FROM effective e JOIN positive_vns v ON v.vn=e.effective_vn;
      CREATE UNIQUE INDEX temp.flagged_id ON flagged(id);
      CREATE INDEX temp.flagged_pid ON flagged(pid);
    """)
    native = {}
    predicate = "icu=1"
    if detection == "native":
        native = prepare_native_scope(db, args, source_flag)
        predicate = "id IN (SELECT id FROM native_flags WHERE real_location OR contactart_fallback OR provider_fallback OR existing_dummy_reference)"
    db.executescript(f"""
      CREATE TEMP TABLE icu AS SELECT * FROM effective WHERE {predicate} AND (level & 4)>0
        AND class_code IN ('IMP','stationaer','SS');
      CREATE UNIQUE INDEX temp.icu_id ON icu(id);
      CREATE INDEX temp.icu_pid ON icu(pid);
      CREATE TEMP TABLE mapped_icu AS SELECT i.*, l.facility, l.method FROM icu i JOIN links l ON l.supply=i.id;
      CREATE INDEX temp.mapped_icu_pid ON mapped_icu(pid);
      CREATE TEMP TABLE positive_icu AS
        SELECT i.* FROM mapped_icu i JOIN flagged s ON s.id=i.id;
      CREATE INDEX temp.positive_icu_pid ON positive_icu(pid);
      CREATE TEMP TABLE counted AS
        SELECT DISTINCT f.* FROM positive_icu i JOIN flagged f ON f.id=i.facility
        WHERE f.class_code IN ('IMP','stationaer','SS');
      CREATE INDEX temp.counted_pid ON counted(pid);
      CREATE TEMP TABLE reasons AS SELECT b.pid, CASE
        WHEN b.pid IN (SELECT pid FROM counted) THEN 'counted'
        WHEN b.pid NOT IN (SELECT pid FROM loaded_patients) THEN 'patient_resource_not_loaded'
        WHEN b.pid NOT IN (SELECT pid FROM source_patients WHERE {source_flag}=1)
          THEN 'no_source_for_ddp_patient_retrieval'
        WHEN b.pid NOT IN (SELECT pid FROM positive_sources WHERE pid IS NOT NULL)
          THEN 'no_positive_source_after_loinc_status_date_filters'
        WHEN b.pid NOT IN (SELECT p.pid FROM positive_sources p JOIN effective e ON e.id=p.enc WHERE p.pid IS NOT NULL)
          THEN 'positive_source_encounter_missing_or_filtered'
        WHEN b.pid NOT IN (SELECT p.pid FROM positive_sources p JOIN effective e ON e.id=p.enc
                          WHERE p.pid IS NOT NULL AND e.effective_vn IS NOT NULL)
          THEN 'positive_source_encounter_without_vn'
        WHEN b.pid NOT IN (SELECT pid FROM icu WHERE pid IS NOT NULL) THEN 'no_valid_inpatient_icu_vk'
        WHEN b.pid NOT IN (SELECT pid FROM mapped_icu WHERE pid IS NOT NULL) THEN 'icu_vk_without_ek_mapping'
        WHEN b.pid NOT IN (SELECT pid FROM positive_icu WHERE pid IS NOT NULL) THEN 'icu_vk_not_in_positive_visit_number'
        ELSE 'mapped_ek_not_positive_inpatient' END AS reason FROM baseline b;
    """)
    name = profile + "_" + mode + "_" + detection
    db.execute(f"CREATE TEMP TABLE result_{name} AS SELECT pid, MIN(id) AS example_ek FROM counted "
               "WHERE pid IS NOT NULL GROUP BY pid")
    db.execute(f"CREATE UNIQUE INDEX temp.result_{name}_pid ON result_{name}(pid)")
    db.execute(f"CREATE TEMP TABLE reasons_{name} AS SELECT * FROM reasons")
    count = scalar(db, f"SELECT COUNT(*) FROM result_{name}")
    baseline_counted = scalar(db, "SELECT COUNT(*) FROM reasons WHERE reason='counted'")
    diagnostics = {
        "eligible_encounters": scalar(db, "SELECT COUNT(*) FROM scope"),
        "eligible_icu_vk": scalar(db, "SELECT COUNT(*) FROM icu"),
        "icu_vk_with_location_still_counted_by_contactart": (scalar(db, "SELECT COUNT(*) FROM icu WHERE has_location=1")
                                                           if detection == "contactart" else None),
        "mapped_supply_contacts": scalar(db, "SELECT COUNT(*) FROM links"),
        "mapped_icu_vk": scalar(db, "SELECT COUNT(*) FROM mapped_icu"),
        "icu_vk_without_original_vn": scalar(db, "SELECT COUNT(*) FROM icu WHERE vn IS NULL"),
        "icu_vk_without_literal_partof": scalar(db, "SELECT COUNT(*) FROM icu WHERE partof IS NULL"),
        "icu_vk_partof_parent_not_eligible": scalar(db, "SELECT COUNT(*) FROM icu i LEFT JOIN scope a "
            "ON a.id=i.partof WHERE i.partof IS NOT NULL AND a.id IS NULL"),
        "icu_vk_partof_direct_to_ek_instead_of_ak": scalar(db, "SELECT COUNT(*) FROM icu i JOIN scope a "
            "ON a.id=i.partof WHERE (a.level & 1)>0 AND (a.level & 2)=0"),
        "icu_vk_without_complete_partof_chain": scalar(db, "SELECT COUNT(*) FROM icu "
            "WHERE id NOT IN (SELECT supply FROM partof_links)"),
        "partof_chains_available": scalar(db, "SELECT COUNT(*) FROM partof_links"),
        "positive_source_encounter_ids": scalar(db, "SELECT COUNT(DISTINCT enc) FROM positive_sources"),
        "positive_source_encounter_ids_loaded": scalar(db, "SELECT COUNT(DISTINCT p.enc) FROM positive_sources p JOIN effective e ON e.id=p.enc"),
        "positive_sources_without_encounter_reference": scalar(db, "SELECT COUNT(*) FROM positive_sources WHERE enc IS NULL"),
        "positive_visit_numbers": scalar(db, "SELECT COUNT(*) FROM positive_vns"),
        "positive_icu_vk": scalar(db, "SELECT COUNT(*) FROM positive_icu"),
        "positive_inpatient_icu_ek": scalar(db, "SELECT COUNT(*) FROM counted"),
        "partof_links_with_different_effective_vns": scalar(db, "SELECT COUNT(*) FROM partof_links p "
            "JOIN effective s ON s.id=p.supply JOIN effective f ON f.id=p.facility "
            "WHERE s.effective_vn IS NOT f.effective_vn"),
        "vn_groups_with_multiple_ek": scalar(db, "SELECT COUNT(*) FROM (SELECT vn FROM scope "
            "WHERE (level & 1)>0 AND vn IS NOT NULL GROUP BY vn HAVING COUNT(*)>1)"),
        "cross_patient_mappings": scalar(db, "SELECT COUNT(*) FROM links l JOIN effective s ON s.id=l.supply "
            "JOIN effective f ON f.id=l.facility WHERE s.pid IS NOT f.pid"),
        "condition_date_behavior": condition_filter_note,
    }
    if detection == "native":
        diagnostics.update(native_diagnostics(db, native))
    diagnostics["vn_selection_bounds"] = vn_selection_bounds(db)
    if args.details_csv:
        write_details(db, args.details_csv, profile, mode, detection)
    return {"icu_detection": detection,
            "data_item": {"itemname": "cumulative.maxtreatmentlevel", "itemtype": "aggregated",
                          "data": {"ICU_undifferentiated": count}}
                         if detection != "native" or native["native_item_locations_gate_satisfied"] else None,
            "patient_count": count,
            "baseline_counted": baseline_counted,
            "baseline_not_counted": scalar(db, "SELECT COUNT(*) FROM baseline") - baseline_counted,
            "counted_outside_baseline": count - baseline_counted,
            "baseline_exclusive_loss_reasons": dict(db.execute("SELECT reason, COUNT(*) FROM reasons GROUP BY reason")),
            "diagnostics": diagnostics}


def native_diagnostics(db: sqlite3.Connection, context: dict) -> dict:
    db.executescript("""
      CREATE TEMP TABLE native_rejections AS SELECT e.id, e.pid, CASE
        WHEN e.has_location=0 THEN 'contactart_fallback_not_exact'
        WHEN NOT EXISTS(SELECT 1 FROM encounter_locations r WHERE r.enc=e.id AND r.lid IS NOT NULL)
          THEN 'no_literal_location_reference'
        WHEN NOT EXISTS(SELECT 1 FROM encounter_locations r JOIN locations l ON l.id=r.lid WHERE r.enc=e.id)
          THEN 'location_not_loaded'
        WHEN NOT EXISTS(SELECT 1 FROM encounter_locations r JOIN locations l ON l.id=r.lid
                        WHERE r.enc=e.id AND l.has_physical_type=1) THEN 'location_missing_physical_type'
        WHEN NOT EXISTS(SELECT 1 FROM encounter_locations r JOIN locations l ON l.id=r.lid
                        WHERE r.enc=e.id AND l.is_ward=1) THEN 'physical_type_not_ward'
        WHEN EXISTS(SELECT 1 FROM encounter_locations r JOIN locations l ON l.id=r.lid
                    WHERE r.enc=e.id AND l.is_ward=1 AND l.any_type_icu=1 AND l.first_type_icu=0)
          THEN 'icu_code_only_in_later_type_concept'
        ELSE 'location_type_not_icu' END AS reason
        FROM effective e LEFT JOIN native_flags n ON n.id=e.id
        WHERE e.icu=1 AND (e.level & 4)>0 AND e.class_code IN ('IMP','stationaer','SS')
        AND NOT (COALESCE(n.real_location,0) OR COALESCE(n.contactart_fallback,0)
                 OR COALESCE(n.provider_fallback,0) OR COALESCE(n.existing_dummy_reference,0));
      CREATE INDEX temp.native_rejections_id ON native_rejections(id);
    """)
    return {**context,
        "native_icu_via_real_location_vk": scalar(db, "SELECT COUNT(*) FROM icu i JOIN native_flags n USING(id) WHERE n.real_location=1"),
        "native_icu_via_contactart_fallback_vk": scalar(db, "SELECT COUNT(*) FROM icu i JOIN native_flags n USING(id) WHERE n.contactart_fallback=1"),
        "native_icu_via_service_provider_fallback_vk": scalar(db, "SELECT COUNT(*) FROM icu i JOIN native_flags n USING(id) WHERE n.provider_fallback=1"),
        "contactart_icu_vk_rejected_by_native": scalar(db, "SELECT COUNT(*) FROM native_rejections"),
        "contactart_icu_vk_rejection_reasons": dict(db.execute("SELECT reason,COUNT(*) FROM native_rejections GROUP BY reason")),
        "positive_contactart_icu_vk_rejection_reasons": dict(db.execute("SELECT r.reason,COUNT(*) FROM native_rejections r JOIN flagged f ON f.id=r.id GROUP BY r.reason"))}


def vn_selection_bounds(db: sqlite3.Connection) -> dict:
    """Conservative patient bounds for unspecified duplicate-VN EK selection."""
    db.executescript("""
      CREATE TEMP TABLE vn_bounds AS SELECT i.id, COUNT(*) AS candidates,
        COUNT(f.id) AS eligible_candidates, MIN(s.pid) AS min_pid, MAX(s.pid) AS max_pid
        FROM positive_icu i JOIN scope s ON s.vn=i.vn AND (s.level & 1)>0
        LEFT JOIN flagged f ON f.id=s.id AND f.class_code IN ('IMP','stationaer','SS')
        WHERE i.method='vn' GROUP BY i.id;
    """)
    fixed = "SELECT f.pid FROM positive_icu i JOIN flagged f ON f.id=i.facility WHERE i.method='partof' AND f.class_code IN ('IMP','stationaer','SS') AND f.pid IS NOT NULL"
    lower = scalar(db, f"SELECT COUNT(*) FROM ({fixed} UNION SELECT min_pid FROM vn_bounds "
                   "WHERE candidates=eligible_candidates AND min_pid=max_pid AND min_pid IS NOT NULL)")
    upper = scalar(db, f"SELECT COUNT(*) FROM ({fixed} UNION SELECT f.pid FROM positive_icu i "
                   "JOIN scope s ON s.vn=i.vn AND (s.level & 1)>0 JOIN flagged f ON f.id=s.id "
                   "WHERE i.method='vn' AND f.class_code IN ('IMP','stationaer','SS') AND f.pid IS NOT NULL)")
    return {"guaranteed_patients_lower_bound": lower, "possible_patients_upper_bound": upper,
            "icu_vk_with_multiple_candidate_ek": scalar(db, "SELECT COUNT(*) FROM vn_bounds WHERE candidates>1")}


def write_details(db: sqlite3.Connection, path: str, profile: str, mode: str, detection: str) -> None:
    with open(path, "a", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        for pid, reason in db.execute("SELECT pid, reason FROM reasons ORDER BY pid"):
            positive = db.execute("SELECT enc FROM positive_sources WHERE pid=? AND enc IS NOT NULL ORDER BY enc LIMIT 1", (pid,)).fetchone()
            icu = db.execute("SELECT id, effective_vn FROM icu WHERE pid=? ORDER BY id LIMIT 1", (pid,)).fetchone()
            first = db.execute("SELECT id FROM counted WHERE pid=? ORDER BY start,id LIMIT 1", (pid,)).fetchone()
            writer.writerow((profile, mode, detection, pid, reason, positive[0] if positive else "",
                             icu[0] if icu else "", icu[1] if icu else "", first[0] if first else ""))


def compare_modes(db: sqlite3.Connection, profile: str, detection: str = "contactart") -> dict:
    no, yes = f"result_{profile}_without_partof_{detection}", f"result_{profile}_with_partof_{detection}"
    return {"both_modes": scalar(db, f"SELECT COUNT(*) FROM {no} n JOIN {yes} y USING(pid)"),
            "only_with_partof": scalar(db, f"SELECT COUNT(*) FROM {yes} WHERE pid NOT IN (SELECT pid FROM {no})"),
            "only_without_partof": scalar(db, f"SELECT COUNT(*) FROM {no} WHERE pid NOT IN (SELECT pid FROM {yes})")}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fhir-base-url", default=os.environ.get("FHIR_BASE_URL"))
    p.add_argument("--mode", choices=("both", *MODES), default="both")
    p.add_argument("--auth", choices=("auto", "none", "basic", "bearer"), default="auto")
    p.add_argument("--page-size", type=int, default=500)
    p.add_argument("--patient-batch-size", type=int, default=25)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--cutoff", default="2020-01-27T00:00:00+01:00")
    p.add_argument("--ddp-pcr-loincs", default=",".join(COVID_PCR_LOINC_CODES))
    p.add_argument("--ddp-variant-loincs", default=",".join(COVID_VARIANT_LOINC_CODES))
    p.add_argument("--icu-detection", choices=("contactart", "native", "both"), default="contactart")
    p.add_argument("--icu-service-provider-ids", default="")
    p.add_argument("--location-batch-size", type=int, default=150)
    p.add_argument("--preset-name", default="custom")
    p.add_argument("--reference-ddp-count", type=int)
    p.add_argument("--condition-link-mode", choices=("direct", "revinclude"), default="direct")
    p.add_argument("--filter-patient-retrieval", action=argparse.BooleanOptionalAction, default=True,
                   help="Match DDP SearchConfiguration (default true); disable with --no-filter-patient-retrieval.")
    p.add_argument("--filter-resources-by-date", action=argparse.BooleanOptionalAction, default=True,
                   help="Match DDP SearchConfiguration (default true); disable with --no-filter-resources-by-date.")
    p.add_argument("--keep-interpretation", action="store_true")
    p.add_argument("--work-db", help="New local SQLite file; never overwrite an existing file.")
    p.add_argument("--keep-work-db", action="store_true")
    p.add_argument("--reuse-work-db", help="Reuse a completed NEW-format snapshot with exactly the same retrieval settings.")
    p.add_argument("--details-csv", help="Optional NEW local file containing patient/encounter IDs.")
    return p


def auth_headers(args) -> dict:
    user, password = os.environ.get("FHIR_USER"), os.environ.get("FHIR_PASSWORD")
    token = os.environ.get("FHIR_BEARER_TOKEN")
    auth = args.auth
    if auth == "auto":
        auth = "bearer" if token else "basic" if user else "none"
    if auth == "bearer":
        if not token:
            raise ValueError("Set FHIR_BEARER_TOKEN locally for bearer authentication.")
        return {"Authorization": "Bearer " + token}
    if auth == "basic":
        if not user:
            raise ValueError("Set FHIR_USER locally for basic authentication.")
        if password is None:
            if not sys.stdin.isatty():
                raise ValueError("Set FHIR_PASSWORD locally or run interactively for the hidden password prompt.")
            password = getpass.getpass("FHIR password: ")
        value = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        return {"Authorization": "Basic " + value}
    return {}


def snapshot_settings(args) -> dict:
    return {"server_hash": hashlib.sha256(args.fhir_base_url.rstrip("/").encode()).hexdigest(),
            "ddp_pcr_loincs": sorted(set(args.ddp_pcr_loincs.split(","))),
            "ddp_variant_loincs": sorted(set(args.ddp_variant_loincs.split(","))),
            "condition_link_mode": args.condition_link_mode, "cutoff": args.cutoff,
            "filter_patient_retrieval": args.filter_patient_retrieval,
            "filter_resources_by_date": args.filter_resources_by_date,
            "keep_interpretation": args.keep_interpretation,
            "icu_service_provider_ids": sorted(set(args.icu_service_provider_ids.split(",")) - {""})}


def compare_detection(db: sqlite3.Connection, profile: str, mode: str) -> dict:
    contact, native = f"result_{profile}_{mode}_contactart", f"result_{profile}_{mode}_native"
    return {"both_detections": scalar(db, f"SELECT COUNT(*) FROM {contact} c JOIN {native} n USING(pid)"),
            "only_contactart": scalar(db, f"SELECT COUNT(*) FROM {contact} WHERE pid NOT IN (SELECT pid FROM {native})"),
            "only_native": scalar(db, f"SELECT COUNT(*) FROM {native} WHERE pid NOT IN (SELECT pid FROM {contact})")}


def main(argv: list[str] | None = None, default_mode: str = "both", defaults: dict | None = None) -> None:
    p = parser()
    p.set_defaults(mode=default_mode)
    if defaults:
        p.set_defaults(**defaults)
    args = p.parse_args(argv)
    if not args.fhir_base_url:
        p.error("Set FHIR_BASE_URL or use --fhir-base-url.")
    if not 1 <= args.page_size <= 10000 or min(args.patient_batch_size, args.location_batch_size, args.timeout) < 1:
        p.error("Page size must be 1..10000; batch sizes and timeout must be positive.")
    if args.work_db and args.reuse_work_db:
        p.error("Use either --work-db or --reuse-work-db.")
    if args.reference_ddp_count is not None and args.reference_ddp_count < 0:
        p.error("--reference-ddp-count must be nonnegative.")
    for field in ("ddp_pcr_loincs", "ddp_variant_loincs", "icu_service_provider_ids"):
        setattr(args, field, ",".join(sorted({c.strip() for c in getattr(args, field).split(",") if c.strip()})))
    if not args.ddp_pcr_loincs:
        p.error("--ddp-pcr-loincs must contain at least one code.")
    if not timestamp(args.cutoff):
        p.error("--cutoff must be an ISO date/time.")
    if args.details_csv:
        # Open exclusively before any clinical work; details remain only in the clinical network.
        with open(args.details_csv, "x", newline="", encoding="utf-8") as output:
            csv.writer(output).writerow(("profile", "mode", "icu_detection", "patient_id", "reason",
                                        "example_positive_encounter", "example_icu_vk",
                                        "example_icu_effective_vn", "first_counted_icu_ek"))
    if args.reuse_work_db:
        path = Path(args.reuse_work_db).resolve()
        if not path.is_file():
            raise ValueError("--reuse-work-db must point to an existing completed snapshot.")
    elif args.work_db:
        path = Path(args.work_db)
        with open(path, "xb"):
            pass
    else:
        fd, name = tempfile.mkstemp(prefix="ddp-covid-icu-", suffix=".sqlite")
        os.close(fd)
        path = Path(name)
    db = (sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if args.reuse_work_db
          else sqlite3.connect(path))
    db.execute("PRAGMA cache_size=-32768")
    db.execute("PRAGMA temp_store=FILE")
    stats = Counter()
    started = time.monotonic()
    try:
        detections = ("contactart", "native") if args.icu_detection == "both" else (args.icu_detection,)
        primary_detection = "native" if "native" in detections else "contactart"
        if args.reuse_work_db:
            try:
                metadata = dict(db.execute("SELECT key,value FROM metadata"))
            except sqlite3.Error:
                raise ValueError("Old/incomplete snapshot format; run a new native comparison first.") from None
            if metadata.get("format") != "2" or metadata.get("complete") != "true":
                raise ValueError("Old/incomplete snapshot format; run a new comparison first.")
            if json.loads(metadata["settings"]) != snapshot_settings(args):
                raise ValueError("Snapshot retrieval settings differ from this run; rerun or match the original settings.")
            if "native" in detections and metadata.get("locations_complete") != "true":
                raise ValueError("Snapshot has no completed Location retrieval; run a new native comparison.")
            stats.update(json.loads(metadata["stats"]))
            progress("Vollstaendigen lokalen Snapshot wiederverwendet; keine FHIR-Anfragen")
        else:
            db.executescript(SCHEMA)
            client = FhirClient(args.fhir_base_url, auth_headers(args), args.timeout)
            load_baseline(db, client, args.page_size)
            load_sources(db, client, args, stats)
            load_encounters(db, client, args, stats)
            if "native" in detections:
                load_locations(db, client, args, stats)
            db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                           (("format", "2"), ("complete", "true"),
                            ("settings", json.dumps(snapshot_settings(args), sort_keys=True)),
                            ("stats", json.dumps(stats))))
            db.commit()
        modes = MODES if args.mode == "both" else (args.mode,)
        profiles = ("ddp_configured",) if set(args.ddp_pcr_loincs.split(",")) >= QUERY_LOINCS else PROFILES
        runs = {}
        for profile in profiles:
            runs[profile] = {}
            for mode in modes:
                results = {}
                for detection in detections:
                    progress(f"Berechne {profile} / {mode} / {detection}")
                    results[detection] = analyze(db, args, profile, mode, detection)
                runs[profile][mode] = dict(results[primary_detection])
                if args.icu_detection == "both":
                    runs[profile][mode]["contactart_countercheck"] = results["contactart"]
        warnings = ["Relevant native ICU/Location and reference paths checked against the build-tag sources "
                    "V0.5.7-update.4 (42185bbe131773f1407f27f49f2fd5c50abb4536); the live registry digest/pod was not inspected.",
                    "Only the ICU_undifferentiated component is calculated, not Outpatient/Normal_ward.",
                    "Separate searches may see different Blaze database snapshots; prefer a stable import window."]
        if "contactart" in detections:
            warnings.append("Contactart countercheck intentionally counts intensivstationaer even with a non-ICU Location.")
        if any(r["diagnostics"]["vn_groups_with_multiple_ek"] for group in runs.values() for r in group.values()):
            warnings.append("Duplicate EK visit numbers found: this script chooses MIN(EK id); "
                            "DDP iteration order is unspecified, so these counts are not an exact reproduction.")
        report = {"baseline_patient_query_count": scalar(db, "SELECT COUNT(*) FROM baseline"),
                  "configuration": {"mode": args.mode, "icu_system": CONTACT_ART,
                      "preset_name": args.preset_name, "icu_detection": args.icu_detection,
                      "primary_icu_detection": primary_detection,
                      "icu_code": "intensivstationaer", "query_loincs": sorted(QUERY_LOINCS),
                      "ddp_pcr_loincs": sorted(set(args.ddp_pcr_loincs.split(","))),
                      "ddp_variant_loincs": sorted(set(args.ddp_variant_loincs.split(",")) - {""}),
                      "icu_service_provider_ids": sorted(set(args.icu_service_provider_ids.split(",")) - {""}),
                      "query_loincs_missing_from_ddp_configuration": sorted(QUERY_LOINCS - set(args.ddp_pcr_loincs.split(","))),
                      "page_size": args.page_size, "patient_batch_size": args.patient_batch_size,
                      "location_batch_size": args.location_batch_size,
                      "cutoff": args.cutoff, "condition_link_mode": args.condition_link_mode,
                      "filter_patient_retrieval": args.filter_patient_retrieval,
                      "filter_resources_by_date": args.filter_resources_by_date,
                      "mimic_ddp_interpretation_removal": not args.keep_interpretation},
                  "retrieval": {**stats, "observations_distinct": scalar(db, "SELECT COUNT(*) FROM observations"),
                      "conditions_distinct": scalar(db, "SELECT COUNT(*) FROM conditions"),
                      "patients_loaded": scalar(db, "SELECT COUNT(*) FROM loaded_patients"),
                      "encounters_distinct": scalar(db, "SELECT COUNT(*) FROM encounters"),
                      "encounters_invalid_status": scalar(db, "SELECT COUNT(*) FROM encounters WHERE status_ok=0"),
                      "encounters_missing_or_pre_cutoff_start": scalar(db, "SELECT COUNT(*) FROM encounters WHERE date_ok=0"),
                      "encounters_icu_code_in_other_system": scalar(db, "SELECT COUNT(*) FROM encounters WHERE icu_any_system=1 AND icu=0"),
                      "locations_distinct": scalar(db, "SELECT COUNT(*) FROM locations"),
                      "location_ids_referenced": scalar(db, "SELECT COUNT(DISTINCT lid) FROM encounter_locations"),
                      "reused_snapshot": bool(args.reuse_work_db)},
                  "runs": runs, "warnings": warnings,
                  "elapsed_seconds": round(time.monotonic() - started, 1)}
        report["baseline_source_checks"] = {
            "patients_with_raw_positive_query_observation": scalar(db, "SELECT COUNT(DISTINCT o.pid) "
                "FROM observations o JOIN baseline b ON b.pid=o.pid WHERE o.query_code=1 AND o.query_positive=1"),
            "patients_with_raw_contactart_icu_encounter": scalar(db, "SELECT COUNT(DISTINCT e.pid) "
                "FROM encounters e JOIN baseline b ON b.pid=e.pid WHERE e.icu=1"),
            "patients_with_positive_query_loinc_missing_from_ddp_list": scalar(db, "SELECT COUNT(DISTINCT o.pid) "
                "FROM observations o JOIN baseline b ON b.pid=o.pid WHERE o.query_code=1 "
                "AND o.query_positive=1 AND o.configured=0"),
        }
        if any(report["baseline_source_checks"][key] != report["baseline_patient_query_count"] for key in (
                "patients_with_raw_positive_query_observation", "patients_with_raw_contactart_icu_encounter")):
            warnings.append("Patient baseline could not be verified from the retrieved Observation/Encounter resources. "
                            "Check permissions, ignored search parameters, and concurrent imports before interpreting losses.")
        if args.mode == "both":
            report["partof_comparison"] = {profile: compare_modes(db, profile, primary_detection) for profile in profiles}
        report["loinc_comparison"] = {}
        for mode in modes:
            a, b = f"result_ddp_configured_{mode}_{primary_detection}", f"result_ddp_plus_query_loincs_{mode}_{primary_detection}"
            report["loinc_comparison"][mode] = ({
                "additional_counted_with_query_loincs": scalar(db, f"SELECT COUNT(*) FROM {b} WHERE pid NOT IN (SELECT pid FROM {a})"),
                "no_longer_counted_with_query_loincs": scalar(db, f"SELECT COUNT(*) FROM {a} WHERE pid NOT IN (SELECT pid FROM {b})")}
                if len(profiles) > 1 else {"profiles_equivalent": True, "additional_counted_with_query_loincs": 0,
                                         "no_longer_counted_with_query_loincs": 0})
        if args.icu_detection == "both":
            report["icu_detection_comparison"] = {profile: {mode: compare_detection(db, profile, mode)
                                                     for mode in modes} for profile in profiles}
        if args.reference_ddp_count is not None:
            report["ddp_count_comparison"] = {"reference_ddp_count": args.reference_ddp_count, "runs": {}}
            for profile in profiles:
                report["ddp_count_comparison"]["runs"][profile] = {}
                for mode in modes:
                    run = runs[profile][mode]
                    bounds = run["diagnostics"]["vn_selection_bounds"]
                    report["ddp_count_comparison"]["runs"][profile][mode] = {
                        "calculated_patients": run["patient_count"] if run["data_item"] else None,
                        "delta": run["patient_count"] - args.reference_ddp_count if run["data_item"] else None,
                        "reference_within_vn_selection_bounds": bool(run["data_item"] and
                            bounds["guaranteed_patients_lower_bound"] <= args.reference_ddp_count <= bounds["possible_patients_upper_bound"])}
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        db.close()
        if args.reuse_work_db:
            progress(f"Wiederverwendeter Snapshot unveraendert behalten: {path}")
        elif args.keep_work_db:
            progress(f"Lokale Arbeitsdatenbank behalten: {path}")
        else:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Debug-Lauf abgebrochen: {exc}", file=sys.stderr)
        sys.exit(1)
