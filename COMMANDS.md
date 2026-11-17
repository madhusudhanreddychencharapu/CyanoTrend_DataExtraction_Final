# CyanoTrend commands, start to finish

Copy-paste commands with sample values (Michigan 2024-08-14, Florida 2026-06-28). Replace
the dates, state ID, scene ID and folder names with your own.

- Options such as `--output` go **before** the command: `cyanotrend --output "Output Data ISIN" status`.
- Use the same `--output` on every command of one project.
- Use a fresh output folder for the new grid/date code (`"Output Data ISIN"` below). An old folder is refused with "Scientific implementation changed".
- The state IDs below are the ones noted earlier for the US. Confirm them with `regions`.

## 0. Every session: go to the project

```bash
conda activate s3olci-daily
cd ~/cyanoTrend/CyanoTrend_DataExtraction_Final
```

## 1. One-time setup

```bash
cyanotrend --output "Output Data ISIN" prepare
cyanotrend --output "Output Data ISIN" preflight
```

`preflight` must report `ok: True`. NASA Earthdata login must be in `~/.netrc` (permissions 0600).

## 2. Find the state ID

```bash
cyanotrend --output "Output Data ISIN" regions                    # all countries
cyanotrend --output "Output Data ISIN" regions --country USA      # all US states and their IDs
cyanotrend --output "Output Data ISIN" regions --country CAN      # another country: same command
```

| State | ID |
| --- | --- |
| Michigan | `66186276B37442548568489` |
| Florida | `66186276B65591139260314` |
| Wisconsin | `66186276B91272823970344` |
| Ohio | `66186276B76680759932915` |
| Indiana | `66186276B82803899326534` |
| Illinois | `66186276B11383391196245` |
| New York | `66186276B43575054209436` |

## 3. Create a plan

One day, Michigan:

```bash
cyanotrend --output "Output Data ISIN" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489
```

One day, Florida:

```bash
cyanotrend --output "Output Data ISIN" plan --start 2026-06-28 --end 2026-06-28 --country USA --state-id 66186276B65591139260314
```

A whole month, Wisconsin:

```bash
cyanotrend --output "Output Data ISIN" plan --start 2024-08-01 --end 2024-08-31 --country USA --state-id 66186276B91272823970344
```

Small test (only the first 5 catalogue results):

```bash
cyanotrend --output "Output Data ISIN" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489 --max-scenes 5
```

Another country (state ID from `regions --country CAN`):

```bash
cyanotrend --output "Output Data ISIN" plan --start 2024-08-14 --end 2024-08-14 --country CAN --state-id STATE_ID_FROM_REGIONS
```

Only scenes that touch the state itself (old notebook rule), instead of every scene touching a lake of the state:

```bash
cyanotrend --output "Output Data ISIN" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489 --scene-rule state
```

No `--lake-region` is needed: it defaults to `WORLD` (all lakes of every country), the same for every state
and country. Add `--lake-region USA` only to the first plan if you want to limit an output folder to lakes that
intersect the US (to reproduce the 791-lake reference). It is fixed per output folder by the first plan, and a
later plan with a different value is refused.
Check the scene count in the report: zero scenes means no work was queued.

## 4. Run the queue

Interactive (asks for the Copernicus username and a hidden password), 5 scenes at a time, exits when done:

```bash
cyanotrend --output "Output Data ISIN" run --workers 5 --once
```

Keep running and wait for more plans (stop with Ctrl+C; running scenes finish first):

```bash
cyanotrend --output "Output Data ISIN" run --workers 5
```

Unattended (login from the environment, no prompt):

```bash
export CDSE_USERNAME="your_copernicus_username"
export CDSE_PASSWORD="your_copernicus_password"
cyanotrend --output "Output Data ISIN" run --workers 5 --once
```

Background, with a log file:

```bash
nohup cyanotrend --output "Output Data ISIN" run --workers 5 --once > run.log 2>&1 &
tail -f run.log
```

Separate scratch disk, or a second job at the same time (each job needs its own scratch):

```bash
cyanotrend --output "Output Data ISIN" --scratch .scratch_v3 run --workers 5 --once
```

## 5. Watch progress (second terminal)

`status` shows the totals, whether a `run` is active, the scenes being worked on, and a short
"attention" list (for example: scenes waiting but no `run` active, or a plan whose file was deleted).

```bash
cyanotrend --output "Output Data ISIN" status
cyanotrend --output "Output Data ISIN" scenes
cyanotrend --output "Output Data ISIN" scenes --status running
cyanotrend --output "Output Data ISIN" scenes --status failed
cyanotrend --output "Output Data ISIN" scenes --status done
cyanotrend --output "Output Data ISIN" coverage
```

One scene in detail (attempts, stage times, error, archive path); the ID comes from `scenes`:

```bash
cyanotrend --output "Output Data ISIN" scene 3f2b8c1e-5a47-4d0e-9b6a-1c2d3e4f5a6b
```

Logs: `Output Data ISIN/logs/<scene_id>_attempt<N>.log`.

## 6. Failed scenes: look, retry, run again

```bash
cyanotrend --output "Output Data ISIN" status
cyanotrend --output "Output Data ISIN" scenes --status failed
cyanotrend --output "Output Data ISIN" scene 3f2b8c1e-5a47-4d0e-9b6a-1c2d3e4f5a6b    # read the error first
cyanotrend --output "Output Data ISIN" retry-failed                                   # no options: requeues every failed scene, 3 more attempts
cyanotrend --output "Output Data ISIN" run --workers 5 --once                         # process them (and anything else still queued)
cyanotrend --output "Output Data ISIN" scenes --status failed                         # empty list = all recovered
```

`retry-failed` only requeues. If a `run` is already active it picks the scenes up by itself.

## 7. Control plans and the queue

List the plans. `plan_file` says `ok`, `MISSING` (file deleted by hand, scenes still queued) or
`not registered` (file in `plans/` that the queue does not know):

```bash
cyanotrend --output "Output Data ISIN" plans
cyanotrend --output "Output Data ISIN" scenes --status queued
```

Wrong dates in a plan: withdraw the plan. It removes the plan's queued, retrying and failed scenes and moves
the plan file to `plans/cancelled/`. A start of the plan ID is enough. Try `--dry-run` first:

```bash
cyanotrend --output "Output Data ISIN" cancel-plan 1f3a9c0d --dry-run
cyanotrend --output "Output Data ISIN" cancel-plan 1f3a9c0d
```

Remove queued scenes without touching the plan, by plan and/or by the scene's UTC start date:

```bash
cyanotrend --output "Output Data ISIN" cancel-queued --dry-run                                  # all queued/retrying scenes
cyanotrend --output "Output Data ISIN" cancel-queued                                            # remove them
cyanotrend --output "Output Data ISIN" cancel-queued --plan 1f3a9c0d                            # only this plan's scenes
cyanotrend --output "Output Data ISIN" cancel-queued --start 2025-01-01 --end 2025-12-31        # only these dates
cyanotrend --output "Output Data ISIN" cancel-queued --include-failed                           # failed scenes too
```

Rules: running, publishing and finished scenes are never removed (stop `run` with Ctrl+C and let them finish
or fail, then run the command again). A scene that another plan also lists is kept. `cancel-queued` leaves the
plans in place; a plan it emptied is listed in the notes, remove it with `cancel-plan`. After a withdrawal you
can run `plan` again with the right dates.

## 8. Merge per day (after the scenes are done)

One date (the date is the lake's local solar date; for US lakes it equals the UTC date):

```bash
cyanotrend --output "Output Data ISIN" daily --start 2024-08-14 --end 2024-08-14
```

A date range:

```bash
cyanotrend --output "Output Data ISIN" daily --start 2024-08-01 --end 2024-08-31
```

Only one state (much faster than every lake the scenes touch). It merges the lakes that touch the state and are
filed under it (a lake shared with an earlier plan's state stays in that state's record). Other states' rows in
the master and their folders are left exactly as they are:

```bash
cyanotrend --output "Output Data ISIN" daily --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489
```

`--country` and `--state-id` go together (the IDs come from `regions`). Lakes that cannot be matched to any state
are only merged by a `daily` run without these two options.

Results: `Output Data ISIN/daily/<country>/<state>/<yyyy>/<mm>/<dd>/` (`<date>_lakes.csv`, `<date>_grid.nc`,
`<date>_manifest.json`) and `Output Data ISIN/daily/master_merged_daily.csv`. Lakes with scenes still
missing are listed under `waiting_lakes` in the manifest. Finish or retry those scenes and run `daily` again
for the same dates.

## 8b. Compare with NASA CyAN: find lakes, scatter plot

Needs the state-limited `daily` run of section 8 for that state and date first, and `pip install matplotlib`
once (or `pip install -e ".[plot]"`). NASA tiles are read from `reference_data/obpg_cyan`; a tile that is not
cached is downloaded (Earthdata login in `~/.netrc`).

1. List the lakes of the state that have both our daily row and a NASA value for that date, biggest first
   (a lake with no name is shown as `(unnamed, Hylak_id N)`):

```bash
cyanotrend --output "Output Data ISIN" lakes --country USA --state-id 66186276B37442548568489 --date 2024-08-14
cyanotrend --output "Output Data ISIN" lakes --country USA --state-id 66186276B37442548568489 --date 2024-08-14 --name huron --limit 10
```

2. Scatter plot for one or more lakes (a name or a Hylak_id; repeat `--lake` for several lakes, one date):

```bash
cyanotrend --output "Output Data ISIN" scatter --country USA --state-id 66186276B37442548568489 --date 2024-08-14 --lake "Lake Huron"
cyanotrend --output "Output Data ISIN" scatter --country USA --state-id 66186276B37442548568489 --date 2024-08-14 --lake "Lake Huron" --lake 12345
cyanotrend --output "Output Data ISIN" scatter --country USA --state-id 66186276B37442548568489 --date 2024-08-14 --lake 12345 --out-dir plots
```

(The names and the Hylak_id above are examples; take yours from the `lakes` list.)

What the plot shows: one dot per NASA 300 m cell whose centre is inside the lake. x = NASA CyAN CI_cyano
(DN converted with NASA's formula; DN 0 = 0), y = our CI_cyano of the ISIN bin that contains the cell centre
(below our detection limit = 0). Only cells valid in both are plotted. The plot also has R, the regression
line and n. Files go to `Output Data ISIN/compare/<country>/<state>/<yyyy>/<mm>/<dd>/`
(`..._scatter.png` and `..._pairs.csv`, one row per dot). The command prints how many NASA cells were left
out and why (NASA land or no data, no bin of ours at the cell centre, our bin without valid data). If the
daily result for that state and date is missing, the command stops and prints the exact `daily` command to run.

## 9. Check the stored files

```bash
cyanotrend --output "Output Data ISIN" verify
cyanotrend --output "Output Data ISIN" verify --scene-id 3f2b8c1e-5a47-4d0e-9b6a-1c2d3e4f5a6b
```

## 10. Whole example: Michigan, 2024-08-14

```bash
conda activate s3olci-daily
cd ~/cyanoTrend/CyanoTrend_DataExtraction_Final

cyanotrend --output "Output Data ISIN" preflight
cyanotrend --output "Output Data ISIN" plan --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489
cyanotrend --output "Output Data ISIN" run --workers 5 --once
cyanotrend --output "Output Data ISIN" status
cyanotrend --output "Output Data ISIN" scenes --status failed
cyanotrend --output "Output Data ISIN" retry-failed        # only if something failed
cyanotrend --output "Output Data ISIN" run --workers 5 --once
cyanotrend --output "Output Data ISIN" daily --start 2024-08-14 --end 2024-08-14 --country USA --state-id 66186276B37442548568489
cyanotrend --output "Output Data ISIN" verify
```

## 11. Whole example: Florida, 2026-06-28

```bash
cyanotrend --output "Output Data ISIN" plan --start 2026-06-28 --end 2026-06-28 --country USA --state-id 66186276B65591139260314
cyanotrend --output "Output Data ISIN" run --workers 5 --once
cyanotrend --output "Output Data ISIN" daily --start 2026-06-28 --end 2026-06-28 --country USA --state-id 66186276B65591139260314
```

## Machine-readable output

`--json` is also before the command:

```bash
cyanotrend --output "Output Data ISIN" --json status
cyanotrend --output "Output Data ISIN" --json scenes --status failed
```

More detail: [docs/RUNNING.md](docs/RUNNING.md), [docs/DAILY_COLUMNS.md](docs/DAILY_COLUMNS.md).
