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

## 7. Merge per day (after the scenes are done)

One date (the date is the lake's local solar date; for US lakes it equals the UTC date):

```bash
cyanotrend --output "Output Data ISIN" daily --start 2024-08-14 --end 2024-08-14
```

A date range:

```bash
cyanotrend --output "Output Data ISIN" daily --start 2024-08-01 --end 2024-08-31
```

Results: `Output Data ISIN/daily/<country>/<state>/<yyyy>/<mm>/<dd>/` (`<date>_lakes.csv`, `<date>_grid.nc`,
`<date>_manifest.json`) and `Output Data ISIN/daily/master_merged_daily.csv`. Lakes with scenes still
missing are listed under `waiting_lakes` in the manifest. Finish or retry those scenes and run `daily` again
for the same dates.

## 8. Check the stored files

```bash
cyanotrend --output "Output Data ISIN" verify
cyanotrend --output "Output Data ISIN" verify --scene-id 3f2b8c1e-5a47-4d0e-9b6a-1c2d3e4f5a6b
```

## 9. Whole example: Michigan, 2024-08-14

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
cyanotrend --output "Output Data ISIN" daily --start 2024-08-14 --end 2024-08-14
cyanotrend --output "Output Data ISIN" verify
```

## 10. Whole example: Florida, 2026-06-28

```bash
cyanotrend --output "Output Data ISIN" plan --start 2026-06-28 --end 2026-06-28 --country USA --state-id 66186276B65591139260314
cyanotrend --output "Output Data ISIN" run --workers 5 --once
cyanotrend --output "Output Data ISIN" daily --start 2026-06-28 --end 2026-06-28
```

## Machine-readable output

`--json` is also before the command:

```bash
cyanotrend --output "Output Data ISIN" --json status
cyanotrend --output "Output Data ISIN" --json scenes --status failed
```

More detail: [docs/RUNNING.md](docs/RUNNING.md), [docs/DAILY_COLUMNS.md](docs/DAILY_COLUMNS.md).
