# Jarvis — configuration schema (contract)

The app is **Jarvis**. The assistant's *name* is a setting (default `Luna`), so
nobody has to guess: change it in Settings and every prompt, greeting and log
label follows.

Single source of truth: `~/.config/jarvis/config.toml`
Written by the settings GUI, read by `lunad`. `lunad` watches it and hot-reloads.
Secrets NEVER live here — see §Secrets.

**Every key below is read by something.** That was not always true: for a while
the GUI wrote the whole file while the daemon used hard-coded constants for most
of it, so a setting could be changed, saved, redisplayed and have no effect
whatsoever. The §Wiring table at the end of this document is the record of what
reads what; the two keys that are still honoured by nothing, and the one
table that belongs to another process entirely, are named there explicitly
rather than left to be discovered.

Where a key has a matching constant in `lunad/config.py`, that constant is a
**fallback only** — what a daemon with no config file at all uses. The setting
wins whenever the file exists. `tests/test_contract.py::DriftCase` fails if a
default and its fallback ever disagree again.

```toml
[assistant]
name         = "Luna"        # display name + how she refers to herself
specialist   = "Sol"         # the delegate persona
agent        = "claude"      # claude | codex   (falls back to ~/.config/omarchy/defaults/agent)
model        = ""            # "" = agent default

[voice]
enabled      = true
provider     = "openrouter"  # openrouter | piper
model        = "deepgram/flux-tts:free"
voice        = "flux-sienna-en"     # DEFAULT (female)
voice_male   = "flux-donovan-en"    # the alternate, selectable in the GUI
fallback     = "piper"       # piper | none — used when the network/provider fails
piper_voice  = "en_GB-jenny_dioco-medium"
speed        = 1.0
max_spoken_chars = 400       # longer replies are summarised for speech, full text on screen

[listen]
enabled      = true
provider     = "openrouter"  # openrouter | local
model        = "fish-audio/transcribe-1"
language     = "en"
keybind      = "F10"

[confirm]
# The safety model. NOT hard blocks — Jarvis asks first, then proceeds.
# Each key: "never" (just do it) | "ask" (confirm first) | "deny" (refuse outright)
install_packages   = "ask"
delete_files       = "ask"
write_outside_home = "ask"
system_config      = "ask"   # /etc, systemd units, hyprland config
network_send       = "ask"   # posting data off the machine
git_push           = "ask"
long_job           = "ask"   # anything estimated over `long_job_seconds`
long_job_seconds   = 300
spend              = "ask"   # anything with a metered cost over `spend_threshold`
spend_threshold    = 0.25    # dollars
# These are ALWAYS "deny" and are not user-editable — they exist to protect
# other running sessions and the machine's own record of itself:
#   signalling a process Jarvis did not spawn
#   restarting omarchy-shell
#   deleting ~/.config/omarchy/CUSTOMISATIONS.md
#   rm -rf outside Jarvis's own directories

[confirm.prompt]
timeout_seconds = 60         # no answer within this = treated as "no"
default_on_timeout = "no"
channel = "notification"     # notification | terminal | both

[memory]
luna_cap_chars = 3000
user_cap_chars = 2000
consolidate_every_turns = 12  # 0 = never; the pass costs tokens
decay_half_life_days = 30

[dispatch]
workspace   = "luna"         # hyprland special workspace name
app_id      = "org.omarchy.luna"
max_parallel = 1
job_retention_days = 14

[ui]
theme_follows_omarchy = true
notify_on_finish = true
```

## Wiring — what reads each key, and when it takes effect

**Live** means the next request already sees the change; the settings watcher
stats the file every two seconds and the value is read at the point of use.
Nothing in this table needs a `lunad` restart.

### `[assistant]`

| key | read by | effect |
|---|---|---|
| `name` | `persona`, `server`, `confirm`, `dispatch`, `audit` | Live. Every prompt, toast headline and log label. Changing it **retires the warm sessions**, because her name is part of the cacheable prompt prefix. |
| `specialist` | `persona`, `dispatch.announce` | Live. Sol's preamble, his persona heading and the enrolment line. |
| `agent` | `server` | Live. Swaps the adapter (`claude` ↔ `codex`). A `--agent` flag on the daemon still wins. |
| `model` | `server` | Live, per ask. `""` means the agent's own default. |

### `[voice]`

| key | read by | effect |
|---|---|---|
| `enabled` | `speech.say` | Live. `false` returns a note instead of speaking; text still reaches the screen. |
| `provider` | `speech` | Live, per utterance. |
| `model` | `speech` (OpenRouter) | Live, per sentence request. |
| `voice` | `speech` (OpenRouter) | Live, per sentence request. |
| `voice_male` | **the GUI only** | The alternate offered in the Voice pane, with its own preview button. It is a stored *choice*, not a second live voice: picking it copies it into `voice`, which is what the daemon reads. Nothing is broken here — but do not expect changing `voice_male` alone to change how she sounds. |
| `fallback` | `speech._play` | Live. `piper` finishes an utterance the provider dropped, mid-sentence-run; `none` raises instead. |
| `piper_voice` | `speech` | Live. Names the ONNX under `~/.local/share/luna/voices/`. A worker already holding a different model is **unloaded and reloaded** — one worker is one model and cannot be re-pointed. |
| `speed` | `speech`, `piper_worker` | Live. piper gets `length_scale = 1/speed` on the synthesis request; OpenRouter gets `speed` in the request body, but **only when it is not 1.0** — not every model behind OpenRouter implements the field, and a 400 on it for the default value would break speech for people who never touched the setting. Off the default, a rejection falls back to piper, which honours the speed anyway. |
| `max_spoken_chars` | `speech.say` | Live. Caps the spoken form at a sentence boundary; the full text still goes to screen. |

### `[listen]`

**Not read by `lunad`, and cannot be.** Listening is voxtype's, in a separate
process with its own config file, wired to Luna through the
`bin/luna-voice-router` `post_process` hook and the `[profiles.luna]` block in
`~/.config/voxtype/config.toml`. These keys are a *mirror* of that
configuration for display, and editing them here changes nothing until the same
change is made in voxtype's own file. See §Not wired.

### `[confirm]`, `[confirm.prompt]`

Read by `confirm.ConfirmBroker` on every gate, live. Eight policy classes plus
the prompt's timeout, default and channel. The four hard denies are not in the
file and the file cannot re-enable them.

### `[memory]`

| key | read by | effect |
|---|---|---|
| `luna_cap_chars` | `memory.Tier1File.cap` | Live, per write **and per read**. Lowering it below the current contents does not truncate anything — the next write is rejected with what must be consolidated, which is the tier-1 contract. |
| `user_cap_chars` | `memory.Tier1File.cap` | Live, same. |
| `consolidate_every_turns` | `consolidate.Consolidator` | Live. Counts completed asks; on the Nth, a background pass reads the tier-2 episodes recorded since the last one, rebuilds the tier-3 profile, and proposes tier-1 edits through the model. **`0` means never** — nothing is counted, no pass starts, no tokens are spent. The pass is subject to the ordinary cap contract: a proposal that would overflow a file is rejected whole and recorded, and the file is left exactly as it was. Never blocks a reply. |
| `decay_half_life_days` | `memory.decayed_salience` | Live. Decay is applied at read time, so a change reaches the very next recall. Corrections score 1.0 and never decay regardless. |

`SOL.md` has a cap of its own (`config.SOL_MD_CAP`) with no key: Sol's
namespace is deliberately outside the user-facing contract.

### `[dispatch]`

| key | read by | effect |
|---|---|---|
| `workspace` | `dispatch.Hyprland` | Live, for the **next** job. The window rule is re-installed when either half of the pair changes — the Lua guard that stops rules stacking is keyed on a digest of `(app_id, workspace)` precisely so a change is not suppressed by the old rule's guard. |
| `app_id` | `dispatch.Hyprland`, `dispatch.Dispatcher` | Live, for the **next** job. Windows already open keep the app-id they were born with: an app-id is set at map time and cannot be changed afterwards, so a running job stays where it is. |
| `max_parallel` | **nothing** | See §Not wired. |
| `job_retention_days` | **nothing** | See §Not wired. |

### `[ui]`

| key | read by | effect |
|---|---|---|
| `theme_follows_omarchy` | **the Jarvis GUI**, `jarvis/theme.py` | Live. On, the palette is re-read from the current Omarchy theme's `colors.toml` and follows `omarchy theme set`. Off, the built-in monochrome palette is pinned and `colors.toml` is never opened. The geometry tokens — rounding, border, spacing, font sizes — are *not* part of the switch: they mirror Hyprland's own values, and a window that stops matching the rounding of every other window is not a theme choice. `lunad` draws nothing and has no opinion here. |
| `notify_on_finish` | `dispatch.Dispatcher.notify_finished` | Live. A dispatched job's window is hidden by design, so without this the only way to learn it finished is to go and look. A failed job's toast is `critical` and carries the exit code. A missing `omarchy-notification-send` is logged and swallowed — a desktop that cannot show a toast is not a job that did not finish. |

### What the consolidation pass costs, since it spends real money

`consolidate_every_turns` is the only key in this file that causes a model call
nobody asked for, so its cost is stated here rather than left to be discovered
on a bill.

One pass is **one call**, bounded above by roughly **3k input tokens and a few
hundred out**. The bound is structural and is not a setting: the pass runs
under a librarian's system prompt of a few hundred characters rather than
Luna's ~8k-token persona, reads at most 24 exchanges clipped to 240 characters
a side, and takes the tier-3 profile as a digest. At the default of 12 turns
that is one extra call per twelve asks.

Five things stop it running away, and none of them is the turn count: `0` means
never; only one pass runs at a time; there is a five-minute floor between
passes whatever the count says; **no new episodes since the last pass means no
call at all**, so an idle daemon spends nothing even at `= 1`; and the pass
records no episode of its own, so it cannot feed itself. What it spends shows
up in `luna status`, in the `consolidated` line in the log next to the ordinary
`reply` line, and as one entry per pass in `luna audit`.

## Not wired — and why

These two are in the file, validated, round-tripped and displayed, and are
honoured by **nothing**. They are listed here rather than quietly wired to a
stub, because a setting that appears to work and does not is worse than one
that is documented as pending.

- **`[dispatch] max_parallel`** — dispatch never fans out. Every `luna dispatch`
  spawns immediately and there is no queue to bound. Wiring this means a real
  admission gate: a semaphore around the spawn, a pending queue with its own
  state in `jobs/`, and an answer for what `luna jobs` shows for a job that has
  been accepted but not started.
- **`[dispatch] job_retention_days`** — nothing under
  `~/.local/share/luna/jobs/` is ever collected. Wiring this means a GC pass
  with a deletion policy: which directories are safe to remove (finished, past
  the retention window, not the last N), when it runs, and an audit entry per
  deletion, since these directories are the only record of what a dispatched
  agent did.

And `[listen]` — five keys — is voxtype's configuration, read by a separate
process from a separate file. `lunad` can display it and cannot honour it.
Changing `[listen] keybind` here does not move the keybind; changing it in
`~/.config/voxtype/config.toml` does.

## Known cosmetic gap

`data/persona.md` and `data/sol-persona.md` are shipped prose and say "Luna"
and "Sol" literally. The *harness* around them is fully templated — the
preamble, the headings and every prompt read `[assistant] name` — so a rename
takes effect everywhere except inside the spec text itself. Renaming her to
`Jarvis` gives a system prompt headed "Jarvis — persona specification" whose
body still refers to Luna in a few sentences.

## Secrets

Never in this file. `~/.config/jarvis/secrets.env`, 0600, in the same 0700
directory, read by systemd through
`lunad.service.d/10-jarvis-secrets.conf`. `lunad` also reads
`~/.config/voxtype/secrets.env` as a fallback so the OpenRouter key already on
this machine keeps working, and never writes to it.
