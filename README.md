# CyanoTrend Final DataExtraction

Independent Python CLI backend for global Sentinel-3A/3B OLCI EFR lake processing.
The reference notebook supplies the scientific behavior; the package never opens
or executes a notebook at runtime. No Gradio, HTML maps or frontend are included.

## Agreed behavior

- HydroLAKES `Lake_area >= 1.3` km²; original `Pour_lat` / `Pour_long` preserved.
- User-selected inclusive dates, countries and optional first-level states/regions.
- Full-scene processing; one scene ID is shared across overlapping plans.
- Five independent scene subprocesses by default; three attempts total.
- Persistent queue: new saved plans are picked up while the service is running.
- One ZIP per successfully processed scene: native `lakepixels.nc`,
  `statistics.csv` and `metadata.json`.
- The sample CSV's 42 columns plus `state_name`, `state_id`, `latitude`, `longitude`.
  `window_id` stays `FULL` for schema compatibility. There is no window planner.
- One coordinator publishes the master CSV. The backup is the **previous successful
  master version**, including the header-only master before the first scene.
- Temporary L1, L2, ancillary and parameter files are removed after publication
  or a failed attempt. Reference source datasets are retained once for reuse.

## Read first

1. [Run guide](docs/RUNNING.md): Linux, the existing `s3olci-batch` environment,
   explained commands, preparation, plans, five workers and progress queries.
2. [Architecture](docs/ARCHITECTURE.md): one responsibility at a time, including
   duplicate prevention and crash recovery.
3. [Science and parity](docs/SCIENCE_PARITY.md): authoritative cells, unchanged
   numerical functions, explicit operational changes and validation boundary.
4. [Validation](docs/VALIDATION.md): exactly what was checked locally and what
   remains to be checked on the HPC.

```text
CyanoTrend_Final_DataExtraction/
  reference_data/               # downloaded once; not copied into scene ZIPs
  .scratch/                     # temporary, isolated by scene UUID
  Output Data/
    master.csv
    master_backup.csv
    operations.sqlite3          # accessible through CLI/JSON reports
    plans/<plan_id>.json
    logs/<scene_id>_attempt<N>.log
    world/<year>/<month>/<scene_id>.zip
```

The CLI accepts alternate output, reference, scratch and OCSSW paths. There are
no per-scene configuration exports or `target_hydrolakes.gpkg` files.
Small hidden locks/journals support operational recovery. Logs and the ledger
are operational records, not stored copies of intermediate scientific files.

**Validation boundary:** implementation and local tests are not proof of a
successful authenticated HPC run or numerical agreement on real satellite data.
A same-scene notebook comparison is still required before scaling production.
