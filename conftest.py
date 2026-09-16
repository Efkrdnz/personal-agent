"""Make the repository root importable when the package is not installed.

pytest prepends the directory of the topmost ``conftest.py`` to ``sys.path``, so
this file existing is what lets ``from jarvis import ...`` work in a checkout
that was never ``pip install -e``'d. The spine has no dependencies outside the
standard library, and running its tests should not need a build step either.
"""
