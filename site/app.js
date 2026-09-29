(function () {
  "use strict";
  var PAGE = 300;
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
  function num(value) { return value === null || value === undefined ? "–" : Number(value).toLocaleString("en"); }
  function when(iso) { return iso ? iso.replace("T", " ").replace("Z", " UTC") : "–"; }
  function pill(text, kind) { return el("span", { class: "pill " + kind, text: text }); }
  function ratio(part, whole) {
    if (!whole) return pill("–", "muted");
    var kind = part === whole ? "good" : (part / whole > 0.98 ? "warn" : "bad");
    return pill(num(part) + " / " + num(whole), kind);
  }
  function itemLinks(row, links) {
    var cell = el("td", { class: "id" });
    var stac = links.stac_root + "/collections/" + encodeURIComponent(row.collection) + "/items/" + encodeURIComponent(row.id);
    cell.appendChild(el("a", { href: stac, title: "STAC item", target: "_blank", rel: "noopener", text: row.id }));
    if (row.scope === "dafab" && links.discovery) {
      cell.appendChild(document.createTextNode(" "));
      cell.appendChild(el("a", { href: links.discovery + "?item=" + encodeURIComponent(row.id), target: "_blank", rel: "noopener", text: "discover" }));
    }
    return cell;
  }
  function table(node, headers, rows) {
    node.textContent = "";
    var head = el("thead", {}, [el("tr", {}, headers.map(function (h) { return el("th", { text: h }); }))]);
    var body = el("tbody", {}, rows);
    node.appendChild(head); node.appendChild(body);
  }

  function renderTotals(summary) {
    var totals = summary.totals;
    var box = document.getElementById("totals"); box.textContent = "";
    [["items audited", totals.items], ["valid documents", totals.valid], ["byte-verified so far", totals.verified], ["items with an anomaly", totals.anomalies]]
      .forEach(function (pair) { box.appendChild(el("div", { class: "total" }, [el("b", { text: num(pair[1]) }), el("span", { text: pair[0] })])); });
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
        el("td", {}, [pill(num(cell.anomalies), cell.anomalies ? "bad" : "good")])
      ]);
    });
    table(document.getElementById("matrix"), ["scope", "collection", "version", "items", "valid", "assets complete / inventoried", "in storage / inventoried", "byte-verified", "changed tonight", "anomalies"], rows);
    var findings = summary.matrix.filter(function (cell) { return cell.top_issues && cell.top_issues.length; }).map(function (cell) {
      return el("tr", {}, [
        el("td", { text: cell.scope + " · " + cell.collection + " · " + cell.version }),
        el("td", { class: "issues" }, cell.top_issues.map(function (row, index) {
          return el("span", {}, [(index ? " · " : ""), el("b", { text: num(row.items) }), " " + row.issue]);
        }))
      ]);
    });
    var box = document.getElementById("findings");
    if (findings.length) { table(box, ["cell", "most common findings, items affected"], findings); box.parentNode.hidden = false; }
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
    if (!rows.length) rows = [el("tr", {}, [el("td", { text: "no products found" })])];
    table(document.getElementById("coverage"), ["use case", "version tag", "products", "source images", "images with a product", "images without"], rows);
  }

  function filteredAnomalies() {
    var scope = document.getElementById("filter-scope").value;
    var collection = document.getElementById("filter-collection").value;
    var kind = document.getElementById("filter-kind").value;
    var id = document.getElementById("filter-id").value.trim().toLowerCase();
    return state.summary.anomalies.filter(function (row) {
      return (!scope || row.scope === scope) && (!collection || row.collection === collection)
        && (!kind || row.kinds.indexOf(kind) >= 0) && (!id || row.id.toLowerCase().indexOf(id) >= 0);
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
        el("td", {}, row.kinds.map(function (kind) { return pill(kind, kind === "bytes" ? "bad" : "warn"); })),
        el("td", { class: "issues", text: row.issues.join("; ") })
      ]);
    });
    if (!body.length) body = [el("tr", {}, [el("td", { text: "no anomalies match" })])];
    table(document.getElementById("anomalies"), ["scope", "collection", "version", "item", "kind", "what the audit found"], body);
    document.getElementById("anomaly-count").textContent = num(rows.length) + " of " + num(state.summary.anomalies.length) + " anomalies";
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
    if (!rows.length) rows = [el("tr", {}, [el("td", { text: "first night" })])];
    table(document.getElementById("history"), ["run", "items", "valid", "byte-verified", "anomalies", "items per scope"], rows);
  }

  function load() {
    return fetch("data/summary.json", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (summary) {
      state.summary = summary;
      document.getElementById("generated").textContent = "Last run " + when(summary.generated_at) + ". " + num(summary.totals.items) + " items across " + summary.matrix.length + " scope, collection and version cells.";
      var links = summary.links || {};
      var linkBox = document.getElementById("links"); linkBox.textContent = "";
      [["discovery site", links.discovery], ["STAC root", links.stac_root], ["repository", links.repository], ["this run", links.run]].forEach(function (pair, index) {
        if (!pair[1]) return;
        if (linkBox.childNodes.length) linkBox.appendChild(document.createTextNode(" · "));
        linkBox.appendChild(el("a", { href: pair[1], target: "_blank", rel: "noopener", text: pair[0] }));
      });
      renderTotals(summary); renderMatrix(summary); renderCoverage(summary); fillFilters(summary); renderAnomalies(true);
      return fetch("data/history.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : []; }).then(renderHistory);
    }).catch(function (error) {
      document.getElementById("generated").textContent = "The run data could not be loaded (" + error + ").";
    });
  }

  ["filter-scope", "filter-collection", "filter-kind"].forEach(function (id) { document.getElementById(id).addEventListener("change", function () { renderAnomalies(true); }); });
  document.getElementById("filter-id").addEventListener("input", function () { renderAnomalies(true); });
  document.getElementById("more").addEventListener("click", function () { renderAnomalies(false); });
  load();
})();
