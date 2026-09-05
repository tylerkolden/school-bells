import os
from pathlib import Path

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("width", [320, 360, 390, 768, 1024, 1440])
@pytest.mark.parametrize("console", ["admin", "operator"], indirect=True)
def test_today_actions_fit_and_states_stay_reachable(page, console, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("http://testserver/")
    expect(page.locator("[data-readiness-label]")).to_have_text("Ready")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    pause = page.locator(".pause-disclosure > summary")
    assert pause.bounding_box()["y"] + pause.bounding_box()["height"] < 900
    assert page.locator("header").bounding_box()["height"] < 180
    if os.environ.get("BELL_TEST_SCREENSHOTS") and width in (390, 1440):
        out = Path(os.environ["BELL_TEST_SCREENSHOTS"])
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / f"today-{width}.png"), full_page=True)
    console["snapshot"]["active_page"] = {"label": "Emergency drill"}
    expect(page.get_by_role("button", name="Stop active page")).to_be_visible(timeout=6000)
    assert page.get_by_role("button", name="Stop active page").bounding_box()["y"] < 900
    console["snapshot"]["pause"] = {"active": True, "reason": "Assembly", "until": "2027-02-02T09:00:00-07:00"}
    expect(page.locator("[data-resume-form]")).to_be_visible(timeout=6000)
    console["snapshot"]["kill_switch"] = True
    expect(page.locator("[data-kill-notice]")).to_be_visible(timeout=6000)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_calendar_and_schedule_are_readable(page, console, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("http://testserver/calendar")
    expect(page.locator(".selected-day-summary")).to_contain_text("Regular Day")
    assert page.locator(".selected-day-summary").bounding_box()["y"] < 600
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.goto("http://testserver/schedules")
    page.locator(".event-fields > summary").first.click()
    expect(page.locator('input[name="event_time"]').first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="+ Add event", exact=True).click()
    expect(page.locator("[data-event-row]").last.locator(".event-fields")).to_have_attribute("open", "")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.goto("http://testserver/manual")
    expect(page.locator("[data-manual-preview]")).to_have_attribute("src", "/sounds/angelus.wav")
    page.get_by_label("Sound", exact=True).select_option("class-bell.wav")
    expect(page.locator("[data-manual-preview]")).to_have_attribute("src", "/sounds/class-bell.wav")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_receiver_evidence_form_fits(page, console, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("http://testserver/commissioning")
    page.get_by_text("Record receiver checks", exact=True).first.click()
    expect(page.locator('input[name="receiver_id"]').first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator('select[name="emergency"]').first).to_have_value("not_tested")
