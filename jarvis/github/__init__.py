"""The GitHub REST API as a seam, and the honest capability matrix behind it.

Stage 4 creates a repository BEFORE it writes any code, and repo creation is the
one side effect in this system that cannot be taken back: the token deliberately
has no ``delete_repo`` scope. Everything in this package exists to make that
honest rather than surprising.

Three modules, in dependency order:

:mod:`jarvis.github.transport`
    One :class:`~jarvis.github.transport.Transport` protocol, an HTTP client on
    ``urllib.request``, and a fake that records what was sent and replays
    scripted responses. api.github.com is never touched by a test.
:mod:`jarvis.github.scopes`
    The capability matrix, read NON-DESTRUCTIVELY out of response headers, and
    the spoken line GENERATED from it. Tri-state per operation, because "I could
    not tell" is a real answer and collapsing it either way is how the spoken
    line starts lying.
:mod:`jarvis.github.repos`
    ``create`` plus the three compensations — ``archive``, ``rename``,
    ``set_private`` — and ``exists`` for the collision check.

Everything here is standard library, for the same reason :mod:`jarvis.telegram`
is: the REST API is HTTPS and JSON, so ``urllib.request`` covers it and this
package is importable and testable on a machine with no token and no network.

It imports the spine (:mod:`jarvis.effects` owns the promise vocabulary the
spoken line is checked against) and nothing else in the tree. Never the reverse:
the spine must keep importing under ``python -S``.
"""

from __future__ import annotations
