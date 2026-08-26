# Jarvis — configuration schema (contract)

The app is **Jarvis**. The assistant's *name* is a setting (default `Luna`), so
nobody has to guess: change it in Settings and every prompt, greeting and log
label follows.

Single source of truth: `~/.config/jarvis/config.toml`
Written by the settings GUI, read by `lunad`. `lunad` watches it and hot-reloads.
Secrets NEVER live here — see §Secrets.

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
keybind      = "SUPER + ALT + L"

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
consolidate_every_turns = 12
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
