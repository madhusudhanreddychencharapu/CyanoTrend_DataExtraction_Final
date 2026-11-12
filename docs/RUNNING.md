# Corrected notebook pipeline: use a fresh output directory

The CSV schema and scientific source fingerprint changed. Do not reuse the old
master/queue. Existing references may be reused. Every command must use the same
new output path; scratch must remain outside output. The prior outputs stay intact.

In the examples, `--output` selects the results/queue directory, and quotes keep
`Output Data Notebook Parity` together as one path containing spaces. This fresh
folder separates corrected results from the old schema. Repeat this option for
planning, running, status and verification; the reference cache can stay shared.

`--lake-region` selects the loaded HydroLAKES universe, independently of the planning
country/state. WORLD is the notebook UI default; USA uses the notebook Census
intersection. The supplied 791-lake reference has the USA/adaptive settings hash.
Use USA when reproducing that reference. Area remains >=1.3 km2.

The notebook's final share CSV names are now used in scene/master/backup. Native
window files remain beside the ZIP. Failed scratch remains available for inspection.

# Linux run guide

Use the existing `s3olci-batch` Conda environment. Do not create a new environment
or reinstall OCSSW. The reference root defaults to `~/cyanoTrend/software/ocssw`,
matching the directory you showed; preflight verifies its actual contents.

Commands below assume you have copied this new project to
`~/cyanoTrend/CyanoTrend_Final_DataExtraction` on Linux. That remote destination
is an instruction for where to place it, not a claim that it already exists there.
No remote connection, upload or package installation was performed by this task.

## 1. Enter the new project and existing environment

`cd` changes directory. `~` is your home directory. The remaining path names the
new project, outside the old `CyanoTrend_DataExtraction` folder.

```bash
cd ~/cyanoTrend/CyanoTrend_Final_DataExtraction
```

`conda` is the environment manager; `activate` selects an existing environment;
`s3olci-batch` is your approved environment name.

```bash
conda activate s3olci-batch
```

`python` uses that environment's interpreter. `-m pip` runs its package installer.
`install` installs this project. `--no-deps` prevents dependency changes.
`--no-build-isolation` uses installed build tools instead of downloading another
build environment. `-e` means editable installation; `.` means the current folder.
If this reports missing build tools, stop and review the missing package before
changing the environment. Required scientific packages are listed in `pyproject.toml`.

```bash
python -m pip install --no-deps --no-build-isolation -e .
```

Alternatively, no installation is necessary: `PYTHONPATH=src` tells Python where
the source package is; `python -m cyanotrend` runs its CLI module; `--help` lists
commands and arguments. Use this same prefix on later commands if not installed.

```bash
PYTHONPATH=src python -m cyanotrend --help
```

## 2. Choose physical storage paths

Defaults are relative to the current directory: `Output Data`, `reference_data`,
and `.scratch`. Keep running from the same project directory unless passing
absolute paths. Final output and scratch must be separate directory trees.

The following is an **example**, not a known existing `/data` destination.
Replace the marked paths before use. `--output` sets final/operational output;
`--scratch` sets temporary scene storage; `--references` locates the single
reference cache; `preflight` checks prerequisites without processing a scene.
Global options appear before the command, and must be repeated consistently
if you override defaults. Choose your writable locations based on disk capacity.

```text
cyanotrend --output /YOUR/FINAL/OUTPUT --scratch /YOUR/TEMP/STORAGE --references /YOUR/REFERENCE/CACHE preflight
```

Five simultaneous scene jobs need temporary disk space even though no raw
or L2 data is retained at completion. The supplied disk snapshot is not a capacity
estimate for ten years of final products; monitor output growth and free space.

## 3. Download and freeze reference data once

`cyanotrend` invokes the backend. `prepare` downloads HydroLAKES and available
global ADM0/ADM1 boundaries into the reference cache. Repeating it resumes
interrupted preparation or reuses the completed manifest; it does not duplicate
references in scene output. This is a substantial first-time download.

```bash
cyanotrend --output "Output Data Notebook Parity" prepare
```

`preflight` verifies package imports, frozen source checksums, OCSSW tools/sensor
data, Earthdata credential-file presence/permissions and free storage. It does
not expose credentials or authenticate a satellite download. Continue only when
it reports `ok: True`; resolve errors before starting the queue.

```bash
cyanotrend --output "Output Data Notebook Parity" preflight
```

NASA Earthdata credentials must already be in your own `~/.netrc` with permissions
0600. Keep credentials out of source files and Git. This project does not change
or print your existing credential files.

## 4. Inspect region names and IDs

`regions` lists country boundaries. Names and IDs are read from the frozen source.

```bash
cyanotrend --output "Output Data Notebook Parity" regions
```

`--country USA` selects the United States using its three-letter ISO code and
lists its first-level state names/IDs. Substitute your desired country code.

```bash
cyanotrend --output "Output Data Notebook Parity" regions --country USA
```

## 5. Save a plan

`plan` discovers and saves eligible scenes. `--start` and `--end` are inclusive
custom dates. `--country` requires a three-letter country code. `--state-id`
requires the exact state/province ID printed by `regions`; replace
`STATE_ID_FROM_REGIONS` with that value. Its full name is used in output folders.
`--max-scenes 5` caps this example at five catalogue results before regional
filtering; it does not set parallelism. Both S3A and S3B are included.
The dates are illustrative; replace them with your own.

```bash
cyanotrend --output "Output Data Notebook Parity" plan --start 2024-08-01 --end 2024-08-02 --country USA --state-id STATE_ID_FROM_REGIONS --max-scenes 5
```

Omit `--max-scenes` for complete discovery in the chosen interval:

```bash
cyanotrend --output "Output Data Notebook Parity" plan --start 2024-08-01 --end 2024-08-31 --country USA --state-id STATE_ID_FROM_REGIONS
```

Country and state/province are required for every plan. Plan additional states
separately, using their IDs. There is no built-in 2016–2026 date restriction.
Inspect the reported eligible-scene count. A plan with zero scenes contains no
work and is not a successful science run. If overlapping plans discover the same
scene, the first saved plan owns its output location. Later plans reuse it.

## 6. Run five scenes concurrently

`run` starts the persistent queue coordinator. `--workers 5` allows five concurrent
scenes. In an interactive terminal, missing CDSE credentials are requested with a
hidden password prompt. No username or password is written to a plan or config.
The command stays running while waiting for new plans. Use another terminal to
create additional plans against the same output/reference directories.

```bash
cyanotrend --output "Output Data Notebook Parity" run --workers 5
```

`--once` changes only idle behavior: after pending work and retries finish, exit.
It still runs up to five scenes and allows up to three attempts per scene's batch.

```bash
cyanotrend --output "Output Data Notebook Parity" run --workers 5 --once
```

Keep a persistent terminal session for long runs, or use your existing Linux
service/session arrangement. For unattended execution, provide `CDSE_USERNAME`
and `CDSE_PASSWORD` through your secure environment-management mechanism; missing
credentials fail immediately instead of waiting on an invisible prompt. No
scheduler is installed or configured by this package.

Ctrl+C or SIGTERM stops new claims and lets active workers finish. On the next
start, the coordinator reconciles incomplete publication and interrupted attempts.
After three failed attempts a scene remains `failed`; the CLI does not silently
reset its attempt budget. Logs/error details remain available for investigation.

### Requeue all failed scenes

`cyanotrend` invokes this application's CLI. `--output` selects the existing
queue/results directory; keep the same path used for planning and processing.
The quotes keep the spaces in the path together. `retry-failed` requeues every
scene currently marked `failed` and grants each up to three additional attempts.
It does not requeue successful, running, queued or automatically retrying scenes.
Earlier attempt numbers and logs are retained, and the previous error is recorded
in the scene's timing history. For example, attempts 1–3 are followed by 4–6.

```bash
cyanotrend --output "Output Data Notebook Parity" retry-failed
```

The report gives the requeued count and scene IDs. If a coordinator is already
running, it picks them up. Otherwise, `run` starts processing; `--once` exits
after all available workspace work and automatic retries finish. This processes
other queued work as well as the requeued failures.

```bash
cyanotrend --output "Output Data Notebook Parity" run --once
```

Each new batch retains the existing 60- then 120-second automatic retry delays.
Once all three attempts fail, the scene is terminally failed again until another
explicit `retry-failed` command. Investigate the reported error before retrying;
requeueing itself does not fix the cause of failure.

## 7. View status and timings from another terminal

`status` gives global distinct-scene counts and active stages. These counts apply
to saved plans, not undiscovered portions of the worldwide catalogue.

```bash
cyanotrend --output "Output Data Notebook Parity" status
```

`scenes` lists identities, status, attempt count and average staging transfer rate.
`--status running` limits the list to active scene jobs.

```bash
cyanotrend --output "Output Data Notebook Parity" scenes --status running
```

`scene` takes one UUID from the scene list. Replace `SCENE_UUID` with that actual
value. The report includes per-attempt stage durations, error details and ZIP path.

```bash
cyanotrend --output "Output Data Notebook Parity" scene SCENE_UUID
```

`coverage` reports discovered/processed scenes for countries/states. Because a
scene can intersect several regions, regional counts must not be summed to obtain
a global unique-scene count.

```bash
cyanotrend --output "Output Data Notebook Parity" coverage
```

`--json` requests structured output for the future frontend. It is a global flag,
so it appears before `coverage`, `status`, `scenes` or `scene`.

```bash
cyanotrend --output "Output Data Notebook Parity" --json coverage
```

## 8. Verify and compare before scaling

`verify` checks completed archive identities, ZIP CRC/member hashes, CSV schema,
master row counts and duplicate scene/lake keys. It is a storage-integrity check,
not a numerical comparison with the notebook.

```bash
cyanotrend --output "Output Data Notebook Parity" verify
```

`python -m unittest` invokes the standard-library test runner. `discover` finds
tests; `-s tests` selects the tests directory; `-v` prints each test name.
`PYTHONPATH=src` makes the source package importable without installation.
Run in the approved environment; skipped science tests mean missing dependencies,
not successful validation of those tests.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The full suite also includes pytest tests. `python -m pytest` invokes pytest in
the selected environment; `-q` makes its output concise:

```bash
PYTHONPATH=src python -m pytest -q
```

## 9. Merge scenes per day (S3A/S3B daily maximum)

`daily` merges processed scenes per UTC date and lake on NASA CyAN's 300 m grid.
Run it after any `run`. It reads saved scene results and publishes daily products;
it does not process scenes. Repeating a date rebuilds its files and replaces its
master rows. In this example, `--start` and `--end` select the same inclusive UTC
date. Replace `MICHIGAN_ID_FROM_REGIONS` with Michigan's actual ID. The previously
explained `plan` and `run --once` commands prepare and process its scenes first.

```bash
cyanotrend --output "Output Data Notebook Parity" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id MICHIGAN_ID_FROM_REGIONS
cyanotrend --output "Output Data Notebook Parity" run --once
cyanotrend --output "Output Data Notebook Parity" daily --start 2024-08-14 --end 2024-08-14
```

For a lake assigned to Michigan by its HydroLAKES pour point, the files are:

```text
Output Data Notebook Parity/daily/USA/Michigan/2024/08/14/
  2024-08-14_lakes.csv
  2024-08-14_grid.nc
  2024-08-14_manifest.json
```

Each lake appears only in its assigned state's folder. The CSV's `states` and
`state_ids` still list all states the lake touches. A lake whose scenes are all
processed gets a CSV row. A lake still missing a scene is listed under
`waiting_lakes` in its state's manifest with the missing scene names. Plan/run
the required scenes, then rerun `daily` for the same dates to update the files.
The daily method is now `max`: each index takes the maximum valid overlapping
value within each scene, then across all selected acquisitions of the UTC day.
The native formulas and masks are unchanged. `--method` selects the method;
`max` is its default and only supported daily value. Old `area`/`nearest` commands
are rejected. See [daily columns](DAILY_COLUMNS.md) for the exact definitions.

`daily/master_merged_daily.csv` combines the published daily CSV rows, with one
row per `date_utc` and `Hylak_id`. A successful rerun replaces all rows for that
date while preserving other dates. `daily/master_merged_daily_backup.csv` keeps
the previous successful master, including the header-only version before the
first publication. Interrupted file replacements are recovered on the next
`daily` invocation before another date is published.

Unmatched/ambiguous pour points and assignments outside the USA remain deferred
in the project Word note. Currently these cases report a date-level error before
publication; no fallback state/country is chosen. This is an unresolved limitation,
not an agreed boundary policy. No affected real-data lake has been verified by
the local synthetic tests. The existing daily science remains CONUS-only.

Processed single-scene files use the first planning state's full name:

```text
Output Data Notebook Parity/world/USA/Michigan/2024/08/14/
  <scene_id>.zip
  <scene_id>_<window>_<config_hash>_lakepixels.nc
  <scene_id>_<config_hash>_nasa_grid.nc
```

There can be more than one native lake-pixel file when a scene has multiple
windows. These files sit directly in the day folder beside the ZIP and NASA grid.
The scene ZIP contains the science NetCDF, one statistics CSV including the NASA
count, and metadata JSON. The separate NASA comparison CSV is removed; detailed
NASA results remain in JSON. Use a fresh output workspace: the new 58-column
daily master cannot be mixed with the old 22-column master. Existing outputs
are not migrated or deleted.

`daily` needs the catalogue (network) to build each date's checklist. Plans made
with `--max-scenes` can leave lakes waiting; the manifest warns about them.

See `SCIENCE_PARITY.md` for the same-scene HPC comparison before production scaling.
The original notebook and real reference outputs remain your comparison material;
no notebook runtime is needed by this package.
