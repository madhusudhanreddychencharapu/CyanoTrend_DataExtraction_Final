"""Day stage: place processed scenes on ISIN bins and merge them per local solar date.

Scene processing (plan -> run) is unchanged. ``grid`` writes one ISIN bin file
per processed scene, beside its ZIP: each pixel is shared among the bins it
overlaps by area (area-weighted binning). ``merge`` builds the per-lake
checklist of scenes for each day and merges a lake only when every scene on its
checklist is processed or permanently failed (a failed scene gives a "partial"
row). It keeps the highest scene value per bin. It reads saved files only; it
never processes or queues scenes.
"""
