"""Point-based DXF georeferencing: locate a parsed local-frame FeatureCollection
in real-world coordinates using geodetic benchmark point labels (see
`greenplan.rules.default.yaml`'s `geodetic_points` rule) looked up against
geobridge.ru's point catalog.

See `transform.py` for the actual matching/fitting logic and `apply.py` for
applying the fitted transform to pipeline data structures.
"""
