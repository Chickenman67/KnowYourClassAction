/* Ledger filters. No dependencies, no build step: the page is a static
   document and this only hides rows that do not match, then reports how
   many are left and which lanes went empty. */

(function () {
  "use strict";

  var stateSel = document.getElementById("filter-state");
  var proofSel = document.getElementById("filter-proof");
  var tierSel = document.getElementById("filter-tier");
  var resetBtn = document.getElementById("filter-reset");
  var out = document.getElementById("filter-count");
  if (!stateSel || !proofSel || !tierSel) return;

  var rows = Array.prototype.slice.call(document.querySelectorAll(".row"));
  var lanes = Array.prototype.slice.call(document.querySelectorAll(".lane"));
  var total = rows.length;

  function isMatch(row, f) {
    if (f.state) {
      var states = (row.getAttribute("data-states") || "").split(/\s+/);
      if (states.indexOf(f.state) === -1) return false;
    }
    if (f.proof && (row.getAttribute("data-proof") || "unknown") !== f.proof) {
      return false;
    }
    if (f.tier && (row.getAttribute("data-tier") || "none") !== f.tier) {
      return false;
    }
    return true;
  }

  function apply() {
    var f = {
      state: stateSel.value,
      proof: proofSel.value,
      tier: tierSel.value
    };
    var active = !!(f.state || f.proof || f.tier);
    var shown = 0;

    rows.forEach(function (row) {
      var ok = isMatch(row, f);
      row.hidden = !ok;
      if (ok) shown++;
    });

    lanes.forEach(function (lane) {
      var visible = lane.querySelectorAll(".row:not([hidden])").length;
      var msg = lane.querySelector(".lane__filtered-empty");
      if (msg) msg.hidden = visible > 0;
      var count = lane.querySelector(".lane__count");
      if (count && active) {
        var laneTotal = lane.querySelectorAll(".row").length;
        count.textContent = visible + "/" + laneTotal;
      } else if (count) {
        count.textContent = count.getAttribute("data-total") || count.textContent;
      }
    });

    if (out) {
      out.textContent = active
        ? shown + " of " + total + " cases match"
        : "";
    }
  }

  // Remember the unfiltered lane totals so the badges can be restored.
  lanes.forEach(function (lane) {
    var count = lane.querySelector(".lane__count");
    if (count) count.setAttribute("data-total", count.textContent.trim());
  });

  [stateSel, proofSel, tierSel].forEach(function (sel) {
    sel.addEventListener("change", apply);
  });

  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      stateSel.value = "";
      proofSel.value = "";
      tierSel.value = "";
      apply();
    });
  }

  apply();
})();