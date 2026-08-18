"""Adapter registry.

Adding a source means one new module plus three edits in this file:
the import, the __all__ entry, and the default_adapters() return.
See the "Adding a source" section of the README.
"""

from alertmux.adapters.eonet import EonetAdapter
from alertmux.adapters.gdacs import GdacsAdapter
from alertmux.adapters.nws import NwsAdapter
from alertmux.adapters.swic import SwicAdapter
from alertmux.adapters.tsunami import TsunamiAdapter
from alertmux.adapters.usgs import UsgsAdapter

__all__ = [
    "EonetAdapter",
    "GdacsAdapter",
    "NwsAdapter",
    "SwicAdapter",
    "TsunamiAdapter",
    "UsgsAdapter",
    "default_adapters",
]


def default_adapters():
    """Every adapter enabled by default."""
    return [
        SwicAdapter(),
        UsgsAdapter(),
        NwsAdapter(),
        GdacsAdapter(),
        EonetAdapter(),
        TsunamiAdapter(),
    ]
