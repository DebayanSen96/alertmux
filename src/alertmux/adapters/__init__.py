"""Adapter registry.

Adding a source means adding one module here and one entry below.
Nothing else changes.
"""

from alertmux.adapters.swic import SwicAdapter
from alertmux.adapters.usgs import UsgsAdapter

__all__ = ["SwicAdapter", "UsgsAdapter", "default_adapters"]


def default_adapters():
    """Every adapter enabled by default."""
    return [SwicAdapter(), UsgsAdapter()]
