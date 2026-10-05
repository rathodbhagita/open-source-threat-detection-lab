#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client.

Used to drive both Antigravity (Electron) and Chrome by DOM rather than by
screen coordinates, which do not survive a resolution change.
"""
import json
import time
import urllib.error
import urllib.request

import websocket  # websocket-client


class CDPError(RuntimeError):
    pass


class CDPConnectionLost(CDPError):
    """The websocket died - typically the app reloaded its windows."""


def http_json(port, path, host="127.0.0.1", timeout=5):
    url = f"http://{host}:{port}/json{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def wait_for_cdp(port, timeout=60, host="127.0.0.1"):
    """Block until the DevTools HTTP endpoint answers."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return http_json(port, "/version", host=host)
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            last = e
            time.sleep(0.5)
    raise CDPError(f"CDP on port {port} never came up: {last}")


def list_targets(port, host="127.0.0.1"):
    try:
        return http_json(port, "/list", host=host)
    except Exception:
        return []


def find_target(port, predicate, timeout=60, host="127.0.0.1"):
    """Wait for a page target matching predicate(target_dict) -> bool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for t in list_targets(port, host=host):
            if t.get("type") == "page" and predicate(t):
                return t
        time.sleep(0.5)
    return None


class Page:
    """A CDP session attached to one page target."""

    def __init__(self, ws_url, timeout=30):
        # suppress_origin avoids the DevTools 403 on cross-origin WS handshakes,
        # so we don't need --remote-allow-origins on the target process.
        self.ws = websocket.create_connection(ws_url, timeout=timeout,
                                              suppress_origin=True,
                                              max_size=64 * 1024 * 1024)
        self._id = 0

    @classmethod
    def attach(cls, target, **kw):
        return cls(target["webSocketDebuggerUrl"], **kw)

    def send(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        try:
            self.ws.send(json.dumps({"id": mid, "method": method,
                                     "params": params or {}}))
        except (websocket.WebSocketException, OSError) as e:
            raise CDPConnectionLost(str(e)) from e
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError) as e:
                raise CDPConnectionLost(str(e)) from e
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # otherwise it's an event; ignore
        raise CDPError(f"timeout waiting for {method}")

    def evaluate(self, expr, timeout=30, await_promise=False):
        """Evaluate JS in the page, returning a plain Python value."""
        r = self.send("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            desc = r["exceptionDetails"].get("exception", {}).get("description")
            raise CDPError(f"JS exception: {desc}")
        return r.get("result", {}).get("value")

    def wait_for(self, expr, timeout=60, interval=0.5, desc=None):
        """Poll a JS expression until it returns truthy. Returns the value."""
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                v = self.evaluate(expr)
                if v:
                    return v
            except CDPError as e:
                last_err = e
            time.sleep(interval)
        raise CDPError(f"timed out waiting for {desc or expr} ({last_err})")

    # ---- input helpers -------------------------------------------------
    # We drive real CDP Input events rather than setting .value directly,
    # because framework-backed fields (Google's login) ignore raw value sets.

    def query_visible(self, selector):
        """True if a visible element matches the CSS selector."""
        js = ("(()=>{const e=document.querySelector(%s);if(!e)return false;"
              "const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})()"
              % json.dumps(selector))
        try:
            return bool(self.evaluate(js))
        except CDPError:
            return False

    def focus(self, selector):
        js = ("(()=>{const e=document.querySelector(%s);if(!e)return false;"
              "e.focus();e.click();return true;})()" % json.dumps(selector))
        return bool(self.evaluate(js))

    def type_text(self, selector, text, clear=True):
        """Focus a field and type into it with genuine input events."""
        if not self.focus(selector):
            raise CDPError(f"cannot focus {selector}")
        if clear:
            self.evaluate("(()=>{const e=document.querySelector(%s);"
                          "if(e){e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                          % json.dumps(selector))
            self.focus(selector)
        self.send("Input.insertText", {"text": text})
        # nudge frameworks that only listen for key events
        self.evaluate("(()=>{const e=document.querySelector(%s);if(e){"
                      "e.dispatchEvent(new Event('input',{bubbles:true}));"
                      "e.dispatchEvent(new Event('change',{bubbles:true}));}})()"
                      % json.dumps(selector))
        return True

    def press(self, key="Enter"):
        codes = {"Enter": (13, "Enter"), "Tab": (9, "Tab")}
        vk, k = codes.get(key, (13, "Enter"))
        for typ in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {
                "type": typ, "key": k, "code": k,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            })

    def click_selector(self, selector):
        js = ("(()=>{const e=document.querySelector(%s);if(!e)return false;"
              "e.click();return true;})()" % json.dumps(selector))
        return bool(self.evaluate(js))

    def click_text(self, text, tags="button,a,[role=button],div[role=button],span"):
        """Click the first visible element whose text contains `text`."""
        js = """
        (() => {
          const want = %s.toLowerCase();
          const els = [...document.querySelectorAll(%s)];
          const el = els.find(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 &&
                   (e.innerText || e.textContent || '').trim().toLowerCase().includes(want);
          });
          if (!el) return false;
          el.scrollIntoView({block:'center'});
          el.click();
          return true;
        })()
        """ % (json.dumps(text), json.dumps(tags))
        return bool(self.evaluate(js))

    def text(self):
        return self.evaluate("document.body ? document.body.innerText : ''") or ""

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
