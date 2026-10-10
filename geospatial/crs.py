"""Coordinate-reference-system rules.

Distances and areas are never computed in a geographic CRS (degrees). The
metric CRS is chosen as: METRIC_CRS_OVERRIDE_EPSG if set, otherwise British
National Grid (EPSG:27700) for data inside Great Britain, otherwise the local
UTM zone (EPSG:326xx north / 327xx south).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Common geographic (degree-based) CRSs.
GEOGRAPHIC_EPSG = {4326, 4258, 4269, 4230, 4277, 4167, 4283, 4612, 4617, 4674, 4686, 4805}
GB_BOUNDS = (-8.75, 49.75, 1.95, 60.95)  # lon_min, lat_min, lon_max, lat_max (approx. extent of 27700)
BRITISH_NATIONAL_GRID = 27700


@dataclass
class BBox:
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    @property
    def centre(self) -> tuple[float, float]:
        return (self.lon_min + self.lon_max) / 2, (self.lat_min + self.lat_max) / 2

    def is_valid_lonlat(self) -> bool:
        return -180 <= self.lon_min <= self.lon_max <= 180 and -90 <= self.lat_min <= self.lat_max <= 90

    def overlaps(self, other: "BBox", tolerance: float = 0.0) -> bool:
        return not (self.lon_max + tolerance < other.lon_min or other.lon_max + tolerance < self.lon_min
                    or self.lat_max + tolerance < other.lat_min or other.lat_max + tolerance < self.lat_min)

    def within(self, bounds: tuple[float, float, float, float]) -> bool:
        lon_min, lat_min, lon_max, lat_max = bounds
        return self.lon_min >= lon_min and self.lat_min >= lat_min and self.lon_max <= lon_max and self.lat_max <= lat_max


def is_geographic(srid: int | None) -> bool:
    return srid in GEOGRAPHIC_EPSG


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180) / 6)) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def recommend_metric_crs(bbox: BBox | None, override: int | None = None,
                         british_epsg: int = BRITISH_NATIONAL_GRID) -> tuple[int, str]:
    """Return (EPSG code, reason) for distance/area calculations."""
    if override:
        return override, f"METRIC_CRS_OVERRIDE_EPSG is set to {override}."
    if bbox is None or not bbox.is_valid_lonlat():
        return british_epsg, "Extent unknown; defaulting to British National Grid. Check the data's location."
    if bbox.within(GB_BOUNDS):
        return british_epsg, "Data lies within Great Britain, so British National Grid (metres) is used."
    lon, lat = bbox.centre
    span = bbox.lon_max - bbox.lon_min
    code = utm_epsg(lon, lat)
    reason = f"Data centred on {lat:.2f}, {lon:.2f}; UTM zone EPSG:{code} (metres)."
    if span > 12:
        reason += " The extent spans more than two UTM zones, so distances near the edges carry more error; " \
                  "consider geography-type calculations instead."
    return code, reason


def coordinate_warnings(srid: int | None, xmin: float | None, ymin: float | None,
                        xmax: float | None, ymax: float | None) -> list[str]:
    """Detect common CRS mistakes from a layer's native extent."""
    warnings: list[str] = []
    if None in (xmin, ymin, xmax, ymax):
        return warnings
    if not srid:
        warnings.append("The layer has SRID 0 (no CRS recorded). Set the correct SRID before spatial analysis.")
    if is_geographic(srid):
        if max(abs(xmin), abs(xmax)) > 180 or max(abs(ymin), abs(ymax)) > 90:
            warnings.append(
                f"SRID {srid} is geographic but coordinates exceed ±180/±90; the data are probably projected "
                "(e.g. metres) and labelled with the wrong SRID."
            )
        elif 49 <= xmin and xmax <= 61 and -9 <= ymin and ymax <= 2.5:
            warnings.append(
                "Coordinates look like UK latitude/longitude stored in the wrong order (x holds latitude). "
                "Longitude must be x and latitude y."
            )
        elif max(abs(ymin), abs(ymax)) <= 90 and max(abs(xmin), abs(xmax)) <= 90 and xmin > ymin + 30:
            warnings.append("x values are much larger than y values; check that longitude and latitude are not swapped.")
    elif srid == BRITISH_NATIONAL_GRID:
        if max(abs(xmax), abs(ymax)) < 1000:
            warnings.append("SRID 27700 expects metres, but coordinates are tiny; the data are probably in degrees.")
        elif not (-100_000 <= xmin and xmax <= 800_000 and -100_000 <= ymin and ymax <= 1_400_000):
            warnings.append("Coordinates fall outside the British National Grid range; check the SRID.")
    return warnings
