# Backend architecture

This is a modular Python application with a thin `argparse` CLI. There is no
web framework or scheduler dependency: this machine runs direct subprocesses,
not Slurm jobs. All five workers use the Python environment that starts the CLI.

## 1. Reference data — `references.py`

Preparation downloads the original HydroLAKES polygon ZIP and the notebook's
geoBoundaries gbOpen ADM0/ADM1 sources. Downloads are cached, checksummed and
frozen by a small manifest. The source shapefile is read directly inside its ZIP;
no filtered GeoPackage or lake geometry copy is produced.

The area filter uses the supplied `Lake_area` attribute. Lake geometry is used
for pixel membership and planning intersections. The original `Pour_lat` and
`Pour_long` attributes become the new CSV coordinates. These are pour points,
not newly calculated centroids. State name and ID come from `shapeName` and
`shapeID` in the ADM1 file that intersects that original point. If zero or
multiple boundaries match, state fields are blank and scene metadata identifies
the unmatched/ambiguous lake IDs. The software does not invent a nearest state.

ADM1 is a frozen contemporary boundary dataset, not a historical boundary
reconstruction for each observation year. Its source year and identifier are
preserved in the shared manifest. A new boundary release needs a separate
reference/output workspace; automatic data refresh is intentionally absent.

## 2. Plans — `planning.py`

An inclusive custom date range is searched with the notebook catalogue cap. Country
and state polygons select eligible planning lakes. With the default
`--scene-rule lake-complete`, a scene is planned when its footprint touches any of
those lakes, even where the scene lies outside the region (for example a scene
seeing only the Wisconsin side of Lake Michigan during a Michigan plan), so every
scene on a lake's daily checklist is planned by the same run. `--scene-rule state`
keeps the notebook rule: the footprint must intersect the administrative geometry
AND those lakes. Only S3A and S3B OLCI EFR products are accepted. The saved
plan retains discovery evidence and selection parameters (including `scene_rule`)
so the queue can resume.

Every plan requires a country and state/province ID. The selected full state
name is saved with each scene as `output_region`. The first plan to insert a
scene into the ledger owns its output location. Later plans retain their own
membership records but reuse the original scene and its folder.

A processing worker receives all eligible lakes in the loaded WORLD/USA universe intersecting the scene,
not just lakes inside the requesting administrative region. This is the notebook's
scene-level deduplication behavior. Region associations are many-to-many: a scene
may appear in several region progress counts, but in only one global scene count.
`--max-scenes` matches notebook max_products: raw catalogue products before regional filtering; it is not concurrency.

## 3. Operational state — `ledger.py`

SQLite stores scene identities, statuses, attempts, plan membership, region
associations and timings. A primary key on scene UUID deduplicates overlapping
plans. Atomic transactions claim work. Each batch permits three attempts, with
retry delays of 60 then 120 seconds. `retry-failed` atomically requeues all
terminal failures and sets `retry_start_attempt` to their lifetime attempt count.
Claims allow three additional attempts relative to that offset. Attempt/log
numbers are never reset; a timing record preserves the prior error. A successful
completed scene is never reset by a later plan or by `retry-failed`.

The ledger is not a second scientific statistics table. Final scientific records
are in scene CSVs and the master. CLI reporting avoids asking users to inspect
SQLite manually.

## 4. Parallel coordinator — `service.py`

One coordinator holds an OS lock for an output workspace and starts up to five
independent Python worker processes. Workers use scene locks and attempt identities;
late/stale workers cannot run a new attempt. Per-scene scratch, ancillary databases,
ancillary downloads and temporary S3 keys prevent cross-worker interference.

The service waits when idle, imports newly saved plans and continues after failed
scenes. `--once` drains available work/retries and exits. SIGINT/SIGTERM stops new
claims and lets active jobs finish. On Linux, parent-death signals stop owned
workers/OCSSW commands when their parent disappears. Unexpected coordinator errors
terminate its workers; the next run reconciles ready ZIPs or abandoned attempts.

## 5. Scientific worker — `worker.py`, `core/`, `execution.py`

The sequence is reference lookup, staging, getanc, full-swath L2Gen, native lake
pixel extraction, statistics and packaging. Runtime installation is never attempted.
OCSSW's sourced environment is passed to its subprocesses, not globally applied to
Python's GIS stack. Full native-pixel QA/statistics follow the notebook's final
functions; the CLI exposes no science-tuning switches.

A scene without native observations completes without a fabricated share ZIP.
The final share exporter needs native observations, as in the notebook. Cloud-masked
native observations still yield lake rows and diagnostic pixel counts.
The worker applies the notebook adaptive window planner and runs L2Gen sequentially
for each window after a single scene download. Export deduplicates overlapping
window observations using six-decimal coordinates and the lake ID.

## 6. Publication — `publication.py`

Workers build pending ZIPs on the output filesystem. The coordinator checks exact
members, ZIP CRC, member hashes, identity, schema and duplicate lake rows. It moves
the archive into `world/<country>/<planning state>/YYYY/MM/DD/UUID.zip`,
using the original scene record from the ledger. It constructs the new master, writes a
recovery journal and replaces master/backup using filesystem operations.

The prior master is hard-linked temporarily before replacement, so the backup
preserves the prior committed content without first copying it. A new master is
written as a stream and atomically replaces the old path. A journal bridges these
filesystem changes with SQLite's completion commit. Recovery checks hashes and
does not rotate the backup twice or append rows twice. Scientific work is not
repeated merely because master publication was interrupted.

Final CSVs share the notebook ALL_INDEX_STATS schema plus the four requested
metadata fields. Native window NC files are listed with checksums in scene metadata
and retained beside the ZIP. The share ZIP contains the notebook mapped NetCDF,
CSV and metadata filenames. All ZIP members use the reference ZIP_DEFLATED policy.
The master rewrite still costs O(total master size) per publication.

## 7. Observability / future frontend — `cli.py`, `telemetry.py`

`status`, `scenes`, `scene` and `coverage` expose reports; global `--json` produces
machine-readable data. Frontend code can call `Ledger.summary()`, `Ledger.scene()`,
`Ledger.regions()` and planning functions directly, or wrap them in a later API.
No HTTP service or world map is included now.

Timing stages include reference loading, staging/download, getanc, L2Gen, native
extraction, statistics and packaging. Live downloaded bytes and average staging
MiB/s are exposed. The staging interval includes transfer/setup/extraction; it is
not a bandwidth benchmark. Per-attempt telemetry remains available after scratch
cleanup. Discovery counts cover saved plans, not undiscovered worldwide inventory.

## 8. Day stage — `daily/`, `cyanotrend daily`

Scene processing stays independent per scene. The day stage merges processed
scenes per local solar date and lake on one global grid for every country: NASA's
Level-3 integerized sinusoidal (ISIN) bins at 69120 rows (bins about 290 x 290 m,
equal in area to 0.005%). NASA CyAN is used only to check results, never as the
method.

*Scene bin file per scene* (`daily/grid.py`). After packaging, the worker bins the
scene's native lake pixels and writes `<scene>_<hash>_isin_grid.nc` beside the
ZIP and the native `<scene>_<window>_<hash>_lakepixels.nc` files. Binning is
area-weighted: each pixel is spread over its footprint (the parallelogram spanned by its
neighbouring pixels) and shared among the bins it overlaps in proportion to the exact
overlap area (Green's theorem on the footprint polygon; no sample points, no resampling).
The weights of one pixel add up to its ground area. A bin's value for each index (CI,
CI_cyano, NDCI, MPH, FAI) is the area-weighted mean of the valid pixels that
overlap it; invalid pixels do not dilute it, and a bin with no valid pixel is
"no data". The file also keeps each bin's footprint area, valid area per index
and the primary-retained/cloud-excluded flags. Bins are not a regular raster
(neighbouring rows do not line up), so the file lists bins: bin number (NASA
1-based numbering), row, column, bin-centre latitude/longitude and bin area.
Bin numbers depend only on latitude/longitude, so the same bin is the same
piece of ground in every scene, in any country, including across the 180
degree meridian. The file is not inside the ZIP, so archive checksums are
unchanged. If it is absent or unreadable, `daily` rebuilds it using only that
scene's native lake-pixel files in the day folder.

*Daily merge* (`daily/merge.py`). The date of a record is the lake's local solar date: scene
start (UTC) plus the lake's longitude / 15 hours (the pour-point longitude). Sentinel-3 passes
at about 10:00 local time, so there is one pass per lake per date everywhere, and near 150 E
the UTC date of the pass is the day before the solar date. For each solar date: the catalogue
gives the S3A/S3B scenes of the UTC days it can reach (one or two); a lake's checklist is the
scenes touching it whose solar date at the lake is the date (NR/NT versions of one
acquisition count once). The ledger ticks the list. Complete lakes are merged;
lakes whose missing scenes all failed permanently are merged and flagged
`partial`; lakes still waiting get no row and are listed in the manifest. A lake's
bins are the ISIN bins whose centres lie inside its polygon; each index keeps
the highest scene value of each bin (the maximum over the lake's scenes of the
scenes' area-weighted means) and is "no valid data" only when no scene saw it
validly. The number of scenes with a valid view of each bin is stored
(`scenes_valid` in the daily NetCDF). `Lake_cells` and the other `*_cells`
columns count bins. Calculated rows/cells are
partitioned by country and state for every country:
`<output>/daily/<country>/<state>/YYYY/MM/DD/YYYY-MM-DD_lakes.csv`, with
`YYYY-MM-DD_manifest.json` and `YYYY-MM-DD_grid.nc` beside it. A lake that borders several
states or countries (Lake Michigan, Lake Erie) has exactly one record per date, under the
region whose plan processed it first (`daily/regions.py`): (1) the earliest-created plan whose
country/state touches the lake and that listed one of its scenes; (2) otherwise the state of
the HydroLAKES pour point, when unambiguous; (3) otherwise the region of the first plan that
listed the scene. A scene is stored once, in the first plan's region, so a later plan for a
neighbouring region (Chicago after Michigan) reuses the processed scene and adds no second
record. The CSV retains all touching states (`states`, `state_ids`, any country) in its
metadata. Each manifest filters its checklist and waiting lakes by that region, records the
basis of each lake's assignment and labels shared warnings as date-wide. No numerical merge
rule changes with partitioning. The NASA CyAN comparison is by UTC date, so it is filled only
for lakes whose passes are all on the same UTC date as the solar date (all of the USA
mainland); otherwise it is blank.

*Daily publication* (`daily/publication.py`). Under the existing daily writer
lock, all state products for one date are staged with a rebuilt
`daily/master_merged_daily.csv`. Old rows for that date are replaced; other dates
are retained, with unique `(date_solar, Hylak_id)` keys. A hard-linked snapshot
preserves the prior master for `daily/master_merged_daily_backup.csv`. A journal
records file hashes and replacements, allowing recovery without rotating the
backup twice. Obsolete files for the rerun date in the new layout are removed;
legacy-layout outputs are left untouched. Readers should wait for publication
to finish: individual files are replaced atomically, not the whole directory
tree at once. The stage never processes or queues scenes.

The daily lake universe is every lake in the workspace's HydroLAKES universe that a processed
scene touches (the same lakes the worker extracts). The CONUS Albers grid has been removed in
favour of ISIN bins. Fresh workspaces are required for the new layout; existing outputs are
not migrated or deleted.

## Storage boundary

Only final science ZIPs, native lake-pixel files, per-scene ISIN bin files,
daily outputs, master/backup, saved plans, reference data and operational
records persist. No HTML, gridded map export, raw satellite archive, full L2 file,
per-scene boundaries or `target_hydrolakes.gpkg` is retained after successful
publication. Pending products survive publication failure to permit recovery.

The daily CSV now includes independent CI, CI_cyano, NDCI, MPH and FAI statistics
of daily bin values (area-weighted within a scene, maximum across scenes), cloud-occurrence diagnostics, and one original NASA detection count
per lake/date. NASA counts are not summed across scenes; missing coverage leaves
a blank count. Detailed NASA provenance is retained in daily manifests.
See [daily columns](DAILY_COLUMNS.md) for the full schema and populations.
