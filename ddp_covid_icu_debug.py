#!/usr/bin/env python3
"""Read-only, paged COVID ICU comparison; compact FHIR fields are staged on disk.

Python 3.10+, standard library only. See docs/covid-icu-debug.md for the modeled
DDP v0.5.7 behavior, intentional contactart ICU override, and clinical usage.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from datetime import datetime, timezone
import getpass
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
            int(bool(resource.get("location"))), start, int(status_ok),
            int(bool(start and start >= cutoff)), vn, int(has_vn), ref_id(resource.get("partOf")))


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
    retrieval = configured | set(COVID_VARIANT_LOINC_CODES)
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
                  "_elements": "id,subject,status,class,type,identifier,partOf,period,location"}
        # Deliberately retrieve invalid/old encounters too, then report their exclusion locally.
        for bundle in client.pages("Encounter", params):
            with db:
                for encounter in resources(bundle):
                    if encounter.get("resourceType") == "Encounter" and encounter.get("id"):
                        stats["encounters_received"] += 1
                        db.execute("INSERT OR REPLACE INTO encounters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   encounter_row(encounter, cutoff))
        batch += 1
        progress(f"Encounter-Patientenbatch {batch}: {len(patients)} Patienten; "
                 f"{stats['encounters_received']} Ressourcen verarbeitet")


def analyze(db: sqlite3.Connection, args, profile: str, mode: str) -> dict:
    extended = profile == "ddp_plus_query_loincs"
    code = "MAX(configured,query_code)" if extended else "configured"
    source_flag = ("extended_" if extended else "configured_") + (
        "positive" if args.filter_patient_retrieval else "all")
    if args.condition_link_mode == "revinclude":
        # In v0.5.7 condition date filtering logs differences but does NOT replace the list.
        condition_filter_note = "revinclude: no recordedDate exclusion, matching v0.5.7 list-retention behavior"
    else:
        condition_filter_note = "direct: optional server recorded-date filter; no additional local date exclusion"
    for table in ("scope", "positive_sources", "partof_links", "vn_facilities", "links", "effective",
                  "positive_vns", "flagged", "icu", "mapped_icu", "positive_icu", "counted", "reasons"):
        db.execute(f"DROP TABLE IF EXISTS temp.{table}")
    db.executescript(f"""
      CREATE TEMP TABLE scope AS
        SELECT e.* FROM encounters e JOIN source_patients p ON p.pid=e.pid
        WHERE p.{source_flag}=1 AND e.status_ok=1 AND e.date_ok=1;
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
      CREATE TEMP TABLE icu AS SELECT * FROM effective WHERE icu=1 AND (level & 4)>0
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
    name = profile + "_" + mode
    db.execute(f"CREATE TEMP TABLE result_{name} AS SELECT pid, MIN(id) AS example_ek FROM counted "
               "WHERE pid IS NOT NULL GROUP BY pid")
    db.execute(f"CREATE UNIQUE INDEX temp.result_{name}_pid ON result_{name}(pid)")
    db.execute(f"CREATE TEMP TABLE reasons_{name} AS SELECT * FROM reasons")
    count = scalar(db, f"SELECT COUNT(*) FROM result_{name}")
    baseline_counted = scalar(db, "SELECT COUNT(*) FROM reasons WHERE reason='counted'")
    diagnostics = {
        "eligible_encounters": scalar(db, "SELECT COUNT(*) FROM scope"),
        "eligible_icu_vk": scalar(db, "SELECT COUNT(*) FROM icu"),
        "icu_vk_with_location_still_counted_by_contactart": scalar(db, "SELECT COUNT(*) FROM icu WHERE has_location=1"),
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
    if args.details_csv:
        write_details(db, args.details_csv, profile, mode)
    return {"data_item": {"itemname": "cumulative.maxtreatmentlevel", "itemtype": "aggregated",
                           "data": {"ICU_undifferentiated": count}},
            "baseline_counted": baseline_counted,
            "baseline_not_counted": scalar(db, "SELECT COUNT(*) FROM baseline") - baseline_counted,
            "counted_outside_baseline": count - baseline_counted,
            "baseline_exclusive_loss_reasons": dict(db.execute("SELECT reason, COUNT(*) FROM reasons GROUP BY reason")),
            "diagnostics": diagnostics}


def write_details(db: sqlite3.Connection, path: str, profile: str, mode: str) -> None:
    with open(path, "a", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        for pid, reason in db.execute("SELECT pid, reason FROM reasons ORDER BY pid"):
            positive = db.execute("SELECT enc FROM positive_sources WHERE pid=? AND enc IS NOT NULL ORDER BY enc LIMIT 1", (pid,)).fetchone()
            icu = db.execute("SELECT id, effective_vn FROM icu WHERE pid=? ORDER BY id LIMIT 1", (pid,)).fetchone()
            first = db.execute("SELECT id FROM counted WHERE pid=? ORDER BY start,id LIMIT 1", (pid,)).fetchone()
            writer.writerow((profile, mode, pid, reason, positive[0] if positive else "",
                             icu[0] if icu else "", icu[1] if icu else "", first[0] if first else ""))


def compare_modes(db: sqlite3.Connection, profile: str) -> dict:
    no, yes = f"result_{profile}_without_partof", f"result_{profile}_with_partof"
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
    p.add_argument("--condition-link-mode", choices=("direct", "revinclude"), default="direct")
    p.add_argument("--filter-patient-retrieval", action=argparse.BooleanOptionalAction, default=True,
                   help="Match DDP SearchConfiguration (default true); disable with --no-filter-patient-retrieval.")
    p.add_argument("--filter-resources-by-date", action=argparse.BooleanOptionalAction, default=True,
                   help="Match DDP SearchConfiguration (default true); disable with --no-filter-resources-by-date.")
    p.add_argument("--keep-interpretation", action="store_true")
    p.add_argument("--work-db", help="New local SQLite file; never overwrite an existing file.")
    p.add_argument("--keep-work-db", action="store_true")
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


def main(argv: list[str] | None = None, default_mode: str = "both") -> None:
    p = parser()
    p.set_defaults(mode=default_mode)
    args = p.parse_args(argv)
    if not args.fhir_base_url:
        p.error("Set FHIR_BASE_URL or use --fhir-base-url.")
    if not 1 <= args.page_size <= 10000 or args.patient_batch_size < 1 or args.timeout < 1:
        p.error("Page size must be 1..10000; patient batch size and timeout must be positive.")
    if not timestamp(args.cutoff):
        p.error("--cutoff must be an ISO date/time.")
    if args.details_csv:
        # Open exclusively before any clinical work; details remain only in the clinical network.
        with open(args.details_csv, "x", newline="", encoding="utf-8") as output:
            csv.writer(output).writerow(("profile", "mode", "patient_id", "reason",
                                        "example_positive_encounter", "example_icu_vk",
                                        "example_icu_effective_vn", "first_counted_icu_ek"))
    if args.work_db:
        path = Path(args.work_db)
        with open(path, "xb"):
            pass
    else:
        fd, name = tempfile.mkstemp(prefix="ddp-covid-icu-", suffix=".sqlite")
        os.close(fd)
        path = Path(name)
    db = sqlite3.connect(path)
    stats = Counter()
    started = time.monotonic()
    try:
        db.executescript(SCHEMA)
        client = FhirClient(args.fhir_base_url, auth_headers(args), args.timeout)
        load_baseline(db, client, args.page_size)
        load_sources(db, client, args, stats)
        load_encounters(db, client, args, stats)
        modes = MODES if args.mode == "both" else (args.mode,)
        runs = {}
        for profile in PROFILES:
            runs[profile] = {}
            for mode in modes:
                progress(f"Berechne {profile} / {mode}")
                runs[profile][mode] = analyze(db, args, profile, mode)
        warnings = ["Modeled against upstream DDP v0.5.7, not a verified V0.5.7-update.4 image.",
                    "ICU detection intentionally uses kontaktart-de|intensivstationaer even when location exists; "
                    "the native DDP Location gate/fallback is not modeled.",
                    "Only the ICU_undifferentiated component is calculated, not Outpatient/Normal_ward.",
                    "Separate searches may see different Blaze database snapshots; prefer a stable import window."]
        if any(r["diagnostics"]["vn_groups_with_multiple_ek"] for group in runs.values() for r in group.values()):
            warnings.append("Duplicate EK visit numbers found: this script chooses MIN(EK id); "
                            "DDP iteration order is unspecified, so these counts are not an exact reproduction.")
        report = {"baseline_patient_query_count": scalar(db, "SELECT COUNT(*) FROM baseline"),
                  "configuration": {"mode": args.mode, "icu_system": CONTACT_ART,
                      "icu_code": "intensivstationaer", "query_loincs": sorted(QUERY_LOINCS),
                      "ddp_pcr_loincs": sorted(set(args.ddp_pcr_loincs.split(","))),
                      "query_loincs_missing_from_ddp_configuration": sorted(QUERY_LOINCS - set(args.ddp_pcr_loincs.split(","))),
                      "page_size": args.page_size, "patient_batch_size": args.patient_batch_size,
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
                      "encounters_icu_code_in_other_system": scalar(db, "SELECT COUNT(*) FROM encounters WHERE icu_any_system=1 AND icu=0")},
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
            report["partof_comparison"] = {profile: compare_modes(db, profile) for profile in PROFILES}
        report["loinc_comparison"] = {}
        for mode in modes:
            a, b = f"result_ddp_configured_{mode}", f"result_ddp_plus_query_loincs_{mode}"
            report["loinc_comparison"][mode] = {
                "additional_counted_with_query_loincs": scalar(db, f"SELECT COUNT(*) FROM {b} WHERE pid NOT IN (SELECT pid FROM {a})"),
                "no_longer_counted_with_query_loincs": scalar(db, f"SELECT COUNT(*) FROM {a} WHERE pid NOT IN (SELECT pid FROM {b})")}
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        db.close()
        if args.keep_work_db:
            progress(f"Lokale Arbeitsdatenbank behalten: {path}")
        else:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Debug-Lauf abgebrochen: {exc}", file=sys.stderr)
        sys.exit(1)
