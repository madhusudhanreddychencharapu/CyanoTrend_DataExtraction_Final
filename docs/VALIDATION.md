# Validation record

Local implementation validation on 2026-09-23:

- Python syntax compilation passed.
- Pyflakes static analysis passed with no undefined names or unused imports.
- CLI help and command registration passed without optional scientific imports.
- Standard-library test suite: **12 passed, 4 skipped** (16 discovered).
- Five real local subprocesses overlapped in the scheduler test; six simulated
  scenes produced six unique ZIPs. No satellite data was used in this test.
- Tests covered concurrent claim deduplication, three attempts total, prior-master
  backup, repeated publication, crashes before/after master replacement,
  recovery of ready ZIPs, retry of abandoned workers, reference mismatch,
  malformed ZIP rejection, custom dates and scratch safety.
- Source audit: 15 normalized science/statistics/helper functions matched the
  notebook. Native extraction differences are documented in SCIENCE_PARITY.md.

Four synthetic science checks are present but were **not executed** in this
interpreter because scientific dependencies were unavailable together:

1. Native masks, bloom rescue, retained statistics and original 42-column schema.
2. State-boundary assignment with unchanged HydroLAKES coordinates.
3. Rejection of non-full-scene processing.
4. Native NetCDF ZIP packaging with all 46 CSV columns.

Permission to install missing packages into the existing temporary local
validation environment was requested separately. No existing environment was
modified without that approval. No changes were made to the remote
`s3olci-batch` environment.

Not performed: full global reference download, authenticated CDSE staging,
Earthdata/getanc retrieval, actual Linux OCSSW execution, real-scene numerical
comparison with the notebook, full-scale CPU/RAM/disk benchmarking, ten-year
catalogue ingestion, or frontend integration.

Therefore this commit is an implementation with tested operational mechanisms,
not a claim of scientifically validated or production-ready global processing.
Run preflight, the scientific tests, and the approved five-scene HPC validation
before scaling. Keep source/output archives from that comparison as evidence.
