"""Pytest plugin shipping jmaplib's fake JMAP server as reusable fixtures.

Registered via the ``pytest11`` entry point, so ``pip install jmaplib`` is enough
for a downstream project to get ``jmap_fake_server`` and ``jmap_client`` without
copying boilerplate.

The fixtures land with the fake transport in M2; until then this module exists so
the entry point resolves.
"""

from __future__ import annotations

__all__: list[str] = []
