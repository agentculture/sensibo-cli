"""Unit-tag derivation for reading fields.

The one trap this module exists to defuse (see ``docs/sensibo-api.md``,
"Trap 1: pm25 is polymorphic"): the JSON key ``pm25`` means two different
things depending on the reporting pod's ``productModel``:

* **Pure** — an air-quality *index enum* (0-3), tagged ``"aqi"``.
* **Elements** — a real concentration in micrograms per cubic metre, tagged
  ``"ug/m3"``.

Storing both under one unit-less column would silently corrupt history, so
every write path threads ``product_model`` through to here before persisting.

Beyond ``pm25``, a small map of well-known fields gets a sensible tag
(temperature in Celsius, humidity in percent, etc). Anything else is
deliberately left untagged (``None``) rather than guessed — the collector is
designed around "whatever fields this pod reports", never a hardcoded field
universe, and a wrong unit is worse than a missing one.
"""

from __future__ import annotations

#: Fields whose unit is fixed regardless of product model.
_KNOWN_UNITS: dict[str, str] = {
    "temperature": "C",
    "feelsLike": "C",
    "humidity": "%",
    "tvoc": "ppb",
    "co2": "ppm",
    "battery": "%",
    # Room Sensors report a raw cell voltage in millivolts alongside the
    # percentage. Leaving it untagged next to `battery` is exactly the
    # ambiguity `unit` exists to prevent, so it is fixed for every model.
    "batteryVoltage": "mV",
}

#: pm25's unit depends on productModel — see the module docstring.
_PM25_UNIT_BY_MODEL: dict[str, str] = {
    "pure": "aqi",
    "elements": "ug/m3",
}


def derive_unit(field: str, product_model: str | None) -> str | None:
    """Return the unit tag for ``field``, or ``None`` if it isn't known.

    ``product_model`` (e.g. ``"pure"``, ``"elements"``, ``"airq"``) only
    matters for the fields whose unit is polymorphic — currently just
    ``pm25``. Matching is case-insensitive since Sensibo's API casing for
    ``productModel`` is not itself guaranteed stable across endpoints.
    """
    if field == "pm25":
        if not product_model:
            return None
        return _PM25_UNIT_BY_MODEL.get(product_model.lower())
    return _KNOWN_UNITS.get(field)
