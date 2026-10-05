"""The application: one double-clickable Jarvis that runs every other process.

Everything below this package is a process somebody starts from a terminal —
``desk``, ``jarvis.schedule``, ``jarvis.telegram``, ``window``. A person who
double-clicks an icon has no terminal, so this layer is the one that starts
them, keeps them running, shows their failures in the HUD instead of a console,
and lets a second double-click bring the window back rather than start a second
copy of everything.

It sits at the top, beside the composition root: it may import every layer, and
no layer may import it. The decisions live here, in modules with tests; the
wiring that binds them to real callables lives in ``jarvis/__main__.py``.
"""
