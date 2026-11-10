"""Day stage: place processed scenes on NASA CyAN's grid and merge them per UTC day.

Scene processing (plan -> run) is unchanged. ``grid`` writes one NASA-grid file
per processed scene, beside its ZIP. ``merge`` builds the per-lake checklist of
scenes for each day and merges a lake only when every scene on its checklist is
processed. It reads saved files only; it never processes or queues scenes.
"""
