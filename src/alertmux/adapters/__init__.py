"""Adapter registry.

Adding a source means one new module plus three edits in this file:
the import, the __all__ entry, and the default_adapters() return.
See the "Adding a source" section of the README.
"""

from alertmux.adapters.swic import SwicAdapter
from alertmux.adapters.usgs import UsgsAdapter

__all__ = ["SwicAdapter", "UsgsAdapter", "default_adapters"]


def default_adapters():
    """Every adapter enabled by default."""
    return [SwicAdapter(), UsgsAdapter()]
