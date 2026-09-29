# Audit operations

The nightly audit described in the [README](../README.md) needs none of the private
configuration below. This page covers the operator tools, the per-product report with
collages, the comparison of two report runs, and the holistic and health checks.

## Prerequisites

- Python 3.10 or newer
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
  --product-list <product-list.json> \
  --report-root <report-root> \
  --use-case both \
  --profile dafab_skim \
  --workers 4
```

Use `--processing-evidence` only for captured, validated workflow evidence that
distinguishes `skipped-no-publication` from products still awaiting a
publication. Do not add private workflow logs or credentials to the report.

The command checkpoints the report while scanning. A successful run must finish
with exit status zero and an empty `<report-root>/scan-errors.json`. Review the status
counts, storage budget, generated links, and changed files before publication.

`--metadata-base-url` selects the human-readable metadata viewer and may also
be set with `DAFAB_AUDIT_METADATA_BASE_URL`. `--artifact-base-url` selects the
collage location and may also be set with `DAFAB_AUDIT_ARTIFACT_BASE_URL`.
For compatibility, metadata uses the artifact base when no metadata base is
set. Omit both options to generate fully relative links.

## Run catalog and storage health checks

The holistic command performs the read-only database, resolver, and storage
audit. The health command can additionally exercise client reads or an explicit
write-and-cleanup probe:

```bash
dafab-audit-holistic --help
dafab-audit-health --help
```

For the configured local fast check, use
`scripts/run_local_dafab_health_check.sh`. By default, it reads the database DSN
from `~/.config/dafab-rucio-client/audit/rucio_health_audit.env`, the RSE account
from the adjacent `rucio_health_rse_account.json`, and client profiles from
`~/.config/dafab-rucio-client/profiles`. These paths can be overridden with the
environment variables documented in the script. Keep every credential file
outside this repository with user-only permissions.

To rebuild only the indexes from existing validated states and canonical skip
evidence, without opening database, catalog, or asset connections, run:

```bash
dafab-audit-report \
  --product-list <product-list.json> \
  --processing-evidence <processing-skips.json> \
  --report-root <report-root> \
  --use-case both \
  --reindex-only
```

## Report layout

```text
<report-root>/
  README.md
  index.html
  scan-errors.json
  storage-budget.json
  <use-case>/
    products/<product-id>/
      metadata.json
      report-state.json
      collage-hd.png
```

`metadata.json` and `report-state.json` are reproducible audit evidence and
`collage-hd.png` a generated visualization. Reports are written to a scratch root and
are no longer committed to this repository. Do not store credentials, signed URLs,
database environment files, DaFab profiles, or raw operational logs in a report.

## Compare generated asset runs

Use the comparison command on two local run directories:

```bash
dafab-audit-compare /path/to/baseline /path/to/candidate \
  --output-dir /path/to/comparison
```

It writes JSON and HTML comparison results and exits nonzero when compared
assets differ.

## Sharing a report

Report snapshots are not committed to this repository any more; the nightly site is the
published audit. Before sharing a report generated with the operator tool, confirm
`scan-errors.json` is empty, that row and unique product counts match the input list,
and that no credential, signed URL or operational log is present below the report root.
