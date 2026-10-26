"""The chief's answer sheet: questions, Done, attachments and dictation."""

from __future__ import annotations

import json as _json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface
from tests.e2e.fixtures.ui import HeldUploads
from tests.e2e.fixtures.waits import stable_eval, stable_read

from tests.e2e.board._chief_support import (
    _CHIEF_UPLOAD,
    _PNG_1x1,
    _QUESTIONS,
    _RECORDER_MOCK,
    _board_payload,
    _capture_chief_input,
    _mock_board,
    _mock_chief_upload,
    _mock_ensure,
    _open_board,
    _paste_image,
    _plan_with,
    _route_plan_from,
    _route_voice,
)


pytestmark = pytest.mark.smoke


def _assert_answer_sheet_questions(dialog, blocks):
    """Assert the sheet shows each question as specified; return first, second, the first's options and Anything else."""
    first = blocks.nth(0)
    expect(first.locator(".chief-answer-repo")).to_have_text("fleet-config")
    expect(first.locator("a.chief-answer-ref")).to_have_attribute(
        "href", "https://github.com/octo/fleet-config/issues/959")
    expect(first.locator(".tr-ask-question")).to_have_text("Approve the four plans?")
    expect(first.locator(".chief-answer-detail")).to_have_text("Each plan ships as its own PR.")
    # The recommended option says so itself: no separate block, no star.
    expect(first.locator(".chief-answer-rec")).to_have_count(0)
    expect(dialog.locator('use[href="#i-star"]')).to_have_count(0)
    options = first.locator(".tr-ask-opt")
    expect(options).to_have_count(3)
    expect(options.nth(0)).to_contain_text("Yes, all four (Recommended)")
    expect(options.nth(0)).to_contain_text("Ship them in order")
    expect(first.locator('.tr-ask-opt[aria-pressed="true"]')).to_have_count(0)

    second = blocks.nth(1)
    expect(second.locator(".chief-answer-ref")).to_have_count(0)
    expect(second.get_by_text("Pick any number")).to_be_visible()
    # A recommendation no option carries stays, as one quiet line of text.
    rec = second.locator(".chief-answer-rec")
    expect(rec).to_have_text("Recommended: Mon and Wed suit the gym.")
    expect(rec).to_have_class(re.compile(r"\btr-ask-hint\b"))
    expect(rec.locator("svg")).to_have_count(0)

    third = blocks.nth(2)
    expect(third.locator(".tr-ask-question")).to_have_text("Remember the last tab?")
    expect(third.locator(".tr-ask-opt")).to_have_count(0)
    expect(third.locator("a.chief-answer-ref")).to_have_attribute(
        "href", "https://github.com/octo/app-launcher/issues/1131")

    expect(dialog.locator(".tr-ask-input")).to_have_count(3)  # Other on every item
    # Dictation is off here (voice-transcriber unconfigured): no Other mic (#1450).
    expect(dialog.locator(".tr-ask-mic")).to_have_count(0)
    expect(third.locator(".tr-ask-input")).to_have_attribute("placeholder", "Your answer")
    # Anything else is the shared composer Chat and Terminal mount: its mic
    # and image controls, but not its Send (Done is the one send) or keys.
    also = dialog.locator("#chiefAnswersAlso")
    expect(also).to_have_class(re.compile(r"\bcomposer\b"))
    expect(also.locator(".composer-input")).to_be_visible()
    expect(also.locator(".composer-mic")).to_be_visible()
    expect(also.locator(".composer-image")).to_be_visible()
    expect(also.locator(".composer-send")).to_be_hidden()
    expect(also.locator(".composer-keys")).to_be_hidden()
    return first, second, options, also


def _assert_sheet_fits_viewport(page: Page, dialog) -> None:
    """Assert the sheet is full-screen at phone width and a centred dialog on desktop."""
    viewport = page.viewport_size
    box = dialog.bounding_box()
    assert box, "answer sheet not laid out"
    if viewport["width"] <= 520:
        assert abs(box["width"] - viewport["width"]) < 2 and box["height"] >= viewport["height"] - 2, (
            f"phone sheet should be full-screen: {box} vs {viewport}")
    else:
        assert box["width"] <= 440, f"desktop sheet should stay a centred dialog: {box}"


def _assert_chief_plan_rows_marked_answered(page: Page) -> None:
    """Assert the plan marks the two answered questions and leaves the skipped one."""
    rows = page.locator("#boardChiefPlan li.board-plan-waiting")
    expect(rows.nth(0)).to_contain_text("answered, waiting for the chief")
    expect(rows.nth(1)).to_contain_text("answered, waiting for the chief")
    expect(rows.nth(2)).not_to_contain_text("answered")


@pytest.mark.iphone
def test_answer_sheet_renders_questions_and_done_sends_one_message(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1295: the sheet shows each question in file order (repo, linked ref,
    question, detail, the options with the recommended one labelled but not
    picked, Other on every item, an old text-only item as a free-text
    question) plus Anything else. The recommendation is no card and no star:
    the labelled option carries it, and an item whose recommendation marks
    no option keeps it as one quiet line. Anything else is the shared
    composer (mic + image, no Send of its own), and a pasted image uploads to
    the chief's session and rides the message as its path, as a Chat send
    carries it. Done sends exactly one message, composed from every answer,
    through the chat bar's ensure → input path; the sheet closes, a toast
    says so, and the card marks what this viewer answered."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    ensured: dict = {}
    _mock_ensure(authed_page, ensured)
    posts = _capture_chief_input(authed_page)
    uploads = _mock_chief_upload(authed_page)
    _route_voice(authed_page, False)
    with authed_page.expect_response(re.compile(r".*/api/status$")):
        _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    blocks = dialog.locator(".chief-answer")
    expect(blocks).to_have_count(3)
    surface(authed_page, "board.answer-sheet")

    first, second, options, also = _assert_answer_sheet_questions(dialog, blocks)

    # Nothing answered yet: Done waits, and says why.
    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_disabled()
    expect(dialog.locator("#chiefAnswersNote")).to_contain_text("Pick or type an answer first")

    # Single-select: typing Other then tapping an option keeps only the pick.
    first.locator(".tr-ask-input").fill("maybe later")
    options.nth(0).click()
    expect(options.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(first.locator(".tr-ask-input")).to_have_value("")
    # Multi-select: any number of picks, plus Other.
    second.locator(".tr-ask-opt").nth(0).click()
    second.locator(".tr-ask-opt").nth(1).click()
    second.locator(".tr-ask-input").fill("not   Friday")
    # The third is skipped.
    also_text = also.locator(".composer-input")
    also_text.fill("ship it tonight")
    expect(done).to_be_enabled()
    # A pasted image uploads to the chief's session, and its path lands in
    # the box as its own paragraph, exactly as the Chat composer appends it.
    assert _paste_image(authed_page, "#chiefAnswersAlso .composer-input")
    expect(also_text).to_have_value("ship it tonight\n\n" + _CHIEF_UPLOAD)
    assert len(uploads) == 1 and "inline=1" in uploads[0], uploads

    _assert_sheet_fits_viewport(authed_page, dialog)

    done.click()
    expect(authed_page.locator("#toast")).to_contain_text("Sent to chief")
    expect(dialog).to_be_hidden()
    assert ensured.get("body", {}).get("fresh") is not True, "answers must never restart the chief"
    # #1351: answers must keep the live chief that asked.
    assert ensured["body"].get("restart") is not True, "answers must keep a live chief"
    assert posts == [{
        "data": (
            "Answers from the Board (2 of 3):\n"
            "1. [fleet-config#959] Approve the four plans? → Yes, all four (recommended) {id: q-plans}\n"
            "2. [life-os] Which days work? → Mon, Wed; Other: not Friday {id: q-days}\n"
            "Skipped: 3\n"
            "Also: ship it tonight\n\n" + _CHIEF_UPLOAD
        ),
        "submit": True,
    }], posts

    _assert_chief_plan_rows_marked_answered(authed_page)


def test_answer_sheet_with_no_chief_disables_done_and_says_why_an_attachment_failed(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """The answer sheet with no chief running (#1515 merged two tests that
    shared this boot and sheet open; no tap or layout is measured, so it runs
    on Chromium only).

    #1295: with no chief running there is nobody to send to — Done is
    disabled with a plain note, answers typed or not, and nothing is sent.

    #1354: with no chief there is no session to store an attachment on; the
    summary toast says so instead of a silent nothing."""
    _mock_board(authed_page, _board_payload(with_chief=False))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    posts = _capture_chief_input(authed_page)
    _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    dialog.locator(".chief-answer").nth(2).locator(".tr-ask-input").fill("yes")
    expect(dialog.locator("#chiefAnswersDone")).to_be_disabled()
    expect(dialog.locator("#chiefAnswersNote")).to_have_text("Chief not running.")

    also = dialog.locator("#chiefAnswersAlso")
    also.locator(".composer-attach-input").set_input_files(files=[
        {"name": "e2e-stub-a.png", "mimeType": "image/png", "buffer": _PNG_1x1},
    ])
    expect(authed_page.locator("#toast")).to_have_text(
        "Uploaded 0 of 1 — 1 failed: e2e-stub-a.png (the chief is not running)")
    expect(also.locator(".composer-input")).to_have_value("")

    dialog.locator("#chiefAnswersClose").click()
    expect(dialog).to_be_hidden()
    assert posts == []


@pytest.mark.iphone
def test_answer_sheet_attach_shows_progress_and_names_a_failure(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1354: the answer sheet's field composer is the same composer.js, so a
    multi-file pick there shows "Uploading N of M" too, and a failed file is
    named in the one summary toast."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    held = HeldUploads(authed_page, re.compile(r".*/api/claude-code/sessions/s-chief/image(?:\?.*)?$"))
    _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    also = dialog.locator("#chiefAnswersAlso")
    also.locator(".composer-attach-input").set_input_files(files=[
        {"name": "e2e-stub-a.png", "mimeType": "image/png", "buffer": _PNG_1x1},
        {"name": "e2e-stub-b.png", "mimeType": "image/png", "buffer": _PNG_1x1},
    ])
    held.wait_for(1)
    expect(also.locator(".composer-upload-status")).to_have_text("Uploading 1 of 2 · e2e-stub-a.png")
    expect(also.locator(".composer-image .composer-upload-count")).to_have_text("1/2")
    held.ok(0, _CHIEF_UPLOAD)
    held.wait_for(2)
    held.fail(1, "file exceeds 12 MB")

    expect(also.locator(".composer-upload-status")).to_be_hidden()
    expect(authed_page.locator("#toast")).to_have_text(
        "Uploaded 1 of 2 — 1 failed: e2e-stub-b.png (file exceeds 12 MB)")
    expect(also.locator(".composer-input")).to_have_value(_CHIEF_UPLOAD)


@pytest.mark.iphone
def test_answer_sheet_busy_image_tap_cancels_the_upload(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1413: the chief's composer cancels a hung upload as the session
    composer does — a tap on the busy image button aborts it and drops the
    rest of the pick, and the next pick reaches the chief without a reload."""
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    held = HeldUploads(authed_page, re.compile(r".*/api/claude-code/sessions/s-chief/image(?:\?.*)?$"))
    _open_board(authed_page, base_url)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    also = authed_page.locator("#chiefAnswersDialog #chiefAnswersAlso")
    also.locator(".composer-attach-input").set_input_files(files=[
        {"name": "e2e-stub-a.png", "mimeType": "image/png", "buffer": _PNG_1x1},
        {"name": "e2e-stub-b.png", "mimeType": "image/png", "buffer": _PNG_1x1},
    ])
    held.wait_for(1)
    image = also.locator(".composer-image")
    expect(image).to_have_attribute("aria-label", "Cancel upload")

    image.click()

    expect(authed_page.locator("#toast")).to_have_text("Upload cancelled")
    expect(image).not_to_have_attribute("aria-busy", "true")
    expect(also.locator(".composer-upload-status")).to_be_hidden()
    expect(also.locator(".composer-input")).to_have_value("")
    expect(authed_page.locator("#chiefAnswersDialog")).to_be_visible()
    assert held.count == 1, "the dropped file must never be requested"

    also.locator(".composer-attach-input").set_input_files(files=[
        {"name": "e2e-stub-c.png", "mimeType": "image/png", "buffer": _PNG_1x1},
    ])
    held.wait_for(2)
    held.ok(1, _CHIEF_UPLOAD)
    expect(also.locator(".composer-input")).to_have_value(_CHIEF_UPLOAD)


@pytest.mark.iphone
def test_answer_sheet_other_fields_dictate_into_the_draft(
    authed_page: Page, base_url: str, tmp_path
) -> None:
    """#1450: with dictation available every Other field has its own mic (the
    chat bar's recorder, stubbed), at the 44px floor and inside the sheet at
    phone width. A take lands in that item's field for review, drops a
    single-select pick as typing does, can be edited, and rides Done as
    "Other: ..." exactly like typed text. Tapping an option still clears Other."""
    authed_page.add_init_script(_RECORDER_MOCK)

    _route_voice(authed_page, True)
    authed_page.route(
        re.compile(r".*/api/transcribe/sessions$"),
        lambda route: route.fulfill(status=503, body="nope"),
    )
    authed_page.route(
        re.compile(r".*/api/transcribe$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"transcript": "dictated reply", "language": "en"}),
        ),
    )
    _mock_board(authed_page, _board_payload(with_chief=True))
    _route_plan_from(authed_page, {"plan": _plan_with(tmp_path, _QUESTIONS)})
    _mock_ensure(authed_page, {})
    posts = _capture_chief_input(authed_page)
    _open_board(authed_page, base_url)
    # The sheet reads dictation availability when it opens: wait for the
    # status that turns it on to have landed. The predicate must be sync:
    # wait_for_function passes on an async one's Promise at once, which let
    # the sheet open before the status under load (0 mics).
    authed_page.evaluate("async () => { window.__e2eVoice = await import('/static/voice.js'); }")
    authed_page.wait_for_function(
        "() => window.__e2eVoice.voiceDictationAvailable()", timeout=15_000)

    authed_page.locator("#boardChiefPlan .board-plan-answer").click()
    dialog = authed_page.locator("#chiefAnswersDialog")
    expect(dialog).to_be_visible()
    blocks = dialog.locator(".chief-answer")
    expect(blocks).to_have_count(3)
    mics = dialog.locator(".tr-ask-mic")
    expect(mics).to_have_count(3)  # one beside every Other field

    for i in range(3):
        box = stable_read(mics.nth(i).bounding_box)
        assert box and box["width"] >= 44 and box["height"] >= 44, f"mic {i} under 44px: {box}"
    overflow = stable_eval(
        dialog, "el => el.scrollWidth - el.clientWidth")
    assert overflow <= 0, f"the sheet scrolls sideways by {overflow}px"
    for i in range(3):
        field = blocks.nth(i).locator(".tr-ask-input")
        fbox = stable_read(field.bounding_box)
        mbox = stable_read(mics.nth(i).bounding_box)
        assert fbox and mbox and fbox["x"] + fbox["width"] <= mbox["x"] + 1, (
            f"item {i}: the field and its mic overlap: {fbox} {mbox}")

    # Single-select: a pick, then a take into Other, which replaces the pick.
    first = blocks.nth(0)
    first.locator(".tr-ask-opt").nth(0).click()
    mics.nth(0).click()
    expect(mics.nth(0)).to_have_class(re.compile(r"\brecording\b"))
    mics.nth(0).click()  # stop
    expect(first.locator(".tr-ask-input")).to_have_value(re.compile("dictated reply"))
    expect(first.locator('.tr-ask-opt[aria-pressed="true"]')).to_have_count(0)
    # Tapping an option clears Other, as it does for typed text.
    first.locator(".tr-ask-opt").nth(1).click()
    expect(first.locator(".tr-ask-input")).to_have_value("")

    # Multi-select: a pick and a take coexist; the transcript is editable.
    second = blocks.nth(1)
    second.locator(".tr-ask-opt").nth(0).click()
    mics.nth(1).click()
    mics.nth(1).click()
    other = second.locator(".tr-ask-input")
    expect(other).to_have_value(re.compile("dictated reply"))
    expect(second.locator('.tr-ask-opt[aria-pressed="true"]')).to_have_count(1)
    other.fill("dictated reply, edited")

    done = dialog.locator("#chiefAnswersDone")
    expect(done).to_be_enabled()
    done.click()
    expect(authed_page.locator("#toast")).to_contain_text("Sent to chief")
    assert posts == [{
        "data": (
            "Answers from the Board (2 of 3):\n"
            "1. [fleet-config#959] Approve the four plans? → Only the first {id: q-plans}\n"
            "2. [life-os] Which days work? → Mon; Other: dictated reply, edited {id: q-days}\n"
            "Skipped: 3"
        ),
        "submit": True,
    }], posts
