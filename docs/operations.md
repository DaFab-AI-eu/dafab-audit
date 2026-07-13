# Audit operations

## Prerequisites

- Python 3.10 or newer
- Git LFS for report collages
- Network access to the DaFab catalog and published assets
- Read-only access to the Rucio database revision inventory
- A local `dafab-client` profile

Install the package and test dependencies in a virtual environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
```

## Private configuration

Keep all connection material outside this repository.

The database environment file is selected explicitly with `--db-env`. If that
option is omitted, `DAFAB_AUDIT_DB_ENV` must point to the file. It contains:

```text
DAFAB_DB_TUNNEL_HOST
DAFAB_DB_TUNNEL_PORT
DAFAB_DB_USER_RUCIO
DAFAB_DB_PASSWORD_RUCIO
DAFAB_DB_NAME
```

The audit opens the database connection in read-only mode. Restrict the file to
the local user and never add it, copied values, or command output containing
those values to Git.

Select the DaFab API profile with `--profile` or `DAFAB_PROFILE`. Use
`--profile-dir` or `DAFAB_PROFILE_DIR` for a private profile directory. HTTPS
uses the public Requests CA bundle by default. If the deployment requires an
additional CA, provide it with `--ca-cert` or `DAFAB_AUDIT_CA_CERT`; the audit
adds it to the public trust roots. Do not disable certificate verification or
copy private profile files into this repository.

## Generate a report

The report command requires an exact product list and an explicit output root:

```bash
export DAFAB_AUDIT_DB_ENV="$HOME/.config/dafab-audit/dafab-postgres.env"

dafab-audit-report \
  --product-list reports/inputs/water-products.json \
  --report-root reports \
  --use-case water-analysis \
  --profile dafab_skim \
  --artifact-base-url https://media.githubusercontent.com/media/DaFab-AI-eu/dafab-audit/main/reports \
  --workers 4
```

Use `--processing-evidence` only for captured, validated workflow evidence that
distinguishes `skipped-no-publication` from products still awaiting a
publication. Do not add private workflow logs or credentials to the report.

The command checkpoints the report while scanning. A successful run must finish
with exit status zero and an empty `reports/scan-errors.json`. Review the status
counts, storage budget, generated links, and changed files before publication.

`--artifact-base-url` may also be set with
`DAFAB_AUDIT_ARTIFACT_BASE_URL`. Omit it when generating a fully local report
with relative links.

To rebuild only the indexes from existing validated states and canonical skip
evidence, without opening database, catalog, or asset connections, run:

```bash
dafab-audit-report \
  --product-list reports/inputs/water-products.json \
  --processing-evidence reports/evidence/processing-skips.json \
  --report-root reports \
  --use-case water-analysis \
  --artifact-base-url https://media.githubusercontent.com/media/DaFab-AI-eu/dafab-audit/main/reports \
  --reindex-only
```

## Report layout

```text
reports/
  README.md
  index.html
  scan-errors.json
  storage-budget.json
  inputs/
    water-products.json
  evidence/
    processing-skips.json
  water_analysis/
    products/<product-id>/
      metadata.json
      report-state.json
      collage-hd.png
```

`metadata.json` and `report-state.json` are reproducible audit evidence.
`collage-hd.png` is a generated visualization tracked by Git LFS. Do not store
credentials, signed URLs, database environment files, DaFab profiles, or raw
operational logs anywhere below `reports/`.

## Compare generated asset runs

Use the comparison command on two local run directories:

```bash
dafab-audit-compare /path/to/baseline /path/to/candidate \
  --output-dir /path/to/comparison
```

It writes JSON and HTML comparison results and exits nonzero when compared
assets differ.

## Publication checks

Before publishing a report snapshot:

1. Run the test suite.
2. Confirm `scan-errors.json` is empty and every row has an expected terminal
   status.
3. Verify report row and unique product counts against the input list.
4. Confirm every available product state and collage is present and valid.
5. Check the storage budget and repository free space.
6. Secret-scan tracked files and Git history.
7. Verify Git LFS objects are available after upload using representative public
   image URLs.
