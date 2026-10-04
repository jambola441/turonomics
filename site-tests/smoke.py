"""Render the fleet view in a real browser and assert it still works.

Every UI change in this project has been checked by loading the page, clicking
through it and looking — and nothing in CI did any of that, because the
workflows are path-filtered to ``api/**`` and ``extension/**``. The whole web
UI had no automated coverage at all.

This is the cheap version of what was being done by hand: serve the page
against a stub API, drive it, and fail on a console error or a section that
did not render. It will not catch a sentence that reads badly — only a person
looking at it will — but it catches the class of thing that makes the page
blank, which is worse and easier to miss.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8910"
SITE = "http://127.0.0.1:8909"


def _wait_for(url: str, *, tries: int = 40) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:  # noqa: BLE001 - still starting
            time.sleep(0.25)
    raise RuntimeError(f"{url} never came up")


def main() -> int:
    stub = subprocess.Popen([sys.executable, str(Path(__file__).parent / "stub_api.py")])
    site = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8909", "--bind", "127.0.0.1"],
        cwd=ROOT / "docs",
    )
    failures: list[str] = []
    try:
        _wait_for(f"{API}/api/fleet")
        _wait_for(f"{SITE}/fleet/")

        with sync_playwright() as pw:
            # CI installs its own chromium; this container ships one at a
            # fixed path and no downloader. One env var covers both.
            executable = os.environ.get("PLAYWRIGHT_CHROMIUM") or None
            browser = pw.chromium.launch(executable_path=executable)
            page = browser.new_page(viewport={"width": 390, "height": 1200})

            # Hermetic: nothing but the stub and the site. Web fonts and map
            # tiles are somebody else's uptime, and a test that fails when
            # Google is slow is a test people learn to ignore.
            page.route(
                "**/*",
                lambda route: route.continue_()
                if "127.0.0.1" in route.request.url
                else route.abort(),
            )

            errors: list[str] = []
            # An uncaught exception is always ours. A console message about a
            # resource is not: the blocked externals above produce one each,
            # and so would a flaky CDN. Conflating the two is how a smoke test
            # becomes noise and stops being read.
            page.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
            page.on(
                "console",
                lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" and "Failed to load resource" not in m.text
                else None,
            )
            page.goto(f"{SITE}/fleet/?api={API}", wait_until="domcontentloaded")
            page.wait_for_timeout(800)

            def check(name: str, condition: bool) -> None:
                print(f"  {'ok  ' if condition else 'FAIL'}  {name}")
                if not condition:
                    failures.append(name)

            check("the error banner is not showing", page.locator("#err").is_hidden())
            check("every vehicle has a chip", page.locator(".chip").count() == 3)
            check("the selected car has a name", page.locator(".name").count() >= 1)
            check("tasks render", page.locator(".task").count() == 2)
            check("a guest note renders", page.locator(".note-guest").count() == 1)
            check("the alerts button renders", page.locator("#alerts").count() == 1)

            # Ticking a task must re-render from the response, not silently fail.
            page.locator(".task").first.click()
            page.wait_for_timeout(600)
            check("ticking a task does not break the page", page.locator("#err").is_hidden())

            # "Where to?" is the one section fetched on demand.
            page.locator(".chip").first.click()
            page.wait_for_timeout(400)
            if page.locator(".where-btn").count():
                page.locator(".where-btn").click()
                page.wait_for_timeout(600)
                check("spot suggestions render", page.locator(".spot").count() == 2)
                check(
                    "the 'no space guarantee' caveat is shown",
                    page.locator(".spots .caveat").count() >= 1,
                )

            # Switching cars must not throw on the one with no tracker.
            for index in range(page.locator(".chip").count()):
                page.locator(".chip").nth(index).click()
                page.wait_for_timeout(250)
            check("no uncaught errors anywhere in that journey", not errors)

            if errors:
                print("\nconsole/page errors:")
                for message in errors[:10]:
                    print(f"  {message}")
            browser.close()
    finally:
        stub.terminate()
        site.terminate()

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
