# Upstream

`camera_protocol.py`, `cloud.py`, `session.py` and `wire.py` are from
[papou-github/wansview-r2](https://github.com/papou-github/wansview-r2/tree/49bff2cc556614d1b7399e69aeeb5e4f6a86ac2f),
at commit `49bff2cc556614d1b7399e69aeeb5e4f6a86ac2f`. See the accompanying MIT license.
The upstream relay protocol is experimental and was tested on the R2 family.
The local copy handles 16-bit sequence rollover and reports relay and media progress to the viewer.
It also supports PTZ, live quality changes, and AAC listening over the authenticated live session, matching the controls used by Wansview Cloud for Windows.
Microphone transmission uses the talk negotiation and audio framing used by Wansview Cloud for Android.
