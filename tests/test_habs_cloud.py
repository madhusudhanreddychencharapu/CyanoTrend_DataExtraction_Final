"""Hand-computed cases for the OCSSW HABS cloud test (get_cloudmask_meris)."""
import importlib.util
import unittest

AVAILABLE = all(importlib.util.find_spec(x) is not None for x in ("numpy", "netCDF4"))
WAVES = (443, 490, 560, 620, 665, 681, 709, 754, 865)


def spectrum(values, cloud_albedo):
    import numpy as np
    rhos = {w: np.array([v], dtype=np.float32) for w, v in zip(WAVES, values)}
    return rhos, np.array([cloud_albedo], dtype=np.float32)


@unittest.skipUnless(AVAILABLE, "numpy/netCDF4 not installed")
class HabsCloud(unittest.TestCase):
    def mask(self, values, cloud_albedo, **kw):
        from cyanotrend.core.science import habs_cloud_mask
        rhos, ca = spectrum(values, cloud_albedo)
        return bool(habs_cloud_mask(rhos, ca, **kw)[0])

    def test_bright_flat_cloud(self):
        self.assertTrue(self.mask([0.30] * 9, 0.30))

    def test_turbid_water_is_not_cloud(self):
        # rho865 = 0.035 would be CLDICE (> 0.027); red hump removes it.
        self.assertFalse(self.mask([0.03, 0.04, 0.06, 0.07, 0.07, 0.068, 0.06, 0.04, 0.035], 0.035))

    def test_scum_exception(self):
        # Albedo 0.12 > 0.08, but green peak and red-edge/NIR excess un-flag it.
        self.assertFalse(self.mask([0.04, 0.05, 0.09, 0.06, 0.06, 0.055, 0.12, 0.14, 0.12], 0.12))

    def test_high_glint(self):
        self.assertTrue(self.mask([0.02, 0.02, 0.02, 0.01, 0.01, 0.01, 0.01, 0.01, 0.30], 0.02))

    def test_negative_band_is_not_evaluated(self):
        self.assertFalse(self.mask([-0.01] + [0.30] * 8, 0.30))

    def test_missing_cloud_albedo_uses_rhos865(self):
        self.assertTrue(self.mask([0.30] * 9, float("nan")))

    def test_rhos865_outside_valid_range(self):
        self.assertTrue(self.mask([0.03, 0.04, 0.06, 0.07, 0.07, 0.068, 0.06, 0.04, 0.035], 0.035, valid_max=0.03))


if __name__ == "__main__":
    unittest.main()
