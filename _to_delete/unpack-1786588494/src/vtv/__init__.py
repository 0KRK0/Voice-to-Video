"""Voice-to-Video: a visual intelligence system.

Speech goes in. Understanding, a story, and a coherent video come out.

This package is the engineering core. It is organised around one idea: the
*intelligence* — deciding what something means and how it should be shown — is
the product, and everything else (which model draws the picture, which cloud
stores the bytes, which library encodes the MP4) is replaceable infrastructure
kept behind an interface.

Layout::

    vtv.contracts   validated schemas for every artefact in the pipeline
    vtv.ports       interfaces to the outside world; no vendor code, ever

Adapters, services and the API arrive in later stages and depend on these two.
Nothing here depends on them.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: The stage of the roadmap this codebase has completed. Kept honest on purpose:
#: see ``docs/ROADMAP.md``.
__stage__ = 0

__all__ = ["__stage__", "__version__"]
