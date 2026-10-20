"""Daily column identities and OCSSW product names."""
from ..schema import COLUMNS

DETECTION_COLUMNS = (
    "S3A_CI_cyano_detection_pixels",
    "S3B_CI_cyano_detection_pixels",
    "merged_CI_cyano_detection_pixels",
)
DAILY_COLUMNS = []
for column in COLUMNS:
    if column == "native_lake_pixels":
        DAILY_COLUMNS.append("daily_lake_pixels")
    elif column == "ci_cyano_detection_pixels":
        DAILY_COLUMNS.extend(DETECTION_COLUMNS)
    else:
        DAILY_COLUMNS.append(column)

# Separate passes prevent the CIcyano winner from selecting another index.
INDEX_PRODUCTS = {
    "CI": "CI_stumpf", "CI_cyano": "CI_cyano",
    "NDCI": "CT_NDCI", "MPH": "CT_MPH", "FAI": "CT_FAI",
}
DIAGNOSTIC_PRODUCTS = {
    "support": "CT_support", "clear": "CT_clear",
    "cloud": "CT_cloud", "ci_input": "CT_ci_input",
}
CUSTOM_PRODUCTS = {
    "CT_NDCI": ("Normalized difference chlorophyll index", -1e10, 1e10),
    "CT_MPH": ("OLCI maximum peak height", -1e10, 1e10),
    "CT_FAI": ("OLCI adapted floating algae index", -1e10, 1e10),
    **{name: (name.removeprefix("CT_") + " observation support", 0, 1)
       for name in DIAGNOSTIC_PRODUCTS.values()},
}
