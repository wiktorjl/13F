#!/usr/bin/env python3
"""Full-data UI walkthrough using Chromium's DevTools protocol and only stdlib."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from tests.support import running_server  # noqa: E402


class WalkthroughFailure(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report or {}


class WebSocket:
    """Minimal RFC 6455 client sufficient for a local Chromium CDP socket."""

    def __init__(self, url: str, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise WalkthroughFailure(f"Unsupported DevTools WebSocket URL: {url}")
        port = parsed.port or 80
        self.socket = socket.create_connection((parsed.hostname, port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.buffer = bytearray()
        resource = parsed.path or "/"
        if parsed.query:
            resource += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://localhost\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        header = self._read_until(b"\r\n\r\n", 64 * 1024)
        lines = header.decode("iso-8859-1").split("\r\n")
        if " 101 " not in f" {lines[0]} ":
            raise WalkthroughFailure(f"DevTools WebSocket upgrade failed: {lines[0]}")
        headers = {
            name.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for name, value in [line.split(":", 1)]
        }
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WalkthroughFailure("DevTools WebSocket returned an invalid accept token")

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buffer:
            chunk = self.socket.recv(64 * 1024)
            if not chunk:
                raise WalkthroughFailure("DevTools WebSocket closed during handshake")
            self.buffer.extend(chunk)
            if len(self.buffer) > limit:
                raise WalkthroughFailure("DevTools WebSocket handshake exceeded its size limit")
        boundary = self.buffer.index(marker) + len(marker)
        value = bytes(self.buffer[:boundary])
        del self.buffer[:boundary]
        return value

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.socket.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise WalkthroughFailure("DevTools WebSocket closed unexpectedly")
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        length = len(payload)
        first = 0x80 | opcode
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length < 65_536:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def send_text(self, value: str) -> None:
        self._send_frame(0x1, value.encode("utf-8"))

    def receive_text(self, timeout: float) -> str:
        self.socket.settimeout(max(0.05, timeout))
        parts: list[bytes] = []
        message_opcode: int | None = None
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise WalkthroughFailure("DevTools WebSocket sent a close frame")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message_opcode = opcode
                parts = [payload]
            elif opcode == 0x0 and message_opcode is not None:
                parts.append(payload)
            else:
                continue
            if final:
                if message_opcode != 0x1:
                    raise WalkthroughFailure("DevTools sent an unexpected binary message")
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.socket.close()


class ChromiumProcess:
    def __init__(self, executable: str | None = None):
        requested = executable or os.environ.get("CHROMIUM")
        self.executable = requested or next(
            (candidate for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
             if (candidate := shutil.which(name))),
            "",
        )
        if not self.executable:
            raise WalkthroughFailure("Chromium was not found; set CHROMIUM or pass --chromium")
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._log_file = None
        self.process: subprocess.Popen[bytes] | None = None
        self.devtools_port: int | None = None

    def __enter__(self) -> "ChromiumProcess":
        self._temporary = tempfile.TemporaryDirectory(prefix="13f-chromium-")
        profile = Path(self._temporary.name)
        log_path = profile / "chromium.log"
        self._log_file = log_path.open("w+b")
        command = [
            self.executable,
            "--headless=new",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-features=MediaRouter,OptimizationHints,Translate",
            "--disable-gpu",
            "--disable-popup-blocking",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--password-store=basic",
            "--remote-allow-origins=*",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--use-mock-keychain",
            "about:blank",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        self.process = subprocess.Popen(command, stdout=self._log_file, stderr=subprocess.STDOUT)
        active_port = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise WalkthroughFailure(
                    f"Chromium exited during startup ({self.process.returncode}): {self.read_log()}"
                )
            if active_port.exists():
                lines = active_port.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].isdigit():
                    self.devtools_port = int(lines[0])
                    break
            time.sleep(0.05)
        if self.devtools_port is None:
            raise WalkthroughFailure(f"Chromium did not expose DevTools in time: {self.read_log()}")
        return self

    def page_websocket_url(self) -> str:
        if self.devtools_port is None:
            raise WalkthroughFailure("Chromium has not started")
        endpoint = f"http://127.0.0.1:{self.devtools_port}/json/list"
        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(endpoint, timeout=2) as response:
                    targets = json.load(response)
                page = next(target for target in targets if target.get("type") == "page")
                return str(page["webSocketDebuggerUrl"])
            except (OSError, ValueError, KeyError, StopIteration) as exc:
                last_error = exc
                time.sleep(0.05)
        raise WalkthroughFailure(f"Could not find a Chromium page target: {last_error}")

    def read_log(self) -> str:
        if self._log_file is None:
            return ""
        self._log_file.flush()
        self._log_file.seek(0)
        return self._log_file.read().decode("utf-8", errors="replace")[-8_000:]

    def __exit__(self, _type, _value, _traceback) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_file is not None:
            self._log_file.close()
        if self._temporary is not None:
            self._temporary.cleanup()


class CDP:
    def __init__(self, websocket_url: str, app_origin: str):
        self.websocket = WebSocket(websocket_url)
        self.app_origin = app_origin.rstrip("/")
        self.next_id = 0
        self.requests: dict[str, dict[str, Any]] = {}
        self.responses: list[dict[str, Any]] = []
        self.console: list[dict[str, Any]] = []
        self.console_failures: list[dict[str, Any]] = []
        self.page_failures: list[dict[str, Any]] = []
        self.network_failures: list[dict[str, Any]] = []

    def _is_app_url(self, url: str) -> bool:
        try:
            target = urllib.parse.urlsplit(url)
            origin = urllib.parse.urlsplit(self.app_origin)
            return (target.scheme, target.hostname, target.port) == (origin.scheme, origin.hostname, origin.port)
        except ValueError:
            return False

    @staticmethod
    def _remote_text(argument: dict[str, Any]) -> str:
        if "value" in argument:
            value = argument["value"]
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return str(argument.get("description") or argument.get("type") or "")

    def _event(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            self.requests[str(params.get("requestId"))] = {
                "url": str(request.get("url", "")),
                "method": str(request.get("method", "")),
                "type": str(params.get("type", "")),
            }
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            record = {
                "url": str(response.get("url", "")),
                "status": int(response.get("status", 0)),
                "mime_type": str(response.get("mimeType", "")),
                "type": str(params.get("type", "")),
            }
            self.responses.append(record)
            if self._is_app_url(record["url"]) and record["status"] >= 400:
                self.network_failures.append(record)
        elif method == "Network.loadingFailed":
            request = self.requests.get(str(params.get("requestId")), {})
            record = {
                "url": request.get("url", "unknown"),
                "error": str(params.get("errorText", "unknown network error")),
                "canceled": bool(params.get("canceled", False)),
            }
            if self._is_app_url(str(record["url"])):
                self.network_failures.append(record)
        elif method == "Runtime.consoleAPICalled":
            record = {
                "type": str(params.get("type", "log")),
                "text": " ".join(self._remote_text(item) for item in params.get("args", [])),
            }
            self.console.append(record)
            if record["type"] in {"error", "assert"}:
                self.console_failures.append(record)
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            exception = detail.get("exception", {})
            self.page_failures.append({
                "kind": "exception",
                "text": str(exception.get("description") or detail.get("text") or "JavaScript exception"),
                "line": detail.get("lineNumber"),
                "column": detail.get("columnNumber"),
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            record = {"type": str(entry.get("level", "")), "text": str(entry.get("text", ""))}
            self.console.append(record)
            if record["type"] == "error":
                self.console_failures.append(record)
        elif method in {"Inspector.targetCrashed", "Page.javascriptDialogOpening"}:
            self.page_failures.append({"kind": method, "details": params})

    def command(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        self.next_id += 1
        command_id = self.next_id
        self.websocket.send_text(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(self.websocket.receive_text(deadline - time.monotonic()))
            except socket.timeout as exc:
                raise WalkthroughFailure(f"Timed out waiting for DevTools command {method}") from exc
            if message.get("id") == command_id:
                if "error" in message:
                    raise WalkthroughFailure(f"DevTools command {method} failed: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self._event(message)
        raise WalkthroughFailure(f"Timed out waiting for DevTools command {method}")

    def evaluate(self, expression: str, timeout: float = 15) -> Any:
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = details.get("exception", {}).get("description") or details.get("text")
            raise WalkthroughFailure(f"Browser evaluation failed: {description}")
        remote = result.get("result", {})
        return remote.get("value", remote.get("description"))

    def wait_for(self, description: str, expression: str, timeout: float = 20) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        while time.monotonic() < deadline:
            last_value = self.evaluate(expression)
            if last_value:
                return last_value
            time.sleep(0.08)
        raise WalkthroughFailure(f"Timed out waiting for {description}; last value: {last_value!r}")

    def wait_for_api(
        self, path: str, since: int, expected: dict[str, str] | None = None, timeout: float = 20
    ) -> dict[str, Any]:
        expected = expected or {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.evaluate("true")
            for response in self.responses[since:]:
                parsed = urllib.parse.urlsplit(response["url"])
                values = urllib.parse.parse_qs(parsed.query)
                if parsed.path == path and all(values.get(key, [None])[0] == value for key, value in expected.items()):
                    if response["status"] != 200:
                        raise WalkthroughFailure(f"{path} returned HTTP {response['status']}")
                    return response
            time.sleep(0.05)
        raise WalkthroughFailure(f"No successful {path} request observed with parameters {expected}")

    def click(self, selector: str) -> None:
        quoted = json.dumps(selector)
        self.evaluate(
            f"""(() => {{
              const element = document.querySelector({quoted});
              if (!element) throw new Error('Missing element: ' + {quoted});
              if (element.disabled) throw new Error('Disabled element: ' + {quoted});
              element.click();
              return true;
            }})()"""
        )

    def input(self, selector: str, value: str, event: str = "input") -> None:
        quoted_selector = json.dumps(selector)
        quoted_value = json.dumps(value)
        self.evaluate(
            f"""(() => {{
              const element = document.querySelector({quoted_selector});
              if (!element) throw new Error('Missing input: ' + {quoted_selector});
              element.focus();
              element.value = {quoted_value};
              element.dispatchEvent(new Event({json.dumps(event)}, {{bubbles: true}}));
              return element.value;
            }})()"""
        )

    def screenshot(self, path: Path) -> None:
        result = self.command(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            timeout=20,
        )
        data = result.get("data")
        if not isinstance(data, str):
            raise WalkthroughFailure("Chromium did not return PNG screenshot data")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data, validate=True))

    def close(self) -> None:
        self.websocket.close()


class UIWalkthrough:
    def __init__(self, cdp: CDP, app_url: str, screenshot_dir: Path | None = None):
        self.cdp = cdp
        self.app_url = app_url
        self.steps: list[dict[str, Any]] = []
        self.viewports: dict[str, dict[str, Any]] = {}
        self.viewport_failures: list[str] = []
        self.screenshot_dir = screenshot_dir
        self.screenshots: dict[str, str] = {}

    def step(self, name: str, action: Callable[[], None]) -> None:
        started = time.monotonic()
        action()
        self.steps.append({"name": name, "seconds": round(time.monotonic() - started, 3)})

    def table_ready(self, panel: str, body: str, summary: str) -> None:
        expression = f"""(() => {{
          const panel = document.querySelector({json.dumps(panel)});
          const body = document.querySelector({json.dumps(body)});
          const summary = document.querySelector({json.dumps(summary)});
          return Boolean(panel && !panel.hidden && body && body.closest('.table-wrap')?.getAttribute('aria-busy') === 'false'
            && body.querySelector('tr') && summary && !/loading|could not/i.test(summary.textContent));
        }})()"""
        self.cdp.wait_for(f"{panel} table", expression)

    def viewport_check(self, name: str, width: int, height: int, mobile: bool) -> None:
        self.cdp.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                # A responsive CSS viewport stays deterministic when switching
                # after desktop interactions. Touch emulation supplies the
                # phone input model without Chromium's legacy 980px layout
                # viewport transition.
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        # Always set touch state explicitly so a desktop check never inherits
        # the phone input model from an earlier mobile step.
        self.cdp.command("Emulation.setTouchEmulationEnabled",
                         {"enabled": True, "maxTouchPoints": 1} if mobile else {"enabled": False})
        if mobile:
            # Reload the current route so responsive initialization is checked,
            # rather than only resizing the already-rendered desktop DOM.
            mark = len(self.cdp.responses)
            self.cdp.command("Page.reload", {"ignoreCache": True})
            self.cdp.wait_for("mobile document readiness", "document.readyState === 'complete'")
            self.cdp.wait_for_api("/api/meta", mark)
            self.cdp.wait_for_api("/api/holdings", mark)
            self.table_ready("#holdings", "#holdingsBody", "#rowSummary")
        self.cdp.wait_for(f"{name} viewport width", f"window.innerWidth === {width}")
        result = self.cdp.evaluate(
            """(() => {
              const visible = element => element && !element.hidden && element.getClientRects().length;
              const originalWindowX = window.scrollX;
              window.scrollTo(1000000, window.scrollY);
              const horizontalScrollReach = window.scrollX;
              window.scrollTo(originalWindowX, window.scrollY);
              const tableScroller = document.querySelector('#holdings .table-wrap');
              const originalTableX = tableScroller?.scrollLeft || 0;
              if (tableScroller) tableScroller.scrollLeft = tableScroller.scrollWidth;
              const tableScrollReach = tableScroller?.scrollLeft || 0;
              if (tableScroller) tableScroller.scrollLeft = originalTableX;
              const selectors = ['#mainContent', '.view-navigation:not([hidden])', '#sharedFilters:not([hidden])', '.results'];
              const boxes = selectors.map(selector => {
                const element = document.querySelector(selector);
                if (!visible(element)) return {selector, skipped: true};
                const rect = element.getBoundingClientRect();
                return {selector, left: rect.left, right: rect.right, width: rect.width,
                  fits: rect.left >= -1 && rect.right <= innerWidth + 1};
              });
              const tabs = [...document.querySelectorAll('.tabs [role=tab]')].map(element => {
                const rect = element.getBoundingClientRect();
                return {id: element.id, width: rect.width, height: rect.height};
              });
              const overflowers = [...document.querySelectorAll('body *')].map(element => {
                const rect = element.getBoundingClientRect();
                let ancestor = element.parentElement;
                let clippedBy = null;
                while (ancestor && ancestor !== document.body) {
                  const overflow = getComputedStyle(ancestor).overflowX;
                  if (['auto', 'scroll', 'hidden', 'clip'].includes(overflow)) {
                    clippedBy = ancestor.id ? `#${ancestor.id}` : `${ancestor.tagName.toLowerCase()}.${ancestor.className}`;
                    break;
                  }
                  ancestor = ancestor.parentElement;
                }
                return {element: element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.className}`,
                  left: rect.left, right: rect.right, width: rect.width, clippedBy};
              }).filter(item => (item.left < -1 || item.right > innerWidth + 1) && !item.clippedBy).slice(0, 25);
              const farthestRects = [...document.querySelectorAll('body *')].map(element => {
                const rect = element.getBoundingClientRect();
                return {element: element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.className}`,
                  left: rect.left, right: rect.right, width: rect.width};
              }).sort((a, b) => b.right - a.right).slice(0, 12);
              const scrollWidths = [...document.querySelectorAll('body *')].filter(element =>
                element.scrollWidth > element.clientWidth + 1).map(element => ({
                  element: element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.className}`,
                  clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
                  overflowX: getComputedStyle(element).overflowX,
                })).slice(0, 20);
              return {
                innerWidth, innerHeight,
                clientWidth: document.documentElement.clientWidth,
                documentWidth: document.documentElement.scrollWidth,
                horizontalScrollReach,
                noPageOverflow: horizontalScrollReach <= 1,
                tableScrollReach,
                innerTableScrolls: Boolean(tableScroller && tableScroller.scrollWidth > tableScroller.clientWidth
                  && tableScrollReach > 1 && ['auto', 'scroll'].includes(getComputedStyle(tableScroller).overflowX)),
                boxes,
                boxesFit: boxes.every(box => box.skipped || box.fits),
                overflowers,
                farthestRects,
                scrollWidths,
                activeElement: document.activeElement?.id || `${document.activeElement?.tagName}.${document.activeElement?.className}`,
                tabsUsable: tabs.every(tab => tab.width > 0 && tab.height > 0),
                errorHidden: Boolean(document.querySelector('#errorToast')?.hidden),
              };
            })()"""
        )
        self.viewports[name] = result
        if self.screenshot_dir is not None:
            screenshot = self.screenshot_dir / f"chromium-{name}.png"
            self.cdp.screenshot(screenshot)
            self.screenshots[name] = str(screenshot.resolve())
        if (result["innerWidth"] != width or not result["noPageOverflow"]
                or not result["boxesFit"] or not result["innerTableScrolls"]):
            self.viewport_failures.append(f"{name} viewport has horizontal overflow or clipped layout: {result}")
        if not result["tabsUsable"] or not result["errorHidden"]:
            self.viewport_failures.append(f"{name} viewport has unusable navigation or a visible error: {result}")

    def dashboard_ready(self, view: str) -> None:
        """Rows rendered for the view, its nav link active, and the status line (loading/error/empty) hidden."""
        expression = f"""(() => {{
          const active = document.querySelector('#dashNav a.active[data-view={json.dumps(view)}]');
          const status = document.querySelector('#dashStatus');
          return Boolean(active && active.getAttribute('aria-current') === 'page'
            && document.querySelectorAll('#dashRows .dash-row').length > 0 && status && status.hidden
            && document.title === '13F Dashboard — ' + active.textContent.trim());
        }})()"""
        self.cdp.wait_for(f"dashboard {view} rows", expression)

    def dashboard_viewport_check(self, name: str, width: int, height: int, mobile: bool) -> None:
        self.cdp.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
             "screenWidth": width, "screenHeight": height},
        )
        self.cdp.command("Emulation.setTouchEmulationEnabled",
                         {"enabled": True, "maxTouchPoints": 1} if mobile else {"enabled": False})
        if mobile:
            # Reload the current movers route so responsive initialization is
            # checked, not only a resized desktop DOM.
            mark = len(self.cdp.responses)
            self.cdp.command("Page.reload", {"ignoreCache": True})
            self.cdp.wait_for("dashboard mobile document readiness", "document.readyState === 'complete'")
            self.cdp.wait_for_api("/api/dashboard", mark, {"view": "movers", "horizon": "2", "side": "losers"})
            self.dashboard_ready("movers")
        self.cdp.wait_for(f"dashboard {name} viewport width", f"window.innerWidth === {width}")
        result = self.cdp.evaluate(
            """(() => {
              const originalX = window.scrollX;
              window.scrollTo(1000000, window.scrollY);
              const horizontalScrollReach = window.scrollX;
              window.scrollTo(originalX, window.scrollY);
              const describe = element => element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.className}`;
              const boxes = ['.dash-header', '#dashMain', '#dashControls', '#dashRows'].map(selector => {
                const element = document.querySelector(selector);
                if (!element || element.hidden) return {selector, skipped: true};
                const rect = element.getBoundingClientRect();
                return {selector, left: rect.left, right: rect.right, width: rect.width,
                  fits: rect.left >= -1 && rect.right <= innerWidth + 1};
              });
              const overflowers = [...document.querySelectorAll('body *')].map(element => {
                const rect = element.getBoundingClientRect();
                return {element: describe(element), left: rect.left, right: rect.right, width: rect.width};
              }).filter(item => item.left < -1 || item.right > innerWidth + 1).slice(0, 25);
              return {
                innerWidth, innerHeight,
                documentWidth: document.documentElement.scrollWidth,
                noPageOverflow: document.documentElement.scrollWidth <= innerWidth && horizontalScrollReach <= 1,
                boxes,
                boxesFit: boxes.every(box => box.skipped || box.fits),
                overflowers,
                rowCount: document.querySelectorAll('#dashRows .dash-row').length,
                statusHidden: Boolean(document.querySelector('#dashStatus')?.hidden),
                controlsVisible: document.querySelector('#dashControls')?.hidden === false,
                activeElement: document.activeElement?.id || `${document.activeElement?.tagName}.${document.activeElement?.className}`,
              };
            })()"""
        )
        self.viewports[f"dashboard-{name}"] = result
        if self.screenshot_dir is not None:
            screenshot = self.screenshot_dir / f"dashboard-{name}.png"
            self.cdp.screenshot(screenshot)
            self.screenshots[f"dashboard-{name}"] = str(screenshot.resolve())
        if result["innerWidth"] != width or not result["noPageOverflow"] or not result["boxesFit"]:
            self.viewport_failures.append(f"dashboard {name} viewport has horizontal overflow or clipped layout: {result}")
        if not result["rowCount"] or not result["statusHidden"] or not result["controlsVisible"]:
            self.viewport_failures.append(f"dashboard {name} viewport has no rows, a visible status line, or hidden controls: {result}")

    def dashboard_sorting(self) -> None:
        """Column headers on Top Holdings: Ticker starts ascending, a second click flips it, and the metric
        header returns to the default route (no sort parameters) with the metric column marked descending."""
        cdp = self.cdp
        tickers = ("[...document.querySelectorAll('#dashRows .dash-ticker')]"
                   ".map(node => node.textContent.trim().toUpperCase()).filter(text => text && text !== '\u2014')")

        def header_sort(column: str) -> str:
            return f"document.querySelector('#dashHead th[data-sort=\"{column}\"]')?.getAttribute('aria-sort')"

        def glyph(column: str) -> str:
            return f"document.querySelector('#dashHead th[data-sort=\"{column}\"] a.dash-sort.active')?.textContent.trim().slice(-1)"

        mark = len(cdp.responses)
        cdp.click('#dashHead th[data-sort="ticker"] a')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "holdings", "sort": "ticker", "direction": "asc", "page": "1"})
        self.dashboard_ready("holdings")
        cdp.wait_for("ticker header ascending", f"{header_sort('ticker')} === 'ascending' && {header_sort('metric')} === 'none' && {glyph('ticker')} === '\u2191'")
        cdp.wait_for("tickers in ascending order", f"(() => {{ const t = {tickers}; return t.length >= 2 && t[0] <= t[1]; }})()")
        cdp.wait_for("ticker ascending URL", "location.pathname === '/dashboard' && location.search === '?sort=ticker&direction=asc'")

        mark = len(cdp.responses)
        cdp.click('#dashHead th[data-sort="ticker"] a')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "holdings", "sort": "ticker", "direction": "desc", "page": "1"})
        self.dashboard_ready("holdings")
        cdp.wait_for("ticker header descending", f"{header_sort('ticker')} === 'descending' && {glyph('ticker')} === '\u2193'")
        cdp.wait_for("tickers in descending order", f"(() => {{ const t = {tickers}; return t.length >= 2 && t[0] >= t[1]; }})()")
        cdp.wait_for("ticker descending URL", "location.pathname === '/dashboard' && location.search === '?sort=ticker'")

        mark = len(cdp.responses)
        cdp.click('#dashHead th[data-sort="metric"] a')
        response = cdp.wait_for_api("/api/dashboard", mark, {"view": "holdings", "page": "1", "size": "100"})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(response["url"]).query)
        if "sort" in query or "direction" in query:
            raise WalkthroughFailure(f"Default metric sort must not send sort parameters: {response['url']}")
        self.dashboard_ready("holdings")
        cdp.wait_for("metric header descending", f"{header_sort('metric')} === 'descending' && {header_sort('ticker')} === 'none' && {glyph('metric')} === '\u2193'")
        cdp.wait_for("holdings weight metrics after sort reset", "[...document.querySelectorAll('#dashRows .dash-metric')].every(node => node.textContent.endsWith('%'))")
        cdp.wait_for("default holdings URL", "location.pathname === '/dashboard' && !location.search")

    def dashboard_views(self) -> None:
        """Holdings (with sorting and paging), Fresh Initiations, and Top Movers at 2Q/Losers, ending on the desktop check."""
        cdp = self.cdp
        # The explorer mobile check runs just before this; drop its touch
        # emulation before navigating so the new document gets the desktop
        # input model.
        cdp.command("Emulation.setTouchEmulationEnabled", {"enabled": False})
        cdp.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
             "screenWidth": 1440, "screenHeight": 1000},
        )
        mark = len(cdp.responses)
        cdp.command("Page.navigate", {"url": self.app_url + "dashboard"})
        cdp.wait_for("dashboard document readiness", "document.readyState === 'complete'")
        cdp.wait_for_api("/api/dashboard", mark, {"view": "holdings", "page": "1", "size": "100"})
        self.dashboard_ready("holdings")
        cdp.wait_for("holdings weight metrics", "[...document.querySelectorAll('#dashRows .dash-metric')].every(node => node.textContent.endsWith('%'))")
        # Securities without a ticker are hidden by default (unmapped=exclude), so the
        # blank-ticker rendering path must not appear on the default holdings page.
        cdp.wait_for("no unmapped rows by default", "document.querySelectorAll('#dashRows .dash-missing').length === 0")
        cdp.wait_for("movers controls hidden", "document.querySelector('#dashControls')?.hidden === true")
        self.dashboard_sorting()

        # Paging is exercised whenever the list needs it (always on full production data).
        if cdp.evaluate("document.querySelector('#dashPager')?.hidden === false") is True:
            if cdp.evaluate("document.querySelector('#dashNext')?.getAttribute('aria-disabled') === 'true'") is True:
                raise WalkthroughFailure("Dashboard pager is shown but Next is disabled on page 1")
            mark = len(cdp.responses)
            cdp.click("#dashNext")
            cdp.wait_for_api("/api/dashboard", mark, {"view": "holdings", "page": "2"})
            self.dashboard_ready("holdings")
            cdp.wait_for("dashboard page 2 URL", "location.pathname === '/dashboard' && new URLSearchParams(location.search).get('page') === '2'")
            cdp.wait_for("dashboard page 2 focus", "document.activeElement?.id === 'dashRows'")
            cdp.wait_for("Previous enabled on page 2", "document.querySelector('#dashPrev')?.getAttribute('aria-disabled') !== 'true'")

        mark = len(cdp.responses)
        cdp.click('#dashNav a[data-view="initiations"]')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "initiations", "page": "1"})
        self.dashboard_ready("initiations")
        cdp.wait_for("new-holder metrics", "[...document.querySelectorAll('#dashRows .dash-metric')].every(node => node.textContent.endsWith(' new'))")
        cdp.wait_for("flat initiation directions", "[...document.querySelectorAll('#dashRows .dash-direction')].every(node => node.classList.contains('flat'))")
        cdp.wait_for("initiations URL", "location.pathname === '/dashboard/initiations' && !location.search")

        mark = len(cdp.responses)
        cdp.click('#dashNav a[data-view="movers"]')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "movers", "horizon": "1", "side": "gainers", "page": "1"})
        self.dashboard_ready("movers")
        cdp.wait_for("movers controls", "document.querySelector('#dashControls')?.hidden === false")
        cdp.wait_for("gainers default", "Boolean(document.querySelector('#dashSide a.active[data-side=\"gainers\"][aria-current=\"page\"]') && document.querySelector('#dashHorizon a.active[data-horizon=\"1\"][aria-current=\"page\"]'))")
        cdp.wait_for("gainers direction", "document.querySelector('#dashRows .dash-direction')?.classList.contains('up')")

        mark = len(cdp.responses)
        cdp.click('#dashHorizon a[data-horizon="2"]')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "movers", "horizon": "2", "side": "gainers", "page": "1"})
        self.dashboard_ready("movers")
        cdp.wait_for("2Q timeframe active", "Boolean(document.querySelector('#dashHorizon a.active[data-horizon=\"2\"][aria-current=\"page\"]'))")

        mark = len(cdp.responses)
        cdp.click('#dashSide a[data-side="losers"]')
        cdp.wait_for_api("/api/dashboard", mark, {"view": "movers", "horizon": "2", "side": "losers", "page": "1"})
        self.dashboard_ready("movers")
        cdp.wait_for("losers active", "Boolean(document.querySelector('#dashSide a.active[data-side=\"losers\"][aria-current=\"page\"]'))")
        cdp.wait_for("losers direction", "document.querySelector('#dashRows .dash-direction')?.classList.contains('down')")
        cdp.wait_for("movers metrics", "[...document.querySelectorAll('#dashRows .dash-metric')].every(node => node.textContent.endsWith('pp'))")
        cdp.wait_for("movers URL", "location.pathname === '/dashboard/movers' && location.search === '?horizon=2&side=losers'")
        self.dashboard_viewport_check("desktop", 1440, 1000, False)

    def run(self) -> None:
        cdp = self.cdp
        cdp.command("Runtime.enable")
        cdp.command("Page.enable")
        cdp.command("Network.enable", {"maxTotalBufferSize": 10_000_000})
        cdp.command("Log.enable")
        cdp.command("Inspector.enable")
        cdp.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
             "screenWidth": 1440, "screenHeight": 1000},
        )

        def initial_load() -> None:
            mark = len(cdp.responses)
            cdp.command("Page.navigate", {"url": self.app_url})
            cdp.wait_for("document readiness", "document.readyState === 'complete'")
            cdp.wait_for_api("/api/meta", mark)
            cdp.wait_for_api("/api/funds", mark)
            self.table_ready("#funds", "#fundsBody", "#fundSummary")
            cdp.wait_for("initial fund rows", "document.querySelectorAll('#fundsBody .fund-open').length > 0")
            cdp.wait_for("filters collapsed by default", "document.querySelector('#sharedFilters')?.open === false")
            cdp.wait_for("dataset span rendered", "document.querySelector('#statPeriod')?.textContent.includes('→')")
            mark = len(cdp.responses)
            cdp.click('#funds [data-fund-sort="signal_return"] .sort-button')
            cdp.wait_for_api("/api/funds", mark, {"sort": "signal_return", "direction": "desc"})
            self.table_ready("#funds", "#fundsBody", "#fundSummary")
            cdp.wait_for("sortable fund signals", "[...document.querySelectorAll('#fundsBody tr td:nth-child(7)')].some(cell => cell.textContent.includes('%'))")
            mark = len(cdp.responses)
            cdp.click('#funds [data-fund-sort="value"] .sort-button')
            cdp.wait_for_api("/api/funds", mark, {"sort": "value", "direction": "desc"})
            self.table_ready("#funds", "#fundsBody", "#fundSummary")

        self.step("Load Funds on desktop", initial_load)

        def fund_detail_and_back() -> None:
            mark = len(cdp.responses)
            cdp.click("#fundsBody .fund-open")
            cdp.wait_for_api("/api/fund-detail", mark)
            self.table_ready("#detail", "#detailBody", "#detailSummary")
            cdp.wait_for("fund detail identity", "document.querySelector('#detailTitle')?.textContent.trim().length > 1")
            mark = len(cdp.responses)
            cdp.click("#detailBack")
            cdp.wait_for("return to Funds", "!document.querySelector('#funds').hidden && document.querySelector('#detail').hidden")
            cdp.wait_for_api("/api/funds", mark)
            self.table_ready("#funds", "#fundsBody", "#fundSummary")

        self.step("Open a fund detail and return", fund_detail_and_back)

        def stock_detail() -> None:
            mark = len(cdp.responses)
            cdp.click("#tab-securities")
            cdp.wait_for_api("/api/aggregate", mark, {"group": "issuer"})
            self.table_ready("#overview", "#breakdownBody", "#breakdownSummary")
            cdp.wait_for("stock links", "document.querySelectorAll('#breakdownBody .stock-open').length > 0")
            mark = len(cdp.responses)
            cdp.click("#breakdownBody .stock-open")
            cdp.wait_for_api("/api/stock-detail", mark)
            self.table_ready("#detail", "#detailBody", "#detailSummary")
            cdp.wait_for("stock detail identity", "document.querySelector('#detailSubtitle')?.textContent.includes('CUSIP')")
            cdp.click("#detailBack")
            cdp.wait_for("return to Stocks", "!document.querySelector('#overview').hidden && document.querySelector('#detail').hidden")

        self.step("Open a stock detail from Stocks", stock_detail)

        def quarterly_changes() -> None:
            mark = len(cdp.responses)
            cdp.click("#tab-netadds")
            cdp.wait_for_api("/api/net-adds", mark, {"metric": "value"})
            self.table_ready("#netadds", "#netBody", "#netSummary")
            cdp.wait_for("quarterly change rows", "document.querySelectorAll('#netBody .stock-open').length > 0")
            for metric in ("portfolio", "position"):
                mark = len(cdp.responses)
                cdp.click(f'[name="netMetric"][value="{metric}"]')
                cdp.wait_for_api("/api/net-adds", mark, {"metric": metric})
                self.table_ready("#netadds", "#netBody", "#netSummary")
                if cdp.evaluate(f'document.querySelector(\'[name="netMetric"][value="{metric}"]\').checked') is not True:
                    raise WalkthroughFailure(f"Quarterly metric {metric} did not remain selected")

            mark = len(cdp.responses)
            cdp.click('#netHead [data-net-sort="issuer"] .sort-button')
            cdp.wait_for_api("/api/net-adds", mark, {"sort": "issuer", "direction": "asc"})
            self.table_ready("#netadds", "#netBody", "#netSummary")
            query = cdp.evaluate(
                """(() => [...document.querySelectorAll('#netBody .ticker-symbol')]
                  .map(node => node.textContent.trim()).find(value => value && value !== '—')
                  || document.querySelector('#netBody .stock-open')?.dataset.cusip?.slice(0, 3))()"""
            )
            if not query:
                raise WalkthroughFailure("Could not derive a quarterly-change filter from rendered data")
            mark = len(cdp.responses)
            cdp.input("#netSearch", str(query))
            cdp.wait_for_api("/api/net-adds", mark, {"stock_q": str(query)})
            self.table_ready("#netadds", "#netBody", "#netSummary")

        self.step("Change quarterly metrics, sort, and filter", quarterly_changes)

        def positions() -> None:
            mark = len(cdp.responses)
            cdp.click("#tab-holdings")
            cdp.wait_for_api("/api/holdings", mark, {"page": "1"})
            self.table_ready("#holdings", "#holdingsBody", "#rowSummary")
            cdp.wait_for("position rows", "document.querySelectorAll('#holdingsBody .stock-open').length > 0")
            self.viewport_check("desktop", 1440, 1000, False)

            mark = len(cdp.responses)
            cdp.click('#holdings [data-sort="issuer"] .sort-button')
            cdp.wait_for_api("/api/holdings", mark, {"sort": "issuer_asc"})
            self.table_ready("#holdings", "#holdingsBody", "#rowSummary")

            if cdp.evaluate("document.querySelector('#nextPage').disabled") is True:
                raise WalkthroughFailure("Full production Positions data did not provide a second page")
            mark = len(cdp.responses)
            cdp.click("#nextPage")
            cdp.wait_for_api("/api/holdings", mark, {"page": "2"})
            self.table_ready("#holdings", "#holdingsBody", "#rowSummary")
            cdp.wait_for("Positions page 2 label", "document.querySelector('#pageInfo')?.textContent.startsWith('Page 2')")

            query = cdp.evaluate(
                """(() => [...document.querySelectorAll('#holdingsBody .ticker-symbol')]
                  .map(node => node.textContent.trim()).find(value => value && value !== '—')
                  || document.querySelector('#holdingsBody .stock-open')?.dataset.cusip?.slice(0, 6))()"""
            )
            if not query:
                raise WalkthroughFailure("Could not derive a Positions filter from rendered data")
            mark = len(cdp.responses)
            cdp.input('[name="q"]', str(query))
            cdp.evaluate("document.querySelector('#filtersForm').requestSubmit(); true")
            cdp.wait_for_api("/api/holdings", mark, {"q": str(query), "page": "1"})
            self.table_ready("#holdings", "#holdingsBody", "#rowSummary")
            cdp.wait_for("active filter status", "document.querySelector('#activeFilterCount')?.textContent.startsWith('1 active filter')")
            cdp.wait_for("filtered positions", "document.querySelectorAll('#holdingsBody .stock-open').length > 0")

        self.step("Sort, page, and filter Positions", positions)
        self.step("Check the mobile viewport", lambda: self.viewport_check("mobile", 390, 844, True))

        self.step("Browse the dashboard views", self.dashboard_views)
        self.step("Check the dashboard mobile viewport", lambda: self.dashboard_viewport_check("mobile", 375, 812, True))

        cdp.evaluate("true")
        if cdp.console_failures or cdp.page_failures or cdp.network_failures or self.viewport_failures:
            raise WalkthroughFailure(
                "Browser recorded console, page, network, or viewport failures: "
                + json.dumps({
                    "console": cdp.console_failures,
                    "page": cdp.page_failures,
                    "network": cdp.network_failures,
                    "viewport": self.viewport_failures,
                }, ensure_ascii=False)
            )


def run_walkthrough(
    database: Path,
    *,
    chromium: str | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    database = Path(database).resolve()
    if not database.is_file():
        raise WalkthroughFailure(f"Database does not exist: {database}")
    report: dict[str, Any] = {
        "database": str(database),
        "passed": False,
        "steps": [],
        "viewports": {},
        "console": [],
        "console_failures": [],
        "page_failures": [],
        "network_failures": [],
        "api_requests": [],
        "screenshots": {},
    }
    cdp: CDP | None = None
    error: Exception | None = None
    try:
        with running_server(database) as address:
            app_url = f"http://{address[0]}:{address[1]}/"
            with ChromiumProcess(chromium) as process:
                cdp = CDP(process.page_websocket_url(), app_url)
                screenshot_dir = Path(report_path).parent if report_path is not None else None
                walkthrough = UIWalkthrough(cdp, app_url, screenshot_dir=screenshot_dir)
                try:
                    walkthrough.run()
                finally:
                    report["steps"] = walkthrough.steps
                    report["viewports"] = walkthrough.viewports
                    report["screenshots"] = walkthrough.screenshots
                report["passed"] = True
    except Exception as exc:  # preserve structured diagnostics for CLI and verify.py
        error = exc
        report["error"] = str(exc)
    finally:
        if cdp is not None:
            report["console"] = cdp.console
            report["console_failures"] = cdp.console_failures
            report["page_failures"] = cdp.page_failures
            report["network_failures"] = cdp.network_failures
            report["api_requests"] = [
                response for response in cdp.responses
                if urllib.parse.urlsplit(response["url"]).path.startswith("/api/")
            ]
            cdp.close()
        if report_path is not None:
            report_path = Path(report_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if error is not None:
        raise WalkthroughFailure(str(error), report) from error
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "13f.sqlite")
    parser.add_argument("--chromium", help="Chromium executable (or set CHROMIUM)")
    parser.add_argument("--report", type=Path, help="Optional JSON report destination")
    args = parser.parse_args()
    try:
        report = run_walkthrough(args.database, chromium=args.chromium, report_path=args.report)
    except WalkthroughFailure as exc:
        print(f"Chromium walkthrough FAILED: {exc}", file=sys.stderr)
        if exc.report:
            print(json.dumps(exc.report, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    print(f"Chromium walkthrough passed: {len(report['steps'])} steps, "
          f"{len(report['api_requests'])} API responses, no recorded failures")
    for step in report["steps"]:
        print(f"  {step['name']}: {step['seconds']:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
