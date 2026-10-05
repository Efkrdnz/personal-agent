"""The window: a local page that shows what every other process is doing.

It is its OWN OS process, started by ``python -m jarvis window``, and it talks to
the rest of Jarvis only through the database. The desk publishes a heartbeat
row and its transcript as events; the window reads them. The window asks the
desk to speak by writing a ``say`` command row; the desk claims it. Nothing here
holds a reference into another process's memory, so the window can start before
the desk, outlive it, or crash without taking a conversation down with it.

:mod:`jarvis.window.server` is the HTTP edge (security, routes, the event
stream); :mod:`jarvis.window.snapshot` turns rows into what the page shows and
never writes; :mod:`jarvis.window.launch` opens it as an app window.

Submodules are not imported here: the composition root imports what it uses,
and a bare ``import jarvis.window`` stays free.
"""
