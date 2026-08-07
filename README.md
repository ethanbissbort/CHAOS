# Homestead Digital Twin Machine-Readable Design Package — v0.3

Generated: 2026-08-06 14:51 America/Toronto  
Schema version: 0.3.0

## Purpose

This package executes the first two items in section 42 of the v0.2 software design document:

1. Convert the master asset/point dictionary into machine-readable YAML and JSON Schema.
2. Build the first asset register for the known rack, network, power-container, battery, inverter, PV, rack-power, NetBotz, and initial load-group assets.

The package preserves the design rule that **asset identity**, **point identity**, and **vendor/protocol binding** are separate records.

## File map

- `data/asset_class_dictionary.yaml` — domains, relationship types, lifecycle vocabularies, asset classes, and point profiles.
- `data/point_dictionary.yaml` — canonical point names, classes, types, units, quality conventions, and provenance status.
- `data/homestead_asset_register.yaml` — initial functional and physical asset register.
- `data/point_bindings.yaml` — binding skeleton for critical points; addresses remain `TBD` unless the source material supplied them.
- `data/*.json` — exact JSON mirrors of the YAML data.
- `schemas/*.schema.json` — JSON Schema Draft 2020-12 validation schemas.
- `tools/validate_bundle.py` — schema and cross-reference validator.
- `Homestead_Digital_Twin_Software_Design_Document_v0.3.md` — updated design-document pass and implementation notes.

## Coverage

The initial register includes:

- Primary homestead site and agrivoltaic/orchard zones.
- The 20-foot combined power, utilities, battery, and server container.
- Separate logical power, battery, and server environmental zones inside the container.
- Revised 45 kWdc agrivoltaic array target, four 10 kW hybrid-inverter functional positions, and the revised 800/640 kWh battery-bank functional position.
- Earlier 26.4 kWdc / 240 kWh design retained as an explicit unresolved predecessor conflict.
- PV combiner, disconnects, AC combiner, source transfer, critical/general panels, and backup generator placeholders.
- Primary 42U rack, APC UPS, switched/basic/reserve PDUs, and planned rack ATS.
- Dell R740xd, Dell T7820, Dell SC200 arrays, Cisco Catalyst 2960-X, Arista 7050QX, Cisco ISR 4321, Cisco WLC 5508, five Aironet 3702e APs, and four Cisco voice endpoints.
- Six approved VLANs as logical network assets.
- NetBotz/APC monitoring, rack access, rack camera, temperature/humidity, smoke, leak, and beacon assets.
- Core application-service functional positions for MQTT, registry, API, Node-RED, Home Assistant, historian, Grafana, Prometheus, OPNsense, Pi-hole, and CUCM.
- A physically separate secondary control-node requirement.
- Initial EMS load groups for the rack, cooling, battery HVAC, water, irrigation, greenhouse, spa, workshop, charging, and opportunistic compute.

## Deliberately unresolved

The register does **not** invent:

- IP addresses, MAC addresses, serial numbers, rack-unit positions, breaker numbers, wire sizes, Modbus registers, SNMP OIDs, Home Assistant entity IDs, or exact circuit ratings.
- Battery chemistry or module count for the revised 800/640 kWh target.
- Generator fuel type, power rating, model, and transfer architecture.
- Final PV electrical stringing or MPPT allocation.
- Exact measured rack, cooling, greenhouse, spa, workshop, or pump loads.

These appear under each asset's `open_fields` and in the design-conflict records.

## Validation

Install the two Python dependencies and run:

```bash
python -m pip install -r requirements.txt
python tools/validate_bundle.py
```

Expected result:

```text
OK schema: asset_class_dictionary.yaml
OK schema: point_dictionary.yaml
OK schema: homestead_asset_register.yaml
OK schema: point_bindings.yaml
OK cross-reference: ...
```

## Provenance markers

Dictionary entries use:

- `baseline_v0_2` — direct machine-readable translation of the v0.2 design document.
- `reconciled_v0_3` — explicit addition required to close an internal reference or represent source-supported equipment not yet formalized as a class/point.

Nothing marked `reconciled_v0_3` is presented as a previously finalized decision.
