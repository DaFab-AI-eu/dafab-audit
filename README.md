# DaFab Audit

DaFab Audit validates generated-product publications and produces a static,
reviewable report with metadata, validation state, and visual collages. It reads
the unified catalog through [`dafab-client`](https://pypi.org/project/dafab-client/);
the generated audit evidence is not catalog data and is not published to Rucio.

The current report is available under [`reports/`](reports/). Open
[`reports/index.html`](reports/index.html) through a web server for the sortable
table. [`reports/README.md`](reports/README.md) is a large machine-generated
Markdown export and may exceed GitHub's rendering limit.

## Repository layout

```text
src/dafab_audit/       Installable audit, collage, and comparison code
scripts/               Local operator wrappers
tests/                 Deterministic tests
docs/                  Configuration and operating procedures
reports/               Published static audit snapshot
  inputs/              Exact product lists used by the audit
  evidence/            Sanitized workflow evidence for explicit skips
  water_analysis/
    products/<id>/     Metadata, report state, and HD collage per product
```

Collages are stored with Git LFS according to [`.gitattributes`](.gitattributes).
Clone with Git LFS enabled when the image content is needed.

The Apache-2.0 license covers the software, not the generated report snapshot.
See [`reports/NOTICE.md`](reports/NOTICE.md) for report-data provenance and
attribution.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
```

The installed commands are:

```text
dafab-audit-report
dafab-audit-compare
dafab-audit-holistic
dafab-audit-health
```

See [`docs/operations.md`](docs/operations.md) before generating or updating a
report.

The published report uses an explicit artifact base URL so HTML hosted by DaFab
can link to report files and Git LFS collages served from this repository.

## Secrets

Do not commit database credentials, DaFab profiles, CA material, tokens, or
connection notes. Keep them in local private configuration, pass the database
environment file with `--db-env` or `DAFAB_AUDIT_DB_ENV`, and provide a custom
CA bundle only when needed with `--ca-cert` or `DAFAB_AUDIT_CA_CERT`.
