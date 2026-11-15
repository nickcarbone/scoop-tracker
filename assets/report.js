(() => {
  "use strict";

  const STORAGE_KEY = "scoop-tracker-triage-v1";
  const cards = Array.from(document.querySelectorAll("[data-story-card]"));
  const buckets = Array.from(document.querySelectorAll("[data-bucket]"));
  const searchInput = document.getElementById("story-search");
  const sourceFilter = document.getElementById("source-filter");
  const sortSelect = document.getElementById("sort-by");
  const resultsSummary = document.getElementById("results-summary");
  const restoreView = document.querySelector('[data-view="dismissed"]');
  const viewButtons = Array.from(document.querySelectorAll("[data-view]"));
  const signalButtons = Array.from(document.querySelectorAll("[data-signal]"));

  let activeView = "all";
  let activeSignal = "all";
  let toastTimer = null;
  let storageAvailable = true;

  function readState() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      return {
        saved: new Set(Array.isArray(parsed.saved) ? parsed.saved : []),
        dismissed: new Set(Array.isArray(parsed.dismissed) ? parsed.dismissed : [])
      };
    } catch (error) {
      storageAvailable = false;
      return { saved: new Set(), dismissed: new Set() };
    }
  }

  const state = readState();

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        saved: Array.from(state.saved),
        dismissed: Array.from(state.dismissed)
      }));
    } catch (error) {
      storageAvailable = false;
      announce("Your browser blocked local storage. Changes will last only until this page is closed.");
    }
  }

  function announce(message) {
    let node = document.getElementById("triage-toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "triage-toast";
      node.className = "toast";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { node.hidden = true; }, 3000);
  }

  function getUrls(card) {
    try {
      const list = JSON.parse(card.dataset.storyUrls || "[]");
      return Array.isArray(list) ? list.filter(url => typeof url === "string" && /^https?:\/\//i.test(url)) : [];
    } catch (error) {
      return [];
    }
  }

  function cardIsSaved(card) {
    return getUrls(card).some(url => state.saved.has(url));
  }

  function cardIsDismissed(card) {
    return getUrls(card).some(url => state.dismissed.has(url));
  }

  function setCardActionLabels(card) {
    const saved = cardIsSaved(card);
    const dismissed = cardIsDismissed(card);
    const save = card.querySelector('[data-action="save"], .save-button');
    const dismiss = card.querySelector(".dismiss-button");
    card.classList.toggle("is-saved", saved);
    card.classList.toggle("is-dismissed", dismissed);
    if (save) {
      save.textContent = saved ? "Saved" : "Save";
      save.setAttribute("aria-pressed", String(saved));
      save.setAttribute("aria-label", saved ? "Remove from saved stories" : "Save this story");
    }
    if (dismiss) {
      dismiss.dataset.action = dismissed ? "restore" : "dismiss";
      dismiss.textContent = dismissed ? "Restore" : "Dismiss";
      dismiss.setAttribute("aria-label", dismissed ? "Restore this story" : "Dismiss this story");
    }
  }

  function populateSources() {
    const sources = new Set();
    cards.forEach(card => {
      try {
        const list = JSON.parse(card.dataset.sources || "[]");
        if (Array.isArray(list)) list.forEach(name => { if (name) sources.add(name); });
      } catch (error) {
        if (card.dataset.source) sources.add(card.dataset.source);
      }
    });
    Array.from(sources).sort((a, b) => a.localeCompare(b)).forEach(source => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = source;
      sourceFilter.appendChild(option);
    });
  }

  function cardSearchText(card) {
    return (card.textContent || "").toLocaleLowerCase();
  }

  function setActiveButton(buttons, attribute, value) {
    buttons.forEach(button => {
      const active = button.dataset[attribute] === value;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function sortCards() {
    const mode = sortSelect.value;
    buckets.forEach(bucket => {
      const list = bucket.querySelector(".story-list");
      if (!list) return;
      const bucketCards = Array.from(list.querySelectorAll("[data-story-card]"));
      bucketCards.sort((a, b) => {
        if (mode === "newest") return Number(b.dataset.time || 0) - Number(a.dataset.time || 0);
        if (mode === "source") return (a.dataset.source || "").localeCompare(b.dataset.source || "");
        return Number(b.dataset.score || 0) - Number(a.dataset.score || 0);
      });
      bucketCards.forEach(card => list.appendChild(card));
    });
  }

  function refreshCounts() {
    const savedCount = cards.filter(cardIsSaved).length;
    const dismissedCount = cards.filter(cardIsDismissed).length;
    const newCount = cards.filter(card => card.dataset.new === "true").length;
    const counts = { all: cards.length, new: newCount, saved: savedCount, dismissed: dismissedCount };
    Object.entries(counts).forEach(([key, count]) => {
      const node = document.querySelector('[data-view-count="' + key + '"]');
      if (node) node.textContent = String(count);
    });
  }

  function render() {
    sortCards();
    let visibleTotal = 0;
    const query = (searchInput.value || "").trim().toLocaleLowerCase();
    const selectedSource = sourceFilter.value;

    buckets.forEach(bucket => {
      const list = bucket.querySelector(".story-list");
      const bucketCards = Array.from(list.querySelectorAll("[data-story-card]"));
      let bucketVisible = 0;

      bucketCards.forEach(card => {
        setCardActionLabels(card);
        const saved = cardIsSaved(card);
        const dismissed = cardIsDismissed(card);
        const isNew = card.dataset.new === "true";
        const sources = (() => {
          try { return JSON.parse(card.dataset.sources || "[]"); }
          catch (error) { return [card.dataset.source || ""]; }
        })();
        const matchesView =
          activeView === "dismissed" ? dismissed :
          activeView === "saved" ? saved && !dismissed :
          activeView === "new" ? isNew && !dismissed :
          !dismissed;
        const matchesSignal = activeSignal === "all" ||
          (card.dataset.tags || "").split(/\s+/).includes(activeSignal);
        const matchesSource = !selectedSource || sources.includes(selectedSource);
        const matchesQuery = !query || cardSearchText(card).includes(query);
        const visible = matchesView && matchesSignal && matchesSource && matchesQuery;
        card.hidden = !visible;
        if (visible) {
          bucketVisible += 1;
          visibleTotal += 1;
        }
      });

      const empty = list.querySelector(".empty");
      if (empty) empty.hidden = bucketVisible > 0;
      const count = bucket.querySelector("[data-bucket-count]");
      if (count) count.textContent = activeView === "all" && activeSignal === "all" && !query && !selectedSource
        ? bucketCards.length + " available"
        : bucketVisible + " visible · " + bucketCards.length + " in section";
      bucket.hidden = bucketVisible === 0;
    });

    const total = cards.length;
    resultsSummary.textContent = "Showing " + visibleTotal + " of " + total + " leads";
    setActiveButton(viewButtons, "view", activeView);
    setActiveButton(signalButtons, "signal", activeSignal);
    refreshCounts();
    restoreView.hidden = !cards.some(cardIsDismissed);
  }

  function updateStory(card, action) {
    const urls = getUrls(card);
    if (!urls.length) {
      announce("This story has no usable article URL, so it cannot be saved across visits.");
      return;
    }
    if (action === "save") {
      urls.forEach(url => {
        state.saved.add(url);
        state.dismissed.delete(url);
      });
      persist();
      announce("Story saved in this browser.");
    } else if (action === "dismiss") {
      urls.forEach(url => {
        state.dismissed.add(url);
        state.saved.delete(url);
      });
      persist();
      announce("Story dismissed. You can restore it from the Dismissed view.");
    } else if (action === "restore") {
      urls.forEach(url => state.dismissed.delete(url));
      persist();
      announce("Story restored to the lead queue.");
    }
    render();
  }

  document.addEventListener("click", event => {
    const actionButton = event.target.closest("[data-action]");
    if (actionButton) {
      const card = actionButton.closest("[data-story-card]");
      if (card) updateStory(card, actionButton.dataset.action);
      return;
    }
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) {
      activeView = viewButton.dataset.view;
      render();
      return;
    }
    const signalButton = event.target.closest("[data-signal]");
    if (signalButton) {
      activeSignal = signalButton.dataset.signal;
      render();
    }
  });

  searchInput.addEventListener("input", render);
  sourceFilter.addEventListener("change", render);
  sortSelect.addEventListener("change", render);
  document.getElementById("reset-filters").addEventListener("click", () => {
    searchInput.value = "";
    sourceFilter.value = "";
    sortSelect.value = "score";
    activeView = "all";
    activeSignal = "all";
    render();
    announce("Filters reset. Saved and dismissed stories were kept.");
  });

  document.addEventListener("keydown", event => {
    const target = event.target;
    const isTyping = target instanceof HTMLElement &&
      (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
    if (event.key === "/" && !isTyping && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      searchInput.focus();
    } else if (event.key === "Escape" && target === searchInput) {
      if (searchInput.value) {
        searchInput.value = "";
        render();
      } else {
        searchInput.blur();
      }
    }
  });

  populateSources();
  cards.forEach(setCardActionLabels);
  sortCards();
  render();

  if (!storageAvailable) {
    announce("Browser storage is unavailable. Saved and dismissed choices may not persist.");
  }
})();
