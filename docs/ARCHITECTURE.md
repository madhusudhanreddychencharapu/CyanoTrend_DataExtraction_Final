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

An inclusive custom date range is searched in calendar-month shards. Country
and state polygons select eligible planning lakes, and scene footprints must
intersect those lakes. Only S3A and S3B OLCI EFR products are accepted. The saved
plan retains discovery evidence and selection parameters so the queue can resume.

A processing worker receives all eligible global lakes intersecting the scene,
not just lakes inside the requesting administrative region. This is the notebook's
scene-level deduplication behavior. Region associations are many-to-many: a scene
may appear in several region progress counts, but in only one global scene count.
`--max-scenes` limits eligible scene IDs after lake filtering; it is not concurrency.

## 3. Operational state — `ledger.py`

SQLite stores scene identities, statuses, attempts, plan membership, region
associations and timings. A primary key on scene UUID deduplicates overlapping
plans. Atomic transactions claim work. Three attempts means the initial attempt
plus at most two retries; retry delays are 60 then 120 seconds. A successful
completed scene is never automatically reset by a later plan.

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

A successful scene without geolocated native lake observations has an explicit
empty native NetCDF and header-only CSV, documented in metadata. Cloud-rejected
observations still retain the notebook's QA/count behavior. State assignment does
not alter pixel masks or index calculations.

## 6. Publication — `publication.py`

Workers build pending ZIPs on the output filesystem. The coordinator checks exact
members, ZIP CRC, member hashes, identity, schema and duplicate lake rows. It moves
the archive into `world/YYYY/MM/UUID.zip`, constructs the new master, writes a
recovery journal and replaces master/backup using filesystem operations.

The prior master is hard-linked temporarily before replacement, so the backup
preserves the prior committed content without first copying it. A new master is
written as a stream and atomically replaces the old path. A journal bridges these
filesystem changes with SQLite's completion commit. Recovery checks hashes and
does not rotate the backup twice or append rows twice. Scientific work is not
repeated merely because master publication was interrupted.

The existing `compact_netcdf` column contains a portable locator such as
`world/2024/08/UUID.zip::lakepixels.nc`. The text after `::` names the ZIP member;
it is not a promise that NetCDF readers accept the locator as a filesystem path.
Extract that member when opening it with a library that needs a normal file.

The master rewrite costs O(total master size) for each publication. That cost
follows the requested CSV/previous-version contract; it should be measured at
archive scale. NetCDF compression happens inside the NetCDF, so the ZIP stores
that member without redundant compression. CSV and JSON ZIP members are compressed.

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

## Storage boundary

Only final science ZIPs, master/backup, saved plans, reference data and operational
records persist. No HTML, gridded map export, raw satellite archive, full L2 file,
per-scene boundaries or `target_hydrolakes.gpkg` is retained after successful
publication. Pending products survive publication failure to permit recovery.
