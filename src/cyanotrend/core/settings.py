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

CI_DETECTION_LIMIT = 0.0001

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
