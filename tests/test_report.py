import importlib.util
import json
import socket
import ssl
import sys
import urllib.error
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "src" / "dafab_audit"
REPORT_SCRIPT = PACKAGE_DIR / "report.py"
COLLAGE_SCRIPT = PACKAGE_DIR / "collage.py"
COMPARE_SCRIPT = PACKAGE_DIR / "compare.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_product_list_parser_accepts_current_shapes(tmp_path):
    module = load_module("build_generated_report_shapes", REPORT_SCRIPT)
    path = tmp_path / "products.json"

    path.write_text(json.dumps(["A", "A", "B"]))
    assert module.load_product_ids(path) == ["A", "B"]
    path.write_text(json.dumps({"product_ids": ["B", "C"]}))
    assert module.load_product_ids(path) == ["B", "C"]
    path.write_text(json.dumps({"cases": [{"product-ids": ["A", "B"]}, {"product_ids": ["B", "C"]}]}))
    assert module.load_product_ids(path) == ["A", "B", "C"]


def test_processing_evidence_loader_accepts_only_confirmed_skips(tmp_path):
    module = load_module("build_generated_report_processing_evidence", REPORT_SCRIPT)
    path = tmp_path / "evidence.json"
    skipped = {
        "product_id": "SKIPPED",
        "workflow": "water-workflow",
        "phase": "Succeeded",
        "publishable_flag": "false",
        "worker_report": {"status": 0},
        "skip_marker": {
            "product_id": "SKIPPED",
            "reason": "no_valid_patches",
            "status": "skipped",
            "use_case": "water-analysis",
        },
    }
    healthy = {"product_id": "HEALTHY", "skip_marker": None}
    path.write_text(json.dumps({"captured_at_utc": "2026-07-12T10:00:00Z", "products": [healthy, skipped]}))

    assert module.load_processing_skips(path) == {("water_analysis", "SKIPPED"): "no_valid_patches"}

    for invalid in (
        {**skipped, "phase": "Failed"},
        {**skipped, "skip_marker": {**skipped["skip_marker"], "product_id": "OTHER"}},
    ):
        path.write_text(json.dumps({"captured_at_utc": "2026-07-12T10:00:00Z", "products": [invalid]}))
        with pytest.raises(ValueError, match="inconsistent"):
            module.load_processing_skips(path)

    path.write_text(json.dumps({"captured_at_utc": "2026-07-12T10:00:00Z", "products": [skipped, skipped]}))
    with pytest.raises(ValueError, match="duplicate"):
        module.load_processing_skips(path)


def test_report_workers_default_to_one_and_must_be_positive(tmp_path, monkeypatch):
    module = load_module("build_generated_report_worker_args", REPORT_SCRIPT)
    db_env = tmp_path / "db.env"
    arguments = [
        str(REPORT_SCRIPT),
        "--product-list",
        str(tmp_path / "products.json"),
        "--report-root",
        str(tmp_path / "report"),
        "--use-case",
        "water-analysis",
        "--db-env",
        str(db_env),
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    args = module.parse_args()
    assert args.db_env == db_env
    assert args.workers == 1

    monkeypatch.setattr(sys, "argv", arguments + ["--workers", "0"])
    with pytest.raises(SystemExit):
        module.parse_args()


def test_report_paths_can_come_from_environment(tmp_path, monkeypatch):
    module = load_module("build_generated_report_environment_args", REPORT_SCRIPT)
    db_env = tmp_path / "db.env"
    ca_cert = tmp_path / "ca.pem"
    artifact_base_url = "https://example.test/audit"
    metadata_base_url = "https://example.test/metadata"
    monkeypatch.setenv("DAFAB_AUDIT_DB_ENV", str(db_env))
    monkeypatch.setenv("DAFAB_AUDIT_CA_CERT", str(ca_cert))
    monkeypatch.setenv("DAFAB_AUDIT_ARTIFACT_BASE_URL", artifact_base_url)
    monkeypatch.setenv("DAFAB_AUDIT_METADATA_BASE_URL", metadata_base_url)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(REPORT_SCRIPT),
            "--product-list",
            str(tmp_path / "products.json"),
            "--report-root",
            str(tmp_path / "report"),
            "--use-case",
            "water-analysis",
        ],
    )

    args = module.parse_args()

    assert args.db_env == db_env
    assert args.ca_cert == ca_cert
    assert args.artifact_base_url == artifact_base_url
    assert args.metadata_base_url == metadata_base_url


def test_report_requires_explicit_database_environment(tmp_path, monkeypatch):
    module = load_module("build_generated_report_required_db", REPORT_SCRIPT)
    monkeypatch.delenv("DAFAB_AUDIT_DB_ENV", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(REPORT_SCRIPT),
            "--product-list",
            str(tmp_path / "products.json"),
            "--report-root",
            str(tmp_path / "report"),
            "--use-case",
            "water-analysis",
        ],
    )

    with pytest.raises(SystemExit):
        module.parse_args()


def test_verified_tls_context_combines_public_and_optional_custom_roots(tmp_path, monkeypatch):
    module = load_module("build_generated_report_tls", REPORT_SCRIPT)
    custom_ca = tmp_path / "custom.pem"
    public_ca = tmp_path / "public.pem"
    custom_ca.write_text("custom")
    public_ca.write_text("public")
    calls = {}

    class Context:
        def load_verify_locations(self, *, cafile):
            calls["custom_ca"] = cafile

    context = Context()
    monkeypatch.setattr(module, "public_ca_bundle", lambda: str(public_ca))
    monkeypatch.setattr(
        module.ssl,
        "create_default_context",
        lambda *, cafile: calls.update(public_ca=cafile) or context,
    )

    assert module.verified_tls_context(custom_ca) is context
    assert calls == {"public_ca": str(public_ca), "custom_ca": str(custom_ca)}

    calls.clear()
    assert module.verified_tls_context() is context
    assert calls == {"public_ca": str(public_ca)}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            urllib.error.URLError(socket.gaierror(8, "DNS unavailable")),
            id="dns",
        ),
        pytest.param(TimeoutError(60, "Operation timed out"), id="direct-timeout"),
        pytest.param(
            urllib.error.URLError(TimeoutError(60, "Operation timed out")),
            id="wrapped-timeout",
        ),
    ],
)
def test_asset_revision_probe_retries_transient_errors_and_uses_supplied_tls_context(
    monkeypatch,
    error,
):
    module = load_module("build_generated_report_probe", REPORT_SCRIPT)
    supplied_context = object()
    calls = []
    sleeps = []

    class Response:
        headers = {
            "Content-Range": "bytes 0-0/42",
            "Content-Length": "1",
            "ETag": "etag",
            "Last-Modified": "timestamp",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size):
            assert size == 1
            return b"x"

    def urlopen(_request, *, timeout, context):
        calls.append((timeout, context))
        if len(calls) < 3:
            raise error
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    revision = module.probe_asset_revision("generated:asset", "https://example.test/asset", supplied_context)

    assert calls == [(60, supplied_context)] * 3
    assert sleeps == [1.0, 2.0]
    assert revision["bytes"] == 42
    assert revision["reusable"] is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            urllib.error.URLError(socket.gaierror(8, "DNS unavailable")),
            id="dns",
        ),
        pytest.param(TimeoutError(60, "Operation timed out"), id="direct-timeout"),
        pytest.param(
            urllib.error.URLError(TimeoutError(60, "Operation timed out")),
            id="wrapped-timeout",
        ),
    ],
)
def test_asset_revision_probe_reraises_transient_error_after_bounded_retries(monkeypatch, error):
    module = load_module("build_generated_report_probe_dns_exhausted", REPORT_SCRIPT)
    calls = []
    sleeps = []

    def urlopen(*_args, **_kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    with pytest.raises(type(error)) as raised:
        module.probe_asset_revision("generated:asset", "https://example.test/asset", object())

    assert raised.value is error
    assert calls == [True, True, True]
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            urllib.error.HTTPError("https://example.test/asset", 404, "Not Found", None, None),
            id="http",
        ),
        pytest.param(urllib.error.URLError(ssl.SSLError("TLS unavailable")), id="tls"),
    ],
)
def test_asset_revision_probe_does_not_retry_non_transient_error(monkeypatch, error):
    module = load_module("build_generated_report_probe_non_transient", REPORT_SCRIPT)
    calls = []

    def urlopen(*_args, **_kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: pytest.fail("HTTP errors must not sleep"))

    with pytest.raises(type(error)) as raised:
        module.probe_asset_revision("generated:asset", "https://example.test/asset", object())

    assert raised.value is error
    assert calls == [True]


def test_fetch_asset_retries_transient_stream_timeout(monkeypatch, tmp_path):
    module = load_module("build_generated_asset_collage_fetch_retry", COLLAGE_SCRIPT)
    context = object()
    calls = []
    sleeps = []

    class Response:
        def __init__(self, attempt):
            self.attempt = attempt
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size):
            assert size == 1024 * 1024
            self.reads += 1
            if self.attempt < 3:
                if self.reads == 1:
                    return b"partial"
                raise urllib.error.URLError(TimeoutError(60, "Operation timed out"))
            return b"final" if self.reads == 1 else b""

    def urlopen(request, *, timeout, context):
        calls.append((request.full_url, timeout, context))
        return Response(len(calls))

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    destination = tmp_path / "asset.bin"

    module.fetch_asset("https://example.test/asset", destination, context)

    assert calls == [("https://example.test/asset", 180, context)] * 3
    assert sleeps == [1.0, 2.0]
    assert destination.read_bytes() == b"final"
    assert not (tmp_path / ".asset.bin.part").exists()


def test_revision_database_connection_is_single_read_only_session(monkeypatch, tmp_path):
    module = load_module("build_generated_report_bulk_revisions", REPORT_SCRIPT)
    calls = {}
    monkeypatch.setattr(
        module,
        "read_env_file",
        lambda _path: {
            "DAFAB_DB_TUNNEL_HOST": "127.0.0.1",
            "DAFAB_DB_TUNNEL_PORT": "5556",
            "DAFAB_DB_USER_RUCIO": "reader",
            "DAFAB_DB_NAME": "catalog",
            "DAFAB_DB_PASSWORD_RUCIO": "secret",
        },
    )
    connection = object()
    monkeypatch.setattr(
        module.psycopg,
        "connect",
        lambda **kwargs: calls.update(kwargs) or connection,
    )

    assert module.connect_revision_database(tmp_path / "db.env") is connection
    assert calls == {
        "host": "127.0.0.1",
        "port": 5556,
        "user": "reader",
        "password": "secret",
        "dbname": "catalog",
        "sslmode": "require",
        "application_name": "dafab-audit",
        "options": "-c default_transaction_read_only=on",
        "autocommit": True,
    }


def test_revision_inventory_uses_one_bulk_query_for_all_pairs():
    module = load_module("build_generated_report_bulk_query", REPORT_SCRIPT)
    calls = []
    rows = [
        {
            "product_id": product_id,
            "collection": collection,
            "original": {"meta_updated_at": f"{product_id}-{collection}"},
            "candidates": {},
        }
        for collection in ("water_analysis", "smart_agriculture")
        for product_id in ("A", "B")
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, parameters):
            calls.append((sql, parameters))

        def fetchall(self):
            return [(row.copy(),) for row in rows]

    class Connection:
        def cursor(self):
            return Cursor()

    revisions = module.query_revision_times_bulk(
        Connection(),
        scope="dafab",
        product_ids=["A", "B"],
        collections=["water_analysis", "smart_agriculture"],
    )

    assert len(calls) == 1
    sql, parameters = calls[0]
    assert sql is module.REVISION_INVENTORY_SQL
    assert parameters == (["A", "B"], ["water_analysis", "smart_agriculture"], "dafab", "dafab")
    assert len(revisions) == 4
    assert revisions[("A", "water_analysis")]["original"]["meta_updated_at"] == "A-water_analysis"
    assert revisions[("B", "smart_agriculture")]["original"]["meta_updated_at"] == "B-smart_agriculture"


def test_parallel_worker_initializes_its_own_profile_tls_and_database(monkeypatch, tmp_path):
    module = load_module("build_generated_report_worker_init", REPORT_SCRIPT)
    profile_dir = tmp_path / "profiles"
    monkeypatch.setenv("DAFAB_PROFILE_DIR", str(tmp_path / "previous-profile"))
    args = SimpleNamespace(
        profile="dafab_skim",
        profile_dir=profile_dir,
        db_env=tmp_path / "db.env",
        ca_cert=tmp_path / "ca.pem",
    )
    calls = []

    class Connection:
        def close(self):
            calls.append("close")

    connection = Connection()
    tls_context = object()
    monkeypatch.setattr(module.dc, "set_active_account", lambda profile: calls.append(("profile", profile)))
    monkeypatch.setattr(module, "verified_tls_context", lambda ca_cert: calls.append(("tls", ca_cert)) or tls_context)
    monkeypatch.setattr(
        module,
        "connect_revision_database",
        lambda db_env: calls.append(("database", db_env)) or connection,
    )
    monkeypatch.setattr(module.atexit, "register", lambda callback: calls.append(("atexit", callback)))

    module.initialize_scan_worker(args)

    assert module.os.environ["DAFAB_PROFILE_DIR"] == str(profile_dir)
    assert module._WORKER_ARGS is args
    assert module._WORKER_SSL_CONTEXT is tls_context
    assert module._WORKER_REVISION_CONNECTION is connection
    assert calls[:3] == [
        ("profile", "dafab_skim"),
        ("tls", tmp_path / "ca.pem"),
        ("database", tmp_path / "db.env"),
    ]
    assert calls[3][0] == "atexit"

    module.close_scan_worker()
    assert calls[-1] == "close"
    assert module._WORKER_REVISION_CONNECTION is None


def test_parallel_worker_returns_serializable_failure(monkeypatch):
    module = load_module("build_generated_report_worker_failure", REPORT_SCRIPT)
    module._WORKER_ARGS = object()
    module._WORKER_SSL_CONTEXT = object()
    module._WORKER_REVISION_CONNECTION = object()
    monkeypatch.setattr(
        module,
        "prepare_product",
        lambda *_args: (_ for _ in ()).throw(module.NoCompletePublication("invalid publication")),
    )

    result = module.prepare_product_worker("water_analysis", "PRODUCT", {}, None)

    assert result == module.ScanFailure(
        "no-complete-publication",
        "NoCompletePublication",
        "invalid publication",
    )


def test_parallel_scan_submission_is_bounded(monkeypatch):
    module = load_module("build_generated_report_bounded_workers", REPORT_SCRIPT)

    class Executor:
        def __init__(self):
            self.active = 0
            self.maximum_active = 0
            self.submissions = []

        def submit(self, _function, collection, product_id, _revisions, _previous):
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.submissions.append((collection, product_id))
            future = Future()
            future.set_result(module.PreparedReport("unchanged"))
            return future

    executor = Executor()

    def complete_one(pending, *, return_when):
        assert return_when is module.FIRST_COMPLETED
        future = next(iter(pending))
        executor.active -= 1
        return {future}, set(pending) - {future}

    monkeypatch.setattr(module, "wait", complete_one)
    work_items = [
        (index, "water_analysis", f"PRODUCT-{index}", {}, None)
        for index in range(1, 6)
    ]

    results = list(module.parallel_scan_results(executor, work_items, 2))

    assert executor.maximum_active == 2
    assert executor.submissions == [
        ("water_analysis", f"PRODUCT-{index}")
        for index in range(1, 6)
    ]
    assert {result[2] for result in results} == {f"PRODUCT-{index}" for index in range(1, 6)}


def test_main_queries_revisions_once_and_passes_inventory_to_scan(monkeypatch, tmp_path):
    module = load_module("build_generated_report_bulk_main", REPORT_SCRIPT)
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(["PRODUCT"]))
    args = SimpleNamespace(
        product_list=product_list,
        report_root=tmp_path / "report",
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
    )
    revisions = {
        "original": {},
        "candidates": {"PRODUCT_water_analysis_200": {"meta_updated_at": "2026-07-10T10:00:00"}},
    }
    bulk_calls = []
    scan_calls = []
    connection = SimpleNamespace(close=lambda: None)
    state = {
        "item_id": "PRODUCT_water_analysis_200",
        "metadata_updated_at": "2026-07-10T10:00:00",
    }

    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(
        module,
        "query_revision_times_bulk",
        lambda supplied_connection, **kwargs: bulk_calls.append((supplied_connection, kwargs))
        or {("PRODUCT", "water_analysis"): revisions},
    )
    monkeypatch.setattr(module, "existing_report_states", lambda _root: {})
    monkeypatch.setattr(module, "complete_report_state", lambda _paths: state)
    monkeypatch.setattr(
        module,
        "scan_product",
        lambda *call_args: scan_calls.append(call_args) or "updated",
    )
    monkeypatch.setattr(module, "write_readme", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "write_budget", lambda *_args: None)
    monkeypatch.setattr(module, "atomic_write", lambda *_args: None)

    assert module.main() == 0
    assert len(bulk_calls) == 1
    assert bulk_calls[0][0] is connection
    assert bulk_calls[0][1]["product_ids"] == ["PRODUCT"]
    assert bulk_calls[0][1]["collections"] == ["water_analysis"]
    assert len(scan_calls) == 1
    assert scan_calls[0][-3] is connection
    assert scan_calls[0][-2] is revisions
    assert scan_calls[0][-1] is None


@pytest.mark.parametrize("pool_breaks", [False, True])
def test_parallel_main_keeps_commits_in_coordinator_and_stops_safely(
    monkeypatch,
    tmp_path,
    pool_breaks,
):
    module = load_module(f"build_generated_report_parallel_main_{pool_breaks}", REPORT_SCRIPT)
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(["PRODUCT"]))
    args = SimpleNamespace(
        product_list=product_list,
        report_root=tmp_path / "report",
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
        workers=2,
    )
    revisions = {
        "original": {},
        "candidates": {"PRODUCT_water_analysis_200": {"meta_updated_at": "2026-07-10T10:00:00"}},
    }
    state = {
        "item_id": "PRODUCT_water_analysis_200",
        "metadata_updated_at": "2026-07-10T10:00:00",
        "report_generation": 1,
    }
    committed = {}
    checkpoints = []
    pool_configuration = {}

    class Connection:
        closed = False
        close_count = 0

        def close(self):
            self.closed = True
            self.close_count += 1

    connection = Connection()
    spawn_context = object()

    class Executor:
        def __init__(self, **kwargs):
            assert connection.closed
            pool_configuration.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def submit(self, function, collection, product_id, supplied_revisions, previous):
            assert function is module.prepare_product_worker
            assert (collection, product_id, supplied_revisions, previous) == (
                "water_analysis",
                "PRODUCT",
                revisions,
                None,
            )
            future = Future()
            if pool_breaks:
                future.set_exception(module.BrokenProcessPool("worker exited"))
            else:
                future.set_result(
                    module.PreparedReport(
                        "updated",
                        metadata={"id": "PRODUCT_water_analysis_200"},
                        collage=b"collage",
                        state=state,
                    )
                )
            return future

    def commit(paths, **kwargs):
        assert connection.closed
        assert kwargs["collage"] == b"collage"
        committed[("water_analysis", paths.product_dir.name)] = kwargs["state"]

    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(
        module,
        "query_revision_times_bulk",
        lambda *_args, **_kwargs: {("PRODUCT", "water_analysis"): revisions},
    )
    monkeypatch.setattr(module, "existing_report_states", lambda _root: {})
    monkeypatch.setattr(module, "load_scan_failures", lambda _root: {})
    monkeypatch.setattr(
        module,
        "complete_report_state",
        lambda paths: committed.get(("water_analysis", paths.product_dir.name)),
    )
    monkeypatch.setattr(module, "commit_report", commit)
    monkeypatch.setattr(
        module,
        "scan_product",
        lambda *_args: pytest.fail("parallel main must not run the serial scanner"),
    )
    monkeypatch.setattr(
        module,
        "checkpoint_report",
        lambda _root, _rows, failures, _budget, **_kwargs: checkpoints.append(deepcopy(failures)),
    )
    monkeypatch.setattr(module, "write_budget", lambda *_args: None)
    monkeypatch.setattr(module, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda method: spawn_context if method == "spawn" else None)

    assert module.main() == int(pool_breaks)
    assert connection.close_count == 1
    assert pool_configuration == {
        "max_workers": 2,
        "mp_context": spawn_context,
        "initializer": module.initialize_scan_worker,
        "initargs": (args,),
    }
    assert committed == ({} if pool_breaks else {("water_analysis", "PRODUCT"): state})
    assert checkpoints
    assert all(not failures for failures in checkpoints)


def test_main_seeds_all_rows_and_checkpoints_complete_outcomes(monkeypatch, tmp_path):
    module = load_module("build_generated_report_incremental_main", REPORT_SCRIPT)
    product_ids = ["READY", "WAITING", "SKIPPED", "STALE", "STABLE"]
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(product_ids))
    processing_evidence = tmp_path / "processing-evidence.json"
    processing_evidence.write_text(
        json.dumps(
            {
                "captured_at_utc": "2026-07-12T10:00:00Z",
                "products": [
                    {
                        "product_id": "SKIPPED",
                        "workflow": "water-workflow",
                        "phase": "Succeeded",
                        "publishable_flag": "false",
                        "worker_report": {"status": 0},
                        "skip_marker": {
                            "product_id": "SKIPPED",
                            "reason": "no_valid_patches",
                            "status": "skipped",
                            "use_case": "water-analysis",
                        },
                    }
                ],
            }
        )
    )
    args = SimpleNamespace(
        product_list=product_list,
        report_root=tmp_path / "report",
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
        processing_evidence=processing_evidence,
    )
    existing_states = {
        ("water_analysis", product_id): {
            "item_id": f"{product_id}_water_analysis_200",
            "metadata_updated_at": "2026-07-10T10:00:00",
            "report_generation": index,
        }
        for index, product_id in enumerate(("STALE", "STABLE"), start=1)
    }
    current_states = dict(existing_states)
    revisions = {
        (product_id, "water_analysis"): {
            "original": {},
            "candidates": (
                {}
                if product_id in {"WAITING", "SKIPPED"}
                else {f"{product_id}_water_analysis_200": {"meta_updated_at": "2026-07-10T10:00:00"}}
            ),
        }
        for product_id in product_ids
    }
    checkpoints = []
    scan_order = []
    connection = SimpleNamespace(close=lambda: None)

    def scan_product(_args, collection, product_id, *_rest):
        scan_order.append(product_id)
        if product_id == "READY":
            current_states[(collection, product_id)] = {
                "item_id": f"{product_id}_water_analysis_200",
                "metadata_updated_at": "2026-07-10T11:00:00",
                "report_generation": 3,
            }
            return "updated"
        return "unchanged"

    def complete_state(paths):
        return current_states.get(("water_analysis", paths.product_dir.name))

    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(module, "query_revision_times_bulk", lambda *_args, **_kwargs: revisions)
    monkeypatch.setattr(module, "existing_report_states", lambda _root: dict(existing_states))
    monkeypatch.setattr(module, "load_scan_failures", lambda _root: {})
    monkeypatch.setattr(module, "complete_report_state", complete_state)
    monkeypatch.setattr(module, "scan_product", scan_product)
    monkeypatch.setattr(
        module,
        "checkpoint_report",
        lambda _root, rows, failures, _budget, **_kwargs: checkpoints.append((deepcopy(rows), deepcopy(failures))),
    )
    monkeypatch.setattr(module, "write_budget", lambda *_args: None)
    monkeypatch.setattr(module, "CHECKPOINT_CHANGES", 1)

    assert module.main() == 0
    initial_rows, initial_failures = checkpoints[0]
    assert len(initial_rows) == 5
    assert initial_rows[("water_analysis", "READY")]["status"] == "pending"
    assert initial_rows[("water_analysis", "WAITING")]["status"] == "pending"
    assert initial_rows[("water_analysis", "SKIPPED")]["status"] == "skipped-no-publication"
    assert initial_rows[("water_analysis", "STALE")]["status"] == "healthy"
    assert initial_rows[("water_analysis", "STABLE")]["status"] == "healthy"
    assert initial_failures == {}
    assert scan_order == ["READY", "STALE", "STABLE"]
    assert any(
        rows[("water_analysis", "READY")]["status"] == "healthy"
        for rows, _failures in checkpoints[1:]
    )
    final_rows, final_failures = checkpoints[-1]
    assert final_rows[("water_analysis", "WAITING")]["status"] == "pending"
    assert final_rows[("water_analysis", "SKIPPED")]["status"] == "skipped-no-publication"
    assert sum(row["status"] == "healthy" for row in final_rows.values()) == 3
    assert final_failures == {}


def test_pending_only_main_writes_consistent_report_and_budget(monkeypatch, tmp_path):
    module = load_module("build_generated_report_pending_budget", REPORT_SCRIPT)
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(["WAITING"]))
    report_root = tmp_path / "report"
    args = SimpleNamespace(
        product_list=product_list,
        report_root=report_root,
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
    )
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(
        module,
        "query_revision_times_bulk",
        lambda *_args, **_kwargs: {("WAITING", "water_analysis"): {"original": {}, "candidates": {}}},
    )
    monkeypatch.setattr(
        module,
        "scan_product",
        lambda *_args: pytest.fail("a product without a catalog candidate must not be deeply scanned"),
    )

    assert module.main() == 0
    assert "`WAITING`" in (report_root / "README.md").read_text()
    assert "| pending |" in (report_root / "README.md").read_text()
    assert json.loads((report_root / "scan-errors.json").read_text()) == []
    stored_budget = json.loads((report_root / "storage-budget.json").read_text())
    actual_budget = module.StorageBudget.load(report_root)
    assert stored_budget["report_bytes"] == actual_budget.report_bytes
    assert stored_budget["collage_bytes"] == actual_budget.collage_bytes


def test_reindex_only_rebuilds_statuses_without_external_setup(monkeypatch, tmp_path):
    module = load_module("build_generated_report_reindex_only", REPORT_SCRIPT)
    product_ids = ["HEALTHY", "SKIPPED", "PENDING", "FAILED"]
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(product_ids))
    report_root = tmp_path / "report"

    paths = module.report_paths(report_root, "water_analysis", "HEALTHY")
    paths.product_dir.mkdir(parents=True)
    metadata = {"id": "HEALTHY_water_analysis_200"}
    paths.metadata.write_text(json.dumps(metadata))
    paths.collage.write_bytes(b"collage")
    paths.state.write_text(
        json.dumps(
            {
                "schema_version": module.REPORT_STATE_SCHEMA_VERSION,
                "collection": "water_analysis",
                "product_id": "HEALTHY",
                "item_id": metadata["id"],
                "publication_valid": True,
                "report_ready": True,
                "metadata_sha256": module.canonical_sha256(metadata),
                "collage": {
                    "bytes": paths.collage.stat().st_size,
                    "sha256": module.sha256_file(paths.collage),
                },
            }
        )
    )
    extra_paths = module.report_paths(report_root, "water_analysis", "EXTRA")
    extra_paths.product_dir.mkdir(parents=True)
    extra_metadata = {"id": "EXTRA_water_analysis_200"}
    extra_paths.metadata.write_text(json.dumps(extra_metadata))
    extra_paths.collage.write_bytes(b"extra-collage")
    extra_paths.state.write_text(
        json.dumps(
            {
                "schema_version": module.REPORT_STATE_SCHEMA_VERSION,
                "collection": "water_analysis",
                "product_id": "EXTRA",
                "item_id": extra_metadata["id"],
                "publication_valid": True,
                "report_ready": True,
                "metadata_sha256": module.canonical_sha256(extra_metadata),
                "collage": {
                    "bytes": extra_paths.collage.stat().st_size,
                    "sha256": module.sha256_file(extra_paths.collage),
                },
            }
        )
    )
    (report_root / "scan-errors.json").write_text(
        json.dumps(
            [
                {
                    "collection": "water_analysis",
                    "product_id": "FAILED",
                    "status": "report-error",
                    "error": "failed before reindex",
                },
                {
                    "collection": "water_analysis",
                    "product_id": "EXTRA",
                    "status": "report-error",
                    "error": "not requested",
                },
            ]
        )
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "captured_at_utc": "2026-07-12T10:00:00Z",
                "products": [
                    {
                        "product_id": "SKIPPED",
                        "workflow": "water-workflow",
                        "phase": "Succeeded",
                        "publishable_flag": False,
                        "worker_report": {"status": 0},
                        "skip_marker": {
                            "product_id": "SKIPPED",
                            "reason": "no_valid_patches",
                            "status": "skipped",
                            "use_case": "water-analysis",
                        },
                    }
                ],
            }
        )
    )
    monkeypatch.delenv("DAFAB_AUDIT_DB_ENV", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(REPORT_SCRIPT),
            "--product-list",
            str(product_list),
            "--report-root",
            str(report_root),
            "--use-case",
            "water-analysis",
            "--processing-evidence",
            str(evidence),
            "--artifact-base-url",
            "https://example.test/audit snapshot",
            "--metadata-base-url",
            "https://example.test/metadata snapshot",
            "--reindex-only",
        ],
    )
    monkeypatch.setattr(module.dc, "set_active_account", lambda *_args: pytest.fail("client setup is forbidden"))
    monkeypatch.setattr(module, "verified_tls_context", lambda *_args: pytest.fail("network setup is forbidden"))
    monkeypatch.setattr(module, "connect_revision_database", lambda *_args: pytest.fail("database setup is forbidden"))

    assert module.main() == 1

    readme = (report_root / "README.md").read_text()
    html_report = (report_root / "index.html").read_text()
    assert readme.count("| healthy |") == 1
    assert readme.count("| skipped-no-publication |") == 1
    assert readme.count("| pending |") == 1
    assert readme.count("| report-error |") == 1
    assert "`EXTRA`" not in readme
    artifact_url = "https://example.test/audit%20snapshot/water_analysis/products/HEALTHY/collage-hd.png"
    metadata_url = "https://example.test/metadata%20snapshot/water_analysis/products/HEALTHY/metadata.json"
    assert f"[collage]({artifact_url})" in readme
    assert f"[JSON]({metadata_url})" in readme
    assert f'<a href="{artifact_url}" target="_blank" rel="noopener">collage</a>' in html_report
    assert f'<a href="{metadata_url}" target="_blank" rel="noopener">JSON</a>' in html_report
    assert json.loads((report_root / "scan-errors.json").read_text())[0]["product_id"] == "FAILED"
    assert json.loads((report_root / "storage-budget.json").read_text())["product_use_case_reports"] == 1


def test_load_scan_failures_restores_only_actionable_rows(tmp_path):
    module = load_module("build_generated_report_load_failures", REPORT_SCRIPT)
    (tmp_path / "scan-errors.json").write_text(
        json.dumps(
            [
                {"collection": "water_analysis", "product_id": "A", "status": "report-error"},
                {
                    "collection": "smart_agriculture",
                    "product_id": "B",
                    "status": "publication-invalid",
                },
                {"collection": "water_analysis", "product_id": "C", "status": "pending"},
                {"collection": "unknown", "product_id": "D", "status": "report-error"},
                {"collection": "water_analysis", "product_id": "unsafe/id", "status": "report-error"},
                "not-an-object",
            ]
        )
    )

    failures = module.load_scan_failures(tmp_path)

    assert set(failures) == {("water_analysis", "A"), ("smart_agriculture", "B")}


def test_main_drops_failure_overlay_from_an_older_report_generation(monkeypatch, tmp_path):
    module = load_module("build_generated_report_stale_failure", REPORT_SCRIPT)
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(["COMPLETE"]))
    args = SimpleNamespace(
        product_list=product_list,
        report_root=tmp_path / "report",
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
    )
    state = {
        "item_id": "COMPLETE_water_analysis_200",
        "metadata_updated_at": "2026-07-10T10:00:00",
        "report_generation": 2,
    }
    checkpoints = []
    connection = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(
        module,
        "query_revision_times_bulk",
        lambda *_args, **_kwargs: {
            ("COMPLETE", "water_analysis"): {
                "original": {},
                "candidates": {"COMPLETE_water_analysis_200": {}},
            }
        },
    )
    monkeypatch.setattr(module, "existing_report_states", lambda _root: {("water_analysis", "COMPLETE"): state})
    monkeypatch.setattr(
        module,
        "load_scan_failures",
        lambda _root: {
            ("water_analysis", "COMPLETE"): {
                "collection": "water_analysis",
                "product_id": "COMPLETE",
                "status": "report-error",
                "report_generation": 1,
            }
        },
    )
    monkeypatch.setattr(module, "scan_product", lambda *_args: "unchanged")
    monkeypatch.setattr(
        module,
        "checkpoint_report",
        lambda _root, rows, failures, _budget, **_kwargs: checkpoints.append((deepcopy(rows), deepcopy(failures))),
    )
    monkeypatch.setattr(module, "write_budget", lambda *_args: None)

    assert module.main() == 0
    assert checkpoints[0][0][("water_analysis", "COMPLETE")]["status"] == "healthy"
    assert checkpoints[0][1] == {}


def test_checkpoint_writes_machine_state_before_readme(monkeypatch, tmp_path):
    module = load_module("build_generated_report_checkpoint_order", REPORT_SCRIPT)
    calls = []
    monkeypatch.setattr(module, "write_scan_failures", lambda *_args: calls.append("failures"))
    monkeypatch.setattr(module, "write_readme", lambda *_args, **_kwargs: calls.append("readme"))

    module.checkpoint_report(tmp_path, {}, {}, module.StorageBudget(tmp_path, 0, 0))

    assert calls == ["failures", "readme"]


def test_scan_reuses_preverified_complete_state(monkeypatch, tmp_path):
    module = load_module("build_generated_report_reuse_state", REPORT_SCRIPT)
    args = SimpleNamespace(
        report_root=tmp_path,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=8,
        force=False,
    )
    state = {"catalog_revision": "catalog", "source_revision": "source"}
    monkeypatch.setattr(
        module,
        "build_publication_snapshot",
        lambda *_args: {"catalog_revision": "catalog", "source_revision": "source", "reusable": True},
    )
    monkeypatch.setattr(
        module,
        "complete_report_state",
        lambda *_args: pytest.fail("the preverified state must avoid a second local collage hash"),
    )

    outcome = module.scan_product(
        args,
        "water_analysis",
        "PRODUCT",
        object(),
        object(),
        object(),
        {},
        state,
    )

    assert outcome == "unchanged"


def test_serial_scan_commits_prepared_payload(monkeypatch, tmp_path):
    module = load_module("build_generated_report_serial_commit", REPORT_SCRIPT)
    args = SimpleNamespace(report_root=tmp_path)
    prepared = module.PreparedReport(
        "updated",
        metadata={"id": "PRODUCT_water_analysis_200"},
        collage=b"collage",
        state={"schema_version": module.REPORT_STATE_SCHEMA_VERSION},
    )
    commits = []
    monkeypatch.setattr(module, "prepare_product", lambda *_args: prepared)
    monkeypatch.setattr(module, "commit_report", lambda *args, **kwargs: commits.append((args, kwargs)))

    outcome = module.scan_product(
        args,
        "water_analysis",
        "PRODUCT",
        object(),
        object(),
        object(),
        {},
    )

    assert outcome == "updated"
    assert len(commits) == 1
    assert commits[0][1]["metadata"] == prepared.metadata
    assert commits[0][1]["collage"] == b"collage"
    assert commits[0][1]["state"] == prepared.state


def test_failed_update_drops_links_when_previous_report_no_longer_verifies(monkeypatch, tmp_path):
    module = load_module("build_generated_report_partial_update_main", REPORT_SCRIPT)
    product_list = tmp_path / "products.json"
    product_list.write_text(json.dumps(["PRODUCT"]))
    args = SimpleNamespace(
        product_list=product_list,
        report_root=tmp_path / "report",
        use_case="water-analysis",
        db_env=tmp_path / "db.env",
        profile="dafab_skim",
        profile_dir=None,
        ca_cert=None,
        artifact_base_url=None,
        reindex_only=False,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=32,
        limit=None,
        force=False,
    )
    state = {
        "item_id": "PRODUCT_water_analysis_200",
        "metadata_updated_at": "2026-07-10T10:00:00",
        "report_generation": 1,
    }
    report_is_valid = True
    checkpoints = []
    connection = SimpleNamespace(close=lambda: None)

    def fail_during_update(*_args):
        nonlocal report_is_valid
        report_is_valid = False
        raise OSError("simulated partial report replacement")

    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module.dc, "set_active_account", lambda _profile: None)
    monkeypatch.setattr(module, "verified_tls_context", lambda _ca_cert: object())
    monkeypatch.setattr(module, "connect_revision_database", lambda _path: connection)
    monkeypatch.setattr(
        module,
        "query_revision_times_bulk",
        lambda *_args, **_kwargs: {
            ("PRODUCT", "water_analysis"): {
                "original": {},
                "candidates": {"PRODUCT_water_analysis_200": {}},
            }
        },
    )
    monkeypatch.setattr(module, "existing_report_states", lambda _root: {("water_analysis", "PRODUCT"): state})
    monkeypatch.setattr(module, "load_scan_failures", lambda _root: {})
    monkeypatch.setattr(module, "scan_product", fail_during_update)
    monkeypatch.setattr(module, "complete_report_state", lambda _paths: state if report_is_valid else None)
    monkeypatch.setattr(
        module,
        "checkpoint_report",
        lambda _root, rows, failures, _budget, **_kwargs: checkpoints.append((deepcopy(rows), deepcopy(failures))),
    )
    monkeypatch.setattr(module, "write_budget", lambda *_args: None)

    assert module.main() == 1
    final_row = checkpoints[-1][0][("water_analysis", "PRODUCT")]
    assert final_row["status"] == "report-error"
    assert final_row["has_complete_report"] is False
    assert ("water_analysis", "PRODUCT") in checkpoints[-1][1]


def test_scan_refreshes_timestamps_on_same_connection_before_commit(monkeypatch, tmp_path):
    module = load_module("build_generated_report_same_connection_refresh", REPORT_SCRIPT)
    args = SimpleNamespace(
        report_root=tmp_path,
        scope="dafab",
        stac_root="https://example.test/stac",
        tile_size=8,
        force=True,
    )
    connection = object()
    initial_revisions = {"stamp": "before"}
    refreshed_revisions = {"stamp": "after"}
    snapshot_calls = []
    refresh_connections = []
    commits = []

    def snapshot(_args, _collection, _product_id, _ssl_context, revisions):
        snapshot_calls.append(revisions)
        return {
            "catalog_revision": revisions["stamp"],
            "source_revision": "selected-publication-is-unchanged",
            "reusable": True,
            "revision_payload": {
                "stamp": revisions["stamp"],
                "original_asset_count": 1,
                "original_data_bytes": 42,
            },
            "original_metadata": {
                "properties": {
                    "datetime": "2025-02-13T10:52:11.025Z",
                    "grid:code": "MGRS-30SYJ",
                }
            },
            "item": {
                "item_id": "PRODUCT_water_analysis_200",
                "metadata": {"properties": {"processing:version": "2.0.0"}},
                "facet_placements": [],
                "official_assets": {},
                "official_validation": {"valid": True},
                "meta_updated_at": revisions["stamp"],
            },
        }

    def render(_snapshot, render_dir, _tile_size, _ssl_context):
        render_dir.mkdir(parents=True)
        collage = render_dir / "collage.png"
        Image.new("RGB", (8, 8), "white").save(collage)
        return {
            "items": [{"assets": [], "collage": "collage.png"}],
            "original_rgb": {
                "available": True,
                "assets": [],
                "source": "TCI_20m",
                "preview_path": "/private/tmp/original-true-color.png",
            },
        }

    def refresh(supplied_connection, **_kwargs):
        refresh_connections.append(supplied_connection)
        return {("PRODUCT", "water_analysis"): refreshed_revisions}

    monkeypatch.setattr(module, "build_publication_snapshot", snapshot)
    monkeypatch.setattr(module, "render_snapshot", render)
    monkeypatch.setattr(module, "complete_report_state", lambda _paths: None)
    monkeypatch.setattr(module, "query_revision_times_bulk", refresh)
    monkeypatch.setattr(module, "commit_report", lambda *args, **kwargs: commits.append((args, kwargs)))

    outcome = module.scan_product(
        args,
        "water_analysis",
        "PRODUCT",
        object(),
        object(),
        connection,
        initial_revisions,
    )

    assert outcome == "updated"
    assert refresh_connections == [connection, connection]
    assert snapshot_calls == [initial_revisions, refreshed_revisions, refreshed_revisions, refreshed_revisions]
    assert len(commits) == 1
    assert commits[0][1]["state"]["rgb_source"] == {
        "available": True,
        "assets": [],
        "source": "TCI_20m",
    }


def test_comparison_html_escapes_report_values(tmp_path):
    module = load_module("compare_generated_assets_html", COMPARE_SCRIPT)
    path = tmp_path / "comparison.html"
    module.write_html(
        path,
        {
            "baseline_dir": "baseline<&",
            "candidate_dir": "candidate<&",
            "all_compared_assets_equal": False,
            "items": [
                {
                    "item_id": "<item>",
                    "assets": [
                        {
                            "asset_key": "<asset>",
                            "comparison": {"kind": "<kind>", "equal_bytes": True},
                        }
                    ],
                }
            ],
        },
    )

    rendered = path.read_text()
    assert "baseline&lt;&amp;" in rendered
    assert "&lt;item&gt;" in rendered
    assert "&lt;asset&gt;" in rendered
    assert "<item>" not in rendered


def test_audit_state_uses_official_fields_and_measured_sizes():
    module = load_module("build_generated_report_audit_state", REPORT_SCRIPT)
    metadata = {
        "id": "PRODUCT_water_analysis_200",
        "properties": {"processing:version": "2.0.0"},
    }
    snapshot = {
        "original_metadata": {
            "id": "PRODUCT",
            "properties": {
                "datetime": "2025-02-13T10:52:11.025Z",
                "grid:code": "MGRS-30SYJ",
            },
        },
        "revision_payload": {
            "original_asset_count": 36,
            "original_data_bytes": 969237633,
        },
        "item": {
            "metadata": metadata,
            "facet_placements": [
                {
                    "facet_parent_catalog_id": "water_anomaly",
                    "facet_value_catalog_id": "water_anomaly_normal",
                },
                {
                    "facet_parent_catalog_id": "water_basin",
                    "facet_value_catalog_id": "water_basin_hybas_id_l3_2030016230",
                },
            ],
            "official_validation": {"valid": True, "error_count": 0, "errors": []},
        },
    }
    downloaded_assets = [
        {"asset_key": "one", "bytes": 10},
        {"asset_key": "two", "bytes": 20},
    ]

    audit = module.audit_state(snapshot, downloaded_assets)

    assert audit["acquired_at"] == "2025-02-13T10:52:11.025Z"
    assert audit["tile_id"] == "MGRS-30SYJ"
    assert audit["basin_catalog_ids"] == ["water_basin_hybas_id_l3_2030016230"]
    assert audit["anomaly_catalog_ids"] == ["water_anomaly_normal"]
    assert audit["validation"] == {"valid": True, "error_count": 0}
    assert audit["sizes"]["original"]["data_bytes"] == 969237633
    assert audit["sizes"]["original"]["asset_count"] == 36
    assert audit["sizes"]["generated"]["data_bytes"] == 30
    assert audit["sizes"]["generated"]["asset_count"] == 2
    assert audit["sizes"]["generated"]["metadata_bytes"] == len(module.json_document(metadata))


def test_readme_includes_automated_audit_columns(tmp_path):
    module = load_module("build_generated_report_readme_audit", REPORT_SCRIPT)
    product_id = "S2C_MSIL2A_20250213T105211_N0511_R051_T30SYJ_20250213T151711"
    paths = module.report_paths(tmp_path, "water_analysis", product_id)
    state = {
        "item_id": f"{product_id}_water_analysis_200",
        "processing_version": "2.0.0",
        "metadata_updated_at": "2026-07-10T17:18:08.933233",
        "audit": {
            "acquired_at": "2025-02-13T10:52:11.025Z",
            "tile_id": "MGRS-30SYJ",
            "basin_catalog_ids": ["water_basin_hybas_id_l3_2030016230"],
            "anomaly_catalog_ids": ["water_anomaly_normal"],
            "validation": {"valid": True, "error_count": 0},
            "sizes": {
                "original": {"metadata_bytes": 4096, "data_bytes": 1048576, "asset_count": 36},
                "generated": {"metadata_bytes": 2048, "data_bytes": 2097152, "asset_count": 10},
            },
        },
    }
    row = module.index_row(paths, "water_analysis", product_id, "healthy", state=state)

    module.write_readme(tmp_path, [row])
    module.write_sortable_report(tmp_path, [row])

    readme = (tmp_path / "README.md").read_text()
    sortable = (tmp_path / "index.html").read_text()
    assert "| Use case | Acquired | Tile | Product ID | Basin | Anomaly |" in readme
    assert "[sortable HTML report](index.html)" in readme
    assert "| water-analysis | 2025-02-13 | `MGRS-30SYJ` |" in readme
    assert "`hybas_id_l3_2030016230` | `normal`" in readme
    assert "| 2.0.0 | 2026-07-10T17:18:08.933233 | healthy | pass | 4.0 KiB / 1.0 MiB | 2.0 KiB / 2.0 MiB |" in readme
    assert '<input id="filter" type="search"' in sortable
    assert 'data-column="3">Product ID</button>' in sortable
    assert 'data-sort-value="1048576">4.0 KiB / 1.0 MiB</td>' in sortable
    assert f"<code>{product_id}</code>" in sortable
    metadata_href = f"water_analysis/products/{product_id}/metadata.json"
    collage_href = f"water_analysis/products/{product_id}/collage-hd.png"
    assert f'<a href="{metadata_href}" target="_blank" rel="noopener">JSON</a>' in sortable
    assert f'<a href="{collage_href}" target="_blank" rel="noopener">collage</a>' in sortable

    legacy_base_url = "https://example.test/audit"
    module.write_readme(tmp_path, [row], artifact_base_url=legacy_base_url)
    module.write_sortable_report(tmp_path, [row], artifact_base_url=legacy_base_url)
    legacy_metadata_href = f"{legacy_base_url}/{metadata_href}"
    legacy_collage_href = f"{legacy_base_url}/{collage_href}"
    readme = (tmp_path / "README.md").read_text()
    sortable = (tmp_path / "index.html").read_text()
    assert f"[JSON]({legacy_metadata_href})" in readme
    assert f"[collage]({legacy_collage_href})" in readme
    assert f'href="{legacy_metadata_href}"' in sortable
    assert f'href="{legacy_collage_href}"' in sortable


def test_candidate_missing_from_initial_inventory_is_checked_first():
    module = load_module("build_generated_report_new_candidate_order", REPORT_SCRIPT)
    known = {"item_id": "PRODUCT_water_analysis_200", "revision_known": True, "meta_updated_at": "2026-07-10"}
    new = {"item_id": "PRODUCT_water_analysis_201", "revision_known": False}

    assert sorted([known, new], key=module.candidate_sort_key, reverse=True)[0] is new


def test_latest_complete_uses_official_validation_and_falls_back(monkeypatch):
    module = load_module("build_generated_report_latest", REPORT_SCRIPT)
    newest = "PRODUCT_water_analysis_201"
    complete = "PRODUCT_water_analysis_200"
    metadata = {
        newest: {"id": newest, "collection": "water_analysis", "assets": {}},
        complete: {"id": complete, "collection": "water_analysis", "assets": {}},
    }
    placements = [
        {"facet_parent_catalog_id": "water_anomaly", "facet_value_catalog_id": "water_anomaly_normal"},
        {"facet_parent_catalog_id": "water_basin", "facet_value_catalog_id": "water_basin_test"},
    ]
    validation_calls = []

    monkeypatch.setattr(module, "discover_candidate_ids", lambda *_args: [complete, newest])
    monkeypatch.setattr(module.dc, "get_bulk_metadata", lambda item_id, **_kwargs: metadata[item_id])
    monkeypatch.setattr(module.dc, "get_item_facet_placements", lambda *_args, **_kwargs: placements)

    def validate(**kwargs):
        validation_calls.append(kwargs)
        valid = kwargs["expected_item_id"] == complete
        return {"valid": valid, "errors": [] if valid else ["incomplete publication"]}

    monkeypatch.setattr(module.dc, "validate_derived_item", validate)
    monkeypatch.setattr(
        module,
        "official_asset_state",
        lambda item_id, *_args: {"valid": item_id == complete, "issues": [], "details": {"asset_entries": []}},
    )

    selected, rejected = module.select_latest_complete(
        scope="dafab",
        stac_root="https://dafab.cern.ch/stac",
        product_id="PRODUCT",
        collection="water_analysis",
        revisions={
            "candidates": {
                complete: {"meta_updated_at": "2026-07-10T10:00:00"},
                newest: {"meta_updated_at": "2026-07-10T11:00:00"},
            }
        },
    )

    assert selected["item_id"] == complete
    assert [row["item_id"] for row in rejected] == [newest]
    assert validation_calls[0]["source"] == "server"
    assert validation_calls[0]["expected_collection_id"] == "water_analysis"
    assert validation_calls[0]["expected_facet_placements"] == placements
    assert validation_calls[0]["autofix"] is False


def test_official_asset_state_requires_canonical_server_and_storage_state(monkeypatch):
    module = load_module("build_generated_report_assets", REPORT_SCRIPT)
    item_id = "PRODUCT_water_analysis_200"
    metadata = {"assets": {"asset": {"href": "https://example.test/asset"}}}
    monkeypatch.setattr(
        module.dc,
        "list_item_asset_entries",
        lambda *_args, **_kwargs: {
            "asset_entries": [
                {
                    "asset_key": "asset",
                    "href": "https://example.test/asset",
                    "available_on_server": True,
                    "available_on_storage": False,
                }
            ]
        },
    )
    monkeypatch.setattr(module.dc, "build_stable_asset_href", lambda *_args, **_kwargs: "https://example.test/asset")

    state = module.official_asset_state(item_id, metadata, "dafab", "https://example.test")

    assert state["valid"] is False
    assert state["issues"] == ["asset is unavailable on storage: asset"]


def test_complete_report_state_requires_both_validity_layers(tmp_path):
    module = load_module("build_generated_report_state", REPORT_SCRIPT)
    paths = module.report_paths(tmp_path, "water_analysis", "PRODUCT")
    metadata = {"id": "PRODUCT_water_analysis_200"}
    paths.product_dir.mkdir(parents=True)
    paths.metadata.write_text(json.dumps(metadata))
    paths.collage.write_bytes(b"collage")
    state = {
        "schema_version": module.REPORT_STATE_SCHEMA_VERSION,
        "publication_valid": True,
        "report_ready": True,
        "metadata_sha256": module.canonical_sha256(metadata),
        "collage": {
            "bytes": paths.collage.stat().st_size,
            "sha256": module.sha256_file(paths.collage),
        },
    }
    paths.state.write_text(json.dumps(state))

    assert module.complete_report_state(paths) == state
    state["schema_version"] -= 1
    paths.state.write_text(json.dumps(state))
    assert module.complete_report_state(paths) is None
    state["schema_version"] = module.REPORT_STATE_SCHEMA_VERSION
    state["report_ready"] = False
    paths.state.write_text(json.dumps(state))
    assert module.complete_report_state(paths) is None


def test_commit_report_updates_budget_by_exact_delta_without_rescan(tmp_path, monkeypatch):
    module = load_module("build_generated_report_budget_delta", REPORT_SCRIPT)
    report_root = tmp_path / "report"
    paths = module.report_paths(report_root, "water_analysis", "PRODUCT")
    collage = tmp_path / "collage.png"
    collage.write_bytes(b"first-collage")
    budget = module.StorageBudget.load(report_root)
    monkeypatch.setattr(
        module.StorageBudget,
        "load",
        classmethod(lambda *_args, **_kwargs: pytest.fail("commit_report must not rescan the repository")),
    )

    module.commit_report(
        paths,
        metadata={"id": "PRODUCT_water_analysis_200"},
        collage=collage,
        state={"schema_version": 2},
        budget=budget,
    )

    expected_report_bytes = sum(path.stat().st_size for path in paths.product_dir.iterdir())
    assert budget.report_bytes == expected_report_bytes
    assert budget.collage_bytes == paths.collage.stat().st_size

    replacement_collage = b"replacement-collage-with-a-different-size"
    module.commit_report(
        paths,
        metadata={"id": "PRODUCT_water_analysis_201", "extra": "value"},
        collage=replacement_collage,
        state={"schema_version": 2, "updated": True},
        budget=budget,
    )

    expected_report_bytes = sum(path.stat().st_size for path in paths.product_dir.iterdir())
    assert budget.report_bytes == expected_report_bytes
    assert budget.collage_bytes == paths.collage.stat().st_size
    assert paths.collage.read_bytes() == replacement_collage


def test_commit_report_reconciles_affected_files_after_partial_failure(tmp_path, monkeypatch):
    module = load_module("build_generated_report_budget_partial_failure", REPORT_SCRIPT)
    report_root = tmp_path / "report"
    paths = module.report_paths(report_root, "water_analysis", "PRODUCT")
    collage = tmp_path / "collage.png"
    collage.write_bytes(b"original-collage")
    budget = module.StorageBudget.load(report_root)
    module.commit_report(
        paths,
        metadata={"id": "PRODUCT_water_analysis_200"},
        collage=collage,
        state={"schema_version": 2},
        budget=budget,
    )
    original_atomic_write = module.atomic_write
    calls = 0

    def fail_on_collage(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated collage write failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(module, "atomic_write", fail_on_collage)
    collage.write_bytes(b"new-collage")

    with pytest.raises(OSError, match="simulated collage write failure"):
        module.commit_report(
            paths,
            metadata={"id": "PRODUCT_water_analysis_201", "larger": "metadata"},
            collage=collage,
            state={"schema_version": 2, "updated": True},
            budget=budget,
        )

    expected_report_bytes = sum(path.stat().st_size for path in paths.product_dir.iterdir())
    assert budget.report_bytes == expected_report_bytes
    assert budget.collage_bytes == paths.collage.stat().st_size


def test_renderer_places_rgb_tile_last(tmp_path, monkeypatch):
    module = load_module("build_generated_asset_collage_order", COLLAGE_SCRIPT)
    metadata = {
        "id": "PRODUCT_water_analysis_200",
        "assets": {
            "a": {"href": "https://example.test/a.png", "type": "image/png"},
            "b": {"href": "https://example.test/b.png", "type": "image/png"},
        },
    }
    source = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "blue").save(source)

    def fetch(_url, destination, _ssl_context):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    captured = {}

    def make_collage(tiles, destination, **_kwargs):
        captured["last_pixel"] = tiles[-1].getpixel((0, 0))
        Image.new("RGB", (10, 10), "white").save(destination)

    monkeypatch.setattr(module, "fetch_asset", fetch)
    monkeypatch.setattr(module, "make_collage", make_collage)
    monkeypatch.setattr(
        module,
        "render_original_rgb",
        lambda *_args, **_kwargs: (
            Image.new("RGB", (10, 10), "red"),
            {"available": True, "assets": [], "source": "TCI_20m"},
        ),
    )
    snapshot = {
        "product_id": "PRODUCT",
        "use_case": "water-analysis",
        "collection": "water_analysis",
        "scope": "dafab",
        "original_metadata": {},
        "item": {"item_id": metadata["id"], "metadata": metadata},
    }

    report = module.render_snapshot(snapshot, tmp_path / "render", 32, object())

    assert captured["last_pixel"] == (255, 0, 0)
    assert report["original_rgb"]["available"] is True


def test_geojson_preview_uses_reference_raster_grid(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    module = load_module("build_generated_asset_collage_grid", COLLAGE_SCRIPT)
    reference = tmp_path / "reference.tif"
    with rasterio.open(
        reference,
        "w",
        driver="GTiff",
        width=200,
        height=100,
        count=1,
        dtype="uint8",
        crs="OGC:CRS84",
        transform=from_origin(0, 100, 1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 100, 200), dtype=np.uint8))
    vector = tmp_path / "vector.geojson"
    vector.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": "OGC:CRS84"}},
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[20, 20], [40, 20], [40, 40], [20, 40], [20, 20]]],
                        },
                    }
                ],
            }
        )
    )

    preview = module.geojson_preview(vector, 100, reference)

    assert preview.size == (100, 50)
    pixels = np.asarray(preview)
    ys, xs = np.where(np.any(pixels != 255, axis=2))
    assert 9 <= xs.min() <= 10
    assert 19 <= xs.max() <= 20
    assert 29 <= ys.min() <= 30
    assert 39 <= ys.max() <= 40
    assert abs((xs.max() - xs.min()) - (ys.max() - ys.min())) <= 1


def test_water_vectors_use_required_difference_mask_grid(tmp_path, monkeypatch):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    module = load_module("build_generated_asset_collage_water_grid", COLLAGE_SCRIPT)
    reference = tmp_path / "source-reference.tif"
    with rasterio.open(
        reference,
        "w",
        driver="GTiff",
        width=40,
        height=20,
        count=1,
        dtype="uint8",
        crs="OGC:CRS84",
        transform=from_origin(0, 20, 1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 20, 40), dtype=np.uint8))
    vector = tmp_path / "source-vector.geojson"
    vector.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[4, 4], [8, 4], [8, 8], [4, 8], [4, 4]]],
                        },
                    }
                ],
            }
        )
    )
    fetches = []

    def fetch(url, destination, _ssl_context):
        fetches.append(url)
        source = reference if url == "reference" else vector
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(module, "fetch_asset", fetch)
    monkeypatch.setattr(
        module,
        "render_original_rgb",
        lambda *_args, **_kwargs: (
            Image.new("RGB", (40, 40), "blue"),
            {"available": True, "assets": [], "source": "TCI_20m"},
        ),
    )
    metadata = {
        "id": "PRODUCT_water_analysis_200",
        "assets": {
            "dafab-water-deficit": {"href": "vector", "type": "application/geo+json"},
            "dafab-AI-difference-water-mask": {
                "href": "reference",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            },
        },
    }
    snapshot = {
        "product_id": "PRODUCT",
        "use_case": "water-analysis",
        "collection": "water_analysis",
        "scope": "dafab",
        "original_metadata": {},
        "item": {"item_id": metadata["id"], "metadata": metadata},
    }

    report = module.render_snapshot(snapshot, tmp_path / "render", 40, object())

    assert fetches == ["reference", "vector"]
    previews = {}
    for row in report["items"][0]["assets"]:
        with Image.open(row["preview_path"]) as preview:
            previews[row["asset_key"]] = preview.size
    assert previews == {
        "dafab-AI-difference-water-mask": (40, 20),
        "dafab-water-deficit": (40, 20),
    }


def test_field_vectors_use_original_scene_grid(tmp_path, monkeypatch):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    module = load_module("build_generated_asset_collage_field_grid", COLLAGE_SCRIPT)
    reference = tmp_path / "original-tci.jp2"
    with rasterio.open(
        reference,
        "w",
        driver="GTiff",
        width=30,
        height=20,
        count=1,
        dtype="uint8",
        crs="OGC:CRS84",
        transform=from_origin(0, 20, 1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 20, 30), dtype=np.uint8))
    vector = tmp_path / "source-field.geojson"
    vector.write_text(json.dumps({"type": "FeatureCollection", "features": []}))

    def fetch(_url, destination, _ssl_context):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(vector.read_bytes())

    monkeypatch.setattr(module, "fetch_asset", fetch)
    monkeypatch.setattr(
        module,
        "render_original_rgb",
        lambda *_args, **_kwargs: (
            Image.new("RGB", (30, 30), "blue"),
            {
                "available": True,
                "assets": [],
                "source": "TCI_20m",
                "_reference_raster": str(reference),
            },
        ),
    )
    metadata = {
        "id": "PRODUCT_smart_agriculture_200",
        "assets": {
            "dafab-field-boundaries": {"href": "vector", "type": "application/geo+json"},
        },
    }
    snapshot = {
        "product_id": "PRODUCT",
        "use_case": "field-delineation",
        "collection": "smart_agriculture",
        "scope": "dafab",
        "original_metadata": {},
        "item": {"item_id": metadata["id"], "metadata": metadata},
    }

    report = module.render_snapshot(snapshot, tmp_path / "render", 30, object())

    asset = report["items"][0]["assets"][0]
    with Image.open(asset["preview_path"]) as preview:
        assert preview.size == (30, 20)


def test_no_complete_publication_reports_official_errors(monkeypatch):
    module = load_module("build_generated_report_invalid", REPORT_SCRIPT)
    item_id = "PRODUCT_water_analysis_200"
    monkeypatch.setattr(module, "discover_candidate_ids", lambda *_args: [item_id])
    monkeypatch.setattr(
        module.dc,
        "get_bulk_metadata",
        lambda *_args, **_kwargs: {"id": item_id, "collection": "water_analysis", "assets": {}},
    )
    monkeypatch.setattr(module.dc, "get_item_facet_placements", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        module.dc,
        "validate_derived_item",
        lambda **_kwargs: {"valid": False, "errors": ["schema mismatch"]},
    )
    monkeypatch.setattr(
        module,
        "official_asset_state",
        lambda *_args: {"valid": True, "issues": [], "details": {}},
    )

    with pytest.raises(module.NoCompletePublication, match="schema mismatch"):
        module.select_latest_complete(
            scope="dafab",
            stac_root="https://dafab.cern.ch/stac",
            product_id="PRODUCT",
            collection="water_analysis",
            revisions={"candidates": {}},
        )
