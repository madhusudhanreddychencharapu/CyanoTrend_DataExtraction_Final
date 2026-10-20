# NASA CyAN parity — findings and implementation plan for Codex

Repository: `CyanoTrend_Final_DataExtraction`
Written: 2026-10-19
Audience: Codex (implementation) and the CyanoTrend team (context)

This file records why the local Sentinel-3 OLCI CI_cyano pipeline reports far
fewer detected pixels than the NASA CyAN V6 daily product for the same lake and
day, what was measured, what was ruled out, and the exact code changes needed to
close the gap. Part B is the task list for Codex.

---

## Part A. Context and findings

### A1. The question

For the same lake and date, the local pipeline reports many fewer cyanobacteria
detections than NASA CyAN (`L*.L3m_DAY_CYAN_CI_cyano_CYAN_CONUS_300m_<tile>.tif`,
DN 1–253 = detectable cyanobacteria).

| Case | Local `ci_cyano_detection_pixels` | NASA DN 1–253 cells |
|---|---|---|
| Lake Erie, 2024-08-14, S3B 15:37 (`a59e9caf…`) | 1,722 (candidates: 6,994) | 11,683 |
| Lake Okeechobee, 2026-06-28, S3B 15:36 (`4fedc231…`, tile 7_5) | 4,715 (candidates: 4,848) | 10,904 |

Okeechobee is the clean test case: the lake was covered by a single OLCI pass
that day. Erie was also covered by an S3A pass at 16:15, so NASA's Erie map is a
two-pass maximum (see A7).

### A2. Things checked and ruled out (or corrected during the analysis)

| Item | Outcome |
|---|---|
| Aerosol correction | Not needed. CyAN computes CI from Rayleigh-corrected reflectance (rhos). The local run (`aer_opt=-99`) is consistent with this. |
| Spatial resolution changed in local code | No. l2gen runs on native pixels; extraction keeps every native pixel whose centre is inside the HydroLAKES polygon; statistics use native observations. The 300 m EPSG:4326 grid in `exports.py` is for display only (cells are 299 × 315 m at Erie, 299 × 300 m at Okeechobee). |
| Native pixel size | ~300 m. Okeechobee: 17,442 native pixels vs 15,737 NASA 300 m cells, so native counts run ~10% higher, not lower. |
| Detection limit | Current code uses `CI_DETECTION_LIMIT = 0.0` (`settings.py`) and the runs recorded `ci_detection_limit = 0.0`. NASA's lowest detectable value is DN 1 = CI 6.68e-5, so local is slightly looser (+99 cells at Okeechobee, 0 at Erie). It cannot explain fewer local detections. |
| The 0.08 in local code | `DEFAULT_BRIGHT_RHOS865 = 0.08` is a bright-pixel screen that is disabled (`DEFAULT_BRIGHT_SCREEN = False`; only runs under `if bright_screen:` in `compact.py`). It is never applied. |
| Wavelengths | Both use nominal 665/681/709 nm. |
| Land mask / lake area | HydroLAKES 1,418.8 km² vs NASA cells 1,416.3 km² at Okeechobee. Negligible. |
| Daily composite | Not applicable to Okeechobee (single pass). Applies to Erie. |
| NASA's extra tests (turbidity, clear water, adjacency) | These remove NASA pixels, so they push NASA's count down, not up. |

### A3. The seven processing stages compared

| Stage | Local pipeline | NASA CyAN V6 | Differs? |
|---|---|---|---|
| 1. Level-1 input | OLCI EFR from Copernicus | Same OLCI EFR scene | No |
| 2. l2gen → rhos | General OLCI defaults; no `suite`, no `gain`; `gas_opt=15` | CyAN suite: V6 vicarious gains "with minor adjustments to the 681 nm and 709 nm wavelengths" and water-vapour correction | Yes |
| 3. Cloud exclusion | l2gen CLDICE flag (rhos 865 > 0.027) excluded unless bloom-rescued | Bloom-aware HABS cloud test (`get_cloudmask_meris`): turbidity-corrected albedo > 0.08, with bloom/scum exceptions | Yes |
| 4. CI formula | CI = −SS681 | CI = 1.052 × [... 0.99·ρ681 ...] for OLCI | Yes |
| 5. Cyano tests | SS681 < 0, SS665 > 0, CI > 0 | 620 nm test with 0.99·ρ681, turbidity test, clear-water test, HABS flags, DN ≥ 1 | Yes |
| 6. Spatial output | One value per native pixel | l2bin area-weighted max into ~290 m bins, S3A+S3B max, area-weighted resample to 300 m Albers | Yes |
| 7. Count unit | Native pixels | 300 m cells (DN 0 / 1–253 / 254 / 255) | Unit only |

### A4. Evidence for each difference

**Stage 2: calibration and gas correction (the largest share)**

- Release notes V6, "Version 6 improvements/changes", item 1: "Updated processing to use the latest NASA S3A and S3B vicarious calibration with minor adjustments to the 681 nm and 709 nm wavelengths". Item 2: "Implemented water vapor correction as part of atmospheric correction".
- NASA ships CyAN-specific l2gen inputs for S3B (OCSSW manifest V2026.5, `share/olci/s3b/`):
  - `msl12_defaults_CYAN.par` (tag T2025.0, matches the V6 release in Feb 2025)
  - `olcis3b_gas_transmittance_cyan.nc` (tag T2024.26)
  - also `share/olci/msl12_defaults_CYAN.par`

  These are loaded only when l2gen runs with `suite=CYAN`. Their contents were not readable from the analysis environment: the server returns them as binary, and OCSSW is not installed on the Mac (`~/SeaDAS/ocssw` is empty). They must be inspected on the HPC.
- Local `run_l2gen` (`src/cyanotrend/core/l2gen.py`, parameter dict starting ~line 323) sets no `suite` and no `gain`, and hard-codes `"gas_opt": 15` (line 337).
- `gas_opt` bits (`l12_parms.h`): O3 = 1, CO2 = 2, NO2 = 4, H2O = 8, ATREM = 16, GAS_TRANS_TBL = 32, CO = 64, CH4 = 128, N2O = 256. So 15 = O3 + CO2 + NO2 + H2O, **without bit 32**. In `gas_trans.c`, bit 32 enables the table-based H2O transmittance and the O2 transmittance. Without it, H2O uses an older coefficient method and O2 is not corrected.
- Measured residual: on 1,741 smooth interior Okeechobee cells (5×5 neighbourhood all valid and detected), NASA's CI exceeds the local CI by ~0.0007 even after applying NASA's formula. The median ratio local/NASA is 0.646 with the local formula and 0.772 with NASA's formula.
- Why such a small offset matters: local CI = 0.636·ρ665 + 0.364·ρ709 − ρ681. The weights sum to zero, so a gain shared by all bands only scales CI and cannot flip a detection. Only band-to-band differences can. At Okeechobee (ρ ≈ 0.037), +0.0007 in CI equals ρ681 −1.9%, or ρ665 +2.9%, or ρ709 +5.4%. NASA's median detected CI there is about 0.001.

**Stage 3: cloud exclusion**

- Local CLDICE is l2gen's standard ocean cloud flag at rhos 865 > 0.027. Proof: in the Okeechobee output, the brightest non-CLDICE valid pixel has rhos 865 = 0.026999975. Okeechobee clear water has a median rhos 865 of 0.024, just under the threshold.
- Okeechobee grid: 15,757 cells; CLDICE 5,598; bloom-rescued 2,311; excluded as cloud 3,287 (21%).
- NASA's CI cloud test for OLCI/MERIS, `get_cloudmask_meris` (OCSSW `cloud_flag.c`, ~lines 144–250):
  1. cldtmp = cloud albedo, falling back to rhos 865.
  2. Subtract 3 × a turbidity term (from 620/665/681 nm, corrected with 443 and 754 nm).
  3. Cloud if the result > 0.08.
  4. Un-flag bloom/scum water via ratio tests (e.g. cldtmp < 0.1 and ρ560 > ρ681).
  5. Bright-red (ρ665 > 0.1 and cldtmp > 0.15) and glint checks.

  Any HABS flag sets CI to BAD_FLT (no data) in `get_habs_ci`.
- NASA has 505 no-data cells at Okeechobee; 504 lie inside the local cloud mask (one just outside). In the other ~2,780 locally excluded cells NASA reports water, and it detects cyano in 1,859 of them.
- Caveat: NASA's L3b metadata lists `LAND,CLDICE,HISATZEN` as binning flags, so the CyAN suite probably also configures the CLDICE flag differently from l2gen's default. Confirm on the HPC (B10).

**Stages 4–5: CI formula and cyano tests**

NASA OCSSW `get_habs.c`, function `get_habs_ci` (docs page labelled "ocssw V2022"; the source file exists in V2026.1 but could not be downloaded to confirm the values are unchanged):

```c
case OLCIS3A: case OLCIS3B: fac = 1.052;  w681_fac = 0.99;   // lines ~100-103
default:                    fac = 1.0;    w681_fac = 1.0;    // MERIS etc.
ci[ip] = fac * ((rhos[709] - rhos[665])*(wav2 - wav1)/(wav3 - wav1)
              - (rhos[681]*w681_fac - rhos[665]));          // lines ~193-194
```

- 1.052 is the OLCI-to-MERIS intercalibration. Release notes V5 item 3 cites Wynne et al. (2021). Wynne et al. (2021), Eq. 13: MERIS = 1.06 × OLCI ("OLCI underestimates the CI relative to MERIS by 6%"). The exact 1.052 is from the code.
- 0.99 multiplies ρ681 in both the CI and the 620 nm cyano/non-cyano test. Because it is already in the 2022 code, the V6 "681/709 adjustment" is most likely a separate calibration-gain change. That is an inference, not verified.
- Other NASA tests in `get_habs_ci` / `get_flags_habs_meris`:
  - Bad input: ρ665, ρ681 or ρ709 ≤ 0, or LAND/NAVFAIL → BAD_FLT.
  - Turbidity: ρ620 − ρ560 + (ρ560 − ρ665)(620−560)/(665−560) > 0 → CI = 0.
  - Clear water (`habs_meris_ci_corr`): Kd < 0.25 and related conditions → CI = 0.
  - Non-cyano: (ρ665 − ρ620) + (ρ620 − 0.99·ρ681)(665−620)/(681−620) < 0 → CI_cyano = 0.
  - HABS flags (cloud, non-water, dry lake, snow/ice, adjacency where MCI < 0 and CI > 0, bad input) → BAD_FLT.
  - Results are clamped to `validMin`.

Okeechobee, 12,470 valid cells, measured step by step:

| Rule | Count |
|---|---|
| Local: SS681 < 0 | 4,390 |
| Local: + SS665 > 0 (current detections) | 4,275 |
| NASA formula: CI > 0 (with 0.99·ρ681) | 5,208 |
| + 620 nm test with 0.99·ρ681 (≥ 0) | 5,169 (−39) |
| + turbidity test | 4,860 (−309) |
| + DN ≥ 1 floor | 4,703 (−157) |
| Would be flagged adjacency | 60 |
| Clear-water test (proxy: ρ885 < 0.005) | 0 |

**Stage 6: NASA's binning and mapping**

From the release notes and the L3b metadata in their appendices:
- Area-weighted binning: "Each pixel is added to all bins that it intersects."
- Binned grid: Integerized Sinusoidal, 289.89 m.
- `composite_prod = CI_cyano`, `composite_scheme = max`: each bin keeps the maximum CI_cyano.
- S3A and S3B daily files are merged by maximum.
- Reprojection to Albers Equal Area 300 m with area-weighted interpolation.
- SRTM 60 m land mask; DN 254 = land, 255 = no data.

The effect is that detections spread into neighbouring cells and cloud gaps shrink.

**Stage 7: DN encoding**

CI = 10^(DN × 0.011714 − 4.1870866). DN 1 → CI 6.68e-5; DN 253 → CI 0.0598.
Detection fractions avoid the unit problem: at Okeechobee, local detects 34.2% of valid water (4,715 / 13,798), NASA 71.9% (10,904 / 15,163).

### A5. Okeechobee decomposition (simulation on NASA's 300 m cells, cumulative)

| Step | Detected cells | Added | Share of gap |
|---|---|---|---|
| A. Local code as is (centre-assigned to NASA cells) | 4,093 | – | – |
| B. + NASA OLCI formula and tests | 4,612 | +519 | ~8% |
| C. + NASA-like cloud handling (local cloud-excluded cells treated as water; CI estimated from nearest valid cell) | 6,012 | +1,400 | ~21% |
| D. + l2bin max-binning (~290 m) and area-weighted Albers resample | 7,659 | +1,647 | ~24% |
| E. + reflectance offset equivalent to 0.98 / 0.97 × ρ681 | 9,441 / 11,444 | +1,782 to +3,785 | ~48% |
| NASA tile, same cells | 10,915 | | |

Shares depend on the order of the steps. Step C uses a nearest-neighbour proxy because rhos for cloud-excluded pixels are not stored in the 300 m share NetCDF. They are stored in the native compact files, so Codex's implementation can compute them properly. Step D is a simulation of NASA's binning, not NASA's code.

Worked example: a typical Okeechobee pixel where NASA detects and local does not (ρ665 = 0.037754, ρ681 = 0.0375, ρ709 = 0.035543):

| Formula | Result |
|---|---|
| Local | CI = −0.000550 (ρ681 above the 665–709 baseline) |
| NASA formula | CI = 1.052 × (0.036950 − 0.99 × 0.0375) = −0.000184 (still not detected) |
| ρ681 scaled by 0.98 | CI = 0.000210, DN 44 |
| ρ681 scaled by 0.97 | CI = 0.000605, DN 83 |

The pixel switches to detected at about 0.985 × ρ681.

### A6. Open questions (must be confirmed on the HPC)

1. Contents of `$OCDATAROOT/olci/s3b/msl12_defaults_CYAN.par` and `$OCDATAROOT/olci/msl12_defaults_CYAN.par`: gains, `gas_opt`, gas table, cloud settings.
2. Whether the installed OCSSW `get_habs.c` still has `fac = 1.052`, `w681_fac = 0.99`.
3. OCSSW version on the HPC versus NASA's V6 production version.

### A7. Erie note

An S3A pass on 2024-08-14 at 16:15 covers the western basin (longitude −83.50 to −82.47), where 96% of NASA's Erie detections are. On the same western-basin cells, local rules give 4,338 detections from S3A versus 2,085 from S3B, and 5,640 for the union. NASA's daily maximum includes S3A, so Erie needs the daily composite (B9) before its counts can be compared.

---

## Part B. Implementation plan for Codex

### B0. Ground rules (read first)

1. **Do not edit the notebook-contract functions.** `tests/test_source_contract.py` hashes the AST of functions listed in `docs/notebook_function_contract.json`, including `science.ci_family`, `compact.extract_lake_only_netcdf`, `compact._open_compact_writer_v264`, `l2gen.l2_products`, `l2gen.validate_l2_file`, `configuration.processing_config_hash`, `statistics.compact_stats` and `exports._share_all_index_stats`. Add new functions and modules instead. `run_l2gen`, `run_getanc` and `worker.py` are not under contract.
2. **Defaults must reproduce current outputs exactly.** Every new behaviour is behind settings that default to off. With parity off, the existing tests, config hash, schema (51 columns) and outputs stay identical.
3. **Do not invent NASA thresholds.** Anything ported from OCSSW (`get_habs.c`, `cloud_flag.c`) is copied from source with file, function and line references in comments. Prefer letting l2gen compute NASA's own products (Route A) over re-implementing them (Route B).
4. **Run new processing into a fresh output workspace.** Outputs with parity enabled get a distinct identity (B6).

### B1. Settings (`src/cyanotrend/core/settings.py`)

Add, all defaulting to current behaviour:

```python
# NASA CyAN V6 parity (all off by default = current notebook behaviour)
L2GEN_SUITE = None                 # "CYAN" loads msl12_defaults_CYAN.par (+ s3a/s3b variants)
L2GEN_GAS_OPT = 15                 # None = omit and let sensor/suite defaults decide
NASA_PARITY_ENABLED = False        # compute NASA-style CI/flags/diagnostics
NASA_PARITY_L2GEN_PRODUCTS = ("CI_cyano", "CI_noncyano", "CI_stumpf", "MCI_stumpf", "flags_habs")
NASA_PARITY_EXTRA_RHOS = (443,)    # needed by the Python ports of get_cloudmask_meris / clear-water test
NASA_OLCI_CI_FAC = 1.052           # OCSSW get_habs.c get_habs_ci, OLCIS3A/OLCIS3B
NASA_OLCI_W681_FAC = 0.99          # OCSSW get_habs.c get_habs_ci, OLCIS3A/OLCIS3B
NASA_DN_SLOPE = 0.011714           # CyAN release notes: CI = 10**(DN*0.011714 - 4.1870866)
NASA_DN_INTERCEPT = -4.1870866
NASA_ISIN_BIN_M = 289.894507       # CyAN L3b metadata spatialResolution
```

### B2. l2gen changes (`src/cyanotrend/core/l2gen.py`, function `run_l2gen` only)

1. If `settings.L2GEN_SUITE` is set, add `"suite": settings.L2GEN_SUITE` to `parameters`.
2. Replace the hard-coded `"gas_opt": 15` with `settings.L2GEN_GAS_OPT`, and omit the key when it is `None`. `write_par_file` already skips `None`. When the suite is CYAN, the recommended value is `None`, so the CyAN gas settings are not overridden.
3. When `NASA_PARITY_ENABLED`, append `NASA_PARITY_L2GEN_PRODUCTS` and `rhos_443` to `products` *after* calling `l2_products()`. Do not edit `l2_products`. Keep them out of `required` so the existing "product X not found" retry drops any that are unavailable; log each dropped product in `info`.
4. **L2 cache name must include the suite and the parity products.** Today the output name is `{name}_L2GEN_{suffix}_{scope_tag}_{spectrum_tag}.nc`, so a non-CYAN file could be silently reused for a CYAN run. Add a tag such as `_SUITE-CYAN` / `_SUITE-NONE` and `_HABS` when parity products are requested.
5. Provenance: after l2gen succeeds, read the L2 NetCDF group `processing_control/input_parameters` (l2gen writes all effective parameters there) and store every attribute in `info["l2gen_effective_parameters"]`. This records the actual gains, `gas_opt`, gas table, cloud threshold and suite used, which answers A6 directly from the output files.
6. Keep `maskland/maskcloud/... = 0` so all pixels are still written. Keep `aer_opt=-99`, `brdf_opt=0` (rhos does not depend on aerosol), but record them in provenance.

### B3. New module `src/cyanotrend/core/nasa_parity.py`

It is picked up automatically by `identity.science_fingerprint()` because that globs `core/*.py`. Functions:

```python
def encode_cyan_dn(ci) -> np.ndarray:
    """CyAN DN: NaN -> 255, CI < DN1 (incl. 0/negative) -> 0, else round((log10(CI) + 4.1870866)/0.011714) clipped to 1..253."""

def decode_cyan_dn(dn) -> np.ndarray:
    """DN 1..253 -> 10**(DN*0.011714 - 4.1870866); 0 -> 0.0; 254/255 -> NaN."""

def nasa_olci_ci(r560, r620, r665, r681, r709, *, fac=1.052, w681=0.99) -> dict:
    """Port of OCSSW get_habs.c get_habs_ci for OLCI (cite lines in comments).
    Returns CI (fac-scaled), CI_cyano (0 for turbid/non-cyano), masks: bad_input, turbid, noncyano, detected (CI_cyano >= DN1)."""

def nasa_olci_mci(r681, r709, r754, *, fac=1.052, w681=0.99) -> np.ndarray:
    """MCI_stumpf as in get_habs_ci (bands 681/709/754, same fac and w681_fac)."""

def habs_cloudmask_meris(rhos: dict[int, np.ndarray], cloud_albedo=None) -> np.ndarray:
    """Port of OCSSW cloud_flag.c get_cloudmask_meris (~lines 144-250) VERBATIM:
    turbidity term (620/665/681 corrected with 443/754), cldtmp - 3*ftemp > 0.08, bloom/scum
    exceptions, bright-red and glint checks. Falls back to rhos 865 where cloud_albedo is missing."""

def habs_flags_meris(...) -> np.ndarray:
    """Optional port of get_flags_habs_meris (non-water, dry lake, snow/ice, adjacency, bad input)."""

def simulate_nasa_l3_counts(lat, lon, ci_cyano, valid, lake_cells_affine, crs) -> dict:
    """Simulate l2bin area-weighted MAX binning (~289.9 m bins) + area-weighted resample to the
    NASA Albers 300 m grid. Return detected/non-detect/no-data cell counts and a DN raster aligned
    to the NASA tile, for like-for-like comparison."""
```

Route A versus Route B:
- **Route A (authoritative):** read l2gen's own `CI_cyano` and `flags_habs` (B2.3). These are NASA's implementation in the installed OCSSW version. Decode `flags_habs` bits from the variable's `flag_meanings`/`flag_masks` attributes. If those are absent, use the `HABS_*` defines in `get_habs.c`.
- **Route B (transparent cross-check):** the Python ports above, run on rhos. Both routes must agree on the same pixels (B7).

`simulate_nasa_l3_counts` reference algorithm (validated in the analysis; reproduces A5 step D):
1. Project native pixel centres to the NASA tile CRS.
2. Tessellate each pixel footprint on a 30 m sub-grid, giving each sub-cell its nearest pixel within 230 m.
3. Assign sub-cells to ~290 m bins.
4. Each bin takes the **max** CI_cyano of any pixel touching it (non-detect = 0; no data only if every touching pixel is no data).
5. Each 300 m Albers cell takes the area-weighted mean of the bins covering it.
6. Encode as DN; count DN 1–253 over HydroLAKES cells that are not land (DN 254).

Process lakes one at a time or in tiles to bound memory (a whole-lake Erie run at a 30 m sub-grid ran out of memory).

### B4. Adding NASA fields to compact files without touching the frozen extractor

Add `augment_compact_with_nasa_parity(compact_path, l2_path)` in `nasa_parity.py`:
- Use the compact file's `source_row` / `source_col` (window-relative; already stored per observation) to sample the L2 file.
- Append new variables to the compact NetCDF: `CI_nasa_formula`, `CI_cyano_nasa_formula`, `nasa_turbid_mask`, `nasa_noncyano_mask`, `nasa_detected_mask`, `habs_cloud_mask` (Route B), `l2gen_CI_cyano`, `l2gen_flags_habs`, `l2gen_MCI_stumpf` (Route A), and `rhos_443`.
- Compute the Route B fields for all inside pixels, including CLDICE pixels; rhos are already stored for every pixel.
- Add attributes for the parity settings, l2gen effective parameters and code references.

Hook it into `src/cyanotrend/worker.py` immediately after `compact.extract_lake_only_netcdf(...)` and `validate_compact_product(...)`, and **before `l2.unlink(...)`** (~lines 130–139). Run it only when `NASA_PARITY_ENABLED`. Reuse a compact window file only if its parity variables are present, so validation stays consistent with parity mode.

### B5. Outputs: new diagnostics file, master schema unchanged

Do not change `schema.py` or the 51-column master. Add a fifth ZIP member, `S3_OLCI_<scene_id>_NASA_PARITY_DIAGNOSTICS.csv`, built in `worker.py` after `compare_scene(...)`. One row per lake:
- `native_pixels`, `valid_local`, `ci_cyano_detection_local`
- `ci_detected_nasa_formula`, `removed_by_turbidity`, `removed_by_noncyano_0p99`, `removed_by_dn1_floor`
- `cloud_excluded_local`, `habs_cloud_python`, `l2gen_flags_habs_cloud`
- `l2gen_ci_cyano_detected` (Route A)
- `sim_l3_detected_cells_local`, `sim_l3_detected_cells_nasa_formula`, `sim_l3_detected_cells_route_a`, `sim_l3_nodata_cells`
- `NASA_CI_Cyano_detectable_cells`, `NASA_CYAN_No_valid_data_pixels` (copied from the comparison)
- `detection_fraction_local`, `detection_fraction_nasa`
- `l2gen_suite`, `l2gen_gas_opt_effective`, `l2gen_version`

Also record the parity settings and `l2gen_effective_parameters` in `METADATA.json` under `nasa_parity`.

### B6. Identity and fingerprint

- `processing_config_hash` is frozen. Add `nasa_parity_config_hash()` in `nasa_parity.py`, built from `L2GEN_SUITE`, `L2GEN_GAS_OPT`, `NASA_PARITY_*`, `NASA_OLCI_*` and the DN constants.
- In `worker.py`, when parity or a suite is enabled, use `config_hash = f"{base_hash}-{parity_hash[:8]}"` for compact file names, native folder and metadata. With parity off it stays exactly `base_hash`.
- `science_fingerprint` already covers `core/*.py`.

### B7. Tests (offline, synthetic; add `tests/test_nasa_parity.py`)

1. `test_contract_untouched`: the existing `test_source_contract.py` still passes.
2. `test_defaults_unchanged`: with default settings, `run_l2gen` writes the same par keys as today (`gas_opt=15`, no `suite`) and `processing_config_hash(1.3, 'USA')` is unchanged.
3. `test_dn_round_trip`: `encode(10**(DN*0.011714-4.1870866)) == DN` for DN 1..253; `encode(6.0e-5) == 0`; `encode(0) == 0`; `encode(NaN) == 255`; `encode(1.0) == 253`.
4. `test_nasa_formula_worked_example`: ρ665 = 0.037754, ρ681 = 0.0375, ρ709 = 0.035543 gives local −SS681 = −0.000550 and NASA CI = −0.0001841 (abs tol 1e-7). With ρ681 × 0.97/0.99, CI = 0.0006049 and DN = 83.
5. `test_turbidity_zeroes_ci` and `test_noncyano_uses_0p99`: hand-built spectra on each side of the thresholds.
6. `test_l2_cache_name_includes_suite`: CYAN and non-CYAN runs produce different L2 file names.
7. `test_par_omits_gas_opt_when_none` and `test_suite_written`.
8. `test_augment_compact_roundtrip`: a tiny fake L2 file plus compact file; the parity variables appear and line up through `source_row` / `source_col`.
9. `test_simulated_binning_max_spreads`: one detected pixel produces at least one detected 300 m cell; a bin mixing a detection and non-detects keeps the max; a bin of all no-data pixels stays no data.
10. `test_route_a_b_agree` (runs on the HPC when a real L2 file is present; skipped offline): l2gen `CI_cyano` equals the Python port within rtol 1e-4 where both are valid.

### B8. Documentation

- Update `docs/NASA_CYAN_COMPARISON.md` with a "Parity mode" section: settings, diagnostics CSV fields, and the warning that native counts and NASA cells are different units.
- Update `docs/SCIENCE_PARITY.md`: parity mode deliberately diverges from the notebook, and is off by default.
- Delete the empty folder `docs/nasa_source_proof/` (left over from a blocked download).

### B9. Optional phase: daily S3A + S3B composite

For lakes covered by more than one pass on the same UTC day (Erie 2024-08-14), add a per-day step that combines compact observations from all scenes of that day by per-bin maximum inside `simulate_nasa_l3_counts`, matching NASA's `l3bin composite_scheme=max`. Write the result to a day-level diagnostics CSV. Each scene's `METADATA.json` already states that the S3A/S3B daily maximum is deliberately not implemented yet (`future_daily_composite`); keep it as a separate, explicitly enabled feature.

### B10. Validation run and acceptance criteria

Scene: `S3B_OL_1_EFR____20260628T153604_20260628T153904_20260629T164358_0179_121_339_2520_ESA_O_NT_004` (Okeechobee, Hylak_id 69); NASA tile `L2026179.L3m_DAY_CYAN_CI_cyano_CYAN_CONUS_300m_7_5.tif`.

Run four configurations into separate fresh workspaces:

| Run | Settings | Expected |
|---|---|---|
| R0 | defaults | identical to current outputs (4,715 native detections) |
| R1 | parity on, suite None, `gas_opt` 15 | Route B formula count ≈ 4,703 valid grid cells; simulated L3 ≈ 6,600–7,700 cells |
| R2 | parity on, `suite=CYAN`, `gas_opt=None` | record ρ665/681/709 change vs R0 (expect band-specific ~1–3%); simulated L3 moves toward 10,900 |
| R3 | R2 + `habs_cloud` used as cloud mask in the diagnostics | simulated no-data cells ≈ NASA's ~500–580 |

Acceptance:
- R0 is byte-for-byte or numerically identical to current outputs, and all existing tests pass.
- Route A and Route B agree (B7.10).
- Best configuration: the simulated L3 detected-cell count is within ±10% of NASA's 10,904 for Okeechobee, with the per-cell confusion matrix reported.
- `l2gen_effective_parameters` is captured for every run.

### B11. Checks the team should run on the HPC first (no code needed)

```bash
cat $OCDATAROOT/olci/s3b/msl12_defaults_CYAN.par
cat $OCDATAROOT/olci/msl12_defaults_CYAN.par
cat $OCDATAROOT/olci/s3b/msl12_defaults.par        # compare gain= and gas_opt= lines
ncdump -h $OCDATAROOT/olci/s3b/olcis3b_gas_transmittance_cyan.nc
l2gen -version                                     # or read the version line in an l2gen log
grep -n "w681_fac\|fac = 1.052" $OCSSWROOT/ocssw_src/src/l2gen/get_habs.c   # only if sources are installed
```

---

## Glossary

- **rhos (ρs):** Rayleigh-corrected surface reflectance from l2gen (no aerosol removal).
- **SS681 / SS665:** spectral shape at 681 / 665 nm relative to a straight baseline between neighbouring bands. CI = −SS681.
- **CI_cyano:** CI kept only where the 620 nm test indicates cyanobacteria.
- **CLDICE:** l2gen's cloud/ice flag in `l2_flags`.
- **HABS flags:** NASA's harmful-algal-bloom flags (`flags_habs`), including its bloom-aware cloud test.
- **Vicarious gain:** a per-band calibration multiplier, near 1, that NASA derives by comparing OLCI with in-situ reference measurements.
- **DN:** CyAN GeoTIFF digital number. 0 = below detection, 1–253 = data, 254 = land, 255 = no data.
- **l2bin / l3bin / l3mapgen:** NASA tools for binning L2 pixels, merging binned files, and mapping to a projected grid.

## References

- NASA OCSSW `get_habs.c`: https://oceancolor.gsfc.nasa.gov/docs/ocssw/get__habs_8c_source.html
- NASA OCSSW `cloud_flag.c`: https://oceancolor.gsfc.nasa.gov/docs/ocssw/cloud__flag_8c_source.html
- NASA OCSSW `l12_parms.h` (gas_opt bits): https://oceancolor.gsfc.nasa.gov/docs/ocssw/l12__parms_8h_source.html
- NASA OCSSW `gas_trans.c`: https://oceancolor.gsfc.nasa.gov/docs/ocssw/gas__trans_8c_source.html
- OCSSW V2026.5 OLCI S3B share folder (CyAN par and gas table): https://oceandata.sci.gsfc.nasa.gov/directdataaccess/Manifest/V2026.5/share/olci/s3b/
- Current `get_habs.c` (V2026.1): https://oceandata.sci.gsfc.nasa.gov/manifest/tags/V2026.1/ocssw_src/src/l2gen/get_habs.c
- CyAN release notes V6: https://oceancolor.gsfc.nasa.gov/images/cyan/version/6/CyAN_NASA_MERISOLCI_CI_release_notes_V6_Feb_2025.pdf (the August 2025 update is in the team's research folder)
- Wynne, T.T., Mishra, S., Meredith, A., Litaker, R.W., Stumpf, R.P. (2021). Intercalibration of MERIS, MODIS, and OLCI Satellite Imagers for Construction of Past, Present, and Future Cyanobacterial Biomass Time Series. Remote Sensing 13(12), 2305. https://doi.org/10.3390/rs13122305
- Project note: `claude/okeechobee_2026-06-28_nasa_cyan_gap_analysis.md`
