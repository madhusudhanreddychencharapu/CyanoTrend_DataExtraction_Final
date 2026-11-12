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
scenes per UTC date and lake, the way NASA CyAN builds its daily product.

*NASA grid file per scene* (`daily/grid.py`). After packaging, the worker places
the scene's native lake pixels on NASA CyAN's CONUS grid and writes
`<scene>_<hash>_nasa_grid.nc` beside the ZIP and the native
`<scene>_<window>_<hash>_lakepixels.nc` files. The lattice is
NASA's: EPSG:5070, 300 m cells, edges taken from NASA tile 7_5
(upper-left 1,034,402.95 m, 914,826.27 m). Each pixel is spread over its footprint
(spanned by its neighbouring pixels). The daily stage now uses the independent
maximum valid overlapping value for each index. Prior CI_cyano area/nearest
fields remain for reference. This is the user-selected maximum rule, not a claim
of reproducing NASA within-scene interpolation. The file is not inside the ZIP, so
archive checksums are unchanged. If a grid file is absent or unreadable, `daily`
rebuilds it using only that scene's native lake-pixel files in the day folder.
Other scenes' files and NASA grid files are excluded from those inputs.

*Daily merge* (`daily/merge.py`). For each date: the catalogue gives every S3A/S3B
scene; a lake's checklist is the scenes touching it (NR/NT versions of one
acquisition count once). The ledger ticks the list. Complete lakes are merged;
lakes whose missing scenes all failed permanently are merged and flagged
`partial`; lakes still waiting get no row and are listed in the manifest. A lake's
cells are the NASA cells whose centres lie inside its polygon (the NASA count
rule); each index keeps its highest valid value over the lake's scenes and is
"no valid data" only when no scene saw it validly. Calculated rows/cells are
partitioned by each lake's existing HydroLAKES pour-point state assignment:
`<output>/daily/USA/<state>/YYYY/MM/DD/YYYY-MM-DD_lakes.csv`, with
`YYYY-MM-DD_manifest.json` and `YYYY-MM-DD_grid.nc` beside it. The CSV retains
all touching states in its metadata; the lake's results occur in only one folder.
Each manifest filters its checklist and waiting lakes by that state, and labels
shared warnings as date-wide. No numerical merge rule changes with partitioning.

*Daily publication* (`daily/publication.py`). Under the existing daily writer
lock, all state products for one date are staged with a rebuilt
`daily/master_merged_daily.csv`. Old rows for that date are replaced; other dates
are retained, with unique `(date_utc, Hylak_id)` keys. A hard-linked snapshot
preserves the prior master for `daily/master_merged_daily_backup.csv`. A journal
records file hashes and replacements, allowing recovery without rotating the
backup twice. Obsolete files for the rerun date in the new layout are removed;
legacy-layout outputs are left untouched. Readers should wait for publication
to finish: individual files are replaced atomically, not the whole directory
tree at once. The stage never processes or queues scenes.

Unmatched/ambiguous and foreign-country pour-point folder policies are deferred
in the project Word note. These unsupported assignments currently raise a
date-level error before publication; no country/state fallback is implemented.
The existing lake selection and CONUS grid remain unchanged. Fresh workspaces
are required for the new layout; existing outputs are not migrated or deleted.

## Storage boundary

Only final science ZIPs, native lake-pixel files, per-scene NASA grid files,
daily outputs, master/backup, saved plans, reference data and operational
records persist. No HTML, gridded map export, raw satellite archive, full L2 file,
per-scene boundaries or `target_hydrolakes.gpkg` is retained after successful
publication. Pending products survive publication failure to permit recovery.

The daily CSV now includes independent maximum CI, CI_cyano, NDCI, MPH and FAI
statistics, cloud-occurrence diagnostics, and one original NASA detection count
per lake/date. NASA counts are not summed across scenes; missing coverage leaves
a blank count. Detailed NASA provenance is retained in daily manifests.
See [daily columns](DAILY_COLUMNS.md) for the full schema and populations.
