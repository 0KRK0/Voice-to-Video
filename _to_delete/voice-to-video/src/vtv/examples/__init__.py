"""Worked examples of the contracts, end to end.

These are not toys. A hand-built, fully-consistent project graph is the cheapest
possible integration test for a set of contracts: if the twelve schemas cannot be
assembled into one valid video by hand, they will certainly not be assembled into
one by a language model at three in the morning.

The examples also become the development fixtures for every later stage. The
Storyboard UI can be built against ``transistor_project()`` before the Visual
Director exists; the renderer can be built against its timeline before any
generation provider is wired up. Being able to develop each stage without
spending money on the stages before it is worth a great deal.
"""

from __future__ import annotations

from vtv.examples.transistor import ExampleProject, transistor_project

__all__ = ["ExampleProject", "transistor_project"]
