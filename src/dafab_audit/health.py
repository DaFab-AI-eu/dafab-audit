#!/usr/bin/env python3
"""Run DaFab catalog and storage health checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def speed(bytes_count: int, seconds: float) -> float:
    return bytes_count / max(seconds, 1e-9)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def run_holistic_audit(args: argparse.Namespace, output: Path) -> tuple[int, dict[str, Any]]:
    command = [
        sys.executable,
        "-m",
        "dafab_audit.holistic",
        "--db-dsn",
        args.db_dsn,
        "--schema",
        args.schema,
        "--rse",
        args.rse,
        "--rse-account-file",
        str(args.rse_account_file),
        "--s3-endpoint",
        args.s3_endpoint,
        "--s3-bucket",
        args.s3_bucket,
        "--stac-root",
        args.stac_root,
        "--timeout",
        str(args.timeout),
        "--checksum-mode",
        args.checksum_mode,
        "--bucket-scan",
        args.bucket_scan,
        "--resolver-checks",
        "sample" if args.mode in {"fast", "sample"} else "all",
        "--output",
        str(output),
    ]
    log("Running database, resolver, and storage audit")
    result = subprocess.run(command, check=False)
    report = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
    return result.returncode, report


def managed_candidates(audit_report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = audit_report.get("asset_candidates")
    if not isinstance(candidates, list):
        return []
    return [row for row in candidates if isinstance(row, dict)]


def sample_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_scopes: set[str] = set()
    selected: list[dict[str, Any]] = []
    for row in sorted(candidates, key=lambda item: (item.get("scope", ""), item.get("item_id", ""), item.get("asset_key", ""))):
        scope = str(row.get("scope") or "")
        if scope and scope not in seen_scopes:
            seen_scopes.add(scope)
            selected.append(row)
    return selected


def probe_via_wan(candidate: dict[str, Any], timeout: int) -> dict[str, Any]:
    href = candidate.get("href")
    if not isinstance(href, str) or not href.strip():
        return {"ok": False, "mode": "stable_url_wan_probe", "error": "missing_href"}

    started = time.perf_counter()
    try:
        with requests.get(
            href,
            headers={"Range": "bytes=0-0"},
            allow_redirects=True,
            stream=True,
            timeout=timeout,
            verify=False,
        ) as response:
            response.raise_for_status()
            first_byte = next(response.iter_content(chunk_size=1), b"")
        elapsed = time.perf_counter() - started
        return {
            "ok": bool(first_byte) or response.status_code in {200, 204, 206},
            "mode": "stable_url_wan_probe",
            "status_code": response.status_code,
            "seconds": elapsed,
            "bytes_read": len(first_byte),
        }
    except Exception as exc:
        return {"ok": False, "mode": "stable_url_wan_probe", "error": f"{type(exc).__name__}: {exc}"}


def download_via_wan(candidate: dict[str, Any], destination_dir: Path, timeout: int) -> dict[str, Any]:
    href = candidate.get("href")
    if not isinstance(href, str) or not href.strip():
        return {"ok": False, "error": "missing_href"}

    destination = destination_dir / f"wan-{candidate['scope']}-{candidate['item_id']}-{candidate['asset_key']}.bin"
    started = time.perf_counter()
    bytes_written = 0
    try:
        with requests.get(href, allow_redirects=True, stream=True, timeout=timeout, verify=False) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        bytes_written += len(chunk)
        elapsed = time.perf_counter() - started
        return {
            "ok": True,
            "mode": "stable_url_wan",
            "bytes_written": bytes_written,
            "seconds": elapsed,
            "bytes_per_second": speed(bytes_written, elapsed),
        }
    except Exception as exc:
        return {"ok": False, "mode": "stable_url_wan", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if destination.is_file():
            destination.unlink()


def set_profile(profile: str, profile_dir: str | None) -> None:
    if profile_dir:
        os.environ["DAFAB_PROFILE_DIR"] = profile_dir
    os.environ["DAFAB_PROFILE"] = profile
    from dafab_client._rucio import global_utils

    global_utils.set_active_account(profile)


def _call_delete_did_tree(scope: str, name: str, dry_run: bool) -> dict[str, Any]:
    import dafab_client.helpers.demo_imports as dc

    return dc.delete_did_tree(scope=scope, name=name, dry_run=dry_run)


def _call_upload_file(path: Path, item_id: str, file_name: str, scope: str, rse: str) -> bool:
    from dafab_client.helpers.file_management.Insertion.upload_file_of_scope_name import upload_file

    return upload_file(fpath=str(path), pname=item_id, scope=scope, rse=rse, did_name=file_name)


def _call_ensure_item(item_id: str, scope: str) -> bool:
    import dafab_client.helpers.demo_imports as dc

    return dc.ensure_item(item_id, scope=scope)


def _call_download_file(file_name: str, destination_dir: Path, scope: str, rse: str, timeout: int) -> bool:
    from dafab_client._rucio.dafab_lib import connection_manager

    client = connection_manager()
    replicas = list(client.list_replicas(
        [{"scope": scope, "name": file_name}],
        schemes=["https"],
        rse_expression=rse,
    ))
    pfns = replicas[0].get("pfns", {}) if replicas else {}
    pfn = next((url for url in pfns if isinstance(url, str) and url.startswith("http")), None)
    if not pfn:
        return False

    service = client.get_rse(rse).get("sign_url") or "s3"
    signed_url = client.get_signed_url(rse, service, "read", pfn)
    destination = destination_dir / file_name
    with requests.get(signed_url, stream=True, timeout=timeout, verify=False) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    return destination.is_file()


def _probe_local_posix(candidate: dict[str, Any]) -> dict[str, Any]:
    s3_key = candidate.get("s3_key")
    if not isinstance(s3_key, str) or not s3_key:
        return {"ok": False, "mode": "local_posix_probe", "error": "missing_s3_key"}

    root = Path(os.environ.get("DAFAB_RUCIO_POSIX_ROOT", "/mnt/tier2/project/p200528/rucio-data"))
    path = root / s3_key
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            first_byte = handle.read(1)
        return {
            "ok": bool(first_byte) or size == 0,
            "mode": "local_posix_probe",
            "local_path": str(path),
            "bytes": size,
            "bytes_read": len(first_byte),
        }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "local_posix_probe",
            "local_path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _missing_did(exc: Exception) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ == "DataIdentifierNotFound" or "data identifier" in text and "not found" in text


def _cleanup_write_probe(scope: str, name: str) -> dict[str, Any]:
    try:
        plan = _call_delete_did_tree(scope, name, dry_run=True)
    except Exception as exc:
        if _missing_did(exc):
            return {"ok": True, "missing": True}
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if plan.get("blockers"):
        return {"ok": False, "plan": plan, "error": "cleanup_blocked"}

    try:
        result = _call_delete_did_tree(scope, name, dry_run=False)
    except Exception as exc:
        return {"ok": False, "plan": plan, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "plan": plan, "result": result}


def run_write_probe(args: argparse.Namespace) -> dict[str, Any]:
    admin_profile = os.environ.get("DAFAB_ADMIN_PROFILE", "dafab_admin")
    scope = "dafab"
    item_id = f"dafab_health_probe_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:12]}"
    file_name = f"{item_id}_asset.bin"
    payload = (f"{item_id}\n".encode("ascii") * ((1024 * 1024) // (len(item_id) + 1) + 1))[:1024 * 1024]
    report: dict[str, Any] = {
        "scope": scope,
        "item_id": item_id,
        "file_name": file_name,
        "rse": args.rse,
        "admin_profile": admin_profile,
        "download_profile": admin_profile,
        "problems": [],
    }

    with tempfile.TemporaryDirectory(prefix="dafab-health-write.") as tmp:
        workdir = Path(tmp)
        source = workdir / file_name
        download_dir = workdir / "downloads"
        download_dir.mkdir()
        source.write_bytes(payload)

        set_profile(admin_profile, args.profile_dir)
        before = _cleanup_write_probe(scope, item_id)
        report["cleanup_before"] = before
        if not before.get("ok"):
            report["problems"].append({"category": "write_probe_cleanup_before_failed", "detail": before})
            return report

        try:
            prepared = _call_ensure_item(item_id, scope)
            report["prepare_dataset"] = {"ok": bool(prepared)}
            if not prepared:
                report["problems"].append({"category": "write_probe_dataset_prepare_failed"})
                return report

            started = time.perf_counter()
            upload_ok = _call_upload_file(source, item_id, file_name, scope, args.rse)
            elapsed = time.perf_counter() - started
            report["upload"] = {
                "ok": bool(upload_ok),
                "bytes": len(payload),
                "seconds": elapsed,
                "bytes_per_second": speed(len(payload), elapsed),
            }
            if not upload_ok:
                report["problems"].append({"category": "write_probe_upload_failed"})
                return report

            set_profile(admin_profile, args.profile_dir)
            started = time.perf_counter()
            download_error = None
            try:
                download_ok = _call_download_file(file_name, download_dir, scope, args.rse, args.timeout)
            except Exception as exc:
                download_ok = False
                download_error = f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - started
            downloaded = download_dir / file_name
            bytes_written = downloaded.stat().st_size if downloaded.is_file() else 0
            report["download"] = {
                "ok": bool(download_ok and bytes_written == len(payload)),
                "bytes": bytes_written,
                "seconds": elapsed,
                "bytes_per_second": speed(bytes_written, elapsed),
            }
            if not report["download"]["ok"]:
                report["problems"].append({
                    "category": "write_probe_download_failed",
                    "detail": {
                        "download_ok": bool(download_ok),
                        "expected_bytes": len(payload),
                        "actual_bytes": bytes_written,
                        "error": download_error,
                    },
                })
        finally:
            set_profile(admin_profile, args.profile_dir)
            after = _cleanup_write_probe(scope, item_id)
            report["cleanup_after"] = after
            if not after.get("ok"):
                report["problems"].append({"category": "write_probe_cleanup_after_failed", "detail": after})

    return report


def download_via_client(candidate: dict[str, Any], destination_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    set_profile(args.read_profile, args.profile_dir)
    import dafab_client.helpers.demo_imports as dc

    started = time.perf_counter()
    download_path: Path | None = None
    try:
        listing = dc.list_item_asset_entries(
            item_id=candidate["item_id"],
            stac_namespace=candidate["scope"],
            check_storage=True,
        )
        result = dc.download_item_asset(
            item_id=candidate["item_id"],
            asset_key=candidate["asset_key"],
            scope=candidate["scope"],
            storage_name=args.rse,
            destination_dir=destination_dir,
            overwrite=True,
            timeout=args.timeout,
        )
        elapsed = time.perf_counter() - started
        download_path = Path(result["download_path"]) if result.get("download_path") else None
        bytes_written = int(result.get("bytes_written") or (download_path.stat().st_size if download_path and download_path.is_file() else 0))
        return {
            "ok": bool(result.get("ok")),
            "mode": result.get("mode"),
            "resolved_file_name": result.get("resolved_file_name"),
            "posix_pfn": result.get("posix_pfn"),
            "posix_error": result.get("posix_error"),
            "asset_entries_checked": listing.get("metadata_asset_count"),
            "bytes_written": bytes_written,
            "seconds": elapsed,
            "bytes_per_second": speed(bytes_written, elapsed) if result.get("ok") else None,
            "error": result.get("error") or result.get("stable_url_error"),
        }
    except Exception as exc:
        return {"ok": False, "mode": "client", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if download_path and destination_dir in download_path.parents and download_path.is_file():
            download_path.unlink()


def probe_via_client(candidate: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    set_profile(args.read_profile, args.profile_dir)
    import dafab_client.helpers.demo_imports as dc

    started = time.perf_counter()
    try:
        listing = dc.list_item_asset_entries(
            item_id=candidate["item_id"],
            stac_namespace=candidate["scope"],
            check_storage=True,
        )
        entries = listing.get("asset_entries") or []
        entry = next((row for row in entries if row.get("asset_key") == candidate["asset_key"]), None)
        posix = _probe_local_posix(candidate)
        elapsed = time.perf_counter() - started
        ok = bool(
            isinstance(entry, dict)
            and entry.get("available_on_server")
            and entry.get("available_on_storage")
            and (args.lan_policy == "skip" or posix.get("ok"))
        )
        return {
            "ok": ok,
            "mode": "client_probe",
            "resolved_file_name": entry.get("resolved_file_name") if isinstance(entry, dict) else None,
            "available_on_server": entry.get("available_on_server") if isinstance(entry, dict) else None,
            "available_on_storage": entry.get("available_on_storage") if isinstance(entry, dict) else None,
            "asset_entries_checked": listing.get("metadata_asset_count"),
            "posix": posix,
            "seconds": elapsed,
        }
    except Exception as exc:
        return {"ok": False, "mode": "client_probe", "error": f"{type(exc).__name__}: {exc}"}


def run_client_checks(args: argparse.Namespace, audit_report: dict[str, Any]) -> dict[str, Any]:
    candidates = managed_candidates(audit_report)
    checked_candidates = sample_candidates(candidates) if args.mode == "sample" else candidates
    transfer_mode = "probe" if args.mode == "fast" else "download"
    output: dict[str, Any] = {
        "summary": {
            "candidates": len(candidates),
            "checked_candidates": len(checked_candidates),
            "transfer_mode": transfer_mode,
            "wan_checked": 0,
            "client_checked": 0,
            "posix_success": 0,
            "stable_url_success": 0,
        },
        "results": [],
        "problems": [],
    }
    if not candidates:
        output["problems"].append({"category": "no_managed_asset_candidates"})
        return output

    with tempfile.TemporaryDirectory(prefix="dafab-health-downloads.") as tmp:
        destination_dir = Path(tmp)
        for index, candidate in enumerate(checked_candidates, start=1):
            log(f"Checking client asset {index}/{len(checked_candidates)}: {candidate['scope']}:{candidate['item_id']} {candidate['asset_key']}")
            row = {"candidate": candidate}

            wan = probe_via_wan(candidate, args.timeout) if transfer_mode == "probe" else download_via_wan(candidate, destination_dir, args.timeout)
            output["summary"]["wan_checked"] += 1
            row["wan"] = wan
            if not wan.get("ok"):
                output["problems"].append({"category": "wan_download_failed", "candidate": candidate, "detail": wan})

            if args.lan_policy != "skip":
                client = probe_via_client(candidate, args) if transfer_mode == "probe" else download_via_client(candidate, destination_dir, args)
                output["summary"]["client_checked"] += 1
                row["client"] = client
                posix_probe = client.get("posix")
                if (client.get("mode") == "local_posix" and client.get("ok")) or (isinstance(posix_probe, dict) and posix_probe.get("ok")):
                    output["summary"]["posix_success"] += 1
                if client.get("mode") == "stable_url" and client.get("ok"):
                    output["summary"]["stable_url_success"] += 1
                if not client.get("ok"):
                    output["problems"].append({"category": "client_download_failed", "candidate": candidate, "detail": client})

            output["results"].append(row)

    if args.lan_policy == "require" and output["summary"]["posix_success"] == 0:
        output["problems"].append({"category": "posix_lan_unavailable"})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DaFab catalog/storage/client health checks.")
    parser.add_argument("--mode", choices=["fast", "sample", "full", "db-storage", "client", "write-probe"], default="full")
    parser.add_argument("--db-dsn", default=os.environ.get("DAFAB_AUDIT_DB_DSN", ""))
    parser.add_argument("--schema", default="dev")
    parser.add_argument("--rse", default="MELUXINA_S3")
    parser.add_argument("--rse-account-file", type=Path, required=True)
    parser.add_argument("--s3-endpoint", default="https://s3.lxp.lu")
    parser.add_argument("--s3-bucket", default="p200528-rucio-data")
    parser.add_argument("--stac-root", default="https://dafab.cern.ch/stac")
    parser.add_argument("--checksum-mode", choices=["full-hash", "size-only"])
    parser.add_argument("--bucket-scan", choices=["full-bucket", "known-scopes"], default="full-bucket")
    parser.add_argument("--lan-policy", choices=["auto", "require", "skip"], default="auto")
    parser.add_argument("--read-profile", default="user_dafab")
    parser.add_argument("--profile-dir", default=os.environ.get("DAFAB_PROFILE_DIR"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("/tmp/dafab_health_check.json"))
    args = parser.parse_args()
    if args.checksum_mode is None:
        args.checksum_mode = "size-only" if args.mode in {"fast", "sample"} else "full-hash"

    if args.mode == "write-probe":
        report = {"mode": args.mode, "write_probe": run_write_probe(args)}
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(args.output, report)
        problem_count = len(report["write_probe"].get("problems") or [])
        print(json.dumps({"output": str(args.output), "summary": {"problem_count": problem_count}}, indent=2))
        return 1 if problem_count else 0

    if not args.db_dsn:
        raise SystemExit("--db-dsn or DAFAB_AUDIT_DB_DSN is required")

    audit_output = args.output.with_suffix(".audit.json")
    report: dict[str, Any] = {"mode": args.mode, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    audit_rc, report["db_storage"] = run_holistic_audit(args, audit_output)

    if args.mode in {"fast", "sample", "full", "client"}:
        report["client"] = run_client_checks(args, report["db_storage"])

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(args.output, report)

    problem_count = int(bool(audit_rc))
    if "client" in report:
        problem_count += len(report["client"].get("problems") or [])
    print(json.dumps({"output": str(args.output), "summary": {"problem_count": problem_count}}, indent=2, sort_keys=True))
    return 1 if problem_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
