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

### Merge each date automatically: `run --auto-daily`

`run --auto-daily` is optional and off by default; without it `run` does exactly what is described above.
With it, `run` also merges a solar date once every scene that can feed that state's lakes and starts (UTC)
from the day before to the day after is final: `done`, `skipped_no_lakes`, or `failed` after three attempts.
(Those are the scenes of the state's plans plus any scene recorded as touching the state.) The merge is the
`daily --start D --end D --country C --state-id S` command of section 9, started as a separate process, one at
a time, oldest date first, with its own log in `<output>/logs/daily_*.log`. It runs in its own process so a
crashing merge cannot stop the scene workers or the CSV publisher, and so Ctrl+C reaches only `run`.

```bash
cyanotrend --output "Output Data Notebook Parity" run --workers 5 --auto-daily
cyanotrend --output "Output Data Notebook Parity" run --workers 5 --once --auto-daily
```

Rules: a failed scene does not block a merge (its lakes become `partial`, flagged); a date is merged again when
the final scene statuses it was merged from change (`retry-failed`, another plan adding scenes of that date);
nothing is ever queued by the merge: after each merge `run` prints how many scenes the date still needs that no
plan lists, and the `plan` command that would add them; Ctrl+C or SIGTERM starts no new merge but lets the running
one finish; with `--once`, `run` waits for the last merge before exiting; a merge that fails is recorded and not
repeated within that `run` (the next `run` tries it again). A merge row left open by a process that died is
marked `interrupted` by the next merge or `run`.

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

`progress` is `status` for the merge side: per planned state and solar date it counts the dates merged, ready to
merge, waiting for scenes (with how many scenes are not final), failed, and lists merged dates whose lakes still
wait, split into scenes in a plan that are not finished and scenes in no plan (with the `plan` command that would
add them), the logs of failed merges, and the last 10 merges. It reads the plans and the ledger only and is safe
while `run` is active.

```bash
cyanotrend --output "Output Data Notebook Parity" progress
```

`--json` requests structured output for the future frontend. It is a global flag,
so it appears before `coverage`, `status`, `scenes`, `scene` or `progress`.

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

## 8b. Plans, the queue and status

`cyanotrend plans` lists the saved plans (dates, region, scenes by status, and whether the plan file exists).
`cyanotrend cancel-plan <id> [--dry-run]` withdraws a plan: its queued, retrying and failed scenes are deleted
(unless another plan lists them) and the plan file is moved to `plans/cancelled/` so `run` cannot import it
again. `cyanotrend cancel-queued [--plan ID] [--start D] [--end D] [--include-failed] [--dry-run]` removes
scenes that have not started. Running, publishing and finished scenes are never removed. Deleting a plan file
by hand does not remove its scenes from the queue; use `cancel-plan` for that.

`cyanotrend status` reports the totals by status, whether a `run` holds the service lock, the scenes in
progress (marked ABANDONED when no `run` is active) and an "attention" list. Read-only commands stop with a
clear message when `--output` has no `operations.sqlite3`, instead of creating an empty workspace.

## 8c. Compare a lake with NASA CyAN (list lakes, scatter plot)

After a state-limited `daily` run, `cyanotrend lakes --country ISO3 --state-id ID --date D [--name TEXT]
[--limit N]` lists the lakes of that state that have our merged daily row and a NASA CyAN value for the date
(biggest first; `--name` filters by text; unnamed lakes show their Hylak_id).
`cyanotrend scatter --country ISO3 --state-id ID --date D --lake NAME_OR_ID [--lake ...] [--out-dir DIR]`
draws one dot per NASA cell inside the lake: x = NASA CyAN CI_cyano, y = our CI_cyano of the ISIN bin at the
cell centre, with R and a regression line, and writes the PNG plus a CSV of the pairs under
`<output>/compare/<country>/<state>/yyyy/mm/dd/`. NASA CyAN is used only for this comparison. The plot needs
matplotlib (`pip install matplotlib`). If the daily result is missing, the command stops and prints the
`daily` command to run first.

Two optional views of the same pairs: `--log` (log-log axes; a value of exactly 0 gets its own place left of
and below the first tick) and `--density` (colour = number of NASA cells per small square instead of dots;
one panel per lake); they can be combined. Each writes its own file (`..._scatter_log.png`,
`..._scatter_density.png`, `..._scatter_log_density.png`). Without them the plot is the linear one and its
file is never overwritten. The reason for `--log`: NASA stores whole-number DN values on a log scale, so on
a linear axis the vertical columns at low values merge into one smear.

## 9. Merge scenes per day (S3A/S3B daily maximum)

`daily` merges processed scenes per local solar date and lake on ISIN bins.
Run it after any `run`. It reads saved scene results and publishes daily products;
it does not process scenes. Repeating a date rebuilds its files and replaces its
master rows. In this example, `--start` and `--end` select the same inclusive solar
date (scene start UTC + lake longitude / 15 hours; the same calendar day as UTC for the USA). Replace `MICHIGAN_ID_FROM_REGIONS` with Michigan's actual ID. The previously
explained `plan` and `run --once` commands prepare and process its scenes first.

```bash
cyanotrend --output "Output Data Notebook Parity" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id MICHIGAN_ID_FROM_REGIONS
cyanotrend --output "Output Data Notebook Parity" run --once
cyanotrend --output "Output Data Notebook Parity" daily --start 2024-08-14 --end 2024-08-14
```

For a lake whose record belongs to Michigan, the files are:

```text
Output Data Notebook Parity/daily/USA/Michigan/2024/08/14/
  2024-08-14_lakes.csv
  2024-08-14_grid.nc
  2024-08-14_science.nc
  2024-08-14_manifest.json
```

`2024-08-14_science.nc` carries every layer of the single-scene science NetCDF (9 `rhos` bands, 5 indices, 12 masks,
`DN`, `scenes_valid`, lake IDs) with one cell per ISIN bin. `cyanotrend reproduce <that file>` rebuilds the lakes CSV
columns from it and compares them with the CSV.

`daily --country ISO3 --state-id ID` merges only the lakes that touch that state and are filed under it,
which is much faster than merging every lake the scenes touch. In that mode only those lakes' rows are
replaced in `master_merged_daily.csv`; other lakes' rows and other states' folders stay as they are.

The same layout, `daily/<country>/<state>/YYYY/MM/DD/`, is used for every country. Each lake
appears only once per date: a lake shared by several states or countries is filed under the
region whose plan processed it first (earliest plan whose region touches the lake; else the
pour-point state; else the region that first processed the scene), and a later plan for a
neighbouring region reuses the same processed scene. The CSV's `states` and `state_ids`
still list all states the lake touches. A lake whose scenes are all
processed gets a CSV row. A lake still missing a scene is listed under
`waiting_lakes` in its state's manifest with the missing scene names. Plan/run
the required scenes, then rerun `daily` for the same dates to update the files.
The daily method is area-weighted binning on ISIN bins (69120 rows, about 290 m):
within each scene every pixel is shared among the bins it overlaps by area and a
bin's value is the area-weighted mean of its valid pixels; the daily value of
each index is then the highest scene value of the bin across all selected
acquisitions of the solar date. The native formulas and masks are unchanged.
`--method area` is the default and only supported value; `max` and `nearest`
are rejected. See [daily columns](DAILY_COLUMNS.md) for the exact definitions.

`daily/master_merged_daily.csv` combines the published daily CSV rows, with one
row per `date_solar` and `Hylak_id`. A successful rerun replaces all rows for that
date while preserving other dates. `daily/master_merged_daily_backup.csv` keeps
the previous successful master, including the header-only version before the
first publication. Interrupted file replacements are recovered on the next
`daily` invocation before another date is published.

A lake whose pour point is unmatched or ambiguous is filed under the region that first
processed its scene, and the manifest records this basis. The NASA CyAN comparison column is
blank for lakes whose passes are not on the UTC date equal to the solar date. No real-data
border lake has been verified by the local synthetic tests.

Processed single-scene files use the first planning state's full name:

```text
Output Data Notebook Parity/world/USA/Michigan/2024/08/14/
  <scene_id>.zip
  <scene_id>_<window>_<config_hash>_lakepixels.nc
  <scene_id>_<config_hash>_isin_grid.nc
```

There can be more than one native lake-pixel file when a scene has multiple
windows. These files sit directly in the day folder beside the ZIP and the ISIN bin file.
The scene ZIP contains the science NetCDF, one statistics CSV including the NASA
count, and metadata JSON. The separate NASA comparison CSV is removed; detailed
NASA results remain in JSON. Use a fresh output workspace: the new 58-column
daily master cannot be mixed with the old 22-column master. Existing outputs
are not migrated or deleted.

`daily` needs the catalogue (network) to build each date's checklist. Plans made
with `--max-scenes` can leave lakes waiting; the manifest warns about them.

Every merge, by hand or from `run --auto-daily`, is recorded in the ledger table `daily_runs` of
`operations.sqlite3` (date, state, source `manual`/`auto`, start/end, lakes complete/partial/waiting/unavailable,
scenes missing and how many of those are in no plan, a fingerprint of the final scene statuses it used, log file,
error). The fingerprint is how `run --auto-daily` and `progress` know that a merged date is still up to date. A
`daily` run without `--state-id` is recorded for all states and is not used for that decision. The table is
created when the database is opened, so existing workspaces need no migration.

See `SCIENCE_PARITY.md` for the same-scene HPC comparison before production scaling.
The original notebook and real reference outputs remain your comparison material;
no notebook runtime is needed by this package.
