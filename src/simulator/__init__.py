"""Simulated homestead: an MQTT site simulator for the Homestead Digital Twin.

SDD section 20 (Phase A) requires a simulated MQTT environment *before* property
deployment, section 19 makes bench testing step 1 of commissioning, and section
49 work-queue item 8 requires the EMS state machine to be prototyped against
simulated telemetry before any physical control is enabled. This package is that
rig.

The simulator owns no wire format. Topics come from
:mod:`homestead_twin.topics` and payloads from :mod:`homestead_twin.envelope`,
so anything the simulator publishes is by construction something the ingest
pipeline can resolve.

Layout::

    clock.py        virtual time (a 24 h day in milliseconds, or real time)
    components/     deterministic physical models (solar, battery, ...)
    site.py         wiring, energy balance, publishing, command handling
    scenarios.py    named reproducible scenarios for SDD section 39 cases
    cli.py          ``homestead-simulator`` entry point

Determinism is a hard requirement: no component reads the wall clock, every
random draw comes from a seeded generator, and a given ``(scenario, seed)`` pair
always produces the same telemetry.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.4.0"
