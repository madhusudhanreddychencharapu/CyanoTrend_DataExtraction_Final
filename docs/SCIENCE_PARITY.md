# Scientific reference and validation boundary

The authority is the user-supplied notebook
`S3_OLCI_L2Gen_CyanoLake_GLOBAL_EFFICIENT_v2_6_8_NATIVE_FIRST_CYAN_COMPARISON (1).ipynb`.
Its SHA-256 and selected function inventory are in `reference.json`. The notebook
is not shipped or needed at runtime. The existing standalone project supplied
module-qualified versions that were checked against the notebook definitions.

## Effective functions

Notebook indices here are zero-based. Later definitions override earlier ones.

- Cell 30: effective CI/CIcyano spectral family.
- Cell 20: NDCI calculation.
- Cell 48: MPH and OLCI-adapted FAI/AFAI, nine-band production selection.
- Cell 57: bloom-aware native extraction, compact writer and compact statistics.
- Cell 44: native polygon membership and append helper.
- Cell 50: administrative planning selects scenes, but processing uses the
  canonical eligible-lake universe and reuses completed scenes globally.

`science_audit.json` records normalized syntax-tree comparisons. Fifteen selected
science/statistics/helper functions match the notebook after module qualifiers
and docstrings are normalized. The extraction function differs in these operational
ways: optional block/buffer arguments resolve fixed defaults on invocation;
non-FULL windows are rejected; window metadata and progress messages say FULL.
The numeric extraction loop, masks, spectral gates and statistics remain unchanged.

The primary mask is `bloom_aware_cyan`. CLDICE recovery requires the notebook's
CIcyano evidence, positive FAI/AFAI and rhos754 > rhos490, with LAND, HISATZEN and
NAVFAIL restrictions preserved. Strict and rescued counts remain separate.
The nine-band archive preserves the nominal 885 nm to L2Gen `rhos_884` mapping.
No gridded-export or comparison-UI science enters native statistics.

## Approved behavior changes

- Filter supplied lake area at >= 1.3 km².
- Always process the whole scene; keep CSV `window_id=FULL` for compatibility.
- Preserve all sample columns and append four administrative/coordinate columns.
- Store native compact NetCDF, not the notebook's optional gridded share export.
- Add five scene workers, saved-plan queue, three total attempts, safe publication,
  progress commands and prior-master backup.
- Download references once, isolate scratch per scene, omit dashboard/map artifacts.

The native coordinate arrays inside NetCDF remain per-pixel geolocation. The new
CSV lake coordinates are HydroLAKES pour-point attributes, not pixel coordinates.
They serve different purposes and neither is substituted for the other.

## Required HPC parity check

Use identical S3A/S3B scene identities, reference lakes and full-scene settings
with the notebook and this package. Compare native source row/column + lake ID,
reflectances, all QA masks, index arrays and each of the original 42 CSV columns.
Exclude expected path/version/metadata differences. Confirm both retained bloom
and heavily cloud-masked examples. Compare floating values with an explicit
agreed tolerance and require exact match for IDs and masks. Do not use different
window scopes or gridded statistics as the reference comparison.

Synthetic masks and syntax-tree agreement are useful evidence but cannot verify
NASA installation, ancillary retrieval, actual L2Gen output or full numerical
parity on satellite observations. No such live HPC run was performed locally.

## Data/API references

- [HydroLAKES product and attribution](https://www.hydrosheds.org/products/hydrolakes)
- [HydroLAKES field definitions](https://data.hydrosheds.org/file/technical-documentation/HydroLAKES_TechDoc_v10.pdf)
- [geoBoundaries API and licensing](https://www.geoboundaries.org/api.html)
- [Copernicus Data Space OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html)

Source licenses/versions are retained in the shared reference manifest. HydroLAKES
and geoBoundaries require appropriate dataset attribution when results are shared.
