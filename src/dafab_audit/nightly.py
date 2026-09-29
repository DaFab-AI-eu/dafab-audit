"""Nightly catalogue audit through the public API, and the static site that reports it.

``collect`` audits one unit, a collection of the public scope or one auxiliary scope, and
writes a JSON state file. ``publish`` merges the unit files into the static site.

Everything here reads the catalogue through the shared read profile of dafab-client and the
public discovery API. No database, no storage credentials, no secrets of any kind.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import random
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SITE_URL_DEFAULT = "https://dafab-ai-eu.github.io/dafab-audit"
STAC_ROOT_DEFAULT = "https://dafab.cern.ch/stac"
DISCOVERY_API_DEFAULT = "https://dafab.cern.ch/discover/api"
DISCOVERY_SITE_DEFAULT = "https://dafab.cern.ch/discover/"
PUBLIC_SCOPE = "dafab"
STATE_SCHEMA_VERSION = 1
HISTORY_LIMIT = 400
SEARCH_PAGE_SIZE = 100
USER_AGENT = "dafab-audit-nightly/1"

UNITS: dict[str, dict[str, Any]] = {
    "sentinel_2_l2a": {"scope": PUBLIC_SCOPE, "collection": "sentinel_2_l2a", "kind": "original"},
    "water_analysis": {"scope": PUBLIC_SCOPE, "collection": "water_analysis", "kind": "derived"},
    "smart_agriculture": {"scope": PUBLIC_SCOPE, "collection": "smart_agriculture", "kind": "derived"},
    "hand": {"scope": "hand", "collection": None, "kind": "auxiliary"},
    "worldcover": {"scope": "worldcover", "collection": None, "kind": "auxiliary"},
    "gfm": {"scope": "gfm", "collection": None, "kind": "auxiliary"},
}
USE_CASE_COLLECTIONS = ("water_analysis", "smart_agriculture")
SERVED_KEYS = ("name", "scope")  # added by the API around the stored document

# --------------------------------------------------------------------------------------------
# pure helpers, covered by the unit tests


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stored_document(served: dict[str, Any]) -> dict[str, Any]:
    """The STAC document as stored, without the keys the API adds when serving it."""
    return {key: value for key, value in served.items() if key not in SERVED_KEYS}


def document_hash(document: dict[str, Any]) -> str:
    payload = json.dumps(stored_document(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def processing_version(document: dict[str, Any]) -> str | None:
    value = (document.get("properties") or {}).get("processing:version")
    return str(value) if value not in (None, "") else None


def product_source(item_id: str, collection: str) -> tuple[str, str] | None:
    """Split a product id into its source image id and version tag, ``(image, "300")``."""
    marker = f"_{collection}_"
    if marker not in item_id:
        return None
    image, _, tag = item_id.rpartition(marker)
    return (image, tag) if image and tag and "_" not in tag else None


def managed_asset_keys(document: dict[str, Any], stac_root: str) -> tuple[list[str], list[str]]:
    """Assets whose file DaFab publishes itself, and the keys of the external references.

    A Sentinel-2 document keeps the links of the whole Copernicus product, an ESA
    WorldCover tile links to previews on ESA's servers. Only assets served under the
    catalogue's own asset path are attached in Rucio and verifiable here.
    """
    assets = document.get("assets")
    if not isinstance(assets, dict):
        return [], []
    prefix = f"{stac_root.rstrip('/')}/assets/"
    managed = sorted(key for key, asset in assets.items() if isinstance(asset, dict) and str(asset.get("href") or "").startswith(prefix))
    return managed, sorted(set(assets) - set(managed))


def attached_file_for(item_id: str, asset_key: str, attached: Iterable[str]) -> str | None:
    """The attached file that carries ``asset_key``; files are named ``<item>_<key>.<extension>``."""
    prefix = f"{item_id}_{asset_key}."
    matches = [name for name in attached if name.startswith(prefix) and "." not in name[len(prefix):]]
    return matches[0] if len(matches) == 1 else None


def rotation_selected(item_id: str, period: int, day_index: int) -> bool:
    """Deterministic rotation so that every item is inventoried once per ``period`` days."""
    if period <= 1:
        return True
    bucket = int(hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:8], 16) % period
    return bucket == day_index % period


def structural_issues(document: dict[str, Any]) -> list[str]:
    """Checks that apply to every STAC item, including the auxiliary scopes without a schema."""
    issues = []
    if document.get("type") != "Feature":
        issues.append("type is not Feature")
    if not isinstance(document.get("id"), str) or not document["id"]:
        issues.append("id missing")
    if not isinstance(document.get("assets"), dict) or not document["assets"]:
        issues.append("no assets")
    else:
        for key, asset in document["assets"].items():
            if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
                issues.append(f"asset {key} has no href")
    geometry = document.get("geometry")
    if geometry is not None and not isinstance(geometry, dict):
        issues.append("geometry is not an object")
    bbox = document.get("bbox")
    if geometry is not None and (not isinstance(bbox, list) or len(bbox) not in (4, 6) or not all(isinstance(v, (int, float)) for v in bbox)):
        issues.append("bbox is not a list of 4 or 6 numbers")
    properties = document.get("properties") or {}
    if not isinstance(properties.get("datetime"), str) and not (
        isinstance(properties.get("start_datetime"), str) and isinstance(properties.get("end_datetime"), str)
    ):
        issues.append("properties.datetime missing, and no start_datetime with end_datetime")
    return issues


INVENTORY_KEYS = ("inventory_at", "attached", "available", "inventory_issues", "surplus", "surplus_bytes", "surplus_issues")
VERIFICATION_KEYS = ("verified_at", "verify_ok", "verified_bytes", "verify_issues")


def assets_signature(document: dict[str, Any], managed: Iterable[str]) -> str:
    """Fingerprint of what a byte check covers, the managed assets with their href, size and checksum.

    Metadata elsewhere in the document, a geometry synchronised from the source image for
    instance, leaves it unchanged."""
    assets = document.get("assets") or {}
    declared = [[key, *((assets.get(key) or {}).get(field) for field in ("href", "file:size", "file:checksum"))] for key in sorted(managed)]
    return hashlib.sha256(json.dumps(declared, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def files_signature(files: dict[str, dict[str, Any]]) -> str:
    """Fingerprint of the files the inventory resolved, their names, sizes and recorded checksums."""
    resolved = sorted([key, meta.get("name"), meta.get("bytes"), meta.get("adler32")] for key, meta in files.items())
    return hashlib.sha256(json.dumps(resolved, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def merge_previous(record: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Carry forward what an earlier night established.

    The inventory carries over while the document is unchanged; a changed item is inventoried
    again tonight. The byte check carries over while the managed assets are unchanged, so a
    metadata edit does not send the item back for a download. A state written before assets
    were fingerprinted counts any document change as an asset change."""
    if not previous:
        record.update({"changed": False, "first_seen": record["audited_at"], "changed_at": None, "assets_changed_at": None})
        return record
    same_document = previous.get("hash") == record["hash"]
    if previous.get("assets_signature"):
        same_assets = previous["assets_signature"] == record.get("assets_signature")
    else:
        same_assets = same_document
    record["changed"] = not same_document
    record["first_seen"] = previous.get("first_seen") or record["audited_at"]
    record["changed_at"] = previous.get("changed_at") if same_document else record["audited_at"]
    record["assets_changed_at"] = previous.get("assets_changed_at") if same_assets else record["audited_at"]
    carried = ["files_signature"]
    if same_document:
        carried += INVENTORY_KEYS
    if same_assets:
        carried += VERIFICATION_KEYS
    for key in carried:
        if key in previous and key not in record:
            record[key] = previous[key]
    return record


def note_file_changes(record: dict[str, Any], audited_at: str) -> None:
    """Compare the files tonight's inventory resolved with the last known ones.

    A replaced file, same name with another size or checksum, sends the item back for a byte
    check even when its document did not change. The first fingerprint is adopted silently."""
    files = record.get("_files")
    if files is None:
        return
    signature = files_signature(files)
    known = record.get("files_signature")
    if known and known != signature:
        record["assets_changed_at"] = audited_at
        for key in VERIFICATION_KEYS:
            record.pop(key, None)
    record["files_signature"] = signature


def unit_baseline(unit: dict[str, Any]) -> str:
    """The unit's first audited night. Items first seen then are the initial catalogue."""
    return unit.get("baseline") or min((row["first_seen"] for row in unit.get("items", []) if row.get("first_seen")), default="")


def verification_due(record: dict[str, Any], baseline: str) -> str | None:
    """Since when the item's bytes wait for a check, or None when a check already covers them.

    An item waits from the last change of its managed assets or their files, from its first
    appearance when it was published after the baseline night, or from its last failed check,
    which is retried. Items of the initial catalogue whose assets never changed are left to
    the random sample."""
    verified = record.get("verified_at")
    if verified and record.get("verify_ok") is False:
        return verified
    since = record.get("assets_changed_at")
    if not since and (record.get("first_seen") or "") > baseline:
        since = record["first_seen"]
    if since and (not verified or verified < since):
        return since
    return None


def expected_bytes(record: dict[str, Any]) -> int:
    return sum(int(meta.get("bytes") or 0) for meta in (record.get("_files") or {}).values())


def select_for_verification(waiting: list[dict[str, Any]], sample: list[dict[str, Any]], max_items: int, max_bytes: int) -> list[dict[str, Any]]:
    """Waiting items first, oldest first, while the random sample keeps up to half of the items
    and bytes so that unchanged items are still checked every night. The first pick is always
    taken, so a unit whose items all exceed the budget still verifies one item per night."""
    sample = [r for r in sample if r.get("_files")]
    sampled = {r["id"] for r in sample}
    waiting = [r for r in waiting if r.get("_files") and r["id"] not in sampled]
    reserve_items = min(len(sample), max_items // 2)
    reserve_bytes = min(sum(expected_bytes(r) for r in sample), max_bytes // 2)
    chosen: list[dict[str, Any]] = []

    def take(pool: list[dict[str, Any]], limit: int, budget: int) -> int:
        for record in pool:
            if len(chosen) >= limit:
                break
            size = expected_bytes(record)
            if chosen and size > budget:
                continue
            chosen.append(record)
            budget -= size
        return budget

    left = take(waiting, max_items - reserve_items, max_bytes - reserve_bytes)
    take(sample, max_items, left + reserve_bytes)
    return chosen


def summarise(units: dict[str, dict[str, Any]], generated_at: str) -> dict[str, Any]:
    """The matrix, the coverage and the anomaly list the page renders."""
    cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    anomalies: list[dict[str, Any]] = []
    images: set[str] = set()
    products: dict[str, dict[str, set[str]]] = {collection: {} for collection in USE_CASE_COLLECTIONS}

    for unit_name, unit in units.items():
        scope = unit["scope"]
        collection = unit.get("collection") or scope
        baseline = unit_baseline(unit)
        for record in unit.get("items", []):
            version = record.get("version") or "unversioned"
            cell = cells.setdefault((scope, collection, version), {
                "scope": scope, "collection": collection, "version": version, "unit": unit_name,
                "items": 0, "valid": 0, "assets_complete": 0, "inventoried": 0, "available": 0,
                "verified": 0, "verify_failed": 0, "verify_waiting": 0, "changed": 0, "anomalies": 0, "external_assets": 0,
                "surplus_items": 0, "surplus_bytes": 0, "issue_counts": {},
            })
            if verification_due(record, baseline):
                cell["verify_waiting"] += 1
            cell["items"] += 1
            cell["external_assets"] += int(record.get("external_assets") or 0)
            surplus_issues = list(record.get("surplus_issues") or [])
            if record.get("surplus"):
                cell["surplus_items"] += 1
                cell["surplus_bytes"] += int(record.get("surplus_bytes") or 0)
            issues = list(record.get("schema_issues") or []) + list(record.get("inventory_issues") or []) + list(record.get("verify_issues") or [])
            for issue in set(issues + [i for i in surplus_issues if not i.startswith("and ")]):
                cell["issue_counts"][issue] = cell["issue_counts"].get(issue, 0) + 1
            if record.get("valid"):
                cell["valid"] += 1
            if record.get("changed"):
                cell["changed"] += 1
            if record.get("inventory_at"):
                cell["inventoried"] += 1
                if not record.get("inventory_issues"):
                    cell["assets_complete"] += 1
                if record.get("available") and record["available"] == record.get("attached"):
                    cell["available"] += 1
            if record.get("verified_at"):
                cell["verified"] += 1
                if record.get("verify_ok") is False:
                    cell["verify_failed"] += 1
            if issues or surplus_issues:
                if issues:
                    cell["anomalies"] += 1
                anomalies.append({
                    "scope": scope, "collection": collection, "version": version, "id": record["id"],
                    "kinds": sorted({kind for kind, present in (
                        ("schema", record.get("schema_issues")), ("assets", record.get("inventory_issues")),
                        ("bytes", record.get("verify_issues")), ("surplus", surplus_issues),
                    ) if present}),
                    "issues": (issues + surplus_issues)[:6],
                    "surplus_bytes": int(record.get("surplus_bytes") or 0),
                    "audited_at": record.get("audited_at"),
                })
            if collection == "sentinel_2_l2a":
                images.add(record["id"])
            elif collection in products:
                source = product_source(record["id"], collection)
                if source:
                    products[collection].setdefault(source[1], set()).add(source[0])

    coverage = []
    for collection in USE_CASE_COLLECTIONS:
        for tag, sources in sorted(products[collection].items()):
            covered = len(sources & images) if images else len(sources)
            coverage.append({
                "collection": collection, "version_tag": tag, "products": len(sources),
                "images": len(images), "images_with_product": covered,
                "images_without_product": max(len(images) - covered, 0) if images else None,
            })

    matrix = sorted(cells.values(), key=lambda cell: (cell["scope"] != PUBLIC_SCOPE, cell["scope"], cell["collection"], cell["version"]))
    for cell in matrix:
        counts = cell.pop("issue_counts")
        cell["top_issues"] = [{"issue": issue, "items": count} for issue, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:6]]
    anomalies.sort(key=lambda row: (row["scope"], row["collection"], row["id"]))
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "units": {name: {k: unit.get(k) for k in ("scope", "collection", "kind", "generated_at", "counts", "settings")} for name, unit in units.items()},
        "matrix": matrix,
        "coverage": coverage,
        "anomalies": anomalies,
        "totals": {
            "items": sum(cell["items"] for cell in matrix),
            "valid": sum(cell["valid"] for cell in matrix),
            "verified": sum(cell["verified"] for cell in matrix),
            "verify_waiting": sum(cell["verify_waiting"] for cell in matrix),
            "anomalies": sum(cell["anomalies"] for cell in matrix),
            "surplus_items": sum(cell["surplus_items"] for cell in matrix),
            "surplus_bytes": sum(cell["surplus_bytes"] for cell in matrix),
        },
    }


def append_history(history: list[dict[str, Any]], summary: dict[str, Any], limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    entry = {
        "generated_at": summary["generated_at"],
        "totals": summary["totals"],
        "cells": [{k: cell.get(k) for k in ("scope", "collection", "version", "items", "valid", "verified", "anomalies", "surplus_items", "surplus_bytes")} for cell in summary["matrix"]],
    }
    day = summary["generated_at"][:10]
    kept = [row for row in history if str(row.get("generated_at", ""))[:10] != day]
    kept.append(entry)
    return kept[-limit:]


# --------------------------------------------------------------------------------------------
# network access


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the platform store
        cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
        return ssl.create_default_context(cafile=cafile)


class Http:
    def __init__(self) -> None:
        self.context = ssl_context()

    def json(self, url: str, payload: dict[str, Any] | None = None, timeout: int = 120) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:  # noqa: S310 fixed origins
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, json.JSONDecodeError) as exc:
                if attempt == 3 or (isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429):
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def adler32(self, url: str, timeout: int = 600) -> tuple[str, int]:
        """Stream a file and return its adler32 (Rucio's checksum) and size.

        A connection closed early makes ``read(amt)`` return an empty chunk without raising,
        so the byte count is checked against the announced length and a short body is retried.
        """
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last_error: Exception | None = None
        for attempt in range(3):
            checksum, size = 1, 0
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:  # noqa: S310 fixed origins
                    announced = response.headers.get("Content-Length")
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        checksum = zlib.adler32(chunk, checksum)
                        size += len(chunk)
                if announced is not None and int(announced) != size:
                    raise http.client.IncompleteRead(b"", int(announced) - size)
                return f"{checksum & 0xFFFFFFFF:08x}", size
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    raise
                time.sleep(2 ** attempt)
        raise last_error if last_error else AssertionError("unreachable")


def discovery_documents(http: Http, api: str, collection: str, limit: int | None, log) -> list[dict[str, Any]]:
    """Every document of a public collection, through the paged discovery search."""
    filter_node = {"type": "comparison", "comparator": "equals", "path": ["collection"], "value": collection, "valueType": "string"}
    documents: list[dict[str, Any]] = []
    cursor = None
    while True:
        url = f"{api}/search?limit={SEARCH_PAGE_SIZE}" + (f"&cursor={urllib.request.quote(cursor, safe='')}" if cursor else "")
        page = http.json(url, {"filter": filter_node})
        documents.extend(page.get("items") or [])
        cursor = page.get("nextCursor")
        if len(documents) % (SEARCH_PAGE_SIZE * 20) == 0:
            log(f"  {len(documents)} documents so far")
        if not cursor or (limit and len(documents) >= limit):
            break
    return documents[:limit] if limit else documents


def auxiliary_documents(scope: str, limit: int | None) -> list[dict[str, Any]]:
    """Every Feature of an auxiliary scope, through the client's paged filter endpoint."""
    import dafab_client as dc

    node = {"type": "comparison", "comparator": "equals", "path": ["type"], "value": "Feature", "valueType": "string"}
    documents = dc.get_items_by_enhanced_filter(node, scope=scope, return_mode="metadata")
    documents = [doc for doc in documents if isinstance(doc, dict) and isinstance(doc.get("id"), str)]
    return documents[:limit] if limit else documents


def schema_validation(kind: str, document: dict[str, Any], collection: str | None, workdir: Path) -> list[str]:
    """Validate against the shipped schemas, offline. Auxiliary scopes have no schema, only structure."""
    issues = structural_issues(document)
    if kind == "auxiliary":
        return issues
    import dafab_client as dc

    path = workdir / f"{hashlib.sha1(document['id'].encode()).hexdigest()}.json"
    path.write_text(json.dumps(stored_document(document)), encoding="utf-8")
    try:
        if kind == "derived":
            report = dc.validate_derived_item(source="local", item_path=str(path), expected_item_id=document["id"],
                                              expected_collection_id=collection, check_catalog_state=False)
        else:
            report = dc.validate_original_item(source="local", item_path=str(path), expected_item_id=document["id"],
                                               expected_collection_id=collection, check_catalog_state=False, check_related_links=False)
        if not report.get("valid"):
            issues.extend(str(error) for error in (report.get("errors") or ["schema validation failed"])[:5])
    except Exception as exc:  # noqa: BLE001 - one bad document must not stop the unit
        issues.append(f"validator error: {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        path.unlink(missing_ok=True)
    return issues


def inventory(client, scope: str, record: dict[str, Any]) -> None:
    """Attached files and replica states from Rucio, compared with the declared assets."""
    item_id = record["id"]
    issues: list[str] = []
    try:
        files = list(client.list_content(scope, f"{item_id}_assets"))
    except Exception as exc:  # noqa: BLE001
        record.update({"inventory_at": utc_now(), "attached": 0, "available": 0, "surplus": 0, "surplus_bytes": 0, "surplus_issues": [],
                       "inventory_issues": [f"assets dataset unreadable: {type(exc).__name__}"]})
        return
    names = [row["name"] for row in files if row.get("type") == "FILE"]
    by_name = {row["name"]: row for row in files if row.get("type") == "FILE"}
    resolved: dict[str, str] = {}
    for key in record.get("assets") or []:
        file_name = attached_file_for(item_id, key, names)
        if file_name is None:
            issues.append(f"asset not attached: {key}")
        else:
            resolved[key] = file_name
    # Files attached beyond the declared assets are not a completeness problem, the contract
    # holds, but they are storage nobody can reach through the catalogue, so they are reported apart.
    surplus_names = sorted(set(names) - set(resolved.values()))
    external_keys = record.get("external_keys") or []
    surplus_issues: list[str] = []
    for name in surplus_names[:5]:
        as_external = next((key for key in external_keys if attached_file_for(item_id, key, [name]) == name), None)
        if as_external:
            surplus_issues.append(f"attached but declared with an external href: {as_external}")
        else:
            surplus_issues.append(f"attached but not declared: {name[len(item_id) + 1:]}")
    if len(surplus_names) > 5:
        surplus_issues.append(f"and {len(surplus_names) - 5} more surplus files")
    available = 0
    if names:
        try:
            replicas = list(client.list_replicas([{"scope": scope, "name": name} for name in names]))
            state_by_name = {row["name"]: row.get("states") or {} for row in replicas}
            for name in names:
                states = state_by_name.get(name)
                if states and any(state == "AVAILABLE" for state in states.values()):
                    available += 1
                else:
                    issues.append(f"no available replica: {name[len(item_id) + 1:]}")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"replica lookup failed: {type(exc).__name__}")
    record.update({
        "inventory_at": utc_now(), "attached": len(names), "available": available,
        "inventory_issues": issues[:8],
        "surplus": len(surplus_names), "surplus_bytes": sum(int(by_name[name].get("bytes") or 0) for name in surplus_names),
        "surplus_issues": surplus_issues,
        "_files": {key: {"name": name, "bytes": by_name[name].get("bytes"), "adler32": by_name[name].get("adler32")} for key, name in resolved.items()},
    })


def verify_bytes(http: Http, stac_root: str, scope: str, record: dict[str, Any]) -> None:
    """Download every declared asset and compare size and adler32 with what Rucio recorded."""
    files = record.get("_files") or {}
    issues: list[str] = []
    total = 0
    for key, meta in files.items():
        url = f"{stac_root}/assets/{scope}/items/{urllib.request.quote(record['id'], safe='')}/{urllib.request.quote(key, safe='')}"
        try:
            checksum, size = http.adler32(url)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"download failed: {key} ({type(exc).__name__})")
            continue
        total += size
        if meta.get("bytes") not in (None, size):
            issues.append(f"size differs: {key} {size} vs {meta['bytes']}")
        if meta.get("adler32") and meta["adler32"].lower() != checksum:
            issues.append(f"checksum differs: {key}")
    record.update({"verified_at": utc_now(), "verify_ok": not issues, "verified_bytes": total, "verify_issues": issues[:8]})


# --------------------------------------------------------------------------------------------
# commands


def load_json(path: Path | None) -> Any:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def fetch_site_json(http: Http, site_url: str, name: str, log) -> Any:
    try:
        return http.json(f"{site_url.rstrip('/')}/data/{name}", timeout=60)
    except Exception as exc:  # noqa: BLE001 - the first night has no previous site
        log(f"no previous {name} on the site ({type(exc).__name__})")
        return None


def collect(args: argparse.Namespace, log=print) -> Path:
    unit = UNITS[args.unit]
    scope, collection, kind = unit["scope"], unit["collection"], unit["kind"]
    http = Http()
    started = time.monotonic()
    day_index = dt.date.today().toordinal()
    previous = load_json(args.previous) if args.previous else fetch_site_json(http, args.site_url, f"units/{args.unit}.json", log)
    previous_items = {row["id"]: row for row in (previous or {}).get("items", [])}
    log(f"[{args.unit}] previous state: {len(previous_items)} items")

    if kind == "auxiliary":
        documents = auxiliary_documents(scope, args.limit)
    else:
        documents = discovery_documents(http, args.discovery_api, collection, args.limit, log)
    log(f"[{args.unit}] {len(documents)} documents in {time.monotonic() - started:.0f}s")

    audited_at = utc_now()
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dafab-audit-") as tmp:
        workdir = Path(tmp)
        for document in documents:
            managed, external = managed_asset_keys(document, args.stac_root)
            record = {
                "id": document["id"], "hash": document_hash(document), "assets_signature": assets_signature(document, managed),
                "version": processing_version(document),
                "datetime": (document.get("properties") or {}).get("datetime"), "assets": managed,
                "external_assets": len(external), "external_keys": external, "audited_at": audited_at,
            }
            record["schema_issues"] = schema_validation(kind, document, collection, workdir)
            record["valid"] = not record["schema_issues"]
            records.append(merge_previous(record, previous_items.get(record["id"])))
    changed = [r for r in records if r["changed"] or r.get("first_seen") == audited_at]
    log(f"[{args.unit}] validated in {time.monotonic() - started:.0f}s; invalid: {sum(1 for r in records if not r['valid'])}; changed or new: {len(changed)}")

    # Items whose managed assets changed, and newly published items, wait for a byte check until
    # one covers them, across nights, so a change that misses one night's budget is checked later.
    baseline = unit_baseline(previous or {}) or audited_at
    waiting = sorted((r for r in records if verification_due(r, baseline)), key=lambda r: (verification_due(r, baseline), r["id"]))
    waiting_head = waiting[: args.verify_max_items] if args.verify == "changed" else []
    log(f"[{args.unit}] waiting for a byte check: {len(waiting)}")

    rng = random.Random(f"{args.unit}:{dt.date.today().isoformat()}")
    sample = rng.sample(records, min(args.sample, len(records))) if records else []
    if args.inventory == "all":
        to_inventory = records
    else:
        rotating = [r for r in records if rotation_selected(r["id"], args.inventory_days, day_index)]
        outdated = [r for r in records if r.get("inventory_at") and "surplus" not in r]  # state written by an older collector
        to_inventory = {r["id"]: r for r in [*changed, *waiting_head, *sample, *rotating, *outdated]}.values()
    to_inventory = list(to_inventory)

    from dafab_client._rucio.dafab_lib import connection_manager

    client = connection_manager()
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        list(pool.map(lambda record: inventory(client, scope, record), to_inventory))
    for record in to_inventory:
        note_file_changes(record, audited_at)
    waiting = sorted((r for r in records if verification_due(r, baseline)), key=lambda r: (verification_due(r, baseline), r["id"]))
    log(f"[{args.unit}] inventoried {len(to_inventory)} items in {time.monotonic() - started:.0f}s")

    if args.verify == "all":
        to_verify = [r for r in records if r.get("_files")]
    elif args.verify == "none":
        to_verify = []
    else:
        to_verify = select_for_verification(waiting, sample, args.verify_max_items, args.verify_max_bytes)
    with ThreadPoolExecutor(max_workers=max(1, args.threads // 2)) as pool:
        list(pool.map(lambda record: verify_bytes(http, args.stac_root, scope, record), to_verify))
    still_waiting = sum(1 for r in records if verification_due(r, baseline))
    log(f"[{args.unit}] byte-verified {len(to_verify)} items ({sum(r.get('verified_bytes', 0) for r in to_verify) // 2**20} MiB) in {time.monotonic() - started:.0f}s; failures: {sum(1 for r in to_verify if r.get('verify_ok') is False)}; still waiting: {still_waiting}")

    for record in records:
        record.pop("_files", None)
    state = {
        "schema_version": STATE_SCHEMA_VERSION, "unit": args.unit, "scope": scope, "collection": collection, "kind": kind,
        "generated_at": audited_at, "baseline": baseline,
        "settings": {"inventory": args.inventory, "inventory_days": args.inventory_days, "verify": args.verify, "sample": args.sample, "limit": args.limit},
        "counts": {
            "items": len(records), "valid": sum(1 for r in records if r["valid"]), "changed": len(changed),
            "inventoried": len(to_inventory), "verified": len(to_verify), "verify_waiting": still_waiting,
            "seconds": round(time.monotonic() - started),
        },
        "items": records,
    }
    out = Path(args.output) / f"{args.unit}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    log(f"[{args.unit}] wrote {out} ({out.stat().st_size // 1024} KB)")
    return out


def publish(args: argparse.Namespace, log=print) -> Path:
    http = Http()
    units: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(args.units).rglob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("unit") in UNITS:
            units[state["unit"]] = state
    if not units:
        raise SystemExit(f"no unit files under {args.units}")
    generated_at = utc_now()
    summary = summarise(units, generated_at)
    summary["links"] = {"stac_root": args.stac_root, "discovery": args.discovery_site, "repository": args.repository_url, "run": args.run_url}
    history = load_json(args.history) if args.history else fetch_site_json(http, args.site_url, "history.json", log)
    history = append_history(history if isinstance(history, list) else [], summary)

    site = Path(args.output)
    if site.exists():
        shutil.rmtree(site)
    shutil.copytree(Path(args.template), site)
    data = site / "data"
    (data / "units").mkdir(parents=True, exist_ok=True)
    (data / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (data / "history.json").write_text(json.dumps(history, separators=(",", ":")), encoding="utf-8")
    for name, state in units.items():
        (data / "units" / f"{name}.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    (site / ".nojekyll").touch()
    log(f"site written to {site}: {summary['totals']['items']} items, {summary['totals']['anomalies']} anomalies, {len(history)} nights of history")
    return site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dafab-audit-nightly", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="audit one unit and write its state file")
    c.add_argument("--unit", required=True, choices=sorted(UNITS))
    c.add_argument("--output", default="units", help="directory for <unit>.json")
    c.add_argument("--previous", type=Path, help="previous state file; default fetches it from the live site")
    c.add_argument("--site-url", default=os.environ.get("AUDIT_SITE_URL", SITE_URL_DEFAULT))
    c.add_argument("--discovery-api", default=DISCOVERY_API_DEFAULT)
    c.add_argument("--stac-root", default=STAC_ROOT_DEFAULT)
    c.add_argument("--inventory", choices=("rotate", "all"), default="rotate", help="attached files and replica states for a rotating share plus changed and sampled items, or for every item")
    c.add_argument("--inventory-days", type=int, default=7, help="days for one full rotation")
    c.add_argument("--verify", choices=("changed", "all", "none"), default="changed", help="download and checksum changed and sampled items, every item, or nothing")
    c.add_argument("--verify-max-items", type=int, default=40)
    c.add_argument("--verify-max-bytes", type=int, default=6 * 1024**3, help="byte budget for the nightly downloads of a unit")
    c.add_argument("--sample", type=int, default=20, help="random items added to inventory and verification each night")
    c.add_argument("--threads", type=int, default=4)
    c.add_argument("--limit", type=int, help="audit only the first N documents (testing)")

    p = sub.add_parser("publish", help="merge unit files into the static site")
    p.add_argument("--units", default="units", help="directory holding the unit state files")
    p.add_argument("--output", default="_site")
    p.add_argument("--template", default="site")
    p.add_argument("--history", type=Path, help="previous history file; default fetches it from the live site")
    p.add_argument("--site-url", default=os.environ.get("AUDIT_SITE_URL", SITE_URL_DEFAULT))
    p.add_argument("--stac-root", default=STAC_ROOT_DEFAULT)
    p.add_argument("--discovery-site", default=DISCOVERY_SITE_DEFAULT)
    p.add_argument("--repository-url", default=os.environ.get("AUDIT_REPOSITORY_URL", ""))
    p.add_argument("--run-url", default=os.environ.get("AUDIT_RUN_URL", ""))

    args = parser.parse_args(argv)
    if args.command == "collect":
        collect(args)
    else:
        publish(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
