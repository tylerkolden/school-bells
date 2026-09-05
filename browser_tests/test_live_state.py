from playwright.sync_api import expect


def test_live_transitions_and_stale_connection(page, console):
    page.goto("http://testserver/")
    expect(page.locator("[data-readiness-label]")).to_have_text("Ready")
    expect(page.locator("[data-school-clock]")).to_contain_text("7:45")
    snapshot = console["snapshot"]
    snapshot["active_page"] = {"label": "A newly started page"}
    snapshot["pause"] = {"active": True, "reason": "Assembly", "until": "2027-02-02T09:00:00-07:00"}
    snapshot["schedule"] = "Mass Day"
    expect(page.locator("[data-active-page]")).to_be_visible(timeout=6000)
    expect(page.locator("[data-active-label]")).to_have_text("A newly started page")
    expect(page.locator("[data-resume-form]")).to_be_visible()
    expect(page.locator("[data-schedule-name]")).to_have_text("Mass Day")
    console["status"] = 500
    expect(page.locator("[data-readiness-label]")).to_have_text("Console disconnected", timeout=6000)
    expect(page.locator("[data-next-countdown]")).to_have_text("Awaiting live state")
    expect(page.locator("[data-readiness-card]")).to_have_class("operation-card readiness-card blocked")
    # Stop remains available: loss of browser connectivity is not proof audio stopped.
    expect(page.get_by_role("button", name="Stop active page")).to_be_enabled()
    console["status"] = 200
    snapshot["active_page"] = None
    snapshot["pause"]["active"] = False
    snapshot["config_hash"] = "changed-on-another-device"
    expect(page.locator("[data-active-page]")).to_be_hidden(timeout=6000)
    expect(page.locator("[data-resume-form]")).to_be_hidden()
    expect(page.locator("[data-config-changed]")).to_be_visible()
    expect(page.get_by_role("button", name="Pause bells", exact=True)).to_be_disabled()


def test_expired_session(page, console):
    page.goto("http://testserver/")
    console["status"] = 401
    expect(page.locator("[data-readiness-label]")).to_contain_text("Session expired", timeout=6000)
