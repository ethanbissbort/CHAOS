# Mosquitto broker — credentials and topic-prefix authorisation

This directory holds the broker configuration for the homestead telemetry and
command bus (SDD section 8.2). Two files matter:

| File | Role |
|---|---|
| `mosquitto.conf` | Mounted read-only into the container. Safe to commit — it contains no secrets. |
| `acl.example` | A worked example of the topic-prefix ACL. **Copy it; never mount it.** |

The live `passwd` and `acl` files are generated on the property into the
`mosquitto-config` volume at `/mosquitto/config/local/`. They are not in the
repository because they name real device identities and hold real password
hashes (SDD section 15.2).

---

## Why topic-prefix ACLs, and not just a broker password

MQTT has no built-in notion of "this device owns this equipment". Without an
ACL, every authenticated client can publish to every topic. On this property
that means:

- A compromised ESP32 in a greenhouse could publish
  `chaos/energy/power_container/inverter_01/cmd/stop` and shut down the
  inverters.
- The same node could publish a fake `battery_soc_pct` of 100 and starve the
  EMS of the truth it needs to protect the battery.
- A camera on VLAN 40 could raise a flood of fake alarms and bury a real one.

None of those are exotic attacks. They are what a single default password and a
flat topic space allow by accident.

The ACL closes them by binding each identity to the asset prefixes it actually
owns. The topic grammar is what makes this cheap to express.

### The one-character rule

From `src/chaos/topics.py` (SDD 10.1, 26.2):

```
chaos/<domain>/<location>/<class>_<instance>/<point_name>     telemetry   1 level
chaos/<domain>/<location>/<class>_<instance>/availability     last will   1 level
chaos/<domain>/<location>/<class>_<instance>/event/<name>     event       2 levels
chaos/<domain>/<location>/<class>_<instance>/cmd/<name>       command     2 levels
chaos/<domain>/<location>/<class>_<instance>/cmd/<name>/ack   ack         3 levels
```

Commands live one level deeper than telemetry. So:

```
topic write chaos/energy/power_container/inverter_01/+     # telemetry only
topic write chaos/energy/power_container/inverter_01/#     # telemetry AND commands
```

The first grants a gateway everything it needs to report. The second lets it
command itself, which defeats the entire supervisory-control model (SDD 5.3:
the platform *requests*, Level 1 and Level 0 *decide*). Use `+`.

### Publishing rights by principal

| Identity | Telemetry write | Command write | Command read | Notes |
|---|---|---|---|---|
| `svc-twin-primary` | own service topics only | all assets | all | The only command publisher. Every command is already in the audit log. |
| `svc-twin-secondary` | own service topics only | **none** | all | Observes and alerts. Commanding would be split-brain (SDD 16.1). |
| `svc-bridge-secondary` | bridge availability only | none | inbound only | Telemetry flows in; nothing flows out. |
| `svc-node-red` | own service topics | per delegated domain, added at commissioning | all | Orchestration, not hard safety logic (SDD 8.4). |
| `svc-home-assistant` | own service topics | none by default | all | Not the authoritative model (SDD 8.3). |
| `gw-*` field gateways | own assets only, `+` level | **none** | own assets only | May write `cmd/+/ack`. |
| `dev-*` sensors | own asset only | none | none | No command path at all. |
| `svc-historian`, `svc-grafana` | none | none | read-only | |
| `svc-healthcheck` | none | none | `$SYS/broker/uptime` only | |

Note the deliberate asymmetry in the first row: the twin can publish commands
but **not** telemetry. If the twin could write telemetry it could paper over a
real condition, and the historian would be a record of what the platform
believed rather than what the plant did.

Note also that no gateway may publish under `alarm/`. Alarms are derived by the
platform from telemetry, so a compromised gateway cannot manufacture an alarm
flood — it can only report bad values, which the quality model and the alarm
engine's own logic are there to handle.

---

## Bootstrapping credentials

Run these on the node hosting the broker. Every device gets its own identity and
its own generated secret; nothing is reused between devices, and nothing is
reused between the primary and secondary nodes.

```sh
COMPOSE="docker compose -f deploy/docker-compose.yml"

# 1. Create the local config directory inside the volume.
$COMPOSE exec mosquitto mkdir -p /mosquitto/config/local/certs

# 2. Create the first identity (-c creates the file; omit -c for every one after).
$COMPOSE exec -it mosquitto mosquitto_passwd -c /mosquitto/config/local/passwd svc-twin-primary

# 3. Add the rest, one per device or service.
for u in svc-twin-secondary svc-bridge-secondary svc-node-red svc-home-assistant \
         svc-historian svc-grafana svc-healthcheck \
         gw-power-container gw-rack-power dev-container-leak-sensor; do
  $COMPOSE exec -it mosquitto mosquitto_passwd /mosquitto/config/local/passwd "$u"
done

# 4. Install the ACL. Start from the example, then edit it for the real fleet.
docker compose -f deploy/docker-compose.yml cp \
  deploy/mosquitto/acl.example mosquitto:/mosquitto/config/local/acl

# 5. Lock down permissions and reload.
$COMPOSE exec mosquitto chmod 600 /mosquitto/config/local/passwd /mosquitto/config/local/acl
$COMPOSE kill -s HUP mosquitto
```

Generate the passwords themselves rather than inventing them:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Record which identity belongs to which asset in the registry as an
`ExternalIdentifier` (`id_type: mqtt_username`) — not in a spreadsheet. The
identity is a vendor binding, not the asset's identity (SDD 25.3 rule 7).

---

## Enabling TLS

The design requires TLS on routed or untrusted links (SDD 8.2). The `8883`
listener block in `mosquitto.conf` is commented out rather than half-configured,
so the broker can never come up serving plaintext on the TLS port.

1. Issue a CA and a server certificate for the broker. A private CA under your
   own control is appropriate here; there is no public name to validate.
2. Place `ca.crt`, `server.crt`, `server.key` in
   `/mosquitto/config/local/certs/` with mode `600`.
3. Uncomment the `listener 8883` block.
4. Set `CHAOS_MQTT_TLS=true` in `deploy/.env`.
5. Once every gateway holds a client certificate, set `require_certificate true`
   and `use_identity_as_username true`. That binds the broker identity to the
   certificate rather than to a password, which is what SDD 15.2 means by
   "certificate-based credentials where supported".

Until step 5, plaintext `1883` remains acceptable only on a switched,
VLAN-separated link inside the property (SDD 15.1). Anything wireless or routed
should be moved to `8883` first.

---

## Verifying the ACL

An ACL that has never been tested is a comment. Section 4 of `acl.example` lists
the exact `mosquitto_pub` invocations that must be **denied** and the one that
must succeed. Run them after every ACL change and attach the output to the
subsystem's commissioning record (SDD 19 step 12).

One trap: for MQTT 3.1.1 clients Mosquitto silently drops a denied publish and
the publisher exits 0. Either watch the broker log for `Denied PUBLISH`, or test
with `-V mqttv5`, which returns a reason code to the client.

```sh
docker compose -f deploy/docker-compose.yml logs -f mosquitto | grep -i denied
```

---

## Retained messages, last will, and persistence

Three broker behaviours the platform depends on:

- **Retained messages** carry current state to late subscribers. After a twin
  restart, retained topics repopulate the current-state cache without waiting
  for the next sample. `retain_available true`.
- **Last will** on `<asset prefix>/availability` is how the platform learns a
  gateway died rather than merely went quiet. Every gateway must set one; the
  ACL grants each identity write access to its own availability topic.
- **Persistent sessions** hold queued messages for a disconnected gateway
  (`persistence true`, `persistent_client_expiration 14d`), which matters on
  flaky LoRa and Wi-Fi links.

Retained state is a convenience, not a source of truth. After any service
restart the platform must reconcile actual device state rather than trusting a
retained desired state (SDD 35.4).
