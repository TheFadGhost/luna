# CONTRACT — the HUD pane's message file

> **This is implemented. It stopped being a handoff on the day it landed.**
>
> The header this file carried until now — *"Nothing in this repo has been
> changed. This is a spec, not an implementation."* — was true when it was
> written and has been false since `lunad/ambient.py` shipped. It is corrected
> rather than deleted, because everything below it is still the **live
> contract** between lunad and the desktop, and the two halves are in
> different repositories: nothing else states what the pane will do with a
> torn write, an unchanged `id`, or a `ts` a minute old.
>
> | The contract below | Implemented in |
> |---|---|
> | writing the message file | `lunad/ambient.py::HudWriter`, the one writer and the one `id` sequence |
> | an ambient event's caption | `lunad/ambient.py::Notifier.pane` |
> | an ordinary spoken reply's caption | `lunad/hud.py::Caption`, called from `lunad/speech.py::Speech.say` |
> | retracting a caption on `luna hush` | `lunad/speech.py::Speech.cancel` |
> | removing the file on shutdown | `lunad/server.py::Daemon.close`, plus the `ExecStopPost` named below |
> | the overlay's own settings | a separate file and a separate contract — `[hud]` in `docs/CONFIG-SCHEMA.md`, published by `lunad/hud.py` |
>
> Tests: `tests/test_ambient.py` for the writer and the ownership rule,
> `tests/test_contract.py::HudCaptionCase` for the spoken caption.
>
> **The file is kept at this path on purpose.** `docs/STATE-OF-PLAY.md` and
> `docs/ARCHITECTURE.md` both cite it by name, and so does the QML side, which
> lives outside this repository and cannot be fixed by a commit here. Moving
> it into `docs/` would have broken three references to save a filename.
>
> One thing below has been overtaken and is marked where it appears: the
> `ExecStopPost` line now removes three files, not two.

## What already exists

`~/.config/omarchy/plugins/ghost.lunahud/` is a Quickshell service plugin that
paints a small click-through pane in a screen corner. It already reads
`$XDG_RUNTIME_DIR/luna/state` — the file `lunad/presence.py` publishes — so it
shows `idle` / `thinking` / `speaking` and the "not running" state today,
sharing that reader with the bar icon.

It had no words to show, because there was nowhere for Luna to put them. There
is now: everything below is built, and `lunad/hud.py::Caption` puts an ordinary
spoken reply there as well as the three ambient hooks.

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
ExecStopPost=-/usr/bin/rm -f %t/luna/state %t/luna/message %t/luna/hud.json
```

Without that, a SIGKILLed daemon leaves its last sentence on disk, and the
`ts` staleness rule is then the only thing standing between the user and a
ghost message at next login.

**Updated:** three files, not the two this line originally named. `hud.json`
is the overlay's settings, published by `lunad/hud.py` and specified as `[hud]`
in `docs/CONFIG-SCHEMA.md`; it joined this line when that landed, and it has
no `ts` of its own, so `ExecStopPost` is the *only* thing that removes it
after a crash. The drop-in lives at
`~/.config/systemd/user/lunad.service.d/20-presence.conf`, outside this
repository, so a change to it needs `systemctl --user daemon-reload`.

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
