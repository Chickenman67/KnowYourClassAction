/* Ledger filters. No dependencies, no build step: the page is a static
   document and this only hides rows that do not match, then reports how
   many are left and which lanes went empty.

   The matching rule and the wiring are exported for the test suite when this
   file is loaded in Node (tests/js/site_filters.test.mjs). The page has no
   build step, so this stays a plain script: the export is guarded and the
   browser path is unchanged. */

(function () {
  "use strict";

  /* Does this row survive the current filters?
     A state filter matches when the state is one of the row's own; proof and
     tier compare exactly. A row missing an attribute reads as the page's
     default ("unknown" / "none"), which is what the markup emits anyway - so
     the two never disagree and a filter cannot silently match nothing. */
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

  /* Wire the filters to a document. Returns the apply function, or null when
     the page has no filter controls. */
  function init(doc) {
    var stateSel = doc.getElementById("filter-state");
    var proofSel = doc.getElementById("filter-proof");
    var tierSel = doc.getElementById("filter-tier");
    var resetBtn = doc.getElementById("filter-reset");
    var out = doc.getElementById("filter-count");
    if (!stateSel || !proofSel || !tierSel) return null;

    var rows = Array.prototype.slice.call(doc.querySelectorAll(".row"));
    var lanes = Array.prototype.slice.call(doc.querySelectorAll(".lane"));
    var total = rows.length;

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
        out.textContent = active ? shown + " of " + total + " cases match" : "";
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
    return apply;
  }

  var api = { isMatch: isMatch, init: init };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document !== "undefined") init(document);
})();