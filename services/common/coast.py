"""Where a coastal city's coast actually is, and what the system still cannot
say about the water.

`coastal: true` in data/cities.yml started life as an unchecked assertion. It
decides whether surfing, swimming, fishing, a boat ride and a beach day are
scored for a city at all, and nothing in the repository said which coast was
meant or how far it was from the point the forecast is taken at. A reviewer
reading that Rome is coastal has to take it on trust, and taking it on trust is
exactly how a forecast measured 25 km inland of Lido di Ostia came to carry a
surf verdict.

So every coastal city now names a reference point on its coast, and the
distance from its forecast point to that reference is *derived* here rather
than written down anywhere. A committed distance is a number that goes stale
the moment somebody nudges a coordinate; a derived one cannot.

The second half of this module is the sentence that has to travel with every
coastal score. The distance is evidence about where the forecast was taken. It
is not evidence about the sea: nothing this system ingests measures wave
height, swell period or water temperature, so no answer it gives may imply
otherwise. `sea_state_unmeasured` in data/activities.yml marks the activities
that claim would apply to, and `score_ceiling` stops their score climbing into
a band that would read as a recommendation.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import asin, cos, radians, sin, sqrt
from typing import Any

# The IUGG mean radius. Any of the usual values is fine at this precision --
# the distances here are tens of kilometres and the point of them is "the
# forecast point is not on the beach", not survey-grade accuracy.
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two decimal-degree points.

    The haversine formula, on a sphere. It is deliberately a small pure
    function with no dependency: the alternative was a geodesy library in three
    images for one number per city, and an air-gapped build pays for every
    dependency twice -- once to stage it and once to explain it.

    Accurate to roughly 0.5% against an ellipsoidal calculation, which over the
    25 km that separates Rome from its coast is about a hundred metres. That is
    far below the precision any wording here claims.
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = radians(lon2 - lon1)
    h = sin(delta_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def format_km(km: float) -> str:
    """A distance a person reads, not a float a machine prints.

    Under ten kilometres the first decimal is the difference between "on the
    beach" and "a bus ride", so it is kept; above that it is noise, and
    "24.66 km" only pretends to a precision the reference point does not have.
    """
    return f"{km:.1f} km" if km < 10 else f"{km:.0f} km"


def sea_state_caveat(city: Mapping[str, Any], *, lead: str = "These scores") -> str:
    """The sentence that must appear wherever a sea-dependent score is shown.

    It states two things a suitability score cannot state for itself: which
    point on the map the forecast behind it describes, and that the water is
    not part of that forecast. `lead` exists only so the sentence can follow
    another one without repeating its subject.

    A city with no stored coast reference -- an inland one, or a row written
    before migration 007 -- still gets the second half. The claim about the
    water is true whether or not the distance is known, and dropping the whole
    caveat because one column is null would be the wrong way to fail.
    """
    name = city.get("name") or city.get("id") or "this city"
    coast_name = city.get("coast_name")
    distance = city.get("coast_distance_km")
    if coast_name and distance is not None:
        where = f"the {name} forecast point, {format_km(float(distance))} from {coast_name}"
    else:
        where = f"the {name} forecast point"
    return (
        f"{lead} rate the stored forecast for {where} -- nothing in the data "
        "measures the waves, the swell or the water temperature."
    )
