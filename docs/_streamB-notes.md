# Stream B — the `[hud]` settings, their publisher, and the spoken caption

Written to a scratch file rather than into `docs/STATE-OF-PLAY.md`, because two
sibling agents were working in this repository at the same time and that file
is the guaranteed conflict. Merge it into §"What is built" and the wiring
tables; nothing here needs to survive as a file of its own.

## What landed

**A `[hud]` table, six keys, and a file the desktop can read.** The orb overlay
is QML in the Quickshell engine — another process, another language, and one
with no TOML parser. So `lunad` does not ask it to read
`~/.config/jarvis/config.toml` (0600, next to `secrets.env`); it **projects**
the six keys into `$XDG_RUNTIME_DIR/luna/hud.json`, one JSON object, written
atomically on daemon start and on every settings reload, removed on shutdown.
`lunad/hud.py` is that projection and nothing else reads it back.

The six are exactly the contract's: `enabled`, `corner`, `scale`,
`idle_visible`, `caption`, `sprite`, with the contract's defaults. `scale` is
clamped to 0.5–3.0 rather than refused, an unrecognised `corner` or `sprite`
falls back to its default, and one bad field never costs the others — the
overlay has no error state to show, so there is nothing to show it.

**The publisher deduplicates.** The daemon offers every settings reload to
`publish()`, unconditionally and deliberately not gated on a `hud.` prefix: the
publisher compares against what it last wrote and does nothing when nothing
moved, so a prefix gate would buy no syscalls and would be one more place to
forget a key. Without the dedup, every reload would move the mtime, wake the
overlay's `FileView` and cost a QML re-parse — thirty times an hour on a daemon
that stats its config file every two seconds.

**Spoken replies now reach the HUD.** `ambient.HudWriter` was only ever called
by the crash, battery and update hooks, so the one surface built to carry
Luna's words carried everything except them — the pane sat empty through an
entire spoken answer. `lunad/hud.py::Caption` is the speech path's user of that
same shared writer, called from `Speech.say`, honouring `[hud] caption` and
`[hud] enabled`. It carries the *spoken* form (already capped by
`[voice] max_spoken_chars`, cut at a sentence boundary), one message per
utterance and never one per sentence.

`Speech.cancel` retracts it, which is what makes `luna hush` clear the words on
screen as well as the words in the air. Every `say()` opens with a barge-in
cancel, so that one passes `retract=False`: a cancel that retracted would
unlink the file microseconds before rewriting it, which the pane reads as
"dismiss" then "show" — a flicker on every single reply. The replacement is one
`os.replace` instead.

Nothing on this path can fail a reply. `Caption` swallows everything and
complains once per process, not once per sentence.

**A Jarvis pane, "Overlay", between Ambient and Memory.** All six keys, built
from `Binder.control_for` like every other pane, with the two dependent blocks
stated the Ambient pane's way: an inert row is dimmed *and* has a sentence
above it saying why. `scale` is a `Real` and therefore a spin button, exactly
like `[voice] speed` — there is no slider widget in `jarvis/widgets.py` and
inventing one for a single row would be a second control kind to keep themed
for no gain.

## Two things worth keeping

**The ownership tag on `HudWriter`, and where it had to live.** Two paths now
write one message file, and each may retract only its own. `clear(only_mine=...)`
was a boolean whose docstring already promised what a boolean could not
deliver — *"ambient must not wipe a caption the speech path put there"* — and
with a second writer sharing the file, `_mine` was true for both of them and
every clear took whatever happened to be up. It is now an owner tag.

The tags live in `lunad/config.py`, not beside the writer that uses them, and
that is not fussiness: `tests/test_ambient.py::NeverSpeaksCase` reads the
shipped source of `lunad/ambient.py` and fails if the word `speech` appears in
its code at all. A `SPEECH_OWNER = "speech"` constant in `ambient.py` failed
that guard on the first run. The guard is right and the constant moved.

**`hud.json` is the only one of the three runtime files with no staleness rule
of its own.** `state` absent means "not running" and the widget acts on that; a
stale `message` expires on the pane's 60 s `ts` rule. A stale `hud.json`
expires never — it carries no timestamp, it is settings rather than an event,
and the overlay is *supposed* to keep using it while lunad is not running. So a
`SIGKILL` in the second between a settings change and the next start would pin
the overlay to the old corner and the old size indefinitely, which is why it
had to join `20-presence.conf`'s `ExecStopPost` rather than be left to the
daemon's clean shutdown.

## Outside the repository

`~/.config/systemd/user/lunad.service.d/20-presence.conf` — its `ExecStopPost`
now removes three files rather than two. **Needs `systemctl --user
daemon-reload`.** Recorded in `~/.config/omarchy/CUSTOMISATIONS.md` §8a.14 and
in its §9 inventory.

## `HANDOFF-hud.md`

Kept at its path, with the stale header replaced by a correct one. Not retired
into `docs/`, because `docs/STATE-OF-PLAY.md` and `docs/ARCHITECTURE.md` both
cite it by name and the QML side cites it from outside this repository —
moving it would have broken three references to save a filename. The new
header maps each clause of the contract to the code that implements it, and
the `ExecStopPost` example inside it is corrected to three files.

## Not done

- No test in `jarvis-settings/tests` builds a pane, so the Overlay pane's
  construction and its dependent-state handler were verified by a throwaway
  headless script, not by a case in the suite. That gap predates this work and
  applies to all nine panes; a `test_panes.py` that builds each one would be a
  worthwhile separate change.
- No screenshot of the new pane. `jarvis-settings/docs/` has one per pane and
  this one has none; it needs a running GTK session to shoot.
- The `[hud]` keys are wired as far as this repository goes — published to
  `hud.json` and captioned from the speech path. Whether the *overlay* honours
  `corner`, `scale`, `idle_visible` and `sprite` is Stream A's half.
