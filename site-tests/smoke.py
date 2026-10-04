"""Render the pages in a real browser and assert they still work.

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
import tempfile
import time
import urllib.request
from pathlib import Path

import stub_api
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8910"
SITE = "http://127.0.0.1:8909"


def _money(cents: int) -> str:
    """The same string the page should render, derived independently.

    Deliberately not the page's function: the thing worth checking is that
    integer cents and the rendered dollars agree, and a shared helper would
    make any disagreement invisible.
    """
    return f"${cents // 100:,}.{cents % 100:02d}"


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
                check("spot suggestions render", page.locator(".spot").count() == 3)
                check(
                    "two blocks of one street are told apart by their cross streets",
                    page.locator(".spot .s-between").count() == 3
                    # text_content, not inner_text: the cross streets are
                    # styled lowercase, and inner_text returns what the CSS
                    # renders rather than what the markup says. The first
                    # version of this check failed on a page that was correct.
                    and "PLAZA STREET" in (page.locator(".spots").text_content() or ""),
                )
                check(
                    "the 'no space guarantee' caveat is shown",
                    page.locator(".spots .caveat").count() >= 1,
                )

            # Switching cars must not throw on the one with no tracker.
            for index in range(page.locator(".chip").count()):
                page.locator(".chip").nth(index).click()
                page.wait_for_timeout(250)
            check("no uncaught errors anywhere in that journey", not errors)

            # ---- the tolls page -------------------------------------------
            # Its own page on purpose: reconciling a statement is a monthly
            # back-office sit-down, not the thing the run sheet is for.
            tolls = stub_api.TOLLS
            total = sum(t["amount_cents"] for t in tolls)
            owed = sum(t["amount_cents"] for t in tolls if not t["recovered_at"])
            loose = sum(t["amount_cents"] for t in tolls if not t["trip_id"])

            page.goto(f"{SITE}/tolls/?api={API}", wait_until="domcontentloaded")
            page.wait_for_timeout(700)

            check("the tolls error banner is not showing", page.locator("#err").is_hidden())
            check("every crossing is listed", page.locator(".toll").count() == len(tolls))
            check("the charged total is the sum of the cents",
                  page.locator("#s-total").text_content() == _money(total))
            check("what is still owed excludes what was billed back",
                  page.locator("#s-owed").text_content() == _money(owed))
            check("unattributed money is reported apart from recoverable money",
                  page.locator("#s-loose").text_content() == _money(loose))

            # $2.01 is the amount that int(2.01 * 100) turns into 200. It cost
            # a cent on 137 of the first 2000 amounts when the importer did
            # that, and it would do the same here if the page divided floats.
            ledger_text = page.locator(".ledger").text_content() or ""
            check("a $2.01 crossing renders as $2.01", "$2.01" in ledger_text)
            check("four figures get a thousands separator", "$1,234.56" in ledger_text)

            # The whole reason this card exists: a tag nobody has bound bills
            # to nobody, and the operator cannot fix that from a number they
            # cannot read in full.
            expected_tags = sorted({t["transponder_id"] for t in tolls
                                    if t["transponder_id"] and not t["vehicle_nickname"]})
            check("unbound transponders are called out",
                  page.locator("#unknown-wrap").is_visible()
                  and page.locator(".tag").count() == len(expected_tags))
            tag_text = page.locator("#unknown-tags").text_content() or ""
            check("each unbound tag is shown in full",
                  all(tag in tag_text for tag in expected_tags))
            env_text = page.locator("#unknown-env").text_content() or ""
            check("the env var line is ready to paste",
                  env_text.startswith("EZPASS_TAGS=")
                  and all(tag in env_text for tag in expected_tags))

            # Ticking must re-render from the response. The figure dropping by
            # exactly this row's amount is the part a stub serving a constant
            # could not distinguish.
            first_open = next(t for t in tolls if not t["recovered_at"])
            row = page.locator(".toll").filter(
                has=page.locator(f'.t-amt:text-is("{_money(first_open["amount_cents"])}")')
            ).first
            row.locator(".tick").click()
            page.wait_for_timeout(600)
            check("billing a toll back lowers what is owed",
                  page.locator("#s-owed").text_content()
                  == _money(owed - first_open["amount_cents"]))
            check("the row it was ticked on is shown as done",
                  page.locator(".toll.done").count() == 2)

            # And untick, so the stub is left as it was found and the undo
            # path is covered.
            row.locator(".tick").click()
            page.wait_for_timeout(600)
            check("unticking puts it back",
                  page.locator("#s-owed").text_content() == _money(owed))

            page.locator("#f-owed").click()
            page.wait_for_timeout(200)
            check("the 'to recover' filter hides what was billed back",
                  page.locator(".toll").count() == len(tolls) - 1)
            page.locator("#f-all").click()
            page.wait_for_timeout(200)

            statement = Path(tempfile.mkdtemp()) / "activity.csv"
            statement.write_text(
                '"Lane Txn ID","Tag/Plate #","Agency","Entry Plaza","Exit Plaza",'
                '"Class","Date","Exit Time","Amount"\n'
                '"1"," 99900000001","NYSTA","15","19","2L","12/29/2025",'
                '"05:13:32 PM","$-2.01"\n'
            )
            page.locator("#file").set_input_files(str(statement))
            page.wait_for_timeout(700)
            check("uploading a statement reports what it read",
                  "9 rows read" in (page.locator("#import-result").text_content() or ""))

            page.locator("#rematch").click()
            page.wait_for_timeout(600)
            check("re-matching says when nothing changed",
                  "Nothing changed" in (page.locator("#import-result").text_content() or ""))
            check("no uncaught errors on the tolls page", not errors)

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
