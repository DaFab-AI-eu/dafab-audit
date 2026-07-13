#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import hashlib
import html
import json
import multiprocessing
import os
import shutil
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dafab_client as dc
import psycopg
from PIL import Image
from requests.certs import where as public_ca_bundle

from dafab_audit.collage import render_snapshot, retry_transient_network, sha256_file


DEFAULT_SCOPE = "dafab"
DEFAULT_STAC_ROOT = "https://dafab.cern.ch/stac"
ORIGINAL_COLLECTION = "sentinel_2_l2a"
USE_CASE_COLLECTIONS = {
    "water-analysis": "water_analysis",
    "field-delineation": "smart_agriculture",
}
COLLECTION_USE_CASES = {value: key for key, value in USE_CASE_COLLECTIONS.items()}
REPORT_COLLECTION_DIRS = {
    "water_analysis": "water_analysis",
    "smart_agriculture": "field_delineation",
}
REMOTE_CURRENT_BUDGET_BYTES = 8 * 1024**3
LOCAL_REPORT_BUDGET_BYTES = 120 * 1024**3
MIN_FREE_BYTES = 50 * 1024**3
MAX_COLLAGE_BYTES = 2 * 1024**2
CHECKPOINT_CHANGES = 25
CHECKPOINT_SECONDS = 60
REPORT_STATE_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class ReportPaths:
    product_dir: Path
    metadata: Path
    collage: Path
    state: Path


@dataclass(frozen=True)
class PreparedReport:
    outcome: str
    metadata: dict[str, Any] | None = None
    collage: bytes | None = None
    state: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScanFailure:
    kind: str
    error_type: str
    message: str


_WORKER_ARGS: argparse.Namespace | None = None
_WORKER_SSL_CONTEXT: ssl.SSLContext | None = None
_WORKER_REVISION_CONNECTION: psycopg.Connection | None = None


@dataclass
class StorageBudget:
    root: Path
    report_bytes: int
    collage_bytes: int

    @classmethod
    def load(cls, root: Path) -> StorageBudget:
        files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
        return cls(
            root=root,
            report_bytes=sum(path.stat().st_size for path in files),
            collage_bytes=sum(path.stat().st_size for path in files if path.name == "collage-hd.png"),
        )

    def check(self, paths: ReportPaths, *, metadata_bytes: int, collage_bytes: int, state_bytes: int) -> None:
        old_metadata = paths.metadata.stat().st_size if paths.metadata.exists() else 0
        old_collage = paths.collage.stat().st_size if paths.collage.exists() else 0
        old_state = paths.state.stat().st_size if paths.state.exists() else 0
        projected_collages = self.collage_bytes - old_collage + collage_bytes
        projected_report = (
            self.report_bytes
            - old_metadata
            - old_collage
            - old_state
            + metadata_bytes
            + collage_bytes
            + state_bytes
        )
        if collage_bytes > MAX_COLLAGE_BYTES:
            raise RuntimeError(
                f"collage is {format_bytes(collage_bytes)}; hard limit is {format_bytes(MAX_COLLAGE_BYTES)}"
            )
        if projected_collages > REMOTE_CURRENT_BUDGET_BYTES:
            raise RuntimeError("projected collage snapshot exceeds the 8 GiB remote safety budget")
        if projected_report > LOCAL_REPORT_BUDGET_BYTES:
            raise RuntimeError("projected report exceeds the 120 GiB local safety budget")
        disk_root = self.root if self.root.exists() else self.root.parent
        additional = max(0, projected_report - self.report_bytes)
        if shutil.disk_usage(disk_root).free - additional < MIN_FREE_BYTES:
            raise RuntimeError("report update would leave less than 50 GiB free")


class NoGeneratedPublication(RuntimeError):
    pass


class NoCompletePublication(RuntimeError):
    pass


class ParallelScanError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the current DaFab generated-product report.")
    parser.add_argument("--product-list", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--use-case", required=True, choices=sorted(USE_CASE_COLLECTIONS) + ["both"])
    parser.add_argument("--db-env", type=Path, default=os.environ.get("DAFAB_AUDIT_DB_ENV") or None)
    parser.add_argument("--ca-cert", type=Path, default=os.environ.get("DAFAB_AUDIT_CA_CERT") or None)
    parser.add_argument(
        "--artifact-base-url",
        default=os.environ.get("DAFAB_AUDIT_ARTIFACT_BASE_URL") or None,
    )
    parser.add_argument("--profile", default=os.environ.get("DAFAB_PROFILE", "dafab_skim"))
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--stac-root", default=DEFAULT_STAC_ROOT)
    parser.add_argument("--tile-size", type=int, default=520)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--processing-evidence", type=Path)
    parser.add_argument("--reindex-only", action="store_true")
    args = parser.parse_args()
    if args.db_env is None and not args.reindex_only:
        parser.error("--db-env or DAFAB_AUDIT_DB_ENV is required")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def collections_for(use_case: str) -> list[str]:
    if use_case == "both":
        return list(REPORT_COLLECTION_DIRS)
    return [USE_CASE_COLLECTIONS[use_case]]


def load_product_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("product_id"), list):
        values = payload["product_id"]
    elif isinstance(payload, dict) and isinstance(payload.get("product_ids"), list):
        values = payload["product_ids"]
    elif isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        values = []
        for case in payload["cases"]:
            if isinstance(case, dict):
                values.extend(case.get("product-ids") or case.get("product_ids") or [])
    else:
        raise ValueError("unsupported product list JSON shape")
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def report_paths(root: Path, collection: str, product_id: str) -> ReportPaths:
    if collection not in REPORT_COLLECTION_DIRS:
        raise ValueError(f"unsupported collection: {collection}")
    if not product_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in product_id):
        raise ValueError(f"unsafe product ID: {product_id!r}")
    product_dir = root / REPORT_COLLECTION_DIRS[collection] / "products" / product_id
    return ReportPaths(
        product_dir=product_dir,
        metadata=product_dir / "metadata.json",
        collage=product_dir / "collage-hd.png",
        state=product_dir / "report-state.json",
    )


def load_processing_skips(path: Path | None) -> dict[tuple[str, str], str]:
    if path is None:
        return {}
    payload = json.loads(path.read_text())
    captured_at = payload.get("captured_at_utc") if isinstance(payload, dict) else None
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(captured_at, str) or not captured_at or not isinstance(products, list):
        raise ValueError("processing evidence must contain captured_at_utc and products")
    skips = {}
    for row in products:
        if not isinstance(row, dict):
            raise ValueError("processing evidence products must be objects")
        marker = row.get("skip_marker")
        if marker is None:
            continue
        product_id = row.get("product_id")
        worker_report = row.get("worker_report")
        publishable = row.get("publishable_flag")
        workflow = row.get("workflow")
        if (
            not isinstance(marker, dict)
            or not isinstance(product_id, str)
            or not isinstance(worker_report, dict)
            or type(worker_report.get("status")) is not int
            or worker_report["status"] != 0
            or row.get("phase") != "Succeeded"
            or not (publishable is False or publishable == "false")
            or not isinstance(workflow, str)
            or not workflow
            or marker.get("product_id") != product_id
            or marker.get("status") != "skipped"
            or marker.get("reason") != "no_valid_patches"
            or marker.get("use_case") not in USE_CASE_COLLECTIONS
        ):
            raise ValueError("processing skip evidence is inconsistent")
        collection = USE_CASE_COLLECTIONS[marker["use_case"]]
        report_paths(Path("."), collection, product_id)
        key = (collection, product_id)
        if key in skips:
            raise ValueError(f"duplicate processing skip evidence for {product_id}")
        skips[key] = marker["reason"]
    return skips


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def json_document(value: Any) -> bytes:
    return (json.dumps(json_safe(value), indent=2, ensure_ascii=False) + "\n").encode()


def verified_tls_context(ca_cert: Path | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=public_ca_bundle())
    if ca_cert is not None:
        ca_cert = ca_cert.expanduser()
        if not ca_cert.is_file():
            raise RuntimeError(f"CA bundle is not readable: {ca_cert}")
        context.load_verify_locations(cafile=str(ca_cert))
    return context


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


REVISION_INVENTORY_SQL = """
with requested as (
  select product_id, collection
  from unnest(%s::text[]) as products(product_id)
  cross join unnest(%s::text[]) as collections(collection)
), originals as (
  select
    requested_products.product_id,
    d.name is not null as exists,
    d.updated_at as did_updated_at,
    m.updated_at as meta_updated_at,
    coalesce(content.asset_count, 0) as asset_count,
    coalesce(content.data_bytes, 0) as data_bytes
  from (select distinct product_id from requested) requested_products
  left join dev.dids d
    on d.scope = %s and d.name = requested_products.product_id
  left join dev.did_meta m using (scope, name)
  left join lateral (
    select
      count(*) filter (where c.child_type = 'F') as asset_count,
      coalesce(sum(c.bytes) filter (where c.child_type = 'F'), 0) as data_bytes
    from dev.contents c
    where c.scope = d.scope and c.name = d.name
  ) content on true
), candidate_rows as (
  select
    regexp_replace(d.name, '_' || requested_collections.collection || '_[^_]+$', '') as product_id,
    requested_collections.collection,
    d.name,
    d.created_at as did_created_at,
    d.updated_at as did_updated_at,
    m.created_at as meta_created_at,
    m.updated_at as meta_updated_at
  from dev.dids d
  join dev.did_meta m using (scope, name)
  cross join (select distinct collection from requested) requested_collections
  where d.scope = %s
    and m.structured_meta ->> 'collection' = requested_collections.collection
    and d.name ~ ('_' || requested_collections.collection || '_[^_]+$')
), candidates as (
  select
    product_id,
    collection,
    jsonb_object_agg(name, jsonb_build_object(
      'did_created_at', did_created_at,
      'did_updated_at', did_updated_at,
      'meta_created_at', meta_created_at,
      'meta_updated_at', meta_updated_at
    )) as revisions
  from candidate_rows
  group by product_id, collection
)
select jsonb_build_object(
  'product_id', r.product_id,
  'collection', r.collection,
  'original', case when o.exists then jsonb_build_object(
    'did_updated_at', o.did_updated_at,
    'meta_updated_at', o.meta_updated_at,
    'asset_count', o.asset_count,
    'data_bytes', o.data_bytes
  ) else null end,
  'candidates', coalesce(c.revisions, '{}'::jsonb)
)
from requested r
left join originals o using (product_id)
left join candidates c using (product_id, collection)
order by r.collection, r.product_id
"""


def connect_revision_database(db_env: Path) -> psycopg.Connection:
    env_values = read_env_file(db_env)
    return psycopg.connect(
        host=env_values["DAFAB_DB_TUNNEL_HOST"],
        port=int(env_values["DAFAB_DB_TUNNEL_PORT"]),
        user=env_values["DAFAB_DB_USER_RUCIO"],
        password=env_values["DAFAB_DB_PASSWORD_RUCIO"],
        dbname=env_values["DAFAB_DB_NAME"],
        sslmode="require",
        application_name="dafab-audit",
        options="-c default_transaction_read_only=on",
        autocommit=True,
    )


def query_revision_times_bulk(
    connection: psycopg.Connection,
    *,
    scope: str,
    product_ids: list[str],
    collections: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    requested = list(dict.fromkeys((product_id, collection) for collection in collections for product_id in product_ids))
    if not requested:
        return {}
    revisions = {}
    with connection.cursor() as cursor:
        cursor.execute(
            REVISION_INVENTORY_SQL,
            (product_ids, collections, scope, scope),
        )
        rows = cursor.fetchall()
    for result in rows:
        row = result[0]
        if isinstance(row, str):
            row = json.loads(row)
        key = (row.pop("product_id"), row.pop("collection"))
        revisions[key] = row
    missing = set(requested) - set(revisions)
    if missing:
        raise RuntimeError(f"revision inventory omitted {len(missing)} requested product/use-case pairs")
    return revisions


def close_scan_worker() -> None:
    global _WORKER_REVISION_CONNECTION
    if _WORKER_REVISION_CONNECTION is not None:
        _WORKER_REVISION_CONNECTION.close()
        _WORKER_REVISION_CONNECTION = None


def initialize_scan_worker(args: argparse.Namespace) -> None:
    global _WORKER_ARGS, _WORKER_SSL_CONTEXT, _WORKER_REVISION_CONNECTION
    if args.profile_dir:
        os.environ["DAFAB_PROFILE_DIR"] = str(args.profile_dir.expanduser())
    dc.set_active_account(args.profile)
    _WORKER_ARGS = args
    _WORKER_SSL_CONTEXT = verified_tls_context(args.ca_cert)
    _WORKER_REVISION_CONNECTION = connect_revision_database(args.db_env)
    atexit.register(close_scan_worker)


def discover_candidate_ids(scope: str, product_id: str, collection: str) -> list[str]:
    rows = dc.get_items(
        scope=scope,
        filters={"name": f"{product_id}_{collection}_*"},
        long=True,
        recursive=False,
    )
    return sorted(
        {
            str(row.get("name"))
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
    )


def item_version(item_id: str) -> int:
    suffix = item_id.rsplit("_", 1)[-1]
    return int(suffix) if suffix.isdigit() else -1


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, str, int, str]:
    return (
        int(not candidate.get("revision_known")),
        str(candidate.get("meta_updated_at") or ""),
        item_version(candidate["item_id"]),
        candidate["item_id"],
    )


def official_asset_state(item_id: str, metadata: dict[str, Any], scope: str, stac_root: str) -> dict[str, Any]:
    state = dc.list_item_asset_entries(item_id, stac_namespace=scope, check_storage=True)
    rows = {
        row.get("asset_key"): row
        for row in state.get("asset_entries", [])
        if isinstance(row, dict) and isinstance(row.get("asset_key"), str)
    }
    issues = []
    for asset_key in (metadata.get("assets") or {}):
        row = rows.get(asset_key)
        expected_href = dc.build_stable_asset_href(
            item_id,
            asset_key,
            scope=scope,
            stac_root_href=stac_root,
        )
        if not isinstance(row, dict):
            issues.append(f"missing official asset entry: {asset_key}")
        elif row.get("href") != expected_href:
            issues.append(f"noncanonical asset href: {asset_key}")
        elif not row.get("available_on_server"):
            issues.append(f"asset is not attached on the server: {asset_key}")
        elif not row.get("available_on_storage"):
            issues.append(f"asset is unavailable on storage: {asset_key}")
    return {"valid": not issues, "issues": issues, "details": json_safe(state)}


def select_latest_complete(
    *,
    scope: str,
    stac_root: str,
    product_id: str,
    collection: str,
    revisions: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_ids = discover_candidate_ids(scope, product_id, collection)
    if not candidate_ids:
        raise NoGeneratedPublication(f"no generated {collection} item")
    candidates = []
    revision_by_name = revisions.get("candidates") or {}
    for item_id in candidate_ids:
        metadata = dc.get_bulk_metadata(item_id, scope=scope, max_retries=1, retry_delay=0)
        if not isinstance(metadata, dict) or metadata.get("collection") != collection:
            continue
        revision = revision_by_name.get(item_id)
        candidates.append(
            {
                "item_id": item_id,
                "metadata": metadata,
                "revision_known": revision is not None,
                **(revision or {}),
            }
        )
    if not candidates:
        raise NoGeneratedPublication(f"no generated {collection} metadata")

    rejected = []
    for candidate in sorted(candidates, key=candidate_sort_key, reverse=True):
        item_id = candidate["item_id"]
        try:
            placements = dc.get_item_facet_placements(item_id, collection_id=collection, scope=scope)
            validation = dc.validate_derived_item(
                source="server",
                scope=scope,
                expected_item_id=item_id,
                expected_collection_id=collection,
                expected_facet_placements=placements,
                original_collection_id=ORIGINAL_COLLECTION,
                root_href=stac_root,
                autofix=False,
            )
            assets = official_asset_state(item_id, candidate["metadata"], scope, stac_root)
        except Exception as exc:
            rejected.append({"item_id": item_id, "issues": [f"{type(exc).__name__}: {exc}"]})
            continue
        issues = list(validation.get("errors") or []) + list(assets["issues"])
        if validation.get("valid") and assets["valid"]:
            return {
                **candidate,
                "facet_placements": placements,
                "official_validation": json_safe(validation),
                "official_assets": assets["details"],
            }, rejected
        rejected.append({"item_id": item_id, "issues": issues})
    raise NoCompletePublication(json.dumps(rejected, sort_keys=True))


def rgb_asset_keys(original_metadata: dict[str, Any]) -> list[str]:
    assets = original_metadata.get("assets") or {}
    return ["TCI_20m"] if isinstance(assets.get("TCI_20m"), dict) else ["B04_10m", "B03_10m", "B02_10m"]


def probe_asset_revision(
    name: str,
    href: str,
    ssl_context: ssl.SSLContext,
) -> dict[str, Any]:
    request = urllib.request.Request(
        href,
        headers={"Range": "bytes=0-0", "User-Agent": "dafab-audit/0.1"},
    )

    def probe() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=60, context=ssl_context) as response:
            response.read(1)
            content_range = response.headers.get("Content-Range") or ""
            total = content_range.rsplit("/", 1)[-1] if "/" in content_range else None
            content_length = response.headers.get("Content-Length")
            size = (
                int(total)
                if total and total.isdigit()
                else int(content_length)
                if content_length and content_length.isdigit()
                else None
            )
            revision = {
                "name": name,
                "href": href,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "bytes": size,
            }
        revision["reusable"] = bool(revision["etag"] and revision["last_modified"] and size is not None)
        return revision

    return retry_transient_network(probe)


def asset_revisions(
    item: dict[str, Any],
    original_metadata: dict[str, Any],
    ssl_context: ssl.SSLContext,
) -> list[dict[str, Any]]:
    requests = []
    for asset_key, asset in (item["metadata"].get("assets") or {}).items():
        if isinstance(asset, dict) and isinstance(asset.get("href"), str):
            requests.append((f"generated:{asset_key}", asset["href"]))
    for asset_key in rgb_asset_keys(original_metadata):
        asset = (original_metadata.get("assets") or {}).get(asset_key)
        if isinstance(asset, dict) and isinstance(asset.get("href"), str):
            requests.append((f"original:{asset_key}", asset["href"]))
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(requests)))) as pool:
        return sorted(
            pool.map(lambda entry: probe_asset_revision(*entry, ssl_context), requests),
            key=lambda row: row["name"],
        )


def build_publication_snapshot(
    args: argparse.Namespace,
    collection: str,
    product_id: str,
    ssl_context: ssl.SSLContext,
    revisions: dict[str, Any],
) -> dict[str, Any]:
    item, rejected = select_latest_complete(
        scope=args.scope,
        stac_root=args.stac_root,
        product_id=product_id,
        collection=collection,
        revisions=revisions,
    )
    original_metadata = dc.get_bulk_metadata(product_id, scope=args.scope, max_retries=1, retry_delay=0)
    if not isinstance(original_metadata, dict):
        raise RuntimeError("official client did not return original metadata")
    original_inventory = revisions.get("original") or {}
    http_revisions = asset_revisions(item, original_metadata, ssl_context)
    revision_payload = {
        "item_id": item["item_id"],
        "did_updated_at": item.get("did_updated_at"),
        "meta_updated_at": item.get("meta_updated_at"),
        "metadata_sha256": canonical_sha256(item["metadata"]),
        "original_meta_updated_at": original_inventory.get("meta_updated_at"),
        "original_metadata_sha256": canonical_sha256(original_metadata),
        "original_asset_count": original_inventory.get("asset_count"),
        "original_data_bytes": original_inventory.get("data_bytes"),
        "facet_placements": item["facet_placements"],
        "official_assets": item["official_assets"],
        "http_assets": http_revisions,
        "rejected_newer_candidates": rejected,
    }
    return {
        "product_id": product_id,
        "use_case": COLLECTION_USE_CASES[collection],
        "collection": collection,
        "scope": args.scope,
        "item": item,
        "original_metadata": original_metadata,
        "revision_payload": revision_payload,
        "catalog_revision": canonical_sha256(revisions),
        "source_revision": canonical_sha256(revision_payload),
        "reusable": all(row.get("reusable") for row in http_revisions),
    }


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def complete_report_state(paths: ReportPaths) -> dict[str, Any] | None:
    state = load_state(paths.state)
    if (
        not state
        or state.get("schema_version") != REPORT_STATE_SCHEMA_VERSION
        or not state.get("publication_valid")
        or not state.get("report_ready")
    ):
        return None
    if not paths.metadata.is_file() or not paths.collage.is_file():
        return None
    try:
        metadata = json.loads(paths.metadata.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if canonical_sha256(metadata) != state.get("metadata_sha256"):
        return None
    collage = state.get("collage") or {}
    if paths.collage.stat().st_size != collage.get("bytes") or sha256_file(paths.collage) != collage.get("sha256"):
        return None
    return state


def facet_catalog_ids(placements: list[dict[str, Any]], parent_catalog_id: str) -> list[str]:
    return list(
        dict.fromkeys(
            placement["facet_value_catalog_id"]
            for placement in placements
            if placement.get("facet_parent_catalog_id") == parent_catalog_id
            and isinstance(placement.get("facet_value_catalog_id"), str)
        )
    )


def audit_state(snapshot: dict[str, Any], downloaded_assets: list[dict[str, Any]]) -> dict[str, Any]:
    original_metadata = snapshot["original_metadata"]
    properties = original_metadata.get("properties") or {}
    acquired_at = properties.get("datetime")
    tile_id = properties.get("grid:code")
    if not isinstance(acquired_at, str) or not acquired_at:
        raise RuntimeError("official original metadata has no acquisition datetime")
    if not isinstance(tile_id, str) or not tile_id:
        raise RuntimeError("official original metadata has no grid:code")

    item = snapshot["item"]
    placements = item["facet_placements"]
    validation = item["official_validation"]
    original_inventory = snapshot["revision_payload"]
    original_data_bytes = original_inventory.get("original_data_bytes")
    original_asset_count = original_inventory.get("original_asset_count")
    if not isinstance(original_data_bytes, int) or original_data_bytes < 0:
        raise RuntimeError("Rucio original-file inventory has no valid data byte total")
    if not isinstance(original_asset_count, int) or original_asset_count < 0:
        raise RuntimeError("Rucio original-file inventory has no valid asset count")

    errors = validation.get("errors") or []
    error_count = validation.get("error_count")
    if not isinstance(error_count, int):
        error_count = len(errors)
    return {
        "acquired_at": acquired_at,
        "tile_id": tile_id,
        "basin_catalog_ids": facet_catalog_ids(placements, "water_basin"),
        "anomaly_catalog_ids": facet_catalog_ids(placements, "water_anomaly"),
        "validation": {
            "valid": validation.get("valid") is True,
            "error_count": error_count,
        },
        "sizes": {
            "original": {
                "metadata_bytes": len(json_document(original_metadata)),
                "data_bytes": original_data_bytes,
                "asset_count": original_asset_count,
            },
            "generated": {
                "metadata_bytes": len(json_document(item["metadata"])),
                "data_bytes": sum(asset["bytes"] for asset in downloaded_assets),
                "asset_count": len(downloaded_assets),
            },
        },
    }


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def commit_report(
    paths: ReportPaths,
    *,
    metadata: dict[str, Any],
    collage: Path | bytes,
    state: dict[str, Any],
    budget: StorageBudget,
) -> None:
    metadata_payload = json_document(metadata)
    collage_payload = collage if isinstance(collage, bytes) else collage.read_bytes()
    state_payload = json_document(state)
    old_metadata_bytes = paths.metadata.stat().st_size if paths.metadata.exists() else 0
    old_collage_bytes = paths.collage.stat().st_size if paths.collage.exists() else 0
    old_state_bytes = paths.state.stat().st_size if paths.state.exists() else 0
    budget.check(
        paths,
        metadata_bytes=len(metadata_payload),
        collage_bytes=len(collage_payload),
        state_bytes=len(state_payload),
    )
    try:
        atomic_write(paths.metadata, metadata_payload)
        atomic_write(paths.collage, collage_payload)
        atomic_write(paths.state, state_payload)
    finally:
        new_metadata_bytes = paths.metadata.stat().st_size if paths.metadata.exists() else 0
        new_collage_bytes = paths.collage.stat().st_size if paths.collage.exists() else 0
        new_state_bytes = paths.state.stat().st_size if paths.state.exists() else 0
        budget.report_bytes += (
            new_metadata_bytes
            + new_collage_bytes
            + new_state_bytes
            - old_metadata_bytes
            - old_collage_bytes
            - old_state_bytes
        )
        budget.collage_bytes += new_collage_bytes - old_collage_bytes


def prepare_product(
    args: argparse.Namespace,
    collection: str,
    product_id: str,
    ssl_context: ssl.SSLContext,
    revision_connection: psycopg.Connection,
    revisions: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
) -> PreparedReport:
    paths = report_paths(args.report_root, collection, product_id)
    active_revisions = revisions
    for attempt in range(2):
        snapshot = build_publication_snapshot(args, collection, product_id, ssl_context, active_revisions)
        previous = previous_state if previous_state is not None else complete_report_state(paths)
        if (
            not args.force
            and previous
            and previous.get("catalog_revision") == snapshot["catalog_revision"]
            and previous.get("source_revision") == snapshot["source_revision"]
            and snapshot["reusable"]
        ):
            return PreparedReport("unchanged")

        with tempfile.TemporaryDirectory(prefix=f"dafab-report-{product_id}-") as temporary_root:
            render_dir = Path(temporary_root) / "render"
            report = render_snapshot(snapshot, render_dir, args.tile_size, ssl_context)
            item_report = report["items"][0]
            render_issues = [
                f"{asset['asset_key']}: {asset['error']}"
                for asset in item_report.get("assets", [])
                if asset.get("error")
            ]
            if not report.get("original_rgb", {}).get("available"):
                render_issues.append(report.get("original_rgb", {}).get("error") or "original RGB is unavailable")
            collage_relative = item_report.get("collage")
            if not collage_relative:
                render_issues.append("collage was not generated")
            if render_issues:
                raise RuntimeError("; ".join(render_issues))
            collage = render_dir / collage_relative

            refreshed_revisions = query_revision_times_bulk(
                revision_connection,
                scope=args.scope,
                product_ids=[product_id],
                collections=[collection],
            )[(product_id, collection)]
            current = build_publication_snapshot(
                args,
                collection,
                product_id,
                ssl_context,
                refreshed_revisions,
            )
            if (
                current["catalog_revision"] != snapshot["catalog_revision"]
                or current["source_revision"] != snapshot["source_revision"]
            ):
                if attempt == 0:
                    active_revisions = refreshed_revisions
                    continue
                raise RuntimeError("Rucio publication changed while the report was being rendered")

            with Image.open(collage) as image:
                width, height = image.size
            downloaded_assets = [
                {
                    key: asset.get(key)
                    for key in ("asset_key", "href", "sha256", "bytes")
                }
                for asset in item_report["assets"]
            ]
            collage_sha = sha256_file(collage)
            item = snapshot["item"]
            state = {
                "schema_version": REPORT_STATE_SCHEMA_VERSION,
                "product_id": product_id,
                "scope": args.scope,
                "use_case": COLLECTION_USE_CASES[collection],
                "collection": collection,
                "item_id": item["item_id"],
                "processing_version": (item["metadata"].get("properties") or {}).get("processing:version"),
                "metadata_updated_at": item.get("meta_updated_at"),
                "catalog_revision": snapshot["catalog_revision"],
                "source_revision": snapshot["source_revision"],
                "source_revision_inputs": snapshot["revision_payload"],
                "release_fingerprint": canonical_sha256(
                    {
                        "metadata": item["metadata"],
                        "facet_placements": item["facet_placements"],
                        "official_assets": item["official_assets"],
                        "downloaded_assets": downloaded_assets,
                        "rgb_assets": report["original_rgb"].get("assets", []),
                        "collage_sha256": collage_sha,
                    }
                ),
                "metadata_sha256": canonical_sha256(item["metadata"]),
                "facet_placements": item["facet_placements"],
                "official_validation": item["official_validation"],
                "publication_valid": True,
                "report_ready": True,
                "report_generation": time.time_ns(),
                "audit": audit_state(snapshot, downloaded_assets),
                "downloaded_assets": downloaded_assets,
                "rgb_source": {
                    key: value
                    for key, value in report["original_rgb"].items()
                    if key != "preview_path"
                },
                "collage": {
                    "path": "collage-hd.png",
                    "sha256": collage_sha,
                    "bytes": collage.stat().st_size,
                    "width": width,
                    "height": height,
                },
                "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            collage_payload = collage.read_bytes()
            if len(collage_payload) > MAX_COLLAGE_BYTES:
                raise RuntimeError(
                    f"collage is {format_bytes(len(collage_payload))}; "
                    f"hard limit is {format_bytes(MAX_COLLAGE_BYTES)}"
                )
            return PreparedReport(
                "updated",
                metadata=item["metadata"],
                collage=collage_payload,
                state=state,
            )
    raise AssertionError("unreachable")


def classify_scan_failure(exc: Exception) -> ScanFailure:
    if isinstance(exc, NoGeneratedPublication):
        kind = "no-generated-publication"
    elif isinstance(exc, NoCompletePublication):
        kind = "no-complete-publication"
    else:
        kind = "report-error"
    return ScanFailure(kind, type(exc).__name__, str(exc))


def prepare_product_worker(
    collection: str,
    product_id: str,
    revisions: dict[str, Any],
    previous_state: dict[str, Any] | None,
) -> PreparedReport | ScanFailure:
    if _WORKER_ARGS is None or _WORKER_SSL_CONTEXT is None or _WORKER_REVISION_CONNECTION is None:
        raise ParallelScanError("scan worker was not initialized")
    try:
        return prepare_product(
            _WORKER_ARGS,
            collection,
            product_id,
            _WORKER_SSL_CONTEXT,
            _WORKER_REVISION_CONNECTION,
            revisions,
            previous_state,
        )
    except Exception as exc:
        return classify_scan_failure(exc)


def scan_product(
    args: argparse.Namespace,
    collection: str,
    product_id: str,
    budget: StorageBudget,
    ssl_context: ssl.SSLContext,
    revision_connection: psycopg.Connection,
    revisions: dict[str, Any],
    previous_state: dict[str, Any] | None = None,
) -> str:
    prepared = prepare_product(
        args,
        collection,
        product_id,
        ssl_context,
        revision_connection,
        revisions,
        previous_state,
    )
    if prepared.outcome == "unchanged":
        return prepared.outcome
    if prepared.outcome != "updated" or prepared.metadata is None or prepared.collage is None or prepared.state is None:
        raise RuntimeError("scan preparation returned an incomplete report")
    commit_report(
        report_paths(args.report_root, collection, product_id),
        metadata=prepared.metadata,
        collage=prepared.collage,
        state=prepared.state,
        budget=budget,
    )
    return prepared.outcome


def report_states(root: Path) -> list[dict[str, Any]]:
    states = []
    for state_path in root.glob("*/products/*/report-state.json"):
        state = load_state(state_path)
        if state:
            states.append(state)
    return states


def index_row(
    paths: ReportPaths,
    collection: str,
    product_id: str,
    status: str,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state or {}
    audit = state.get("audit") or {}
    return {
        "use_case": COLLECTION_USE_CASES[collection],
        "product_id": product_id,
        "item_id": state.get("item_id"),
        "processing_version": state.get("processing_version"),
        "metadata_updated_at": state.get("metadata_updated_at"),
        "acquired_at": audit.get("acquired_at"),
        "tile_id": audit.get("tile_id"),
        "basin_catalog_ids": audit.get("basin_catalog_ids") or [],
        "anomaly_catalog_ids": audit.get("anomaly_catalog_ids") or [],
        "validation": audit.get("validation") or {},
        "sizes": audit.get("sizes") or {},
        "status": status,
        "has_complete_report": bool(state),
        "product_dir": paths.product_dir,
    }


def existing_report_states(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    states = {}
    for state_path in root.glob("*/products/*/report-state.json"):
        state = load_state(state_path)
        if not state:
            continue
        collection = state.get("collection")
        product_id = state.get("product_id")
        if collection not in REPORT_COLLECTION_DIRS or not isinstance(product_id, str):
            continue
        complete = complete_report_state(report_paths(root, collection, product_id))
        if complete:
            states[(collection, product_id)] = complete
    return states


def load_scan_failures(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = root / "scan-errors.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    failures = {}
    for row in payload:
        if not isinstance(row, dict) or row.get("status") not in {"publication-invalid", "report-error"}:
            continue
        collection = row.get("collection")
        product_id = row.get("product_id")
        if collection in REPORT_COLLECTION_DIRS and isinstance(product_id, str):
            try:
                report_paths(root, collection, product_id)
            except ValueError:
                continue
            failures[(collection, product_id)] = row
    return failures


def initial_index_state(
    root: Path,
    product_ids: list[str],
    collections: list[str],
    processing_skips: dict[tuple[str, str], str],
    revision_inventory: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
    list[tuple[str, str]],
]:
    requested_pairs = [(collection, product_id) for collection in collections for product_id in product_ids]
    requested_keys = set(requested_pairs)
    states = {key: state for key, state in existing_report_states(root).items() if key in requested_keys}
    rows = {
        key: index_row(report_paths(root, key[0], key[1]), key[0], key[1], "healthy", state=state)
        for key, state in states.items()
    }
    failures = {key: failure for key, failure in load_scan_failures(root).items() if key in requested_keys}
    for key, failure in list(failures.items()):
        state = states.get(key)
        generation = state.get("report_generation") if state else None
        if generation is not None and failure.get("report_generation") != generation:
            failures.pop(key)
            continue
        rows[key] = index_row(
            report_paths(root, key[0], key[1]),
            key[0],
            key[1],
            failure["status"],
            state=state,
        )

    for collection, product_id in requested_pairs:
        key = (collection, product_id)
        if key in rows:
            continue
        skipped = key in processing_skips
        if skipped and revision_inventory is not None:
            skipped = not revision_inventory[(product_id, collection)].get("candidates")
        rows[key] = index_row(
            report_paths(root, collection, product_id),
            collection,
            product_id,
            "skipped-no-publication" if skipped else "pending",
        )
    return states, failures, rows, requested_pairs


def commit_index_file(path: Path, payload: bytes, budget: StorageBudget | None) -> None:
    if budget is None:
        atomic_write(path, payload)
        return
    old_bytes = path.stat().st_size if path.exists() else 0
    projected_report = budget.report_bytes - old_bytes + len(payload)
    if projected_report > LOCAL_REPORT_BUDGET_BYTES:
        raise RuntimeError("projected report exceeds the 120 GiB local safety budget")
    additional = max(0, projected_report - budget.report_bytes)
    disk_root = budget.root if budget.root.exists() else budget.root.parent
    if shutil.disk_usage(disk_root).free - additional < MIN_FREE_BYTES:
        raise RuntimeError("report update would leave less than 50 GiB free")
    try:
        atomic_write(path, payload)
    finally:
        new_bytes = path.stat().st_size if path.exists() else 0
        budget.report_bytes += new_bytes - old_bytes


def write_scan_failures(
    root: Path,
    failures: dict[tuple[str, str], dict[str, Any]],
    budget: StorageBudget | None = None,
) -> None:
    ordered = [failures[key] for key in sorted(failures)]
    commit_index_file(root / "scan-errors.json", json_document(ordered), budget)


def artifact_href(base_url: str | None, relative_path: str) -> str:
    encoded_path = urllib.parse.quote(relative_path.lstrip("/"), safe="/")
    if base_url is None:
        return encoded_path
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("artifact base URL must be an absolute HTTP(S) URL")
    base_path = urllib.parse.quote(parsed.path.rstrip("/"), safe="/%:@")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{base_path}/{encoded_path}", parsed.query, parsed.fragment)
    )


def write_readme(
    root: Path,
    rows: list[dict[str, Any]],
    budget: StorageBudget | None = None,
    *,
    artifact_base_url: str | None = None,
) -> None:
    lines = [
        "# DaFab generated report",
        "",
        "Current latest-complete publications. Metadata is the current Rucio structured document.",
        "",
        "For interactive sorting and filtering, open the self-contained [sortable HTML report](index.html).",
        "",
        "Rows are seeded as pending and promoted in place after the complete per-product audit succeeds. "
        "Explicit successful no-publication evidence is shown as skipped-no-publication. Publication-invalid "
        "and report-error rows are retried on later runs.",
        "",
        "Acquisition, tile, basin, anomaly, processing version, and validation come from official metadata and validation state. "
        "Facet parent prefixes are omitted in the table.",
        "",
        "Size cells are `metadata / data`: metadata is deterministic UTF-8 JSON; original data is the bulk Rucio "
        "attached-file total; generated data is the verified downloaded-asset total. Collages are excluded.",
        "",
        "| Use case | Acquired | Tile | Product ID | Basin | Anomaly | Latest complete item | Version | Updated UTC | Status | Validation | Original meta/data | Generated meta/data | Metadata | HD collage |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda value: (value["use_case"], value["product_id"])):
        product_dir = Path(row["product_dir"]).relative_to(root).as_posix()
        acquired = (row.get("acquired_at") or "—").split("T", 1)[0]
        tile_id = f"`{row['tile_id']}`" if row.get("tile_id") else "—"
        basin = format_catalog_ids(row.get("basin_catalog_ids"), "water_basin")
        anomaly = format_catalog_ids(row.get("anomaly_catalog_ids"), "water_anomaly")
        item_id = f"`{row['item_id']}`" if row.get("item_id") else "—"
        version = row.get("processing_version") or "—"
        updated = row.get("metadata_updated_at") or "—"
        validation = format_validation(row.get("validation"))
        sizes = row.get("sizes") or {}
        original_sizes = format_size_pair(sizes.get("original"))
        generated_sizes = format_size_pair(sizes.get("generated"))
        if row["has_complete_report"]:
            metadata_link = f"[JSON]({artifact_href(artifact_base_url, f'{product_dir}/metadata.json')})"
            collage_link = f"[collage]({artifact_href(artifact_base_url, f'{product_dir}/collage-hd.png')})"
        else:
            metadata_link = collage_link = "—"
        lines.append(
            f"| {row['use_case']} | {acquired} | {tile_id} | `{row['product_id']}` | {basin} | {anomaly} | "
            f"{item_id} | {version} | {updated} | {row['status']} | {validation} | {original_sizes} | "
            f"{generated_sizes} | {metadata_link} | {collage_link} |"
        )
    commit_index_file(root / "README.md", ("\n".join(lines) + "\n").encode(), budget)


def html_catalog_ids(values: Any, parent_catalog_id: str) -> str:
    if not isinstance(values, list) or not values:
        return "—"
    prefix = f"{parent_catalog_id}_"
    return ", ".join(value.removeprefix(prefix) for value in values)


def write_sortable_report(
    root: Path,
    rows: list[dict[str, Any]],
    budget: StorageBudget | None = None,
    *,
    artifact_base_url: str | None = None,
) -> None:
    def escaped(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def cell(value: Any, *, sort_value: Any | None = None, code: bool = False) -> str:
        display = escaped(value)
        if code and value != "—":
            display = f"<code>{display}</code>"
        sort_attribute = "" if sort_value is None else f' data-sort-value="{escaped(sort_value)}"'
        return f"<td{sort_attribute}>{display}</td>"

    ordered_rows = sorted(rows, key=lambda value: (value["use_case"], value["product_id"]))
    status_counts: dict[str, int] = {}
    for row in ordered_rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = ", ".join(f"{status}: {count}" for status, count in sorted(status_counts.items()))
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>DaFab generated report</title>",
        "<style>",
        ":root { color-scheme: light dark; font-family: system-ui, sans-serif; }",
        "body { margin: 1.5rem; }",
        ".toolbar { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; margin-bottom: 1rem; }",
        "input { min-width: min(32rem, 90vw); padding: .55rem .7rem; }",
        ".table-wrap { overflow: auto; max-height: calc(100vh - 12rem); border: 1px solid #8886; }",
        "table { border-collapse: collapse; width: max-content; min-width: 100%; font-size: .85rem; }",
        "th, td { border-bottom: 1px solid #8884; padding: .45rem .6rem; text-align: left; white-space: nowrap; }",
        "th { position: sticky; top: 0; z-index: 1; background: Canvas; }",
        "th button { all: unset; cursor: pointer; font-weight: 700; }",
        "th button::after { content: ' ↕'; opacity: .45; }",
        "th[aria-sort='ascending'] button::after { content: ' ↑'; opacity: 1; }",
        "th[aria-sort='descending'] button::after { content: ' ↓'; opacity: 1; }",
        "tbody tr:nth-child(even) { background: #8881; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>DaFab generated report</h1>",
        f"<p>{escaped(summary)}. Rows are updated as complete per-product audits succeed.</p>",
        '<div class="toolbar">',
        '<label>Filter <input id="filter" type="search" placeholder="Product, tile, basin, anomaly, status…"></label>',
        f'<span id="visible-count">{len(ordered_rows)} of {len(ordered_rows)} rows</span>',
        "</div>",
        '<div class="table-wrap">',
        '<table id="report-table">',
        "<thead><tr>",
    ]
    headers = [
        ("Use case", "text"),
        ("Acquired", "text"),
        ("Tile", "text"),
        ("Product ID", "text"),
        ("Basin", "text"),
        ("Anomaly", "text"),
        ("Latest complete item", "text"),
        ("Version", "text"),
        ("Updated UTC", "text"),
        ("Status", "text"),
        ("Validation", "text"),
        ("Original meta/data", "number"),
        ("Generated meta/data", "number"),
        ("Metadata", "text"),
        ("HD collage", "text"),
    ]
    for index, (label, sort_type) in enumerate(headers):
        lines.append(
            f'<th scope="col" data-type="{sort_type}"><button type="button" data-column="{index}">'
            f"{escaped(label)}</button></th>"
        )
    lines.extend(["</tr></thead>", "<tbody>"])
    for row in ordered_rows:
        product_dir = Path(row["product_dir"]).relative_to(root).as_posix()
        acquired = (row.get("acquired_at") or "—").split("T", 1)[0]
        basin = html_catalog_ids(row.get("basin_catalog_ids"), "water_basin")
        anomaly = html_catalog_ids(row.get("anomaly_catalog_ids"), "water_anomaly")
        validation = format_validation(row.get("validation"))
        sizes = row.get("sizes") or {}
        original = sizes.get("original") or {}
        generated = sizes.get("generated") or {}
        lines.append("<tr>")
        lines.append(cell(row["use_case"]))
        lines.append(cell(acquired))
        lines.append(cell(row.get("tile_id") or "—", code=True))
        lines.append(cell(row["product_id"], code=True))
        lines.append(cell(basin))
        lines.append(cell(anomaly))
        lines.append(cell(row.get("item_id") or "—", code=True))
        lines.append(cell(row.get("processing_version") or "—"))
        lines.append(cell(row.get("metadata_updated_at") or "—"))
        lines.append(cell(row["status"]))
        lines.append(cell(validation))
        lines.append(cell(format_size_pair(original), sort_value=original.get("data_bytes", -1)))
        lines.append(cell(format_size_pair(generated), sort_value=generated.get("data_bytes", -1)))
        if row["has_complete_report"]:
            metadata_href = artifact_href(artifact_base_url, f"{product_dir}/metadata.json")
            collage_href = artifact_href(artifact_base_url, f"{product_dir}/collage-hd.png")
            lines.append(f'<td><a href="{escaped(metadata_href)}">JSON</a></td>')
            lines.append(f'<td><a href="{escaped(collage_href)}">collage</a></td>')
        else:
            lines.extend(["<td>—</td>", "<td>—</td>"])
        lines.append("</tr>")
    lines.extend(
        [
            "</tbody></table></div>",
            "<script>",
            "const table = document.getElementById('report-table');",
            "const body = table.tBodies[0];",
            "const filter = document.getElementById('filter');",
            "const visibleCount = document.getElementById('visible-count');",
            "const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});",
            "let activeColumn = -1;",
            "let direction = 1;",
            "function updateCount() {",
            "  const rows = [...body.rows];",
            "  visibleCount.textContent = `${rows.filter(row => !row.hidden).length} of ${rows.length} rows`;",
            "}",
            "filter.addEventListener('input', () => {",
            "  const query = filter.value.trim().toLocaleLowerCase();",
            "  for (const row of body.rows) row.hidden = query && !row.textContent.toLocaleLowerCase().includes(query);",
            "  updateCount();",
            "});",
            "for (const button of table.querySelectorAll('th button')) {",
            "  button.addEventListener('click', () => {",
            "    const column = Number(button.dataset.column);",
            "    direction = activeColumn === column ? -direction : 1;",
            "    activeColumn = column;",
            "    const header = button.closest('th');",
            "    const numeric = header.dataset.type === 'number';",
            "    for (const other of table.querySelectorAll('th')) other.removeAttribute('aria-sort');",
            "    header.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');",
            "    const rows = [...body.rows];",
            "    rows.sort((left, right) => {",
            "      const a = left.cells[column].dataset.sortValue ?? left.cells[column].textContent.trim();",
            "      const b = right.cells[column].dataset.sortValue ?? right.cells[column].textContent.trim();",
            "      const compared = numeric ? Number(a) - Number(b) : collator.compare(a, b);",
            "      return direction * compared;",
            "    });",
            "    for (const row of rows) body.append(row);",
            "  });",
            "}",
            "</script>",
            "</body></html>",
        ]
    )
    commit_index_file(root / "index.html", ("\n".join(lines) + "\n").encode(), budget)


def checkpoint_report(
    root: Path,
    rows: dict[tuple[str, str], dict[str, Any]],
    failures: dict[tuple[str, str], dict[str, Any]],
    budget: StorageBudget,
    *,
    artifact_base_url: str | None = None,
) -> None:
    write_scan_failures(root, failures, budget)
    write_readme(root, list(rows.values()), budget, artifact_base_url=artifact_base_url)
    write_sortable_report(root, list(rows.values()), budget, artifact_base_url=artifact_base_url)


def format_catalog_ids(values: Any, parent_catalog_id: str) -> str:
    if not isinstance(values, list) or not values:
        return "—"
    prefix = f"{parent_catalog_id}_"
    return ", ".join(f"`{value.removeprefix(prefix)}`" for value in values)


def format_validation(validation: Any) -> str:
    if not isinstance(validation, dict) or "valid" not in validation:
        return "—"
    if validation.get("valid") is True:
        return "pass"
    return f"fail ({validation.get('error_count', 0)})"


def format_size_pair(sizes: Any) -> str:
    if not isinstance(sizes, dict):
        return "—"
    metadata_bytes = sizes.get("metadata_bytes")
    data_bytes = sizes.get("data_bytes")
    if not isinstance(metadata_bytes, int) or not isinstance(data_bytes, int):
        return "—"
    return f"{format_bytes(metadata_bytes)} / {format_bytes(data_bytes)}"


def write_budget(root: Path, budget: StorageBudget, product_use_case_reports: int) -> None:
    path = root / "storage-budget.json"
    old_bytes = path.stat().st_size if path.exists() else 0
    base_bytes = budget.report_bytes - old_bytes
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report_bytes = base_bytes
    for _attempt in range(10):
        payload = json_document(
            {
                "schema_version": 1,
                "product_use_case_reports": product_use_case_reports,
                "report_bytes": report_bytes,
                "collage_bytes": budget.collage_bytes,
                "remote_current_budget_bytes": REMOTE_CURRENT_BUDGET_BYTES,
                "local_report_budget_bytes": LOCAL_REPORT_BUDGET_BYTES,
                "minimum_free_bytes": MIN_FREE_BYTES,
                "updated_at": updated_at,
            }
        )
        next_report_bytes = base_bytes + len(payload)
        if next_report_bytes == report_bytes:
            break
        report_bytes = next_report_bytes
    else:
        raise RuntimeError("storage budget size did not converge")
    commit_index_file(path, payload, budget)
    if budget.report_bytes != report_bytes:
        raise RuntimeError("storage budget does not match the written report")


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def parallel_scan_results(
    executor: ProcessPoolExecutor,
    work_items: list[tuple[int, str, str, dict[str, Any], dict[str, Any] | None]],
    max_pending: int,
):
    work_iterator = iter(work_items)
    pending = {}

    def fill_pending() -> None:
        while len(pending) < max_pending:
            try:
                index, collection, product_id, revisions, previous = next(work_iterator)
            except StopIteration:
                return
            future = executor.submit(
                prepare_product_worker,
                collection,
                product_id,
                revisions,
                previous,
            )
            pending[future] = (index, collection, product_id)

    fill_pending()
    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        completed = []
        for future in done:
            index, collection, product_id = pending.pop(future)
            try:
                result = future.result()
            except BrokenProcessPool:
                raise
            except Exception as exc:
                raise ParallelScanError(
                    f"worker failed before returning {collection}:{product_id}: {type(exc).__name__}: {exc}"
                ) from exc
            completed.append((index, collection, product_id, result))
        fill_pending()
        yield from completed


def reindex_report(
    args: argparse.Namespace,
    product_ids: list[str],
    collections: list[str],
    processing_skips: dict[tuple[str, str], str],
) -> int:
    budget = StorageBudget.load(args.report_root)
    states, failures, rows, requested_pairs = initial_index_state(
        args.report_root,
        product_ids,
        collections,
        processing_skips,
    )
    checkpoint_report(
        args.report_root,
        rows,
        failures,
        budget,
        artifact_base_url=args.artifact_base_url,
    )
    reconciled_budget = StorageBudget.load(args.report_root)
    budget.report_bytes = reconciled_budget.report_bytes
    budget.collage_bytes = reconciled_budget.collage_bytes
    write_budget(args.report_root, budget, len(states))
    print(args.report_root)
    requested_keys = set(requested_pairs)
    return int(any(key in requested_keys for key in failures))


def main() -> int:
    args = parse_args()
    workers = getattr(args, "workers", 1)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    args.report_root = args.report_root.expanduser().resolve()
    args.report_root.mkdir(parents=True, exist_ok=True)
    product_ids = load_product_ids(args.product_list)
    if args.limit is not None:
        product_ids = product_ids[: args.limit]
    collections = collections_for(args.use_case)
    processing_skips = load_processing_skips(getattr(args, "processing_evidence", None))
    if args.reindex_only:
        return reindex_report(args, product_ids, collections, processing_skips)
    if args.profile_dir:
        os.environ["DAFAB_PROFILE_DIR"] = str(args.profile_dir.expanduser())
    dc.set_active_account(args.profile)
    ssl_context = verified_tls_context(args.ca_cert)
    revision_connection = connect_revision_database(args.db_env)
    pool_failure = None
    try:
        revision_inventory = query_revision_times_bulk(
            revision_connection,
            scope=args.scope,
            product_ids=product_ids,
            collections=collections,
        )
        print(
            f"Opened one read-only database connection and loaded {len(revision_inventory)} "
            "product/use-case revisions in one bulk query.",
            flush=True,
        )
        if workers > 1:
            revision_connection.close()
            revision_connection = None
        budget = StorageBudget.load(args.report_root)
        states, failures, rows, requested_pairs = initial_index_state(
            args.report_root,
            product_ids,
            collections,
            processing_skips,
            revision_inventory,
        )
        checkpoint_report(
            args.report_root,
            rows,
            failures,
            budget,
            artifact_base_url=args.artifact_base_url,
        )

        work_pairs = sorted(requested_pairs, key=lambda key: rows[key]["status"] == "healthy")
        changed_since_checkpoint = 0
        last_checkpoint = time.monotonic()

        def process_pair(
            index: int,
            collection: str,
            product_id: str,
            result: PreparedReport | ScanFailure | None = None,
            *,
            parallel: bool = False,
        ) -> None:
            nonlocal changed_since_checkpoint, last_checkpoint
            key = (collection, product_id)
            paths = report_paths(args.report_root, collection, product_id)
            previous = states.get(key)
            old_row = rows[key]
            old_failure = failures.get(key)
            revisions = revision_inventory[(product_id, collection)]
            failure = None
            try:
                if previous is None and not revisions.get("candidates"):
                    outcome = "skipped-no-publication" if key in processing_skips else "pending"
                    rows[key] = index_row(paths, collection, product_id, outcome)
                    failures.pop(key, None)
                else:
                    if parallel:
                        if isinstance(result, ScanFailure):
                            failure = result
                        elif isinstance(result, PreparedReport):
                            outcome = result.outcome
                            if outcome == "updated":
                                if result.metadata is None or result.collage is None or result.state is None:
                                    raise ParallelScanError("worker returned an incomplete report")
                                commit_report(
                                    paths,
                                    metadata=result.metadata,
                                    collage=result.collage,
                                    state=result.state,
                                    budget=budget,
                                )
                            elif outcome != "unchanged":
                                raise ParallelScanError(f"worker returned an unknown outcome: {outcome!r}")
                        else:
                            raise ParallelScanError("worker returned an invalid result")
                    else:
                        if revision_connection is None:
                            raise ParallelScanError("serial revision database connection is closed")
                        outcome = scan_product(
                            args,
                            collection,
                            product_id,
                            budget,
                            ssl_context,
                            revision_connection,
                            revisions,
                            previous,
                        )
                    if failure is None:
                        state = previous if outcome == "unchanged" and previous else complete_report_state(paths)
                        if not state:
                            raise RuntimeError("scan completed without a complete report")
                        states[key] = state
                        rows[key] = index_row(paths, collection, product_id, "healthy", state=state)
                        failures.pop(key, None)
                if failure is None:
                    print(f"[{collection}] {index}/{len(work_pairs)} {outcome} {product_id}", flush=True)
            except ParallelScanError:
                raise
            except Exception as exc:
                failure = classify_scan_failure(exc)

            if failure is not None:
                surviving_state = complete_report_state(paths)
                if surviving_state:
                    states[key] = surviving_state
                else:
                    states.pop(key, None)
                if failure.kind == "no-generated-publication" and previous is None:
                    status = "pending"
                    failures.pop(key, None)
                else:
                    status = "publication-invalid" if failure.kind in {
                        "no-generated-publication",
                        "no-complete-publication",
                    } else "report-error"
                    failures[key] = {
                        "collection": collection,
                        "product_id": product_id,
                        "status": status,
                        "error": f"{failure.error_type}: {failure.message}",
                        "report_generation": surviving_state.get("report_generation") if surviving_state else None,
                        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                rows[key] = index_row(paths, collection, product_id, status, state=surviving_state)
                label = "pending" if status == "pending" else "failed"
                print(
                    f"[{collection}] {index}/{len(work_pairs)} {label} {product_id}: {failure.message}",
                    flush=True,
                )

            if rows[key] != old_row or failures.get(key) != old_failure:
                changed_since_checkpoint += 1
            now = time.monotonic()
            if changed_since_checkpoint and (
                changed_since_checkpoint >= CHECKPOINT_CHANGES or now - last_checkpoint >= CHECKPOINT_SECONDS
            ):
                checkpoint_report(
                    args.report_root,
                    rows,
                    failures,
                    budget,
                    artifact_base_url=args.artifact_base_url,
                )
                changed_since_checkpoint = 0
                last_checkpoint = now

        if workers == 1:
            for index, (collection, product_id) in enumerate(work_pairs, start=1):
                process_pair(index, collection, product_id)
        else:
            work_items = []
            for index, (collection, product_id) in enumerate(work_pairs, start=1):
                revisions = revision_inventory[(product_id, collection)]
                previous = states.get((collection, product_id))
                if previous is None and not revisions.get("candidates"):
                    process_pair(index, collection, product_id, parallel=True)
                    continue
                work_items.append((index, collection, product_id, revisions, previous))
            try:
                with ProcessPoolExecutor(
                    max_workers=workers,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=initialize_scan_worker,
                    initargs=(args,),
                ) as executor:
                    for index, collection, product_id, result in parallel_scan_results(
                        executor,
                        work_items,
                        workers,
                    ):
                        process_pair(
                            index,
                            collection,
                            product_id,
                            result,
                            parallel=True,
                        )
            except Exception as exc:
                pool_failure = f"{type(exc).__name__}: {exc}"
                print(f"Parallel scan stopped safely: {pool_failure}", flush=True)
                checkpoint_report(
                    args.report_root,
                    rows,
                    failures,
                    budget,
                    artifact_base_url=args.artifact_base_url,
                )
    finally:
        if revision_connection is not None:
            revision_connection.close()
    checkpoint_report(
        args.report_root,
        rows,
        failures,
        budget,
        artifact_base_url=args.artifact_base_url,
    )
    reconciled_budget = StorageBudget.load(args.report_root)
    budget.report_bytes = reconciled_budget.report_bytes
    budget.collage_bytes = reconciled_budget.collage_bytes
    write_budget(args.report_root, budget, len(states))
    print(args.report_root)
    requested_keys = set(requested_pairs)
    return 1 if pool_failure or any(key in requested_keys for key in failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
