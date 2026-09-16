"""The second channel: Telegram. Buttons, asynchronous, text.

The desk channel is voice — live, spoken, synchronous. This one is the opposite
in every affordance it has, and that is the point of building it second: a
:class:`~jarvis.requests.Presentation` that renders correctly here and at the
desk, and an :class:`~jarvis.requests.Answer` that both can post through the same
compare-and-swap, is a Presentation the phone will not need a new shape for.

Everything here is standard library. The Bot API is HTTPS and JSON, so
``urllib.request`` covers it, and the whole channel is therefore importable and
testable on a machine with no token, no network and no third-party package.

The seam that makes that true is :class:`jarvis.telegram.transport.Transport`:
one protocol, an HTTP implementation nothing in the test suite ever constructs
against a real host, and a fake that records what was sent and replays scripted
updates. api.telegram.org is unreachable from CI by design here, so a test that
could reach it would be a test that only runs on one machine.
"""

from __future__ import annotations
