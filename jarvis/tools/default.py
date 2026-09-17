"""The tools that exist, in one list, so "what can it do?" has one answer.

Assembled from the modules in :mod:`jarvis.tools.builtin` rather than
auto-discovered by walking the package. Import-time discovery means the tool
surface depends on which files happen to be present, which is how a half-finished
module becomes a capability the model offers on a phone call; this list is
reviewed the way a permission table should be.
"""

from __future__ import annotations

from jarvis.tools.builtin import code_build, status
from jarvis.tools.registry import Registry, Tool

__all__ = ["BUILTIN", "registry"]

BUILTIN: tuple[Tool, ...] = (code_build.tool, *status.TOOLS)


def registry(extra: tuple[Tool, ...] = ()) -> Registry:
    """A fresh registry per process. Never a module-level singleton.

    Rule 3: no module-level mutable state. Two registries in one process (the
    desk and a phone leg, say) must be able to differ without one of them
    mutating the other's table out from under it.
    """
    return Registry((*BUILTIN, *extra))
