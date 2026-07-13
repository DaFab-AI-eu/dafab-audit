#!/usr/bin/env python3
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFile, ImageFont
from rasterio import Affine
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.warp import transform_geom


DISPLAY_ASSET_ORDER = {
    "water_analysis": [
        "dafab-AI-cloud-mask",
        "dafab-AI-difference-water-mask",
        "dafab-AI-observed-water",
        "dafab-AI-postprocessed-observed-water",
        "dafab-ESA-worldcover",
        "dafab-GFM-reference-water-mask",
        "dafab-no-AI-observed-water",
        "dafab-no-AI-postprocessed-observed-water",
        "dafab-water-deficit",
        "dafab-water-excess",
    ],
    "smart_agriculture": ["dafab-field-boundaries"],
}
RENDERABLE_SUFFIXES = {".tif", ".tiff", ".jp2", ".png", ".jpg", ".jpeg", ".json", ".geojson"}
TRANSIENT_NETWORK_ATTEMPTS = 3
TRANSIENT_NETWORK_RETRY_SECONDS = 1.0


def retry_transient_network(operation: Callable[[], Any]) -> Any:
    for attempt in range(TRANSIENT_NETWORK_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if (
                not isinstance(reason, (socket.gaierror, TimeoutError))
                or attempt == TRANSIENT_NETWORK_ATTEMPTS - 1
            ):
                raise
            time.sleep(TRANSIENT_NETWORK_RETRY_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


def extension_for(asset_key: str, asset: dict[str, Any]) -> str:
    href = str(asset.get("href") or "")
    media_type = str(asset.get("type") or "").lower()
    tail = href.rstrip("/").split("/")[-1]
    if "." in tail:
        return "." + tail.rsplit(".", 1)[-1].lower()
    if "geo+json" in media_type or asset_key.endswith("geojson"):
        return ".geojson"
    if "jp2" in media_type or "jpeg2000" in media_type:
        return ".jp2"
    if "tiff" in media_type:
        return ".tif"
    if "png" in media_type:
        return ".png"
    if "jpeg" in media_type:
        return ".jpg"
    return ".asset"


def asset_sort_key(asset_key: str, collection: str) -> tuple[int, str]:
    order = DISPLAY_ASSET_ORDER.get(collection, [])
    try:
        return order.index(asset_key), asset_key
    except ValueError:
        return len(order), asset_key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_asset(url: str, destination: Path, ssl_context: ssl.SSLContext) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    request = urllib.request.Request(url, headers={"User-Agent": "dafab-audit/0.1"})

    def fetch() -> None:
        partial.unlink(missing_ok=True)
        try:
            with urllib.request.urlopen(
                request,
                timeout=180,
                context=ssl_context,
            ) as response, partial.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    try:
        retry_transient_network(fetch)
        os.replace(partial, destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def normalize_array(array: np.ndarray) -> np.ndarray:
    values = array.astype("float64", copy=False)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros(values.shape[:2], dtype=np.uint8)
    low, high = np.percentile(finite, [2, 98])
    if high <= low:
        high = low + 1.0
    return ((np.clip(values, low, high) - low) * (255.0 / (high - low))).astype(np.uint8)


def raster_preview(path: Path, size: int) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = None
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    with Image.open(path) as image:
        image.seek(0)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode in {"RGB", "RGBA", "P", "L"}:
            return image.convert("RGB")
        array = np.asarray(image)
    if array.ndim == 3:
        array = array[:, :, :3]
        if array.dtype != np.uint8:
            array = normalize_array(array)
        return Image.fromarray(array.astype(np.uint8), "RGB")
    return Image.fromarray(normalize_array(array), "L").convert("RGB")


def geojson_crs(payload: dict[str, Any]) -> CRS:
    declared = payload.get("crs")
    if declared is None:
        value: Any = "OGC:CRS84"
    elif isinstance(declared, str):
        value = declared
    elif isinstance(declared, dict) and declared.get("type") == "name":
        properties = declared.get("properties")
        value = properties.get("name") if isinstance(properties, dict) else None
    else:
        raise ValueError("GeoJSON crs must be a CRS name declaration")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("GeoJSON crs name is missing")
    try:
        return CRS.from_user_input(value)
    except Exception as exc:
        raise ValueError(f"invalid GeoJSON crs {value!r}") from exc


def geojson_geometries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        features = payload.get("features")
        if not isinstance(features, list):
            raise ValueError("GeoJSON FeatureCollection features must be a list")
    elif payload_type == "Feature":
        features = [payload]
    elif payload_type in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        features = [{"type": "Feature", "geometry": payload}]
    else:
        raise ValueError(f"unsupported GeoJSON type {payload_type!r}")

    geometries: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("GeoJSON FeatureCollection contains a non-Feature entry")
        geometry = feature.get("geometry")
        if geometry is None:
            continue
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            raise ValueError("GeoJSON feature geometry is invalid")
        geometries.append(geometry)
    return geometries


def reference_preview_grid(path: Path, size: int) -> tuple[CRS, Affine, tuple[int, int]]:
    if not path.is_file():
        raise FileNotFoundError(f"reference raster is missing: {path}")
    with rasterio.open(path) as reference:
        if reference.crs is None:
            raise ValueError(f"reference raster has no CRS: {path}")
        if reference.width < 1 or reference.height < 1:
            raise ValueError(f"reference raster has invalid dimensions: {path}")
        transform = reference.transform
        if transform.is_identity or transform.determinant == 0:
            raise ValueError(f"reference raster has no invertible geotransform: {path}")
        scale = min(size / reference.width, size / reference.height)
        preview_width = max(1, round(reference.width * scale))
        preview_height = max(1, round(reference.height * scale))
        preview_transform = transform * Affine.scale(
            reference.width / preview_width,
            reference.height / preview_height,
        )
        return reference.crs, preview_transform, (preview_width, preview_height)


def geojson_preview(path: Path, size: int, reference_raster: Path) -> Image.Image:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("GeoJSON root must be an object")
    source_crs = geojson_crs(payload)
    geometries = geojson_geometries(payload)
    target_crs, preview_transform, preview_size = reference_preview_grid(reference_raster, size)
    image = Image.new("RGB", preview_size, "white")
    if not geometries:
        ImageDraw.Draw(image).text(
            (8, preview_size[1] // 2),
            "empty GeoJSON",
            fill=(80, 80, 80),
            font=ImageFont.load_default(),
        )
        return image

    projected = [
        transform_geom(source_crs, target_crs, geometry, precision=-1)
        for geometry in geometries
    ]
    mask = rasterize(
        ((geometry, 1) for geometry in projected),
        out_shape=(preview_size[1], preview_size[0]),
        transform=preview_transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)
    pixels = np.full((preview_size[1], preview_size[0], 3), 255, dtype=np.uint8)
    pixels[mask] = (230, 130, 140)
    padded = np.pad(mask, 1, constant_values=False)
    interior = (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    pixels[mask & ~interior] = (120, 30, 45)
    image = Image.fromarray(pixels, "RGB")
    return image


def make_tile(preview: Image.Image, label: str, tile_size: int) -> Image.Image:
    label_height = 64
    canvas = Image.new("RGB", (tile_size, tile_size + label_height), "white")
    canvas.paste(preview.convert("RGB"), ((tile_size - preview.width) // 2, (tile_size - preview.height) // 2))
    ImageDraw.Draw(canvas).text(
        (8, tile_size + 14),
        label,
        fill=(40, 40, 40),
        font=ImageFont.load_default(),
    )
    return canvas


def make_collage(tiles: list[Image.Image], destination: Path, columns: int = 2, gap: int = 36) -> None:
    rows = (len(tiles) + columns - 1) // columns
    width = columns * tiles[0].width + (columns + 1) * gap
    height = rows * tiles[0].height + (rows + 1) * gap
    collage = Image.new("RGB", (width, height), "white")
    for index, tile in enumerate(tiles):
        row, column = divmod(index, columns)
        collage.paste(tile, (gap + column * (tile.width + gap), gap + row * (tile.height + gap)))
    collage.save(destination, optimize=True, compress_level=9)


def render_asset(
    asset_key: str,
    asset_path: Path,
    output_dir: Path,
    tile_size: int,
    reference_raster: Path | None = None,
) -> Path:
    suffix = asset_path.suffix.lower()
    if suffix in {".tif", ".tiff", ".jp2", ".png", ".jpg", ".jpeg"}:
        preview = raster_preview(asset_path, tile_size)
    elif suffix in {".json", ".geojson"}:
        if reference_raster is None:
            raise RuntimeError(f"vector asset {asset_key} has no authoritative reference raster")
        preview = geojson_preview(asset_path, tile_size, reference_raster)
    else:
        raise ValueError(f"unsupported suffix {suffix}")
    preview_path = output_dir / "previews" / f"{safe_name(asset_key)}.png"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(preview_path, optimize=True, compress_level=9)
    return preview_path


def _band_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.seek(0)
        array = np.asarray(image)
    if array.ndim == 3:
        array = array[:, :, 0]
    return Image.fromarray(normalize_array(array), "L")


def render_original_rgb(
    original_metadata: dict[str, Any],
    output_dir: Path,
    tile_size: int,
    ssl_context: ssl.SSLContext,
) -> tuple[Image.Image | None, dict[str, Any]]:
    report: dict[str, Any] = {"available": False, "assets": []}
    assets = original_metadata.get("assets") or {}
    original_dir = output_dir / "original"
    try:
        tci = assets.get("TCI_20m")
        if isinstance(tci, dict) and tci.get("href"):
            local_path = original_dir / f"TCI_20m{extension_for('TCI_20m', tci)}"
            fetch_asset(tci["href"], local_path, ssl_context)
            preview = raster_preview(local_path, tile_size)
            report["source"] = "TCI_20m"
            source_assets = [("TCI_20m", tci, local_path)]
        else:
            channels: list[Image.Image] = []
            source_assets = []
            for asset_key in ("B04_10m", "B03_10m", "B02_10m"):
                asset = assets.get(asset_key)
                if not isinstance(asset, dict) or not asset.get("href"):
                    raise RuntimeError(f"original item is missing {asset_key}")
                local_path = original_dir / f"{asset_key}{extension_for(asset_key, asset)}"
                fetch_asset(asset["href"], local_path, ssl_context)
                channels.append(_band_image(local_path))
                source_assets.append((asset_key, asset, local_path))
            size = channels[0].size
            channels = [channel if channel.size == size else channel.resize(size, Image.Resampling.BILINEAR) for channel in channels]
            preview = Image.merge("RGB", tuple(channels))
            preview.thumbnail((tile_size, tile_size), Image.Resampling.LANCZOS)
            report["source"] = "B04_10m+B03_10m+B02_10m"
        for asset_key, asset, local_path in source_assets:
            report["assets"].append(
                {
                    "asset_key": asset_key,
                    "href": asset["href"],
                    "sha256": sha256_file(local_path),
                    "bytes": local_path.stat().st_size,
                }
            )
        report["_reference_raster"] = str(source_assets[0][2])
        preview_path = original_dir / "original-true-color.png"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview.save(preview_path, optimize=True, compress_level=9)
        report.update({"available": True, "preview_path": str(preview_path)})
        return make_tile(preview, "original-true-color", tile_size), report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return None, report


def render_snapshot(
    snapshot: dict[str, Any],
    output_dir: Path,
    tile_size: int,
    ssl_context: ssl.SSLContext,
) -> dict[str, Any]:
    item = snapshot["item"]
    metadata = item["metadata"]
    item_id = item["item_id"]
    collection = snapshot["collection"]
    item_dir = output_dir / safe_name(item_id)
    asset_dir = item_dir / "assets"
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "stac_item.json").write_text(json.dumps(metadata, indent=2) + "\n")

    rgb_tile, rgb_report = render_original_rgb(
        snapshot["original_metadata"],
        output_dir,
        tile_size,
        ssl_context,
    )
    original_reference_value = rgb_report.pop("_reference_raster", None)
    original_reference = Path(original_reference_value) if isinstance(original_reference_value, str) else None
    tiles: list[Image.Image] = []
    item_report: dict[str, Any] = {"item_id": item_id, "assets": []}
    assets = metadata.get("assets") or {}
    vector_asset_keys = [
        asset_key
        for asset_key, asset in assets.items()
        if isinstance(asset, dict) and extension_for(asset_key, asset) in {".json", ".geojson"}
    ]
    reference_raster: Path | None = None
    prefetched_assets: dict[str, Path] = {}
    if vector_asset_keys:
        if collection == "water_analysis":
            reference_key = "dafab-AI-difference-water-mask"
            reference_asset = assets.get(reference_key)
            if not isinstance(reference_asset, dict) or not reference_asset.get("href"):
                raise RuntimeError(f"water vector assets require {reference_key}")
            reference_suffix = extension_for(reference_key, reference_asset)
            if reference_suffix not in {".tif", ".tiff", ".jp2"}:
                raise RuntimeError(f"{reference_key} is not a georeferenced raster")
            reference_raster = asset_dir / f"{safe_name(reference_key)}{reference_suffix}"
            fetch_asset(reference_asset["href"], reference_raster, ssl_context)
            prefetched_assets[reference_key] = reference_raster
        elif collection == "smart_agriculture":
            if original_reference is None:
                raise RuntimeError("field vector assets require the original TCI/B04 reference grid")
            reference_raster = original_reference
        else:
            raise RuntimeError(f"collection {collection!r} has no vector reference-grid contract")
        reference_preview_grid(reference_raster, tile_size)

    for asset_key in sorted(assets, key=lambda key: asset_sort_key(key, collection)):
        asset = assets[asset_key]
        asset_report = {
            "asset_key": asset_key,
            "href": asset.get("href") if isinstance(asset, dict) else None,
            "type": asset.get("type") if isinstance(asset, dict) else None,
            "title": asset.get("title") if isinstance(asset, dict) else None,
        }
        try:
            href = asset_report["href"]
            if not isinstance(href, str) or not href:
                raise RuntimeError("asset href is missing")
            suffix = extension_for(asset_key, asset)
            if suffix not in RENDERABLE_SUFFIXES:
                raise RuntimeError(f"unsupported suffix {suffix}")
            local_path = asset_dir / f"{safe_name(asset_key)}{suffix}"
            if asset_key not in prefetched_assets:
                fetch_asset(href, local_path, ssl_context)
            preview_path = render_asset(
                asset_key,
                local_path,
                item_dir,
                tile_size,
                reference_raster=reference_raster,
            )
            with Image.open(preview_path) as preview:
                tiles.append(make_tile(preview, asset_key, tile_size))
            asset_report.update(
                {
                    "local_path": str(local_path),
                    "preview_path": str(preview_path),
                    "sha256": sha256_file(local_path),
                    "bytes": local_path.stat().st_size,
                }
            )
        except Exception as exc:
            asset_report["error"] = f"{type(exc).__name__}: {exc}"
            if asset_key in vector_asset_keys:
                raise RuntimeError(f"failed to render vector asset {asset_key}") from exc
        item_report["assets"].append(asset_report)

    if rgb_tile is not None:
        tiles.append(rgb_tile)
    if tiles:
        collage_path = item_dir / "collage.png"
        make_collage(tiles, collage_path)
        item_report["collage"] = str(collage_path.relative_to(output_dir))

    report = {
        "product_id": snapshot["product_id"],
        "use_case": snapshot["use_case"],
        "collection": collection,
        "scope": snapshot["scope"],
        "original_rgb": rgb_report,
        "items": [item_report],
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
