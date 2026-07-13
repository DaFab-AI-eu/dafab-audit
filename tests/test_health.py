import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from dafab_audit import health


def _args(**overrides):
    values = {
        "rse": "MELUXINA_S3",
        "read_profile": "user_dafab",
        "profile_dir": None,
        "timeout": 120,
        "mode": "write-probe",
        "lan_policy": "auto",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _candidate(scope, item_id, asset_key):
    return {"scope": scope, "item_id": item_id, "asset_key": asset_key, "href": f"https://example.test/{scope}/{item_id}/{asset_key}", "s3_key": f"{scope}/aa/bb/{item_id}_{asset_key}.bin"}


def test_holistic_audit_uses_packaged_module(monkeypatch, tmp_path):
    output = tmp_path / "audit.json"
    args = _args(
        db_dsn="postgresql://example.test/db",
        schema="dev",
        rse_account_file=tmp_path / "rse.json",
        s3_endpoint="https://s3.example.test",
        s3_bucket="bucket",
        stac_root="https://example.test/stac",
        checksum_mode="size-only",
        bucket_scan="full-bucket",
        mode="fast",
    )
    commands = []

    def fake_run(command, check):
        commands.append((command, check))
        output.write_text(json.dumps({"summary": {"ok": True}}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(health.subprocess, "run", fake_run)

    returncode, report = health.run_holistic_audit(args, output)

    assert returncode == 0
    assert report == {"summary": {"ok": True}}
    assert commands[0][0][:3] == [health.sys.executable, "-m", "dafab_audit.holistic"]
    assert commands[0][1] is False


def test_write_probe_uploads_downloads_and_cleans(monkeypatch):
    deleted = []
    profiles = []
    uploaded = {}

    monkeypatch.setattr(health, "set_profile", lambda profile, _profile_dir: profiles.append(profile))
    monkeypatch.setattr(health, "_call_ensure_item", lambda *_: True)
    monkeypatch.setattr(
        health,
        "_call_delete_did_tree",
        lambda scope, name, dry_run: deleted.append((scope, name, dry_run)) or ({"blockers": [], "replicas": []} if dry_run else {"deleted": True}),
    )

    def fake_upload(path, _item_id, file_name, _scope, _rse):
        uploaded[file_name] = Path(path).read_bytes()
        return True

    def fake_download(file_name, destination_dir, _scope, _rse, _timeout):
        (destination_dir / file_name).write_bytes(uploaded[file_name])
        return True

    monkeypatch.setattr(health, "_call_upload_file", fake_upload)
    monkeypatch.setattr(health, "_call_download_file", fake_download)

    report = health.run_write_probe(_args())

    assert report["problems"] == []
    assert report["upload"]["ok"] is True
    assert report["download"]["ok"] is True
    assert [call[2] for call in deleted] == [True, False, True, False]
    assert "dafab_admin" in profiles
    assert "user_dafab" not in profiles


def test_write_probe_still_cleans_after_upload_failure(monkeypatch):
    deleted = []

    monkeypatch.setattr(health, "set_profile", lambda *_: None)
    monkeypatch.setattr(health, "_call_ensure_item", lambda *_: True)
    monkeypatch.setattr(
        health,
        "_call_delete_did_tree",
        lambda scope, name, dry_run: deleted.append((scope, name, dry_run)) or ({"blockers": [], "replicas": []} if dry_run else {"deleted": True}),
    )
    monkeypatch.setattr(health, "_call_upload_file", lambda *_: False)

    report = health.run_write_probe(_args())

    assert report["problems"] == [{"category": "write_probe_upload_failed"}]
    assert [call[2] for call in deleted] == [True, False, True, False]


def test_sample_candidates_picks_stable_first_per_scope():
    candidates = [
        _candidate("worldcover", "b", "map"),
        _candidate("dafab", "z", "B03_10m"),
        _candidate("dafab", "a", "B02_10m"),
    ]

    selected = health.sample_candidates(candidates)

    assert [(row["scope"], row["item_id"], row["asset_key"]) for row in selected] == [
        ("dafab", "a", "B02_10m"),
        ("worldcover", "b", "map"),
    ]


def test_fast_client_checks_probe_without_downloading(monkeypatch):
    candidates = [_candidate("dafab", "item-a", "B02_10m"), _candidate("hand", "item-b", "Map")]
    wan_checked = []
    client_checked = []

    monkeypatch.setattr(health, "download_via_wan", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected WAN download")))
    monkeypatch.setattr(health, "download_via_client", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected client download")))
    monkeypatch.setattr(health, "probe_via_wan", lambda candidate, _timeout: wan_checked.append(candidate) or {"ok": True, "mode": "stable_url_wan_probe"})
    monkeypatch.setattr(health, "probe_via_client", lambda candidate, _args: client_checked.append(candidate) or {"ok": True, "mode": "client_probe", "posix": {"ok": True}})

    report = health.run_client_checks(_args(mode="fast", lan_policy="require"), {"asset_candidates": candidates})

    assert report["problems"] == []
    assert report["summary"]["checked_candidates"] == 2
    assert report["summary"]["transfer_mode"] == "probe"
    assert report["summary"]["posix_success"] == 2
    assert wan_checked == candidates
    assert client_checked == candidates


def test_sample_client_checks_download_one_candidate_per_scope(monkeypatch):
    candidates = [
        _candidate("dafab", "z", "B03_10m"),
        _candidate("dafab", "a", "B02_10m"),
        _candidate("hand", "h", "Map"),
    ]
    wan_checked = []
    client_checked = []

    monkeypatch.setattr(health, "probe_via_wan", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected WAN probe")))
    monkeypatch.setattr(health, "probe_via_client", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected client probe")))
    monkeypatch.setattr(health, "download_via_wan", lambda candidate, _destination_dir, _timeout: wan_checked.append(candidate) or {"ok": True, "mode": "stable_url_wan"})
    monkeypatch.setattr(health, "download_via_client", lambda candidate, _destination_dir, _args: client_checked.append(candidate) or {"ok": True, "mode": "local_posix"})

    report = health.run_client_checks(_args(mode="sample", lan_policy="require"), {"asset_candidates": candidates})

    assert report["problems"] == []
    assert report["summary"]["checked_candidates"] == 2
    assert report["summary"]["transfer_mode"] == "download"
    assert [(row["scope"], row["item_id"], row["asset_key"]) for row in wan_checked] == [
        ("dafab", "a", "B02_10m"),
        ("hand", "h", "Map"),
    ]
    assert client_checked == wan_checked
