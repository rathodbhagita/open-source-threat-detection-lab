#!/usr/bin/env python3
"""Unattended Antigravity sign-in + setup for the BwG Track 2 lab VM.

Performs, with no human input, every step of the "Lab Setup" module:

  1. launch Antigravity (Electron) with a DevTools port
  2. choose "Use Google Cloud project instead"
  3. capture the OAuth URL Antigravity hands to the desktop
  4. open it in a Chrome we control, with first-run dialogs suppressed
  5. sign in (email -> password -> "Use Chrome without an account" -> Sign in)
  6. enter the Google Cloud project ID
  7. walk the wizard (ToS -> Theme -> Build with Google/Antigravity SDK -> Finish)
  8. apply the required settings (Turbo Mode, Always Proceed, Keep In Menu Bar off)
  9. open the session folder as a project
 10. relaunch Antigravity cleanly, without the debug port

The UI is driven through the DevTools Protocol (DOM selectors), not screen
coordinates: the lab VM runs at 3456x2168 with Xwayland Scale=2, and XTEST
mouse clicks are unreliable under KWin/Wayland. The one native GTK dialog
that CDP cannot reach is driven with xdotool *keyboard* only, which is
reliable here.

Safe to run on every boot: if setup is already complete it just starts the app.
"""
import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIM_DIR = os.path.join(HERE, "shim")
LOG_DIR = os.path.join(ROOT, "logs")
ENV_FILE = os.path.join(ROOT, "lab.env")
URL_CAPTURE = "/tmp/ag_auth_url.txt"

ANTIGRAVITY = "/usr/local/bin/antigravity"
CHROME = "/usr/bin/google-chrome-stable.orig"
# distinctive enough that killing it can never touch the attendee's own Chrome
CHROME_PROFILE_TAG = "google-chrome-ag-auth"
AG_PORT = 9500
CHROME_PORT = 9501

log = logging.getLogger("autologin")


# ---------------------------------------------------------------- utilities

def state_file():
    """Marker recording that setup finished.

    Kept beside the profile it describes, not in the code directory, so a run
    against a different HOME cannot mask a real profile's state.
    """
    return os.path.expanduser("~/.antigravity-lab-setup-complete")


def setup_logging(verbose=True):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, time.strftime("autologin-%Y%m%d-%H%M%S.log"))
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(path)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.DEBUG)
    log.info("log file: %s", path)
    return path


def load_config(args):
    """Environment wins; lab.env is the fallback."""
    cfg = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("AG_EMAIL", "AG_PASSWORD", "AG_PROJECT_ID", "AG_SESSION_DIR", "AG_SCALE_FACTOR"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if args.email:
        cfg["AG_EMAIL"] = args.email
    if args.password:
        cfg["AG_PASSWORD"] = args.password
    if args.project_id:
        cfg["AG_PROJECT_ID"] = args.project_id
    if not cfg.get("AG_SESSION_DIR"):
        for candidate in [os.path.expanduser("~/Desktop/Session1")]:
            if os.path.isdir(candidate):
                cfg["AG_SESSION_DIR"] = candidate
                break
        cfg.setdefault("AG_SESSION_DIR", os.path.expanduser("~/Desktop/Session1"))

    missing = [k for k in ("AG_EMAIL", "AG_PASSWORD", "AG_PROJECT_ID") if not cfg.get(k)]
    if missing:
        raise SystemExit(
            f"missing credentials: {', '.join(missing)}\n"
            f"set them in {ENV_FILE} or as environment variables.")
    return cfg


def sh(cmd, **kw):
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.call(cmd, **kw)


def xdo(*args):
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    return subprocess.run(["xdotool", *args], env=env,
                          capture_output=True, text=True).stdout.strip()


def set_clipboard(text):
    """Put text on the clipboard.

    Paths are pasted rather than typed: `xdotool type` drops and duplicates
    characters under this compositor, which silently corrupts the path.
    """
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(),
                   env=env, check=False)


def port_free(port):
    """True if nothing is listening and we can actually bind the port."""
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def pick_port(start, span=60):
    """First bindable port at or after `start`.

    Ports are chosen at run time because a just-killed instance can still be
    holding the previous one when we relaunch.
    """
    for p in range(start, start + span):
        if port_free(p):
            return p
    raise RuntimeError(f"no free port in {start}..{start + span}")


def wait_gone(fragment, timeout=15):
    """Wait for processes matching `fragment` to actually exit."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any_matching(fragment):
            return True
        time.sleep(0.5)
    return False


def any_matching(fragment):
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cl = f.read().replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if fragment in cl and "ag_autologin" not in cl:
            return True
    return False


def kill_matching(fragment, hard=False):
    """Kill processes whose cmdline contains `fragment` (never ourselves)."""
    me = os.getpid()
    killed = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cl = f.read().replace(b"\0", b" ").decode("utf8", "replace")
        except OSError:
            continue
        if fragment in cl and "ag_autologin" not in cl:
            try:
                os.kill(int(pid), signal.SIGKILL if hard else signal.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


# the binary is reached both directly and through the /usr/local/bin symlink
AG_PROC_HINTS = ("Antigravity-x64/antigravity", "bin/antigravity")


def kill_antigravity():
    """Stop Antigravity, asking politely first.

    SIGKILL is a last resort: the language server flushes auth state on
    shutdown, and killing it outright can leave that state half-written.
    """
    n = 0
    for frag in AG_PROC_HINTS:
        n += kill_matching(frag, hard=False)
    if n:
        log.info("stopping %d Antigravity process(es)", n)
    alive = [f for f in AG_PROC_HINTS if not wait_gone(f, timeout=20)]
    if alive:
        log.warning("  did not exit gracefully; forcing")
        for frag in alive:
            kill_matching(frag, hard=True)
            wait_gone(frag, timeout=10)
    return n


def screenshot(name, window_name="^Antigravity$"):
    """Best-effort diagnostic capture of a window (Antigravity or Chrome)."""
    try:
        wid = xdo("search", "--name", window_name).split("\n")[0]
        if not wid:
            # Fall back to any chrome/antigravity window
            wid = xdo("search", "--name", "Antigravity|Chrome").split("\n")[0]
            if not wid:
                return None
        out = os.path.join(LOG_DIR, f"{name}.png")
        raw = subprocess.run(["xwd", "-display", os.environ.get("DISPLAY", ":1"),
                              "-id", wid, "-silent"], capture_output=True).stdout
        import struct
        from PIL import Image
        h = struct.unpack(">25I", raw[:100])
        hdr, w, ht, bpl, ncol = h[0], h[4], h[5], h[12], h[19]
        off = hdr + ncol * 12
        im = Image.frombytes("RGBA", (bpl // 4, ht), raw[off:off + bpl * ht],
                             "raw", "BGRA").convert("RGB").crop((0, 0, w, ht))
        im.thumbnail((1400, 1400))
        im.save(out)
        log.info("screenshot: %s", out)
        return out
    except Exception as e:  # diagnostics must never break the run
        log.debug("screenshot failed: %s", e)
        return None


# ------------------------------------------------------------ app launching

def launch_antigravity(debug_port=None, extra_args=(), cfg=None):
    env = dict(os.environ)
    env["DISPLAY"] = env.get("DISPLAY", ":1")
    env["PATH"] = SHIM_DIR + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["AG_URL_CAPTURE"] = URL_CAPTURE
    scale = (cfg and cfg.get("AG_SCALE_FACTOR")) or env.get("AG_SCALE_FACTOR", "1.35")
    cmd = [
        ANTIGRAVITY,
        "--no-sandbox",
        "--disable-gpu",
        "--ozone-platform=x11",
        "--start-maximized",
    ]
    if scale:
        cmd.append(f"--force-device-scale-factor={scale}")
    if debug_port:
        cmd += [f"--remote-debugging-port={debug_port}"]
    cmd += list(extra_args)
    log.info("launching Antigravity (scale=%s)%s",
             scale, f" (devtools :{debug_port})" if debug_port else "")
    subprocess.Popen(cmd, env=env, stdout=open(os.path.join(LOG_DIR, "antigravity.log"), "ab"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)


def launch_chrome(url, port, profile=None):
    """Chrome with every first-run/sign-in dialog suppressed.

    A dedicated profile is required: Chrome refuses remote debugging on the
    default data directory ("DevTools remote debugging requires a non-default
    data directory"). It also keeps the attendee's own Chrome untouched.
    """
    close_auth_chrome()
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    profile = profile or os.path.expanduser(f"~/.config/{CHROME_PROFILE_TAG}")
    os.makedirs(profile, exist_ok=True)
    cmd = [
        CHROME, "--no-sandbox", "--disable-gpu", "--ozone-platform=x11",
        f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--disable-features=Translate,ChromeWhatsNewUI",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--password-store=basic", "--no-service-autorun",
        "--window-size=1400,1000", "--window-position=60,60", url,
    ]
    log.info("launching Chrome for sign-in (devtools :%d)", port)
    subprocess.Popen(cmd, env=env, stdout=open(os.path.join(LOG_DIR, "chrome.log"), "ab"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)


def close_auth_chrome():
    """Shut down the sign-in browser once the token has been handed back.

    Matches only our dedicated profile, so the attendee's own Chrome is never
    touched. Asks politely over CDP first, then terminates.
    """
    if not any_matching(CHROME_PROFILE_TAG):
        return
    log.info("closing the sign-in browser")
    try:
        for t in cdp.list_targets(CHROME_PORT):
            if t.get("type") == "page":
                try:
                    cdp.http_json(CHROME_PORT, f"/close/{t['id']}", timeout=3)
                except Exception:
                    pass
        time.sleep(2)
    except Exception:
        pass
    kill_matching(CHROME_PROFILE_TAG)
    if not wait_gone(CHROME_PROFILE_TAG, timeout=8):
        kill_matching(CHROME_PROFILE_TAG, hard=True)
        wait_gone(CHROME_PROFILE_TAG, timeout=5)
    log.info("  sign-in browser closed")


def main_antigravity_window():
    """Largest top-level window titled "Antigravity", or "" if none yet.

    The app also creates 1x1/16x16 helper windows, so size is the reliable
    discriminator rather than window order.
    """
    best, best_area = "", 0
    for cand in xdo("search", "--name", "^Antigravity$").split("\n"):
        cand = cand.strip()
        if not cand:
            continue
        geo = xdo("getwindowgeometry", "--shell", cand)
        w = re.search(r"WIDTH=(\d+)", geo)
        h = re.search(r"HEIGHT=(\d+)", geo)
        if not (w and h):
            continue
        area = int(w.group(1)) * int(h.group(1))
        if int(w.group(1)) > 400 and area > best_area:
            best, best_area = cand, area
    return best


def focus_antigravity(timeout=45):
    """Raise and maximize/resize the Antigravity window so the attendee lands on it.

    Focus is re-asserted and verified: right after launch the app is still
    creating windows, and focus otherwise drifts to a hidden helper window.
    """
    deadline = time.time() + timeout
    wid = ""
    while time.time() < deadline:
        wid = main_antigravity_window()
        if wid:
            break
        time.sleep(1)
    if not wid:
        log.info("  no Antigravity window to focus yet")
        return False

    def resize_to_screen(target_wid):
        try:
            geo = xdo("getdisplaygeometry").split()
            if len(geo) >= 2:
                dw, dh = geo[0], geo[1]
                xdo("windowsize", target_wid, dw, dh)
                xdo("windowmove", target_wid, "0", "0")
        except Exception as e:
            log.debug("window resize failed: %s", e)

    resize_to_screen(wid)

    for attempt in range(6):
        xdo("windowactivate", "--sync", wid)
        xdo("windowraise", wid)
        time.sleep(1.5)
        if xdo("getactivewindow") == wid:
            log.info("  Antigravity is in the foreground (window %s)", wid)
            return True
        # a newer window may have replaced it during startup
        newer = main_antigravity_window()
        if newer and newer != wid:
            wid = newer
            resize_to_screen(wid)
        time.sleep(1.5)
    log.warning("  could not keep Antigravity focused (window %s)", wid)
    return False


# ------------------------------------------------------- Antigravity helpers

def ag_page(timeout=120):
    """Attach to the Antigravity workbench/onboarding page."""
    cdp.wait_for_cdp(AG_PORT, timeout=timeout)
    t = cdp.find_target(AG_PORT, lambda x: "127.0.0.1" in x.get("url", ""),
                        timeout=timeout)
    if not t:
        raise RuntimeError("Antigravity UI target not found over CDP")
    return cdp.Page.attach(t)


def ag_state(page):
    """Classify the current screen from its content, not its URL.

    The app briefly serves "/" before routing to "/onboarding", and it reloads
    its windows once the language-server port settles, so the URL alone is not
    a reliable signal.
    """
    try:
        body = (page.text() or "").lower()
    except cdp.CDPError:
        return "unknown"
    if not body:
        return "unknown"
    if ("use google cloud project instead" in body
            or "use business account" in body
            or "continue with google cloud" in body
            or "welcome to antigravity" in body
            or "enter google cloud project details" in body
            or "awaiting authentication" in body
            or "sign in with google" in body
            or "continue with different account" in body
            or "unexpected issue" in body
            or "context deadline exceeded" in body):
        return "onboarding"
    if ("terms of service" in body
            or "select antigravity theme" in body
            or "select your license" in body
            or "provide google cloud project" in body
            or "build with google" in body):
        return "wizard"
    if "new conversation" in body:
        return "workbench"
    return "unknown"


def ag_wait_ready(timeout=240):
    """Wait until the UI has settled; return (page, state).

    Re-attaches on each poll because the app reloads its windows during
    startup, which tears down the DevTools websocket.
    """
    deadline = time.time() + timeout
    last = "unknown"
    while time.time() < deadline:
        page = None
        try:
            page = ag_page(timeout=30)
            state = ag_state(page)
            if state != "unknown":
                log.info("UI ready: %s", state)
                return page, state
            last = state
        except (cdp.CDPError, RuntimeError) as e:
            last = f"error: {e}"
        if page:
            page.close()
        time.sleep(2)
    raise RuntimeError(f"Antigravity UI never became ready (last={last})")


JS_CLICK_EXACT = """
(() => {
  const want = %s.toLowerCase();
  const els = [...document.querySelectorAll('button,[role=button],[role=menuitem],a')];
  const el = els.find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 &&
           (e.innerText || '').trim().toLowerCase() === want;
  });
  if (!el) return false;
  el.scrollIntoView({block: 'center'});
  el.click();
  return true;
})()
"""

JS_OPEN_COMBO = """
(() => {
  const label = %s.toLowerCase();
  const dlg = document.querySelector('[role=dialog]') || document;
  const labs = [...dlg.querySelectorAll('*')].filter(
      e => e.childElementCount === 0 && (e.innerText || '').trim().toLowerCase() === label);
  for (const lab of labs) {
    let n = lab;
    for (let d = 0; d < 6 && n; d++) {
      n = n.parentElement;
      if (!n) break;
      const cb = n.querySelector('[role=combobox]');
      if (cb) { cb.scrollIntoView({block:'center'}); cb.click(); return true; }
    }
  }
  return false;
})()
"""

# options carry a description on a second line, so match the first line only
JS_PICK_OPTION = """
(() => {
  const want = %s.toLowerCase();
  const opts = [...document.querySelectorAll(
      '[role=option],[role=menuitem],li,div[data-radix-collection-item]')];
  const el = opts.find(e => {
    const r = e.getBoundingClientRect();
    const first = ((e.innerText || '').trim().split('\\n')[0] || '').trim().toLowerCase();
    return r.width > 0 && r.height > 0 && first === want;
  });
  if (!el) return false;
  el.click();
  return true;
})()
"""

JS_SWITCH_OFF = """
(() => {
  const label = %s.toLowerCase();
  const dlg = document.querySelector('[role=dialog]') || document;
  const labs = [...dlg.querySelectorAll('*')].filter(
      e => e.childElementCount === 0 && (e.innerText || '').trim().toLowerCase() === label);
  for (const lab of labs) {
    let n = lab;
    for (let d = 0; d < 6 && n; d++) {
      n = n.parentElement;
      if (!n) break;
      const sw = n.querySelector('[role=switch]');
      if (sw) {
        const on = sw.getAttribute('aria-checked') === 'true';
        if (on) { sw.scrollIntoView({block:'center'}); sw.click(); }
        return on ? 'turned-off' : 'already-off';
      }
    }
  }
  return 'not-found';
})()
"""


def click_exact(page, text, tries=3, delay=1.5):
    for i in range(tries):
        if page.evaluate(JS_CLICK_EXACT % json.dumps(text)):
            log.info("  clicked %r", text)
            return True
        time.sleep(delay)
    return False


def set_combo(page, label, value):
    if not page.evaluate(JS_OPEN_COMBO % json.dumps(label)):
        log.warning("  combobox %r not found", label)
        return False
    time.sleep(1.5)
    ok = page.evaluate(JS_PICK_OPTION % json.dumps(value))
    time.sleep(1.5)
    log.info("  %s -> %s%s", label, value, "" if ok else "  [OPTION NOT FOUND]")
    return bool(ok)


# ------------------------------------------------------------- Google log-in

def dismiss_profile_notice(target):
    """Click "Use Chrome without an account" on Chrome's managed-profile notice.

    It is a WebUI page whose buttons live inside nested shadow roots, so it is
    reached by id first and by button text as a fallback (the id has changed
    between Chrome versions).
    """
    try:
        q = cdp.Page.attach(target)
    except (cdp.CDPError, KeyError, TypeError):
        return "no-button"
    try:
        return q.evaluate("""
        (() => {
          const all = (root, out, d) => {
            if (d > 8 || !root) return out;
            for (const e of root.querySelectorAll('*')) {
              out.push(e);
              if (e.shadowRoot) all(e.shadowRoot, out, d + 1);
            }
            return out;
          };
          const els = all(document, [], 0);
          let b = els.find(e => e.id === 'cancel-button');
          if (!b) b = els.find(e => /^(CR-BUTTON|BUTTON)$/.test(e.tagName) &&
                     /use chrome without an account|continue without an account/i
                       .test((e.innerText || e.textContent || '')));
          if (!b) return 'no-button';
          b.click();
          return 'clicked';
        })()""")
    except cdp.CDPError:
        return "no-button"
    finally:
        q.close()


def google_signin(email, password, timeout=180):
    """Drive the Google OAuth pages in the Chrome we launched."""
    cdp.wait_for_cdp(CHROME_PORT, timeout=90)
    t = cdp.find_target(CHROME_PORT,
                        lambda x: "accounts.google.com" in x.get("url", ""),
                        timeout=90)
    if not t:
        raise RuntimeError("Google sign-in page never appeared")
    p = cdp.Page.attach(t)

    log.info("handling Google sign-in / account chooser")
    deadline = time.time() + timeout

    def submit_email(page_obj):
        page_obj.press("Enter")
        time.sleep(0.3)
        res = page_obj.evaluate("""
        (() => {
          const emailInput = document.querySelector('input[type="email"], input[name="identifier"], input#identifierId');
          if (emailInput) {
            emailInput.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
            emailInput.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
            emailInput.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
          }
          for (const s of ['#identifierNext button', '#identifierNext', 'button[type=submit]', 'input[type=submit]']) {
            const el = document.querySelector(s);
            if (el) {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) { el.click(); return 'clicked ' + s; }
            }
          }
          const els = [...document.querySelectorAll('button, [role=button], input[type=submit], div.VfPpkd-LgbsSe')];
          for (const want of ['next', 'continue', 'submit']) {
            const el = els.find(e => {
              const r = e.getBoundingClientRect();
              const txt = (e.innerText || e.textContent || e.value || '').trim().toLowerCase();
              return r.width > 0 && r.height > 0 && (txt === want || txt.startsWith(want));
            });
            if (el) {
              const clickable = el.closest('button, [role=button], div.VfPpkd-LgbsSe') || el;
              clickable.click();
              return 'clicked ' + want;
            }
          }
          return 'dispatched enter';
        })()""")
        log.info("  email submit: %s", res)

    def submit_password(page_obj):
        page_obj.press("Enter")
        time.sleep(0.3)
        res = page_obj.evaluate("""
        (() => {
          const pwd = document.querySelector('input[type="password"]');
          if (pwd) {
            pwd.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
            pwd.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
            pwd.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
          }
          for (const s of ['#passwordNext button', '#passwordNext', 'button[type=submit]', 'input[type=submit]', '#submit', '[data-idom-class*="Sign in"]']) {
            const el = document.querySelector(s);
            if (el) {
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) { el.click(); return 'clicked ' + s; }
            }
          }
          const els = [...document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], div.VfPpkd-LgbsSe, span')];
          for (const want of ['next', 'sign in', 'signin', 'log in', 'login', 'continue', 'submit']) {
            const el = els.find(e => {
              const r = e.getBoundingClientRect();
              const txt = (e.innerText || e.textContent || e.value || '').trim().toLowerCase();
              return r.width > 0 && r.height > 0 && (txt === want || txt.startsWith(want));
            });
            if (el) {
              const clickable = el.closest('button, [role=button], div.VfPpkd-LgbsSe') || el;
              clickable.click();
              return 'clicked ' + want;
            }
          }
          return 'dispatched enter';
        })()""")
        log.info("  password submit: %s", res)

    # Phase 1: Email input or Account selection
    entered_email = False
    while time.time() < deadline:
        if p.query_visible("#identifierId") or p.query_visible("input[type=email]"):
            log.info("entering email")
            sel = "#identifierId" if p.query_visible("#identifierId") else "input[type=email]"
            p.type_text(sel, email)
            time.sleep(0.5)
            submit_email(p)
            entered_email = True
            break
        elif email.lower() in (p.text() or "").lower() or "choose an account" in (p.text() or "").lower():
            log.info("selecting existing account from account chooser")
            clicked_acct = p.evaluate("""
            (() => {
              const want = %s.toLowerCase();
              const target = [...document.querySelectorAll('*')].find(e =>
                (e.getAttribute('data-identifier')||'').toLowerCase() === want ||
                (e.getAttribute('data-email')||'').toLowerCase() === want ||
                ((e.innerText||'').trim().toLowerCase() === want && e.childElementCount === 0)
              );
              if (!target) return false;
              const clickable = target.closest('[role=link], li, button, [role=button]') || target;
              clickable.click();
              return true;
            })()""" % json.dumps(email))
            if clicked_acct:
                entered_email = True
                break
        elif p.query_visible("input[type=password]"):
            entered_email = True
            break
        time.sleep(1.5)

    # Phase 2: Password prompt (if requested by Google)
    pwd_deadline = time.time() + 15
    pwd_visible = False
    while time.time() < pwd_deadline:
        if p.query_visible("input[type=password]"):
            pwd_visible = True
            break
        time.sleep(0.5)

    if pwd_visible:
        log.info("entering password")
        p.type_text("input[type=password]", password)
        time.sleep(0.5)
        submit_password(p)
        # Wait briefly for password submission to register
        for _ in range(8):
            time.sleep(0.5)
            if not p.query_visible("input[type=password]"):
                break

    try:
        p.close()
    except Exception:
        pass

    # Chrome may interject its managed-profile notice, and Google shows a
    # final consent screen. Handle whichever appears, in any order.
    log.info("clearing consent / profile dialogs")
    deadline = time.time() + timeout
    done = False
    while time.time() < deadline and not done:
        time.sleep(2)
        for tgt in cdp.list_targets(CHROME_PORT):
            url = tgt.get("url", "")
            is_google_host = "accounts.google.com" in url

            # 1. Successful redirect away from Google accounts
            if not is_google_host and any(s in url for s in ("auth-success", "oauth-callback", "oauth-success", "/auth/callback", "antigravity.google", "localhost:", "127.0.0.1:")):
                log.info("  OAuth callback URL reached: %s", url)
                if "antigravity.google" in url and "oauth-callback" in url:
                    try:
                        q = cdp.Page.attach(tgt)
                        q.evaluate("(() => { const b = document.querySelector('[data-copy-btn]'); if (b) b.click(); })()")
                        q.close()
                    except Exception:
                        pass
                time.sleep(3)  # Let local language server HTTP callback handler finish
                done = True
                break

            # 2. Chrome profile notice WebUI
            if "managed-user-profile-notice" in url:
                r = dismiss_profile_notice(tgt)
                if r != "no-button":
                    log.info("  profile notice: %s", r)

            # 3. Google accounts login / consent page
            elif is_google_host:
                try:
                    q = cdp.Page.attach(tgt)

                    # Secondary password prompt fallback
                    if q.query_visible("input[type=password]"):
                        log.info("  re-entering password on consent step")
                        q.type_text("input[type=password]", password)
                        time.sleep(0.5)
                        submit_password(q)
                        time.sleep(1)

                    clicked_btn = q.evaluate("""
                    (() => {
                      // 1. Check all unchecked permission/scope checkboxes
                      let checkedCount = 0;
                      for (const cb of document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
                        const isChecked = cb.checked || cb.getAttribute('aria-checked') === 'true';
                        if (!isChecked) {
                          cb.click();
                          checkedCount++;
                        }
                      }

                      // 2. Click consent / interstitial action button ("I understand", "Allow", "Continue", etc.)
                      const labels = [
                        "i understand", "understand", "continue", "allow", "accept", "i accept",
                        "agree", "i agree", "confirm", "done", "next", "sign in", "signin", "not now", "skip"
                      ];
                      const els = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button], cr-button, md-filled-button, div.VfPpkd-LgbsSe, span.VfPpkd-vQzf8d')];
                      for (const want of labels) {
                        const el = els.find(e => {
                          const r = e.getBoundingClientRect();
                          const txt = (e.innerText || e.textContent || e.value || '').trim().toLowerCase();
                          const isVisible = r.width > 0 && r.height > 0;
                          const isDisabled = e.disabled || e.getAttribute('aria-disabled') === 'true';
                          return isVisible && !isDisabled && (txt === want || txt.includes(want));
                        });
                        if (el) {
                          const clickable = el.closest('button, [role=button], a, div.VfPpkd-LgbsSe') || el;
                          clickable.click();
                          return (checkedCount ? `checked ${checkedCount} boxes & ` : '') + `clicked ${want}`;
                        }
                      }
                      if (checkedCount > 0) return `checked ${checkedCount} boxes`;
                      return null;
                    })()""")
                    if clicked_btn:
                        log.info("  consent: %s", clicked_btn)
                    q.close()
                except Exception as e:
                    log.debug("consent poll error: %s", e)
    if not done:
        screenshot("consent-timeout", window_name="Chrome|Antigravity")
        log.warning("did not observe the auth-success page; "
                    "continuing - Antigravity may still have the token")
    else:
        log.info("authentication succeeded")


# -------------------------------------------------------------- setup phases

def do_onboarding(cfg):
    page, state = ag_wait_ready()
    log.info("Antigravity UI at %s (%s)",
             page.evaluate("location.href") or "?", state)

    if state in ("workbench",):
        log.info("no onboarding screen - already signed in")
        page.close()
        return False
    if state == "wizard":
        log.info("resuming a part-finished wizard")
        return finish_wizard(page, cfg)

    # 0. If previously stuck on an error/failure screen, reset back to sign-in
    page.evaluate("""
        (() => {
          const els = [...document.querySelectorAll('button, a, [role=button]')];
          let el = els.find(e => (e.innerText||'').trim().toLowerCase().includes('continue with different account'));
          if (el) { el.click(); return 'continue-different'; }
          el = els.find(e => (e.innerText||'').trim().toLowerCase().includes('try again'));
          if (el) { el.click(); return 'try-again'; }
          return null;
        })()""")
    time.sleep(1)

    # 1. choose the Google Cloud project flow
    if os.path.exists(URL_CAPTURE):
        os.remove(URL_CAPTURE)
    log.info("selecting Google Cloud / Business account flow")
    clicked = False
    for _ in range(6):
        try:
            clicked = bool(page.evaluate("""
                (() => {
                  const els = [...document.querySelectorAll('button,a,[role=button]')];
                  let el = els.find(e => (e.innerText||'').trim().toLowerCase()
                                           .includes('use business account'));
                  if (el) { el.click(); return true; }
                  el = els.find(e => (e.innerText||'').trim().toLowerCase()
                                           .includes('use google cloud project instead'));
                  if (el) { el.click(); return true; }
                  el = els.find(e => (e.innerText||'').trim().toLowerCase()
                                           .includes('continue with google cloud'));
                  if (el) { el.click(); return true; }
                  return false;
                })()"""))
            if clicked:
                time.sleep(1.5)
                page.evaluate("""
                    (() => {
                      const els = [...document.querySelectorAll('button,a,[role=button]')];
                      const el = els.find(e => (e.innerText||'').trim().toLowerCase()
                                               .includes('continue with google cloud'));
                      if (el) el.click();
                    })()""")
                break
            time.sleep(2)
        except cdp.CDPConnectionLost:
            log.info("  UI reloaded; reattaching")
            page, _ = ag_wait_ready()
            time.sleep(2)
    if not clicked:
        screenshot("no-gcp-button")
        raise RuntimeError("Google Cloud / Business account sign-in button not found")

    # 2. the xdg-open shim records the OAuth URL
    log.info("waiting for the OAuth URL")
    auth_url = None
    for _ in range(60):
        time.sleep(1)
        if os.path.exists(URL_CAPTURE):
            txt = open(URL_CAPTURE).read().strip().split("\n")
            if txt and txt[-1].startswith("http"):
                auth_url = txt[-1]
                break
    if not auth_url:
        raise RuntimeError("Antigravity never requested a browser URL")
    log.info("OAuth URL captured (%d chars)", len(auth_url))

    # 3. sign in
    launch_chrome(auth_url, CHROME_PORT)
    try:
        google_signin(cfg["AG_EMAIL"], cfg["AG_PASSWORD"])
    finally:
        # the token arrives over the localhost callback, so the browser has
        # done its job either way - never leave it sitting on screen
        close_auth_chrome()
    focus_antigravity()

    return finish_wizard(page, cfg)


PROJECT_SEL = 'input[placeholder="my-gcp-project"], input[placeholder="my-project-id"]'

JS_PICK_PLUGIN = """
(() => {
  const want = 'google antigravity sdk';
  for (const cb of document.querySelectorAll('input[type=checkbox]')) {
    let n = cb, label = '';
    for (let d = 0; d < 6 && n; d++) {
      n = n.parentElement;
      if (n) { const t = (n.innerText || '').trim();
               if (t.length > 2) { label = t.split('\\n')[0]; break; } }
    }
    if (label.toLowerCase().includes(want)) {
      if (!cb.checked) cb.click();
      return 'ok';
    }
  }
  return 'not-found';
})()
"""


def finish_wizard(page, cfg, timeout=360):
    """Project ID, then Terms -> Theme -> Build with Google -> Finish.

    Driven as a state machine rather than a fixed sequence. Antigravity
    reloads all of its windows whenever the language-server port changes,
    which drops the DevTools websocket part-way through; re-reading the
    current screen each pass means a reload costs one retry instead of the
    whole run, and the steps may legitimately arrive in a different order.
    """
    log.info("walking the setup wizard")
    deadline = time.time() + timeout
    seen = {}

    while time.time() < deadline:
        try:
            if page is None:
                page = ag_page(timeout=60)

            low = (page.text() or "").lower()
            if not low:
                time.sleep(2)
                continue

            if page.query_visible('input[placeholder="my-gcp-project"]') or \
               page.query_visible('input[placeholder="my-project-id"]') or \
               page.query_visible('input[placeholder*="project" i]') or \
               "select your license" in low or \
               "provide google cloud project" in low:
                step = "project-id"
            elif "terms of service" in low:
                step = "terms"
            elif "select antigravity theme" in low:
                step = "theme"
            elif "build with google" in low:
                step = "plugins"
            elif "new conversation" in low:
                log.info("  wizard finished")
                time.sleep(4)
                page.close()
                return True
            else:
                time.sleep(2)
                continue

            # a screen we cannot get past should fail loudly, not spin
            seen[step] = seen.get(step, 0) + 1
            if seen[step] > 8:
                raise RuntimeError(f"stuck on the {step!r} wizard step")

            if step == "project-id":
                log.info("  entering project ID %s", cfg["AG_PROJECT_ID"])
                for p_sel in ('input[placeholder="my-gcp-project"]',
                              'input[placeholder="my-project-id"]',
                              'input[placeholder*="project" i]',
                              'input[type="text"]'):
                    if page.query_visible(p_sel):
                        page.type_text(p_sel, cfg["AG_PROJECT_ID"])
                        break
                time.sleep(1.5)
                page.evaluate("""
                    (() => {
                      const els = [...document.querySelectorAll('*')];
                      const el = els.find(e => (e.innerText||'').trim().toLowerCase()
                                               .includes('use agent platform instead'));
                      if (el) el.click();
                    })()""")
                time.sleep(1)
                click_exact(page, "Next", tries=2)
            elif step == "terms":
                page.evaluate("""
                    (() => {
                      for (const cb of document.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
                        if (!cb.checked && cb.getAttribute('aria-checked') !== 'true') cb.click();
                      }
                    })()""")
                time.sleep(0.5)
                click_exact(page, "Next", tries=2)
            elif step == "theme":
                click_exact(page, "Next", tries=2)
            elif step == "plugins":
                log.info("  plugin: %s", page.evaluate(JS_PICK_PLUGIN))
                time.sleep(1)
                click_exact(page, "Finish", tries=2)
            time.sleep(3)

        except (cdp.CDPConnectionLost, cdp.CDPError):
            log.info("  UI reloaded mid-wizard; reattaching")
            page = None
            time.sleep(4)

    raise RuntimeError("the setup wizard did not complete in time")


def resilient(fn, *a, tries=3, **kw):
    """Run a step, retrying if the app reloads its windows underneath it."""
    for attempt in range(tries):
        try:
            return fn(*a, **kw)
        except cdp.CDPConnectionLost:
            log.info("  UI reloaded during %s; retrying", fn.__name__)
            time.sleep(5)
    log.warning("  %s did not survive the UI reloads", fn.__name__)
    return None


def is_settings_open(page):
    """Check if the Agent Settings modal/dialog is currently visible."""
    js_check = """
    (() => {
      const body = (document.body.innerText || '').toLowerCase();
      if (body.includes('security preset') || body.includes('artifact review policy') ||
          body.includes('keep in menu bar') ||
          (body.includes('appearance') && body.includes('models') && body.includes('customizations'))) {
        return true;
      }
      const dlgs = [...document.querySelectorAll('[role=dialog], [class*=modal], [class*=dialog], [class*=settings]')];
      return dlgs.some(d => {
        const r = d.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        const t = (d.innerText || d.textContent || '').toLowerCase();
        return t.includes('agent') || t.includes('security preset') ||
               t.includes('artifact review policy') || t.includes('keep in menu bar') ||
               (t.includes('settings') && (t.includes('appearance') || t.includes('models')));
      });
    })()
    """
    try:
        return bool(page.evaluate(js_check))
    except Exception:
        return False


def is_sidebar_expanded(page):
    """Check if the sidebar is currently expanded."""
    js_check = """
    (() => {
      const textMatches = (e) => {
        const txt = (e.innerText || e.textContent || '').trim().toLowerCase();
        return ['projects', 'new conversation', 'settings', 'scheduled tasks', 'conversation history'].includes(txt);
      };
      const els = [...document.querySelectorAll('*')].filter(e => textMatches(e));
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return true;
      }
      return false;
    })()
    """
    try:
        return bool(page.evaluate(js_check))
    except Exception:
        return False


def expand_sidebar(page):
    """Expand the sidebar if it is currently collapsed."""
    if is_sidebar_expanded(page):
        log.info("  sidebar is already expanded")
        return True

    js_toggle = """
    (() => {
      const btns = [...document.querySelectorAll('button, [role=button], [aria-label], [title]')];
      const btn = btns.find(b => {
        const r = b.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        const aria = (b.getAttribute('aria-label') || b.getAttribute('title') || '').toLowerCase();
        return aria.includes('toggle sidebar') || aria.includes('expand sidebar') || aria.includes('show sidebar');
      });
      if (btn) {
        btn.click();
        return true;
      }
      return false;
    })()
    """
    try:
        if page.evaluate(js_toggle):
            log.info("  expanded sidebar via toggle button")
            time.sleep(1.5)
            if is_sidebar_expanded(page):
                return True

        # Fallback: Ctrl+B shortcut to toggle sidebar
        log.info("  attempting sidebar toggle via Ctrl+B shortcut")
        for typ in ("keyDown", "keyUp"):
            page.send("Input.dispatchKeyEvent", {
                "type": typ,
                "key": "b",
                "code": "KeyB",
                "windowsVirtualKeyCode": 66,
                "nativeVirtualKeyCode": 66,
                "modifiers": 2,  # Control
            })
        time.sleep(1.5)
        if is_sidebar_expanded(page):
            return True

        # If Ctrl+B collapsed it instead, press Ctrl+B again to revert
        for typ in ("keyDown", "keyUp"):
            page.send("Input.dispatchKeyEvent", {
                "type": typ,
                "key": "b",
                "code": "KeyB",
                "windowsVirtualKeyCode": 66,
                "nativeVirtualKeyCode": 66,
                "modifiers": 2,  # Control
            })
        time.sleep(1.0)
        return is_sidebar_expanded(page)
    except Exception as e:
        log.warning("  expand_sidebar exception: %s", e)
        return False


def open_settings(page):
    """Open Settings, expanding the sidebar if it is collapsed."""
    if is_settings_open(page):
        return True

    for attempt in range(4):
        # 1. Try clicking exact visible text "Settings"
        if page.evaluate(JS_CLICK_EXACT % json.dumps("Settings")):
            time.sleep(2)
            if is_settings_open(page):
                return True

        # 2. Try clicking element with aria-label or title "Settings" or "Agent Settings"
        js_click_attr = """
        (() => {
          const els = [...document.querySelectorAll('button, [role=button], [role=menuitem], a, div, span')];
          const el = els.find(e => {
            const r = e.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const txt = (e.innerText || e.textContent || '').trim().toLowerCase();
            const aria = (e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('data-tooltip') || '').trim().toLowerCase();
            return txt === 'settings' || aria === 'settings' || aria === 'agent settings';
          });
          if (el) { el.scrollIntoView({block: 'center'}); el.click(); return true; }
          return false;
        })()
        """
        if page.evaluate(js_click_attr):
            time.sleep(2)
            if is_settings_open(page):
                return True

        # 3. If settings is still not open, sidebar may be collapsed -> expand it
        log.info("  Settings not opened yet (attempt %d); expanding sidebar if collapsed", attempt + 1)
        expand_sidebar(page)
        time.sleep(1.5)

        # 4. Fallback: Ctrl+, keyboard shortcut to open Settings directly
        for typ in ("keyDown", "keyUp"):
            page.send("Input.dispatchKeyEvent", {
                "type": typ,
                "key": ",",
                "code": "Comma",
                "windowsVirtualKeyCode": 188,
                "nativeVirtualKeyCode": 188,
                "modifiers": 2,  # Control
            })
        time.sleep(2)
        if is_settings_open(page):
            return True

    return is_settings_open(page)


def close_settings(page):
    for _ in range(2):
        for typ in ("keyDown", "keyUp"):
            page.send("Input.dispatchKeyEvent",
                      {"type": typ, "key": "Escape", "code": "Escape",
                       "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
        time.sleep(1.5)


def apply_settings():
    """Turbo Mode + Always Proceed, and Keep In Menu Bar off (per the lab guide)."""
    page = ag_page()
    log.info("applying required settings")
    if not open_settings(page):
        screenshot("settings-not-open")
        log.warning("could not open Settings; skipping")
        page.close()
        return

    click_exact(page, "Agent")
    time.sleep(2)
    set_combo(page, "Security Preset", "Turbo mode")
    set_combo(page, "Artifact Review Policy", "Always Proceed")

    click_exact(page, "App")
    time.sleep(2)
    log.info("  Keep In Menu Bar: %s",
             page.evaluate(JS_SWITCH_OFF % json.dumps("Keep In Menu Bar")))

    close_settings(page)
    page.close()


def get_folder_dialog_window():
    """Find the window ID of any active GTK / system folder picker dialog."""
    patterns = ("^Open workspace$", "^Open Workspace$", "^Select Folder$", "^Open Folder$",
                "^Select a folder$", "^Open$", "workspace", "FileChooser")
    for pat in patterns:
        out = xdo("search", "--onlyvisible", "--name", pat)
        if out:
            wids = [w.strip() for w in out.split("\n") if w.strip()]
            if wids:
                return wids[0]
    return ""


def close_popovers(page):
    """Close any leftover popovers or dropdown menus in the UI."""
    try:
        page.evaluate("""
        (() => {
          const esc = new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true});
          document.dispatchEvent(esc);
        })()""")
    except Exception:
        pass


def add_session_project(session_dir):
    """Add the Desktop session folder as a project.

    Uses multiple fallback strategies for triggering the folder picker (UI buttons,
    menu items, Create Project wizard, Ctrl+O shortcut) and robust confirmation handling for GTK dialogs.
    """
    if not session_dir or session_dir.strip() == "/" or not os.path.isdir(session_dir):
        log.warning("session dir %s is invalid or does not exist; skipping project step", session_dir)
        return False

    name = os.path.basename(session_dir.rstrip("/"))
    if not name:
        log.warning("invalid project name from %s; skipping project step", session_dir)
        return False

    page = ag_page()
    log.info("adding %s as a project", session_dir)
    close_popovers(page)

    # Retry loop for opening and driving the folder picker (up to 3 attempts)
    for attempt in range(1, 4):
        log.info("  attempt %d/3 to add session project %s", attempt, session_dir)
        if attempt > 1:
            page.close()
            page = ag_page()

        close_popovers(page)
        expand_sidebar(page)
        time.sleep(1)

        # 1. First check if the project is ALREADY open/added on EVERY attempt
        body = page.text() or ""
        if name in body and "does not exist" not in body:
            log.info("  project %r is already open/added in Antigravity", name)
            click_exact(page, name, tries=1, delay=0.5)
            close_popovers(page)
            page.close()
            return True

        # Close any leftover GTK dialogs first
        leftover = get_folder_dialog_window()
        if leftover:
            xdo("windowactivate", "--sync", leftover)
            xdo("key", "Escape")
            time.sleep(0.5)

        # Step A: Attempt to trigger folder picker via UI buttons/menu
        opened = page.evaluate("""
        (() => {
          // Check for direct visible action buttons first
          const btns = [...document.querySelectorAll('button, [role=button], a, span')];
          const direct = btns.find(b => {
            const r = b.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
            return ['add folder', 'open folder', 'add project', 'open workspace', 'open workspace...'].includes(txt);
          });
          if (direct) { direct.click(); return 'direct_action'; }

          // Fallback: Find exact h2 Projects heading and click its '+' button
          const h2 = [...document.querySelectorAll('h2')].find(e => (e.textContent||'').trim() === 'Projects');
          if (h2 && h2.parentElement) {
            const btn = [...h2.parentElement.querySelectorAll('button, [role=button]')].find(b => {
              const aria = (b.getAttribute('aria-label') || '').toLowerCase();
              return !aria.includes('display options');
            });
            if (btn) { btn.click(); return 'plus_button'; }
          }
          return false;
        })()""")

        if opened == 'plus_button':
            time.sleep(1.5)
            for label in ("New Project", "Add Folder", "Open Folder", "Add project", "Open Workspace"):
                if page.evaluate(JS_CLICK_EXACT % json.dumps(label)):
                    log.info("  clicked dropdown item %r", label)
                    time.sleep(1.5)
                    break

        # Check if "Create Project" modal appeared with an "Add Folder" button
        time.sleep(1.0)
        page.evaluate("""
        (() => {
          const btns = [...document.querySelectorAll('button, [role=button], a, span')].filter(b => {
            const r = b.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && (b.innerText||'').trim() === 'Add Folder';
          });
          if (btns.length) { btns[0].click(); return true; }
          return false;
        })()""")

        # Step B: If GTK dialog didn't open via UI, try keyboard shortcuts (Ctrl+O)
        time.sleep(2)
        wid = get_folder_dialog_window()
        if not wid:
            log.info("  dialog didn't open via UI menu; attempting Ctrl+O shortcut")
            focus_antigravity()
            xdo("key", "ctrl+o")
            time.sleep(2)
            wid = get_folder_dialog_window()

        if not wid:
            log.warning("  folder dialog never appeared on attempt %d", attempt)
            time.sleep(2)
            continue

        log.info("  driving GTK folder dialog (window %s)", wid)
        set_clipboard(session_dir)
        xdo("windowactivate", "--sync", wid)
        time.sleep(0.8)

        # Paste folder path into location bar
        xdo("key", "ctrl+l"); time.sleep(0.6)    # GTK location bar
        xdo("key", "ctrl+a"); time.sleep(0.3)
        xdo("key", "ctrl+v"); time.sleep(1.0)    # paste, never type

        # Step C: Confirm the typed path directly using alt+o without pressing Return inside
        # the location bar. Pressing Return first navigates into the folder where GTK preselects
        # its first subdirectory (e.g. .agents). alt+o triggers "Open" directly on the path.
        closed = False
        for confirm_step in range(10):
            xdo("windowactivate", "--sync", wid)
            if confirm_step % 2 == 0:
                xdo("key", "alt+o")
            else:
                xdo("key", "alt+O")
            time.sleep(1.0)
            if not get_folder_dialog_window():
                closed = True
                log.info("  GTK folder dialog closed successfully")
                break

        if not closed:
            log.warning("  folder dialog still open after alt+o attempts; escaping")
            xdo("windowactivate", "--sync", wid)
            xdo("key", "Escape")
            time.sleep(2)
            continue

        # Step D: Handle post-picker dialog / wizard buttons ("Create Project", "Add", "Trust", "OK")
        time.sleep(2)
        for next_label in ("Create Project", "Next", "Add Folder", "Add", "Create", "Trust", "Open Workspace", "Confirm", "OK"):
            if click_exact(page, next_label, tries=1, delay=0.5):
                log.info("  clicked post-picker button %r", next_label)
                time.sleep(2)

        time.sleep(2)
        close_popovers(page)
        time.sleep(1)
        body = page.text() or ""
        if name in body and "does not exist" not in body:
            log.info("  project added successfully: %s", name)
            page.close()
            return True
        else:
            log.warning("  project %r not found in page body after attempt %d", name, attempt)

    close_popovers(page)
    page.close()
    return False


def start_first_conversation():
    """Click the project's 'Start first conversation' call to action."""
    page = ag_page()
    log.info("starting the first conversation")
    ok = False
    for _ in range(8):
        for candidate in ("Start first conversation", "Start First Conversation",
                          "Start conversation", "New conversation", "Start Conversation"):
            if click_exact(page, candidate, tries=1, delay=0.5):
                ok = True
                break
        if ok:
            break

        opened = page.evaluate("""
        (() => {
          const btns = [...document.querySelectorAll('button, [role=button], a')];
          const btn = btns.find(b => {
            const r = b.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const t = (b.innerText || b.textContent || '').toLowerCase();
            return t.includes('start') && t.includes('conversation');
          });
          if (btn) { btn.click(); return true; }
          return false;
        })()""")
        if opened:
            log.info("  clicked 'start conversation' CTA via JS")
            ok = True
            break

        time.sleep(2)

    if not ok:
        log.info("  no 'Start first conversation' card (already started or dismissed)")
    else:
        time.sleep(4)
    page.close()
    return ok



# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Unattended Antigravity lab sign-in")
    ap.add_argument("--email"); ap.add_argument("--password")
    ap.add_argument("--project-id"); ap.add_argument("--session-dir")
    ap.add_argument("--force", action="store_true",
                    help="run setup even if it is already marked complete")
    ap.add_argument("--restart", action="store_true",
                    help="relaunch the app at the end to drop the debug port "
                         "(off by default: the relaunch can come up windowless)")
    ap.add_argument("--no-kill", action="store_true",
                    help="do not terminate other running Antigravity instances")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config(args)
    log.info("account: %s   project: %s", cfg["AG_EMAIL"], cfg["AG_PROJECT_ID"])

    global AG_PORT, CHROME_PORT

    # start from a clean slate so the DevTools port is ours
    if not args.no_kill:
        kill_antigravity()

    # A just-killed instance can still hold a port, so pick one at run time
    # and retry on the (rare) race where it is taken between check and bind.
    did_onboard = False
    for attempt in range(1, 4):
        AG_PORT = pick_port(9500)
        CHROME_PORT = pick_port(AG_PORT + 1)
        log.info("attempt %d: devtools ports app=%d chrome=%d",
                 attempt, AG_PORT, CHROME_PORT)
        launch_antigravity(debug_port=AG_PORT, cfg=cfg)
        try:
            cdp.wait_for_cdp(AG_PORT, timeout=90)
            break
        except cdp.CDPError as e:
            log.warning("devtools did not come up on %d (%s)", AG_PORT, e)
            kill_antigravity()
    else:
        log.error("Antigravity never exposed a devtools port; giving up")
        return 1

    # The marker alone is not trusted: state can be cleared out from under it,
    # which would otherwise leave the attendee staring at a sign-in screen
    # while the script reported "already complete" and did nothing.
    if os.path.exists(state_file()) and not args.force:
        try:
            page, state = ag_wait_ready()
            page.close()
        except Exception:
            state = "unknown"
        if state == "workbench":
            log.info("setup already complete and the app is signed in")
            focus_antigravity()
            return 0
        log.warning("setup was marked complete but the app is at %r; "
                    "running setup again", state)

    try:
        did_onboard = do_onboarding(cfg)
    except Exception as e:
        log.error("onboarding failed: %s", e)
        screenshot("onboarding-failure")
        return 1

    try:
        resilient(apply_settings)
    except Exception as e:
        log.error("settings step failed: %s", e)
        screenshot("settings-failure")

    try:
        resilient(add_session_project, cfg["AG_SESSION_DIR"])
    except Exception as e:
        log.error("project step failed: %s", e)
        screenshot("project-failure")

    try:
        resilient(start_first_conversation)
    except Exception as e:
        log.error("first-conversation step failed: %s", e)
        screenshot("conversation-failure")

    open(state_file(), "w").write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
    log.info("setup complete%s", " (onboarding performed)" if did_onboard else "")

    close_auth_chrome()

    # The configured instance is left running. Restarting here used to be the
    # default, purely to drop the localhost debug port, and it cost more than
    # it bought: Antigravity often came back up without ever creating a window
    # (the app then quits), and stopping it mid-write risked its auth state.
    if args.restart:
        log.info("relaunching Antigravity without the debug port")
        if not args.no_kill:
            kill_antigravity()
        launch_antigravity(cfg=cfg)
        time.sleep(18)          # let the window settle before taking focus
        if not main_antigravity_window():
            log.warning("relaunched Antigravity has no window; "
                        "starting it once more")
            kill_antigravity()
            launch_antigravity(cfg=cfg)
            time.sleep(18)
    focus_antigravity()
    return 0


if __name__ == "__main__":
    sys.exit(main())
