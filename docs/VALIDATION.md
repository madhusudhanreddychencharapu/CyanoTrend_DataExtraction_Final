# Validation record — notebook parity correction

Local validation: 25 tests passed, none skipped, using an isolated temporary
Python 3.12 environment. No remote Conda environment was modified.

Covered:

- 57 function bodies checked against normalized hashes extracted independently
  from the authoritative notebook, including adaptive windows and final exporters.
- Real metadata configuration hash: USA + 1.3 km2 + adaptive notebook defaults
  reproduces 58f41b2c1318eaf1. WORLD is intentionally different.
- Regional admission regression: reject a scene outside the requested state even
  when another part of a large candidate lake intersects it; count regional lakes.
- Adaptive window ordering, maximum-window, area-fraction, dateline and explicit
  full-scene fallback cases.
- Synthetic native masks, bloom rescue, unchanged compact statistics and window IDs.
- Duplicate overlapping native observations are removed in final share statistics.
- Same-lake mapped NetCDF export and final 50-column scene/master contract.
- End-to-end synthetic worker: one download, two L2Gen calls with window bboxes,
  persisted native windows, one share ZIP, one master publication.
- State attributes preserve HydroLAKES coordinates and ambiguous boundaries stay blank.
- Concurrent claims, five isolated workers, three total attempts, publication
  idempotency, prior-master backup, crash recovery, failed-scratch retention,
  rejection of changed references/universes and refusal to relabel old CSV schemas.

Actual supplied-scene downstream comparison:

The implemented export module was run against supplied standalone native NC for
08d76a5e-d66c-4e3c-b59f-d0bdd3d463c7. All 41 numeric Colab columns match for all
791 shared lake IDs (rtol=1e-12, atol=1e-15; exact counts). This comparison deliberately
matches IDs for diagnosis and does not filter production output by the reference CSV.
The global input still has 5,331 lakes; matching the supplied run requires its USA
universe. See supplied_scene_comparison.json for per-column results.

The test environment emitted one NumPy extension ABI-size warning on first import;
tests and the supplied-data comparison passed. This warning is recorded, not evidence
that the Linux environment has been validated. Key local versions: NumPy 2.5.2,
Pandas 3.0.5, netCDF4 1.7.4, SciPy 1.18.1. OCSSW was mocked in synthetic worker tests.

Not executed: a fresh authenticated CDSE/getanc/Linux OCSSW scene, global reference
download, a mapped-raster comparison against the supplied Colab raster using the
original HydroLAKES geometries, or a multi-scene HPC performance benchmark. No
claim of bitwise equality, historical runtime identity, or remote deployment is made.

To run the offline tests, `PYTHONPATH=src` points Python at this checkout's source;
`python -m pytest` invokes the test runner in the selected environment; `-q` requests
concise output. Install the declared test extra in a test environment if needed.

```bash
PYTHONPATH=src python -m pytest -q
```
