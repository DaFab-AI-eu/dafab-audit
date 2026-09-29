import argparse
import json

from dafab_audit import nightly


def document(item_id, collection="water_analysis", version="3.0.0", assets=("dafab-water-excess", "dafab-water-excess-overview")):
    return {
        "type": "Feature", "stac_version": "1.1.0", "id": item_id, "collection": collection,
        "bbox": [0, 0, 1, 1], "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        "properties": {"datetime": "2016-02-29T03:26:52Z", "processing:version": version},
        "links": [], "assets": {key: {"href": f"https://example.test/{key}", "roles": ["data"]} for key in assets},
    }


def test_hash_ignores_the_keys_the_api_adds_when_serving():
    stored = document("A_water_analysis_300")
    served = {**stored, "name": "A_water_analysis_300", "scope": "dafab"}
    assert nightly.document_hash(served) == nightly.document_hash(stored)
    assert nightly.document_hash(served) != nightly.document_hash(document("B_water_analysis_300"))


def test_product_source_and_version_tag():
    assert nightly.product_source("IMG_water_analysis_300", "water_analysis") == ("IMG", "300")
    assert nightly.product_source("IMG_smart_agriculture_300", "water_analysis") is None
    assert nightly.product_source("IMG_water_analysis_3_0", "water_analysis") is None
    assert nightly.processing_version(document("X")) == "3.0.0"
    assert nightly.processing_version({"properties": {}}) is None


def test_attached_file_resolution_needs_the_extension_dot_after_the_key():
    item = "IMG_water_analysis_300"
    attached = [f"{item}_dafab-water-excess.geojson", f"{item}_dafab-water-excess-overview.png", f"{item}_dafab-water-excess-thumbnail.png"]
    assert nightly.attached_file_for(item, "dafab-water-excess", attached) == f"{item}_dafab-water-excess.geojson"
    assert nightly.attached_file_for(item, "dafab-water-excess-overview", attached) == f"{item}_dafab-water-excess-overview.png"
    assert nightly.attached_file_for(item, "dafab-water-deficit", attached) is None


def test_rotation_covers_every_item_exactly_once_per_period():
    ids = [f"item-{n}" for n in range(500)]
    for period in (1, 7):
        hits = {item: sum(nightly.rotation_selected(item, period, day) for day in range(period)) for item in ids}
        assert set(hits.values()) == {1}


def test_structural_issues_for_auxiliary_items():
    assert nightly.structural_issues(document("X")) == []
    broken = {"type": "Feature", "id": "X", "assets": {"data": {}}, "properties": {}, "geometry": {"type": "Point"}, "bbox": {"xmin": 0}}
    issues = nightly.structural_issues(broken)
    assert "asset data has no href" in issues
    assert "bbox is not a list of 4 or 6 numbers" in issues
    assert "properties.datetime missing, and no start_datetime with end_datetime" in issues
    ranged = document("Y")
    del ranged["properties"]["datetime"]
    ranged["properties"].update({"start_datetime": "2010-12-01T00:00:00Z", "end_datetime": "2015-02-01T00:00:00Z"})
    assert nightly.structural_issues(ranged) == []
    without_geometry = document("Z")
    without_geometry["geometry"] = None
    del without_geometry["bbox"]
    assert nightly.structural_issues(without_geometry) == []


def test_only_assets_served_by_the_catalogue_are_managed():
    doc = document("IMG", collection="sentinel_2_l2a", assets=())
    doc["assets"] = {
        "B02_10m": {"href": "https://dafab.cern.ch/stac/assets/dafab/items/IMG/B02_10m"},
        "AOT_10m": {"href": "s3://eodata/Sentinel-2/IMG/AOT_10m.jp2"},
        "preview": {"href": "https://titiler.terrascope.be/preview.png"},
    }
    assert nightly.managed_asset_keys(doc, "https://dafab.cern.ch/stac") == (["B02_10m"], ["AOT_10m", "preview"])
    assert nightly.managed_asset_keys({"assets": None}, "https://dafab.cern.ch/stac") == ([], [])


def test_merge_previous_carries_results_forward_only_for_unchanged_documents():
    record = {"id": "X", "hash": "h1", "audited_at": "2026-09-30T02:00:00Z"}
    previous = {"id": "X", "hash": "h1", "first_seen": "2026-09-01T02:00:00Z", "inventory_at": "2026-09-20T02:00:00Z", "attached": 3, "available": 3, "undeclared": 0, "inventory_issues": [], "verified_at": "2026-09-10T02:00:00Z", "verify_ok": True}
    merged = nightly.merge_previous(dict(record), previous)
    assert merged["changed"] is False and merged["first_seen"] == "2026-09-01T02:00:00Z"
    assert merged["inventory_at"] == "2026-09-20T02:00:00Z" and merged["verify_ok"] is True

    changed = nightly.merge_previous(dict(record), {**previous, "hash": "h0"})
    assert changed["changed"] is True and changed["changed_at"] == record["audited_at"] and "verified_at" not in changed

    new = nightly.merge_previous(dict(record), None)
    assert new["changed"] is False and new["first_seen"] == record["audited_at"] and new["changed_at"] is None


def test_summary_matrix_coverage_and_anomalies():
    def item(item_id, **extra):
        base = {"id": item_id, "hash": "h", "version": "3.0.0", "assets": ["a"], "audited_at": "t", "valid": True, "schema_issues": [], "changed": False}
        base.update(extra)
        return base

    units = {
        "sentinel_2_l2a": {"scope": "dafab", "collection": "sentinel_2_l2a", "kind": "original", "items": [
            item("IMG1", version=None), item("IMG2", version=None), item("IMG3", version=None)]},
        "water_analysis": {"scope": "dafab", "collection": "water_analysis", "kind": "derived", "items": [
            item("IMG1_water_analysis_300", inventory_at="t", attached=2, available=2, undeclared=0, inventory_issues=[], verified_at="t", verify_ok=True),
            item("IMG2_water_analysis_300", inventory_at="t", attached=2, available=1, undeclared=0, inventory_issues=["no available replica: x"]),
            item("IMG3_water_analysis_200", version="2.0.0", valid=False, schema_issues=["missing datetime"])]},
        "hand": {"scope": "hand", "collection": None, "kind": "auxiliary", "items": [item("TILE", version=None, changed=True)]},
    }
    summary = nightly.summarise(units, "2026-09-30T02:00:00Z")
    cells = {(c["scope"], c["collection"], c["version"]): c for c in summary["matrix"]}
    assert cells[("dafab", "water_analysis", "3.0.0")]["items"] == 2
    assert cells[("dafab", "water_analysis", "3.0.0")]["assets_complete"] == 1
    assert cells[("dafab", "water_analysis", "3.0.0")]["available"] == 1
    assert cells[("dafab", "water_analysis", "3.0.0")]["verified"] == 1
    assert cells[("dafab", "water_analysis", "2.0.0")]["valid"] == 0
    assert cells[("hand", "hand", "unversioned")]["changed"] == 1
    assert summary["matrix"][0]["scope"] == "dafab"  # public scope first

    coverage = {(c["collection"], c["version_tag"]): c for c in summary["coverage"]}
    assert coverage[("water_analysis", "300")]["images_with_product"] == 2
    assert coverage[("water_analysis", "300")]["images_without_product"] == 1
    assert coverage[("water_analysis", "200")]["images_with_product"] == 1

    kinds = {row["id"]: row["kinds"] for row in summary["anomalies"]}
    assert kinds == {"IMG2_water_analysis_300": ["assets"], "IMG3_water_analysis_200": ["schema"]}
    assert summary["totals"] == {"items": 7, "valid": 6, "verified": 1, "anomalies": 2, "surplus_items": 0, "surplus_bytes": 0}
    assert cells[("dafab", "water_analysis", "3.0.0")]["top_issues"] == [{"issue": "no available replica: x", "items": 1}]
    assert cells[("dafab", "water_analysis", "2.0.0")]["top_issues"] == [{"issue": "missing datetime", "items": 1}]


def test_surplus_files_are_reported_apart_from_completeness():
    item = {"id": "IMG", "hash": "h", "version": None, "assets": ["B02_10m"], "audited_at": "t", "valid": True, "schema_issues": [], "changed": False,
            "inventory_at": "t", "attached": 3, "available": 3, "inventory_issues": [],
            "surplus": 2, "surplus_bytes": 700_000_000, "surplus_issues": ["attached but declared with an external href: AOT_10m", "attached but not declared: WVP_10m.jp2"]}
    units = {"sentinel_2_l2a": {"scope": "dafab", "collection": "sentinel_2_l2a", "kind": "original", "items": [item]}}
    summary = nightly.summarise(units, "2026-09-30T02:00:00Z")
    cell = summary["matrix"][0]
    assert cell["assets_complete"] == 1 and cell["anomalies"] == 0
    assert cell["surplus_items"] == 1 and cell["surplus_bytes"] == 700_000_000
    assert summary["totals"]["anomalies"] == 0 and summary["totals"]["surplus_items"] == 1
    assert summary["anomalies"][0]["kinds"] == ["surplus"] and summary["anomalies"][0]["surplus_bytes"] == 700_000_000
    assert [t["issue"] for t in cell["top_issues"]] == ["attached but declared with an external href: AOT_10m", "attached but not declared: WVP_10m.jp2"]


def test_history_replaces_the_same_day_and_caps_the_length():
    summary = {"generated_at": "2026-09-30T02:00:00Z", "totals": {"items": 1, "valid": 1, "verified": 0, "anomalies": 0},
               "matrix": [{"scope": "dafab", "collection": "c", "version": "v", "items": 1, "valid": 1, "verified": 0, "anomalies": 0}]}
    history = [{"generated_at": f"2026-08-{day:02d}T02:00:00Z", "totals": {}} for day in range(1, 30)]
    history.append({"generated_at": "2026-09-30T01:00:00Z", "totals": {"items": 0}})
    updated = nightly.append_history(history, summary, limit=10)
    assert len(updated) == 10 and updated[-1]["generated_at"] == "2026-09-30T02:00:00Z"
    assert sum(1 for row in updated if row["generated_at"].startswith("2026-09-30")) == 1


def test_publish_writes_the_site_from_unit_files(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    state = {"schema_version": 1, "unit": "gfm", "scope": "gfm", "collection": None, "kind": "auxiliary", "generated_at": "t",
             "counts": {"items": 1}, "settings": {}, "items": [{"id": "G", "hash": "h", "version": None, "assets": ["data"], "audited_at": "t", "valid": True, "schema_issues": [], "changed": False}]}
    (units / "gfm.json").write_text(json.dumps(state))
    template = tmp_path / "site"
    template.mkdir()
    (template / "index.html").write_text("<html></html>")
    history = tmp_path / "history.json"
    history.write_text("[]")
    args = argparse.Namespace(units=str(units), output=str(tmp_path / "_site"), template=str(template), history=history,
                              site_url="https://example.test", stac_root="https://stac.test", discovery_site="https://disc.test/",
                              repository_url="", run_url="")
    site = nightly.publish(args, log=lambda *_: None)
    summary = json.loads((site / "data" / "summary.json").read_text())
    assert summary["totals"]["items"] == 1 and (site / "data" / "units" / "gfm.json").exists() and (site / ".nojekyll").exists()
    assert json.loads((site / "data" / "history.json").read_text())[-1]["totals"]["items"] == 1
