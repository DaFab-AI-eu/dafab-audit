#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFile


Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two generated asset collage runs.")
    parser.add_argument("baseline_dir", type=Path)
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def item_assets(run_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    report = read_json(run_dir / "report.json")
    return {
        item["item_id"]: {asset["asset_key"]: asset for asset in item.get("assets", [])}
        for item in report.get("items", [])
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(path: Path) -> bytes:
    return json.dumps(read_json(path), sort_keys=True, separators=(",", ":")).encode()


def json_summary(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    features = payload.get("features") if payload.get("type") == "FeatureCollection" else None
    return {
        "sha256": hashlib.sha256(canonical_json(path)).hexdigest(),
        "type": payload.get("type"),
        "feature_count": len(features) if isinstance(features, list) else None,
    }


def array_counts(array: np.ndarray) -> dict[str, int]:
    values, counts = np.unique(array, return_counts=True)
    return {str(value.item() if hasattr(value, "item") else value): int(count) for value, count in zip(values, counts)}


def raster_summary(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        image.seek(0)
        array = np.asarray(image)
        return {
            "sha256": sha256(path),
            "mode": image.mode,
            "size": list(image.size),
            "dtype": str(array.dtype),
            "shape": list(array.shape),
            "min": float(np.nanmin(array)),
            "max": float(np.nanmax(array)),
            "unique_counts": array_counts(array) if array.size <= 150_000_000 else None,
        }


def compare_rasters(left: Path, right: Path) -> dict[str, Any]:
    left_summary = raster_summary(left)
    right_summary = raster_summary(right)
    result = {"baseline": left_summary, "candidate": right_summary}
    if left_summary["shape"] != right_summary["shape"]:
        result["equal_pixels"] = False
        result["reason"] = "shape differs"
        return result
    with Image.open(left) as left_image, Image.open(right) as right_image:
        left_image.seek(0)
        right_image.seek(0)
        left_array = np.asarray(left_image)
        right_array = np.asarray(right_image)
        diff = left_array.astype("float64") - right_array.astype("float64")
        unequal = int(np.count_nonzero(diff))
        result.update(
            {
                "equal_pixels": unequal == 0,
                "unequal_pixels": unequal,
                "unequal_percent": unequal * 100.0 / int(left_array.size),
                "max_abs_diff": float(np.max(np.abs(diff))) if diff.size else 0.0,
                "mean_abs_diff": float(np.mean(np.abs(diff))) if diff.size else 0.0,
            }
        )
    return result


def compare_json(left: Path, right: Path) -> dict[str, Any]:
    left_summary = json_summary(left)
    right_summary = json_summary(right)
    return {
        "baseline": left_summary,
        "candidate": right_summary,
        "equal_canonical_json": left_summary["sha256"] == right_summary["sha256"],
    }


def compare_asset(left: Path, right: Path) -> dict[str, Any]:
    suffix = left.suffix.lower()
    if suffix in {".tif", ".tiff", ".png", ".jpg", ".jpeg"}:
        return {"kind": "raster", **compare_rasters(left, right)}
    if suffix in {".json", ".geojson"}:
        return {"kind": "json", **compare_json(left, right)}
    return {
        "kind": "binary",
        "baseline": {"sha256": sha256(left)},
        "candidate": {"sha256": sha256(right)},
        "equal_bytes": sha256(left) == sha256(right),
    }


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_html(path: Path, report: dict[str, Any]) -> None:
    def escaped(value: Any) -> str:
        return html.escape(str(value), quote=True)

    lines = [
        "<!doctype html>",
        "<meta charset='utf-8'>",
        "<title>DaFab generated asset comparison</title>",
        "<style>body{font-family:sans-serif;margin:24px} table{border-collapse:collapse} td,th{border:1px solid #ddd;padding:6px 8px} code{font-size:12px}</style>",
        "<h1>DaFab generated asset comparison</h1>",
        f"<p>Baseline: <code>{escaped(report['baseline_dir'])}</code></p>",
        f"<p>Candidate: <code>{escaped(report['candidate_dir'])}</code></p>",
        f"<p>All compared assets equal: <b>{escaped(report['all_compared_assets_equal'])}</b></p>",
        "<table><tr><th>Item</th><th>Asset</th><th>Kind</th><th>Equal</th><th>Unequal %</th><th>Max abs diff</th></tr>",
    ]
    for item in report["items"]:
        for asset in item["assets"]:
            cmp = asset.get("comparison", {})
            equal = cmp.get("equal_pixels", cmp.get("equal_canonical_json", cmp.get("equal_bytes")))
            lines.append(
                "<tr>"
                f"<td>{escaped(item['item_id'])}</td>"
                f"<td>{escaped(asset['asset_key'])}</td>"
                f"<td>{escaped(cmp.get('kind'))}</td>"
                f"<td>{escaped(equal)}</td>"
                f"<td>{escaped(cmp.get('unequal_percent', ''))}</td>"
                f"<td>{escaped(cmp.get('max_abs_diff', ''))}</td>"
                "</tr>"
            )
    lines.append("</table>")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()
    baseline_dir = args.baseline_dir.resolve()
    candidate_dir = args.candidate_dir.resolve()
    output_dir = args.output_dir or candidate_dir / f"comparison_against_{baseline_dir.name}"
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline = item_assets(baseline_dir)
    candidate = item_assets(candidate_dir)
    item_ids = sorted(set(baseline) | set(candidate))
    report: dict[str, Any] = {
        "baseline_dir": str(baseline_dir),
        "candidate_dir": str(candidate_dir),
        "output_dir": str(output_dir),
        "items": [],
    }

    all_equal = True
    for item_id in item_ids:
        item_report = {"item_id": item_id, "assets": []}
        asset_keys = sorted(set(baseline.get(item_id, {})) | set(candidate.get(item_id, {})))
        for asset_key in asset_keys:
            left = baseline.get(item_id, {}).get(asset_key)
            right = candidate.get(item_id, {}).get(asset_key)
            asset_report = {"asset_key": asset_key}
            if not left or not right:
                asset_report["missing"] = "baseline" if not left else "candidate"
                all_equal = False
            else:
                left_path = Path(left["local_path"])
                right_path = Path(right["local_path"])
                comparison = compare_asset(left_path, right_path)
                asset_report["baseline_path"] = rel(left_path, baseline_dir)
                asset_report["candidate_path"] = rel(right_path, candidate_dir)
                asset_report["comparison"] = comparison
                equal = comparison.get("equal_pixels", comparison.get("equal_canonical_json", comparison.get("equal_bytes")))
                all_equal = all_equal and bool(equal)
            item_report["assets"].append(asset_report)
        report["items"].append(item_report)

    report["all_compared_assets_equal"] = all_equal
    (output_dir / "comparison_report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_html(output_dir / "comparison.html", report)
    print(output_dir)
    return 0 if all_equal else 2


if __name__ == "__main__":
    raise SystemExit(main())
