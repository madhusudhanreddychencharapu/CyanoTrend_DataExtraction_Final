"""Fixed reference science defaults; paths are worker-local."""

import re
from pathlib import Path
import numpy as np

OCSSWROOT = Path("~/ocssw").expanduser().resolve()

L2_MODE = "full_swath_rhos"

NDCI_SOURCE = "rhos"

PROCESS_FULL_SCENE = False

GET_ANCILLARY = True

ALLOW_CLIMATOLOGY_FALLBACK = False

SAVE_FULL_REFLECTANCE_SPECTRA = False

COMPUTE_SHA256 = False

# Lowest reportable NASA CyAN CIcyano: DN 1 of the published conversion
# CIcyano = 10**(DN * 0.011714 - 4.1870866) (CyAN V6 release notes, 2025);
# DN 0 = "below threshold CIcyano detection limits". 10**(0.011714-4.1870866) = 6.68e-5.
CI_DETECTION_LIMIT = 10 ** (0.011714 - 4.1870866)

DEFAULT_MASK_PROFILE = "bloom_aware_cyan"

QA_EXCLUDE_FLAGS = ("HISATZEN", "NAVFAIL")

LAND_ADJACENCY_PIXELS = 0

NETCDF_DEFLATE = 4

ROW_BLOCK_SIZE = 256

DEFAULT_SHORE_BUFFER_M = 0.0

DEFAULT_BRIGHT_SCREEN = False

DEFAULT_BRIGHT_RHOS865 = 0.08

RAW_L1_DIR = Path("scratch/raw_l1_fallback").resolve()

STAGING_DIR = Path("scratch/staging").resolve()

L2_DIR = Path("scratch/l2").resolve()

EXPORT_DIR = Path("data/exports").resolve()

PAR_DIR = Path("data/par").resolve()

LOG_DIR = Path("data/logs").resolve()

CDSE_CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

CDSE_DOWNLOAD_ROOT = "https://download.dataspace.copernicus.eu/odata/v1"

CORE_RHOS_WAVELENGTHS = (620, 665, 681, 709)

SPECTRAL_RHOS_WAVELENGTHS = (
    400,
    412,
    443,
    490,
    510,
    560,
    620,
    665,
    674,
    681,
    709,
    754,
    779,
    865,
    885,
    900,
)

AQUATIC_RRS_WAVELENGTHS = (400, 412, 443, 490, 510, 560, 620, 665, 674, 681, 709)

PRODUCT_PATTERN = re.compile(
    "^(?P<prefix>[A-Za-z]+)_(?P<wavelength>\\d+(?:\\.\\d+)?)$", 32
)

DERIVED_FILL = np.float32(-32767.0)

DOWNLOAD_METHOD = "auto"

COMPACT_DEFLATE = 6

COMPACT_CHUNK_OBS = 65536

REQUIRE_L2_NONLAND = True

CDSE_S3_ENDPOINT = "https://eodata.dataspace.copernicus.eu"

CDSE_S3_KEYS_URL = "https://s3-keys-manager.cloudferro.com/api/user/credentials"

GLOBAL_ANALYSIS_RHOS_WAVELENGTHS = (490, 560, 620, 665, 681, 709, 754, 865, 885)

ADDITIONAL_INDEX_NAMES = ("MPH", "FAI")

OLCI_L2GEN_RHOS_NAME_OVERRIDES = {885: 884}

V264_SCHEMA = "global-efficient-v2.6.4"

CYAN_STRICT_QA_FLAGS = ("CLDICE", "HISATZEN", "NAVFAIL")

BLOOM_AWARE_HARD_FLAGS = ("HISATZEN", "NAVFAIL")

BLOOM_RESCUE_AFAI_MIN = 0.0

L2GEN_BIN = None

GETANC_BIN = None

OCSSW_SUBPROCESS_ENV = {}

# Reference cell 34 adaptive planner and cell 57 identity settings.
PROCESSING_STRATEGY = "adaptive_lake_windows"
L2_WINDOW_MARGIN_KM = 3.0
MAX_L2_WINDOWS_PER_SCENE = 6
FULL_SCENE_FRACTION_THRESHOLD = 0.35
TRUE_COLOR_RHOS_WAVELENGTHS = (665, 560, 490)
BLOOM_RESCUE_REQUIRE_CI_CYANO = True
BLOOM_RESCUE_REQUIRE_POSITIVE_AFAI = True
BLOOM_RESCUE_REQUIRE_NIR_OVER_BLUE = True

# NASA CyAN V6 parity (see docs/NASA_CYAN_COMPARISON.md).
# L2Gen suite: loads share/olci/<s3a|s3b>/msl12_defaults_CYAN.par, i.e. the CyAN
# vicarious gains (NOAA-adjusted Oa10/681 nm and Oa11/709 nm) and the CyAN gas
# transmittance table (water-vapour correction). gas_opt is no longer forced, so
# the OLCI default (431, table-based, includes H2O) applies.
L2GEN_SUITE = "CYAN"

# Extra L2Gen products needed by the HABS cloud test. rhos_443 is required;
# cloud_albedo is optional (rhos_865 is used where it is unavailable).
HABS_CLOUD_L2_PRODUCTS = ("rhos_443", "cloud_albedo")

# Cloud mask used for exclusion: "HABS" = OCSSW get_cloudmask_meris (turbidity-
# corrected albedo > 0.08 with scum/glint tests, as used for NASA CyAN CI);
# "CLDICE" = generic l2gen rhos865/cloud_albedo > 0.027 flag. CLDICE is always
# kept in cldice_mask as a diagnostic.
CLOUD_MASK_METHOD = "HABS"
HABS_CLOUD_THRESHOLD = 0.08

# The CLDICE bloom-rescue heuristic is superseded by the HABS scum exceptions.
BLOOM_RESCUE_ENABLED = False

# CI false-positive corrections, as published in NOAA TM NOS NCCOS 296
# (Wynne et al. 2018/2021, doi:10.25923/606t-m243):
#  - Clear-water correction (Section 3.1 and Appendix C2): CI set to non-detect
#    where kd < 0.31 and kd > 0, rho865 is not above rho490/665/709, and the
#    560 nm peak ss560 < 0.01.
#  - Adjacency flag (Section 3.1 and Appendix B2): CI > 0 but MCI < 0 is a
#    false positive from adjacency effects; the pixel is invalid for CI.
CI_CLEAR_WATER_CORRECTION = True
CI_CLEAR_WATER_KD_MAX = 0.31
CI_ADJACENCY_FLAG = True
