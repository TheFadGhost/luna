# HANDOFF — the HUD pane's message file

The desktop half is built and live. This file specifies the **one thing lunad
has to do** for it to carry Luna's words: write a message file.

Nothing in this repo has been changed. This is a spec, not an implementation.

## What already exists

`~/.config/omarchy/plugins/ghost.lunahud/` is a Quickshell service plugin that
paints a small click-through pane in a screen corner. It already reads
`$XDG_RUNTIME_DIR/luna/state` — the file `lunad/presence.py` publishes — so it
shows `idle` / `thinking` / `speaking` and the "not running" state today,
sharing that reader with the bar icon.

It has no words to show, because there is nowhere for Luna to put them.

## The contract

**Path:** `$XDG_RUNTIME_DIR/luna/message` — a sibling of `state`, in the
directory `presence.py` already creates.

**Format:** one JSON object, UTF-8, no wrapper array, no trailing newline
required.

```json
{
  "id": 17,
  "text": "The build finished. Three tests failed in tests/test_dispatch.py.",
  "ts": 1756570000.123,
  "ttl": 8,
  "kind": "say"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | integer | **yes** | Monotonically increasing within a daemon run. **This is what makes a message new.** The pane shows a message when `id` differs from the one it last showed; the same sentence said twice is two messages only if `id` moves. Start at 1 on daemon start. |
| `text` | string | **yes** | Plain text. No markup, no ANSI. The pane wraps it and elides after 4 lines, and truncates anything past 500 characters, so write for a sentence or two — this is a glance surface, not a transcript. |
| `ts` | number | no | Unix seconds, float. Used only for staleness: a message more than **60 s** older than the moment the pane reads it is silently ignored, so a file left behind by a previous run does not pop open at login. Omitting it means "never stale", which is the wrong default for a daemon — **send it**. |
| `ttl` | number | no | Seconds the pane stays up. Default **8**. `0` means "stay until replaced or removed". The countdown does not start while `state` says `speaking`, so a long spoken answer is not outlived by its own caption — set `ttl` for the reading time after she stops talking, not for the whole turn. |
| `kind` | string | no | `"say"` (default) or `"alert"`. `alert` turns the caption line urgent-coloured. Anything unrecognised reads as `"say"`. |

**Writing it:** atomically, the same way `presence.py` writes `state` — write
`message.tmp` in the same directory, then `os.replace()` it over `message`. A
partial write is read as malformed, and malformed is a no-op (see below), so a
torn write costs one missed message rather than a blank pane. Never write in
place.

**Removing it:** deleting the file **dismisses whatever is on screen**, at once.
That is the intended way to retract a message (a `luna hush` mid-answer, for
example). It also means the file must be removed on shutdown — add it to the
same `ExecStopPost` line `20-presence.conf` already uses for `state`:

```ini
ExecStopPost=-/usr/bin/rm -f %t/luna/state %t/luna/message
```

Without that, a SIGKILLed daemon leaves its last sentence on disk, and the
`ts` staleness rule is then the only thing standing between the user and a
ghost message at next login.

## How the pane degrades — you cannot break it from here

All three of these are tested, and all three are silent. No error card, no
placeholder, no "waiting for Luna".

| On disk | Pane does |
|---|---|
| file absent | nothing — this is the normal state today |
| unparseable JSON, or a truncated write | **keeps whatever is already on screen.** A parse failure is not news and must never blank a good message |
| valid JSON with no `text`, or empty `text` | ignored |
| `id` unchanged from the last shown | ignored (no re-show, no timer reset) |
| `ts` older than 60 s | ignored, but the `id` is still consumed |
| `text` longer than 500 chars | truncated |

## What NOT to do

- **Do not open a socket for this.** The reasoning is the same one that killed
  the `subscribe` op: a subscriber is a socket the daemon has to write to, and
  a socket has a buffer a stalled reader fills — so the moment a message is
  published down a pipe nobody is draining, the blocked thread is the thread
  that was about to speak. A file has no reader-side backpressure. The pane
  reads it through inotify, so nothing polls.
- **Do not send every log line.** This surface interrupts a person who did not
  ask for it. It is for things she would otherwise have to say out loud.
- **Do not depend on it being read.** The pane may not be running. Writing the
  file must never block or fail the reply path.

## Where the rest of it is written down

`~/.config/omarchy/CUSTOMISATIONS.md` §8a.14 — the pane itself, the
click-through mechanism, the alt-drag bind and its cost, and the dead ends.
