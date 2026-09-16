"""Source adapters.

Each adapter exposes one job: turn an upstream payload into raw entries that
:mod:`kya.normalize` can turn into :class:`kya.models.Settlement` objects.
Adapters never invent data - anything they cannot read is reported as a
warning on the record rather than guessed at.

Sourcing policy (see README): an aggregator index is the *discovery* layer, and
the official claim portal is the *authority*. Every record keeps both links so
a reader can always check the primary source.
"""

from __future__ import annotations

SOURCE_OPENCLASSACTIONS = "openclassactions"

__all__ = ["SOURCE_OPENCLASSACTIONS"]