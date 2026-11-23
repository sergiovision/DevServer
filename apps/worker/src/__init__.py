"""DevServer worker package: FastAPI app, PgQueuer consumer and task runner.

``main.py`` puts this directory on ``sys.path``, so modules import each other as
top-level packages (``config``, ``routes``, ``services``, ``models``).
"""
