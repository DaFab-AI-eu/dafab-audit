# DaFab Audit

Every night, DaFab Audit reads the whole DaFab catalogue through its public API and
publishes a static page answering one question, is what the catalogue publishes valid,
complete and intact. The page covers every scope, collection and processing version,
the three public collections of `dafab` and the auxiliary scopes `hand`, `worldcover`
and `gfm`.

The page is published by GitHub Pages from this repository's nightly workflow.

For every item the audit checks three things.

- **Validity.** The stored STAC document validates against the schemas shipped with
  [`dafab-client`](https://pypi.org/project/dafab-client/); auxiliary items, which have
  no schema, are checked structurally.
- **Asset completeness.** Every asset the document declares is attached to the item in
  the catalogue and has an available replica in storage, and no attached file is
  undeclared. This inventory covers every changed item, a random sample and a rotating
  share, so that each item is re-inventoried at least weekly.
- **Storage integrity.** Changed and sampled items are downloaded and every file is
  compared, size and adler32, with what the catalogue recorded at publication. A full
  pass over everything can be requested by hand.

It also states coverage, how many Sentinel-2 source images have a product per use case
and version. Why an image has no product is not the audit's question; the processing
platform holds that answer. Browsing items and previews is the discovery site's job, so
the audit stores neither documents nor images, only its findings.

## How it runs

The workflow [`nightly.yml`](.github/workflows/nightly.yml) runs at 02:30 UTC and on
demand. One job per collection or scope audits its unit in parallel, capped to protect
the production API, and a final job merges the unit files, appends one record to the
history and deploys the site. Nothing is committed; the site is replaced on every run.
The job uses the read profile shipped with the client and holds no secret of any kind,
no database access, no storage credentials, no tokens.

Run the same audit locally, for one unit, against a scratch directory.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/dafab-audit-nightly collect --unit smart_agriculture --limit 200 --output /tmp/units
.venv/bin/dafab-audit-nightly publish --units /tmp/units --template site --output /tmp/_site
python3 -m http.server --directory /tmp/_site 8000
```

Without `--previous`, the collector fetches last night's state of the unit from the
live site to tell changed items from unchanged ones. `--inventory all` and
`--verify all` request the complete passes.

## Repository layout

```text
src/dafab_audit/nightly.py   Nightly collector and site publisher (API only)
site/                        Static page template, rendered client-side from JSON
.github/workflows/           Nightly workflow with Pages deployment
src/dafab_audit/             Operator tools: report, compare, holistic, health
scripts/                     Local operator wrappers
tests/                       Deterministic tests
docs/                        Operating procedures for the operator tools
pystac/                      Standalone PySTAC interoperability demonstration
```

The site's data files are `data/summary.json` (the matrix, coverage and anomalies),
`data/history.json` (one record per night) and `data/units/<unit>.json` (per-item
findings, used the next night for change detection).

## Operator tools

`dafab-audit-report`, `dafab-audit-compare`, `dafab-audit-holistic` and
`dafab-audit-health` remain available for operators. The report renders per-product
collages, and the holistic and health checks cross-check the database and the object
store; they need private configuration and are documented in
[`docs/operations.md`](docs/operations.md).

## Development

```bash
.venv/bin/python -m pytest
```

## Secrets

Nothing in this repository or its workflow needs a secret. Operator tools take their
database environment file with `--db-env` or `DAFAB_AUDIT_DB_ENV` and a profile
directory with `--profile-dir` or `DAFAB_PROFILE_DIR`. Never commit credentials,
profiles, CA material, tokens or connection notes.
