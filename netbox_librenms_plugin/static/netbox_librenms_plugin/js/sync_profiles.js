// Sync profiles: the ordered step picker (drag and drop, move buttons) and the run result filter.
(function () {
  "use strict";

  function initStepsWidget(root) {
    if (root.dataset.ready) return;
    root.dataset.ready = "1";
    const list = root.querySelector(".nblp-steps-list");
    let dragged = null;

    // Number the ticked rows in run order; an unticked row does not run.
    function refresh() {
      let position = 0;
      list.querySelectorAll(".nblp-step").forEach(function (row) {
        const checked = row.querySelector("input[type=checkbox]").checked;
        row.classList.toggle("is-checked", checked);
        row.querySelector(".nblp-step-number").textContent = checked ? String(++position) : "–";
      });
    }

    list.addEventListener("change", refresh);
    list.addEventListener("click", function (event) {
      const button = event.target.closest("[data-move]");
      if (!button) return;
      const row = button.closest(".nblp-step");
      if (button.dataset.move === "up" && row.previousElementSibling) {
        list.insertBefore(row, row.previousElementSibling);
      } else if (button.dataset.move === "down" && row.nextElementSibling) {
        list.insertBefore(row.nextElementSibling, row);
      }
      refresh();
    });
    list.addEventListener("dragstart", function (event) {
      dragged = event.target.closest(".nblp-step");
      if (!dragged) return;
      dragged.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", "");
    });
    list.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-dragging");
      list.querySelectorAll(".is-drop-target").forEach(function (row) { row.classList.remove("is-drop-target"); });
      dragged = null;
    });
    list.addEventListener("dragover", function (event) {
      const target = event.target.closest(".nblp-step");
      if (!dragged || !target || target === dragged) return;
      event.preventDefault();
      list.querySelectorAll(".is-drop-target").forEach(function (row) { row.classList.remove("is-drop-target"); });
      target.classList.add("is-drop-target");
    });
    list.addEventListener("drop", function (event) {
      const target = event.target.closest(".nblp-step");
      if (!dragged || !target || target === dragged) return;
      event.preventDefault();
      const rect = target.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      list.insertBefore(dragged, after ? target.nextElementSibling : target);
      target.classList.remove("is-drop-target");
      refresh();
    });
    refresh();
  }

  function initResultFilter(root) {
    if (root.dataset.ready) return;
    root.dataset.ready = "1";
    root.addEventListener("click", function (event) {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      root.querySelectorAll("[data-filter]").forEach(function (b) { b.classList.toggle("active", b === button); });
      const wanted = button.dataset.filter;
      const scope = root.closest("[data-run-results]") || document;
      scope.querySelectorAll("[data-device-status]").forEach(function (row) {
        row.classList.toggle("d-none", wanted !== "all" && row.dataset.deviceStatus !== wanted);
      });
    });
  }

  // Keep the newest log lines in view while a run's results reload themselves.
  function scrollLogs() {
    document.querySelectorAll("[data-job-log]").forEach(function (box) { box.scrollTop = box.scrollHeight; });
  }

  function initAll() {
    scrollLogs();
    document.querySelectorAll("[data-steps-widget]").forEach(initStepsWidget);
    document.querySelectorAll("[data-result-filter]").forEach(initResultFilter);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
  document.addEventListener("htmx:afterSettle", initAll);
})();
