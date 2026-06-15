# OpenGolfSim

OpenFlight streams shots into [OpenGolfSim](https://opengolfsim.com/) using its
TCP/JSON developer API.

See the connector architecture in [README.md](README.md). This page covers
requirements and setup specific to OpenGolfSim.

## Requirements

- **OpenGolfSim desktop app** with the developer API enabled. The API listens
  on **TCP port 3111**.
- **Network reachability.** OpenFlight (the Raspberry Pi) and the PC running
  OpenGolfSim must be on the same LAN. You need the OpenGolfSim PC's IP.
- No account/credentials are sent by OpenFlight — the API has no auth.

Check OpenGolfSim's own documentation for enabling the developer API and any
account/licensing requirements: <https://help.opengolfsim.com/desktop/apis/>.

## Setup

1. **Enable the developer API in OpenGolfSim** and confirm it's listening on
   port 3111.

2. **Find the OpenGolfSim PC's IP** (e.g. `192.168.1.60`).

3. **Configure OpenFlight.** Copy the example config if you haven't already:
   ```bash
   cp config/sim.example.json config/sim.json
   ```
   Enable the OpenGolfSim connector:
   ```jsonc
   {
     "connectors": [
       {
         "type": "opengolfsim",
         "enabled": true,
         "host": "192.168.1.60",
         "port": 3111,
         "units": "imperial"
       }
     ]
   }
   ```
   Or pass it at launch:
   ```bash
   scripts/start-kiosk.sh --kld7 --opengolfsim 192.168.1.60
   ```
   `units` is `imperial` (mph) or `metric` (m/s) — match your OpenGolfSim
   setting.

4. **Start OpenFlight.** On connect it sends a `{"type":"device","status":"ready"}`
   frame; the header OpenGolfSim pill should turn **green**.

5. **Hit a shot.** It appears in OpenGolfSim, and the "Sent to OpenGolfSim"
   panel shows the values sent with measured/estimated badges.

## What gets sent

OpenGolfSim takes a compact ball-only message; it computes carry itself and
does not accept club data:

```json
{
  "type": "shot",
  "unit": "imperial",
  "shot": {
    "ballSpeed": 135.0,
    "verticalLaunchAngle": 11.1,
    "horizontalLaunchAngle": 1.2,
    "spinAxis": -2.5,
    "spinSpeed": 4800
  }
}
```

Spin (`spinSpeed`) uses the measured value when high-confidence, otherwise a
per-club model — the "Sent to OpenGolfSim" badges tell you which.

## Differences from GSPro

- **No heartbeat.** OpenGolfSim documents no keepalive, so OpenFlight sends
  none. Connection health relies on TCP-level detection plus automatic
  reconnect, so a silently dropped socket is detected a little less promptly
  than GSPro's heartbeat-backed link.
- **No documented ack codes.** Shot acknowledgements aren't documented, so
  OpenFlight treats sends as fire-and-forget (errors only surface as socket
  failures).

## Two transports — pick one

The `opengolfsim` connector reaches OGS one of two ways, set by `transport`:

| `transport` | Port | OGS side | Shots | Club sync |
|---|---|---|---|---|
| `openconnect` *(default, recommended)* | 921 | OpenConnect plugin | ✅ | ✅ |
| `native` | 3111 | OGS's built-in API | ✅ | ❌ |

Use exactly one — both pointed at the same OGS sends it duplicate shots. The
native transport sends the compact ball-only message shown above; the
openconnect transport speaks the shared OpenConnect V1 format (same as GSPro)
through OGS's plugin, which is the only path that also carries club.

## Club sync (recommended path: `transport: openconnect` + the plugin)

OpenGolfSim's **native** API streams shots fine but **cannot sync club** —
neither it nor the stock OpenConnect plugin sends club to a device, and the
native protocol has no club field (confirmed against OGS's own source). Club is
only available to an OGS *plugin* via the SDK `shotData.on('club')` event.

The working path uses OGS's **OpenConnect plugin** (port 921) plus a small fork
that forwards club changes:

1. Install **`tools/ogs-openconnect-plugin/`** (this repo) into the OGS plugins
   folder — it's the stock OpenConnect plugin plus club forwarding. See that
   folder's README for install paths, then select it as OGS's launch-monitor
   device.
2. Configure OpenFlight's **OpenGolfSim** connector with the **openconnect**
   transport (port 921):
   ```json
   { "connectors": [ { "type": "opengolfsim", "transport": "openconnect", "enabled": true, "host": "127.0.0.1", "port": 921 } ] }
   ```
   Under the hood this uses the shared OpenConnect V1 codec, but the connector
   reports as **OpenGolfSim** in the config, UI pill, and logs.

Now shots stream LM → OGS, and when you change the club in OpenGolfSim the
plugin forwards it as an OpenConnect `201` player message, which OpenFlight
applies to its club picker and carry/spin model. The `club` event fires on
*change* (no current-club getter exists), so change the club once after
connecting to sync.

If you'd rather not install a plugin, the native `opengolfsim`/3111 connector
still works for shots — you just set the club manually in OpenFlight.

## Club selection over the native API (one-way: OpenGolfSim → OpenFlight)

Club sync is **one-directional**. OpenGolfSim's API has no command for a device
to set the current club — the device can only send `device` status and `shot`
data. So OpenFlight cannot push your OpenFlight-side club choice to the sim;
instead the **sim is the source of truth** and pushes club changes to
OpenFlight via its `player` message:

```json
{
  "type": "player",
  "data": {
    "playerId": "…",
    "currentPosition": { "x": 0, "y": 0, "z": 0 },
    "club": { "name": "3W", "id": "3W", "distance": 205 }
  }
}
```

OpenFlight reads `data.club` and maps the two-letter id / name to its internal
club (used for shot tagging and the carry/spin model). The documented player
message carries no handedness field, so handedness is not tracked for
OpenGolfSim. The id/name mapping lives in
`src/openflight/opengolfsim/clubs.py`.

> **Known limitation (observed 2026-06-13):** in testing against the live
> OpenGolfSim app, the API connected and acked our device-ready with
> `{"status": 200}`, but **did not emit any `player` message when the club was
> changed** in the sim. OpenGolfSim's own connection example shows no subscribe
> step and doesn't demonstrate receiving player updates, so club sync from the
> sim may not be functional in current builds. OpenFlight's parser is ready for
> the documented `player` shape the moment OGS sends one — capture it by
> launching the server with `OPENFLIGHT_SIM_LOG_RAW=1` (logs inbound frames
> verbatim) and, if the format differs, the mapping is a quick fix. Until then,
> **set the club in
> OpenFlight's own picker**; outbound shots are unaffected.
>
> The message *shape* we parse matches OpenGolfSim's documented `player` event,
> but the club-id vocabulary (how irons/wedges are abbreviated) is likewise
> unverified.

## Troubleshooting

- **Pill stays amber (reconnecting):** OpenFlight can't reach `host:port`.
  Verify the developer API is enabled, the IP is correct, and port 3111 isn't
  firewalled.
- **Shots don't appear:** confirm OpenGolfSim is on a hittable screen and the
  API is connected. Check `sim_send` entries in the session log to confirm
  OpenFlight is sending.

## References

- [OpenGolfSim Developer API](https://help.opengolfsim.com/desktop/apis/)
