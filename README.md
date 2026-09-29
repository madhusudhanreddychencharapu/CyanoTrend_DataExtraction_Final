# CyanoTrend Final DataExtraction

Independent Python CLI backend for global Sentinel-3A/3B OLCI EFR lake processing.
The reference notebook supplies the scientific behavior; the package never opens
or executes a notebook at runtime. No Gradio, HTML maps or frontend are included.

## Agreed behavior

- HydroLAKES `Lake_area >= 1.3` km²; original `Pour_lat` / `Pour_long` preserved.
- User-selected inclusive dates, countries and optional first-level states/regions.
- Notebook adaptive windows: 3 km margin, at most six windows, whole-scene fallback at 35% coverage or dateline/other notebook fallback cases. One scene ID is shared across overlapping plans with the same loaded HydroLAKES universe.
- Five independent scene subprocesses by default; three attempts total.
- Persistent queue: new saved plans are picked up while the service is running.
- The notebook three-file scene ZIP: `S3_OLCI_<id>_ALL_SCIENCE_300m_CF.nc`,
  `S3_OLCI_<id>_ALL_INDEX_STATS.csv`, and `S3_OLCI_<id>_METADATA.json`.
- Exactly the notebook final export's 46 column names/order, followed by
  `state_name`, `state_id`, `latitude`, `longitude`: 50 columns in scene/master/backup.
- Native window NetCDFs are retained next to the ZIP for verification, as in the notebook.
- `--lake-region WORLD` (notebook default) or `USA` controls loaded HydroLAKES, independently of the planning state. USA uses the notebook Census polygon intersection, not a Country-column filter.
- One coordinator publishes the master CSV. The backup is the **previous successful
  master version**, including the header-only master before the first scene.
- Successful scratch is removed after publication. Failed scratch remains for inspection/retry, matching notebook `KEEP_FAILED_SCRATCH=True`. Reference datasets are retained once.

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
    world/<year>/<month>/<scene_id>_<config_hash>_native/*.nc
```

The CLI accepts alternate output, reference, scratch and OCSSW paths. There are
no per-scene configuration exports or `target_hydrolakes.gpkg` files.
Small hidden locks/journals support operational recovery. Logs and the ledger
are operational records, not stored copies of intermediate scientific files.

**Validation boundary:** implementation and local tests are not proof of a
successful authenticated HPC run or numerical agreement on real satellite data.
A same-scene notebook comparison is still required before scaling production.

## Upgrading the earlier full-scene CLI

Use a new output directory for the corrected implementation. Old plans are bound
to old source fingerprints; old 46-column masters cannot be mixed with the new
50-column final-export schema. Existing results are not rewritten or relabelled.

The supplied 791-lake Colab run has configuration hash `58f41b2c1318eaf1`.
The notebook hash is reproduced by area 1.3, region USA, and adaptive windows.
WORLD has a different population/hash and need not produce 791 lake rows.
For that reference comparison select `--lake-region USA`; planning `--country USA`
alone is NOT the HydroLAKES population selection.
