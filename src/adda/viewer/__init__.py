"""Public entry point for the live run viewer.

    python -m adda.viewer <study-dir>

Requires the ``viewer`` optional-dependency group
(``pip install adda[viewer]``). The real implementation lives in
``adda._src.viewer`` — this top-level package exists only so
``python -m adda.viewer`` resolves, mirroring ``adda/__main__.py``'s
own thin-forwarding convention for ``python -m adda``.

Deliberately NO eager import here (not even of ``create_app``/``run_viewer``):
Python always executes a package's ``__init__.py`` before any of its
submodules, so an eager Starlette import here would run BEFORE
``__main__.py``'s own try/except ever gets a chance to turn a missing
``viewer`` extra into a friendly message — it would surface as a raw
``ModuleNotFoundError`` instead. Import ``adda._src.viewer.app`` directly
for programmatic access.
"""
from __future__ import annotations
