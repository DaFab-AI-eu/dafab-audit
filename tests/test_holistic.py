from dafab_audit import holistic as audit


def test_checksum_size_only_ignores_hash_fields():
    did = {"bytes": 10, "adler32": "bad", "md5": "bad"}
    checksums = {"bytes": 10, "adler32": "ok", "md5": "ok"}

    assert audit.checksum_matches(did, checksums, "size-only") is True


def test_checksum_full_hash_checks_adler32():
    did = {"bytes": 10, "adler32": "bad"}
    checksums = {"bytes": 10, "adler32": "ok"}

    assert audit.checksum_matches(did, checksums, "full-hash") is False


class FakeS3:
    def list_objects(self, **kwargs):
        if kwargs.get("Delimiter") == "/":
            return {
                "CommonPrefixes": [{"Prefix": "dafab/"}, {"Prefix": "demo/"}],
                "Contents": [{"Key": "root-object", "Size": 3}],
                "IsTruncated": False,
            }
        if kwargs.get("Prefix") == "demo/":
            return {
                "Contents": [
                    {"Key": "demo/aa/bb/file-a", "Size": 10},
                    {"Key": "demo/cc/dd/file-b", "Size": 20},
                ],
                "IsTruncated": False,
            }
        return {"Contents": [], "IsTruncated": False}


def test_s3_root_listing_exposes_unmanaged_prefixes():
    prefixes, root_objects = audit.list_s3_root(FakeS3(), "bucket")

    assert prefixes == ["dafab/", "demo/"]
    assert root_objects == [{"Key": "root-object", "Size": 3}]


def test_s3_prefix_summary_counts_dark_data_without_hashing():
    summary = audit.summarize_s3_prefix(FakeS3(), "bucket", "demo/", sample_limit=1)

    assert summary["objects"] == 2
    assert summary["bytes"] == 30
    assert summary["samples"] == [{"key": "demo/aa/bb/file-a", "bytes": 10}]


def test_original_collection_hierarchy_flags_missing_native_attachment():
    report = {"summary": {"original_collection_items_checked": 0}, "problems": audit.defaultdict(list)}
    dids = {
        ("dafab", "sentinel_2_l2a"): {"did_type": "C"},
        ("dafab", "S2C_ITEM"): {"did_type": "D"},
    }
    metadata = {("dafab", "S2C_ITEM"): {"collection": "sentinel_2_l2a"}}

    audit.check_original_collection_hierarchy(report, dids, metadata, set())

    assert report["summary"]["original_collection_items_checked"] == 1
    assert report["problems"]["original_item_missing_collection_attachment"] == [
        {"scope": "dafab", "name": "S2C_ITEM", "collection": "sentinel_2_l2a"}
    ]


def test_original_collection_hierarchy_accepts_native_attachment():
    report = {"summary": {"original_collection_items_checked": 0}, "problems": audit.defaultdict(list)}
    dids = {
        ("dafab", "sentinel_2_l2a"): {"did_type": "C"},
        ("dafab", "S2C_ITEM"): {"did_type": "D"},
    }
    metadata = {("dafab", "S2C_ITEM"): {"collection": "sentinel_2_l2a"}}
    content_pairs = {("dafab", "sentinel_2_l2a", "dafab", "S2C_ITEM")}

    audit.check_original_collection_hierarchy(report, dids, metadata, content_pairs)

    assert report["summary"]["original_collection_items_checked"] == 1
    assert dict(report["problems"]) == {}
