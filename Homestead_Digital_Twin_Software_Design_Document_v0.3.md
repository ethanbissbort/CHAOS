# Homestead Digital Twin and Master Control Platform

**Software Design Document — Draft v0.3**  
**Project:** Long-Horizon Automated Off-Grid Homestead  
**Document date:** 2026-08-06  
**Status:** Machine-readable dictionaries, JSON Schemas, initial real asset register, and binding skeleton added

---

## Document Control

| Revision | Date | Scope | Status |
|---|---|---|---|
| v0.1 | 2026-08-06 | Initial architecture, subsystem scope, functional requirements, reliability position, and phased implementation | Superseded by v0.2 |
| v0.2 | 2026-08-06 | Adds asset/point naming standard, initial master point dictionary, energy operating-state machine, load-shedding and restoration logic, generator coordination, black-start sequence, alarms, and verification cases | Superseded by v0.3 |
| v0.3 | 2026-08-06 | Adds machine-readable YAML/JSON dictionaries, Draft 2020-12 JSON Schemas, the first rack/network/power-container asset register, point-binding skeleton, and automated validation | Current working draft |

### Source basis for this revision

This revision continues from the existing homestead planning portfolio, especially the rack and power infrastructure plan, the revised solar/battery design, the land-maintenance automation plan, the orchard and food-forest plan, the nitrogen-storage plan, the controlled-environment greenhouse plan, and the hydrothermal spa control requirements. Where the source material contains unresolved or conflicting values, the conflict is preserved as an open design decision rather than silently reconciled.

---

## 1. Executive Summary

The Homestead Digital Twin and Master Control Platform is the local-first operational control plane for the entire homestead. It will maintain a live software representation of the property, its buildings, infrastructure, equipment, crops, energy flows, water flows, environmental conditions, operating modes, maintenance state, alarms, and historical performance.

The platform is not merely a Home Assistant dashboard and not merely a three-dimensional model. It is a continuously updated operational model that connects four layers:

1. **The physical homestead:** sensors, meters, valves, pumps, relays, inverters, battery systems, servers, cameras, climate equipment, irrigation zones, machinery, tanks, storage systems, and buildings.
2. **The state model:** assets, relationships, current state, configuration, operating limits, dependencies, maintenance history, and location.
3. **The control layer:** schedules, rules, supervisory control, interlocks, load shedding, resource allocation, commands, overrides, and emergency modes.
4. **The historical and analytical layer:** time-series data, event history, trend analysis, yield tracking, energy and water accounting, predictive maintenance, simulation, and long-term planning.

The intended result is a homestead that remains operable without internet access, degrades safely when software or communications fail, and can be understood from one master interface without making every subsystem dependent on one central server.

---

## 2. Document Purpose

This Software Design Document defines the initial architecture, boundaries, requirements, data model, service layout, control philosophy, interfaces, failure behavior, and phased implementation plan for the homestead master monitoring and control system.

This draft is intended to become the software counterpart to the homestead master CAPEX, infrastructure, energy, food-production, communications, and building plans. It should eventually support procurement specifications, sensor schedules, control narratives, commissioning procedures, network diagrams, operating procedures, and maintenance documentation.

---

## 3. Current Homestead Scope Reflected in This Draft

The software platform is being designed around the following known or planned systems.

### 3.1 Energy and electrical infrastructure

- Ground-mounted agrivoltaic solar array.
- Current revised planning target of approximately 45 kWdc photovoltaic capacity.
- Four approximately 10 kW hybrid inverter units, yielding approximately 40 kW continuous inverter capacity.
- Current revised planning target of approximately 800 kWh nominal battery capacity and approximately 640 kWh usable planning capacity.
- Critical-load separation, with an identified 1.5 kW critical baseload planning target.
- Emergency generator input, transfer controls, surge protection, grounding, disconnects, and load shedding.
- Dedicated circuits for the server/network rack and separate treatment of rack cooling.

The project records also contain an older 12 kW PV / 40 kWh usable battery baseline. This draft treats the 45 kWdc / 800 kWh nominal system as the current revised design, but the discrepancy must be formally resolved and versioned rather than silently overwritten.

### 3.2 Power, utilities, battery, and server container

A 20-foot container is planned to house power/utilities/battery equipment and the homestead server rack. The rack includes enterprise compute, storage, networking, communications, UPS, switched PDU, and APC NetBotz environmental monitoring equipment.

Known rack and network elements include:

- 42U rack.
- Dell PowerEdge R740xd virtualization/storage host.
- Dell Precision T7820 auxiliary compute node.
- Dell Compellent SC200 storage array.
- Cisco Catalyst 2960-X.
- Arista 7050QX.
- Cisco ISR 4321.
- Cisco 5508 wireless LAN controller with multiple Aironet access points.
- Cisco Unified Communications Manager and wired/wireless IP phones.
- APC UPS, managed PDUs, rack ATS, NetBotz sensors, environmental monitoring, and rack access control.
- ZFS-backed storage for operational data, documentation, archives, and backups.

### 3.3 Property networking and communications

The currently approved VLAN scheme is retained:

| VLAN | Subnet | Purpose |
|---:|---|---|
| 10 | 10.10.10.0/24 | Management, out-of-band interfaces, PDUs, UPS, NetBotz, switches, access points |
| 20 | 10.10.20.0/24 | Servers and core services |
| 30 | 10.10.30.0/24 | Automation and IoT |
| 40 | 10.10.40.0/24 | Cameras and NVR |
| 50 | 10.10.50.0/24 | Guest Wi-Fi |
| 60 | 10.10.60.0/24 | Voice and telephony |

The platform must use these zones rather than flattening the property into one IoT network.

### 3.4 Water systems

- Well and pump system.
- Rainwater collection.
- Cisterns and distributed storage tanks.
- Water treatment and potable-water monitoring.
- Irrigation distribution and zone valves.
- Graywater collection and orchard reuse.
- Freeze protection and winter drainage.
- Leak monitoring.
- Hydrothermal spa pavilion with fresh-water, recirculation, heating, filtration, sanitation, humidity, and drying modes.
- Potential process water for greenhouse and hydrogel production.

### 3.5 Food production and land systems

- Orchard and food forest using perennial guilds, swales, ponds, and soil-building practices.
- Automated irrigation informed by soil moisture and local weather.
- Greenhouse and container-based controlled-environment agriculture.
- Experimental carrageenan–seaweed hydrogel growing substrate.
- Nutrient reservoirs, fertigation, pH and electrical-conductivity monitoring.
- Compost and aerated static pile monitoring.
- Robotic mowing and land-maintenance equipment.
- Weather station and distributed LoRa sensors.
- Beekeeping and possible avian systems as future modules.

### 3.6 Storage and process systems

- Nitrogen-displacement food storage in shipping containers.
- Pressure-swing adsorption nitrogen generation.
- Oxygen concentration monitoring.
- Container temperature and humidity control.
- Door access, purge state, alarm state, and ventilation interlocks.
- Workshop, tools, fabrication equipment, and consumables inventory.
- Future Aircela synthetic gasoline production subsystem.

### 3.7 Security and emergency systems

- Cameras and NVR.
- Gate, perimeter, building, rack, and container access monitoring.
- Motion, door, smoke, heat, leak, vibration, and environmental sensors.
- Local sirens, beacons, push/email notification, and telephone alerting.
- Emergency shutdowns and degraded operating modes.

---

## 4. Design Goals

### 4.1 Primary goals

1. Provide one coherent operational picture of the property.
2. Keep all essential control functions available without internet access.
3. Permit each critical subsystem to continue safe local operation if the central platform is unavailable.
4. Minimize repetitive labor through reliable automation.
5. Expose every important resource flow: energy, water, heat, fuel, nutrients, data, and food production.
6. Track the complete lifecycle of physical assets: planning, procurement, installation, commissioning, operation, maintenance, failure, replacement, and retirement.
7. Preserve operational history for multi-year optimization and planning.
8. Support manual override at the equipment or subsystem level.
9. Allow phased deployment without requiring the entire homestead to exist at once.
10. Prefer open protocols, local APIs, replaceable components, and inspectable logic.

### 4.2 Non-goals for the first release

- Fully autonomous strategic decision-making.
- A photorealistic three-dimensional simulation of the property.
- Dependence on a proprietary cloud platform.
- Automatic control of life-safety systems in ways that bypass listed local controllers.
- Replacing physical gauges, disconnects, interlocks, pressure relief, thermal protection, or equipment-native safety controls.
- Machine-learning control before deterministic control logic and high-quality data are established.

---

## 5. Core Architectural Principles

### 5.1 Local-first operation

The homestead must remain monitorable and controllable during internet loss. Remote cloud access is an optional extension, not the control path.

### 5.2 Distributed autonomy

No central software service should be able to turn a communications outage into a frozen greenhouse, overflowing tank, overheated battery room, or failed irrigation system. Local controllers must enforce hard limits and minimum viable operation.

### 5.3 Supervisory control rather than fragile central control

The central platform should set targets, schedules, priorities, and operating modes. Fast loops and safety loops belong in equipment-native controllers, PLCs, inverter/BMS logic, thermostats, pump controllers, and hardwired interlocks.

### 5.4 Explicit source of truth

Asset identity, location, configuration, dependencies, units, alarms, documentation, and maintenance state must be stored in a structured asset registry rather than scattered across dashboards and YAML files.

### 5.5 Fail safe and fail understandable

Every controlled output must have:

- A defined normal state.
- A defined communications-loss state.
- A defined power-loss state.
- A defined sensor-invalid state.
- A manual override method.
- A recovery procedure.

### 5.6 Versioned everything

Control logic, thresholds, dashboards, network rules, firmware, calibration records, equipment manuals, wiring diagrams, and asset definitions must be version-controlled or revision-tracked.

### 5.7 Human authority is explicit

Commands must record who or what issued them, why, when, under which operating mode, and whether the command was accepted, rejected, timed out, or overridden.

### 5.8 Cybersecurity is part of physical reliability

IoT devices cannot be trusted merely because they are on private land. The system will use VLAN separation, restrictive firewall rules, unique credentials, certificate-based service identity where practical, audit logs, and offline backups.

---

## 6. System Context and Control Hierarchy

```text
LEVEL 4 — Planning, analytics, simulation, forecasting
          Long-term energy/water/crop models, maintenance planning,
          budget linkage, seasonal optimization, digital twin scenarios

LEVEL 3 — Homestead master control plane
          Asset registry, event engine, supervisory rules, schedules,
          dashboards, command service, alerts, work orders, reports

LEVEL 2 — Subsystem coordinators
          Energy management, irrigation, greenhouse, water plant,
          container environment, security, nitrogen storage, spa controls

LEVEL 1 — Local controllers and gateways
          PLCs, inverter/BMS controls, pump controllers, ESP32 nodes,
          LoRaWAN nodes, relays, thermostats, valve controllers, NetBotz

LEVEL 0 — Physical process and hardwired protection
          Breakers, fuses, relief valves, float switches, contactors,
          mechanical stops, high-limit thermostats, emergency stops
```

The platform may request a pump start at Level 3, but Level 1 and Level 0 retain authority to reject that request because of dry-run, low tank level, freeze lockout, motor overload, open disconnect, or emergency stop.

---

## 7. Proposed Logical Architecture

```text
                               OPTIONAL REMOTE ACCESS
                         VPN / notification relay / backups
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    HOMESTEAD SERVER ENVIRONMENT                      │
│                                                                     │
│  ┌────────────────────┐     ┌────────────────────────────────────┐  │
│  │ Digital Twin API   │<--->│ PostgreSQL + PostGIS              │  │
│  │ Asset/state model  │     │ Assets, topology, config, work    │  │
│  └─────────┬──────────┘     └────────────────────────────────────┘  │
│            │                                                        │
│  ┌─────────▼──────────┐     ┌────────────────────────────────────┐  │
│  │ MQTT Broker        │<--->│ Time-series database              │  │
│  │ Telemetry/events   │     │ InfluxDB or TimescaleDB           │  │
│  └─────────┬──────────┘     └────────────────────────────────────┘  │
│            │                                                        │
│  ┌─────────▼──────────┐     ┌────────────────────────────────────┐  │
│  │ Node-RED / Rules   │<--->│ Home Assistant                    │  │
│  │ Supervisory logic  │     │ Device integration/operator UI   │  │
│  └─────────┬──────────┘     └────────────────────────────────────┘  │
│            │                                                        │
│  ┌─────────▼──────────┐     ┌────────────────────────────────────┐  │
│  │ Alert service      │     │ Grafana / map / reports           │  │
│  │ Email + push + VoIP│     │ Operational visualization        │  │
│  └────────────────────┘     └────────────────────────────────────┘  │
│                                                                     │
│  Prometheus / logs / audit / Git / object storage / backups         │
└─────────────────────────────────────────────────────────────────────┘
              │                 │                   │
              │ MQTT/HTTPS      │ SNMP/Modbus       │ LoRaWAN
              ▼                 ▼                   ▼
      Structure gateways   Power/rack systems   Field gateway
              │                 │                   │
       PLCs / ESP32 /       Inverter, BMS,      Soil, weather,
       relays / meters      UPS, PDU, NetBotz   compost, tanks
```

---

## 8. Proposed Software Components

### 8.1 Digital Twin Core Service

A custom local service should provide the authoritative asset and state model. A practical implementation is a Python/FastAPI application backed by PostgreSQL and PostGIS.

Responsibilities:

- Asset registry.
- Property and geospatial hierarchy.
- Device-to-asset mapping.
- Point definitions and engineering units.
- Relationships and dependencies.
- State aggregation.
- Command authorization and audit.
- Maintenance and inspection records.
- Document/manual references.
- Configuration revisions.
- API for dashboards and other services.

Home Assistant should not be the authoritative asset database. It can remain the best rapid-integration interface, but the digital twin core must preserve stable identity even when devices, integrations, or entity IDs change.

### 8.2 MQTT broker

MQTT is the primary lightweight event and telemetry bus for homestead automation.

Initial baseline:

- Eclipse Mosquitto.
- TLS for routed or untrusted links.
- Per-device or per-gateway credentials.
- Access-control lists by topic prefix.
- Retained messages for selected current-state topics.
- Last-will messages for node availability.
- Persistent session and message storage for critical delivery classes.

### 8.3 Home Assistant

Home Assistant provides:

- Rapid integration with supported devices.
- Operator controls.
- Mobile interface.
- Presence and occupancy context.
- Simple automations that are not process-critical.
- Device discovery and prototyping.

It is not the only automation engine and should not host hard safety logic.

### 8.4 Node-RED

Node-RED provides:

- Visual supervisory workflows.
- Cross-subsystem orchestration.
- Protocol translation.
- Scheduled control.
- State-machine implementation.
- Alarm routing.
- Integration of legacy APIs.

Production flows must be exported to Git, reviewed, tagged, and tested.

### 8.5 Time-series database

The system requires high-resolution historical telemetry. InfluxDB is a strong initial fit; TimescaleDB is an alternative if a unified PostgreSQL stack is preferred.

Data classes include:

- Electrical production, consumption, state of charge, inverter state, power quality.
- Water levels, flow, pressure, quality, pump runtime.
- Temperature, humidity, CO₂, pH, EC, soil moisture, rainfall, wind, light.
- Equipment state, runtime, starts, faults, cycles, command history.
- Crop, harvest, soil, and treatment observations.
- IT power, temperatures, network availability, disk health, UPS state.

### 8.6 Visualization

- Grafana for time-series dashboards, alarms, comparisons, and energy/water views.
- A custom property map using PostGIS and MapLibre for geographic assets, zones, lines, tanks, valves, cameras, trees, beds, and equipment.
- Home Assistant for fast operational controls.
- Dedicated wall display views for normal operations and emergency status.

### 8.7 Infrastructure monitoring

Prometheus-compatible exporters, SNMP polling, and log collection should monitor:

- Servers and virtual machines.
- Network switches and access points.
- UPS and PDUs.
- NetBotz.
- Storage arrays and ZFS pools.
- Application services.
- Gateway health.
- Backup jobs.

### 8.8 Documentation and version control

- Git repository for code, configuration, infrastructure-as-code, dashboards, Node-RED flows, schemas, and control narratives.
- ZFS-backed document library for manuals, drawings, photos, commissioning records, and maintenance records.
- Automated configuration backups from network devices, controllers, and services.

---

## 9. Asset and Digital Twin Data Model

### 9.1 Hierarchy

```text
Homestead Site
├── Geographic Zones
│   ├── Orchard Zone 1
│   ├── Food Forest Zone 2
│   ├── Agrivoltaic Field
│   └── Perimeter Sector A
├── Structures
│   ├── Residence
│   ├── Power/Utilities/Server Container
│   ├── Workshop
│   ├── Greenhouse Container
│   ├── Hydrogel Lab
│   ├── Nitrogen Storage Container
│   └── Hydrothermal Spa Pavilion
├── Utility Networks
│   ├── Electrical
│   ├── Potable Water
│   ├── Rainwater
│   ├── Graywater
│   ├── Irrigation
│   ├── Data/Communications
│   └── Fuel/Gas
└── Assets
    ├── Systems
    ├── Equipment
    ├── Sensors
    ├── Actuators
    ├── Control Points
    └── Biological Assets
```

### 9.2 Asset classes

- Site.
- Zone.
- Structure.
- Room or enclosure.
- Utility network.
- Pipe, wire, conduit, or communication link.
- Source.
- Storage vessel.
- Converter.
- Pump.
- Fan.
- Valve.
- Meter.
- Sensor.
- Controller.
- Compute device.
- Security device.
- Machine or tool.
- Tree, shrub, bed, crop batch, guild, or hive.
- Consumable or spare part.

### 9.3 Required fields for every controlled asset

| Field | Meaning |
|---|---|
| `asset_id` | Stable, non-reused identifier |
| `asset_type` | Pump, tank, inverter, tree, room, sensor, etc. |
| `name` | Human-readable name |
| `parent_id` | Containment hierarchy |
| `location` | Coordinates, structure, room, rack unit, or zone |
| `status` | Planned, installed, commissioned, active, degraded, failed, retired |
| `criticality` | Life-safety, critical, important, discretionary |
| `control_authority` | Local-only, supervisory, manual, vendor-native |
| `dependencies` | Power, communications, upstream/downstream resources |
| `manual_override` | Location and procedure |
| `documentation` | Manual, drawing, photo, warranty, model, serial |
| `maintenance_plan` | Interval, counters, inspections, spare parts |
| `telemetry_points` | Mapped measurement topics |
| `command_points` | Mapped command topics/API endpoints |
| `limits` | Operating, warning, alarm, shutdown thresholds |

### 9.4 Example asset definition

```yaml
asset_id: water.pump.irrigation.main_01
asset_type: pump
name: Main Orchard Irrigation Pump
parent_id: water.system.irrigation
location:
  structure: pump_house
  coordinates: [TBD, TBD]
criticality: important
status: active
control_authority: local_controller_with_supervisory_setpoints
dependencies:
  - power.panel.critical.irrigation
  - water.cistern.irrigation_01
  - network.gateway.pump_house
telemetry:
  discharge_pressure_kpa: chaos/water/irrigation/pump01/pressure
  flow_lpm: chaos/water/irrigation/pump01/flow
  current_a: chaos/water/irrigation/pump01/current
  state: chaos/water/irrigation/pump01/state
commands:
  requested_mode: chaos/water/irrigation/pump01/cmd/mode
fail_states:
  communication_loss: continue_local_schedule
  low_source_level: stop_and_lockout
  freeze_condition: inhibit_start
manual_override:
  type: local_hand_off_auto_switch
  location: pump_house_control_panel
```

---

## 10. Telemetry and Command Conventions

### 10.1 Topic convention

```text
chaos/<domain>/<site-or-structure>/<asset>/<point>
chaos/<domain>/<site-or-structure>/<asset>/cmd/<command>
chaos/<domain>/<site-or-structure>/<asset>/event/<event-type>
chaos/<domain>/<site-or-structure>/<asset>/availability
```

Examples:

```text
chaos/energy/power_container/battery_bank/soc_pct
chaos/energy/power_container/inverter_01/ac_output_kw
chaos/water/orchard/cistern_01/level_pct
chaos/agriculture/greenhouse_01/climate/temperature_c
chaos/storage/nitrogen_container/o2_sensor_01/o2_pct
chaos/it/rack_01/netbotz/temperature_c
chaos/security/perimeter/gate_01/event/opened
```

### 10.2 Standard telemetry envelope

```json
{
  "ts": "2026-08-06T18:13:00Z",
  "asset_id": "energy.battery.bank_01",
  "point": "soc_pct",
  "value": 73.4,
  "unit": "%",
  "quality": "good",
  "source": "bms.master",
  "sequence": 182771,
  "schema_version": 1
}
```

### 10.3 Command envelope

```json
{
  "command_id": "01J...",
  "issued_at": "2026-08-06T18:13:00Z",
  "issued_by": "rules.energy_manager",
  "asset_id": "load.greenhouse.hvac_01",
  "command": "set_mode",
  "value": "reduced_power",
  "reason": "battery_reserve_protection",
  "expires_at": "2026-08-06T20:13:00Z",
  "requires_ack": true
}
```

Commands must receive an acknowledgement and final result. The digital twin must distinguish requested state, accepted state, and actual measured state.

---

## 11. Operating Modes

Every major subsystem should expose a standard mode set:

1. **Off:** intentionally unavailable.
2. **Manual:** local or operator-directed control.
3. **Automatic:** normal local control with supervisory targets.
4. **Scheduled:** operation according to a defined calendar.
5. **Maintenance:** alarms modified, automatic starts inhibited, lockout visible.
6. **Degraded:** reduced capability because of failed sensor, communications, power, or equipment.
7. **Emergency:** predefined protective state; discretionary loads shed.

Mode transitions must be logged. Emergency mode must not be cleared automatically unless the triggering condition and reset policy explicitly permit it.

---

## 12. Functional Requirements

### 12.1 Platform-wide requirements

- **FR-001:** The platform shall provide a single property overview showing subsystem health, active alarms, energy reserve, water reserve, communications status, and current operating mode.
- **FR-002:** The platform shall maintain a stable asset identity independent of vendor integrations and device replacement.
- **FR-003:** The platform shall record telemetry with timestamps, units, quality flags, and source identity.
- **FR-004:** The platform shall record every supervisory command and its acknowledgement or failure.
- **FR-005:** The platform shall support manual, automatic, maintenance, degraded, and emergency states.
- **FR-006:** The platform shall function on the local network without internet access.
- **FR-007:** The platform shall support email and push alerts, with optional CUCM/voice escalation for critical events.
- **FR-008:** The platform shall link each alarm to an operating procedure, affected assets, dependencies, and manual controls.
- **FR-009:** The platform shall track calibration, inspection, runtime, cycle count, maintenance, and replacement dates.
- **FR-010:** The platform shall export historical data in open formats.

### 12.2 Energy management

- **FR-100:** Collect PV production, inverter state, battery state of charge, battery temperature, battery current, AC load, generator state, breaker/disconnect state where instrumented, and critical-load consumption.
- **FR-101:** Compute energy balance, reserve time, forecast reserve, and subsystem consumption.
- **FR-102:** Execute tiered load shedding according to criticality and reserve thresholds.
- **FR-103:** Prevent discretionary loads from restarting simultaneously after an outage.
- **FR-104:** Coordinate generator start requests through the equipment-native generator/inverter interface.
- **FR-105:** Preserve power to communications, control, refrigeration, water protection, and critical food-production loads before discretionary comfort loads.
- **FR-106:** Allow operator-defined temporary priorities, such as workshop operation during a high-solar window.

### 12.3 Water management

- **FR-200:** Track level, flow, pressure, temperature, pump runtime, filter condition, and leak state for each water subsystem.
- **FR-201:** Maintain source-to-use accounting for well, rainwater, potable, irrigation, greenhouse, graywater, and spa water.
- **FR-202:** Detect probable leaks using unexpected flow, pressure decay, tank-level loss, and occupancy/schedule context.
- **FR-203:** Inhibit pumps on dry-run, low-source level, freeze lockout, overcurrent, or unavailable discharge path.
- **FR-204:** Support winterization state for outdoor lines and equipment.
- **FR-205:** Preserve a configurable emergency potable-water reserve.

### 12.4 Orchard, food forest, and irrigation

- **FR-300:** Represent every irrigation zone, valve, soil sensor, tree group, swale, pond, and water source geographically.
- **FR-301:** Use soil moisture, rainfall, forecast inputs, crop stage, and seasonal limits to request irrigation.
- **FR-302:** Enforce local maximum runtime, minimum rest period, and low-flow/high-flow fault detection.
- **FR-303:** Track planting, cultivar, rootstock, planting date, treatment, pruning, disease, yield, and mortality.
- **FR-304:** Maintain calibration curves and quality flags for soil sensors.
- **FR-305:** Permit manual watering when automation is unavailable.

### 12.5 Greenhouse and controlled-environment agriculture

- **FR-400:** Monitor temperature, relative humidity, dew point, CO₂, light, reservoir level, water temperature, pH, EC, flow, and equipment state.
- **FR-401:** Control lighting schedules, pumps, fans, heating, cooling, humidification, dehumidification, and nutrient delivery through local controllers.
- **FR-402:** Implement freeze protection independent of the central server.
- **FR-403:** Track crop batches, germination, transplant, harvest, yield, nutrient recipe, and environmental exposure.
- **FR-404:** Track hydrogel batch composition, production date, reuse cycles, observed degradation, and crop performance.
- **FR-405:** Apply energy-aware operating profiles without violating minimum crop-survival limits.

### 12.6 Compost and soil systems

- **FR-500:** Monitor compost temperature, ambient conditions, blower current, and blower runtime.
- **FR-501:** Control aeration using local temperature logic with central optimization.
- **FR-502:** Flag overheating, sensor failure, blower failure, excessive drying, and abnormal cooling.
- **FR-503:** Record batch inputs, dates, temperatures, turning or aeration, and finished-compost disposition.

### 12.7 Nitrogen-displacement storage

- **FR-600:** Monitor oxygen concentration, nitrogen generator state, container pressure if applicable, temperature, humidity, door state, and ventilation state.
- **FR-601:** Prevent or suspend purge operation when access doors are open unless an explicit maintenance mode is active.
- **FR-602:** Raise local and remote alarms for oxygen concentration outside the defined safe access policy.
- **FR-603:** Track purge cycles, generator runtime, filter maintenance, food lot location, and container inventory.
- **FR-604:** Require local verification of safe atmosphere before normal occupied access is indicated.

### 12.8 Hydrothermal spa pavilion

- **FR-700:** Monitor fresh-water flow, recirculation flow, reservoir level, temperature, filter pressure differential, sanitizer state, humidity, floor temperature, leak state, and pump current.
- **FR-701:** Implement named operating modes including fresh wash, recirculation, mist, hydrotherapy, outdoor winter, sanitation, purge, drying, and maintenance.
- **FR-702:** Prevent heater operation without proven flow.
- **FR-703:** Prevent pump operation below minimum reservoir level.
- **FR-704:** Enforce post-use drying and dehumidification logic.
- **FR-705:** Record sanitation cycle completion and inhibit recirculation when sanitation status is invalid.

### 12.9 Datacenter and communications

- **FR-800:** Collect server, storage, network, UPS, PDU, NetBotz, rack access, room temperature, humidity, leak, smoke, and cooling data.
- **FR-801:** Coordinate graceful shutdown based on upstream power state and remaining energy reserve.
- **FR-802:** Permit controlled PDU outlet cycling through an audited command path.
- **FR-803:** Maintain communications and alerting services at a higher load priority than noncritical compute workloads.
- **FR-804:** Shift or suspend discretionary compute workloads based on energy availability and container thermal state.
- **FR-805:** Back up network-device configurations and core platform configuration automatically.

### 12.10 Security

- **FR-900:** Display cameras, gates, doors, perimeter sensors, and intrusion events on the property map.
- **FR-901:** Keep camera traffic in VLAN 40 and allow retrieval only through approved NVR/service paths.
- **FR-902:** Correlate door, motion, camera, access-card, and occupancy events.
- **FR-903:** Support local alarm, push/email, and voice escalation profiles.
- **FR-904:** Preserve event data during internet loss.

---

## 13. Energy-Aware Supervisory Control

The central energy manager should not directly switch every load. It should publish an energy operating state and load budget.

### 13.1 Proposed energy states

| State | Example condition | Behaviour |
|---|---|---|
| Surplus | Battery high and PV exceeds load | Permit deferred loads, charging, pumping, heating, compute jobs |
| Normal | Reserve healthy | Normal schedules |
| Conserve | Reserve declining | Reduce discretionary HVAC, defer workshop and compute tasks |
| Critical reserve | Low state of charge or poor forecast | Shed noncritical loads, preserve water/freeze/refrigeration/communications |
| Emergency | Inverter/BMS fault or reserve exhausted | Local survival modes, generator request, controlled shutdown |

### 13.2 Load tiers

- **Tier 0 — Physical protection:** battery controls, fire detection, freeze protection, emergency lighting, controller power.
- **Tier 1 — Essential services:** communications, digital twin core, potable-water controls, critical refrigeration, security, minimal greenhouse survival.
- **Tier 2 — Important operations:** normal food production, pumps, ventilation, rack cooling, routine household loads.
- **Tier 3 — Deferrable operations:** workshop machinery, hydrotherapy heating, bulk water heating, heavy compute, EV or equipment charging.
- **Tier 4 — Opportunistic surplus loads:** thermal storage charging, discretionary production, experiments, nonurgent processing.

---

## 14. Alarm Model

### 14.1 Severity

- **Info:** expected state change or advisory.
- **Warning:** attention required but no immediate loss of function.
- **Major:** subsystem degraded or resource at risk.
- **Critical:** immediate action required to prevent major damage or loss of essential service.
- **Emergency:** life-safety or rapidly escalating physical hazard; local alarms and protective actions execute independently.

### 14.2 Alarm lifecycle

```text
Detected → Active → Acknowledged → Mitigated → Cleared → Reviewed
```

The platform must prevent alarm floods by correlating root causes. A power-container outage should not create hundreds of separate notifications without a parent incident.

### 14.3 Alarm requirements

Every alarm definition must include:

- Trigger and reset logic.
- Delay and hysteresis.
- Severity.
- Affected assets.
- Probable causes.
- Automatic protective action.
- Operator action.
- Escalation path.
- Suppression conditions.
- Maintenance-mode behavior.

---

## 15. Security Architecture

### 15.1 Network placement

- Core applications: VLAN 20.
- Field and building gateways/controllers: VLAN 30.
- Cameras: VLAN 40.
- NetBotz, UPS, PDU, switch management: VLAN 10.
- Voice notification endpoints: VLAN 60.

Firewall policy should be deny-by-default. Typical permitted flows:

- VLAN 30 gateways to MQTT/API services in VLAN 20.
- VLAN 20 monitoring services to approved SNMP/Modbus endpoints.
- NVR in approved server/camera path to VLAN 40 cameras.
- Alert service to CUCM/voice services as required.
- No direct internet access for most controllers.

### 15.2 Identity and access

- Named operator accounts.
- Separate service accounts.
- Role-based permissions: viewer, operator, maintainer, administrator.
- Multi-factor authentication for remote administration.
- Short-lived or certificate-based credentials where supported.
- All control actions audited.

### 15.3 Remote access

Remote access should terminate through a VPN under user control. No port-forwarded Home Assistant, PLC, inverter, camera, NetBotz, or PDU interfaces.

---

## 16. Reliability and Failure Design

### 16.1 Common-mode risk: combined battery and server container

Housing batteries, power conversion, utilities, and the primary server rack in one 20-foot container creates a common physical failure domain. Fire, smoke, overheating, water ingress, electrical fault, or container HVAC failure could remove both the plant being controlled and the master controller.

The software design must therefore assume complete loss of that container.

Required mitigation:

1. Local controllers continue safe subsystem operation without the server rack.
2. A small secondary control node is installed in a separate structure.
3. Critical configurations and asset data are replicated outside the container.
4. Essential alerts can originate from independent local devices.
5. Battery and server zones are physically separated, monitored independently, and do not share a single uncontrolled airflow path.
6. The secondary node can provide a reduced dashboard, MQTT bridge, and emergency communications.

### 16.2 High-availability target

The first release does not require a complex active-active cluster. A practical design is:

- Primary application host on the R740xd.
- Secondary lightweight host on the T7820 or, preferably, a physically separate low-power industrial computer.
- Replicated PostgreSQL backups and time-series exports.
- MQTT configuration backup and optional warm standby.
- Independent local controllers for physical processes.
- UPS and controlled shutdown.

### 16.3 Time synchronization

All servers, gateways, PLCs, cameras, and field nodes must use the homestead NTP service. The NTP service should have an external source when available and a local holdover source when isolated.

### 16.4 Data retention

Initial policy:

- Raw high-frequency operational telemetry: 90 days.
- Downsampled 1-minute data: 2 years.
- Downsampled hourly/daily data: indefinite.
- Alarm and command audit: indefinite.
- Maintenance and asset history: indefinite.
- Camera footage: separate retention policy based on capacity and event priority.

---

## 17. User Interface Structure

### 17.1 Home screen

- Property operating state.
- Active critical and major alarms.
- Battery state of charge and estimated reserve.
- Current PV production and total load.
- Potable and irrigation water reserves.
- Weather and freeze/heat risk.
- Greenhouse and orchard status.
- Communications and server status.
- Security state.

### 17.2 Property map

The map should show:

- Structures.
- Utility lines.
- Water tanks and valves.
- Irrigation zones.
- Orchard blocks, tree identities, beds, and swales.
- Solar rows.
- Cameras and perimeter sectors.
- LoRa nodes and signal quality.
- Active alarms and maintenance work.

### 17.3 Domain dashboards

- Energy.
- Water.
- Orchard/food forest.
- Greenhouse.
- Compost/soil.
- Storage and nitrogen.
- Spa.
- Datacenter/network.
- Security.
- Maintenance and inventory.

### 17.4 Control presentation

Every control should show:

- Actual state.
- Requested state.
- Local/remote authority.
- Interlocks preventing operation.
- Last command and issuer.
- Manual override status.
- Impact on energy and resource budgets.

---

## 18. Maintenance and Work Management

The digital twin should ultimately include a lightweight computerized maintenance management function.

Each asset can generate work based on:

- Calendar interval.
- Runtime hours.
- Cycle count.
- Condition threshold.
- Alarm occurrence.
- Seasonal procedure.
- Inspection result.

Examples:

- Clean irrigation filters after a pressure differential threshold.
- Calibrate soil sensors each season.
- Inspect battery-room cooling monthly.
- Test generator start and transfer monthly.
- Replace water-treatment media by throughput or date.
- Exercise valves after prolonged inactivity.
- Inspect greenhouse pumps after defined runtime.
- Test nitrogen-container oxygen sensors and calibration.
- Validate spa sanitation and drying cycle performance.
- Scrub ZFS pools and review disk-health alerts.

Spare parts should be linked to assets and minimum stock quantities.

---

## 19. Commissioning Strategy

Every subsystem integration should pass the following sequence:

1. Bench test.
2. Point-to-point verification.
3. Sensor calibration.
4. Manual control test.
5. Local automatic control test.
6. Communications-loss test.
7. Sensor-failure test.
8. Power-loss and restoration test.
9. Supervisory control test.
10. Alarm and notification test.
11. Manual override test.
12. Documentation and baseline capture.

No subsystem should be placed under automatic supervisory control until its local control and failure modes have been tested.

---

## 20. Phased Implementation Plan

### Phase A — Digital twin foundation before property deployment

- Establish Git repository and document structure.
- Create initial PostgreSQL/PostGIS asset registry.
- Import existing CAPEX and equipment records.
- Model known rack equipment, energy architecture, structures, and planned subsystems.
- Define asset IDs, topic naming, units, alarm format, and command format.
- Build a simulated MQTT environment.
- Create the first master dashboard.

### Phase B — Power/utilities/server container

- Integrate NetBotz, UPS, PDUs, rack access, servers, network equipment, and environmental sensors.
- Integrate inverter, battery BMS, site meter, generator, and critical-load panel.
- Implement energy state and load-tier logic.
- Deploy independent secondary control node outside the container.
- Test graceful shutdown and black-start recovery.

### Phase C — Water and land infrastructure

- Integrate well, tanks, pumps, pressure, flow, leak detection, and treatment.
- Deploy weather station and LoRaWAN gateway.
- Deploy soil and compost sensors.
- Commission one irrigation zone before expansion.
- Add orchard/food-forest geographic model.

### Phase D — Food production and specialized facilities

- Greenhouse climate and fertigation.
- Hydrogel lab batch tracking.
- Nitrogen-storage container controls.
- Hydrothermal spa controls.
- Robotic mower and land-maintenance status.

### Phase E — Optimization and prediction

- Energy and water forecasting.
- Crop yield and condition models.
- Predictive maintenance.
- Resource scheduling based on weather and solar surplus.
- Scenario simulation.
- Optional computer vision and aerial mapping.

---

## 21. Minimum Viable Product Acceptance Criteria

The MVP is complete when:

1. The asset registry contains all installed core rack, power, water, and environmental assets.
2. The platform displays live energy, water, rack, weather, and alarm state locally.
3. MQTT telemetry follows one documented schema.
4. Critical alarms work during internet loss.
5. Home Assistant, Node-RED, Grafana, and the digital twin API are backed up automatically.
6. One physical subsystem can be controlled through the supervisory command path with full acknowledgement and audit.
7. Communications-loss and central-server-loss tests demonstrate safe local continuation.
8. The property map displays installed structures, utility assets, and sensor locations.
9. Every critical asset has a manual override and failure-state record.
10. Data export and restore have been tested.

---

## 22. Open Design Decisions

1. Confirm the authoritative energy design: older 12 kW / 40 kWh baseline or revised 45 kWdc / 800 kWh nominal design.
2. Select the virtualization platform and container/VM deployment model.
3. Choose InfluxDB versus TimescaleDB.
4. Choose LoRaWAN architecture and frequency plan rather than ad hoc LoRa links.
5. Choose the standard PLC/controller family for critical local control.
6. Select inverter/BMS/generator products and verify their local communication interfaces.
7. Confirm final property location, acreage, terrain, and structure placement.
8. Decide which system owns scheduling when Home Assistant, Node-RED, and subsystem controllers overlap.
9. Define exact remote-access and offsite-backup architecture.
10. Define data-retention limits based on storage and power budget.
11. Define acceptable command latency by subsystem.
12. Define the exact physical separation and independent cooling strategy inside the power/server container.
13. Decide whether the secondary control node is in the residence, workshop, or another independent enclosure.
14. Define integration boundaries for Aircela, beekeeping, avian systems, workshop machinery, and future businesses.

---

## 23. Recommended Next Design Package

The next iteration should produce five linked artifacts:

1. **System requirements specification** with numbered requirements and verification methods.
2. **Master asset-class and point-name dictionary**.
3. **Network and trust-boundary diagram** using the approved VLAN schema.
4. **Subsystem control narratives** beginning with energy, water, greenhouse, and nitrogen storage.
5. **Deployment architecture and service Bill of Materials** for the primary and secondary control nodes.

---

## 24. Initial Design Position

The homestead should be run as a small industrial site, not as a collection of consumer smart-home gadgets. Home Assistant remains useful, but the durable design is a layered operational technology system: hardwired protection at the bottom, autonomous local control above it, a local message bus and asset model in the middle, and dashboards, optimization, and planning at the top.

That architecture matches the project’s actual scale: enterprise computing, large off-grid energy storage, distributed water infrastructure, automated food production, specialized storage, multiple containers and buildings, and a requirement for long-term repairability and independence.

---

## 25. Master Asset Identification Standard

### 25.1 Purpose

The asset-identification standard provides stable, human-readable identities for physical and logical homestead assets. Vendor names, IP addresses, Home Assistant entity IDs, PLC register numbers, and MQTT topics may change during the life of the property; the canonical `asset_id` must not.

### 25.2 Canonical asset-ID format

```text
<domain>.<asset_class>.<location_or_system>.<instance>
```

Examples:

```text
energy.inverter.power_container.01
energy.battery_bank.power_container.01
energy.load.server_rack.01
water.cistern.orchard.01
water.pump.irrigation.01
agriculture.irrigation_zone.orchard.03
agriculture.crop_batch.greenhouse.2026_0042
storage.nitrogen_generator.food_container.01
it.server.rack_01.r740xd_01
security.camera.perimeter_north.02
structure.room.spa_pavilion.mechanical
```

### 25.3 Identification rules

1. IDs are lowercase ASCII.
2. Components are separated by periods.
3. Spaces and punctuation are replaced with underscores.
4. Instance numbers use two digits unless a natural serial or batch key is required.
5. IDs are never reused after retirement.
6. Physical replacement does not automatically change the functional asset ID. The replacement device is recorded as a new equipment instance linked to the continuing functional position.
7. A vendor serial number, MAC address, IP address, Modbus ID, LoRa DevEUI, or Home Assistant entity ID is an external identifier, not the canonical ID.
8. Planned assets receive IDs before procurement so drawings, CAPEX, controls, and commissioning records can reference the same object.
9. Biological assets may use either individual identity or managed-group identity depending on operational value. High-value trees can be individually tracked; dense groundcover can be represented by block or guild.
10. Every commandable asset must have one authoritative control owner at a time.

### 25.4 Domain vocabulary

| Domain | Scope |
|---|---|
| `site` | Property-wide objects, geographic sectors, weather and shared state |
| `structure` | Buildings, containers, rooms, enclosures, roof areas and service spaces |
| `energy` | Generation, storage, conversion, distribution, metering and loads |
| `water` | Well, potable water, rainwater, graywater, irrigation and process water |
| `agriculture` | Orchard, food forest, greenhouse, compost, crops, soil and biological assets |
| `storage` | Food storage, nitrogen displacement, dry storage, cold storage and inventory |
| `spa` | Hydrothermal pavilion water, thermal, sanitation and humidity systems |
| `it` | Servers, storage, network, telephony, applications and platform services |
| `security` | Cameras, access control, gates, doors, perimeter and alarm devices |
| `workshop` | Tools, machinery, fabrication equipment, consumables and shop utilities |
| `fuel` | Generator fuel, synthetic fuel, gas storage and future Aircela integration |
| `safety` | Fire, smoke, leak, emergency-stop, hazardous-atmosphere and emergency systems |

### 25.5 Relationship types

The digital twin must support typed relationships rather than relying only on the containment hierarchy.

| Relationship | Meaning | Example |
|---|---|---|
| `contains` | Physical or logical containment | Rack contains server |
| `located_in` | Geographic or structural location | Inverter located in power container |
| `feeds` | Supplies energy, water, data or material | Battery feeds critical panel |
| `returns_to` | Return path | Spa recirculation returns to reservoir |
| `controls` | Controller owns a process output | PLC controls irrigation pump |
| `monitors` | Sensor or service observes an asset | NetBotz monitors rack enclosure |
| `protects` | Protection device acts on equipment | Breaker protects inverter feeder |
| `depends_on` | Operational dependency | MQTT bridge depends on core switch |
| `backs_up` | Redundant or alternate resource | Generator backs up inverter system |
| `measures` | Meter point association | Flow meter measures orchard main |
| `serves` | Utility serves end-use system | Cistern serves orchard zones |
| `part_of` | Functional grouping | PV string part of array 01 |
| `associated_with` | Non-flow contextual link | Crop batch associated with nutrient recipe |
| `replaces` | Asset lifecycle succession | New pump replaces failed pump |

---

## 26. Point Naming and Classification Standard

### 26.1 Purpose

A point is a measured value, state, command, setpoint, calculated value, configuration value, event, or alarm associated with an asset. Point names must remain consistent across MQTT, the digital-twin API, databases, dashboards, reports, and control narratives.

### 26.2 Canonical point path

```text
<asset_id>/<point_name>
```

MQTT representation:

```text
chaos/<domain>/<location>/<asset>/<point_name>
chaos/<domain>/<location>/<asset>/cmd/<command_name>
chaos/<domain>/<location>/<asset>/setpoint/<setpoint_name>
chaos/<domain>/<location>/<asset>/event/<event_name>
chaos/<domain>/<location>/<asset>/alarm/<alarm_name>
```

The canonical asset registry maps the MQTT topic to `asset_id` and `point_id`; parsing a topic is not the sole identity mechanism.

### 26.3 Point classes

| Code | Class | Read/write | Description |
|---|---|---|---|
| `AI` | Analog input | Read | Continuously measured numeric value |
| `DI` | Digital input | Read | Boolean or enumerated observed state |
| `AO` | Analog output | Write | Numeric command or setpoint |
| `DO` | Digital output | Write | Boolean or enumerated command |
| `CALC` | Calculated | Read | Derived value generated by software or controller |
| `CFG` | Configuration | Controlled write | Persistent configuration value |
| `EVENT` | Event | Append-only | Timestamped discrete occurrence |
| `ALARM` | Alarm | State machine | Abnormal condition with lifecycle |
| `COUNTER` | Counter | Read | Monotonic runtime, cycle, volume or energy total |
| `TEXT` | Text/status | Read | Diagnostic message, firmware, model or status detail |

### 26.4 Point-name construction

Point names use this order when applicable:

```text
<quantity>_<qualifier>_<unit_or_state>
```

Examples:

```text
temperature_air_c
temperature_cell_max_c
humidity_relative_pct
power_ac_output_kw
energy_import_total_kwh
pressure_discharge_kpa
flow_instant_lpm
flow_total_m3
state_operating
mode_requested
mode_actual
setpoint_temperature_c
fault_active
fault_code
runtime_total_h
availability_state
```

### 26.5 Standard state and mode points

Every commandable or monitored equipment asset should implement the applicable subset below.

| Point name | Class | Type | Meaning |
|---|---|---|---|
| `availability_state` | DI | enum | `online`, `offline`, `degraded`, `unknown` |
| `state_operating` | DI | enum | Equipment-specific actual state |
| `mode_actual` | DI | enum | Actual control mode |
| `mode_requested` | DO | enum | Requested supervisory mode |
| `control_owner` | DI | enum | `local`, `supervisory`, `manual`, `vendor`, `safety` |
| `manual_override_active` | DI | boolean | Local override is active |
| `interlock_permissive` | DI | boolean | All required start/run permissives are satisfied |
| `interlock_block_reason` | TEXT | string | Primary reason operation is blocked |
| `fault_active` | DI | boolean | At least one active equipment fault |
| `fault_code` | TEXT | string | Manufacturer or normalized code |
| `alarm_summary` | DI | enum | Highest active alarm severity |
| `command_last_id` | TEXT | string | Last command acknowledged |
| `command_last_result` | DI | enum | `accepted`, `rejected`, `completed`, `failed`, `expired` |
| `heartbeat_age_s` | CALC | number | Age of last valid message |
| `firmware_version` | TEXT | string | Installed firmware/software version |
| `runtime_total_h` | COUNTER | hours | Accumulated operating time |
| `starts_total` | COUNTER | count | Accumulated start count |

### 26.6 Measurement-quality model

Each telemetry value must carry a quality code.

| Quality | Meaning |
|---|---|
| `good` | Valid and within expected acquisition timing |
| `uncertain` | Usable with caution; drift, estimated value, or stale-but-tolerable |
| `bad` | Invalid, failed sensor, failed checksum, or out-of-physical-range |
| `stale` | No update within the defined point timeout |
| `substituted` | Replaced by fallback or manually entered value |
| `calculated` | Derived from other values |
| `maintenance` | Device intentionally unavailable or under test |

Control logic must define whether `uncertain`, `stale`, or `substituted` values are acceptable for each decision. Safety-critical permissives must not default to permissive on missing data.

### 26.7 Standard engineering units

The registry stores units explicitly. The preferred display and storage units are:

| Quantity | Unit |
|---|---|
| Electrical power | W or kW |
| Electrical energy | Wh or kWh |
| Voltage | V |
| Current | A |
| Frequency | Hz |
| Power factor | ratio |
| Temperature | °C |
| Relative humidity | %RH |
| Pressure | kPa |
| Differential pressure | kPa |
| Liquid flow | L/min or m³/h |
| Liquid volume | L or m³ |
| Tank level | % and, where geometry is known, L |
| Gas concentration | ppm or volume % |
| Soil volumetric water content | % VWC |
| Rainfall | mm |
| Wind speed | m/s or km/h, with canonical storage unit defined once |
| Irradiance | W/m² |
| pH | pH units |
| Electrical conductivity | mS/cm |
| Runtime | h |
| Time duration | s |

Conversion for display is permitted, but the canonical unit for a point cannot change without a schema revision.

---

## 27. Initial Master Asset-Class Dictionary

This dictionary is the minimum common model. Subsystem-specific extensions are expected, but they must inherit the common state, quality, documentation, maintenance, and command fields.

### 27.1 Site and structure classes

| Asset class | Purpose | Required properties | Typical points |
|---|---|---|---|
| `site` | Entire property | Boundary, timezone, climate profile, operating state | `mode_actual`, `alarm_summary`, `occupancy_state` |
| `geographic_zone` | Managed land area | Polygon, land use, slope, soil type | `status_condition`, `accessibility_state` |
| `structure` | Building or container | Footprint, envelope, utilities, fire zone | `temperature_air_c`, `humidity_relative_pct`, `door_state`, `leak_active` |
| `room` | Interior operational space | Structure, volume, environmental class | `temperature_air_c`, `humidity_relative_pct`, `occupancy_state` |
| `enclosure` | Cabinet, rack, battery bay, control panel | Ingress rating, cooling path, access method | `temperature_air_c`, `door_state`, `smoke_active` |

### 27.2 Energy classes

| Asset class | Purpose | Required properties | Core points |
|---|---|---|---|
| `pv_array` | Group of PV strings | Rated kWdc, orientation, tilt, row geometry | `power_dc_kw`, `energy_today_kwh`, `irradiance_w_m2`, `availability_state` |
| `pv_string` | String-level generation | Module count, Voc, Isc, combiner | `voltage_dc_v`, `current_dc_a`, `power_dc_kw`, `fault_active` |
| `combiner` | PV collection/protection | Inputs, breaker/fuse/SPD references | `current_total_a`, `door_state`, `temperature_air_c` |
| `inverter` | DC/AC conversion and charging | Rated power, topology, firmware, Modbus map | `power_ac_output_kw`, `power_dc_input_kw`, `voltage_ac_v`, `frequency_hz`, `state_operating`, `fault_code` |
| `battery_bank` | Site energy storage | Nominal/usable kWh, chemistry, reserve policy | `soc_pct`, `soh_pct`, `power_kw`, `energy_available_kwh`, `temperature_cell_max_c`, `charge_limit_kw`, `discharge_limit_kw` |
| `battery_module` | Serviceable battery unit | Chemistry, serial, capacity, rack/bay | `voltage_v`, `temperature_cell_max_c`, `soc_pct`, `fault_code` |
| `bms` | Battery-management controller | Protected bank/modules, communications | `state_operating`, `charge_permissive`, `discharge_permissive`, `contactor_state` |
| `generator` | Backup generation | Fuel type, rated power, auto-start interface | `state_operating`, `power_output_kw`, `fuel_level_pct`, `runtime_total_h`, `start_failure_active` |
| `ats` | Source transfer | Source A/B, transfer policy | `source_selected`, `source_a_available`, `source_b_available`, `transfer_total` |
| `panel` | Distribution board | Voltage, phase, upstream, location | `power_total_kw`, `energy_total_kwh`, `breaker_trip_active` |
| `circuit` | Monitored branch | Panel, breaker, wire, served asset | `power_kw`, `current_a`, `energy_total_kwh`, `energized_state` |
| `load` | Controllable or modeled end use | Criticality tier, minimum uptime, restart policy | `power_kw`, `enabled_actual`, `enabled_requested`, `shed_state`, `priority_effective` |
| `meter` | Revenue/process electrical meter | CT/PT ratios, direction convention | `power_import_kw`, `power_export_kw`, `energy_import_total_kwh`, `energy_export_total_kwh` |
| `ups` | Short-duration conditioned power | VA/W rating, battery type, protected load | `load_pct`, `runtime_remaining_min`, `input_available`, `on_battery`, `battery_replace_due` |
| `pdu` | Rack distribution | Feed, outlet map, rating | `power_total_kw`, `current_total_a`, `outlet_state`, `overload_active` |
| `thermal_storage` | Deferrable heat/cold store | Capacity, medium, temperature range | `temperature_mean_c`, `energy_estimated_kwh`, `charge_requested` |

### 27.3 Water classes

| Asset class | Purpose | Required properties | Core points |
|---|---|---|---|
| `well` | Groundwater source | Depth, static level, pump association | `water_level_m`, `water_temperature_c`, `quality_status` |
| `pump` | Liquid movement | Curve, motor rating, source/destination | `state_operating`, `flow_instant_lpm`, `pressure_discharge_kpa`, `current_a`, `dry_run_active` |
| `tank` | Water or nutrient storage | Geometry, gross/working volume, material | `level_pct`, `volume_estimated_l`, `temperature_liquid_c`, `high_level_active`, `low_level_active` |
| `valve` | Isolation or modulation | Type, fail position, served line | `position_actual_pct`, `position_requested_pct`, `open_limit`, `closed_limit`, `fault_active` |
| `flow_meter` | Water accounting | Pipe size, calibration factor, direction | `flow_instant_lpm`, `flow_total_m3`, `reverse_flow_active` |
| `pressure_sensor` | Pressure monitoring | Range, location, calibration | `pressure_kpa`, `quality` |
| `filter` | Particulate or media treatment | Media type, nominal flow, replacement policy | `pressure_differential_kpa`, `throughput_total_m3`, `service_due` |
| `treatment_stage` | UV, RO, chemical, aeration or other treatment | Process type, design capacity | `state_operating`, `quality_in`, `quality_out`, `service_due` |
| `irrigation_zone` | Managed irrigation area | Polygon, crop/guild, valve, design flow | `soil_moisture_pct`, `irrigation_requested`, `irrigation_active`, `volume_today_l` |
| `graywater_branch` | Reuse path | Source fixtures, destination, filter | `flow_instant_lpm`, `diverter_position`, `quality_status` |

### 27.4 Agriculture classes

| Asset class | Purpose | Required properties | Core points |
|---|---|---|---|
| `weather_station` | Local weather | Elevation, exposure, sensor inventory | `temperature_air_c`, `humidity_relative_pct`, `rain_today_mm`, `wind_speed_m_s`, `solar_irradiance_w_m2` |
| `soil_sensor_node` | Distributed soil monitoring | Coordinates, depth, soil type, calibration | `soil_moisture_vwc_pct`, `soil_temperature_c`, `battery_pct`, `rssi_dbm` |
| `tree` | Individual perennial | Species, cultivar, rootstock, planting date | `phenology_stage`, `health_status`, `yield_season_kg`, `irrigation_zone_id` |
| `planting_block` | Group of trees/shrubs/crops | Polygon, species mix, planting density | `health_status`, `yield_season_kg`, `irrigation_demand_mm` |
| `guild` | Food-forest association | Canopy, understory, support species | `establishment_stage`, `health_status` |
| `greenhouse_zone` | Controlled grow area | Volume, crop class, environmental recipe | `temperature_air_c`, `humidity_relative_pct`, `co2_ppm`, `light_ppfd_umol_m2_s` |
| `nutrient_reservoir` | Fertigation solution | Volume, recipe, served zones | `level_pct`, `temperature_liquid_c`, `ph`, `ec_ms_cm` |
| `crop_batch` | Traceable production cohort | Crop, cultivar, dates, location, recipe | `stage`, `plants_active`, `yield_total_kg`, `quality_grade` |
| `hydrogel_batch` | Experimental substrate batch | Formula, ingredients, production date | `reuse_cycles`, `degradation_status`, `ph`, `associated_crop_performance` |
| `compost_batch` | Compost process lot | Inputs, start date, target method | `temperature_core_c`, `temperature_mean_c`, `aeration_state`, `maturity_status` |
| `mower_robot` | Autonomous mowing | Boundary/map, dock, cut height | `state_operating`, `battery_pct`, `position`, `fault_code`, `area_today_m2` |

### 27.5 IT and communications classes

| Asset class | Purpose | Required properties | Core points |
|---|---|---|---|
| `rack` | Equipment enclosure | U height, power feeds, cooling zone | `temperature_inlet_c`, `temperature_exhaust_c`, `humidity_relative_pct`, `door_state` |
| `server` | Compute host | Model, CPU, memory, hypervisor role | `power_w`, `cpu_utilization_pct`, `memory_used_pct`, `temperature_cpu_c`, `availability_state` |
| `storage_array` | Bulk storage | Chassis, disks, topology, host path | `capacity_used_pct`, `disk_fault_count`, `pool_state`, `scrub_age_d` |
| `switch` | Network switching | Model, management IP, VLAN role | `availability_state`, `port_up_count`, `temperature_c`, `packet_error_rate` |
| `router` | WAN/routing/voice gateway | Interfaces, routing role, failover | `wan_state`, `vpn_state`, `cpu_utilization_pct`, `voice_gateway_state` |
| `wireless_controller` | AP management | Managed APs, software version | `ap_online_count`, `client_count`, `alarm_summary` |
| `access_point` | Wireless coverage | Coordinates, antenna, controller | `client_count`, `channel`, `rssi_health`, `availability_state` |
| `application_service` | Digital-twin software service | Host, port, dependencies, backup plan | `availability_state`, `response_time_ms`, `error_rate`, `backup_age_h` |
| `gateway` | Protocol/field bridge | Protocols, served devices, location | `availability_state`, `message_rate`, `queue_depth`, `clock_offset_ms` |
| `voice_endpoint` | Desk/rugged phone or alert endpoint | Extension, location, priority | `registration_state`, `availability_state` |

### 27.6 Storage, spa and security classes

| Asset class | Purpose | Core points |
|---|---|---|
| `nitrogen_generator` | PSA nitrogen generation | `state_operating`, `nitrogen_purity_pct`, `flow_instant_lpm`, `pressure_discharge_kpa`, `runtime_total_h` |
| `controlled_atmosphere_container` | Low-oxygen food storage | `oxygen_pct`, `temperature_air_c`, `humidity_relative_pct`, `door_state`, `safe_entry_state` |
| `storage_lot` | Traceable food/material lot | `quantity_current`, `location_bin`, `date_stored`, `inspection_due` |
| `spa_reservoir` | Recirculation storage | `level_pct`, `temperature_liquid_c`, `sanitation_valid`, `volume_estimated_l` |
| `spa_pump` | Hydrotherapy/atmospheric circuit | `flow_instant_lpm`, `pressure_discharge_kpa`, `current_a`, `state_operating` |
| `spa_heater` | Water or surface heating | `power_kw`, `temperature_outlet_c`, `flow_proven`, `high_limit_active` |
| `dehumidifier` | Post-use drying | `state_operating`, `humidity_in_pct`, `humidity_out_pct`, `condensate_total_l` |
| `camera` | Video monitoring | `availability_state`, `recording_state`, `tamper_active`, `motion_event` |
| `door` | Access boundary | `open_state`, `locked_state`, `forced_open_active`, `access_last_identity` |
| `gate` | Property access | `position_actual_pct`, `locked_state`, `obstruction_active` |
| `access_controller` | Credential management | `availability_state`, `denied_event`, `tamper_active` |
| `safety_sensor` | Smoke, leak, gas, heat or vibration | `alarm_active`, `value`, `self_test_due`, `battery_pct` |

---

## 28. Initial Master Point Dictionary

The following dictionary is deliberately repetitive: consistent names are more valuable than clever names. Not every asset implements every point.

### 28.1 Electrical and energy points

| Point | Class | Unit/type | Description |
|---|---|---|---|
| `voltage_dc_v` | AI | V | DC bus or source voltage |
| `current_dc_a` | AI | A | Signed DC current; direction convention stored in metadata |
| `power_dc_kw` | AI | kW | Signed DC power |
| `voltage_ac_v` | AI | V | AC RMS voltage |
| `current_ac_a` | AI | A | AC RMS current |
| `frequency_hz` | AI | Hz | AC frequency |
| `power_ac_kw` | AI | kW | Signed active AC power |
| `power_reactive_kvar` | AI | kvar | Reactive power |
| `power_factor` | AI | ratio | Signed or unsigned according to point metadata |
| `energy_import_total_kwh` | COUNTER | kWh | Cumulative imported energy |
| `energy_export_total_kwh` | COUNTER | kWh | Cumulative exported energy |
| `energy_today_kwh` | CALC | kWh | Energy since local midnight |
| `soc_pct` | AI | % | Battery state of charge |
| `soh_pct` | AI | % | Battery state of health |
| `energy_available_kwh` | CALC | kWh | Usable energy above current reserve floor |
| `charge_limit_kw` | AI | kW | Current maximum permitted charge power |
| `discharge_limit_kw` | AI | kW | Current maximum permitted discharge power |
| `temperature_cell_min_c` | AI | °C | Minimum cell temperature |
| `temperature_cell_max_c` | AI | °C | Maximum cell temperature |
| `cell_voltage_delta_mv` | AI | mV | Maximum cell voltage spread |
| `contactor_state` | DI | enum | `open`, `closed`, `precharge`, `fault` |
| `charge_permissive` | DI | boolean | BMS permits charging |
| `discharge_permissive` | DI | boolean | BMS permits discharging |
| `source_selected` | DI | enum | Active ATS/inverter source |
| `generator_start_request` | DO | boolean | Supervisory request to native generator control |
| `generator_available` | DI | boolean | Generator ready and permitted |
| `fuel_level_pct` | AI | % | Estimated generator fuel level |
| `load_tier` | CFG | integer | Base load priority tier |
| `priority_effective` | CALC | integer | Current priority after operator and process adjustments |
| `shed_state` | DI | enum | `connected`, `shed_pending`, `shed`, `restore_pending`, `locked_out` |
| `shed_reason` | TEXT | string | Reason for current shed state |
| `restart_delay_s` | CFG | s | Minimum restoration delay |
| `minimum_on_time_s` | CFG | s | Anti-short-cycle minimum on time |
| `minimum_off_time_s` | CFG | s | Anti-short-cycle minimum off time |
| `power_budget_kw` | AO/CALC | kW | Granted subsystem power envelope |
| `power_request_kw` | AO/AI | kW | Subsystem requested power envelope |

### 28.2 Water and process points

| Point | Class | Unit/type | Description |
|---|---|---|---|
| `level_pct` | AI | % | Vessel level normalized to working range |
| `volume_estimated_l` | CALC | L | Volume derived from geometry/calibration |
| `flow_instant_lpm` | AI | L/min | Instantaneous liquid flow |
| `flow_total_m3` | COUNTER | m³ | Cumulative volume |
| `pressure_suction_kpa` | AI | kPa | Pump suction pressure |
| `pressure_discharge_kpa` | AI | kPa | Pump discharge pressure |
| `pressure_differential_kpa` | CALC/AI | kPa | Filter or process differential |
| `temperature_liquid_c` | AI | °C | Liquid temperature |
| `ph` | AI | pH | Acidity/alkalinity |
| `ec_ms_cm` | AI | mS/cm | Electrical conductivity |
| `turbidity_ntu` | AI | NTU | Turbidity where instrumented |
| `pump_run_request` | DO | boolean | Requested run state |
| `pump_speed_requested_pct` | AO | % | Requested variable-speed output |
| `flow_proven` | DI | boolean | Minimum valid flow established |
| `dry_run_active` | ALARM/DI | boolean | Dry-run condition detected |
| `high_level_active` | DI | boolean | Independent high-level state |
| `low_level_active` | DI | boolean | Independent low-level state |
| `leak_active` | ALARM/DI | boolean | Leak detector or leak model active |
| `freeze_lockout_active` | DI | boolean | Operation blocked by freeze conditions |
| `water_quality_status` | DI | enum | `good`, `advisory`, `invalid`, `unknown` |

### 28.3 Environmental and agricultural points

| Point | Class | Unit/type | Description |
|---|---|---|---|
| `temperature_air_c` | AI | °C | Ambient air temperature |
| `humidity_relative_pct` | AI | %RH | Relative humidity |
| `dew_point_c` | CALC | °C | Calculated dew point |
| `co2_ppm` | AI | ppm | Carbon-dioxide concentration |
| `oxygen_pct` | AI | volume % | Oxygen concentration |
| `solar_irradiance_w_m2` | AI | W/m² | Solar irradiance |
| `light_ppfd_umol_m2_s` | AI | µmol/m²/s | Photosynthetic photon flux density |
| `rain_increment_mm` | EVENT/AI | mm | Tipping-bucket increment |
| `rain_today_mm` | CALC | mm | Rain since local midnight |
| `wind_speed_m_s` | AI | m/s | Wind speed |
| `wind_gust_m_s` | AI | m/s | Recent peak gust |
| `soil_moisture_vwc_pct` | AI | % VWC | Calibrated volumetric water content |
| `soil_temperature_c` | AI | °C | Soil temperature at sensor depth |
| `irrigation_demand_mm` | CALC | mm | Modeled water deficit |
| `irrigation_requested` | DO/CALC | boolean | Supervisory request for irrigation |
| `phenology_stage` | DI/TEXT | enum | Biological development stage |
| `health_status` | DI | enum | `good`, `watch`, `treatment`, `failed` |
| `yield_total_kg` | COUNTER | kg | Batch or seasonal yield |
| `rssi_dbm` | AI | dBm | Radio signal strength |
| `battery_pct` | AI | % | Field-node battery estimate |

### 28.4 IT, communications and maintenance points

| Point | Class | Unit/type | Description |
|---|---|---|---|
| `cpu_utilization_pct` | AI | % | CPU utilization |
| `memory_used_pct` | AI | % | Memory utilization |
| `storage_used_pct` | AI | % | Filesystem/pool utilization |
| `temperature_cpu_c` | AI | °C | Processor temperature |
| `response_time_ms` | AI | ms | Service or network response time |
| `packet_loss_pct` | AI | % | Network packet loss |
| `message_rate_s` | AI | messages/s | Gateway or broker traffic rate |
| `queue_depth` | AI | count | Pending messages/commands |
| `clock_offset_ms` | AI | ms | Time synchronization offset |
| `backup_age_h` | CALC | h | Time since last verified backup |
| `configuration_age_d` | CALC | d | Time since last exported configuration |
| `service_due` | CALC/DI | boolean | Maintenance due by date/counter/condition |
| `inspection_due` | CALC/DI | boolean | Inspection due |
| `calibration_due` | CALC/DI | boolean | Calibration due |
| `spare_stock_qty` | AI/CFG | count | Current linked spare quantity |
| `work_order_open_count` | CALC | count | Open work orders affecting asset |

---

## 29. Point Metadata Requirements

Each point definition in the registry must contain:

```yaml
point_id: energy.battery_bank.power_container.01/soc_pct
asset_id: energy.battery_bank.power_container.01
point_name: soc_pct
point_class: AI
data_type: float
unit: "%"
canonical_direction: positive_discharge
source_protocol: modbus_tcp
source_address: "holding:31013"
sample_interval_s: 2
publish_interval_s: 5
stale_after_s: 20
historian_policy: high_resolution
quality_policy: reject_outside_physical_range
minimum_physical: 0
maximum_physical: 100
warning_low: 30
alarm_low: 20
critical_low: 12
hysteresis: 2
control_use: energy_state_machine
```

Point metadata must also identify scaling, endianness, calibration date, sensor range, precision, deadband, alarm delay, reset delay, security classification, and whether the point can be used for automatic control.

---

## 30. Detailed Energy-Management Control Narrative

### 30.1 Scope

The Energy Management System (EMS) supervises the agrivoltaic PV array, battery bank, hybrid inverters, generator, transfer equipment, monitored distribution panels, controllable loads, UPS-supported IT equipment, and deferrable process loads.

The EMS is not the battery-management system, inverter protection system, generator controller, breaker protection, or fire-protection system. It issues operating requests and budgets within the limits exposed by those systems.

### 30.2 Current design basis and unresolved capacity conflict

The project portfolio contains two energy baselines:

- Earlier baseline: approximately 12 kW PV and 40 kWh usable storage.
- Revised planning basis: approximately 45 kWdc PV, four approximately 10 kW hybrid inverters, approximately 800 kWh nominal storage, approximately 640 kWh usable planning capacity, and a 1.5 kW average critical baseload target.

This narrative is capacity-agnostic where possible and uses percentages, reserve energy and measured limits rather than hard-coding the revised system size. Final thresholds must be commissioned against the selected battery chemistry, inverter capabilities, generator, climate, and actual site loads.

### 30.3 EMS objectives in priority order

1. Respect all equipment-native safety and operating limits.
2. Preserve physical-protection loads and emergency communications.
3. Preserve minimum potable-water, freeze-protection, refrigeration and crop-survival functions.
4. Maintain a configurable battery emergency reserve.
5. Avoid unnecessary generator operation while preventing uncontrolled depletion.
6. Use PV surplus for useful deferred work instead of curtailment where practical.
7. Reduce battery cycling, high current, thermal stress and avoidable conversion losses.
8. Restore loads predictably without creating a rebound surge.
9. Produce an auditable explanation for every automatic shedding, generator request and restoration action.
10. Provide the operator with temporary, bounded priority overrides.

### 30.4 Control ownership

| Function | Primary owner | EMS role |
|---|---|---|
| Cell over/under-voltage, overcurrent and thermal protection | BMS | Observe; never bypass |
| Inverter anti-islanding, overcurrent and transfer protection | Inverter/controller | Observe and request permitted modes |
| Generator cranking, oil pressure, overspeed and shutdown | Generator controller | Issue start/stop request; monitor result |
| Branch-circuit protection | Breakers/fuses | No control authority unless listed shunt-trip/contactor exists |
| Fast motor interlocks | Local PLC/controller | Publish permissive and fault; accept enable/budget |
| Site reserve management | EMS | Authoritative supervisory owner |
| Load shedding and restoration | EMS plus local load controller | EMS requests; local controller enforces equipment interlocks |
| Server shutdown | IT orchestration service | EMS publishes deadline/energy state; IT service executes orderly sequence |

### 30.5 Required EMS inputs

#### Electrical state

- PV power available and generated.
- Inverter AC power, DC power, state, limits and faults.
- Battery SOC, SOH, voltage, current, temperature, charge/discharge limits and contactor state.
- Total site load and critical-panel load.
- Per-circuit or per-subsystem load where metered.
- Generator availability, power, fuel estimate, runtime, faults and maintenance lockout.
- UPS runtime and protected-load state.
- ATS/source state.

#### Forecast and planning state

- Short-term local solar forecast when internet or local forecast data is available.
- Local irradiance trend and weather-station data.
- Time of day, season and expected sunrise/sunset.
- Scheduled high-energy tasks.
- Deferrable-work backlog.
- Minimum process-survival requirements.
- Operator reservations, such as a planned welding or workshop period.

#### Context and constraints

- Occupancy and residence mode.
- Severe-weather mode.
- Freeze-protection demand.
- Greenhouse crop-survival limits.
- Potable-water reserve.
- Active maintenance and lockout states.
- Battery/inverter room temperature and cooling availability.

### 30.6 Derived EMS values

| Derived value | Description |
|---|---|
| `critical_load_rolling_kw` | Rolling average of loads that must remain energized |
| `site_load_rolling_kw` | Rolling site demand |
| `energy_above_emergency_reserve_kwh` | Usable battery energy above the emergency floor |
| `autonomy_critical_h` | Estimated time supporting critical loads only |
| `autonomy_current_h` | Estimated time at current load |
| `forecast_pv_next_6h_kwh` | Expected near-term PV energy |
| `forecast_pv_next_24h_kwh` | Expected daily PV energy |
| `forecast_load_next_24h_kwh` | Scheduled and baseline demand estimate |
| `forecast_energy_margin_kwh` | Battery energy plus expected generation minus expected load and reserve |
| `surplus_power_kw` | PV/inverter capability above current demand and safe charge acceptance |
| `curtailment_risk_kw` | PV energy likely to be unused without activating deferred loads |
| `thermal_derate_pct` | Available capacity reduction caused by temperature or cooling limits |
| `deferrable_backlog_kwh` | Estimated energy needed for pending nonurgent work |
| `restoration_headroom_kw` | Capacity available for reconnecting loads without violating reserve/ramp limits |

The algorithm must expose the calculation components so an operator can understand why a state was selected.

### 30.7 Energy operating-state machine

The EMS publishes one site energy state. Subsystems translate that state into their own bounded operating profiles.

| State | Intent | Typical entry basis | Typical exit basis |
|---|---|---|---|
| `COMMISSIONING` | Point verification and controlled testing | Explicit operator selection | Operator completion |
| `MAINTENANCE` | Prevent autonomous transitions under active work | Explicit lockout or maintenance window | Authorized release |
| `SURPLUS` | Consume useful excess generation | High SOC, positive forecast margin, curtailment risk | Margin falls below surplus threshold |
| `NORMAL` | Full normal operation | Healthy reserve and equipment | Reserve or forecast deteriorates |
| `CONSERVE` | Reduce discretionary demand and defer work | Declining reserve or negative forecast margin | Sustained recovery or worsening to critical |
| `CRITICAL_RESERVE` | Preserve essential and survival loads | Reserve floor approached, poor forecast or major generation fault | Generator support or sustained recovery |
| `GENERATOR_SUPPORT` | Run generator to protect reserve/serve load | Start criteria satisfied and generator accepted | Stop criteria plus cooldown satisfied |
| `EMERGENCY` | Protect equipment and retain minimal control | BMS/inverter emergency, severe thermal fault, loss of sources | Manual reset after cause is removed |
| `BLACK_START` | Recover from de-energized or collapsed AC system | Valid black-start request and permissives | Stable critical bus and state reevaluation |
| `DEGRADED_SENSOR` | Operate conservatively with impaired observability | Required input invalid without immediate shutdown | Data quality restored or operator action |

`MAINTENANCE`, `EMERGENCY` and `BLACK_START` override normal economic dispatch. A single numeric SOC threshold is insufficient; the state selection must consider available discharge power, battery temperature, load, generation forecast, generator availability and the validity of measurements.

### 30.8 Preliminary state-entry logic

Exact values remain commissioning parameters. The initial software should support the following configurable logic:

#### Enter `SURPLUS`

All conditions should normally be true for a sustained qualification period:

- Battery SOC is above the configured surplus threshold.
- BMS accepts additional charge or PV curtailment is occurring/imminent.
- PV power exceeds essential and normal active demand.
- Forecast energy margin is positive.
- No battery, inverter or thermal derate requires conservation.

#### Enter `CONSERVE`

Any configured combination may qualify:

- SOC below the normal reserve target.
- Forecast energy margin is negative.
- Extended low-solar forecast.
- Available discharge limit is declining.
- Generator unavailable while reserve is trending down.
- Battery/server-container cooling is constrained.

#### Enter `CRITICAL_RESERVE`

- Energy above emergency reserve is below the configured critical margin; or
- Estimated critical-load autonomy is below the configured response horizon; or
- Major PV/inverter fault creates an immediate deficit; or
- Battery power limit cannot support the present load safely.

#### Enter `EMERGENCY`

- BMS discharge is not permitted while AC loads remain; or
- Inverter reports a shutdown-class fault; or
- Battery/container temperature exceeds the emergency threshold; or
- Fire/smoke/emergency-stop logic requests power isolation; or
- Control authority and source state are contradictory in a way that could damage equipment.

### 30.9 State hysteresis and dwell times

To prevent oscillation:

- Every automatic state transition has a qualification delay.
- Recovery thresholds are separated from entry thresholds.
- Loads have minimum on/off times.
- Generator operation has minimum run and cooldown times.
- The site must demonstrate sustained positive energy margin before leaving `CONSERVE` or `CRITICAL_RESERVE`.
- Temporary irradiance changes caused by passing clouds must not trigger repeated load switching.
- An operator can freeze the state temporarily, but cannot override equipment-native safety limits.

---

## 31. Load Classification and Power-Budget Model

### 31.1 Load record

Every material controllable load must include:

```yaml
asset_id: energy.load.greenhouse_hvac.01
base_tier: 1
criticality: critical_process
rated_power_kw: 3.5
measured_power_point: energy.circuit.greenhouse_hvac.01/power_kw
control_method: local_controller_budget
minimum_service:
  mode: freeze_protection
  maximum_off_time_min: 20
restart:
  delay_s: 180
  inrush_class: high
  minimum_off_time_s: 300
shed:
  permitted: true
  local_survival_mode: true
  notification_required: true
```

### 31.2 Preliminary load tiers

The following is an initial classification, not a final electrical schedule.

#### Tier 0 — Physical protection and control survival

Normally not shed by the EMS:

- BMS and inverter controls.
- Fire/smoke detection.
- Emergency-stop circuits and control-panel power.
- Minimum battery/container environmental protection.
- Freeze-protection controls.
- Secondary control node, core timing and minimum communications.
- Local PLCs and critical sensor power.

#### Tier 1 — Essential service

Shed only under emergency plans with defined survival behavior:

- Core networking, routing, DNS, MQTT and digital-twin services.
- Potable-water control and minimum well/pump protection.
- Critical refrigeration/freezer loads.
- Security recording and essential cameras.
- Greenhouse freeze protection and minimum life-support ventilation.
- Required food-storage environmental control.
- Alarm and telephony functions.

#### Tier 2 — Important operations

May be reduced, duty-cycled or temporarily shed:

- Normal greenhouse environmental optimization above survival limits.
- Irrigation pumping when no immediate crop stress exists.
- Normal rack cooling above the minimum critical-compute profile.
- Normal residence HVAC and water treatment.
- Routine compost aeration within safe process bounds.
- General lighting and normal communications endpoints.

#### Tier 3 — Deferrable operations

Normally shed in `CONSERVE` or earlier if forecast margin is poor:

- Workshop machinery and welding.
- Bulk water heating.
- Hydrothermal spa heating, pumps and infrared emitters.
- EV, electric cart, mower and tool-battery charging.
- Heavy compute, rendering and archival processing.
- Nonurgent pumping and transfer operations.

#### Tier 4 — Opportunistic surplus operations

Enabled only under a granted surplus budget:

- Thermal-storage charging beyond normal needs.
- Experimental greenhouse lighting extension.
- Nonurgent hydrogel/lab processing.
- Data-processing jobs without deadlines.
- Optional equipment charging to absorb expected PV curtailment.
- Future synthetic-fuel production when integrated.

### 31.3 Dynamic priority

Base tier may be temporarily adjusted by process context. Examples:

- Orchard irrigation moves from Tier 3 to Tier 2 when measured soil moisture reaches a defined stress threshold.
- Greenhouse lighting remains deferrable, but freeze protection remains Tier 1.
- A scheduled welding period can receive a bounded priority reservation if the forecast supports it.
- Spa sanitation or post-use drying can become an important completion load after a session begins, even though initial spa operation is deferrable.
- Server workloads are split: core services remain Tier 1 while batch compute is Tier 3 or Tier 4.

Dynamic priority changes must have an expiry time and reason.

### 31.4 Power-budget leases

The EMS should allocate power through time-limited leases rather than simple global permission flags.

Example:

```json
{
  "lease_id": "power-20260806-000184",
  "asset_id": "workshop.system.main",
  "granted_kw": 8.0,
  "starts_at": "2026-08-06T15:00:00-04:00",
  "expires_at": "2026-08-06T16:30:00-04:00",
  "priority": 3,
  "reason": "operator_reserved_high_solar_window",
  "revocable": true
}
```

Subsystems must remain safe if a lease expires or is revoked. A power budget is not a safety permissive.

---

## 32. Load-Shedding Sequence

### 32.1 General rules

1. Verify that low reserve is real: validate SOC, BMS limits, total load and source state.
2. Freeze new Tier 4 and Tier 3 grants.
3. Cancel or defer not-yet-started tasks before interrupting active processes.
4. Shed controllable loads in ordered groups, not all at once.
5. Confirm measured load reduction after each group.
6. Escalate if the expected reduction does not occur.
7. Preserve completion of processes that would be damaged by abrupt interruption unless emergency conditions override them.
8. Log each action, expected kW reduction, actual reduction, and rejected command.

### 32.2 Preliminary shedding groups

| Group | Typical action | Examples |
|---|---|---|
| S1 | Stop opportunistic surplus loads | Thermal charging, optional compute, optional equipment charging |
| S2 | Defer new deferrable work | Workshop reservations, spa start, bulk pumping, vehicle charging |
| S3 | Reduce active deferrable loads | Throttle compute, pause charger, reduce noncritical HVAC |
| S4 | Enter subsystem conservation profiles | Greenhouse reduced-light profile, residence setback, reduced rack compute |
| S5 | Shed nonessential Tier 2 branches | Selected ventilation, noncritical lighting, nonurgent treatment |
| S6 | Enter critical survival profiles | Minimum greenhouse, water, storage, communications and refrigeration |
| S7 | Controlled IT shutdown | Stop noncore VMs, storage workloads, auxiliary server, then primary host as required |
| S8 | Emergency isolation | Only through defined emergency procedure and equipment-native controls |

The final schedule must identify the exact contactor, relay, API or local-controller command for each load. A load is not considered shed until measured current/power confirms the result.

### 32.3 Command failure during shedding

If a shed command is rejected or no reduction is observed:

- Raise a `load_shed_failed` alarm for the asset.
- Mark the expected reduction as unavailable.
- Recalculate reserve with the load still present.
- Advance to the next permitted load group if required.
- Do not repeatedly issue rapid on/off commands.
- Require operator inspection if a locally overridden load blocks the energy plan.

---

## 33. Load Restoration Sequence

### 33.1 Restoration qualification

Loads may be restored only after:

- Source state is stable.
- Battery discharge/charge limits are healthy.
- Forecast margin meets the configured recovery threshold.
- The EMS state has remained improved for the qualification time.
- No equipment or thermal condition requires continued conservation.

### 33.2 Restoration rules

1. Restore Tier 1 services first if any were reduced.
2. Restore process-survival functions before comfort or throughput.
3. Stagger starts based on inrush class and measured headroom.
4. Enforce each asset's minimum off time.
5. Restore one group, verify power and frequency stability, then continue.
6. Do not automatically restart workshop machinery or attended equipment.
7. Do not automatically resume a spa session.
8. Batch compute and equipment charging resume only under a new lease.
9. A process interrupted in an unsafe or invalid way enters `operator_review_required` instead of blindly resuming.

### 33.3 Preliminary restoration groups

| Group | Typical restoration |
|---|---|
| R1 | Core controls, communications, refrigeration, freeze protection |
| R2 | Water and greenhouse normal-minimum operation |
| R3 | Normal rack services and routine building systems |
| R4 | Deferred pumping, treatment and equipment charging |
| R5 | Batch compute and opportunistic loads |

---

## 34. Generator Coordination Narrative

### 34.1 Generator start request criteria

The EMS may request generator start when any configured criterion is satisfied and all start permissives are true:

- Critical-load autonomy is below the minimum required response horizon.
- Battery energy above emergency reserve is below the generator-start margin.
- Forecast energy margin remains materially negative.
- Battery discharge power limit cannot support expected load.
- A major PV/inverter outage is active.
- An operator requests generator support for a planned load and the request is permitted.

### 34.2 Start permissives

- Generator is in automatic/remote-enabled mode.
- No generator maintenance lockout.
- Fuel estimate above minimum start threshold.
- No active shutdown-class generator fault.
- Exhaust/ventilation and enclosure conditions are valid where monitored.
- Transfer/inverter interface is available.
- The BMS/inverter can accept generator power or the generator can directly support the bus according to the selected topology.

### 34.3 Start sequence

```text
1. EMS asserts generator_start_request.
2. Native generator controller performs preheat/crank/start logic.
3. EMS waits for running state, valid oil/fault status, stable voltage and frequency.
4. Inverter/ATS performs equipment-native synchronization/transfer.
5. Generator enters warm-up or minimum-load phase.
6. EMS grants charging/load budget within generator and battery acceptance limits.
7. EMS records start cause, start time, fuel estimate and expected stop condition.
```

The EMS must not imitate a generator engine controller with generic relays unless a purpose-built local controller is explicitly designed and commissioned for that role.

### 34.4 Generator run control

During generator support, the EMS should avoid inefficient short cycling. The strategy may combine:

- Support of current critical/important loads.
- Battery charging to a configurable stop target.
- Execution of selected useful loads when doing so improves generator loading without threatening reserve.
- Minimum run time.
- Maximum continuous run or inspection interval.
- Fuel-conservation mode when fuel reserve is constrained.

### 34.5 Stop criteria

Generator stop may be requested when all applicable conditions are satisfied:

- Minimum run time complete.
- Battery reaches the configured stop target or forecast margin recovers.
- PV has become sufficient and stable.
- Load is below the inverter/battery support limit.
- No active process requires generator completion.
- Cooldown can occur without dropping critical loads.

### 34.6 Stop sequence

```text
1. Remove or reduce discretionary generator-supported loads.
2. Reduce battery charging to the permitted transfer level.
3. Confirm inverter/battery can carry the live load.
4. Transfer source through the native inverter/ATS sequence.
5. Run generator cooldown.
6. Remove generator_start_request.
7. Confirm stopped state and update fuel/runtime records.
```

### 34.7 Generator failure

If start, synchronization or transfer fails:

- Enter or remain in `CRITICAL_RESERVE`.
- Shed loads according to the next required group.
- Raise a critical alarm with the failed sequence step.
- Preserve remaining battery for physical-protection and Tier 1 loads.
- Prevent repeated cranking beyond the generator controller's configured attempt policy.
- Require explicit reset after repeated failure unless the generator controller safely manages retries.

---

## 35. Black-Start and Recovery Narrative

### 35.1 Purpose

Black start is the controlled recovery from a fully de-energized AC distribution state or an inverter shutdown where normal supervisory services may also be unavailable.

### 35.2 Independent prerequisites

- BMS and inverter controls have protected DC power or an approved manual startup source.
- Battery temperature and voltage are within black-start limits.
- Fire/smoke/emergency-stop conditions are clear.
- Critical distribution can be isolated from noncritical branches.
- At least one local controller can execute the sequence without the primary server rack.

### 35.3 Preliminary sequence

```text
1. Verify emergency isolation and physical safety conditions.
2. Energize BMS, inverter controls and critical control power.
3. Close battery contactor through the native precharge sequence.
4. Start one inverter or the manufacturer-defined master group.
5. Energize the critical control/communications bus only.
6. Start secondary control node, core switch/router and minimum MQTT/time services.
7. Validate battery, inverter, frequency and critical-bus measurements.
8. Energize minimum refrigeration, water protection, greenhouse survival and security loads in staggered order.
9. Start generator if reserve or battery limits require it.
10. Start primary rack services only after the critical bus and container environment are stable.
11. Reconcile actual asset states with the digital twin before normal restoration.
```

No unattended restart of workshop machinery, spa equipment or other attended loads is permitted after black start.

### 35.4 State reconciliation

After services recover, the platform must not assume retained desired state equals physical state. It must poll or receive actual status, mark unknown points, clear expired commands, and reconstruct the event timeline from local-controller logs.

---

## 36. PV-Surplus Dispatch Narrative

When the system is in `SURPLUS`, the EMS should rank useful loads by readiness, benefit, conversion efficiency, process constraints and likely curtailment duration.

Preliminary order:

1. Serve live site loads.
2. Charge batteries within BMS limits and lifecycle policy.
3. Complete time-sensitive water, greenhouse, sanitation or drying tasks.
4. Pump water into elevated or strategic storage where useful.
5. Charge thermal storage or preheat permitted water systems.
6. Charge land-tool, mower, cart or vehicle batteries.
7. Run scheduled heavy compute or data-maintenance jobs.
8. Permit workshop reservations.
9. Permit spa preheating or other discretionary comfort loads.
10. Future: run synthetic-fuel or other high-energy production processes.

A surplus load must have a minimum useful run window. The EMS should not start a large thermal or mechanical process for a five-minute irradiance spike.

---

## 37. Energy Alarms and Events

### 37.1 Core alarms

| Alarm | Preliminary severity | Trigger basis |
|---|---|---|
| `battery_soc_low` | Warning/Major | SOC below configured threshold with delay |
| `battery_reserve_critical` | Critical | Energy above emergency reserve below limit |
| `battery_temperature_high` | Major/Critical | BMS or room/cell temperature threshold |
| `battery_cell_imbalance` | Warning/Major | Cell voltage delta above limit |
| `battery_charge_inhibited` | Major | Charging required but BMS denies charge |
| `battery_discharge_inhibited` | Critical | Live load exists but BMS denies discharge |
| `inverter_fault` | Major/Critical | Normalized fault class |
| `inverter_overload_risk` | Major | Load near available power limit with duration |
| `pv_generation_underperformance` | Warning | Irradiance-normalized output below model |
| `generator_start_failed` | Critical | Native start sequence exhausted |
| `generator_fuel_low` | Warning/Major | Fuel below response thresholds |
| `transfer_failed` | Critical | Source transfer not completed |
| `load_shed_failed` | Major | Command accepted/rejected without expected reduction |
| `load_restore_failed` | Warning/Major | Load did not return as expected |
| `energy_meter_data_invalid` | Major | Required meter stale/bad |
| `critical_load_growth` | Warning | Critical baseline exceeds planning envelope |
| `ups_runtime_low` | Major | Rack UPS runtime below graceful-shutdown need |
| `power_container_cooling_failed` | Critical | Cooling unavailable with rising thermal trend |

### 37.2 Events

Events are not alarms by default. Record at minimum:

- Energy-state transition.
- Load shed and restoration.
- Power-budget lease granted, revoked, expired or violated.
- Generator request, start, transfer, stop and cooldown.
- Black-start initiation and completion.
- Operator priority override.
- Inverter mode change.
- BMS contactor transition.
- Manual breaker/transfer state change where instrumented.
- Configuration or threshold revision.

---

## 38. Energy Dashboard Requirements

The energy dashboard should display:

- One-line power-flow diagram showing PV, battery, generator, inverter, critical panel, normal panel and major loads.
- Current site energy state and reason.
- SOC, usable energy, emergency reserve and estimated autonomy.
- Current BMS charge/discharge limits.
- PV production, total load, critical load and surplus/deficit.
- Forecast PV, load and energy margin.
- Generator status, fuel, runtime and latest start cause.
- Active shed groups and estimated/actual kW reduction.
- Pending restoration groups and minimum wait times.
- Load-budget leases and deferred-work backlog.
- Power-container temperature, cooling and safety state.
- Data-quality indicators for every value used by the state machine.

The dashboard must show measured values separately from calculated and forecast values.

---

## 39. Energy Verification and Commissioning Cases

The following tests are required before automatic shedding and generator control are enabled.

| Test ID | Test | Expected result |
|---|---|---|
| `EMS-T001` | Lose internet while site is normal | No control loss; local dashboards and alerts remain functional |
| `EMS-T002` | Stop primary digital-twin host | Local controllers continue; secondary node provides reduced status |
| `EMS-T003` | Stale SOC input | EMS enters conservative data-quality state; does not assume healthy reserve |
| `EMS-T004` | Simulated declining reserve | Tier 4 grants stop, then ordered shedding occurs after delays |
| `EMS-T005` | Shed command rejected | Alarm raised; reserve recalculated; next valid action selected |
| `EMS-T006` | PV recovers briefly then falls | Hysteresis prevents rapid restoration and reshedding |
| `EMS-T007` | Sustained PV recovery | Loads restore in staggered groups with headroom verification |
| `EMS-T008` | Generator unavailable during critical reserve | Critical alarm, deeper conservation and no uncontrolled repeated start request |
| `EMS-T009` | Generator starts successfully | Warm-up, transfer, charging and stop sequence execute through native controls |
| `EMS-T010` | Generator fails during run | Transfer back if possible; load shed; critical alarm with sequence context |
| `EMS-T011` | Battery discharge limit derates thermally | EMS reduces load before inverter overload or BMS trip |
| `EMS-T012` | Complete AC blackout | Black-start sequence restores critical bus before noncritical loads |
| `EMS-T013` | Manual override holds a Tier 3 load on | Override visible; failed shed identified; operator receives reserve impact |
| `EMS-T014` | Primary rack requires shutdown | Noncore VMs stop first; storage quiesces; host shuts down before UPS exhaustion |
| `EMS-T015` | Configuration threshold changed | Revision, author, old/new values and approval are recorded |

Each test record must include preconditions, injected condition, expected sequence, actual telemetry, commands, alarms, operator observations and pass/fail result.

---

## 40. Initial Database Entities for the Asset and Point Registry

A practical first schema should include:

```text
assets
asset_classes
asset_relationships
locations
points
point_bindings
point_samples_index
external_identifiers
commands
command_results
operating_modes
alarms
alarm_definitions
alarm_events
maintenance_plans
work_orders
inspections
calibrations
documents
configuration_revisions
power_load_profiles
power_budget_leases
energy_state_transitions
```

### 40.1 Separation of registry and historian

PostgreSQL stores identity, configuration, topology, current authoritative state and lifecycle records. The time-series historian stores high-volume samples. The registry must retain the historian series key and sampling policy, but should not become a high-frequency telemetry table.

### 40.2 Current-state cache

The digital-twin service may maintain a current-state table or cache containing:

- Last value.
- Timestamp.
- Quality.
- Source.
- Sequence number.
- Effective alarm state.
- Requested state.
- Actual state.

The current-state model must be reconstructable from the registry plus the message/event stream after service restart.

---

## 41. Initial API Surface

```text
GET    /api/v1/assets
GET    /api/v1/assets/{asset_id}
GET    /api/v1/assets/{asset_id}/points
GET    /api/v1/assets/{asset_id}/relationships
GET    /api/v1/points/{point_id}/current
GET    /api/v1/alarms/active
POST   /api/v1/commands
GET    /api/v1/commands/{command_id}
POST   /api/v1/operating-modes/{domain}
GET    /api/v1/energy/state
GET    /api/v1/energy/load-budgets
POST   /api/v1/energy/load-budgets/reservations
GET    /api/v1/work-orders
POST   /api/v1/work-orders
```

Write endpoints require authentication, authorization, an audit reason, an idempotency key and an expiry time where applicable.

---

## 42. Next-Pass Work Queue

The next document revision should proceed in this order:

1. Convert the point dictionary into machine-readable YAML and JSON Schema.
2. Build the first asset register for the known rack, network and power-container equipment.
3. Build the preliminary load schedule with exact branch, rated power, measured power, tier, control method and restoration policy.
4. Resolve the authoritative solar/battery/inverter/generator design revision.
5. Produce the detailed network and trust-boundary diagram.
6. Produce the water-system control narrative and water asset/point extensions.
7. Define the primary and physically separate secondary-control-node deployment architecture.
8. Define verification methods for every numbered functional requirement.
9. Import CAPEX references and equipment lifecycle fields into the asset registry design.
10. Prototype the EMS state machine against simulated MQTT telemetry before any physical control is enabled.

---

## 43. v0.2 Design Position

The key architectural move in this revision is the separation of three things that consumer automation platforms often collapse:

1. **Asset identity:** what the physical object is and how it relates to the property.
2. **Point identity:** what is measured or commanded, with units, quality and control meaning.
3. **Vendor binding:** how the current device exposes that point through Modbus, SNMP, MQTT, an API or a Home Assistant entity.

That separation is what allows the homestead digital twin to survive decades of equipment replacement and software migration.

The energy manager is likewise defined as a supervisory allocator, not a universal relay board. It manages reserve, priorities, forecasts, load budgets and orderly transitions while the BMS, inverters, generator controller, local PLCs and hardwired protection retain immediate equipment safety authority.

---

## 44. Machine-Readable Design Package

Revision v0.3 converts the naming and dictionary rules from sections 25 through 29 into a versioned implementation package. The package is not a replacement for the design narrative; it is the first executable representation of that narrative.

The package contains four data layers:

1. **Asset-class dictionary:** approved domains, lifecycle vocabulary, criticality, control-authority vocabulary, relationship types, asset classes, and reusable point profiles.
2. **Point dictionary:** canonical point names, point classes, data types, units, quality rules, control capability, and provenance status.
3. **Asset register:** functional and physical objects that are presently known from the homestead plans.
4. **Point bindings:** protocol-address placeholders that connect a canonical point to a current device interface without changing the asset or point identity.

Both YAML and JSON representations are supplied. JSON Schema Draft 2020-12 schemas validate the document shape, while the supplied Python validator checks cross-document references that JSON Schema cannot conveniently enforce.

## 45. Dictionary Reconciliation Rules

The v0.2 asset-class tables referenced several point names that were not present in the initial master point table. Revision v0.3 does not silently ignore those references. It introduces explicit `reconciled_v0_3` entries for the missing names.

The provenance values are:

- `baseline_v0_2`: direct translation of a v0.2 class or point.
- `reconciled_v0_3`: a new formal definition that closes an internal reference or represents source-supported hardware already present in the planning portfolio.

This makes it possible to distinguish established design language from formalization work performed during this pass.

## 46. Initial Asset Register Scope

The initial register contains 90 assets and 99 typed relationships. It covers the presently known site, power-container, energy, rack, network, monitoring, telephony, digital-twin service, and load-group objects.

The register treats functional positions separately from replaceable equipment. For example, `energy.inverter.power_container.01` is the stable functional inverter position. Its future serial number, MAC address, Modbus unit ID, and vendor model are external identifiers or properties and may change without changing the canonical functional identity.

### 46.1 Energy design conflict preservation

The register uses the revised 45 kWdc / four-inverter / 800 kWh nominal functional target because that is the current design basis in v0.2 and the agrivoltaic field diagram. It also records the earlier 26.4 kWdc / two-inverter / 240 kWh battery design as an unresolved predecessor. Battery chemistry, voltage, module count, and procurement configuration remain `TBD`.

### 46.2 Common-mode failure treatment

The combined power, battery, utilities, and server container remains in the asset hierarchy. A physically separate secondary low-power control node is now a formal planned asset rather than only a narrative recommendation. Its host structure, power source, replication strategy, and service allocation remain open design fields.

### 46.3 Network segmentation

VLANs 10, 20, 30, 40, 50, and 60 are represented as logical assets. Device records use the approved VLAN purpose but do not invent management addresses. Firewall, DHCP, gateway, and DNS details remain open fields until the network implementation package is produced.

## 47. Point-Binding State

The binding file currently contains 245 point-binding records for the battery bank, four inverters, PV array, UPS, PDUs, core rack/network devices, environmental monitors, and EMS load groups.

All unverified addresses remain `TBD`. The file intentionally does not manufacture Modbus registers, SNMP OIDs, Redfish paths, or Home Assistant entity IDs. A binding cannot be enabled for automatic control merely because the point dictionary marks it as control-capable; commissioning must explicitly permit the binding.

## 48. Automated Validation

The package validator performs:

- JSON Schema validation for all four YAML documents.
- Asset-ID uniqueness checks.
- Parent-reference checks.
- Relationship endpoint checks.
- Asset-class and allowed-domain checks.
- Point-profile reference checks.
- Point-binding asset and point reference checks.
- Point-ID construction checks.
- Detection of duplicate relationship IDs and point bindings.
- Verification that every asset-class and profile point exists in the point dictionary.

## 49. Revised Next-Pass Work Queue

With items 1 and 2 of the v0.2 work queue complete, the next sequence is:

1. Build the preliminary load schedule with branch, rated power, measured power, tier, control method, minimum on/off time, and restoration policy.
2. Resolve the authoritative solar, battery, inverter, and generator design revision.
3. Produce the network and trust-boundary diagram with firewall flows and service identities.
4. Assign preliminary rack-unit positions, PDU outlets, switch ports, VLANs, and power feeds.
5. Produce the water-system control narrative and water asset/point extensions.
6. Define the primary and physically separate secondary-control-node deployment architecture.
7. Import CAPEX references, ownership state, warranty state, replacement cost, and spare-parts linkage.
8. Prototype the EMS state machine against simulated MQTT telemetry before enabling physical control.

## 50. v0.3 Design Position

The system now has a concrete, validated data contract. The important result is not merely that the homestead equipment has been listed. The design now has stable rules for how assets are named, how points are defined, how vendor interfaces are bound, and how uncertainty is recorded without being disguised as a final decision.
