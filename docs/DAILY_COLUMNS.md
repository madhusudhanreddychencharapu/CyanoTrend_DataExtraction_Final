# Daily lake CSV: independent maximum indices

This contract applies to date-named lake CSVs, `master_merged_daily.csv`, and its
backup. Columns 1–16 retain their names, order and metadata roles. Column 10,
`within_pass_method`, now records the `area` method (area-weighted binning).
There are 58 columns. Native scene formulas, masks and statistics are unchanged.

## Calculation order

1. Read existing native index values and validity masks. Do not recompute
   reflectance formulas, cloud masks or thresholds.
2. Use the fixed global ISIN bin grid (69120 rows, about 290 m, equal-area bins)
   and exact native-pixel footprints. Each pixel is shared among the bins it
   overlaps in proportion to the exact overlap area. A bin's value for each index is the
   area-weighted mean of the valid overlapping pixels; invalid pixels do not
   dilute it. Negative valid NDCI/MPH/FAI values are not replaced by zero.
3. Across all selected acquisitions of the solar date, retain each bin's highest
   scene value for each index, independently. The existing acquisition-version selection, lake
   checklist, complete/partial/waiting rules and lake-cell selection remain.
4. Calculate count, mean, median, p10, p90 and maximum from each lake's finite
   daily cell values. These are statistics of daily bin values (the maximum over
   scenes of the scene area-weighted means), not maxima of scene CSV statistics.
   A bin counts once in each lake's daily statistics. "Cells" in column names
   means ISIN bins.

Footprint overlap is exact polygon overlap (Green's theorem); the weights of a pixel add up
to its ground area. `date_solar` is the lake's local solar date: scene start (UTC) plus the lake
longitude / 15 hours. The earlier within-scene maximum and nearest-pixel rules were removed.

## Columns

| Positions | Fields, in order |
| --- | --- |
| 1–16 | `date_solar`, `Hylak_id`, `Lake_name`, `Lake_area_km2`, `states`, `state_ids`, `latitude`, `longitude`, `day_status`, `within_pass_method`, `satellites`, `scenes_used`, `scene_ids`, `scene_names`, `failed_or_unavailable_scenes`, `Lake_cells` |
| 17 | `primary_retained_cells` |
| 18–20 | `Cloud_excluded_any_scene_cells`, `Cloud_excluded_with_valid_daily_CI_cells`, `Cloud_excluded_without_valid_daily_CI_cells` |
| 21–22 | `ci_input_valid_cells`, `ci_candidate_cells` |
| 23–27 | `CI_cyano_detectable_cells`, `Non_detectable_cells`, `No_valid_data_cells`, `Cells_valid_in_two_or_more_scenes`, `NASA_CI_Cyano_detectable_cells` |
| 28–33 | `CI_valid_cells`, `CI_mean`, `CI_median`, `CI_p10`, `CI_p90`, `CI_max` |
| 34–39 | `CI_cyano_valid_cells`, `CI_cyano_mean`, `CI_cyano_median`, `CI_cyano_p10`, `CI_cyano_p90`, `CI_cyano_max` |
| 40 | `CI_cyano_mean_detectable` |
| 41–46 | `NDCI_valid_cells`, `NDCI_mean`, `NDCI_median`, `NDCI_p10`, `NDCI_p90`, `NDCI_max` |
| 47–52 | `MPH_valid_cells`, `MPH_mean`, `MPH_median`, `MPH_p10`, `MPH_p90`, `MPH_max` |
| 53–58 | `FAI_valid_cells`, `FAI_mean`, `FAI_median`, `FAI_p10`, `FAI_p90`, `FAI_max` |

`native_lake_pixels` is intentionally omitted. Generic CLDICE sensitivity counts,
bloom-rescue counts and their ratio are not copied into the daily CSV. The
approved cloud diagnostics replace that group.

## Populations and missing values

- `primary_retained_cells`: at least one overlapping observation has the existing
  `valid_water_mask`. It need not have valid inputs for every index.
- `ci_input_valid_cells`: at least one valid CI-input observation, using the
  existing `ci_valid_mask` path. This includes valid non-detections.
- `ci_candidate_cells` and `CI_valid_cells`: bins with a finite CI candidate value.
- `CI_cyano_valid_cells` and its ordinary statistics include all valid daily
  CI-cyano values, including valid non-detections. The existing daily value rule
  uses CI when finite and SS665 is positive, otherwise zero for CI-valid water.
  This differs from the scene CSV's detection-only CI-cyano statistics.
- `CI_cyano_mean_detectable` includes only daily values at or above the existing
  daily detection limit, separately from the all-valid mean.
- NDCI, MPH and FAI each use their own existing native validity mask and finite
  stored values. Their contributing cells and scenes can differ.
- Empty local populations have count zero and blank statistics. Missing NASA
  measurements are blank; actual measured zero remains zero.
- `Non_detectable_cells + CI_cyano_detectable_cells + No_valid_data_cells`
  equals `Lake_cells`. `Cells_valid_in_two_or_more_scenes` refers to CI-input
  availability across selected acquisitions, not all catalogue versions.

## Cloud diagnostics

These counts use the stored `cloud_excluded_mask`, not generic CLDICE as a
substitute for actual exclusion. `Cloud_excluded_any_scene_cells` counts unique
lake cells touched by a cloud-excluded observation during the day. Its two
subdivisions distinguish cells with and without a valid daily CI observation.
A cell with no observation is not automatically called cloudy. Excluded and
valid observations may overlap within one scene or across different scenes.
The without-valid count does not prove cloud was the only cause of missing data.

The daily NetCDF lists the lake bins (bin number, row, column, bin-centre
latitude/longitude, bin area, lake id, number of scenes with a valid view) with
all five daily bin values and `primary_retained_any` and `cloud_excluded_any`
flags for inspection. NASA counts come from the original
NASA daily raster and remain independent of these local grids.

## Fresh output and provenance

Use the fresh output workspace already agreed. An existing 22-column daily master
is rejected rather than silently relabelled or padded. Old scene-grid schemas
can be rebuilt from complete native files; existing ZIPs and daily masters are
not migrated. Scene ZIPs contain one statistics CSV, one science NetCDF and
metadata JSON. NASA details are in that JSON and daily manifests; no separate
comparison CSV is packaged. NASA coverage summaries in state manifests are
labelled date-wide, while per-lake results are filtered to that state's lakes.

## Science NetCDF and rebuilding the columns

`<date>_science.nc` (see `daily/science.py`) carries the layers behind these columns, one cell per ISIN bin on lake
cells, so every count and statistic can be rebuilt from it at any time: `python -m cyanotrend reproduce <file>`.

| CSV column | From the science file |
|---|---|
| `Lake_cells` | cells with this `Hylak_id` |
| `primary_retained_cells` | `valid_water_mask` = 1 |
| `Cloud_excluded_any_scene_cells` | `cloud_excluded_mask` = 1 |
| `Cloud_excluded_with/without_valid_daily_CI_cells` | the same, split by `DN` != 255 / = 255 |
| `ci_input_valid_cells` | `DN` != 255 |
| `ci_candidate_cells`, `CI_valid_cells` | `CI` has a value |
| `CI_cyano_detectable_cells` | `DN` != 255 and `CI_cyano` >= the file's `CI_cyano_detection_limit` |
| `Non_detectable_cells` | `DN` != 255 and below the limit |
| `No_valid_data_cells` | `DN` = 255 |
| `Cells_valid_in_two_or_more_scenes` | `scenes_valid` >= 2 |
| `NASA_CI_Cyano_detectable_cells` | the lake table (`lake_NASA_CI_Cyano_detectable_cells`; -1 = blank) |
| `<index>_valid_cells/mean/median/p10/p90/max` | the finite values of that index layer |

Daily `rhos` bands are combined like the indices: the highest pass value per band, independently, so a bin seen by two
passes can mix them. Masks are set when any pass set them.
