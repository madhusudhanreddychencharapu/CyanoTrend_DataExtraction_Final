"""Original notebook columns plus approved NASA count and lake attributes."""

NOTEBOOK_COLUMNS = ['scene_id', 'scene_name', 'acquisition_start', 'Hylak_id', 'Lake_name', 'Lake_area_km2', 'native_lake_pixels', 'primary_retained_pixels', 'generic_flag_exclusion_sensitivity_pixels', 'bloom_rescued_pixels', 'CLDICE_pixels', 'cloud_excluded_pixels', 'bloom_rescue_fraction_of_CLDICE', 'ci_input_valid_pixels', 'ci_candidate_pixels', 'ci_cyano_detection_pixels', 'CI_valid_pixels', 'CI_mean', 'CI_median', 'CI_p10', 'CI_p90', 'CI_max', 'CI_cyano_valid_pixels', 'CI_cyano_mean', 'CI_cyano_median', 'CI_cyano_p10', 'CI_cyano_p90', 'CI_cyano_max', 'NDCI_valid_pixels', 'NDCI_mean', 'NDCI_median', 'NDCI_p10', 'NDCI_p90', 'NDCI_max', 'MPH_valid_pixels', 'MPH_mean', 'MPH_median', 'MPH_p10', 'MPH_p90', 'MPH_max', 'FAI_valid_pixels', 'FAI_mean', 'FAI_median', 'FAI_p10', 'FAI_p90', 'FAI_max']
NASA_DETECTION_COLUMN = "NASA_CI_Cyano_detectable_cells"
COLUMNS = NOTEBOOK_COLUMNS.copy()
COLUMNS.insert(COLUMNS.index("CI_valid_pixels"), NASA_DETECTION_COLUMN)
COLUMNS += ["state_name", "state_id", "latitude", "longitude"]

COMPARISON_LOCAL_COLUMNS = [
    "scene_id", "scene_name", "acquisition_start", "Hylak_id", "Lake_name",
    "Lake_area_km2", "native_lake_pixels", "primary_retained_pixels",
    "CLDICE_pixels", "bloom_rescued_pixels", "ci_candidate_pixels",
    "ci_cyano_detection_pixels",
]
NASA_MEASUREMENT_COLUMNS = [
    "NASA_CYAN_Lake_area_km2", "NASA_CYAN_Lake_pixels",
    "NASA_CYAN_Non_detectable_pixels", NASA_DETECTION_COLUMN,
    "NASA_CYAN_Land_pixels", "NASA_CYAN_No_valid_data_pixels",
]
NASA_TRACE_COLUMNS = [
    "NASA_CYAN_date", "NASA_CYAN_source_files", "NASA_CYAN_tile_ids",
    "NASA_CYAN_coverage_status", "NASA_CYAN_status_reason",
]
COMPARISON_COLUMNS = COMPARISON_LOCAL_COLUMNS + NASA_MEASUREMENT_COLUMNS + NASA_TRACE_COLUMNS
