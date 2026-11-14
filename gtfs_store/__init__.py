"""Small, local GTFS static store and service-date expansion engine.

The package intentionally uses only the Python standard library and SQLite.
See README.md for the data contract and command line entry points.
"""

from .errors import GtfsError, RejectError

__version__ = "1.0.0"

__all__ = ["GtfsError", "RejectError", "Store", "__version__"]


def __getattr__(name: str):
    if name == "Store":
        from .store import Store
        return Store
    raise AttributeError(name)
