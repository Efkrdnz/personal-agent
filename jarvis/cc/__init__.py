"""Process P2 — the Claude Code driver, and nothing else.

One OS process per job. It hosts ``can_use_tool`` and the hooks, it writes rows,
and it exits. It never speaks, never opens an audio device, and never imports
``google.genai`` or a UI toolkit: the whole architecture rests on that seam, and
a seam retrofitted later is a rewrite.

This package is deliberately EMPTY of imports. ``jarvis.cc.policy``,
``jarvis.cc.settings`` and ``jarvis.cc.gate`` are pure standard-library-plus-spine
and must stay importable on a machine that has never installed
``claude-agent-sdk``; only :mod:`jarvis.cc.permission_host` and
:mod:`jarvis.cc.driver` reach the SDK, and they do it through the single seam in
:mod:`jarvis.cc.sdk`. Importing the SDK here would drag it into every one of
them, and the first symptom would be a phone worker that cannot import a pure
numbering function.
"""

from __future__ import annotations
