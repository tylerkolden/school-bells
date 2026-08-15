"use strict";

const countdown = document.querySelector("[data-target]");
if (countdown) {
  const tick = () => {
    const seconds = Math.max(0, Math.floor((new Date(countdown.dataset.target) - new Date()) / 1000));
    countdown.textContent = `Next · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  tick();
  window.setInterval(tick, 1000);
}

const scheduleName = document.querySelector("#schedule-name");
const scheduleAction = document.querySelector("#action-schedule");
if (scheduleName && scheduleAction) {
  scheduleName.addEventListener("change", () => {
    if (scheduleName.value) scheduleAction.checked = true;
  });
}

const noBellReason = document.querySelector("#reason");
const noBellAction = document.querySelector("#action-no-bells");
if (noBellReason && noBellAction) {
  noBellReason.addEventListener("input", () => {
    if (noBellReason.value.trim()) noBellAction.checked = true;
  });
}

const autoRefresh = document.querySelector("[data-auto-refresh]");
if (autoRefresh) {
  const refreshWhenReachable = async () => {
    try {
      const response = await window.fetch("/updates", { cache: "no-store" });
      if (response.ok) {
        window.location.replace("/updates");
        return;
      }
    } catch (_error) {
      // The service is expected to be briefly unreachable while switching releases.
    }
    window.setTimeout(refreshWhenReachable, 3000);
  };
  window.setTimeout(refreshWhenReachable, 2000);
}
