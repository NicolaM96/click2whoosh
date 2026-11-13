# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org).

## [1.1.0] - 2026-11-11

First public release.

### Added
- Support for both Zwift Click v2 controllers (left and right), kept awake without the Zwift app.
- Gears and steering sent to MyWhoosh over the network with OpenBikeControl, with a keyboard fallback.
- Every button configurable for click, double click and long press: gears, steering, music, volume,
  emotes, MyWhoosh's minimal UI, screenshots.
- Welcome window for permissions and pairing, and a Settings window.
- Battery level of each Click, a sound on every shift, alerts for low battery, MyWhoosh disconnected or
  unexpected firmware.
- English and Italian interface, following the language of the Mac.
- Packaging as `Click2Whoosh.app` and `.dmg` (`packaging/build.sh`).

### Changed
- Left Click: it is restarted as soon as it reports its lock, instead of on a timer.

### Known limitations
- Tested only with Click v2 firmware 1.1.0. Firmware 1.2.0 is not verified yet.
- MyWhoosh lets you choose Track A/B at forks only with the mouse.

[1.1.0]: ../../releases/tag/v1.1.0
