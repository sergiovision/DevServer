"""Flat service modules holding the worker's business logic (runner, git, memory, notify, ...).

Routes stay thin and call into these modules; each service opens its own DB
session per unit of work rather than holding one across subprocesses or sleeps.
"""
