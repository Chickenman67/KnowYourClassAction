/* The site's filter logic, tested without a browser.

   app.js was the last behaviour in the shipped product with no test at all: 76
   lines of JS that decide which rows a reader sees, and the only thing checking
   it was that the file was *shipped*. That exact gap has bitten before - the
   page once deployed with dead filters while every test stayed green, because
   nothing looked at behaviour.

   No DOM dependency: the script exports its matching rule and its wiring when
   loaded under Node, and this file drives them with a hand-built tree of just
   the elements the selector strings actually use.
*/

import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const APP_PATH = fileURLToPath(
  new URL("../../src/kya/templates/static/app.js", import.meta.url)
);
const { isMatch, init } = require(APP_PATH);

// --- a DOM with exactly the parts app.js touches ------------------------------

const SUPPORTED = /^\.[a-z0-9_-]+(:not\(\[hidden\]\))?$/i;

function matchesSelector(el, selector) {
  if (!SUPPORTED.test(selector)) throw new Error(`fake DOM cannot do ${selector}`);
  const negated = selector.endsWith(":not([hidden])");
  const base = negated ? selector.replace(":not([hidden])", "") : selector;
  const classes = (el.className || "").split(/\s+/).filter(Boolean);
  if (!classes.includes(base.slice(1))) return false;
  return negated ? el.hidden === false : true;
}

class FakeElement {
  constructor(tag, { className = "", attrs = {}, text = "" } = {}) {
    this.tag = tag;
    this.className = className;
    this.attributes = new Map(Object.entries(attrs));
    this.textContent = text;
    this.hidden = false;
    this.value = "";
    this.children = [];
    this.listeners = new Map();
  }

  append(child) {
    this.children.push(child);
    return child;
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  fire(type) {
    for (const fn of this.listeners.get(type) || []) fn();
  }

  *descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }

  querySelectorAll(selector) {
    return [...this.descendants()].filter((el) => matchesSelector(el, selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

class FakeDocument extends FakeElement {
  constructor() {
    super("document");
    this.byId = new Map();
  }

  add(el, id) {
    if (id) this.byId.set(id, el);
    this.append(el);
    return el;
  }

  getElementById(id) {
    return this.byId.get(id) || null;
  }
}

// --- a fixture shaped like the real page --------------------------------------

/** One row, with the attributes the template emits. */
function addRow(lane, { states = "", proof = "unknown", tier = "none" } = {}) {
  return lane.append(
    new FakeElement("li", {
      className: "row",
      attrs: { "data-states": states, "data-proof": proof, "data-tier": tier },
    })
  );
}

function addLane(doc, rows) {
  const lane = doc.append(new FakeElement("section", { className: "lane" }));
  const badge = lane.append(
    new FakeElement("span", { className: "lane__count", text: String(rows.length) })
  );
  const empty = lane.append(new FakeElement("p", { className: "lane__filtered-empty" }));
  empty.hidden = true;
  const elements = rows.map((spec) => addRow(lane, spec));
  return { lane, badge, empty, rows: elements };
}

const FIXTURE_ROWS = [
  { states: "CA", proof: "L1", tier: "A" },
  { states: "NY CA", proof: "L3", tier: "B" },
  { states: "", proof: "L1", tier: "A" },
];

function buildDoc({ withFilters = true } = {}) {
  const doc = new FakeDocument();
  const controls = {};
  if (withFilters) {
    for (const id of ["filter-state", "filter-proof", "filter-tier"]) {
      controls[id] = doc.add(new FakeElement("select"), id);
    }
    controls["filter-reset"] = doc.add(new FakeElement("button"), "filter-reset");
    controls["filter-count"] = doc.add(new FakeElement("output"), "filter-count");
  }
  const first = addLane(doc, FIXTURE_ROWS);
  const second = addLane(doc, [{ states: "TX", proof: "L3", tier: "B" }]);
  return { doc, controls, first, second };
}

const visible = (rows) => rows.filter((r) => r.hidden === false).length;

// --- the matching rule, on its own ---------------------------------------------

test("isMatch: no filters means every row matches", () => {
  const row = new FakeElement("li", {
    className: "row",
    attrs: { "data-states": "CA", "data-proof": "L1", "data-tier": "A" },
  });
  assert.equal(isMatch(row, { state: "", proof: "", tier: "" }), true);
});

test("isMatch: a state filter matches any of the row's states", () => {
  const row = new FakeElement("li", {
    className: "row",
    attrs: { "data-states": "NY CA", "data-proof": "L1", "data-tier": "A" },
  });
  assert.equal(isMatch(row, { state: "CA", proof: "", tier: "" }), true);
  assert.equal(isMatch(row, { state: "NY", proof: "", tier: "" }), true);
  assert.equal(isMatch(row, { state: "TX", proof: "", tier: "" }), false);
});

test("isMatch: a state filter excludes a row that has no states", () => {
  const row = new FakeElement("li", { className: "row", attrs: { "data-states": "" } });
  assert.equal(isMatch(row, { state: "CA", proof: "", tier: "" }), false);
});

test("isMatch: proof and tier compare exactly", () => {
  const row = new FakeElement("li", {
    className: "row",
    attrs: { "data-proof": "L3", "data-tier": "B" },
  });
  assert.equal(isMatch(row, { state: "", proof: "L3", tier: "" }), true);
  assert.equal(isMatch(row, { state: "", proof: "L1", tier: "" }), false);
  assert.equal(isMatch(row, { state: "", proof: "", tier: "B" }), true);
  assert.equal(isMatch(row, { state: "", proof: "", tier: "A" }), false);
});

test("isMatch: a row with no attributes reads as the page's own defaults", () => {
  // The template emits data-proof="unknown" and data-tier="none" for a row that
  // has neither, so the fallback here must agree with it - if the two drifted,
  // filtering by "unknown" would silently return nothing.
  const bare = new FakeElement("li", { className: "row" });
  assert.equal(isMatch(bare, { state: "", proof: "unknown", tier: "" }), true);
  assert.equal(isMatch(bare, { state: "", proof: "L1", tier: "" }), false);
  assert.equal(isMatch(bare, { state: "", proof: "", tier: "none" }), true);
  assert.equal(isMatch(bare, { state: "", proof: "", tier: "A" }), false);
});

test("isMatch: every active filter must pass, not just one", () => {
  const row = new FakeElement("li", {
    className: "row",
    attrs: { "data-states": "CA", "data-proof": "L1", "data-tier": "A" },
  });
  assert.equal(isMatch(row, { state: "CA", proof: "L1", tier: "A" }), true);
  assert.equal(isMatch(row, { state: "CA", proof: "L3", tier: "A" }), false);
  assert.equal(isMatch(row, { state: "CA", proof: "L1", tier: "B" }), false);
});

// --- the wiring -----------------------------------------------------------------

test("init: a page with no filter controls is left alone", () => {
  // app.js is loaded on every page the builder writes, so a page without the
  // selects must not throw.
  const { doc } = buildDoc({ withFilters: false });
  assert.equal(init(doc), null);
});

test("init: returns the apply function so it can be driven directly", () => {
  const { doc } = buildDoc();
  assert.equal(typeof init(doc), "function");
});

test("unfiltered: nothing is hidden, badges show totals, output is empty", () => {
  const { doc, controls, first, second } = buildDoc();
  init(doc);

  assert.equal(visible(first.rows), 3);
  assert.equal(visible(second.rows), 1);
  assert.equal(first.badge.textContent, "3");
  assert.equal(second.badge.textContent, "1");
  assert.equal(controls["filter-count"].textContent, "");
  assert.equal(first.empty.hidden, true);
  assert.equal(second.empty.hidden, true);
});

test("filtering by state hides the rest and reports the tally", () => {
  const { doc, controls, first, second } = buildDoc();
  const apply = init(doc);

  controls["filter-state"].value = "CA";
  apply();

  // CA matches the "CA" row and the "NY CA" row, never the one with no states.
  assert.equal(visible(first.rows), 2);
  assert.equal(visible(second.rows), 0);
  assert.equal(controls["filter-count"].textContent, "2 of 4 cases match");
});

test("lane badges become visible/total while a filter is active", () => {
  const { doc, controls, first, second } = buildDoc();
  const apply = init(doc);

  controls["filter-proof"].value = "L1";
  apply();

  assert.equal(first.badge.textContent, "2/3");
  assert.equal(second.badge.textContent, "0/1");
});

test("a lane emptied by the filters says so, and stops saying so when it is not", () => {
  const { doc, controls, second } = buildDoc();
  const apply = init(doc);

  controls["filter-state"].value = "CA";
  apply();
  assert.equal(second.empty.hidden, false, "an all-hidden lane must explain itself");

  controls["filter-state"].value = "TX";
  apply();
  assert.equal(second.empty.hidden, true, "the message must go once a row shows");
  assert.equal(second.badge.textContent, "1/1");
});

test("filters combine rather than override each other", () => {
  const { doc, controls, first } = buildDoc();
  const apply = init(doc);

  controls["filter-state"].value = "CA";
  controls["filter-proof"].value = "L1";
  apply();

  // Only the first row is both Californian and no-proof.
  assert.equal(visible(first.rows), 1);
  assert.equal(controls["filter-count"].textContent, "1 of 4 cases match");
});

test("changing a select applies the filters without an explicit call", () => {
  const { doc, controls, first } = buildDoc();
  init(doc);

  controls["filter-tier"].value = "A";
  controls["filter-tier"].fire("change");

  assert.equal(visible(first.rows), 2);
});

test("reset clears every filter and restores the unfiltered view", () => {
  const { doc, controls, first, second } = buildDoc();
  init(doc);

  controls["filter-state"].value = "CA";
  controls["filter-proof"].value = "L1";
  controls["filter-tier"].value = "A";
  controls["filter-proof"].fire("change");
  assert.equal(visible(first.rows), 1);

  controls["filter-reset"].fire("click");

  assert.equal(controls["filter-state"].value, "");
  assert.equal(controls["filter-proof"].value, "");
  assert.equal(controls["filter-tier"].value, "");
  assert.equal(visible(first.rows), 3);
  assert.equal(visible(second.rows), 1);
  assert.equal(first.badge.textContent, "3", "the badge must go back to the total");
  assert.equal(second.badge.textContent, "1");
  assert.equal(controls["filter-count"].textContent, "");
});

test("a filter that matches nothing leaves every row hidden and says so", () => {
  const { doc, controls, first, second } = buildDoc();
  const apply = init(doc);

  controls["filter-state"].value = "ZZ";
  apply();

  assert.equal(visible(first.rows), 0);
  assert.equal(visible(second.rows), 0);
  assert.equal(controls["filter-count"].textContent, "0 of 4 cases match");
  assert.equal(first.empty.hidden, false);
  assert.equal(second.empty.hidden, false);
});

// --- the file itself ------------------------------------------------------------

test("app.js stays a plain browser script, not a module", () => {
  // It is loaded with a bare <script src>, so a top-level import/export would
  // break the page outright, and the page has no loader for require(). The Node
  // export has to stay behind its guard.
  const source = readFileSync(APP_PATH, "utf8");
  assert.match(source, /typeof module !== "undefined"/);
  assert.doesNotMatch(source, /^\s*(import|export)\s/m, "a module statement would break the page");
  assert.doesNotMatch(source, /\brequire\(/, "the page has no module loader");
});

