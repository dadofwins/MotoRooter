"""Ask the real Maps API what it actually does.

Every map behaviour in this project is tested against fakes we wrote, and a fake confirms the
assumptions it encodes. This is the only thing that does not: it loads the live API with the
browser key from `frontend/.env.local`, exercises the beliefs the canvas rests on, and posts the
answers back. **It cannot be a test** — no test in this project touches a live API — so it is a
tool you run by hand and a record you read.

    cd frontend && python3 scripts/maps-probe/run.py

Three things learned the hard way, all of them about the harness rather than the API:

- **`--disable-gpu` makes every answer worthless.** A Map ID means a vector map, which needs
  WebGL; without it the map never reaches `idle` and every downstream check reports a failure
  that is really "the map never rendered". The first run said `fitBounds` did not fire
  `zoom_changed` and that markers received nothing. Both were false.
- **`--virtual-time-budget` breaks timing.** `performance.now()` is virtual under it, so a
  measurement of how long an operation takes reports 0.000 ms and looks like a pass.
- **`--dump-dom` returns when the page loads**, seconds before the answers exist. The page posts
  its results back here instead, so nothing depends on guessing when to look.

Each check runs in its own `try`. A loud failure must not take a quiet answer down with it: the
one that matters most is `fitBounds -> zoom_changed`, because when it is wrong the map looks like
clustering working hard rather than clustering never running.
"""

from __future__ import annotations

import http.server
import json
import pathlib
import socketserver
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).parent
FRONTEND = HERE.parent.parent
PORT = 8199
DEBUG_PORT = 9223
CHROME = "google-chrome-stable"


def secrets() -> tuple[str, str]:
    """The browser key and Map ID, from the gitignored env file. Never committed, never printed."""
    env_path = FRONTEND / ".env.local"
    if not env_path.exists():
        sys.exit(f"no {env_path}: this needs the browser key a real map load requires")
    values = {}
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip('"')
    key = values.get("VITE_GOOGLE_MAPS_BROWSER_KEY", "")
    map_id = values.get("VITE_GOOGLE_MAPS_MAP_ID", "")
    if not key or not map_id:
        sys.exit("VITE_GOOGLE_MAPS_BROWSER_KEY and VITE_GOOGLE_MAPS_MAP_ID must both be set")
    return key, map_id


def main() -> None:
    key, map_id = secrets()
    work = pathlib.Path("/tmp/motorooter-maps-probe")
    work.mkdir(exist_ok=True)
    result = work / "result.txt"
    result.unlink(missing_ok=True)

    page = (HERE / "probe.template.html").read_text()
    (work / "probe.html").write_text(
        page.replace("__BROWSER_KEY__", key).replace("__MAP_ID__", map_id)
    )

    ready = work / "ready.json"
    ready.unlink(missing_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(work), **kw)

        def do_POST(self):  # noqa: N802 -- the base class spells it this way
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            # `/ready` hands out where to click; `/report` is the answer. The page asks to be
            # driven because the gesture has to be *trusted* input, which only the protocol can
            # produce — a synthesised MouseEvent reaches no Map-level listener at all.
            (ready if self.path == "/ready" else result).write_bytes(body)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # Real WebGL and real time. See the note above about what each missing flag costs.
        browser = subprocess.Popen(  # noqa: S603
            [
                CHROME,
                "--headless=new",
                "--enable-unsafe-swiftshader",
                "--window-size=1100,800",
                "--no-first-run",
                f"--user-data-dir={work / 'chrome'}",
                f"--remote-debugging-port={DEBUG_PORT}",
                f"http://127.0.0.1:{PORT}/probe.html",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # The page asks for as many gestures as it needs; each request is consumed here so
            # the next one is distinguishable from the last.
            for _ in range(90):
                if ready.exists():
                    at = json.loads(ready.read_text())
                    ready.unlink(missing_ok=True)
                    delivered = subprocess.run(  # noqa: S603
                        [
                            "node",
                            str(HERE / "trusted-input.mjs"),
                            str(DEBUG_PORT),
                            str(at["x"]),
                            str(at["y"]),
                            str(at.get("gesture", "drag")),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if delivered.returncode != 0:
                        print(f"gesture not delivered: {delivered.stderr.strip()}", file=sys.stderr)
                if result.exists():
                    break
                time.sleep(1)
        finally:
            browser.terminate()
        server.shutdown()

    if not result.exists():
        sys.exit("no answer: the page never reported, so nothing here is known either way")
    print(result.read_text())


if __name__ == "__main__":
    main()
