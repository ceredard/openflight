# Simulator Connectors

OpenFlight can stream each shot into golf simulators in real time. Connectors
are **optional** and **additive**: you can run zero, one, or several at once
(e.g. GSPro and OpenGolfSim simultaneously). The shot-detection pipeline is
never modified — connectors are listeners that hang off the shot it already
produces.

Supported simulators:

- [GSPro](gspro.md) (OpenConnectV1)
- [OpenGolfSim](opengolfsim.md)

This document explains how the connector layer works and how to add a new
simulator. For setting up a specific simulator, see its page above.

## Quick start

1. Copy the example config and enable the simulator(s) you use:
   ```bash
   cp config/sim.example.json config/sim.json
   ```
   ```jsonc
   {
     "connectors": [
       { "type": "gspro",       "enabled": true,  "host": "192.168.1.50", "port": 921 },
       { "type": "opengolfsim", "enabled": false, "host": "127.0.0.1",    "port": 3111 }
     ]
   }
   ```
2. Start OpenFlight normally — enabled connectors come up automatically:
   ```bash
   scripts/start-kiosk.sh --kld7
   ```
3. Or enable/override a connector from the command line for a one-off run:
   ```bash
   scripts/start-kiosk.sh --gspro 192.168.1.50            # host, default port 921
   scripts/start-kiosk.sh --opengolfsim 127.0.0.1:3111    # host:port
   scripts/start-kiosk.sh --no-sim                        # disable all, ignore config
   ```
   Precedence: `--no-sim` > per-sim flag > `config/sim.json` > built-in defaults.

The header shows a status pill per connector (green = connected, amber =
connecting/reconnecting, red = error, gray = disabled). After each shot, a
"Sent to <sim>" panel shows every field that was sent with an **M** (measured)
or **E** (estimated) badge.

## How it works

```
OPS243 + K-LD7  ──►  shot pipeline  ──►  on_shot_detected()
                                              │  (UI emit unchanged)
                                              ▼
                                       resolve_shot(shot)            ← sim/resolver.py
                                              │  ResolvedShot + provenance
                                              ▼
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        GSProCodec      OpenGolfSimCodec    (future)   ← per-sim codec
                              │               │
                        TcpSimClient    TcpSimClient                   ← sim/transport.py
                              ▼               ▼
                          GSPro :921     OpenGolfSim :3111
```

The design separates the parts that are **shared across all simulators** from
the parts that are **specific to one protocol**:

| Layer | File | Shared? |
|---|---|---|
| TCP lifecycle, reconnect/backoff, JSON framing, optional heartbeat | `sim/transport.py` | shared |
| Shot → resolved fields + measured/estimated provenance (fallback table) | `sim/resolver.py` | shared |
| Connection state, player state, inbound event types, resolved shot | `sim/types.py` | shared |
| Connector = codec + transport, and the codec registry | `sim/codec.py` | shared |
| Config (`config/sim.json` + CLI merge) | `sim/config.py` | shared |
| **Wire format**: serialize a shot, parse inbound messages, heartbeat | `gspro/codec.py`, `opengolfsim/codec.py` | **per-sim** |

The key idea: two simulators differ only in their **wire format** and their
**inbound message shapes**. Everything else — connecting, reconnecting,
filling in missing fields, tracking provenance, fanning out to multiple
targets — is written once and reused.

### The resolver and provenance

`resolve_shot()` turns a `Shot` into a `ResolvedShot` with every
simulator-relevant field populated, applying a fallback when a measurement is
missing (e.g. no measured spin → a per-club model value). Each field is tagged
`measured` or `estimated`. This logic lives in exactly one place, so every
connector is honest about what came from hardware versus a model, and the UI
renders identical badges for any simulator.

A shot is **only dropped** when ball speed is missing — every other field has
a model fallback.

### The codec contract

A codec is a small class implementing the `Codec` protocol
(`sim/transport.py`):

```python
class Codec(Protocol):
    name: str
    def build_shot(self, resolved: ResolvedShot) -> bytes: ...      # serialize a shot
    def parse_inbound(self, frame: bytes) -> list[InboundEvent]: ... # decode sim → us
    def heartbeat_bytes(self) -> Optional[bytes]: ...                # None = no keepalive
    def on_connect_bytes(self) -> Optional[bytes]: ...               # e.g. a hello frame
    def fields_for_target(self) -> list[str]: ...                    # which fields it sends
```

Inbound messages are normalized into protocol-neutral events
(`PlayerUpdate`, `ShotAck`, `SimError`) so the server handles club changes and
errors the same way regardless of simulator.

## Adding a new simulator

1. **Create a codec.** Add `src/openflight/<sim>/codec.py` implementing the
   `Codec` protocol. Map `ResolvedShot` fields to the simulator's wire format
   in `build_shot`, and translate inbound messages to `PlayerUpdate` /
   `ShotAck` / `SimError` in `parse_inbound`. Return `None` from
   `heartbeat_bytes()` if the protocol has no keepalive.
2. **Register it.** Add the type to `_codec_for()` and to `_DEFAULTS` /
   `KNOWN_TYPES` in `sim/config.py` (default host/port/units).
3. **Add a CLI flag (optional).** Mirror `--gspro` / `--opengolfsim` in
   `server.py` `main()` and in `scripts/start-kiosk.sh`.
4. **UI display names.** Add the target → display-name entry in
   `ui/src/components/SimStatus.tsx` and `SimShotBadges.tsx`.
5. **Tests.** Add a codec round-trip test (serialize + parse inbound) and a
   club-mapping test. The shared transport/resolver are already covered.
6. **Docs.** Add `docs/simulator/<sim>.md` and link it from this page.

You do **not** touch the transport, the resolver, the server fan-out, or the
config loader — that's the point of the abstraction.

## Session logging

Each connector logs three entry types to the session JSONL:

- `sim_send` — a shot forwarded to a simulator (target, shot number, per-field
  values + provenance)
- `sim_status` — a connection-state change (connected, reconnecting, error)
- `sim_player` — a player/club update pushed back by the simulator
