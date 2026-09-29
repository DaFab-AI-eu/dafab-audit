(function () {
  "use strict";
  var PAGE = 300;
  var STALE_HOURS = 36;
  var THEME_KEY = "dafab-audit-theme";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var ICONS = {
    run: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    code: '<path d="m18 16 4-4-4-4"/><path d="m6 8-4 4 4 4"/><path d="m14.5 4-5 16"/>',
    data: '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/><path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>'
  };
  var state = { summary: null, anomalies: [], shown: 0 };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "class") node.className = attrs[key];
      else if (key === "text") node.textContent = attrs[key];
      else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) { node.appendChild(typeof child === "string" ? document.createTextNode(child) : child); });
    return node;
  }
  function icon(name) {
    var holder = document.createElement("span");
    holder.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">' + ICONS[name] + "</svg>";
    return holder.firstChild;
  }
  function num(value) { return value === null || value === undefined ? "–" : Number(value).toLocaleString("en"); }
  function pad(value) { return value < 10 ? "0" + value : String(value); }
  function when(iso) {
    var date = iso ? new Date(iso) : null;
    if (!date || isNaN(date.getTime())) return iso || "–";
    return date.getUTCDate() + " " + MONTHS[date.getUTCMonth()] + " " + date.getUTCFullYear() + ", "
      + pad(date.getUTCHours()) + ":" + pad(date.getUTCMinutes()) + " UTC";
  }
  function pill(text, kind) { return el("span", { class: "pill " + kind, text: text }); }
  function ratio(part, whole) {
    if (!whole) return pill("–", "muted");
    var kind = part === whole ? "good" : (part / whole > 0.98 ? "warn" : "bad");
    return pill(num(part) + " / " + num(whole), kind);
  }
  function itemLinks(row, links) {
    var cell = el("td", { class: "id" });
    var stac = links.stac_root + "/collections/" + encodeURIComponent(row.collection) + "/items/" + encodeURIComponent(row.id);
    cell.appendChild(el("a", { href: stac, title: "Open the STAC item", target: "_blank", rel: "noopener", text: row.id }));
    if (row.scope === "dafab" && links.discovery) {
      cell.appendChild(document.createTextNode(" · "));
      cell.appendChild(el("a", { href: links.discovery + "?item=" + encodeURIComponent(row.id), title: "Open the item in discovery", target: "_blank", rel: "noopener", text: "discovery" }));
    }
    return cell;
  }
  function table(node, headers, rows) {
    node.textContent = "";
    var head = el("thead", {}, [el("tr", {}, headers.map(function (h) { return el("th", { text: h }); }))]);
    var body = el("tbody", {}, rows);
    node.appendChild(head); node.appendChild(body);
  }
  function emptyRow(text, span) { return el("tr", {}, [el("td", { class: "empty", colspan: String(span), text: text })]); }

  function gb(bytes) {
    if (!bytes) return "0 GB";
    if (bytes >= 1e12) return (bytes / 1e12).toFixed(2) + " TB";
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(bytes >= 1e11 ? 0 : 1) + " GB";
    return (bytes / 1e6).toFixed(bytes >= 1e8 ? 0 : 1) + " MB";
  }
  function setStat(key, value, detail, tone) {
    var card = document.querySelector('[data-total="' + key + '"]');
    if (!card) return;
    card.querySelector("b").textContent = value;
    var small = card.querySelector("small");
    if (detail) {
      if (!small) { small = el("small"); card.querySelector("div").appendChild(small); }
      small.textContent = detail;
    } else if (small) {
      small.remove();
    }
    card.classList.remove("is-good", "is-bad", "is-warn");
    if (tone) card.classList.add("is-" + tone);
  }
  function share(part, whole) { return whole ? (100 * part / whole).toFixed(part === whole ? 0 : 1) + "% of items" : ""; }
  function renderTotals(summary) {
    var totals = summary.totals;
    setStat("items", num(totals.items), summary.matrix.length + " collection groups");
    setStat("valid", num(totals.valid), share(totals.valid, totals.items), totals.valid === totals.items ? "good" : null);
    var failed = summary.matrix.reduce(function (sum, cell) { return sum + (cell.verify_failed || 0); }, 0);
    setStat("verified", num(totals.verified), failed ? num(failed) + " failed" : "None failed", failed ? "bad" : null);
    setStat("anomalies", num(totals.anomalies), share(totals.anomalies, totals.items), totals.anomalies ? "bad" : "good");
    setStat("surplus", gb(totals.surplus_bytes), totals.surplus_items ? "On " + num(totals.surplus_items) + " items" : "No surplus files", totals.surplus_bytes ? "warn" : "good");
  }

  function renderMatrix(summary) {
    var rows = summary.matrix.map(function (cell) {
      return el("tr", {}, [
        el("td", { text: cell.scope }), el("td", { text: cell.collection }), el("td", { text: cell.version }),
        el("td", { class: "num", text: num(cell.items) }),
        el("td", {}, [ratio(cell.valid, cell.items)]),
        el("td", {}, [ratio(cell.assets_complete, cell.inventoried)]),
        el("td", {}, [ratio(cell.available, cell.inventoried)]),
        el("td", { class: "num", text: num(cell.verified) + (cell.verify_failed ? " (" + cell.verify_failed + " failed)" : "") }),
        el("td", { class: "num", text: num(cell.changed) }),
        el("td", {}, [pill(num(cell.anomalies), cell.anomalies ? "bad" : "good")]),
        el("td", {}, [cell.surplus_items ? pill(num(cell.surplus_items) + " items · " + gb(cell.surplus_bytes), "warn") : pill("None", "good")])
      ]);
    });
    table(document.getElementById("matrix"), ["Scope", "Collection", "Version", "Items", "Valid", "Assets complete / inventoried", "In storage / inventoried", "Byte-verified", "Changed", "Findings", "Surplus files"], rows);
    var findings = summary.matrix.filter(function (cell) { return cell.top_issues && cell.top_issues.length; }).map(function (cell) {
      return el("tr", {}, [
        el("td", { text: cell.scope + " · " + cell.collection + " · " + cell.version }),
        el("td", { class: "issues" }, cell.top_issues.map(function (row, index) {
          return el("span", {}, [(index ? " · " : ""), el("b", { text: num(row.items) }), " " + row.issue]);
        }))
      ]);
    });
    var box = document.getElementById("findings");
    if (findings.length) { table(box, ["Group", "Most common findings, items affected"], findings); box.parentNode.hidden = false; }
    else { box.parentNode.hidden = true; }
  }

  function renderCoverage(summary) {
    var rows = summary.coverage.map(function (row) {
      return el("tr", {}, [
        el("td", { text: row.collection }), el("td", { text: row.version_tag }),
        el("td", { class: "num", text: num(row.products) }), el("td", { class: "num", text: num(row.images) }),
        el("td", {}, [ratio(row.images_with_product, row.images)]),
        el("td", { class: "num", text: num(row.images_without_product) })
      ]);
    });
    if (!rows.length) rows = [emptyRow("No products found", 6)];
    table(document.getElementById("coverage"), ["Use case", "Version tag", "Products", "Source images", "Images with a product", "Images without"], rows);
  }

  function filteredAnomalies() {
    var scope = document.getElementById("filter-scope").value;
    var collection = document.getElementById("filter-collection").value;
    var kind = document.getElementById("filter-kind").value;
    var id = document.getElementById("filter-id").value.trim().toLowerCase();
    return state.summary.anomalies.filter(function (row) {
      var kindOk = kind === "problems" ? row.kinds.some(function (k) { return k !== "surplus"; }) : (!kind || row.kinds.indexOf(kind) >= 0);
      return (!scope || row.scope === scope) && (!collection || row.collection === collection)
        && kindOk && (!id || row.id.toLowerCase().indexOf(id) >= 0);
    });
  }
  function renderAnomalies(reset) {
    var rows = filteredAnomalies();
    if (reset) state.shown = 0;
    state.shown = Math.min(rows.length, state.shown + PAGE);
    var links = state.summary.links || {};
    var body = rows.slice(0, state.shown).map(function (row) {
      return el("tr", {}, [
        el("td", { text: row.scope }), el("td", { text: row.collection }), el("td", { text: row.version }),
        itemLinks(row, links),
        el("td", {}, row.kinds.map(function (kind) { return pill(kind, kind === "bytes" ? "bad" : (kind === "surplus" ? "muted" : "warn")); })),
        el("td", { class: "issues", text: row.issues.join("; ") + (row.surplus_bytes ? " (" + gb(row.surplus_bytes) + ")" : "") })
      ]);
    });
    if (!body.length) body = [emptyRow("No findings match the filters", 6)];
    table(document.getElementById("anomalies"), ["Scope", "Collection", "Version", "Item", "Kind", "Details"], body);
    document.getElementById("anomaly-count").textContent = num(rows.length) + " of " + num(state.summary.anomalies.length) + " findings";
    document.getElementById("more").hidden = state.shown >= rows.length;
  }
  function fillFilters(summary) {
    var scopes = {}, collections = {};
    summary.anomalies.forEach(function (row) { scopes[row.scope] = true; collections[row.collection] = true; });
    Object.keys(scopes).sort().forEach(function (v) { document.getElementById("filter-scope").appendChild(el("option", { value: v, text: v })); });
    Object.keys(collections).sort().forEach(function (v) { document.getElementById("filter-collection").appendChild(el("option", { value: v, text: v })); });
  }

  function renderHistory(history) {
    var rows = history.slice().reverse().slice(0, 30).map(function (night) {
      var cells = night.cells || [];
      var byScope = {};
      cells.forEach(function (c) { byScope[c.scope] = (byScope[c.scope] || 0) + c.items; });
      return el("tr", {}, [
        el("td", { text: when(night.generated_at) }),
        el("td", { class: "num", text: num(night.totals.items) }),
        el("td", { class: "num", text: num(night.totals.valid) }),
        el("td", { class: "num", text: num(night.totals.verified) }),
        el("td", {}, [pill(num(night.totals.anomalies), night.totals.anomalies ? "bad" : "good")]),
        el("td", { class: "issues", text: Object.keys(byScope).sort().map(function (s) { return s + " " + num(byScope[s]); }).join(", ") })
      ]);
    });
    if (!rows.length) rows = [emptyRow("No earlier runs", 6)];
    table(document.getElementById("history"), ["Run", "Items", "Valid", "Byte-verified", "Findings", "Items per scope"], rows);
  }

  function renderRunState(summary) {
    var pillNode = document.getElementById("run-pill");
    var generated = summary.generated_at ? new Date(summary.generated_at) : null;
    var age = generated ? (Date.now() - generated.getTime()) / 3600000 : Infinity;
    pillNode.classList.toggle("is-current", age <= STALE_HOURS);
    pillNode.classList.toggle("is-stale", age > STALE_HOURS);
    document.getElementById("run-pill-text").textContent = "Updated " + when(summary.generated_at);
    document.getElementById("generated").textContent = "Last run " + when(summary.generated_at) + " · " + num(summary.totals.items) + " items in " + summary.matrix.length + " collection groups";
    var links = summary.links || {};
    var box = document.getElementById("links"); box.textContent = "";
    [["Run log", links.run, "run"], ["Source code", links.repository, "code"], ["Summary data", "data/summary.json", "data"]].forEach(function (entry) {
      if (!entry[1]) return;
      var external = /^https?:/.test(entry[1]);
      var attrs = { class: "secondary-button", href: entry[1] };
      if (external) { attrs.target = "_blank"; attrs.rel = "noopener"; }
      box.appendChild(el("a", attrs, [icon(entry[2]), entry[0]]));
    });
  }

  function load() {
    return fetch("data/summary.json", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (summary) {
      state.summary = summary;
      renderRunState(summary);
      renderTotals(summary); renderMatrix(summary); renderCoverage(summary); fillFilters(summary); renderAnomalies(true);
      return fetch("data/history.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : []; }).then(renderHistory);
    }).catch(function (error) {
      document.getElementById("generated").textContent = "The latest run could not be loaded (" + error + ").";
      document.getElementById("run-pill-text").textContent = "Data unavailable";
    });
  }

  function currentTheme() { return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light"; }
  function labelThemeButton() {
    document.getElementById("theme-toggle").setAttribute("aria-label", currentTheme() === "dark" ? "Use light theme" : "Use dark theme");
  }
  document.getElementById("theme-toggle").addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { window.localStorage.setItem(THEME_KEY, next); } catch (error) { /* the choice lasts for this page only */ }
    labelThemeButton();
  });
  labelThemeButton();

  var mobileNav = document.getElementById("mobile-nav");
  document.addEventListener("click", function (event) { if (mobileNav.open && !mobileNav.contains(event.target)) mobileNav.open = false; });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && mobileNav.open) { mobileNav.open = false; mobileNav.querySelector("summary").focus(); } });

  ["filter-scope", "filter-collection", "filter-kind"].forEach(function (id) { document.getElementById(id).addEventListener("change", function () { renderAnomalies(true); }); });
  document.getElementById("filter-id").addEventListener("input", function () { renderAnomalies(true); });
  document.getElementById("more").addEventListener("click", function () { renderAnomalies(false); });
  load();
})();
