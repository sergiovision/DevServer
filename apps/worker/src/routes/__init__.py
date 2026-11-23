"""HTTP routers for the worker, mounted by ``main.py`` under ``/internal`` (plus health).

The worker is unauthenticated by design: every route here is trusted and must
only be reachable from the web tier or localhost, never exposed publicly.
"""
