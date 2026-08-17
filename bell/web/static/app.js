"use strict";

const operations = document.querySelector("[data-operations-dashboard]");
if (operations) {
  let serverEpoch = new Date(operations.dataset.serverTime).getTime();
  let receivedEpoch = Date.now();
  let nextRefreshAfter = 0;

  const serverNow = () => new Date(serverEpoch + (Date.now() - receivedEpoch));
  const setText = (selector, value) => {
    const element = document.querySelector(selector);
    if (element) element.textContent = value;
  };
  const formatClock = (value) => value.toLocaleTimeString("en-US", {
    hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
  });
  const formatCountdown = (milliseconds) => {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    const minutes = Math.floor(seconds / 60);
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      return `in ${hours} hr ${minutes % 60} min`;
    }
    if (minutes > 0) return `in ${minutes} min ${seconds % 60} sec`;
    return `in ${seconds} sec`;
  };
  const renderUpcoming = (items) => {
    const list = document.querySelector("[data-upcoming-list]");
    if (!list) return;
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty-inline";
      empty.textContent = "No bell is scheduled in the next year.";
      list.append(empty);
      return;
    }
    items.forEach((item, index) => {
      const article = document.createElement("article");
      article.className = `event${index === 0 ? " next" : ""}`;
      const time = document.createElement("time");
      time.dateTime = item.time;
      time.textContent = item.display_time;
      const detail = document.createElement("div");
      detail.className = "event-detail";
      const strong = document.createElement("strong");
      strong.textContent = item.label;
      const small = document.createElement("small");
      small.textContent = `${item.display_day} · ${item.zone} · ${item.sound}`;
      detail.append(strong, small);
      article.append(time, detail);
      if (index === 0) {
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = "Next";
        article.append(pill);
      }
      list.append(article);
    });
  };
  const applySnapshot = (data) => {
    serverEpoch = new Date(data.server_time).getTime();
    receivedEpoch = Date.now();
    operations.dataset.nextTime = data.next_bell ? data.next_bell.time : "";
    setText("[data-school-date]", data.display_date);
    setText("[data-next-label]", data.next_bell ? data.next_bell.label : "No bell scheduled");
    setText("[data-next-absolute]", data.next_bell ? `${data.next_bell.display_day} · ${data.next_bell.display_time}` : "—");
    setText("[data-next-detail]", data.next_bell ? `${data.next_bell.zone} · ${data.next_bell.sound}` : "Review the calendar before the next school day.");
    setText("[data-readiness-label]", data.ready ? "Ready" : "Needs attention");
    setText("[data-readiness-detail]", data.ready ? "All required checks pass" : data.blocked_reasons.join(" · "));
    const readiness = document.querySelector("[data-readiness-card]");
    if (readiness) readiness.className = `operation-card readiness-card ${data.ready ? "ready" : "blocked"}`;
    setText("[data-upcoming-count]", `${data.upcoming.length} shown`);
    if (data.last_fire) {
      setText("[data-last-label]", data.last_fire.event_label);
      setText("[data-last-detail]", `${data.last_fire.result} · ${data.last_fire.timestamp}`);
    }
    renderUpcoming(data.upcoming);
  };
  const refresh = async () => {
    try {
      const response = await window.fetch("/operations/snapshot", { cache: "no-store" });
      if (response.ok) applySnapshot(await response.json());
    } catch (_error) {
      setText("[data-readiness-label]", "Console disconnected");
      setText("[data-readiness-detail]", "Trying to reconnect to the Raspberry Pi");
    }
  };
  const tick = () => {
    const now = serverNow();
    setText("[data-school-clock]", formatClock(now));
    const target = operations.dataset.nextTime;
    if (!target) {
      setText("[data-next-countdown]", "None in the next year");
      return;
    }
    const remaining = new Date(target).getTime() - now.getTime();
    setText("[data-next-countdown]", formatCountdown(remaining));
    if (remaining <= 0 && Date.now() >= nextRefreshAfter) {
      nextRefreshAfter = Date.now() + 5000;
      refresh();
    }
  };
  tick();
  window.setInterval(tick, 1000);
  window.setInterval(refresh, 15000);
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

const builder = document.querySelector("[data-schedule-builder]");
if (builder) {
  const form = builder.querySelector("[data-builder-form]");
  const list = builder.querySelector("[data-event-list]");
  const template = document.querySelector("#event-row-template");
  const emptyState = builder.querySelector("[data-empty-events]");
  const warning = builder.querySelector("[data-event-warning]");
  const timeline = builder.querySelector("[data-timeline-preview]");
  const previewCount = builder.querySelector("[data-preview-count]");
  const previewSound = builder.querySelector("[data-preview-sound]");
  const previewAudio = builder.querySelector("[data-preview-audio]");
  const eventLimit = Number(builder.dataset.eventLimit);
  const emergencyThreshold = Number(builder.dataset.emergencyThreshold);
  let dirty = false;
  let submitting = false;

  const rows = () => Array.from(list.querySelectorAll("[data-event-row]"));
  const field = (row, name) => row.querySelector(`[name="${name}"]`);

  const updateAudio = (sound) => {
    if (!sound || !previewAudio || !previewSound) return;
    previewSound.value = sound;
    previewAudio.src = `/sounds/${encodeURIComponent(sound)}`;
    previewAudio.load();
  };

  const refresh = () => {
    const currentRows = rows();
    emptyState.hidden = currentRows.length !== 0;
    previewCount.textContent = String(currentRows.length);
    currentRows.forEach((row, index) => {
      const time = field(row, "event_time").value || "Time not set";
      const label = field(row, "event_label").value.trim() || "Untitled event";
      row.querySelector("[data-row-number]").textContent = `Event ${index + 1}`;
      row.querySelector("[data-row-summary]").textContent = `${time} · ${label}`;
    });

    timeline.replaceChildren();
    const previewRows = currentRows
      .map((row, index) => ({ row, index, time: field(row, "event_time").value }))
      .sort((left, right) => left.time.localeCompare(right.time) || left.index - right.index);
    previewRows.forEach(({ row, time }) => {
      const item = document.createElement("li");
      const clock = document.createElement("time");
      const details = document.createElement("div");
      const title = document.createElement("strong");
      const meta = document.createElement("small");
      const repeats = Number(field(row, "event_repeat_count").value || 1);
      const priority = Number(field(row, "event_priority").value || 50);
      clock.textContent = time || "--:--";
      title.textContent = field(row, "event_label").value.trim() || "Untitled event";
      meta.textContent = `${field(row, "event_zone").value || "No zone"} · ${field(row, "event_sound").value || "No sound"} · ${repeats} play${repeats === 1 ? "" : "s"} · priority ${priority}`;
      details.append(title, meta);
      item.append(clock, details);
      if (priority >= emergencyThreshold) item.classList.add("emergency-priority");
      timeline.append(item);
    });
    if (!previewRows.length) {
      const item = document.createElement("li");
      item.className = "preview-empty";
      item.textContent = "Add an event to see the daily run.";
      timeline.append(item);
    }

    const times = currentRows.map((row) => field(row, "event_time").value).filter(Boolean);
    const duplicateTimes = [...new Set(times.filter((value, index) => times.indexOf(value) !== index))];
    const messages = [];
    if (currentRows.length > eventLimit) messages.push(`Reduce this schedule to ${eventLimit} events or fewer.`);
    if (duplicateTimes.length) messages.push(`Two events cannot share a time: ${duplicateTimes.join(", ")}.`);
    const emergencyRows = currentRows.filter((row) => Number(field(row, "event_priority").value) >= emergencyThreshold);
    if (emergencyRows.length) messages.push(`${emergencyRows.length} event${emergencyRows.length === 1 ? " has" : "s have"} emergency-level priority (at least ${emergencyThreshold}).`);
    warning.hidden = messages.length === 0;
    warning.textContent = messages.join(" ");
  };

  const addEvent = () => {
    if (rows().length >= eventLimit) {
      refresh();
      warning.hidden = false;
      warning.textContent = `The safe limit is ${eventLimit} schedule events.`;
      return;
    }
    const fragment = template.content.cloneNode(true);
    const newRow = fragment.querySelector("[data-event-row]");
    const timeInput = field(newRow, "event_time");
    const usedTimes = rows().map((row) => field(row, "event_time").value).filter(Boolean).sort();
    let nextMinutes = usedTimes.length
      ? Number(usedTimes.at(-1).slice(0, 2)) * 60 + Number(usedTimes.at(-1).slice(3)) + 5
      : Number(timeInput.min.slice(0, 2)) * 60 + Number(timeInput.min.slice(3));
    const maximum = Number(timeInput.max.slice(0, 2)) * 60 + Number(timeInput.max.slice(3));
    nextMinutes = Math.min(nextMinutes, maximum);
    timeInput.value = `${String(Math.floor(nextMinutes / 60)).padStart(2, "0")}:${String(nextMinutes % 60).padStart(2, "0")}`;
    list.append(fragment);
    dirty = true;
    refresh();
    field(newRow, "event_label").focus();
  };

  builder.querySelectorAll("[data-add-event]").forEach((button) => button.addEventListener("click", addEvent));
  list.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-event]");
    if (!remove) return;
    remove.closest("[data-event-row]").remove();
    dirty = true;
    refresh();
  });
  form.addEventListener("input", (event) => {
    dirty = true;
    if (event.target.matches("[data-sound-select]")) updateAudio(event.target.value);
    refresh();
  });
  form.addEventListener("change", (event) => {
    dirty = true;
    if (event.target.matches("[data-sound-select]")) updateAudio(event.target.value);
    refresh();
  });
  form.addEventListener("submit", () => { submitting = true; });
  previewSound?.addEventListener("change", () => updateAudio(previewSound.value));
  document.querySelector("[data-delete-form]")?.addEventListener("submit", (event) => {
    if (!window.confirm("Delete this schedule? This cannot be undone from the web interface.")) event.preventDefault();
  });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty || submitting) return;
    event.preventDefault();
    event.returnValue = "";
  });
  if (!rows().length) {
    addEvent();
    dirty = false;
  }
  refresh();
  const initialSound = rows()[0] ? field(rows()[0], "event_sound").value : previewSound?.value;
  if (initialSound) updateAudio(initialSound);
}

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll("form[data-capture-form]").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (!button) return;
    button.disabled = true;
    button.textContent = "Listening for a known page…";
  });
});

document.querySelectorAll("[data-destination-fields]").forEach((container) => {
  const selector = container.querySelector("[data-protocol-select]");
  const wireFormat = container.querySelector('[name="wire_format"]');
  const calibrationCallout = container.querySelector("[data-poly-calibration-callout]");
  const codecField = container.querySelector("[data-codec-field]");
  const codecLabel = container.querySelector("[data-codec-label]");
  const codecHelp = container.querySelector("[data-codec-help]");
  const codecOptions = [...container.querySelectorAll("[data-codec-option]")];
  const normalizeMulticastCodecs = (changed) => {
    if (selector.value !== "multicast") return;
    const selected = changed?.checked ? changed : codecOptions.find((option) => option.checked);
    codecOptions.forEach((option) => { option.checked = option === selected; });
    if (!selected && codecOptions.length) codecOptions[0].checked = true;
  };
  const refreshProtocol = () => {
    const usesPoly = selector.value === "multicast" && wireFormat?.value === "poly_group_page";
    container.querySelectorAll("[data-protocol-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.protocolPanel !== selector.value;
    });
    const port = container.querySelector('[name="port"]');
    const portLabel = container.querySelector("[data-port-label]");
    if (selector.value === "multicast") {
      if (["443", "5060"].includes(port.value)) port.value = "601";
      portLabel.textContent = "UDP port";
    } else if (selector.value === "sip") {
      if (port.value === "601") port.value = "5060";
      portLabel.textContent = "SIP port";
    } else {
      if (["601", "5060"].includes(port.value)) port.value = "443";
      portLabel.textContent = "Integration port";
    }
    if (calibrationCallout) {
      calibrationCallout.hidden = selector.value !== "multicast" || wireFormat?.value !== "poly_group_page";
    }
    if (codecField) codecField.hidden = selector.value === "http";
    if (codecLabel) {
      codecLabel.textContent = selector.value === "multicast" ? "Multicast codec (choose one)" : "SIP codec preference";
    }
    codecOptions.forEach((option) => {
      const unsupported = usesPoly && option.value === "pcma";
      option.disabled = unsupported;
      if (unsupported) option.checked = false;
    });
    if (codecHelp) {
      codecHelp.textContent = usesPoly
        ? "Poly Group Page supports PCMU or G722. Choose the codec configured on the phones and speakers."
        : "Multicast uses exactly one codec and must match the receivers. SIP may offer multiple codecs.";
    }
    normalizeMulticastCodecs();
  };
  selector.addEventListener("change", refreshProtocol);
  wireFormat?.addEventListener("change", refreshProtocol);
  codecOptions.forEach((option) => option.addEventListener("change", () => normalizeMulticastCodecs(option)));
  refreshProtocol();
});
