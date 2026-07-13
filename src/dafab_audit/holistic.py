#!/usr/bin/env python3
"""Read-only consistency audit for DaFab Rucio DIDs, STAC metadata, and S3 objects."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
import requests
import urllib3
from psycopg.rows import dict_row
import psycopg

DEFAULT_DB_DSN = os.environ.get("DAFAB_AUDIT_DB_DSN", "")
DEFAULT_SCHEMA = "dev"
DEFAULT_RSE = "MELUXINA_S3"
DEFAULT_S3_ENDPOINT = "https://s3.lxp.lu"
DEFAULT_S3_BUCKET = "p200528-rucio-data"
DEFAULT_STAC_ROOT = "https://dafab.cern.ch/stac"
ORIGINAL_COLLECTION_ID = "sentinel_2_l2a"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def load_rse_account(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    return next(iter(json.loads(path.read_text()).values()))


def s3_client(endpoint_url: str, account: dict[str, str]):
    kwargs = {"endpoint_url": endpoint_url}
    if account:
        kwargs.update(
            aws_access_key_id=account["access_key"],
            aws_secret_access_key=account["secret_key"],
            region_name=account.get("region") or os.environ.get("AWS_DEFAULT_REGION"),
        )
    return boto3.client("s3", **kwargs)


def hash_key(scope: str, name: str) -> str:
    digest = hashlib.md5(f"{scope}:{name}".encode()).hexdigest()
    return f"{scope}/{digest[:2]}/{digest[2:4]}/{name}"


def parse_hash_key(key: str) -> tuple[str, str] | None:
    parts = key.split("/")
    if len(parts) < 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
        return None
    return parts[0], "/".join(parts[3:])


def parse_s3_url_key(url: str, bucket: str) -> str | None:
    path = unquote(urlparse(url).path).lstrip("/")
    prefix = f"{bucket}/"
    return path[len(prefix):] if path.startswith(prefix) else None


def parse_stac_asset_href(href: Any, stac_root: str) -> dict[str, str] | None:
    if not isinstance(href, str) or not href.startswith(stac_root.rstrip("/") + "/assets/"):
        return None
    parts = [unquote(part) for part in urlparse(href).path.strip("/").split("/")]
    try:
        index = parts.index("assets")
    except ValueError:
        return None
    tail = parts[index + 1:]
    if len(tail) == 4 and tail[1] == "items":
        return {"route": "item_asset", "scope": tail[0], "item_id": tail[2], "asset_key": tail[3]}
    if len(tail) == 2:
        return {"route": "file_asset", "scope": tail[0], "file_name": tail[1]}
    return {"route": "unknown"}


def is_external_asset_href(href: Any, stac_root: str) -> bool:
    return isinstance(href, str) and href.strip() and not href.startswith(stac_root.rstrip("/") + "/assets/")


def parse_stac_item_link(href: Any, stac_root: str) -> str | None:
    if not isinstance(href, str) or not href.startswith(stac_root.rstrip("/") + "/collections/"):
        return None
    parts = [unquote(part) for part in urlparse(href).path.strip("/").split("/")]
    return parts[-1] if len(parts) >= 4 and parts[-2] == "items" else None


def stream_s3_checksums(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    adler = 1
    md5 = hashlib.md5()
    size = 0
    try:
        for chunk in body.iter_chunks(chunk_size=io.DEFAULT_BUFFER_SIZE * 32):
            if chunk:
                size += len(chunk)
                adler = zlib.adler32(chunk, adler)
                md5.update(chunk)
    finally:
        body.close()
    return {"bytes": size, "adler32": f"{adler & 0xFFFFFFFF:08x}", "md5": md5.hexdigest()}


def iter_s3_objects(s3: Any, bucket: str, prefixes: list[str]):
    for prefix in prefixes:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        while True:
            page = s3.list_objects(**kwargs)
            contents = page.get("Contents", [])
            yield from contents
            if not page.get("IsTruncated"):
                break
            marker = page.get("NextMarker") or (contents[-1]["Key"] if contents else None)
            if not marker:
                raise RuntimeError(f"S3 listing for prefix '{prefix}' is truncated without a continuation marker")
            kwargs["Marker"] = marker


def list_s3_root(s3: Any, bucket: str) -> tuple[list[str], list[dict[str, Any]]]:
    prefixes: list[str] = []
    root_objects: list[dict[str, Any]] = []
    kwargs = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 1000}
    while True:
        page = s3.list_objects(**kwargs)
        prefixes.extend(str(row["Prefix"]) for row in page.get("CommonPrefixes", []))
        root_objects.extend(page.get("Contents", []))
        if not page.get("IsTruncated"):
            return prefixes, root_objects
        marker = page.get("NextMarker") or (root_objects[-1]["Key"] if root_objects else prefixes[-1] if prefixes else None)
        if not marker:
            raise RuntimeError("S3 root listing is truncated without a continuation marker")
        kwargs["Marker"] = marker


def summarize_s3_prefix(s3: Any, bucket: str, prefix: str, sample_limit: int = 20) -> dict[str, Any]:
    count = 0
    total_bytes = 0
    samples = []
    for obj in iter_s3_objects(s3, bucket, [prefix]):
        count += 1
        total_bytes += int(obj.get("Size") or 0)
        if len(samples) < sample_limit:
            samples.append({"key": obj.get("Key"), "bytes": obj.get("Size")})
    return {"prefix": prefix, "objects": count, "bytes": total_bytes, "samples": samples}


def checksum_matches(did: dict[str, Any], checksums: dict[str, Any], checksum_mode: str = "full-hash") -> bool:
    if did.get("bytes") != checksums.get("bytes"):
        return False
    if checksum_mode == "size-only":
        return True
    if did.get("adler32") and str(did["adler32"]).lower() != str(checksums.get("adler32", "")).lower():
        return False
    return not did.get("md5") or str(did["md5"]).lower() == str(checksums.get("md5", "")).lower()


def fetch_rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def load_db(conn: Any, schema: str, rse: str) -> dict[str, Any]:
    log("Loading Rucio DB state")
    rows = {
        "dids": fetch_rows(conn, f"select scope, name, did_type, account, bytes, md5, adler32 from {schema}.dids"),
        "replicas": fetch_rows(
            conn,
            f"""
            select rep.scope, rep.name, rep.rse_id, r.rse, rep.bytes, rep.md5, rep.adler32, rep.state
            from {schema}.replicas rep
            left join {schema}.rses r on r.id = rep.rse_id
            """,
        ),
        "contents": fetch_rows(
            conn,
            f"""
            select scope, name, did_type, child_scope, child_name, child_type, bytes, md5, adler32
            from {schema}.contents
            """,
        ),
        "metadata": fetch_rows(
            conn,
            f"select scope, name, did_type, meta, structured_meta from {schema}.did_meta",
        ),
        "target_rse": fetch_rows(conn, f"select id, rse from {schema}.rses where rse = %s", (rse,)),
    }
    return rows


def add_problem(report: dict[str, Any], category: str, detail: dict[str, Any]) -> None:
    report["problems"][category].append(detail)


def write_report(path: Path, report: dict[str, Any]) -> None:
    report["summary"]["problem_counts"] = {k: len(v) for k, v in sorted(report["problems"].items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def check_original_collection_hierarchy(
        report: dict[str, Any],
        dids: dict[tuple[str, str], dict[str, Any]],
        metadata: dict[tuple[str, str], Any],
        content_pairs: set[tuple[str, str, str, str]],
) -> None:
    for (scope, name), doc in metadata.items():
        if dids.get((scope, name), {}).get("did_type") != "D" or not isinstance(doc, dict):
            continue
        if doc.get("collection") != ORIGINAL_COLLECTION_ID:
            continue
        report["summary"]["original_collection_items_checked"] += 1
        collection_key = (scope, ORIGINAL_COLLECTION_ID)
        if collection_key not in dids:
            add_problem(report, "original_item_collection_did_missing", {"scope": scope, "name": name, "collection": ORIGINAL_COLLECTION_ID})
        elif (scope, ORIGINAL_COLLECTION_ID, scope, name) not in content_pairs:
            add_problem(report, "original_item_missing_collection_attachment", {"scope": scope, "name": name, "collection": ORIGINAL_COLLECTION_ID})


def resolve_href(href: str, timeout: int) -> dict[str, Any]:
    try:
        response = requests.get(href, allow_redirects=False, timeout=timeout, verify=False)
        try:
            return {
                "status_code": response.status_code,
                "ok": response.status_code in {302, 303, 307, 308},
                "location": response.headers.get("Location"),
                "error": None if response.status_code in {302, 303, 307, 308} else response.text[:500],
            }
        finally:
            response.close()
    except Exception as exc:
        return {"status_code": None, "ok": False, "location": None, "error": f"{type(exc).__name__}: {exc}"}


def probe_url(url: str, timeout: int) -> dict[str, Any]:
    try:
        response = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=timeout, verify=False)
        try:
            return {"ok": response.status_code in {200, 206}, "status_code": response.status_code, "error": None}
        finally:
            response.close()
    except Exception as exc:
        return {"ok": False, "status_code": None, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only holistic DaFab Rucio/S3/STAC audit.")
    parser.add_argument("--db-dsn", default=DEFAULT_DB_DSN)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--rse", default=DEFAULT_RSE)
    parser.add_argument("--rse-account-file", type=Path, required=True)
    parser.add_argument("--s3-endpoint", default=DEFAULT_S3_ENDPOINT)
    parser.add_argument("--s3-bucket", default=DEFAULT_S3_BUCKET)
    parser.add_argument("--stac-root", default=DEFAULT_STAC_ROOT)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--checksum-mode", choices=["full-hash", "size-only"], default="full-hash")
    parser.add_argument("--bucket-scan", choices=["full-bucket", "known-scopes"], default="full-bucket")
    parser.add_argument("--resolver-checks", choices=["all", "sample"], default="all")
    parser.add_argument("--output", type=Path, default=Path("/tmp/dafab_holistic_asset_audit.json"))
    args = parser.parse_args()

    if not args.db_dsn:
        raise SystemExit("--db-dsn or DAFAB_AUDIT_DB_DSN is required")

    s3 = s3_client(args.s3_endpoint, load_rse_account(args.rse_account_file))
    with psycopg.connect(args.db_dsn) as conn:
        db = load_db(conn, args.schema, args.rse)

    dids = {(row["scope"], row["name"]): row for row in db["dids"]}
    file_dids = {key: row for key, row in dids.items() if row["did_type"] == "F"}
    dataset_dids = {key: row for key, row in dids.items() if row["did_type"] == "D"}
    metadata = {(row["scope"], row["name"]): (row.get("structured_meta") or row.get("meta")) for row in db["metadata"]}
    target_replicas = {
        (row["scope"], row["name"]): row
        for row in db["replicas"]
        if row.get("rse") == args.rse
    }
    report: dict[str, Any] = {
        "mode": "holistic_asset_audit",
        "rse": args.rse,
        "s3_bucket": args.s3_bucket,
        "checksum_mode": args.checksum_mode,
        "bucket_scan": args.bucket_scan,
        "resolver_checks": args.resolver_checks,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "dids": len(dids),
            "file_dids": len(file_dids),
            "dataset_dids": len(dataset_dids),
            "metadata_docs": len(metadata),
            "target_rse_replicas": len(target_replicas),
            "s3_objects": 0,
            "s3_objects_hashed": 0,
            "metadata_asset_hrefs": 0,
            "external_metadata_asset_hrefs": 0,
            "metadata_asset_resolvers_checked": 0,
            "metadata_asset_resolvers_skipped": 0,
            "original_collection_items_checked": 0,
            "s3_root_prefixes": 0,
            "s3_unmanaged_prefixes": 0,
            "s3_root_objects": 0,
        },
        "asset_candidates": [],
        "problems": defaultdict(list),
    }
    parent_by_file: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    files_by_dataset: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    content_pairs: set[tuple[str, str, str, str]] = set()
    for row in db["contents"]:
        parent_key = (row["scope"], row["name"])
        child_key = (row["child_scope"], row["child_name"])
        content_pairs.add((row["scope"], row["name"], row["child_scope"], row["child_name"]))
        if parent_key not in dids:
            add_problem(report, "content_parent_did_missing", {"content": row})
        if child_key not in dids:
            add_problem(report, "content_child_did_missing", {"content": row})
        elif row.get("child_type") and dids[child_key].get("did_type") != row.get("child_type"):
            add_problem(report, "content_child_type_mismatch", {"content": row, "child_did": dids[child_key]})
        if row.get("child_type") == "F":
            parent_by_file[child_key].append(row)
            files_by_dataset[parent_key].append(row)

    for row in db["replicas"]:
        replica_key = (row["scope"], row["name"])
        if replica_key not in dids:
            add_problem(report, "replica_did_missing", {"replica": row})
        elif dids[replica_key].get("did_type") != "F":
            add_problem(report, "replica_did_not_file", {"replica": row, "did": dids[replica_key]})

    for meta_key in metadata:
        if meta_key not in dids:
            add_problem(report, "metadata_did_missing", {"scope": meta_key[0], "name": meta_key[1]})
    check_original_collection_hierarchy(report, dids, metadata, content_pairs)

    s3_checksums: dict[str, dict[str, Any]] = {}
    s3_seen_keys: set[str] = set()
    known_scope_prefixes = [f"{scope}/" for scope in sorted({scope for scope, _ in file_dids})]
    root_prefixes, root_objects = list_s3_root(s3, args.s3_bucket)
    report["summary"]["s3_root_prefixes"] = len(root_prefixes)
    report["summary"]["s3_root_objects"] = len(root_objects)
    unexpected_prefixes = sorted(set(root_prefixes) - set(known_scope_prefixes))
    report["summary"]["s3_unmanaged_prefixes"] = len(unexpected_prefixes)
    for prefix in unexpected_prefixes:
        add_problem(report, "s3_unmanaged_prefix", summarize_s3_prefix(s3, args.s3_bucket, prefix))
    for obj in root_objects:
        add_problem(report, "s3_unmanaged_root_object", {"key": obj.get("Key"), "bytes": obj.get("Size")})
    s3_prefixes = known_scope_prefixes
    checksum_note = "streaming object checksums" if args.checksum_mode == "full-hash" else "checking object sizes"
    log(f"Scanning S3 and {checksum_note} for prefixes: {', '.join(s3_prefixes)}")
    for index, obj in enumerate(iter_s3_objects(s3, args.s3_bucket, s3_prefixes), start=1):
        key = str(obj.get("Key") or "")
        s3_seen_keys.add(key)
        report["summary"]["s3_objects"] += 1
        parsed = parse_hash_key(key)
        if not parsed:
            add_problem(report, "s3_unparsed_key", {"key": key, "bytes": obj.get("Size")})
            continue
        scope, name = parsed
        expected_key = hash_key(scope, name)
        did = file_dids.get((scope, name))
        if key != expected_key:
            add_problem(report, "s3_wrong_hash_path", {"key": key, "expected_key": expected_key, "scope": scope, "name": name})
        if not did:
            add_problem(report, "s3_missing_file_did", {"key": key, "scope": scope, "name": name, "bytes": obj.get("Size")})
            continue
        if args.checksum_mode == "size-only":
            checksums = {"bytes": obj.get("Size")}
            s3_checksums[key] = checksums
            if not checksum_matches(did, checksums, args.checksum_mode):
                add_problem(
                    report,
                    "s3_size_mismatch_file_did",
                    {"key": key, "scope": scope, "name": name, "did": did, "s3": checksums},
                )
        else:
            try:
                checksums = stream_s3_checksums(s3, args.s3_bucket, key)
                s3_checksums[key] = checksums
                report["summary"]["s3_objects_hashed"] += 1
                if not checksum_matches(did, checksums, args.checksum_mode):
                    add_problem(
                        report,
                        "s3_checksum_mismatch_file_did",
                        {"key": key, "scope": scope, "name": name, "did": did, "s3": checksums},
                    )
            except Exception as exc:
                add_problem(report, "s3_checksum_read_failed", {"key": key, "scope": scope, "name": name, "error": f"{type(exc).__name__}: {exc}"})
        if index % 1000 == 0:
            log(f"S3 objects scanned: {index}")
            write_report(args.output, report)

    log("Checking FILE DID replicas, storage objects, and attachments")
    for key, did in file_dids.items():
        scope, name = key
        replica = target_replicas.get(key)
        if not replica:
            add_problem(report, "file_missing_target_rse_replica", {"scope": scope, "name": name})
        else:
            expected_key = hash_key(scope, name)
            if expected_key not in s3_seen_keys:
                add_problem(report, "file_replica_missing_s3_object", {"scope": scope, "name": name, "expected_key": expected_key, "replica": replica})
            elif expected_key in s3_checksums and s3_checksums[expected_key].get("bytes") != did.get("bytes"):
                add_problem(report, "file_replica_s3_size_mismatch", {"scope": scope, "name": name, "expected_key": expected_key, "did": did, "s3": s3_checksums[expected_key]})

        parents = parent_by_file.get(key, [])
        if not parents:
            add_problem(report, "file_without_dataset_attachment", {"scope": scope, "name": name})
        for parent in parents:
            parent_key = (parent["scope"], parent["name"])
            if parent_key not in dids:
                add_problem(report, "file_attachment_parent_missing", {"scope": scope, "name": name, "parent": parent})
            elif dids[parent_key]["did_type"] != "D":
                add_problem(report, "file_attachment_parent_not_dataset", {"scope": scope, "name": name, "parent": parent, "parent_did": dids[parent_key]})
            if parent_key not in metadata:
                add_problem(report, "file_attachment_parent_missing_metadata", {"scope": scope, "name": name, "parent": parent})

    log("Checking dataset metadata, STAC asset hrefs, and resolver routes")
    sampled_scopes: set[str] = set()
    for dataset_index, (dataset_key, files) in enumerate(files_by_dataset.items(), start=1):
        if dataset_index % 1000 == 0:
            log(f"Datasets checked: {dataset_index}/{len(files_by_dataset)}; resolver routes checked: {report['summary']['metadata_asset_resolvers_checked']}")
        if dataset_key not in dataset_dids:
            continue
        if dataset_key not in metadata:
            add_problem(report, "dataset_missing_metadata", {"scope": dataset_key[0], "name": dataset_key[1]})
            continue
        doc = metadata[dataset_key]
        assets = doc.get("assets") if isinstance(doc, dict) else None
        if not isinstance(assets, dict):
            add_problem(report, "dataset_metadata_missing_assets", {"scope": dataset_key[0], "name": dataset_key[1]})
            continue

        resolved_files = set()
        for asset_key, entry in sorted(assets.items()):
            if not isinstance(entry, dict):
                continue
            href = entry.get("href")
            parts = parse_stac_asset_href(href, args.stac_root)
            report["summary"]["metadata_asset_hrefs"] += 1
            if not parts:
                if is_external_asset_href(href, args.stac_root):
                    report["summary"]["external_metadata_asset_hrefs"] += 1
                    continue
                add_problem(report, "metadata_asset_href_not_dafab_resolver", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "href": href})
                continue
            if parts.get("route") != "item_asset":
                add_problem(report, "metadata_asset_href_route_unexpected", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "href": href, "parsed": parts})
                continue
            errors = []
            if parts["scope"] != dataset_key[0]:
                errors.append("scope")
            if parts["item_id"] != dataset_key[1]:
                errors.append("item_id")
            if parts["asset_key"] != asset_key:
                errors.append("asset_key")
            if errors:
                add_problem(report, "metadata_asset_href_identity_mismatch", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "href": href, "parsed": parts, "errors": errors})

            should_check_resolver = args.resolver_checks == "all" or dataset_key[0] not in sampled_scopes
            if not should_check_resolver:
                report["summary"]["metadata_asset_resolvers_skipped"] += 1
                continue
            sampled_scopes.add(dataset_key[0])

            resolver = resolve_href(href, args.timeout)
            report["summary"]["metadata_asset_resolvers_checked"] += 1
            if not resolver["ok"]:
                add_problem(report, "metadata_asset_resolver_failed", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "href": href, "resolver": resolver})
                continue
            probe = probe_url(resolver["location"], args.timeout) if resolver.get("location") else {"ok": False, "error": "missing_location"}
            if not probe["ok"]:
                add_problem(report, "metadata_asset_download_probe_failed", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "href": href, "resolver": resolver, "probe": probe})
                continue
            redirected_key = parse_s3_url_key(resolver["location"], args.s3_bucket)
            if not redirected_key:
                add_problem(report, "metadata_asset_redirect_key_unparsed", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "location": resolver["location"]})
                continue
            parsed_key = parse_hash_key(redirected_key)
            if not parsed_key:
                add_problem(report, "metadata_asset_redirect_key_not_hashed", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "s3_key": redirected_key})
                continue
            file_key = parsed_key
            expected_name_prefix = f"{dataset_key[1]}_{asset_key}."
            if file_key[0] != dataset_key[0] or not file_key[1].startswith(expected_name_prefix):
                add_problem(
                    report,
                    "metadata_asset_resolves_noncanonical_file_did",
                    {
                        "scope": dataset_key[0],
                        "name": dataset_key[1],
                        "asset_key": asset_key,
                        "file": {"scope": file_key[0], "name": file_key[1]},
                        "expected_scope": dataset_key[0],
                        "expected_name_prefix": expected_name_prefix,
                    },
                )
            if file_key not in parent_by_file:
                add_problem(report, "metadata_asset_redirect_file_not_attached_anywhere", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "file": {"scope": file_key[0], "name": file_key[1]}})
                continue
            if not any(parent["scope"] == dataset_key[0] and parent["name"] == dataset_key[1] for parent in parent_by_file[file_key]):
                add_problem(report, "metadata_asset_redirect_file_not_attached_to_item", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "file": {"scope": file_key[0], "name": file_key[1]}})
                continue
            resolved_files.add(file_key)
            report["asset_candidates"].append(
                {
                    "scope": dataset_key[0],
                    "item_id": dataset_key[1],
                    "asset_key": asset_key,
                    "href": href,
                    "file_scope": file_key[0],
                    "file_name": file_key[1],
                    "s3_key": redirected_key,
                }
            )
            checksums = s3_checksums.get(redirected_key)
            did = file_dids.get(file_key)
            if did and checksums and not checksum_matches(did, checksums, args.checksum_mode):
                add_problem(report, "metadata_asset_redirect_checksum_mismatch", {"scope": dataset_key[0], "name": dataset_key[1], "asset_key": asset_key, "file": {"scope": file_key[0], "name": file_key[1]}, "did": did, "s3": checksums})

        if args.resolver_checks == "all":
            for file_row in files:
                file_key = (file_row["child_scope"], file_row["child_name"])
                if file_key not in resolved_files:
                    add_problem(report, "dataset_attachment_missing_metadata_asset", {"scope": dataset_key[0], "name": dataset_key[1], "file": {"scope": file_key[0], "name": file_key[1]}})

    log("Checking STAC item links")
    for did_key, doc in metadata.items():
        if not isinstance(doc, dict):
            continue
        for link in doc.get("links") or []:
            if not isinstance(link, dict) or link.get("rel") not in {"item", "child", "related", "derived_from"}:
                continue
            item_id = parse_stac_item_link(link.get("href"), args.stac_root)
            if item_id and (did_key[0], item_id) not in dataset_dids:
                add_problem(report, "metadata_link_target_dataset_missing", {"source": {"scope": did_key[0], "name": did_key[1]}, "rel": link.get("rel"), "href": link.get("href"), "target_item": item_id})

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["summary"]["asset_candidates"] = len(report["asset_candidates"])
    write_report(args.output, report)
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, indent=2, sort_keys=True))
    return 1 if any(report["problems"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
