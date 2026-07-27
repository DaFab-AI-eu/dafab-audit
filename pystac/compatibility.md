# DaFab STAC and PySTAC compatibility

## Purpose

This document records how the DaFab D3.2 structure aligns with STAC 1.1.0 and
how PySTAC behaves against the live DaFab service.

The accompanying [notebook](pystac-navigation.ipynb) starts only from
<https://dafab.cern.ch/stac>. It discovers the graph through STAC links and
retains the live results used for this assessment.

## STAC compatibility

| Area | Status | Assessment |
|---|---|---|
| Root Catalog | Compatible | The root is a standard Catalog with `root`, `self`, and `child` links. |
| Collections | Compatible | Collection objects include the required identification, license, extent, and link fields. |
| Facet Catalogs | Compatible | STAC permits application-specific Catalog hierarchies. Facet indexes and values use ordinary `child` and `item` links. |
| Items | Compatible | Items are GeoJSON Features with the required STAC fields and one Collection identifier. |
| Navigation links | Compatible | DaFab uses standard `root`, `self`, `parent`, `child`, `item`, and `collection` relations. |
| Provenance links | Compatible | Derived Items use `derived_from`. Source Items can use the generic `related` relation. |
| Assets | Compatible | Product files are represented by ordinary STAC Asset objects. |
| Processing extension | Compatible | Derived Items declare the Processing extension and retain its namespaced properties. |
| DaFab properties | Compatible | STAC permits additional namespaced Item properties such as `dafab:water-analysis`. |

DaFab publication rules form a stricter application profile above core STAC.
For example, DaFab can require particular assets, provenance links, or facet
placement even when a more permissive object would pass the generic STAC schema.

Facet Catalogs do not require a new STAC object type. They are normal Catalog
objects whose links express DaFab's navigation indexes.

## PySTAC compatibility

The notebook demonstrates the following capabilities against the live service.

- Read the root Catalog over HTTP.
- Discover Collections from root `child` links.
- Walk nested facet Catalogs.
- Discover and fetch Items from `item` links.
- Navigate from an Item through its declared outbound links.
- Inspect geometry, properties, provenance, and assets.
- Preserve DaFab namespaced properties and custom link relations.
- Validate core STAC fields and declared extension schemas.
- Apply lightweight checks for the stricter DaFab profile.
- Serialize parsed objects and verify round-trip preservation.
- Recover reverse facet membership by scanning Catalog `item` links.

PySTAC models this graph with its existing `Catalog`, `Collection`, `Item`,
`Link`, and `Asset` classes. No DaFab-specific object class is required.

## Directionality

DaFab facet membership can be many to many. One Item may appear in a basin
Catalog and an anomaly Catalog at the same time. These Catalogs are navigation
indexes rather than alternative canonical parents.

DaFab therefore records facet membership in the downward direction with Catalog
`item` links. Adding an Item `parent` or `collection` backlink for every facet
would misuse links that describe canonical hierarchy and Collection membership.
Reverse membership can be reconstructed from the root-driven tree or exposed by
a DaFab query endpoint.

## Capability boundaries

| Capability | Current boundary | Appropriate component |
|---|---|---|
| Spatial, temporal, and property filtering | PySTAC core navigates linked objects and does not provide service-side search. | DaFab enhanced filtering provides the Rucio-aware query behavior. |
| Standard STAC API Item Search | This requires a service endpoint and an API client rather than the PySTAC object model. | `pystac-client` can consume Item Search when such an endpoint is available. It is not required for link-based navigation. |
| Reverse Item-to-facet lookup from an Item alone | Items do not carry backlinks to every indexing Catalog. | Traverse the root graph or use a DaFab membership lookup. |
| Rucio DID and replica operations | PySTAC has no Rucio data-management model. | Use DaFab service and Rucio APIs. |
| Remote metadata writes and asset transfer | PySTAC serializes STAC objects but does not call DaFab mutation or transfer endpoints. | Use DaFab publication and transfer workflows. |
| DaFab publication policy | Generic STAC validation does not enforce DaFab-specific placement and provenance rules. | Use DaFab validators in addition to STAC validation. |
| Typed `dafab:*` accessors | PySTAC preserves custom properties without DaFab-specific helper classes. | Raw properties work now. A DaFab extension helper could add typed convenience methods. |
| Typed Processing accessors | Processing fields are preserved without a bundled typed helper in the demonstrated PySTAC package. | Raw extension properties work now. |
| Python certificate trust | HTTP reads depend on the Python certificate store. | Configure a trusted CA bundle when the local Python installation does not trust the service chain. |

These boundaries do not prevent standard Catalog navigation. They separate the
STAC object model from DaFab's Rucio-backed querying, publication, and transfer
operations.

## Validation coverage

The notebook performs three complementary checks.

| Check | What it establishes |
|---|---|
| Core schema validation | Required STAC Catalog, Collection, and Item fields have valid types and structure. |
| PySTAC full validation | Declared STAC extensions are validated when their schemas can be resolved. |
| DaFab profile checks | Expected Collections, source and derived provenance, facet placement, and product-specific requirements are checked. |

Round-trip checks then serialize each parsed object and confirm that identifiers,
links, assets, Collection values, and DaFab properties remain available.

## Conclusion

The DaFab D3.2 graph is representable with standard STAC objects and is
navigable with PySTAC. The live notebook provides executable evidence for
root-first and Item-first interaction. DaFab enhanced filtering remains the
appropriate interface for project-specific discovery over Rucio metadata.

## References

- [STAC specification 1.1.0](https://github.com/radiantearth/stac-spec/tree/v1.1.0)
- [STAC Catalog specification](https://github.com/radiantearth/stac-spec/blob/v1.1.0/catalog-spec/catalog-spec.md)
- [STAC Collection specification](https://github.com/radiantearth/stac-spec/blob/v1.1.0/collection-spec/collection-spec.md)
- [STAC Item specification](https://github.com/radiantearth/stac-spec/blob/v1.1.0/item-spec/item-spec.md)
- [STAC Processing extension 1.2.0](https://github.com/stac-extensions/processing/tree/v1.2.0)
- [PySTAC documentation](https://pystac.readthedocs.io/)
