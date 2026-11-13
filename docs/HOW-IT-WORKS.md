# How Click2Whoosh works

Technical notes: how the Zwift Click v2 talks over Bluetooth, how commands reach MyWhoosh, and the
macOS details that took the longest to figure out. Everything here was verified with Click v2
firmware 1.1.0 and MyWhoosh 5.7.0 on macOS 27.

## Architecture

```
Zwift Click v2 (left + right) ──BLE──▶ ble.py ──▶ app.Dispatcher ──▶ actions.py ──▶ MyWhoosh (OpenBikeControl, TCP)
                                                   click / double / long        └─▶ macOS (media keys, keyboard, screenshot)
```

| File (in `src/`) | Role |
|---|---|
| `ble.py` | Bluetooth (bleak): scan, connect, decode buttons, keep the Click awake. |
| `obc.py` | OpenBikeControl: TCP server (port 36867+) and Bonjour announcement via the system `NSNetService`. |
| `actions.py` | What a button can do: OpenBikeControl commands (with keyboard fallback), keys, media keys, screenshot; default button sets |
| `app.py` | Menu bar app (rumps): config, `Dispatcher` (click / double click / long press / repeat), `ClickBridge` (asyncio thread, keep-awake policy), menu, sleep/wake handling |
| `settings.py`, `onboarding.py` + `.html` | Settings and welcome windows: HTML pages in a native `WKWebView` (`webwindow.py`), sharing `common.html` |
| `tests/test_logic.py` | Unit tests for decoding, actions, dispatcher and keep-awake logic (no hardware needed) |

## Zwift Click v2 over Bluetooth

- Advertises service `0xFC82` and manufacturer data `0x094A` (Zwift). The first byte tells the side:
  `0x0A` right, `0x0B` left.
- Handshake: write `RideOn` to the sync RX characteristic (`00000003-19ca-4651-86e5-fa29dcdd09d1`).
  The left side also gets `ff 04 00` after it.
- Buttons: messages with opcode `0x23`, a protobuf `RideKeyPadStatus` whose field 1 is a bitmask
  (bit **cleared** = pressed).

  | Right | bit | Left | bit |
  |---|---|---|---|
  | + | `0x01000` | − | `0x00100` |
  | Y | `0x00040` | ↑ | `0x00002` |
  | Z | `0x00080` | ↓ | `0x00008` |
  | A | `0x00010` | ← | `0x00001` |
  | B | `0x00020` | → | `0x00004` |

- **The left Click relays the right one.** Over its own connection the left side also reports the
  right side's buttons and battery (opcode `0x19`). Without filtering, every right button arrives
  twice. Each connection therefore keeps only its own buttons, and the battery is read only from the
  standard Battery Level characteristic `0x2A19` of each Click.
- Firmware revision: standard characteristic `0x2A26`. The app warns if it is not the tested one, since
  an update may change the protocol.

### Keeping the Click awake

Without a Zwift session the Click switches itself off after **60 seconds**; reads, `RideOn` and other
traffic do not prevent it.

- A "wake" sequence (observed in BikeControl's Bluetooth traffic) keeps it on for about **944 s** on
  the same connection: re-enable notifications on `…0100` and `…0102`, then write `01` to `…0102`
  (reply `01 f0 00`), an 18-byte message to `…0100` (reply `00`), and `03` to `…0102` (reply `12 00…`).
  Click2Whoosh renews it every 5 minutes. It keeps both Clicks connected without the Zwift app, but
  only the right one keeps sending buttons (see the next point).
- **The left Click locks.** Unless Zwift has unlocked it (riding in Zwift in the last ~24 h), it stops
  sending buttons about **51 s** after it starts, although it stays connected and the wake sequence
  does not help. It announces this with a status message
  `ff 05 00 ea 05 <len> 0a 0c <serial> 10 <state> 18 <value> …`: `state` is 0 at the start and 1 at the
  lock (`value` goes from 15 to 900). The right Click does not lock. Click2Whoosh restarts the left
  Click (`RESET`) as soon as it sees state 1 (or after 60 s if the message does not come), and
  reconnects to it directly: about 4 s without buttons each time, and more battery use. Why some
  left Clicks were not locked in earlier tests is not explained.
- `RESET` (opcode `0x18`) cancels the remaining time: the Click turns off about a minute later.
  `POWER_OFF` (`0x0F`) is answered with NOT_SUPPORTED. The app uses `RESET` when you quit, when the Mac
  sleeps, and when the Click is no longer needed (MyWhoosh closed and no recent button presses).
- Fallback if a firmware refuses the sequence: reset the Click just before the minute is up
  (loses buttons for 3–4 s each time).

## MyWhoosh

- **OpenBikeControl** (open protocol): the app announces `_openbikecontrol._tcp` with Bonjour and
  MyWhoosh connects over TCP. Button message: `[0x01, id, state, …]`. MyWhoosh declares gears,
  steering, menu navigation and camera, but **only executes gears and steering**. It also sends a
  `0x00` byte every ~4 s (heartbeat, not in the spec).
- **Keyboard fallback**: if MyWhoosh is not connected over the network, gears and steering use the
  keyboard (K shift up, I shift down, A/D steer), only while MyWhoosh is in front. Other keys:
  U minimal UI, 1–7 emotes. MyWhoosh has no keyboard shortcut for camera, U-turn or menus.
- **MyWhoosh Link** (the companion app's protocol) would offer camera angles, emotes, U-turn and tuck:
  MyWhoosh connects over TCP to port 21587 and receives JSON lines
  `{"MessageType":"Controls","InGameControls":{…}}`. But MyWhoosh only connects after its server tells
  it where the Link app is (same account), the Link app does not run on macOS, and a local listener
  is never contacted. So it is not usable from a Mac.

## macOS details

- **Permissions**: Bluetooth, Local Network (needed for Bonjour; its state is not exposed, so the
  welcome window checks it by browsing for its own announcement) and Accessibility (keyboard and media
  key events are posted with `CGEventPost`, so music control needs it too). `CGPreflightPostEventAccess`
  caches its first answer until restart; `AXIsProcessTrusted` does not.
- **Windows**: while a Click2Whoosh window is the key window, Click buttons only light up on the
  drawing. On recent macOS `activateIgnoringOtherApps` may be ignored, so the app uses `NSApp.activate()`
  and asks the window for `isKeyWindow` on every press instead of trusting notifications.
- **Notifications**: `NSUserNotification` (used by rumps) no longer works. `UNUserNotificationCenter`
  refuses ad-hoc and self-signed apps ("Notifications are not allowed for this application"), so the
  app falls back to `osascript` (generic icon). An Apple Development certificate should fix it.
- **Signing**: `build.sh` signs with a keychain certificate named "Click2Whoosh" if present, otherwise
  ad-hoc. A stable signature keeps the Accessibility permission across builds.
- **Packaging**: PyInstaller (universal2, Python 3.14). Several copies with the same bundle id confuse
  Spotlight, so `build.sh` unregisters the copies in `dist` and in the disk image.
