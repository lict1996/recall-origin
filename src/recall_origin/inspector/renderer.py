"""Render an authorized Inspector payload as a self-contained HTML document.

The renderer deliberately performs no authorization or redaction. Callers must
only pass data that has already been authorized and made safe to disclose.
"""

from __future__ import annotations

import html
import json
from typing import Any


def _json_for_script(payload: dict[str, Any]) -> str:
    """Serialize JSON without allowing data to terminate its script element."""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_inspector(
    payload: dict[str, Any],
    *,
    title: str = "RecallOrigin Inspector",
) -> str:
    """Return one standalone, read-only Inspector HTML document.

    ``payload`` is expected to be a JSON-compatible dictionary whose optional
    top-level keys are ``timeline``, ``retrieval``, and ``benchmarks``. Missing
    or empty sections render explicit empty states; the renderer never invents
    benchmark values.
    """
    if not isinstance(payload, dict):
        msg = "payload must be a dict"
        raise TypeError(msg)

    safe_title = html.escape(title, quote=True)
    data = _json_for_script(payload)
    before_title, title_marker, after_title = _DOCUMENT.partition("__DOCUMENT_TITLE__")
    before_data, data_marker, after_data = after_title.partition("__INSPECTOR_PAYLOAD__")
    if not title_marker or not data_marker:  # pragma: no cover - developer invariant
        msg = "inspector document template is missing a marker"
        raise RuntimeError(msg)
    return before_title + safe_title + before_data + data + after_data


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';
                 img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none';
                 form-action 'none'">
  <title>__DOCUMENT_TITLE__</title>
  <style>
    :root {
      --paper: #f2f7fa;
      --surface: #ffffff;
      --ink: #142633;
      --muted: #5f707c;
      --line: #cbd9e1;
      --indigo: #4856b8;
      --teal: #087b82;
      --amber: #ad6412;
      --danger: #a23b44;
      --shadow: 0 18px 55px rgba(27, 51, 68, .1);
      --radius: 18px;
      --display: Charter, "Iowan Old Style", "Palatino Linotype", Georgia, serif;
      --body: "Avenir Next", Avenir, "Segoe UI", Helvetica, Arial, sans-serif;
      --mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }
    * { box-sizing: border-box; }
    html { background: var(--paper); color: var(--ink); }
    body {
      margin: 0;
      min-width: 280px;
      font: 15px/1.55 var(--body);
      background:
        linear-gradient(90deg, rgba(72, 86, 184, .045) 1px, transparent 1px) 0 0 / 42px 42px,
        linear-gradient(rgba(72, 86, 184, .045) 1px, transparent 1px) 0 0 / 42px 42px,
        var(--paper);
    }
    button { font: inherit; }
    .shell { width: min(1180px, calc(100% - 36px)); margin: 0 auto; padding: 42px 0 56px; }
    .masthead {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 32px;
      align-items: end;
      padding: 4px 2px 28px;
    }
    .eyebrow {
      margin: 0 0 7px;
      color: var(--indigo);
      font: 700 11px/1.2 var(--mono);
      letter-spacing: .13em;
      text-transform: uppercase;
    }
    h1 {
      max-width: 750px;
      margin: 0;
      font: 600 clamp(34px, 6vw, 66px)/.94 var(--display);
      letter-spacing: -.045em;
    }
    .lede { max-width: 690px; margin: 18px 0 0; color: var(--muted); font-size: 16px; }
    .snapshot {
      display: grid;
      grid-template-columns: repeat(3, minmax(64px, auto));
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--line);
      box-shadow: var(--shadow);
    }
    .snapshot div { min-width: 86px; padding: 13px 14px 11px; background: var(--surface); }
    .snapshot strong { display: block; font: 600 20px/1 var(--display); }
    .snapshot span {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font: 700 9px/1.2 var(--mono);
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .workspace {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, .93);
      box-shadow: var(--shadow);
    }
    .tabs {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding: 8px;
      border-bottom: 1px solid var(--line);
      background: #e8f0f4;
    }
    .tab {
      display: inline-flex;
      flex: 0 0 auto;
      align-items: center;
      gap: 9px;
      min-height: 44px;
      padding: 9px 14px;
      border: 1px solid transparent;
      border-radius: 11px;
      color: #405361;
      background: transparent;
      cursor: pointer;
      font-weight: 650;
    }
    .tab:hover { color: var(--ink); background: rgba(255, 255, 255, .6); }
    .tab[aria-selected="true"] {
      border-color: var(--line);
      color: var(--ink);
      background: var(--surface);
      box-shadow: 0 4px 12px rgba(27, 51, 68, .07);
    }
    .tab:focus-visible, .panel:focus-visible {
      outline: 3px solid rgba(72, 86, 184, .35);
      outline-offset: 2px;
    }
    .badge {
      min-width: 23px;
      padding: 2px 6px;
      border-radius: 999px;
      color: var(--muted);
      background: #dce8ed;
      font: 700 10px/1.5 var(--mono);
      text-align: center;
    }
    .tab[aria-selected="true"] .badge { color: #fff; background: var(--indigo); }
    .panel { min-height: 470px; padding: clamp(22px, 4vw, 44px); }
    .panel[hidden] { display: none; }
    .section-head {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: start;
      margin-bottom: 28px;
    }
    h2 { margin: 0; font: 600 clamp(25px, 4vw, 36px)/1.05 var(--display); letter-spacing: -.025em; }
    .section-copy { max-width: 630px; margin: 8px 0 0; color: var(--muted); }
    .meta-line { margin: 4px 0 0; color: var(--muted); font: 12px/1.5 var(--mono); }
    .origin-rail { position: relative; display: grid; gap: 18px; padding-left: 33px; }
    .origin-rail::before {
      position: absolute;
      top: 17px;
      bottom: 17px;
      left: 10px;
      width: 2px;
      background: linear-gradient(var(--indigo), var(--teal));
      content: "";
    }
    .memory-card {
      position: relative;
      padding: 20px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface);
    }
    .memory-card::before {
      position: absolute;
      top: 24px;
      left: -29px;
      width: 11px;
      height: 11px;
      border: 3px solid var(--surface);
      outline: 2px solid var(--indigo);
      background: var(--indigo);
      content: "";
      transform: rotate(45deg);
    }
    .card-top, .candidate-top, .metric-top {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
    }
    .kind, .status, .selected {
      display: inline-block;
      padding: 3px 7px;
      border-radius: 5px;
      background: #e7eafd;
      color: #39459b;
      font: 700 10px/1.4 var(--mono);
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .status { background: #e2f1f1; color: #07666c; }
    .selected { background: #dcefeb; color: #086457; }
    .card-title { margin: 9px 0 4px; font: 650 18px/1.3 var(--body); }
    .summary { margin: 0; color: #3f5360; white-space: pre-wrap; }
    .identifier, .timestamp {
      color: var(--muted);
      font: 11px/1.45 var(--mono);
      overflow-wrap: anywhere;
    }
    .timestamp { text-align: right; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
    .chip {
      max-width: 100%;
      padding: 3px 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font: 11px/1.45 var(--mono);
      overflow-wrap: anywhere;
    }
    .query-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 22px;
      padding: 20px;
      border-left: 4px solid var(--indigo);
      border-radius: 0 12px 12px 0;
      background: #edf0fd;
    }
    .query-label {
      margin: 0 0 4px;
      color: var(--indigo);
      font: 700 10px/1.2 var(--mono);
      text-transform: uppercase;
    }
    .query { margin: 0; font: 600 18px/1.4 var(--body); white-space: pre-wrap; }
    .candidate-list { display: grid; gap: 12px; }
    .candidate {
      display: grid;
      grid-template-columns: 52px minmax(0, 1fr);
      gap: 16px;
      padding: 17px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface);
    }
    .rank {
      display: grid;
      place-items: center;
      width: 45px;
      height: 45px;
      border-radius: 50%;
      color: var(--indigo);
      background: #e7eafd;
      font: 700 15px/1 var(--mono);
    }
    .scores { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 12px; }
    .score { color: var(--muted); font: 11px/1.4 var(--mono); }
    .score strong { color: var(--ink); }
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .metric-card {
      padding: 20px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface);
    }
    .metric-name { margin: 0; font-weight: 700; }
    .metric-values { display: grid; gap: 11px; margin-top: 18px; }
    .metric-row {
      display: grid;
      grid-template-columns: 100px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .metric-label { color: var(--muted); font: 11px/1.3 var(--mono); overflow-wrap: anywhere; }
    .bar-track { height: 8px; overflow: hidden; border-radius: 99px; background: #e4edf1; }
    .bar { height: 100%; border-radius: inherit; background: var(--indigo); }
    .metric-row.candidate .bar { background: var(--teal); }
    .metric-value { min-width: 60px; font: 700 12px/1.3 var(--mono); text-align: right; }
    .notes {
      margin: 24px 0 0;
      padding: 17px 19px 17px 36px;
      border-radius: 12px;
      color: #5f4b31;
      background: #fbf0df;
    }
    .notes li + li { margin-top: 6px; }
    .empty {
      display: grid;
      place-items: center;
      min-height: 300px;
      padding: 45px 24px;
      border: 1px dashed #aebfc9;
      border-radius: 14px;
      text-align: center;
      background:
        repeating-linear-gradient(-45deg, #fbfdfe, #fbfdfe 10px, #f6fafc 10px, #f6fafc 20px);
    }
    .empty-mark {
      width: 42px;
      height: 42px;
      margin: 0 auto 16px;
      border: 2px solid var(--line);
      transform: rotate(45deg);
    }
    .empty h3 { margin: 0; font: 600 21px/1.25 var(--display); }
    .empty p { max-width: 480px; margin: 8px auto 0; color: var(--muted); }
    .footnote { margin: 16px 2px 0; color: var(--muted); font: 11px/1.55 var(--mono); }
    @media (max-width: 800px) {
      .shell { width: min(100% - 24px, 1180px); padding-top: 24px; }
      .masthead { grid-template-columns: 1fr; gap: 22px; }
      .snapshot { width: 100%; }
      .metric-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      .shell { width: min(100% - 16px, 1180px); }
      .panel { min-height: 420px; padding: 21px 16px 28px; }
      .section-head, .card-top, .candidate-top { display: block; }
      .timestamp { margin-top: 8px; text-align: left; }
      .query-card { grid-template-columns: 1fr; }
      .candidate { grid-template-columns: 38px minmax(0, 1fr); gap: 10px; }
      .rank { width: 34px; height: 34px; font-size: 12px; }
      .metric-row { grid-template-columns: 76px minmax(0, 1fr); }
      .metric-value { grid-column: 2; text-align: left; }
      .snapshot div { min-width: 0; padding: 11px 9px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
    }
    @media print {
      body { background: #fff; font-size: 11pt; }
      .shell { width: 100%; padding: 0; }
      .masthead { padding-bottom: 18px; }
      .workspace { border: 0; box-shadow: none; }
      .tabs { display: none; }
      .panel, .panel[hidden] {
        display: block !important;
        min-height: 0;
        padding: 24px 0;
        break-before: page;
      }
      .panel:first-of-type { break-before: auto; }
      .memory-card, .candidate, .metric-card { break-inside: avoid; box-shadow: none; }
      .footnote { display: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="masthead">
      <div>
        <p class="eyebrow">RecallOrigin / read-only inspector</p>
        <h1>Follow every memory back to its origin.</h1>
        <p class="lede">Inspect what changed, why retrieval ranked it, and what a measured
          benchmark actually observed. This view cannot modify memory.</p>
      </div>
      <div class="snapshot" aria-label="Inspector snapshot counts">
        <div><strong id="timeline-count">0</strong><span>Events</span></div>
        <div><strong id="candidate-count">0</strong><span>Candidates</span></div>
        <div><strong id="metric-count">0</strong><span>Metrics</span></div>
      </div>
    </header>

    <section class="workspace" aria-label="Memory inspection workspace">
      <div class="tabs" role="tablist" aria-label="Inspector views">
        <button class="tab" id="tab-timeline" role="tab" aria-selected="true"
                aria-controls="panel-timeline" type="button">
          Memory Timeline <span class="badge" id="badge-timeline">0</span>
        </button>
        <button class="tab" id="tab-retrieval" role="tab" aria-selected="false"
                aria-controls="panel-retrieval" tabindex="-1" type="button">
          Retrieval Explain <span class="badge" id="badge-retrieval">0</span>
        </button>
        <button class="tab" id="tab-benchmarks" role="tab" aria-selected="false"
                aria-controls="panel-benchmarks" tabindex="-1" type="button">
          Benchmark Compare <span class="badge" id="badge-benchmarks">0</span>
        </button>
      </div>

      <section class="panel" id="panel-timeline" role="tabpanel"
               aria-labelledby="tab-timeline" tabindex="0"></section>
      <section class="panel" id="panel-retrieval" role="tabpanel"
               aria-labelledby="tab-retrieval" tabindex="0" hidden></section>
      <section class="panel" id="panel-benchmarks" role="tabpanel"
               aria-labelledby="tab-benchmarks" tabindex="0" hidden></section>
    </section>
    <p class="footnote" id="generated-at">Authorized snapshot · no mutation controls</p>
  </main>

  <script type="application/json" id="inspector-data">__INSPECTOR_PAYLOAD__</script>
  <script>
    "use strict";
    const dataNode = document.getElementById("inspector-data");
    const data = JSON.parse(dataNode.textContent);

    const asObject = (value) =>
      value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const asArray = (value) => Array.isArray(value) ? value : [];
    const present = (value) => value !== undefined && value !== null && value !== "";
    const text = (value, fallback = "") => present(value) ? String(value) : fallback;

    function element(tag, className, value) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (present(value)) node.textContent = text(value);
      return node;
    }

    function appendText(parent, tag, className, value, fallback) {
      const node = element(tag, className, text(value, fallback));
      parent.appendChild(node);
      return node;
    }

    function emptyState(title, message) {
      const box = element("div", "empty");
      const body = element("div");
      body.appendChild(element("div", "empty-mark"));
      appendText(body, "h3", "", title);
      appendText(body, "p", "", message);
      box.appendChild(body);
      return box;
    }

    function sectionHeader(title, copy) {
      const head = element("div", "section-head");
      const body = element("div");
      appendText(body, "h2", "", title);
      appendText(body, "p", "section-copy", copy);
      head.appendChild(body);
      return head;
    }

    function addChips(parent, values) {
      const clean = asArray(values).filter(present);
      if (!clean.length) return;
      const chips = element("div", "chips");
      clean.forEach((value) => appendText(chips, "span", "chip", value));
      parent.appendChild(chips);
    }

    function renderTimeline() {
      const panel = document.getElementById("panel-timeline");
      const records = asArray(data.timeline);
      panel.appendChild(sectionHeader(
        "Memory Timeline",
        "A chronological ledger of memory formation, revision, evidence, and governance events."
      ));
      if (!records.length) {
        panel.appendChild(emptyState(
          "No memory events in this snapshot",
          "Capture or import authorized timeline events to inspect their origin " +
            "and revision history."
        ));
        return;
      }
      const rail = element("div", "origin-rail");
      records.forEach((recordValue) => {
        const record = asObject(recordValue);
        const card = element("article", "memory-card");
        const top = element("div", "card-top");
        const left = element("div");
        appendText(left, "span", "kind", record.kind, "event");
        appendText(left, "h3", "card-title", record.title, "Untitled memory event");
        if (present(record.claim_id)) appendText(left, "div", "identifier", record.claim_id);
        top.appendChild(left);
        appendText(top, "time", "timestamp", record.timestamp, "Time not provided");
        card.appendChild(top);
        if (present(record.summary)) appendText(card, "p", "summary", record.summary);
        const labels = [];
        if (present(record.status)) labels.push("status: " + text(record.status));
        asArray(record.evidence).forEach((item) => labels.push("evidence: " + text(item)));
        addChips(card, labels);
        rail.appendChild(card);
      });
      panel.appendChild(rail);
    }

    function appendScore(parent, label, value) {
      if (!present(value)) return;
      const score = element("span", "score");
      appendText(score, "span", "", label + " ");
      appendText(score, "strong", "", value);
      parent.appendChild(score);
    }

    function renderRetrieval() {
      const panel = document.getElementById("panel-retrieval");
      const retrieval = asObject(data.retrieval);
      const candidates = asArray(retrieval.candidates);
      panel.appendChild(sectionHeader(
        "Retrieval Explain",
        "See the query, ranking signals, selection outcome, and evidence references " +
          "returned by retrieval."
      ));
      const hasQuery = present(retrieval.query) ||
        present(retrieval.strategy) ||
        present(retrieval.latency_ms);
      if (hasQuery) {
        const queryCard = element("div", "query-card");
        const queryBody = element("div");
        appendText(queryBody, "p", "query-label", "Query");
        appendText(queryBody, "p", "query", retrieval.query, "Query text withheld");
        queryCard.appendChild(queryBody);
        const meta = element("p", "meta-line");
        const parts = [];
        if (present(retrieval.strategy)) parts.push("strategy: " + text(retrieval.strategy));
        if (present(retrieval.latency_ms)) {
          parts.push("latency: " + text(retrieval.latency_ms) + " ms");
        }
        meta.textContent = parts.join(" · ");
        queryCard.appendChild(meta);
        panel.appendChild(queryCard);
      }
      if (!candidates.length) {
        panel.appendChild(emptyState(
          "No retrieval candidates recorded",
          "Run retrieval with tracing enabled, then pass the authorized trace into this view."
        ));
        return;
      }
      const list = element("div", "candidate-list");
      candidates.forEach((candidateValue, index) => {
        const candidate = asObject(candidateValue);
        const card = element("article", "candidate");
        appendText(card, "div", "rank", candidate.rank, index + 1);
        const body = element("div");
        const top = element("div", "candidate-top");
        const titleWrap = element("div");
        appendText(titleWrap, "h3", "card-title", candidate.title, "Untitled candidate");
        if (present(candidate.claim_id)) {
          appendText(titleWrap, "div", "identifier", candidate.claim_id);
        }
        top.appendChild(titleWrap);
        if (candidate.selected === true) appendText(top, "span", "selected", "selected");
        body.appendChild(top);
        if (present(candidate.reason)) appendText(body, "p", "summary", candidate.reason);
        const scores = element("div", "scores");
        appendScore(scores, "score", candidate.score);
        appendScore(scores, "lexical", candidate.lexical_score);
        appendScore(scores, "vector", candidate.vector_score);
        appendScore(scores, "rrf", candidate.rrf_score);
        if (scores.childNodes.length) body.appendChild(scores);
        addChips(body, asArray(candidate.evidence_refs).map((value) => "evidence: " + text(value)));
        card.appendChild(body);
        list.appendChild(card);
      });
      panel.appendChild(list);
    }

    function finiteNumber(value) {
      if (typeof value === "number" && Number.isFinite(value)) return value;
      return null;
    }

    function displayValue(value, unit) {
      if (!present(value)) return "not measured";
      return text(value) + (present(unit) ? " " + text(unit) : "");
    }

    function renderBenchmarks() {
      const panel = document.getElementById("panel-benchmarks");
      const benchmarks = asObject(data.benchmarks);
      const metrics = asArray(benchmarks.metrics);
      panel.appendChild(sectionHeader(
        "Benchmark Compare",
        "Compare only supplied measurements. Missing values remain explicitly unmeasured."
      ));
      if (!metrics.length) {
        panel.appendChild(emptyState(
          "No benchmark measurements supplied",
          "This Inspector does not estimate results. Add measured baseline and " +
            "candidate metrics to compare them."
        ));
        return;
      }
      const baselineLabel = text(benchmarks.baseline_label, "Baseline");
      const candidateLabel = text(benchmarks.candidate_label, "Candidate");
      const grid = element("div", "metric-grid");
      metrics.forEach((metricValue) => {
        const metric = asObject(metricValue);
        const card = element("article", "metric-card");
        const top = element("div", "metric-top");
        appendText(top, "h3", "metric-name", metric.name, "Unnamed metric");
        if (present(metric.sample_size)) {
          appendText(top, "span", "chip", "n=" + text(metric.sample_size));
        }
        card.appendChild(top);
        const values = element("div", "metric-values");
        const numeric = [finiteNumber(metric.baseline), finiteNumber(metric.candidate)]
          .filter((value) => value !== null).map((value) => Math.abs(value));
        const maximum = numeric.length ? Math.max(...numeric, 0) : 0;
        [
          [baselineLabel, metric.baseline, "baseline"],
          [candidateLabel, metric.candidate, "candidate"]
        ].forEach(([label, value, className]) => {
          const row = element("div", "metric-row " + className);
          appendText(row, "span", "metric-label", label);
          const track = element("div", "bar-track");
          const bar = element("div", "bar");
          const measured = finiteNumber(value);
          const width = measured === null || maximum === 0 ? 0 : Math.abs(measured) / maximum * 100;
          bar.style.width = String(Math.max(0, Math.min(100, width))) + "%";
          track.appendChild(bar);
          row.appendChild(track);
          appendText(row, "span", "metric-value", displayValue(value, metric.unit));
          values.appendChild(row);
        });
        card.appendChild(values);
        if (present(metric.direction)) {
          appendText(card, "p", "meta-line", "Preferred direction: " + text(metric.direction));
        }
        grid.appendChild(card);
      });
      panel.appendChild(grid);
      const notes = asArray(benchmarks.notes).filter(present);
      if (notes.length) {
        const list = element("ul", "notes");
        notes.forEach((note) => appendText(list, "li", "", note));
        panel.appendChild(list);
      }
    }

    function setCount(countId, badgeId, value) {
      document.getElementById(countId).textContent = String(value);
      document.getElementById(badgeId).textContent = String(value);
    }

    const timelineCount = asArray(data.timeline).length;
    const candidateCount = asArray(asObject(data.retrieval).candidates).length;
    const metricCount = asArray(asObject(data.benchmarks).metrics).length;
    setCount("timeline-count", "badge-timeline", timelineCount);
    setCount("candidate-count", "badge-retrieval", candidateCount);
    setCount("metric-count", "badge-benchmarks", metricCount);

    if (present(data.generated_at)) {
      document.getElementById("generated-at").textContent =
        "Authorized snapshot generated " + text(data.generated_at) + " · no mutation controls";
    }

    renderTimeline();
    renderRetrieval();
    renderBenchmarks();

    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    function selectTab(tab) {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
      });
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => selectTab(tab));
      tab.addEventListener("keydown", (event) => {
        let next = null;
        if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
        if (event.key === "Home") next = tabs[0];
        if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          event.preventDefault();
          selectTab(next);
          next.focus();
        }
      });
    });
  </script>
</body>
</html>
"""
