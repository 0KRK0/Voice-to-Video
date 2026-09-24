"""A small, offline place-name gazetteer.

Deliberately tiny. A map is only worth drawing if the marker is in the right
place, so this contains only names whose coordinates are unambiguous. Anything
not listed here resolves to ``None`` and the Visual Director falls back to a
different strategy rather than putting a pin somewhere plausible.

A real geocoding provider belongs behind a port when one is available. Until
then, being able to say "I do not know where that is" is worth more than a
guess.
"""

from __future__ import annotations

#: name → (latitude, longitude, scope)
PLACES: dict[str, tuple[float, float, str]] = {
    # Continents and regions
    "africa": (0.0, 20.0, "world"),
    "asia": (34.0, 100.0, "world"),
    "europe": (54.0, 15.0, "continent"),
    "north america": (48.0, -100.0, "world"),
    "south america": (-15.0, -60.0, "world"),
    "australia": (-25.0, 133.0, "country"),
    "antarctica": (-82.0, 0.0, "world"),
    "middle east": (29.0, 45.0, "region"),
    # Countries
    "united states": (39.8, -98.6, "country"),
    "america": (39.8, -98.6, "country"),
    "united kingdom": (54.0, -2.0, "country"),
    "britain": (54.0, -2.0, "country"),
    "england": (52.4, -1.5, "country"),
    "scotland": (56.5, -4.2, "country"),
    "wales": (52.3, -3.7, "country"),
    "ireland": (53.4, -8.2, "country"),
    "france": (46.6, 2.3, "country"),
    "germany": (51.2, 10.4, "country"),
    "spain": (40.4, -3.7, "country"),
    "italy": (42.8, 12.5, "country"),
    "greece": (39.1, 21.8, "country"),
    "russia": (61.5, 105.3, "country"),
    "china": (35.9, 104.2, "country"),
    "japan": (36.2, 138.3, "country"),
    "india": (20.6, 79.0, "country"),
    "brazil": (-14.2, -51.9, "country"),
    "canada": (56.1, -106.3, "country"),
    "mexico": (23.6, -102.6, "country"),
    "egypt": (26.8, 30.8, "country"),
    "kenya": (-0.02, 37.9, "country"),
    "nigeria": (9.1, 8.7, "country"),
    "south africa": (-30.6, 22.9, "country"),
    "netherlands": (52.1, 5.3, "country"),
    "sweden": (60.1, 18.6, "country"),
    "norway": (60.5, 8.5, "country"),
    "switzerland": (46.8, 8.2, "country"),
    "korea": (35.9, 127.8, "country"),
    "indonesia": (-0.8, 113.9, "country"),
    # Cities
    "london": (51.51, -0.13, "city"),
    "paris": (48.86, 2.35, "city"),
    "berlin": (52.52, 13.40, "city"),
    "rome": (41.90, 12.50, "city"),
    "madrid": (40.42, -3.70, "city"),
    "amsterdam": (52.37, 4.90, "city"),
    "moscow": (55.76, 37.62, "city"),
    "tokyo": (35.68, 139.69, "city"),
    "beijing": (39.90, 116.41, "city"),
    "shanghai": (31.23, 121.47, "city"),
    "delhi": (28.61, 77.21, "city"),
    "mumbai": (19.08, 72.88, "city"),
    "bangalore": (12.97, 77.59, "city"),
    "cairo": (30.04, 31.24, "city"),
    "lagos": (6.52, 3.38, "city"),
    "nairobi": (-1.29, 36.82, "city"),
    "sydney": (-33.87, 151.21, "city"),
    "toronto": (43.65, -79.38, "city"),
    "new york": (40.71, -74.01, "city"),
    "san francisco": (37.77, -122.42, "city"),
    "silicon valley": (37.39, -122.08, "city"),
    "los angeles": (34.05, -118.24, "city"),
    "chicago": (41.88, -87.63, "city"),
    "boston": (42.36, -71.06, "city"),
    "seattle": (47.61, -122.33, "city"),
    "washington": (38.91, -77.04, "city"),
    "murray hill": (40.68, -74.40, "city"),  # Bell Labs
    "bell labs": (40.68, -74.40, "city"),
    "cambridge": (52.21, 0.12, "city"),
    "oxford": (51.75, -1.26, "city"),
    "geneva": (46.20, 6.14, "city"),
    "dubai": (25.20, 55.27, "city"),
    "singapore": (1.35, 103.82, "city"),
    "hong kong": (22.32, 114.17, "city"),
    "sao paulo": (-23.55, -46.63, "city"),
    "mexico city": (19.43, -99.13, "city"),
}

#: Widest scope wins when several markers are on one map: two cities on
#: different continents need a world view, not a city view.
_SCOPE_ORDER = ["city", "region", "country", "continent", "world"]


def lookup(name: str) -> tuple[float, float, str] | None:
    """Coordinates for a place name, or ``None`` if we do not know it."""
    key = " ".join(name.lower().replace(",", " ").split())
    if key in PLACES:
        return PLACES[key]
    # "the United States" / "Bell Labs in New Jersey" — try the longest
    # contained name rather than the first, so "new york" beats "york".
    matches = [candidate for candidate in PLACES if candidate in key]
    if matches:
        best = max(matches, key=len)
        return PLACES[best]
    return None


def widest_scope(scopes: list[str]) -> str:
    """The scope that contains all the given ones."""
    if not scopes:
        return "world"
    return max(scopes, key=lambda scope: _SCOPE_ORDER.index(scope) if scope in _SCOPE_ORDER else 0)


__all__ = ["PLACES", "lookup", "widest_scope"]
