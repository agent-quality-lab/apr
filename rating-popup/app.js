(function () {
  "use strict";

  var finished = null;
  var reasons = new Set();
  var ratings = { overall: 0, quality: 0, speed: 0, resource_efficiency: 0 };
  var cardRevision = null;
  var cardLoaded = false;
  var busy = false;
  var formLocked = false;

  var btnYes = document.getElementById("btn-yes");
  var btnNo = document.getElementById("btn-no");
  var reasonsBlock = document.getElementById("reasons-block");
  var ratingsBlock = document.getElementById("ratings-block");
  var btnSubmit = document.getElementById("btn-submit");
  var btnSkip = document.getElementById("btn-skip");
  var btnRetry = document.getElementById("btn-retry");
  var confirmation = document.getElementById("confirmation");
  var ratingsHint = document.getElementById("ratings-hint");

  function setBinary(value) {
    finished = value;
    if (value === true) {
      reasons.clear();
      reasonsBlock.querySelectorAll(".chip").forEach(function (chip) {
        chip.setAttribute("aria-pressed", "false");
      });
    }
    btnYes.setAttribute("aria-pressed", value === true ? "true" : "false");
    btnNo.setAttribute("aria-pressed", value === false ? "true" : "false");
    reasonsBlock.hidden = value !== false;
    ratingsBlock.hidden = false;
    confirmation.hidden = true;
    updateSubmit();
    if (value === false) {
      var firstChip = reasonsBlock.querySelector(".chip");
      if (firstChip) firstChip.focus();
    }
  }

  function paintStars(row) {
    var key = row.getAttribute("data-rating");
    var value = ratings[key];
    row.querySelectorAll(".stars button").forEach(function (btn) {
      var v = Number(btn.getAttribute("data-value"));
      btn.classList.toggle("lit", v <= value);
      btn.setAttribute("aria-pressed", v === value ? "true" : "false");
    });
  }

  function allRated() {
    return ratings.overall > 0 && ratings.quality > 0 && ratings.speed > 0 &&
      ratings.resource_efficiency > 0;
  }

  function updateSubmit() {
    if (formLocked) {
      btnSubmit.disabled = true;
      btnSkip.disabled = true;
      return;
    }
    var ready = cardLoaded && !busy && finished !== null && allRated();
    btnSubmit.disabled = !ready;
    btnSkip.disabled = !cardLoaded || busy;
    if (!cardLoaded) {
      ratingsHint.textContent = "Loading run summary…";
    } else if (finished === null) {
      ratingsHint.textContent = "First answer Yes or No above, then rate each row.";
    } else if (!allRated()) {
      ratingsHint.textContent = "Select a star rating for each row to enable Save locally.";
    } else {
      ratingsHint.textContent = "All ratings set. Save locally, or Skip review to discard.";
    }
  }

  btnYes.addEventListener("click", function () { if (!busy && !formLocked) setBinary(true); });
  btnNo.addEventListener("click", function () { if (!busy && !formLocked) setBinary(false); });

  reasonsBlock.querySelectorAll(".chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      if (busy || formLocked) return;
      var key = chip.getAttribute("data-reason");
      if (reasons.has(key)) {
        reasons.delete(key);
        chip.setAttribute("aria-pressed", "false");
      } else {
        reasons.add(key);
        chip.setAttribute("aria-pressed", "true");
      }
    });
  });

  document.querySelectorAll(".rating-row").forEach(function (row) {
    var key = row.getAttribute("data-rating");
    row.querySelectorAll(".stars button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (busy || formLocked) return;
        ratings[key] = Number(btn.getAttribute("data-value"));
        paintStars(row);
        updateSubmit();
      });
    });
  });

  function showConfirmation(text) {
    confirmation.textContent = text;
    confirmation.hidden = false;
  }

  function setEditableDisabled(value) {
    btnYes.disabled = value;
    btnNo.disabled = value;
    reasonsBlock.querySelectorAll(".chip").forEach(function (chip) {
      chip.disabled = value;
    });
    document.querySelectorAll(".stars button").forEach(function (btn) {
      btn.disabled = value;
    });
  }

  function setBusy(value, label) {
    if (formLocked) {
      busy = false;
      setEditableDisabled(true);
      btnSubmit.disabled = true;
      btnSkip.disabled = true;
      btnRetry.hidden = true;
      btnRetry.disabled = true;
      return;
    }
    busy = value;
    setEditableDisabled(value);
    btnSubmit.disabled = true;
    btnSkip.disabled = value || !cardLoaded;
    btnSubmit.textContent = value ? label : "Save locally";
    btnRetry.hidden = true;
    btnRetry.disabled = true;
    if (!value) updateSubmit();
  }

  function lockForm(message) {
    formLocked = true;
    busy = false;
    setEditableDisabled(true);
    btnSubmit.hidden = true;
    btnSkip.hidden = true;
    btnRetry.hidden = true;
    btnRetry.disabled = true;
    document.querySelectorAll(".popup button").forEach(function (btn) {
      if (btn !== btnRetry) btn.disabled = true;
    });
    if (message) showConfirmation(message);
  }

  async function postSubmit(payload, label) {
    if (window.location.protocol === "file:") {
      showConfirmation("Preview only. Opened directly from disk, so nothing was saved. The installed local service saves reviews.");
      return;
    }
    if (busy || formLocked || !cardLoaded) return;
    setBusy(true, label);
    try {
      var response = await fetch("api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      var result = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        if (response.status === 409) {
          await loadCard();
          if (formLocked) {
            setBusy(false);
            return;
          }
          throw new Error((result.error || "This card changed.") +
            " The latest summary is shown above — review it, then save again.");
        }
        throw new Error(result.error || "The local service could not save this review.");
      }
      var doneMessage = result.status === "skipped"
        ? "Skipped. No rating was saved; the review link still works."
        : "Saved on this computer. Nothing was published.";
      lockForm(doneMessage);
    } catch (error) {
      if (formLocked) {
        setBusy(false);
        return;
      }
      setBusy(false, null);
      btnRetry.hidden = false;
      btnRetry.disabled = false;
      showConfirmation(error.message + " You can Retry, or Skip review to discard.");
    }
  }

  btnSubmit.addEventListener("click", function () {
    if (btnSubmit.disabled) return;
    postSubmit({
      completion: finished ? "yes" : "no",
      gap_reasons: Array.from(reasons),
      ratings: ratings,
      revision: cardRevision
    }, "Saving…");
  });

  btnSkip.addEventListener("click", function () {
    if (btnSkip.disabled) return;
    postSubmit({ skip: true }, "Skipping…");
  });

  btnRetry.addEventListener("click", function () {
    if (busy || formLocked) return;
    btnRetry.hidden = true;
    btnRetry.disabled = true;
    confirmation.hidden = true;
    if (!cardLoaded) {
      loadCard();
    } else {
      setBusy(false, null);
    }
  });

  function readable(value) {
    if (value === undefined || value === null || value === "" || value === "unknown") return "Unavailable";
    if (typeof value === "object") {
      var entries = Object.entries(value);
      if (!entries.length) return "None observed";
      return entries.map(function (entry) { return entry[0] + " (" + entry[1] + ")"; }).join(", ");
    }
    return String(value);
  }

  function starSummary(savedRatings) {
    return ["overall", "quality", "speed", "resource_efficiency"]
      .map(function (key) { return key + " " + savedRatings[key] + "/5"; }).join(" · ");
  }

  function showSavedState(card) {
    var block = document.getElementById("saved-block");
    var detail = document.getElementById("saved-detail");
    var saved = card.saved || {};
    detail.textContent = "Saved " + (saved.recorded_at || "earlier") +
      " · " + (saved.completion === "yes" ? "finished" : "not finished") +
      (saved.ratings ? " · " + starSummary(saved.ratings) : "") +
      ". This page is read-only; further saves return the same saved record.";
    block.hidden = false;
    document.querySelector(".binary-block").hidden = true;
    reasonsBlock.hidden = true;
    ratingsBlock.hidden = true;
    lockForm("This review is already saved. The saved snapshot and ratings are shown above.");
  }

  function updateSummary(data) {
    document.getElementById("sample-badge").hidden = true;
    document.getElementById("summary-heading").textContent = "This run";
    document.getElementById("summary-task").textContent = readable(data.task);
    document.getElementById("summary-result").textContent = readable(data.result);
    document.getElementById("summary-action").textContent = readable(data.action);
    var context = data.context || {};
    var resources = data.resources || {};
    document.getElementById("summary-model").textContent = readable(context.model);
    document.getElementById("summary-tools").textContent = readable(context.connections);
    document.getElementById("summary-context").textContent =
      "Runtime: " + readable(context.runtime) +
      " · Agent: " + readable(context.agent) +
      " · Model: " + readable(context.model) +
      " · Tools: " + readable(context.connections) +
      " · Workflow: " + readable(context.loops) +
      " · Roles: " + readable(context.roles);
    document.getElementById("summary-usage").textContent =
      "Tokens " + readable(resources.tokens) +
      " · " + readable(resources.tool_calls) + " tool calls";
    document.getElementById("summary-resources").textContent =
      "Time " + readable(resources.elapsed_time) +
      " · Tokens " + readable(resources.tokens) +
      " · Cost " + readable(resources.cost) +
      " · Tool calls " + readable(resources.tool_calls);
  }

  async function loadCard() {
    cardLoaded = false;
    updateSubmit();
    var response;
    try {
      response = await fetch("api/review", { cache: "no-store" });
    } catch (error) {
      btnRetry.hidden = false;
      btnRetry.disabled = false;
      showConfirmation("Run summary could not be loaded. Check that the local service is running, then Retry.");
      updateSubmit();
      return;
    }
    if (!response.ok) {
      btnRetry.hidden = false;
      btnRetry.disabled = false;
      showConfirmation("This rating link is invalid. Ask the agent for a fresh link, or Retry.");
      updateSubmit();
      return;
    }
    var card = await response.json();
    if (!card || !card.snapshot || typeof card.revision !== "number") {
      btnRetry.hidden = false;
      btnRetry.disabled = false;
      showConfirmation("Run summary could not be loaded. Retry, or ask the agent for a fresh link.");
      updateSubmit();
      return;
    }
    cardRevision = card.revision;
    cardLoaded = true;
    updateSummary(card.snapshot);
    if (card.status === "saved") {
      showSavedState(card);
      return;
    }
    btnRetry.hidden = true;
    btnRetry.disabled = true;
    confirmation.hidden = true;
    updateSubmit();
  }

  if (window.location.protocol !== "file:") {
    loadCard();
  } else {
    document.getElementById("summary-heading").textContent = "Preview";
    showConfirmation("Preview only. Opened directly from disk, so nothing can be saved here.");
  }

  updateSubmit();
})();
