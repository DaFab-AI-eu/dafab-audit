# DaFab STAC interoperability

This standalone directory documents how PySTAC represents, navigates, and
validates the live Rucio-backed DaFab STAC catalog. It depends only on PySTAC
and the public STAC endpoint.

## Contents

| File | Purpose |
|---|---|
| [`pystac-navigation.ipynb`](pystac-navigation.ipynb) | Executable notebook that starts from the live DaFab root and progressively discovers the graph with PySTAC |
| [`pystac-navigation.html`](pystac-navigation.html) | Browser-ready rendering with captured results |
| [`structure.md`](structure.md) | D3.2 Catalog, Collection, Item, facet, provenance, and Rucio mapping |
| [`compatibility.md`](compatibility.md) | STAC and PySTAC compatibility assessment with current capability boundaries |

The notebook uses the public endpoint at <https://dafab.cern.ch/stac>. It does
not depend on a local DaFab checkout or substitute fixture data. The HTML
retains results captured from that service when the notebook was executed.

## Rucio relationship

The DaFab service exposes Rucio-backed metadata as STAC JSON. PySTAC reads,
validates, and navigates that representation. DaFab service and Rucio APIs
remain responsible for enhanced filtering, publication, metadata mutation,
file transfer, and replica management.

This separation lets standard STAC tools navigate DaFab while preserving the
Rucio-specific capabilities that power DaFab workflows.

## Running the notebook

Use any Python environment with Jupyter and PySTAC installed.

```bash
python -m pip install jupyter pystac
jupyter notebook pystac-navigation.ipynb
```

The notebook contains direct defaults for the public service. A custom CA bundle
is needed only when the local Python certificate store cannot validate the
service certificate chain.
