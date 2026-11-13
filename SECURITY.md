# Security policy

## Supported versions
Only the latest release receives fixes.

## Reporting a vulnerability
Please **do not open a public issue** for a security problem. Use
[Report a vulnerability](../../security/advisories/new) (GitHub private vulnerability reporting) and
include the app version, macOS version and the steps to reproduce.

This is a hobby project: I will answer as soon as I can, but I cannot promise a deadline.

## What the app does
Click2Whoosh runs locally. It talks to your Click over Bluetooth, sends commands to MyWhoosh over your
local network (OpenBikeControl), and can press keys through the macOS Accessibility permission. It has
no accounts, no server and no telemetry. Logs stay on your Mac in `~/Library/Logs/Click2Whoosh/`.

Before sharing a log or a Bluetooth capture in an issue, check that it contains nothing you want to keep
private.
