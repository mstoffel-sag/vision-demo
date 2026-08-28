# relay-auto-open

A relay closed from Cumulocity releases itself again after a configurable delay.
Nothing to click, and nothing to remember: closing the relay is the only manual
step, and the open is guaranteed by the device rather than by an operator.

Built for thin-edge.io **2.0.1**, deployed as a `flow` software item.

## How it works

```
Cumulocity  ──c8y_Relay CLOSED──▶  c8y/devicecontrol/notifications
                                            │
                                            ├──▶ mapper on_fragment ──▶ relay-c8y-op ──▶ relay closes
                                            │
                                            └──▶ [flow.toml/main.js]  arm deadline = now + delay_minutes
                                                          │
                                                     onInterval, every 10s
                                                          │  deadline passed
                                                          ▼
                                                 relay-auto-open/open
                                                          │
                                                 [emit.toml/emit.js]
                                                          ▼
                                            c8y/devicecontrol/notifications
                                                          │
                                            mapper on_fragment ──▶ relay-c8y-op ──▶ relay opens
```

The flow never touches the hardware. It asks for the same `c8y_Relay` operation
an operator would have sent, and the existing handler
(`/etc/tedge/operations/c8y/c8y_Relay` → `/opt/tedge/bin/relay-c8y-op`) does the
actuation — so the auto-open path and the manual path are the same path.

`c8y/devicecontrol/notifications` is inbound-only across the built-in Cumulocity
bridge, so publishing to it drives the relay locally and sends nothing to the
cloud.

## Why two flows

The runtime refuses to let a flow publish to a topic it also subscribes to:

```
ERROR flows: Flow 'relay-auto-open' is dropping output message to
'c8y/devicecontrol/notifications' to prevent an infinite loop
```

This flow has to do exactly that — read relay operations and eventually write
one. So the write is split into a second flow that does not subscribe there:

| File | Role |
|---|---|
| `flow.toml` + `main.js` | The timer. Reads operations, arms the deadline, emits on `relay-auto-open/open`. |
| `emit.toml` + `emit.js` | The emitter. Reads `relay-auto-open/open`, publishes to the operation topic. |
| `params.toml.template` | User-configurable defaults. Must ship, or `${params.*}` fails to resolve. |

Both live in one package and install together. A side effect is that the plugin
lists **two** modules — `c8y/relay-auto-open` and `c8y/relay-auto-open/emit` —
so both appear in Cumulocity's installed-software list. Installing and removing
`c8y/relay-auto-open` handles both; the directory is what gets deleted.

## Configuration

Copy `params.toml.template` to `params.toml` on the device to override. An
existing `params.toml` is preserved across package updates.

| Parameter | Default | Meaning |
|---|---|---|
| `delay_minutes` | `5` | How long the relay may stay closed. Fractions allowed (`0.5` = 30 s). |
| `relay_fragment` | `c8y_Relay` | Operation fragment to watch. |

The relay opens up to one check interval (10 s, set on the step in `flow.toml`)
after the deadline.

## Behaviour

- **First close wins.** A repeated `CLOSED` while already armed does not push the
  deadline out — a retry or a double-click cannot extend the closed time
  indefinitely. Re-closing after an auto-open starts a fresh episode.
- **Any `OPEN` disarms**, including the flow's own (it comes straight back in on
  the subscribed topic) and one an operator sends early. `OPEN` never arms
  anything, which is what makes the echo safe rather than a loop.
- **`c8y_RelayArray` is ignored.** It carries an array of states and would need
  different handling; it is not silently half-supported.
- The emitted operation copies `externalSource` (and `deviceId`/`agentId`) from
  the operation that armed it, so the package is portable — it never has to be
  told which device it runs on. `externalSource` is not decoration: thin-edge's
  `C8yOperation` requires `externalSource` and `id`, and drops anything without
  them.
- The emitted `id` is a millisecond timestamp — numeric and unique, but naming no
  operation that exists in Cumulocity. Results are reported with name-addressed
  SmartREST (`501`/`503` `c8y_Relay`), so the relay still opens; expect
  Cumulocity to log the status update as unmatched when no `c8y_Relay` operation
  is pending. Uniqueness matters because thin-edge de-duplicates operations
  arriving in quick succession by id.

## Known limitation: a mapper restart forgets a pending open

State lives in `context.flow`, which the runtime does not persist across mapper
restarts. **If `tedge-mapper-c8y` restarts while the relay is closed, the pending
deadline is lost and the relay stays closed until someone opens it.**

There is no clean fix inside a 2.0.1 flow. Re-arming from the retained twin
(`te/device/main///twin/c8y_Relay`, which `relay-c8y-op` publishes after
actuating) would survive a restart, but the twin carries only `{"relayState":…}`
— no `externalSource` — so the flow could not build an operation the mapper
would accept. Treat the delay as best-effort supervision, not a safety
interlock; anything that must fail safe belongs in hardware or in the relay
handler itself.

## Build and deploy

```bash
scripts/build-flow-package.sh flows/relay-auto-open
```

Upload the resulting `.tar.gz` to **Management → Software repository** with:

| Field | Value |
|---|---|
| Name | `c8y/relay-auto-open` |
| Version | `0.1.0` |
| Software type | `flow` |

Then install it on the device from its **Software** tab. The plugin validates
the flow before it goes live and refuses to install one that does not load.

## Testing

`tedge flows test` runs the real runtime with no MQTT and no side effects:

```bash
OPS=c8y/devicecontrol/notifications
CLOSED='{"id":"1800380","status":"PENDING","c8y_Relay":{"relayState":"CLOSED"},
         "externalSource":{"externalId":"edge-gw-001","type":"c8y_Serial"}}'

# Arms but does not fire — the deadline is minutes away
echo "[$OPS] $CLOSED" | tedge flows test --mapper c8y \
    --flows-dir . --final-on-interval
```

To watch it fire, drop a `params.toml` with a tiny `delay_minutes` (e.g.
`0.0000001`) so the deadline is already in the past when `onInterval` runs, and
expect one message on `relay-auto-open/open`.

Note that `--final-on-interval` fires `onInterval` about a millisecond after the
message, so any delay larger than that correctly produces nothing — silence
there is a pass, not a failure.
