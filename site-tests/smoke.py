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

import json
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

            # The run sheet collapses to its handle so the map can have the
            # screen. Hidden, not removed: the vehicle panel must survive a
            # round trip, and the button has to say what state it is in.
            # The journey above ends on the car with no tracker, which has no
            # tasks to find afterwards; start from one that does.
            page.locator(".chip").first.click()
            page.wait_for_timeout(250)
            sheet_height = lambda: page.locator("#sheet").bounding_box()["height"]
            open_height = sheet_height()
            page.locator("#grab").click()
            page.wait_for_timeout(300)
            check(
                "collapsing hides the vehicle panel",
                page.locator("#sheet-body").is_hidden()
                and page.locator("#grab").get_attribute("aria-expanded") == "false"
                and sheet_height() < open_height / 4,
            )
            page.locator("#grab").click()
            page.wait_for_timeout(300)
            check(
                "expanding brings it back intact",
                page.locator("#sheet-body").is_visible()
                and page.locator("#grab").get_attribute("aria-expanded") == "true"
                and page.locator(".task").count() >= 1,
            )

            # ---- the tolls page -------------------------------------------
            # Its own page on purpose: reconciling a statement is a monthly
            # back-office sit-down, not the thing the run sheet is for.
            tolls = stub_api.TOLLS
            total = sum(t["amount_cents"] for t in tolls)
            owed = sum(t["amount_cents"] for t in tolls if not t["recovered_at"])
            loose = sum(t["amount_cents"] for t in tolls
                        if not t["trip_id"] and not t["outside_label"])
            outside = sum(t["amount_cents"] for t in tolls
                          if not t["trip_id"] and t["outside_label"])

            # One handler for both kinds of dialog the page raises, routed by
            # type. Two handlers do not work: Playwright calls every one that
            # is registered and the first to act wins, so a blanket "accept"
            # for the token prompt silently confirmed the delete as well — and
            # the test for cancelling a delete passed a page that had deleted.
            confirm_answers: list[bool] = []

            def dialog(d) -> None:
                if d.type == "prompt":
                    d.accept(stub_api.TOLLS_TOKEN)   # the tolls token
                elif confirm_answers.pop(0) if confirm_answers else False:
                    d.accept()
                else:
                    d.dismiss()

            page.on("dialog", dialog)

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

            # A crossing billed to a guest because the car came back late is
            # an inference about whose money it is, so it is shown as one
            # rather than appearing as an ordinary attribution.
            over = page.locator(".t-over")
            check("a late return is billed but labelled",
                  over.count() == 1 and "late return" in (over.first.text_content() or ""))
            check("and says how far over it ran",
                  "34m" in (over.first.text_content() or ""))

            # Off-platform rentals: the place an unattributed crossing leads.
            check("recorded rentals are listed",
                  page.locator(".rental").count() == len(stub_api.TRIPS))
            check("a rental that caught no tolls is called out",
                  page.locator(".r-tolls.none").count() == 1)

            page.locator("#r-car").fill("Jerry")
            page.locator("#r-from").fill("2026-10-04T13:00")
            page.locator("#r-to").fill("2026-10-04T18:00")
            page.locator("#r-save").click()
            page.wait_for_timeout(700)
            check("recording a rental reports what it attributed",
                  "2 tolls now attributed" in (page.locator("#r-result").text_content() or ""))

            # Removing one is confirmed, like removing a toll: it unbills a
            # guest rather than just tidying a list.
            rentals_before = page.locator(".rental").count()
            confirm_answers.append(False)
            page.locator(".rental").first.locator(".drop").click()
            page.wait_for_timeout(400)
            check("cancelling keeps the rental",
                  page.locator(".rental").count() == rentals_before)
            confirm_answers.append(True)
            page.locator(".rental").first.locator(".drop").click()
            page.wait_for_timeout(700)
            check("confirming removes it",
                  page.locator(".rental").count() == rentals_before - 1)

            # An unattributed crossing shows how far it sits from the nearest
            # rental, which is the difference between "the guest was still
            # driving" and "that was one of mine".
            check("the nearest rental is offered as a hint",
                  page.locator(".t-near").count() == 1)
            hint = page.locator(".t-near").first.text_content() or ""
            check("the hint says how long and which way",
                  "20m" in hint and "after" in hint and "Dylan" in hint)
            check("the hint is not phrased as an attribution",
                  "probably" not in hint.lower() and "owes" not in hint.lower())

            # A crossing on a car outside the fleet is labelled, kept out of the
            # chase-this figure, and still counted in what the account paid.
            note = page.locator("#outside-note")
            check("money outside the fleet is reported apart",
                  note.is_visible() and _money(outside) in (note.text_content() or ""))
            check("and named, so it is recognisable next month",
                  "Mum's car" in (note.text_content() or ""))
            check("an outside crossing is not shown as a gap",
                  page.locator(".t-who.outside").count() == 1
                  and page.locator(".t-who.loose").count()
                      == sum(1 for t in tolls
                             if not t["vehicle_nickname"] and not t["outside_label"]))

            # $2.01 is the amount that int(2.01 * 100) turns into 200. It cost
            # a cent on 137 of the first 2000 amounts when the importer did
            # that, and it would do the same here if the page divided floats.
            ledger_text = page.locator(".ledger").text_content() or ""
            check("a $2.01 crossing renders as $2.01", "$2.01" in ledger_text)
            check("four figures get a thousands separator", "$1,234.56" in ledger_text)

            # The whole reason this card exists: a tag nobody has bound bills
            # to nobody, and the operator cannot fix that from a number they
            # cannot read in full.
            # A labelled tag is not an unbound one: it belongs to a car that is
            # not in this fleet and never will be, so it must not appear here.
            expected_tags = sorted({t["transponder_id"] for t in tolls
                                    if t["transponder_id"] and not t["vehicle_nickname"]
                                    and not t["outside_label"]})
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

            # Removing a crossing is confirmed, unlike the tick: a mis-tap
            # loses money the operator was owed.
            before = page.locator(".toll").count()
            confirm_answers.append(False)
            page.locator(".toll").first.locator(".drop").click()
            page.wait_for_timeout(500)
            check("cancelling the confirm keeps the crossing",
                  page.locator(".toll").count() == before)

            confirm_answers.append(True)
            page.locator(".toll").first.locator(".drop").click()
            page.wait_for_timeout(700)
            check("confirming removes it", page.locator(".toll").count() == before - 1)
            check("and the charged total drops with it",
                  page.locator("#s-total").text_content() != _money(total))

            page.locator("#rematch").click()
            page.wait_for_timeout(600)
            check("re-matching says when nothing changed",
                  "Nothing changed" in (page.locator("#import-result").text_content() or ""))

            # Nothing above would have worked without the token, but assert it
            # from the server's side too: the page could have been handed a 401
            # on each write and shown a stale figure.
            seen = json.loads(urllib.request.urlopen(f"{API}/seen-auth").read())["seen"]
            # Every write above: recording a rental, removing one, the tick,
            # the untick, the toll delete, the upload, the re-match. An exact
            # count rather than a floor, so an extra write nobody meant to add
            # shows up here — and the two cancelled confirms must not appear at
            # all, because a dismissed confirm must not reach the API.
            check(f"the page sent the token on every write (saw {len(seen)})",
                  seen == [stub_api.TOLLS_TOKEN] * 7)

            # And a rejected token must be forgotten, or the page asks nobody
            # and fails the same way forever.
            page.evaluate("localStorage.setItem('turonomics.tolls.token', 'wrong')")
            page.once("dialog", lambda d: d.dismiss())
            page.locator("#rematch").click()
            page.wait_for_timeout(600)
            check("a rejected token is shown as rejected",
                  "rejected" in (page.locator("#err").text_content() or "").lower())
            check("a rejected token is not kept",
                  page.evaluate("localStorage.getItem('turonomics.tolls.token')") in (None, ""))

            check("no uncaught errors on the tolls page", not errors)

            # ---- the invoices page ----------------------------------------
            # The view that turns the ledger into money: what to bill whom, and
            # how long is left to ask. The grouping is the point, so that is
            # what gets checked.
            invoices = stub_api.INVOICES
            page.goto(f"{SITE}/invoices/?api={API}", wait_until="domcontentloaded")
            page.wait_for_timeout(700)

            check("the invoices error banner is not showing", page.locator("#err").is_hidden())
            check("every rental to bill is listed",
                  page.locator(".inv").count() == len(invoices))
            check("the total to bill is the sum of the invoices",
                  page.locator("#s-total").text_content()
                  == _money(sum(i["total_cents"] for i in invoices)))

            # An expired invoice must be reported apart from collectable money:
            # Turo will not take it, and counting it as work to do would have
            # the operator chasing something that cannot be filed.
            expired = [i for i in invoices if i["expired"]]
            check("money past the window is reported apart",
                  page.locator("#s-gone").text_content()
                  == _money(sum(i["total_cents"] for i in expired)))
            check("and it is not counted as due soon",
                  page.locator("#s-urgent").text_content()
                  != page.locator("#s-total").text_content())

            headings = (page.locator("#groups").text_content() or "")
            check("the urgent ones get their own heading", "File these first" in headings)
            check("the expired ones get their own heading", "Past the window" in headings)
            check("off-platform rentals are separated",
                  "Off-platform" in headings and "no deadline" in headings)
            check("an expired invoice says how long ago it lapsed",
                  "5 days past the window" in headings)
            check("an urgent one says how long is left", "6 days left" in headings)

            # The soonest deadline has to be at the top, or the view does not
            # answer the question it exists for.
            first = page.locator(".inv").first.text_content() or ""
            check("the soonest deadline is first", "Samuel" in first)

            # Lines are collapsed until a rental is picked.
            check("lines start hidden", page.locator(".line").count() == 0)
            page.locator(".inv-head").first.click()
            page.wait_for_timeout(300)
            check("opening a rental shows its lines", page.locator(".line").count() >= 1)
            check("a late-return line says why it is on the bill",
                  page.locator(".line .late").count() == 1)
            check("and there is a link to file it on Turo",
                  "reservation/54958910" in (
                      page.locator(".acts a").first.get_attribute("href") or ""))

            before_count = page.locator(".inv").count()
            confirm_answers.append(False)
            page.locator(".acts .btn.go").first.click()
            page.wait_for_timeout(400)
            check("cancelling leaves the invoice alone",
                  page.locator(".inv").count() == before_count)
            confirm_answers.append(True)
            page.locator(".acts .btn.go").first.click()
            page.wait_for_timeout(700)
            check("marking it billed back removes it from the list",
                  page.locator(".inv").count() == before_count - 1)

            # Money Turo has already charged. Asking twice for what a guest has
            # paid is a dispute rather than income, so it is on the row.
            text = page.locator("#groups").text_content() or ""
            check("a rental charged a different amount is flagged",
                  "Turo already charged" in text and "check before asking again" in text)
            # A bundled invoice has to read as a bundle: its total alone looks
            # like a mystery, its lines explain it.
            check("the bundled invoice's toll line is quoted",
                  "its toll line was $15.55" in text)
            # Turo's own word for the section, and its own quantified labels.
            # The page used to say "that invoice:" and the labels used to be
            # invented, which is how a parser that read none of them went a
            # year unnoticed.
            check("the charges are listed the way the invoice words them",
                  "incidental charges: 7 tolls $15.55" in text
                  and "22 mi additional distance $40.00" in text)
            # The ledger: the second look at the same money.
            page.locator("#ledgerWrap > summary").click()
            # Lowercased: the state cells are uppercased by CSS, and
            # inner_text returns what is rendered rather than what is in the
            # DOM. Asserting on the source casing fails for a page that is
            # working perfectly.
            ledger = page.locator("#ledgerWrap").inner_text().lower()
            check("the ledger splits a rental into asked and not yet asked",
                  "$11.00" in ledger and "$6.79" in ledger and "partly billed" in ledger)
            check("and says what arrived after the first invoice",
                  "arrived after the first invoice" in ledger)
            check("a settled rental appears in the ledger though not in the list",
                  "settled" in ledger and "$9.79" in ledger)
            check("Turo's own charge is shown beside ours, not merged into it",
                  "$25.00" in ledger)
            check("the ledger totals are stated",
                  "tolls $27.58" in ledger and "to bill $11.00" in ledger)

            check("an invoice filed but unpaid is distinguished from one paid",
                  "filed and unpaid" in text)
            look = page.locator("#look")
            check("and the total needing a look is called out",
                  look.is_visible()
                  and _money(sum(i["total_cents"] for i in invoices
                                 if i["charged_but_different"]))
                      in (look.text_content() or ""))

            check("no uncaught errors on the invoices page", not errors)

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
