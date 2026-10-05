# Antigravity lab auto-login

Unattended sign-in and setup for the **BwG Track 2 · CE428** lab VM. Give it a
username, password and project ID; on boot it launches Antigravity and performs
every step of the "Lab Setup" module without human input.

## Quick start

```bash
# 1. credentials (or inject the same names as environment variables)
vi /config/automata/lab.env

# 2. run at every desktop startup
/config/automata/install.sh

# 3. run right now, without rebooting
/config/automata/bin/ag-start.sh
```

## Credentials

`bin/ag_autologin.py` reads, in order of precedence: command-line flags →
environment variables → `lab.env`.

| Name | Meaning |
|---|---|
| `AG_EMAIL` | lab Google account |
| `AG_PASSWORD` | its password |
| `AG_PROJECT_ID` | Google Cloud project ID for the wizard |
| `AG_SESSION_DIR` | session folder to open (default `~/Desktop/Session1`) |

`lab.env` is `chmod 600`. The password is never written to a log.

## What it automates

1. Launch Antigravity with a DevTools port.
2. Click **Use Google Cloud project instead**.
3. Capture the OAuth URL Antigravity hands to the desktop.
4. Open it in a Chrome instance we control, first-run dialogs suppressed.
5. Sign in: email → password → **Use Chrome without an account** → **Sign in**.
6. Enter the Google Cloud project ID.
7. Wizard: Terms of Service → Theme → **Google Antigravity SDK** → Finish.
8. Settings → Agent: **Security Preset = Turbo Mode**,
   **Artifact Review Policy = Always Proceed**.
9. Settings → App: **Keep In Menu Bar = off**.
10. Add the session folder as a project and click **Start first conversation**.
11. Close the sign-in browser and relaunch Antigravity without the debug port,
    leaving it focused in the foreground.

Typical run: about 90 seconds.

## How it works, and why

The UI is driven over the **Chrome DevTools Protocol** using DOM selectors and
element text, not screen coordinates. Three properties of this VM forced that
choice, all verified on the box:

- **XTEST mouse clicks are unreliable.** Under KWin/Wayland, XWayland only
  delivers a synthetic click when the compositor's own pointer already happens
  to sit over the target surface. Keyboard injection works; the pointer does not.
- **Coordinates are not portable.** The screen is 3456x2168 with
  `Xwayland Scale=2`, so the lab guide's 1540x860 screenshots share no
  coordinate system with the running app.
- **Antigravity is Electron**, so `--remote-debugging-port` exposes its whole
  UI to CDP. Chrome is driven the same way.

Two places need care:

- **`bin/shim/xdg-open`** is placed ahead of the real one on the child's `PATH`.
  Antigravity calls `xdg-open` to hand the OAuth URL to a browser; the shim
  records the URL so the automation can open it in a browser it controls,
  instead of the attendee's default Chrome. No system file is modified.
- **The folder picker is a native GTK dialog**, unreachable over CDP. It is
  driven with `xdotool` *keyboard* only: `ctrl+l`, paste, `Return`, `alt+o`.
  The path is **pasted from the clipboard, never typed** — `xdotool type` drops
  and duplicates characters here, which silently corrupted the path during
  testing (`/tmp/agtest2` became `/tmp/agtestst2`).

**The sign-in browser is closed as soon as the token is handed back**, so no
Chrome window (including Chrome's "Use Chrome without an account" managed-profile
notice, whose appearance is timing-dependent) is ever left on screen. Only the
dedicated `google-chrome-ag-auth` profile is terminated, never the attendee's
own Chrome. Antigravity is then raised to the foreground; focus is re-asserted
and verified, because immediately after launch the app is still creating windows
and focus otherwise drifts to a hidden 1x1 helper window.

The sign-in browser uses its own profile at `~/.config/google-chrome-ag-auth`.
That is required, not cosmetic: Chrome refuses remote debugging on the default
data directory. It also leaves the attendee's own Chrome untouched, which
matches the guide's "Use Chrome without an account" instruction.

The OAuth flow is authorization-code + PKCE with a `http://localhost:<port>/auth/callback`
redirect, so the hand-back is an ordinary localhost redirect. There is no
`antigravity://` protocol-confirmation dialog to dismiss.

## Re-running

A marker file `~/.antigravity-lab-setup-complete` makes startup idempotent: it
sits beside the profile it describes (not in this directory) so a run against a
different `HOME` cannot mask a real profile. Once setup has run,
later boots only launch the app.

```bash
# redo setup from scratch
/opt/venv/bin/python3 bin/ag_autologin.py --force

# useful flags
--restart      relaunch the app at the end to drop the debug port. Off by
               default: the relaunch frequently came back up with no window,
               at which point Antigravity quits. The configured instance is
               left running instead, and the port is localhost-only.
--no-kill      do not terminate other running Antigravity instances
```

## Logs and troubleshooting

- `logs/autologin-<timestamp>.log` — one file per run
- `logs/startup.log` — boot-time wrapper
- `logs/antigravity.log`, `logs/chrome.log` — child process output
- `logs/*.png` — screenshots captured automatically on failure

If sign-in stops at an unexpected screen (a verification prompt, a forced
password change), the run aborts with the screen name and saves a screenshot
rather than silently continuing.

## Uninstall

```bash
/config/automata/install.sh --uninstall
```
