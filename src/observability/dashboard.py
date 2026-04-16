"""
Web Dashboard â€“ Sprint 4 of FlowDelta.

A lightweight FastAPI web application that serves an interactive
delta visualisation dashboard. Features:

* **Flow list** â€” all recorded flows with run counts
* **Trace viewer** â€” snapshot-by-snapshot state changes for any run
* **Delta diff view** â€” side-by-side variable changes with colour coding
* **Trend chart** â€” Chart.js line chart of change volume over time
* **Invariant panel** â€” list of detected invariants for a flow
* **REST API** â€” JSON endpoints for all data (consumable by external tools)

The dashboard runs entirely in-process (no database server needed) and
reads directly from the :class:`DeltaStore`.

Usage::

    from src.observability import DeltaDashboard
    from src.delta_engine import DeltaStore

    store = DeltaStore(store_path=".flowdelta/runs")
    dashboard = DeltaDashboard(store)
    dashboard.run(host="127.0.0.1", port=8765)
    # â†’ open http://localhost:8765

Or from the CLI::

    flowdelta dashboard --port 8765

Requires ``fastapi`` + ``uvicorn``::

    pip install "flowdelta[dashboard]"
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..delta_engine.delta_store import DeltaStore
from .trend_chart import TrendChartGenerator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inline HTML / CSS / JS for the dashboard (single-file, no build step)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>FlowDelta Dashboard</title>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body, #root { height: 100%; }
    body {
      background: #0d0f18;
      color: #dde1f5;
      font-family: Inter, system-ui, -apple-system, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #2a2f52; border-radius: 99px; }
    ::-webkit-scrollbar-thumb:hover { background: #4a5280; }

    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes shimmer {
      from { background-position: -600px 0; }
      to   { background-position:  600px 0; }
    }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(12px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to   { transform: translateX(0);    opacity: 1; }
    }
    .fade-up { animation: fadeUp 0.24s ease both; }

    #boot {
      position: fixed; inset: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      background: #0d0f18; color: #4a5280; gap: 16px; font-size: 14px;
      font-family: Inter, system-ui, sans-serif;
    }
    .spin-ring {
      width: 38px; height: 38px;
      border: 3px solid #2a2f52; border-top-color: #6c8bff;
      border-radius: 50%; animation: spin 0.75s linear infinite;
    }
    .fd-tr-hover { transition: background 0.12s; cursor: pointer; }
    .val-pre {
      background: #09090f; border: 1px solid #2a2f52;
      border-radius: 6px; padding: 6px 10px;
      font-family: 'JetBrains Mono', monospace; font-size: 12px;
      color: #8892c0; white-space: pre-wrap; word-break: break-all;
      max-width: 230px; max-height: 80px; overflow: auto; margin: 0;
    }
    .val-pre.is-new { color: #34d399; }
  </style>
</head>
<body>
  <div id="root">
    <div id="boot">
      <div class="spin-ring"></div>
      <span>Initialising FlowDelta...</span>
    </div>
  </div>

  <script type="text/babel">
    const { useState, useEffect, useRef, useCallback } = React;

    /* --- Colour tokens ------------------------------------ */
    const C = {
      bg:      "#0d0f18",
      sidebar: "#0a0c1e",
      surface: "#13162a",
      s2:      "#1a1e36",
      border:  "#2a2f52",
      primary: "#6c8bff",
      accent:  "#a78bfa",
      success: "#34d399",
      danger:  "#f87171",
      warn:    "#fbbf24",
      muted:   "#4a5280",
      soft:    "#8892c0",
      text:    "#dde1f5",
    };

    /* --- API helper --------------------------------------- */
    async function apiFetch(path) {
      const r = await fetch(path);
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    }

    /* --- Utilities ---------------------------------------- */
    const fmt = iso => iso ? iso.slice(0, 19).replace("T", " ") : "--";

    const shortVal = v => {
      if (v === null || v === undefined) return "null";
      const s = typeof v === "string" ? v : JSON.stringify(v, null, 2);
      return s.length > 200 ? s.slice(0, 200) + "..." : s;
    };

    /* Badge config - ASCII-only icons */
    const BADGE_CFG = {
      added:        { color: "#34d399", bg: "rgba(52,211,153,.13)",  icon: "[+]" },
      removed:      { color: "#f87171", bg: "rgba(248,113,113,.13)", icon: "[-]" },
      changed:      { color: "#6c8bff", bg: "rgba(108,139,255,.15)", icon: "[~]" },
      type_changed: { color: "#a78bfa", bg: "rgba(167,139,250,.15)", icon: "[T]" },
      golden:       { color: "#fbbf24", bg: "rgba(251,191,36,.14)",  icon: "[G]" },
    };

    /* --- Badge -------------------------------------------- */
    function Badge({ type }) {
      const b = BADGE_CFG[type] || { color: C.soft, bg: "rgba(100,100,100,.1)", icon: "" };
      return (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "3px 9px", borderRadius: 99,
          fontSize: 12, fontWeight: 600,
          color: b.color, background: b.bg,
        }}>
          <span style={{ fontFamily: "monospace", fontWeight: 700 }}>{b.icon}</span>
          {" "}{type.replace("_", " ")}
        </span>
      );
    }

    /* --- StatCard ----------------------------------------- */
    function StatCard({ label2, value, label, accent = false, onClick }) {
      const [hov, setHov] = useState(false);
      const active = hov && !!onClick;
      return (
        <div
          onClick={onClick}
          onMouseEnter={() => setHov(true)}
          onMouseLeave={() => setHov(false)}
          style={{
            background: C.surface,
            border: "1px solid " + (active ? C.primary : C.border),
            borderRadius: 12, padding: "18px 20px",
            display: "flex", flexDirection: "column", gap: 7,
            transition: "border-color .18s, box-shadow .18s, transform .12s",
            boxShadow: active ? "0 0 0 1px " + C.primary + "40" : "none",
            cursor: onClick ? "pointer" : "default",
            transform: active ? "translateY(-2px)" : "none",
            position: "relative",
          }}
        >
          <span style={{
            fontSize: 11, fontWeight: 700, textTransform: "uppercase",
            letterSpacing: "0.07em", color: C.muted,
          }}>
            {label2}
          </span>
          <span style={{
            fontSize: 30, fontWeight: 800, lineHeight: 1,
            color: accent ? C.accent : C.primary,
            letterSpacing: "-0.02em",
          }}>
            {value}
          </span>
          <span style={{
            fontSize: 11, fontWeight: 500, color: C.muted,
          }}>
            {label}
          </span>
          {onClick && (
            <span style={{
              position: "absolute", top: 10, right: 12,
              fontSize: 10, color: active ? C.primary : C.muted,
              fontFamily: "monospace", transition: "color .15s",
            }}>details &rsaquo;</span>
          )}
        </div>
      );
    }

    /* --- StatDrawer --------------------------------------- */
    function StatDrawer({ title, badge2, onClose, children }) {
      useEffect(() => {
        const onKey = e => e.key === "Escape" && onClose();
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
      }, [onClose]);
      return (
        <>
          {/* overlay */}
          <div
            onClick={onClose}
            style={{
              position: "fixed", inset: 0, background: "rgba(0,0,0,.55)",
              zIndex: 100, backdropFilter: "blur(2px)",
            }}
          />
          {/* panel */}
          <div style={{
            position: "fixed", top: 0, right: 0, bottom: 0, width: 480,
            background: C.sidebar, borderLeft: "1px solid " + C.border,
            zIndex: 101, display: "flex", flexDirection: "column",
            boxShadow: "-12px 0 48px rgba(0,0,0,.5)",
            animation: "slideIn .22s cubic-bezier(.22,.61,.36,1)",
          }}>
            {/* drawer header */}
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "18px 22px", borderBottom: "1px solid " + C.border, flexShrink: 0,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {badge2 && (
                  <span style={{
                    background: C.s2, border: "1px solid " + C.border,
                    borderRadius: 5, padding: "2px 8px",
                    fontSize: 11, fontFamily: "monospace", color: C.primary,
                  }}>{badge2}</span>
                )}
                <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{title}</span>
              </div>
              <button
                onClick={onClose}
                style={{
                  background: C.s2, border: "1px solid " + C.border,
                  color: C.soft, borderRadius: 7, padding: "5px 12px",
                  cursor: "pointer", fontSize: 13,
                }}
              >x close</button>
            </div>
            {/* drawer body */}
            <div style={{ flex: 1, overflowY: "auto", padding: "20px 22px" }}>
              {children}
            </div>
          </div>
        </>
      );
    }

    /* --- Skeleton loader ---------------------------------- */
    function Skel({ w = "100%", h = 14, r = 6, style = {} }) {
      return (
        <div style={{
          width: w, height: h, borderRadius: r,
          background: "linear-gradient(90deg, " + C.surface + " 25%, " + C.s2 + " 50%, " + C.surface + " 75%)",
          backgroundSize: "600px 100%", animation: "shimmer 1.4s infinite",
          ...style,
        }} />
      );
    }

    /* --- Empty state -------------------------------------- */
    function EmptyState({ symbol = "~", title, sub }) {
      return (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", gap: 14, minHeight: 340, color: C.muted,
        }}>
          <div style={{
            width: 72, height: 72, borderRadius: 18,
            background: "rgba(108,139,255,.08)", border: "1px solid " + C.border,
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 30, fontWeight: 700, color: C.muted, fontFamily: "monospace",
          }}>
            {symbol}
          </div>
          <div style={{ fontSize: 17, fontWeight: 700, color: C.soft }}>{title}</div>
          {sub && <div style={{ fontSize: 14, textAlign: "center", maxWidth: 380, lineHeight: 1.7 }}>{sub}</div>}
        </div>
      );
    }

    /* --- Panel card --------------------------------------- */
    function Panel({ title, badge2, right, noPad = false, children }) {
      return (
        <div style={{
          background: C.surface, border: "1px solid " + C.border,
          borderRadius: 12, overflow: "hidden",
        }}>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "13px 20px", borderBottom: "1px solid " + C.border,
          }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 10,
              fontSize: 12, fontWeight: 700, textTransform: "uppercase",
              letterSpacing: "0.08em", color: C.muted,
            }}>
              {badge2 && (
                <span style={{
                  background: C.s2, border: "1px solid " + C.border,
                  borderRadius: 5, padding: "2px 7px",
                  fontSize: 11, fontFamily: "monospace", color: C.primary,
                }}>
                  {badge2}
                </span>
              )}
              {title}
            </div>
            {right && <div style={{ fontSize: 12, color: C.muted }}>{right}</div>}
          </div>
          {noPad ? children : <div style={{ padding: "16px 20px" }}>{children}</div>}
        </div>
      );
    }

    /* --- Chart.js wrapper --------------------------------- */
    function TrendChart({ points }) {
      const canvasRef = useRef(null);
      const chartRef  = useRef(null);

      useEffect(() => {
        if (!canvasRef.current) return;
        if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
        if (!points || points.length === 0) return;

        chartRef.current = new Chart(canvasRef.current.getContext("2d"), {
          type: "line",
          data: {
            labels: points.map(p => p.run_id.slice(0, 7)),
            datasets: [{
              label: "Changes",
              data:  points.map(p => p.total_changes),
              borderColor:          C.primary,
              backgroundColor:      "rgba(108,139,255,0.1)",
              pointBackgroundColor: points.map(p => p.golden ? C.warn : C.primary),
              pointBorderColor:     points.map(p => p.golden ? C.warn : C.primary),
              pointRadius: 6, pointHoverRadius: 9,
              tension: 0.4, fill: true, borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
              legend: { display: false },
              tooltip: {
                backgroundColor: "#1a1e36", borderColor: C.border, borderWidth: 1,
                titleColor: C.text, bodyColor: C.soft, padding: 12,
                callbacks: {
                  title: i => "Run " + i[0].label,
                  label: i => "  " + i.parsed.y + " change" + (i.parsed.y !== 1 ? "s" : ""),
                  afterBody: i => points[i[0].dataIndex].golden ? ["  [G] golden baseline"] : [],
                },
              },
            },
            scales: {
              x: {
                grid:  { color: "rgba(42,47,82,0.5)" },
                ticks: { color: C.muted, font: { size: 11 } },
              },
              y: {
                beginAtZero: true,
                grid:  { color: "rgba(42,47,82,0.5)" },
                ticks: { color: C.muted, font: { size: 11 }, precision: 0 },
              },
            },
          },
        });

        return () => {
          if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
        };
      }, [points]);

      if (!points || points.length === 0) {
        return (
          <div style={{ textAlign: "center", color: C.muted, padding: "40px 0", fontSize: 14 }}>
            No trend data yet. Run the pipeline more times to populate this chart.
          </div>
        );
      }
      return <canvas ref={canvasRef} style={{ height: 240 }} />;
    }

    /* --- Table header cell -------------------------------- */
    const TH = ({ children }) => (
      <th style={{
        padding: "10px 18px", textAlign: "left",
        fontSize: 11, fontWeight: 700, textTransform: "uppercase",
        letterSpacing: "0.07em", color: C.muted,
        borderBottom: "1px solid " + C.border, whiteSpace: "nowrap",
        background: C.bg,
      }}>
        {children}
      </th>
    );

    /* --- Table data cell ---------------------------------- */
    const TD = ({ children, mono = false, last = false, style = {} }) => (
      <td style={{
        padding: "11px 18px",
        borderBottom: last ? "none" : "1px solid " + C.border,
        fontFamily: mono ? "'JetBrains Mono', monospace" : "inherit",
        fontSize: mono ? 13 : 14,
        verticalAlign: "top",
        ...style,
      }}>
        {children}
      </td>
    );

    /* --- Run ID chip -------------------------------------- */
    const RunChip = ({ id }) => (
      <span style={{
        fontFamily: "'JetBrains Mono', monospace", fontSize: 13,
        color: C.primary, background: "rgba(108,139,255,.1)",
        padding: "3px 9px", borderRadius: 6,
      }}>
        {id}
      </span>
    );

    /* --- Back button -------------------------------------- */
    function BackBtn({ onClick, label }) {
      const [hov, setHov] = useState(false);
      return (
        <button
          onClick={onClick}
          onMouseEnter={() => setHov(true)}
          onMouseLeave={() => setHov(false)}
          style={{
            display: "flex", alignItems: "center", gap: 6,
            background: hov ? C.border : C.s2,
            border: "1px solid " + C.border,
            color: hov ? C.text : C.soft,
            padding: "8px 16px", borderRadius: 8,
            cursor: "pointer", fontSize: 13, transition: "all .15s",
          }}
        >
          {"< Back to " + label}
        </button>
      );
    }

    /* --- Sidebar ------------------------------------------ */
    function Sidebar({ flows, selectedFlow, onSelect, storePath, loading }) {
      return (
        <aside style={{
          width: 260, flexShrink: 0, background: C.sidebar,
          borderRight: "1px solid " + C.border,
          display: "flex", flexDirection: "column",
          height: "100vh", overflow: "hidden",
        }}>
          {/* Logo */}
          <div style={{
            display: "flex", alignItems: "center", gap: 12,
            padding: "20px 18px 16px", borderBottom: "1px solid " + C.border,
          }}>
            <div style={{
              width: 38, height: 38, borderRadius: 10, flexShrink: 0,
              background: "linear-gradient(135deg, #6c8bff, #a78bfa)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 18, fontWeight: 800, color: "#fff",
              fontFamily: "monospace", userSelect: "none",
            }}>
              FD
            </div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15, color: C.text }}>FlowDelta</div>
              <div style={{ fontSize: 11, color: C.muted, letterSpacing: "0.04em" }}>
                State-delta engine
              </div>
            </div>
          </div>

          {/* Section label */}
          <div style={{
            padding: "14px 18px 8px",
            fontSize: 11, fontWeight: 700, textTransform: "uppercase",
            letterSpacing: "0.09em", color: C.muted,
          }}>
            Flows
          </div>

          {/* Flow list */}
          <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 10px" }}>
            {loading ? (
              <div style={{ padding: "4px 8px", display: "flex", flexDirection: "column", gap: 9 }}>
                <Skel w="80%" /><Skel w="62%" /><Skel w="74%" />
              </div>
            ) : flows.length === 0 ? (
              <div style={{ padding: "8px", fontSize: 13, color: C.muted }}>
                No flows recorded yet. Run the pipeline first.
              </div>
            ) : flows.map(f => {
              const active = selectedFlow === f.flow_id;
              return (
                <div key={f.flow_id}
                  onClick={() => onSelect(f.flow_id)}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "9px 12px 9px " + (active ? "9px" : "12px"),
                    borderRadius: 8, cursor: "pointer", marginBottom: 2,
                    fontSize: 14,
                    color:      active ? C.primary : C.soft,
                    background: active
                      ? "linear-gradient(90deg, rgba(108,139,255,.16), rgba(167,139,250,.06))"
                      : "transparent",
                    borderLeft: "3px solid " + (active ? C.primary : "transparent"),
                    transition: "all .15s",
                  }}
                >
                  <span>{f.flow_id}</span>
                  <span style={{
                    background: active ? "rgba(108,139,255,.2)" : C.s2,
                    color: active ? C.primary : C.muted,
                    fontSize: 11, padding: "2px 7px", borderRadius: 99,
                  }}>
                    {f.run_count}
                  </span>
                </div>
              );
            })}
          </div>

          {/* Footer */}
          <div style={{
            padding: "12px 18px", borderTop: "1px solid " + C.border,
            fontSize: 12, color: C.muted,
            display: "flex", alignItems: "center", gap: 8,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
              background: C.success, boxShadow: "0 0 6px " + C.success,
            }} />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {storePath || ".flowdelta/runs"}
            </span>
          </div>
        </aside>
      );
    }

    /* --- Welcome screen ----------------------------------- */
    function WelcomeScreen() {
      return (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", height: "100%", gap: 20, color: C.muted,
        }}>
          <div style={{
            width: 90, height: 90, borderRadius: 22,
            background: "rgba(108,139,255,.08)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 36, fontWeight: 800, color: C.primary,
            fontFamily: "monospace", border: "1px solid " + C.border,
          }}>
            FD
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, color: C.soft }}>
            Welcome to FlowDelta
          </div>
          <div style={{ fontSize: 14, textAlign: "center", maxWidth: 380, lineHeight: 1.8, color: C.muted }}>
            Select a flow from the sidebar to explore its delta history,
            regression trends, and variable-level change timeline.
          </div>
        </div>
      );
    }

    /* --- Inline bar ---------------------------------------- */
    function MiniBar({ value, max, color }) {
      const pct = max > 0 ? Math.round((value / max) * 100) : 0;
      return (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            flex: 1, height: 8, borderRadius: 99,
            background: C.s2, overflow: "hidden",
          }}>
            <div style={{
              height: "100%", width: pct + "%",
              background: color || C.primary,
              borderRadius: 99, transition: "width .4s ease",
            }} />
          </div>
          <span style={{
            fontSize: 13, fontWeight: 700, color: C.text,
            minWidth: 28, textAlign: "right",
          }}>{value}</span>
        </div>
      );
    }

    /* --- Flow overview ------------------------------------ */
    function FlowView({ flowId, onSelectRun }) {
      const [s, setS]           = useState({ loading: true, data: null, error: null });
      const [activeCard, setActiveCard] = useState(null);

      useEffect(() => {
        setS({ loading: true, data: null, error: null });
        Promise.all([
          apiFetch("/api/flows/" + flowId + "/runs"),
          apiFetch("/api/flows/" + flowId + "/trend"),
        ])
          .then(([runs, trend]) => setS({ loading: false, data: { runs, trend }, error: null }))
          .catch(e => setS({ loading: false, data: null, error: e.message }));
      }, [flowId]);

      if (s.loading) return (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Skel h={30} w="40%" r={8} />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 12 }}>
            {[...Array(5)].map((_, i) => <Skel key={i} h={100} r={12} />)}
          </div>
          <Skel h={290} r={12} />
          <Skel h={220} r={12} />
        </div>
      );

      if (s.error) return <EmptyState symbol="!" title="Failed to load flow" sub={s.error} />;

      const pts    = s.data.trend.points || [];
      const rl     = s.data.runs.runs    || [];
      const total  = pts.reduce((acc, p) => acc + p.total_changes, 0);
      const avgNum = pts.length ? total / pts.length : 0;
      const avg    = avgNum.toFixed(1);
      const goldenRuns = rl.filter(r => r.golden);
      const golden = goldenRuns.length;
      const peak   = pts.length ? Math.max(...pts.map(p => p.total_changes)) : 0;
      const peakRun = pts.find(p => p.total_changes === peak);
      const maxChg = peak;

      /* --- Drawer content builders ----------------------- */
      const DrawerRuns = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 12, lineHeight: 1.7 }}>
            All {rl.length} recorded run{rl.length !== 1 ? "s" : ""} for the <strong style={{ color: C.text }}>{flowId}</strong> flow, ordered by timestamp.
          </p>
          {rl.map((r, i) => {
            const chg = pts.find(p => p.run_id === r.run_id)?.total_changes ?? null;
            return (
              <div key={r.run_id} style={{
                background: C.surface, border: "1px solid " + C.border,
                borderRadius: 10, padding: "12px 16px",
                display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
              }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{
                      fontFamily: "monospace", fontSize: 13, color: C.primary,
                      background: "rgba(108,139,255,.1)", padding: "2px 8px", borderRadius: 5,
                    }}>#{i + 1} {r.run_id}</span>
                    {r.golden && <Badge type="golden" />}
                  </div>
                  <span style={{ fontSize: 12, color: C.muted }}>{fmt(r.saved_at)}</span>
                </div>
                <div style={{ textAlign: "right" }}>
                  {chg !== null
                    ? <span style={{ fontSize: 16, fontWeight: 700, color: chg > 0 ? C.primary : C.muted }}>{chg}</span>
                    : <span style={{ fontSize: 12, color: C.muted }}>no delta</span>}
                  <div style={{ fontSize: 11, color: C.muted }}>changes</div>
                </div>
              </div>
            );
          })}
        </div>
      );

      const DrawerAvg = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{
            background: C.surface, border: "1px solid " + C.border,
            borderRadius: 10, padding: "16px 20px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <span style={{ fontSize: 13, color: C.muted }}>Average changes per run</span>
            <span style={{ fontSize: 28, fontWeight: 800, color: C.primary }}>{avg}</span>
          </div>
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7 }}>
            Changes per run compared to the average ({avg}).
            Bars above average indicate regressions or more complex state changes.
          </p>
          {pts.map(p => (
            <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontFamily: "monospace", fontSize: 12, color: C.soft }}>{p.run_id}</span>
                <span style={{
                  fontSize: 11,
                  color: p.total_changes > avgNum ? C.warn : C.success,
                  fontWeight: 600,
                }}>
                  {p.total_changes > avgNum ? "above avg" : p.total_changes < avgNum ? "below avg" : "at avg"}
                </span>
              </div>
              <MiniBar
                value={p.total_changes}
                max={maxChg || 1}
                color={p.total_changes > avgNum ? C.warn : C.success}
              />
              {/* avg reference line */}
              <div style={{ position: "relative", height: 1, marginTop: -6 }}>
                <div style={{
                  position: "absolute",
                  left: Math.round((avgNum / (maxChg || 1)) * 100) + "%",
                  top: 0, width: 1, height: 8,
                  background: C.muted, opacity: 0.6,
                }} />
              </div>
            </div>
          ))}
          {pts.length === 0 && <div style={{ color: C.muted, fontSize: 13 }}>No trend data yet.</div>}
        </div>
      );

      const DrawerGolden = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 8, lineHeight: 1.7 }}>
            Golden runs are canonical baselines. Future runs are compared against the most recent golden run to detect regressions.
          </p>
          {goldenRuns.length === 0 && (
            <div style={{ color: C.muted, fontSize: 13 }}>No golden runs recorded yet.</div>
          )}
          {goldenRuns.map((r, i) => (
            <div key={r.run_id} style={{
              background: "rgba(251,191,36,.06)", border: "1px solid rgba(251,191,36,.25)",
              borderRadius: 10, padding: "14px 18px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <span style={{
                  fontFamily: "monospace", fontSize: 13, color: C.warn,
                  background: "rgba(251,191,36,.12)", padding: "2px 8px", borderRadius: 5,
                }}>baseline #{i + 1}</span>
                <span style={{
                  fontFamily: "monospace", fontSize: 13, color: C.text,
                }}>{r.run_id}</span>
              </div>
              <div style={{ fontSize: 12, color: C.muted, marginBottom: 4 }}>Recorded: {fmt(r.saved_at)}</div>
              {i === goldenRuns.length - 1 && (
                <div style={{
                  marginTop: 8, fontSize: 12, color: C.success, fontWeight: 600,
                }}>Active baseline - new runs compare against this</div>
              )}
            </div>
          ))}
        </div>
      );

      /* Build change-type totals across all pts */
      const DrawerTotal = () => {
        const [detail, setDetail] = useState(null);
        const typeTotals = {};
        pts.forEach(p => {
          Object.entries(p.change_counts || {}).forEach(([t, n]) => {
            typeTotals[t] = (typeTotals[t] || 0) + n;
          });
        });
        const hasBreakdown = Object.keys(typeTotals).length > 0;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{
              background: C.surface, border: "1px solid " + C.border,
              borderRadius: 10, padding: "16px 20px",
              display: "flex", alignItems: "center", justifyContent: "space-between",
            }}>
              <span style={{ fontSize: 13, color: C.muted }}>Total state changes across all runs</span>
              <span style={{ fontSize: 28, fontWeight: 800, color: C.primary }}>{total}</span>
            </div>
            {hasBreakdown && (
              <>
                <p style={{ fontSize: 13, color: C.muted }}>Change type breakdown across all runs:</p>
                {Object.entries(typeTotals).map(([t, n]) => (
                  <div key={t} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <Badge type={t} />
                      <span style={{ fontSize: 12, color: C.muted }}>{Math.round(n / total * 100)}%</span>
                    </div>
                    <MiniBar value={n} max={total} color={BADGE_CFG[t]?.color || C.primary} />
                  </div>
                ))}
              </>
            )}
            <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7, marginTop: 4 }}>
              Per-run breakdown:
            </p>
            {pts.map(p => (
              <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ fontFamily: "monospace", fontSize: 12, color: C.soft }}>{p.run_id}</span>
                  {p.golden && <Badge type="golden" />}
                </div>
                <MiniBar value={p.total_changes} max={maxChg || 1} color={C.primary} />
              </div>
            ))}
          </div>
        );
      };

      const DrawerPeak = () => (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{
            background: "rgba(167,139,250,.08)", border: "1px solid rgba(167,139,250,.3)",
            borderRadius: 10, padding: "16px 20px",
          }}>
            <div style={{ fontSize: 12, color: C.muted, marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.07em" }}>Peak run</div>
            {peakRun ? (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                  <span style={{
                    fontFamily: "monospace", fontSize: 14, color: C.accent,
                    background: "rgba(167,139,250,.12)", padding: "3px 10px", borderRadius: 6,
                  }}>{peakRun.run_id}</span>
                  {peakRun.golden && <Badge type="golden" />}
                </div>
                <span style={{ fontSize: 32, fontWeight: 800, color: C.accent }}>{peak}</span>
                <span style={{ fontSize: 13, color: C.muted, marginLeft: 8 }}>state changes</span>
              </>
            ) : (
              <span style={{ color: C.muted, fontSize: 13 }}>No data yet.</span>
            )}
          </div>
          <p style={{ fontSize: 13, color: C.muted, lineHeight: 1.7 }}>
            The peak run had the most state changes in a single execution.
            This often indicates a complex or edge-case code path was exercised.
          </p>
          <p style={{ fontSize: 13, color: C.muted, marginBottom: 6 }}>All runs vs peak ({peak}):</p>
          {pts.map(p => (
            <div key={p.run_id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontFamily: "monospace", fontSize: 12, color: p.run_id === peakRun?.run_id ? C.accent : C.soft }}>
                  {p.run_id}{p.run_id === peakRun?.run_id ? " [peak]" : ""}
                </span>
                <span style={{ fontSize: 11, color: C.muted }}>{p.total_changes} chg</span>
              </div>
              <MiniBar
                value={p.total_changes}
                max={maxChg || 1}
                color={p.run_id === peakRun?.run_id ? C.accent : C.primary}
              />
            </div>
          ))}
        </div>
      );

      const DRAWERS = {
        runs:    { title: "All Runs",           badge2: "RUNS",   content: <DrawerRuns /> },
        avg:     { title: "Average Changes",    badge2: "AVG",    content: <DrawerAvg /> },
        golden:  { title: "Golden Baselines",   badge2: "GOLDEN", content: <DrawerGolden /> },
        total:   { title: "Total Changes",      badge2: "TOTAL",  content: <DrawerTotal /> },
        peak:    { title: "Peak Run",           badge2: "PEAK",   content: <DrawerPeak /> },
      };

      return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          {/* Drawer */}
          {activeCard && (
            <StatDrawer
              title={DRAWERS[activeCard].title}
              badge2={DRAWERS[activeCard].badge2}
              onClose={() => setActiveCard(null)}
            >
              {DRAWERS[activeCard].content}
            </StatDrawer>
          )}

          {/* Header */}
          <div style={{ paddingBottom: 18, borderBottom: "1px solid " + C.border }}>
            <div style={{ fontSize: 23, fontWeight: 800, letterSpacing: "-0.02em" }}>{flowId}</div>
            <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>
              Flow overview &middot; {rl.length} run{rl.length !== 1 ? "s" : ""}
              <span style={{ marginLeft: 10, color: C.border, fontSize: 11 }}>Click any tile for details</span>
            </div>
          </div>

          {/* Stats grid */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(130px,1fr))", gap: 12 }}>
            <StatCard label2="Runs"          value={rl.length} label="recorded"   onClick={() => setActiveCard("runs")} />
            <StatCard label2="Avg Changes"   value={avg}       label="per run"    onClick={() => setActiveCard("avg")} />
            <StatCard label2="Golden Runs"   value={golden}    label="baselines"  onClick={() => setActiveCard("golden")} />
            <StatCard label2="Total Changes" value={total}     label="all runs"   onClick={() => setActiveCard("total")} />
            <StatCard label2="Peak"          value={peak}      label="single run" onClick={() => setActiveCard("peak")} accent />
          </div>

          {/* Trend chart */}
          <Panel title="Regression Trend" badge2="CHART">
            <div style={{ height: 260 }}>
              <TrendChart points={pts} />
            </div>
          </Panel>

          {/* Run history table */}
          <Panel title="Run History" badge2="TABLE" right="Click a row to inspect deltas" noPad>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr><TH>Run ID</TH><TH>Timestamp</TH><TH>Changes</TH><TH>Flag</TH></tr>
                </thead>
                <tbody>
                  {rl.length === 0 ? (
                    <tr>
                      <td colSpan={4} style={{ textAlign: "center", color: C.muted, padding: "2rem" }}>
                        No runs recorded yet.
                      </td>
                    </tr>
                  ) : rl.map((r, i) => {
                    const chg  = pts.find(p => p.run_id === r.run_id)?.total_changes ?? "--";
                    const last = i === rl.length - 1;
                    return (
                      <tr key={r.run_id}
                        className="fd-tr-hover"
                        onClick={() => onSelectRun(r.run_id)}
                        onMouseEnter={e => e.currentTarget.style.background = "rgba(108,139,255,.06)"}
                        onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                      >
                        <TD last={last}><RunChip id={r.run_id} /></TD>
                        <TD last={last} style={{ color: C.soft }}>{fmt(r.saved_at)}</TD>
                        <TD last={last} style={{
                          fontWeight: 600,
                          color: typeof chg === "number" && chg > 0 ? C.primary : C.muted,
                        }}>
                          {typeof chg === "number" ? chg + " change" + (chg !== 1 ? "s" : "") : chg}
                        </TD>
                        <TD last={last}>
                          {r.golden
                            ? <Badge type="golden" />
                            : <span style={{ color: C.muted }}>--</span>}
                        </TD>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      );
    }

    /* --- Run detail --------------------------------------- */
    function RunDetailView({ flowId, runId, onBack }) {
      const [s, setS] = useState({ loading: true, data: null, error: null });

      useEffect(() => {
        setS({ loading: true, data: null, error: null });
        apiFetch("/api/runs/" + runId + "/delta")
          .then(d  => setS({ loading: false, data: d,    error: null }))
          .catch(e => setS({ loading: false, data: null, error: e.message }));
      }, [runId]);

      if (s.loading) return (
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Skel h={32} w="50%" r={8} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Skel h={100} r={12} /><Skel h={100} r={12} />
          </div>
          <Skel h={400} r={12} />
        </div>
      );

      if (s.error) return <EmptyState symbol="!" title="Could not load run" sub={s.error} />;

      /* Golden baseline run — no delta was computed (nothing to compare against) */
      if (s.data.note === "no_delta") return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            paddingBottom: 18, borderBottom: "1px solid " + C.border,
          }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 20, fontWeight: 800 }}>Run</span>
                <RunChip id={runId} />
                <Badge type="golden" />
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>Flow: {flowId}</div>
            </div>
            <BackBtn onClick={onBack} label={flowId} />
          </div>
          <div style={{
            display: "flex", flexDirection: "column", alignItems: "center",
            justifyContent: "center", gap: 16, padding: "48px 20px",
            background: C.surface, border: "1px solid " + C.border,
            borderRadius: 12,
          }}>
            <div style={{
              width: 64, height: 64, borderRadius: 16,
              background: "rgba(251,191,36,.12)", border: "1px solid rgba(251,191,36,.3)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 24, fontWeight: 800, color: C.warn, fontFamily: "monospace",
            }}>G</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: C.soft }}>Golden Baseline Run</div>
            <div style={{ fontSize: 14, color: C.muted, textAlign: "center", maxWidth: 420, lineHeight: 1.8 }}>
              This is the first recorded run for this flow and serves as the golden baseline.
              No delta is available because there was no prior run to compare against.
              Future runs will be compared to this baseline.
            </div>
          </div>
        </div>
      );

      const deltas   = s.data.deltas || [];
      const totalChg = deltas.reduce((acc, d) => acc + d.changes.length, 0);

      const typeCts = {};
      deltas.forEach(d => d.changes.forEach(c => {
        typeCts[c.change_type] = (typeCts[c.change_type] || 0) + 1;
      }));

      const rows = [];
      deltas.forEach((d, di) => {
        if (!d.changes.length) return;
        const from = (d.from_location || "").includes("(")
          ? d.from_location.split("(").pop().replace(")", "") : d.from_location || "?";
        const to = (d.to_location || "").includes("(")
          ? d.to_location.split("(").pop().replace(")", "") : d.to_location || "?";

        rows.push(
          <tr key={"sep-" + di} style={{ background: "rgba(9,9,15,0.8)" }}>
            <td colSpan={5} style={{
              padding: "6px 18px", fontSize: 12, color: C.muted,
              borderTop: di > 0 ? "1px solid " + C.border : "none",
            }}>
              <span style={{ fontFamily: "monospace", color: C.accent }}>{from}</span>
              <span style={{ margin: "0 10px", color: C.border }}>{"--->"}</span>
              <span style={{ fontFamily: "monospace", color: C.accent }}>{to}</span>
              <span style={{ marginLeft: 10, color: C.muted, fontSize: 11 }}>
                {"[ transition " + (di + 1) + " ]"}
              </span>
            </td>
          </tr>
        );

        d.changes.forEach((c, ci) => {
          const last = di === deltas.length - 1 && ci === d.changes.length - 1;
          rows.push(
            <tr key={di + "-" + ci}
              className="fd-tr-hover"
              onMouseEnter={e => e.currentTarget.style.background = "rgba(108,139,255,.05)"}
              onMouseLeave={e => e.currentTarget.style.background = "transparent"}
            >
              <TD mono last={last} style={{ color: C.primary }}>{c.name}</TD>
              <TD last={last}><Badge type={c.change_type} /></TD>
              <TD mono last={last} style={{ color: C.accent, fontSize: 12 }}>{to}</TD>
              <TD last={last}><pre className="val-pre">{shortVal(c.old_value)}</pre></TD>
              <TD last={last}><pre className="val-pre is-new">{shortVal(c.new_value)}</pre></TD>
            </tr>
          );
        });
      });

      return (
        <div className="fade-up" style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          {/* Header */}
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            paddingBottom: 18, borderBottom: "1px solid " + C.border,
          }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 20, fontWeight: 800 }}>Run</span>
                <RunChip id={runId} />
              </div>
              <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>Flow: {flowId}</div>
            </div>
            <BackBtn onClick={onBack} label={flowId} />
          </div>

          {/* Stats */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(130px,1fr))", gap: 12 }}>
            <StatCard label2="Transitions" value={deltas.length} label="state transitions" />
            <StatCard label2="Changes"     value={totalChg}      label="variable changes" accent />
          </div>

          {/* Breakdown */}
          {Object.keys(typeCts).length > 0 && (
            <div style={{
              display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
              background: C.surface, border: "1px solid " + C.border,
              borderRadius: 12, padding: "12px 20px",
            }}>
              <span style={{
                fontSize: 11, fontWeight: 700, textTransform: "uppercase",
                letterSpacing: "0.08em", color: C.muted,
              }}>
                Breakdown
              </span>
              {Object.entries(typeCts).map(([t, n]) => (
                <div key={t} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>{n}x</span>
                  <Badge type={t} />
                </div>
              ))}
            </div>
          )}

          {/* Delta table */}
          <Panel
            title="State Delta Timeline"
            badge2="DELTA"
            right={totalChg + " variable change" + (totalChg !== 1 ? "s" : "")}
            noPad
          >
            {rows.length === 0 ? (
              <div style={{ textAlign: "center", color: C.muted, padding: "2rem" }}>
                No state changes recorded for this run.
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <TH>Variable</TH><TH>Change Type</TH><TH>Function</TH>
                      <TH>Old Value</TH><TH>New Value</TH>
                    </tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      );
    }

    /* --- Root App ----------------------------------------- */
    function App() {
      const [flows,     setFlows]     = useState([]);
      const [sideLoad,  setSideLoad]  = useState(true);
      const [storePath, setStorePath] = useState("");
      const [nav, setNav] = useState({ view: "welcome", flowId: null, runId: null });

      useEffect(() => {
        Promise.all([
          apiFetch("/api/flows"),
          apiFetch("/api/health").catch(() => ({})),
        ]).then(([f, h]) => {
          const fl = f.flows || [];
          setFlows(fl);
          setStorePath(h.store || "");
          setSideLoad(false);
          if (fl.length > 0) setNav({ view: "flow", flowId: fl[0].flow_id, runId: null });
        }).catch(() => setSideLoad(false));
      }, []);

      const onSelectFlow = useCallback(id => {
        setNav({ view: "flow", flowId: id, runId: null });
      }, []);

      const onSelectRun = useCallback(id => {
        setNav(prev => ({ ...prev, view: "run", runId: id }));
      }, []);

      const onBack = useCallback(() => {
        setNav(prev => ({ ...prev, view: "flow", runId: null }));
      }, []);

      return (
        <div style={{
          display: "flex", minHeight: "100vh",
          background: C.bg, color: C.text,
        }}>
          <Sidebar
            flows={flows}
            selectedFlow={nav.flowId}
            onSelect={onSelectFlow}
            storePath={storePath}
            loading={sideLoad}
          />
          <main style={{ flex: 1, overflowY: "auto", height: "100vh", padding: "30px 34px" }}>
            {nav.view === "welcome" && <WelcomeScreen />}
            {nav.view === "flow" && nav.flowId && (
              <FlowView key={nav.flowId} flowId={nav.flowId} onSelectRun={onSelectRun} />
            )}
            {nav.view === "run" && nav.runId && (
              <RunDetailView
                key={nav.runId}
                flowId={nav.flowId}
                runId={nav.runId}
                onBack={onBack}
              />
            )}
          </main>
        </div>
      );
    }

    ReactDOM.createRoot(document.getElementById("root")).render(<App />);
  </script>
</body>
</html>
"""





# Dashboard server
# ---------------------------------------------------------------------------

class DeltaDashboard:
    """
    FastAPI-based web dashboard for FlowDelta delta visualisation.

    Parameters
    ----------
    store : DeltaStore
        The delta store to query.
    title : str
        Browser tab title.
    """

    def __init__(self, store: DeltaStore, title: str = "FlowDelta Dashboard") -> None:
        self.store = store
        self.title = title
        self._trend_gen = TrendChartGenerator(store)
        self._app = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_app(self):
        """
        Return the FastAPI application instance (lazy-created).
        Raises ``ImportError`` if ``fastapi`` / ``uvicorn`` not installed.
        """
        if self._app is None:
            self._app = self._build_app()
        return self._app

    def run(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Start the dashboard server (blocking)."""
        try:
            import uvicorn
        except ImportError as exc:
            raise ImportError(
                "Install dashboard extras: pip install fastapi uvicorn"
            ) from exc

        app = self.get_app()
        logger.info("FlowDelta Dashboard â†’ http://%s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    # ------------------------------------------------------------------
    # App builder
    # ------------------------------------------------------------------

    def _build_app(self):
        try:
            from fastapi import FastAPI
            from fastapi.responses import HTMLResponse, JSONResponse
        except ImportError as exc:
            raise ImportError(
                "Install dashboard extras: pip install fastapi uvicorn"
            ) from exc

        app = FastAPI(title=self.title, docs_url=None, redoc_url=None)

        store = self.store
        trend_gen = self._trend_gen

        # ---- UI ----
        @app.get("/", response_class=HTMLResponse)
        async def index():
            return _DASHBOARD_HTML

        # ---- API ----
        @app.get("/api/flows")
        async def list_flows():
            all_runs = store.list_runs()
            flow_counts: Dict[str, int] = {}
            for r in all_runs:
                fid = r.get("flow_id", "unknown")
                flow_counts[fid] = flow_counts.get(fid, 0) + 1
            return {"flows": [
                {"flow_id": fid, "run_count": cnt}
                for fid, cnt in sorted(flow_counts.items())
            ]}

        @app.get("/api/flows/{flow_id}/runs")
        async def flow_runs(flow_id: str):
            runs = store.list_runs(flow_id=flow_id)
            runs.sort(key=lambda r: r.get("saved_at", ""))
            return {"flow_id": flow_id, "runs": [
                {
                    "run_id": r.get("run_id"),
                    "saved_at": r.get("saved_at"),
                    "golden": bool(r.get("golden")),
                }
                for r in runs
            ]}

        @app.get("/api/flows/{flow_id}/trend")
        async def flow_trend(flow_id: str):
            points = trend_gen.get_points(flow_id)
            return {
                "flow_id": flow_id,
                "points": [p.to_dict() for p in points],
            }

        @app.get("/api/runs/{run_id}/delta")
        async def run_delta(run_id: str):
            data = store.load_delta(run_id)
            if data is None:
                # Run exists as a trace (golden baseline) but has no delta computed yet
                trace = store.load_trace(run_id)
                if trace:
                    return {
                        "run_id": run_id,
                        "flow_id": trace.get("flow_id"),
                        "deltas": [],
                        "golden": bool(trace.get("golden")),
                        "saved_at": trace.get("saved_at"),
                        "note": "no_delta",
                    }
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail=f"No run found with id {run_id!r}")
            return data

        @app.get("/api/runs/{run_id}/trace")
        async def run_trace(run_id: str):
            data = store.load_trace(run_id)
            if data is None:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail=f"No trace for run {run_id!r}")
            # Strip large locals to keep response lightweight
            snapshots = []
            for s in data.get("snapshots", []):
                snapshots.append({
                    "sequence": s.get("sequence"),
                    "file": s.get("file", "").split("\\")[-1].split("/")[-1],
                    "line": s.get("line"),
                    "function": s.get("function"),
                    "event": s.get("event"),
                    "local_count": len(s.get("locals", {})) if isinstance(s.get("locals"), dict) else 0,
                })
            return {"run_id": run_id, "snapshots": snapshots}

        @app.get("/api/health")
        async def health():
            return {"status": "ok", "store": str(store.store_path)}

        return app
