"""Hand-computed cases for the NOAA clear-water correction and adjacency flag
(NOAA TM NOS NCCOS 296, Appendix C2 and B2)."""
import importlib.util
import unittest

AVAILABLE = all(importlib.util.find_spec(x) is not None for x in ("numpy", "netCDF4"))


def bands(**values):
    import numpy as np
    return {int(k[1:]): np.array([v], dtype=np.float32) for k, v in values.items()}


CLEAR = dict(r443=0.030, r490=0.028, r560=0.020, r620=0.006, r665=0.005, r709=0.004, r865=0.002)


@unittest.skipUnless(AVAILABLE, "numpy/netCDF4 not installed")
class ClearWater(unittest.TestCase):
    def mask(self, **values):
        from cyanotrend.core.science import clear_water_mask
        return bool(clear_water_mask(bands(**values))[0])

    def test_clear_lake_is_set_to_non_detect(self):
        # kd = 0.7*(0.0055-0.002)/(0.029-0.002) = 0.091 < 0.31; ss560 = 0.0059 < 0.01
        self.assertTrue(self.mask(**CLEAR))

    def test_turbid_or_bloom_water_is_kept(self):
        # kd = 0.7*(0.0275-0.010)/(0.0225-0.010) = 0.98 > 0.31
        self.assertFalse(self.mask(r443=0.020, r490=0.025, r560=0.050, r620=0.030,
                                   r665=0.025, r709=0.040, r865=0.010))

    def test_green_peak_is_kept(self):
        # Clear water but ss560 = 0.0259 >= 0.01 (algae green peak)
        self.assertFalse(self.mask(**{**CLEAR, "r560": 0.040}))

    def test_bright_nir_is_kept(self):
        # rho865 above rho490, rho665 and rho709 (floating material)
        self.assertFalse(self.mask(**{**CLEAR, "r865": 0.0295}))

    def test_threshold_is_published_value(self):
        from cyanotrend.core import settings
        self.assertEqual(settings.CI_CLEAR_WATER_KD_MAX, 0.31)
        self.assertAlmostEqual(settings.CI_DETECTION_LIMIT, 6.68e-5, places=7)


@unittest.skipUnless(AVAILABLE, "numpy/netCDF4 not installed")
class Adjacency(unittest.TestCase):
    def test_positive_ci_with_negative_mci_is_flagged(self):
        import numpy as np
        from cyanotrend.core.science import ci_adjacency_mask, mci_index
        r665, r681, r709, r754 = 0.030, 0.020, 0.015, 0.010
        ss681 = np.array([r681 - r665 - (r709 - r665) * 16 / 44])   # CI = -ss681 = 0.0045
        mci = mci_index(np.array([r681]), np.array([r709]), np.array([r754]))  # -0.0012
        self.assertLess(mci[0], 0)
        self.assertTrue(ci_adjacency_mask(ss681, np.array([False]), mci)[0])
        # After the clear-water correction CI is 0, so no adjacency flag.
        self.assertFalse(ci_adjacency_mask(ss681, np.array([True]), mci)[0])


if __name__ == "__main__":
    unittest.main()
