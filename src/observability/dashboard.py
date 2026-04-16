"""
Web Dashboard -- Sprint 4 of FlowDelta.

A lightweight FastAPI web application that serves an interactive
delta visualisation dashboard. Features:

* **Flow list** -- all recorded flows with run counts
* **Trace viewer** -- snapshot-by-snapshot state changes for any run
* **Delta diff view** -- side-by-side variable changes with colour coding
* **Trend chart** -- Chart.js line chart of change volume over time
* **Invariant panel** -- list of detected invariants for a flow
* **REST API** -- JSON endpoints for all data (consumable by external tools)

The dashboard runs entirely in-process (no database server needed) and
reads directly from the :class:`DeltaStore`.

Usage::

    from src.observability import DeltaDashboard
    from src.delta_engine import DeltaStore

    store = DeltaStore(store_path=".flowdelta/runs")
    dashboard = DeltaDashboard(store)
    dashboard.run(host="127.0.0.1", port=8765)
    # -> open http://localhost:8765

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
    async function apiFetch(path, options) {
      const r = await fetch(path, options);
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
    function Sidebar({ flows, selectedFlow, onSelect, storePath, loading, activePage, onPageSelect }) {
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

          {/* Tools nav */}
          <div style={{ padding: "14px 10px 6px" }}>
            <div style={{
              padding: "4px 8px 8px",
              fontSize: 11, fontWeight: 700, textTransform: "uppercase",
              letterSpacing: "0.09em", color: C.muted,
            }}>Tools</div>
            {[{ id: "tests", label: "Unit Tests", badge: null }].map(item => {
              const isActive = activePage === item.id;
              return (
                <div
                  key={item.id}
                  onClick={() => onPageSelect(item.id)}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "9px 12px 9px " + (isActive ? "9px" : "12px"),
                    borderRadius: 8, cursor: "pointer", marginBottom: 2,
                    fontSize: 14,
                    color:      isActive ? C.primary : C.soft,
                    background: isActive
                      ? "linear-gradient(90deg, rgba(108,139,255,.16), rgba(167,139,250,.06))"
                      : "transparent",
                    borderLeft: "3px solid " + (isActive ? C.primary : "transparent"),
                    transition: "all .15s",
                  }}
                >
                  <span>{item.label}</span>
                  {item.badge && (
                    <span style={{
                      background: "rgba(108,139,255,.2)", color: C.primary,
                      fontSize: 10, padding: "1px 6px", borderRadius: 99, fontWeight: 700,
                    }}>{item.badge}</span>
                  )}
                </div>
              );
            })}
          </div>

          {/* Divider */}
          <div style={{ height: 1, background: C.border, margin: "0 18px" }} />

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
              const active = activePage !== "tests" && selectedFlow === f.flow_id;
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

      /* Golden baseline run - no delta was computed (nothing to compare against) */
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

      const onPageSelect = useCallback(pageId => {
        setNav(prev => ({ ...prev, view: pageId, runId: null }));
      }, []);

      /* activePage for sidebar highlight */
      const activePage = nav.view === "tests" ? nav.view : null;

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
            activePage={activePage}
            onPageSelect={onPageSelect}
          />
          <main style={{ flex: 1, overflowY: "auto", height: "100vh", padding: "30px 34px" }}>
            {nav.view === "welcome" && <WelcomeScreen />}
            {nav.view === "tests"   && <TestsView flows={flows} />}
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

    /* =============================================================
       TESTS VIEW - generated unit test browser + generator
       ============================================================= */

    function TestsView({ flows }) {
      const [files,       setFiles]       = useState(null);
      const [outputDir,   setOutputDir]   = useState("");
      const [selected,    setSelected]    = useState(null);    // filename
      const [source,      setSource]      = useState("");
      const [loading,     setLoading]     = useState(true);
      const [generating,  setGenerating]  = useState(false);
      const [preview,     setPreview]     = useState(null);    // {filename, source, test_count}
      const [genFlowId,   setGenFlowId]   = useState("");
      const [framework,   setFramework]   = useState("pytest");
      const [toast,       setToast]       = useState(null);
      const [confirmOpen, setConfirmOpen] = useState(false);
      const [runResult,   setRunResult]   = useState(null);   // {filename, passed, failed, …}
      const [running,     setRunning]     = useState(false);

      /* ---- load test file list ---- */
      useEffect(() => {
        apiFetch("/api/tests")
          .then(d => { setFiles(d.files || []); setOutputDir(d.output_dir || ""); })
          .catch(() => setFiles([]))
          .finally(() => setLoading(false));
      }, []);

      /* ---- load source when file selected ---- */
      useEffect(() => {
        if (!selected) { setSource(""); return; }
        apiFetch("/api/tests/" + selected)
          .then(d => setSource(d.source || ""))
          .catch(() => setSource("// Could not load file."));
      }, [selected]);

      /* ---- show toast ---- */
      function showToast(msg, ok = true) {
        setToast({ msg, ok });
        setTimeout(() => setToast(null), 3200);
      }

      /* ---- preview tests ---- */
      async function handlePreview() {
        if (!genFlowId) { showToast("Select a flow first", false); return; }
        setGenerating(true);
        try {
          const d = await apiFetch("/api/tests/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ flow_id: genFlowId, confirmed: false, framework }),
          });
          setPreview(d);
          setConfirmOpen(true);
        } catch (e) {
          showToast("Preview failed: " + (e.message || e), false);
        } finally {
          setGenerating(false);
        }
      }

      /* ---- confirm + write ---- */
      async function handleConfirm() {
        setConfirmOpen(false);
        setGenerating(true);
        try {
          const d = await apiFetch("/api/tests/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ flow_id: genFlowId, confirmed: true, framework }),
          });
          showToast("Written: " + d.filename + "  (" + d.test_count + " tests)");
          // Refresh file list and open the new file
          const listData = await apiFetch("/api/tests");
          setFiles(listData.files || []);
          setSelected(d.filename);
          // Auto-run after writing
          handleRun(d.filename);
        } catch (e) {
          showToast("Generate failed: " + (e.message || e), false);
        } finally {
          setGenerating(false);
          setPreview(null);
        }
      }

      /* ---- run tests ---- */
      async function handleRun(fname) {
        const target = fname || selected;
        if (!target) { showToast("Select a test file first", false); return; }
        setRunning(true);
        setRunResult(null);
        try {
          const d = await apiFetch("/api/tests/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: target }),
          });
          setRunResult(d);
          setSelected(target);
          const total = (d.passed||0) + (d.failed||0) + (d.error||0);
          const ok = d.failed === 0 && d.error === 0;
          showToast(
            ok
              ? `✓ All ${d.passed} test${d.passed !== 1 ? "s" : ""} passed`
              : `✗ ${d.failed + d.error} of ${total} test${total !== 1 ? "s" : ""} failed`,
            ok
          );
        } catch (e) {
          showToast("Run failed: " + (e.message || e), false);
        } finally {
          setRunning(false);
        }
      }

      /* ---- styles ---- */
      const panelStyle = {
        background: C.s1, border: "1px solid " + C.border,
        borderRadius: 12, overflow: "hidden",
      };
      const panelHead = {
        padding: "14px 18px", borderBottom: "1px solid " + C.border,
        fontWeight: 600, fontSize: 14, color: C.text,
        display: "flex", alignItems: "center", gap: 8,
      };
      const fileItemBase = active => ({
        padding: "9px 14px", cursor: "pointer", fontSize: 13,
        borderRadius: 7, marginBottom: 3, display: "flex",
        alignItems: "center", justifyContent: "space-between",
        background: active
          ? "linear-gradient(90deg,rgba(108,139,255,.18),rgba(167,139,250,.06))"
          : "transparent",
        color: active ? C.primary : C.soft,
        borderLeft: "3px solid " + (active ? C.primary : "transparent"),
        transition: "all .13s",
      });
      const btnStyle = (primary, disabled) => ({
        padding: "9px 20px", borderRadius: 8, cursor: disabled ? "not-allowed" : "pointer",
        fontWeight: 600, fontSize: 13, border: "none",
        background: disabled ? C.border : (primary ? "linear-gradient(135deg,#6c8bff,#a78bfa)" : C.s2),
        color: disabled ? C.muted : (primary ? "#fff" : C.soft),
        opacity: disabled ? 0.6 : 1,
        transition: "all .15s",
      });

      return (
        <div className="fade-up" style={{ paddingBottom: 40 }}>
          {/* Toast */}
          {toast && (
            <div style={{
              position: "fixed", top: 22, right: 28, zIndex: 1000,
              background: toast.ok ? "rgba(52,211,153,.15)" : "rgba(239,68,68,.15)",
              border: "1px solid " + (toast.ok ? C.success : C.danger),
              color: toast.ok ? C.success : C.danger,
              padding: "10px 20px", borderRadius: 10,
              fontSize: 14, fontWeight: 500,
              animation: "slideIn .2s ease",
            }}>
              {toast.msg}
            </div>
          )}

          {/* Confirm dialog */}
          {confirmOpen && preview && (
            <div style={{
              position: "fixed", inset: 0, zIndex: 900,
              background: "rgba(13,15,24,.82)", backdropFilter: "blur(4px)",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <div style={{
                background: C.s1, border: "1px solid " + C.border,
                borderRadius: 16, width: "min(820px,92vw)",
                maxHeight: "85vh", display: "flex", flexDirection: "column",
                animation: "fadeUp .18s ease",
              }}>
                {/* Dialog header */}
                <div style={{
                  padding: "18px 24px", borderBottom: "1px solid " + C.border,
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                }}>
                  <div>
                    <div style={{ fontWeight: 700, fontSize: 16, color: C.text }}>
                      Confirm: generate tests
                    </div>
                    <div style={{ fontSize: 13, color: C.muted, marginTop: 2 }}>
                      {preview.filename} &nbsp;·&nbsp; {preview.test_count} test{preview.test_count !== 1 ? "s" : ""}
                      &nbsp;·&nbsp;
                      <span style={{
                        background: preview.framework === "selenium" ? "rgba(52,211,153,.15)" : "rgba(108,139,255,.15)",
                        color: preview.framework === "selenium" ? "#34d399" : C.primary,
                        borderRadius: 4, padding: "1px 6px", fontSize: 11, fontWeight: 600,
                      }}>
                        {preview.framework === "selenium" ? "🌐 Selenium" : "🧪 pytest"}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => { setConfirmOpen(false); setPreview(null); }}
                    style={{
                      background: "transparent", border: "none", color: C.muted,
                      fontSize: 20, cursor: "pointer", lineHeight: 1,
                    }}
                  >×</button>
                </div>
                {/* Preview source */}
                <div style={{ flex: 1, overflowY: "auto" }}>
                  <pre style={{
                    margin: 0, padding: "18px 24px",
                    fontFamily: "'JetBrains Mono', monospace", fontSize: 12,
                    lineHeight: 1.6, color: "#aab4d8",
                    background: "#09090f", whiteSpace: "pre-wrap", wordBreak: "break-all",
                  }}>
                    {preview.preview}
                  </pre>
                </div>
                {/* Dialog footer */}
                <div style={{
                  padding: "14px 24px", borderTop: "1px solid " + C.border,
                  display: "flex", gap: 10, justifyContent: "flex-end",
                }}>
                  <button style={btnStyle(false, false)} onClick={() => { setConfirmOpen(false); setPreview(null); }}>
                    Cancel
                  </button>
                  <button style={btnStyle(true, false)} onClick={handleConfirm}>
                    ✓ Write to disk
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Page header */}
          <div style={{ marginBottom: 26 }}>
            <h1 style={{ fontSize: 24, fontWeight: 800, color: C.text }}>Unit Tests</h1>
            <p style={{ color: C.muted, fontSize: 14, marginTop: 5 }}>
              Browse and generate FlowDelta-powered pytest files for any recorded flow.
            </p>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 20 }}>
            {/* Left: file list + generator */}
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              {/* Generator card */}
              <div style={panelStyle}>
                <div style={panelHead}>⚡ Generate Tests</div>
                <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
                  <select
                    value={genFlowId}
                    onChange={e => setGenFlowId(e.target.value)}
                    style={{
                      background: C.s2, border: "1px solid " + C.border,
                      color: genFlowId ? C.text : C.muted,
                      padding: "8px 10px", borderRadius: 7, fontSize: 13,
                      width: "100%", cursor: "pointer",
                    }}
                  >
                    <option value="">— select flow —</option>
                    {(flows || []).map(f => (
                      <option key={f.flow_id} value={f.flow_id}>{f.flow_id}</option>
                    ))}
                  </select>
                  {/* Framework selector */}
                  <div style={{ display: "flex", gap: 8 }}>
                    {[["pytest", "🧪 pytest"], ["selenium", "🌐 Selenium"]].map(([val, label]) => (
                      <label key={val} style={{
                        flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
                        gap: 6, padding: "7px 6px", borderRadius: 7, cursor: "pointer",
                        fontSize: 12, fontWeight: 500,
                        border: "1px solid " + (framework === val ? C.primary : C.border),
                        background: framework === val ? "rgba(108,139,255,.15)" : C.s2,
                        color: framework === val ? C.primary : C.soft,
                        transition: "all .15s",
                      }}>
                        <input
                          type="radio"
                          name="framework"
                          value={val}
                          checked={framework === val}
                          onChange={() => setFramework(val)}
                          style={{ display: "none" }}
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                  <button
                    style={btnStyle(true, generating || !genFlowId)}
                    disabled={generating || !genFlowId}
                    onClick={handlePreview}
                  >
                    {generating ? "Working…" : "Preview & Generate"}
                  </button>
                  <p style={{ fontSize: 11, color: C.muted, lineHeight: 1.5, margin: 0 }}>
                    {framework === "selenium"
                      ? "Generates a Selenium WebDriver test class with locator placeholders and golden-trace assertions."
                      : "A preview of the generated test file will appear for your confirmation before anything is written to disk."}
                  </p>
                </div>
              </div>

              {/* File list */}
              <div style={panelStyle}>
                <div style={panelHead}>📄 Generated Files</div>
                <div style={{ padding: "8px 10px" }}>
                  {loading ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: 8 }}>
                      <Skel w="85%" /><Skel w="65%" /><Skel w="75%" />
                    </div>
                  ) : !files || files.length === 0 ? (
                    <div style={{ padding: "8px 4px", fontSize: 13, color: C.muted }}>
                      No test files yet. Generate some above.
                    </div>
                  ) : files.map(f => (
                    <div
                      key={f.name}
                      style={fileItemBase(selected === f.name)}
                      onClick={() => setSelected(f.name)}
                    >
                      <span style={{ fontFamily: "'JetBrains Mono', monospace", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</span>
                      <div style={{ display: "flex", alignItems: "center", gap: 5, flexShrink: 0 }}>
                        <span style={{
                          fontSize: 11, color: C.muted,
                          background: C.s2, padding: "2px 7px", borderRadius: 4,
                        }}>
                          {(f.size_bytes / 1024).toFixed(1)}k
                        </span>
                        <button
                          onClick={e => { e.stopPropagation(); handleRun(f.name); }}
                          disabled={running}
                          title="Run tests"
                          style={{
                            background: "rgba(52,211,153,.15)", border: "none",
                            color: "#34d399", borderRadius: 5, cursor: running ? "wait" : "pointer",
                            padding: "2px 7px", fontSize: 11, fontWeight: 700, lineHeight: 1.4,
                          }}
                        >▶</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Right: source viewer + run results */}
            <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>

              {/* Run results panel — shown when runResult is set */}
              {runResult && (
                <div style={{ ...panelStyle, overflow: "visible" }}>
                  <div style={{ ...panelHead, justifyContent: "space-between" }}>
                    <span>
                      {running ? "⏳ Running tests…" : (
                        runResult.returncode === 0
                          ? "✅ Test Run Passed"
                          : "❌ Test Run Failed"
                      )}
                    </span>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      {[
                        ["passed",  "#34d399", "✓ Passed"],
                        ["failed",  "#f87171", "✗ Failed"],
                        ["skipped", "#fbbf24", "⊘ Skipped"],
                        ["error",   "#fb923c", "⚠ Error"],
                      ].map(([k, col, lbl]) => runResult[k] > 0 && (
                        <span key={k} style={{
                          fontSize: 11, fontWeight: 700,
                          color: col, background: col.replace(")", ",.12)").replace("(", "a("),
                          padding: "2px 8px", borderRadius: 5,
                        }}>{lbl}: {runResult[k]}</span>
                      ))}
                      <button
                        onClick={() => handleRun(runResult.filename)}
                        disabled={running}
                        style={{
                          background: "rgba(108,139,255,.15)", border: "none",
                          color: C.primary, borderRadius: 6, padding: "3px 10px",
                          fontSize: 12, cursor: running ? "wait" : "pointer", fontWeight: 600,
                        }}>
                        {running ? "Running…" : "↻ Re-run"}
                      </button>
                      <button
                        onClick={() => setRunResult(null)}
                        style={{ background: "transparent", border: "none", color: C.muted, cursor: "pointer", fontSize: 16 }}>
                        ×
                      </button>
                    </div>
                  </div>
                  {/* Per-test rows */}
                  {runResult.tests && runResult.tests.length > 0 && (
                    <div style={{ padding: "8px 10px", display: "flex", flexDirection: "column", gap: 3 }}>
                      {runResult.tests.map((t, i) => {
                        const col = t.outcome === "PASSED" ? "#34d399"
                                  : t.outcome === "SKIPPED" ? "#fbbf24"
                                  : "#f87171";
                        return (
                          <div key={i} style={{
                            display: "flex", alignItems: "center", gap: 8,
                            padding: "5px 8px", borderRadius: 6,
                            background: col.replace(")", ",.07)").replace("(", "a("),
                          }}>
                            <span style={{ color: col, fontWeight: 700, fontSize: 12, minWidth: 54 }}>
                              {t.outcome}
                            </span>
                            <span style={{
                              fontFamily: "'JetBrains Mono', monospace", fontSize: 11,
                              color: C.soft, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                            }}>{t.nodeid.split("::").slice(1).join("::")}</span>
                            {t.duration_s !== null && (
                              <span style={{ fontSize: 10, color: C.muted }}>{t.duration_s.toFixed(3)}s</span>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {/* Full output log (collapsible) */}
                  <details style={{ borderTop: "1px solid " + C.border }}>
                    <summary style={{
                      padding: "8px 16px", cursor: "pointer", fontSize: 12,
                      color: C.muted, userSelect: "none",
                    }}>Full pytest output</summary>
                    <pre style={{
                      margin: 0, padding: "12px 18px",
                      fontFamily: "'JetBrains Mono', monospace", fontSize: 11,
                      lineHeight: 1.55, color: "#9aa5c4",
                      background: "#09090f", maxHeight: 300, overflowY: "auto",
                      whiteSpace: "pre-wrap", wordBreak: "break-all",
                    }}>{runResult.output}</pre>
                  </details>
                </div>
              )}

              {/* Source viewer */}
              <div style={{ ...panelStyle, display: "flex", flexDirection: "column", minHeight: 480 }}>
                <div style={{ ...panelHead, justifyContent: "space-between" }}>
                  <span>
                    {selected
                      ? <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 13 }}>{selected}</span>
                      : <span style={{ color: C.muted }}>Select a file to view its source</span>
                    }
                  </span>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    {selected && (
                      <span style={{ fontSize: 12, color: C.muted }}>
                        {((source || "").split("\\n").length)} lines
                      </span>
                    )}
                    {selected && (
                      <button
                        onClick={() => handleRun(selected)}
                        disabled={running}
                        style={{
                          background: "rgba(52,211,153,.15)", border: "none",
                          color: "#34d399", borderRadius: 6, padding: "3px 11px",
                          fontSize: 12, cursor: running ? "wait" : "pointer", fontWeight: 700,
                        }}>
                        {running ? "⏳ Running…" : "▶ Run Tests"}
                      </button>
                    )}
                  </div>
                </div>
                <div style={{ flex: 1, overflowY: "auto", background: "#09090f" }}>
                  {selected ? (
                    <pre style={{
                      margin: 0, padding: "18px 22px",
                      fontFamily: "'JetBrains Mono', monospace", fontSize: 12.5,
                      lineHeight: 1.65, color: "#aab4d8",
                      whiteSpace: "pre-wrap", wordBreak: "break-all",
                    }}>
                      {source || "Loading…"}
                    </pre>
                  ) : (
                    <div style={{
                      height: "100%", display: "flex", alignItems: "center",
                      justifyContent: "center", color: C.muted, fontSize: 14,
                      flexDirection: "column", gap: 10,
                    }}>
                      <div style={{ fontSize: 32 }}>🧪</div>
                      <div>Select a test file from the left to view its source</div>
                      <div style={{ fontSize: 12 }}>or generate new tests from a recorded flow</div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
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
        logger.info("FlowDelta Dashboard -> http://%s:%d", host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    # ------------------------------------------------------------------
    # App builder
    # ------------------------------------------------------------------

    def _build_app(self):
        try:
            from fastapi import FastAPI, HTTPException, Body
            from fastapi.responses import HTMLResponse, JSONResponse
            from pydantic import BaseModel as _BaseModel
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
                raise HTTPException(status_code=404, detail=f"No run found with id {run_id!r}")
            return data

        @app.get("/api/runs/{run_id}/trace")
        async def run_trace(run_id: str):
            data = store.load_trace(run_id)
            if data is None:
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

        # ---- Tests endpoints ----

        def _get_output_dir() -> Path:
            """Resolve the generated_tests directory relative to the store."""
            # Walk up from store_path until we find generated_tests/
            p = Path(store.store_path).resolve()
            for _ in range(5):
                candidate = p / "generated_tests"
                if candidate.is_dir():
                    return candidate
                p = p.parent
            return Path("generated_tests").resolve()

        @app.get("/api/tests")
        async def list_tests():
            """List all generated test files with metadata."""
            out_dir = _get_output_dir()
            files = []
            if out_dir.is_dir():
                for f in sorted(out_dir.glob("test_*.py")):
                    stat = f.stat()
                    files.append({
                        "name": f.name,
                        "flow_id": f.stem.replace("test_", "", 1).replace("_", "-"),
                        "path": str(f),
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    })
            return {"output_dir": str(out_dir), "files": files}

        @app.get("/api/tests/{filename}")
        async def get_test_file(filename: str):
            """Return the source of a generated test file."""
            # Sanitize: only allow test_*.py filenames, no path traversal
            if not filename.startswith("test_") or not filename.endswith(".py") or "/" in filename or "\\" in filename:
                raise HTTPException(status_code=400, detail="Invalid filename")
            out_dir = _get_output_dir()
            path = out_dir / filename
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"{filename} not found")
            return {"filename": filename, "source": path.read_text(encoding="utf-8")}

        @app.post("/api/tests/generate")
        async def generate_tests(
            flow_id: str = Body(...),
            run_id: Optional[str] = Body(None),
            confirmed: bool = Body(False),
            framework: str = Body("pytest"),
        ):
            """
            Preview or generate tests for a flow.

            Parameters
            ----------
            framework : str
                "pytest"    — pure pytest assertions (default)
                "selenium"  — Selenium WebDriver test class template
            confirmed : bool
                False → preview only; True → write to disk.
            """
            from ..delta_engine.state_diff import TraceDelta
            from ..test_generator import AssertionGenerator, LLMTestWriter, TestRenderer
            from ..state_tracker.trace_recorder import FlowTrace
            from ..flow_identifier.llm_flow_mapper import Flow

            # Validate framework
            _valid_frameworks = {"pytest", "selenium"}
            if framework not in _valid_frameworks:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown framework '{framework}'. Choose from: {sorted(_valid_frameworks)}",
                )

            # Load delta
            if run_id:
                delta_data = store.load_delta(run_id)
            else:
                # load_golden returns the golden *trace*; we need its delta
                golden_trace = store.load_golden(flow_id)
                if golden_trace:
                    delta_data = store.load_delta(golden_trace.get("run_id", ""))
                else:
                    delta_data = None
            if not delta_data:
                raise HTTPException(
                    status_code=404,
                    detail=f"No stored delta for flow '{flow_id}'. Record a trace first.",
                )
            delta_data["flow_id"] = flow_id
            td = TraceDelta.from_dict(delta_data)

            spec = AssertionGenerator().generate(td)
            # Use heuristic augmentation only (no LLM network call)
            writer = LLMTestWriter(api_key="")
            spec = writer.augment(spec)

            # Render to string (not file yet)
            from jinja2 import Environment, FileSystemLoader, StrictUndefined
            from datetime import datetime, timezone as tz

            _pkg_root = Path(__file__).parent.parent.parent
            tmpl_dir = _pkg_root / "templates"
            env = Environment(
                loader=FileSystemLoader(str(tmpl_dir)),
                undefined=StrictUndefined,
                trim_blocks=True,
                lstrip_blocks=True,
            )
            _tmpl_name = (
                "test_module_selenium.py.j2"
                if framework == "selenium"
                else "test_module.py.j2"
            )
            tmpl = env.get_template(_tmpl_name)
            source = tmpl.render(
                spec=spec,
                generated_at=datetime.now(tz.utc).isoformat(),
            )

            safe_name = flow_id.replace("-", "_").replace(" ", "_")
            _suffix = "_selenium" if framework == "selenium" else ""
            filename = f"test_{safe_name}{_suffix}.py"

            if not confirmed:
                return {
                    "confirmed": False,
                    "filename": filename,
                    "flow_id": flow_id,
                    "framework": framework,
                    "test_count": len(spec.groups),
                    "preview": source,
                }

            # Write to disk
            out_dir = _get_output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / filename
            out_path.write_text(source, encoding="utf-8")
            return {
                "confirmed": True,
                "filename": filename,
                "path": str(out_path),
                "flow_id": flow_id,
                "framework": framework,
                "test_count": len(spec.groups),
            }

        # ---- Run test file endpoint ----
        @app.post("/api/tests/run")
        async def run_tests(filename: str = Body(..., embed=True)):
            """
            Execute a generated test file with pytest and return the results.
            """
            import subprocess
            import sys
            import re as _re

            # Sanitize filename
            if (
                not filename.startswith("test_")
                or not filename.endswith(".py")
                or "/" in filename
                or "\\" in filename
            ):
                raise HTTPException(status_code=400, detail="Invalid filename")

            out_dir = _get_output_dir()
            test_path = out_dir / filename
            if not test_path.exists():
                raise HTTPException(status_code=404, detail=f"{filename} not found")

            # Build pytest command — run in the project root so imports resolve
            project_root = Path(store.store_path).resolve()
            for _ in range(5):
                if (project_root / "src").is_dir() or (project_root / "pyproject.toml").exists():
                    break
                project_root = project_root.parent

            cmd = [
                sys.executable, "-m", "pytest",
                str(test_path),
                "-v", "--tb=short", "--no-header",
                "--color=no",
                "-p", "no:cacheprovider",
            ]

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(project_root),
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                return {
                    "filename": filename,
                    "returncode": -1,
                    "passed": 0, "failed": 0, "error": 0, "skipped": 0,
                    "output": "pytest timed out after 120 s",
                    "tests": [],
                }
            except Exception as exc:
                return {
                    "filename": filename,
                    "returncode": -1,
                    "passed": 0, "failed": 0, "error": 0, "skipped": 0,
                    "output": f"Could not launch pytest: {exc}",
                    "tests": [],
                }

            combined = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")

            # Parse per-test lines: "tests/foo.py::test_bar PASSED"
            tests = []
            _line_re = _re.compile(
                r"^(?P<nodeid>\S+::test_\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            )
            _dur_re  = _re.compile(r"\((?P<d>[0-9.]+)s\)")
            for line in combined.splitlines():
                m = _line_re.match(line.strip())
                if m:
                    dur_m = _dur_re.search(line)
                    tests.append({
                        "nodeid":     m.group("nodeid"),
                        "outcome":    m.group("outcome"),
                        "duration_s": float(dur_m.group("d")) if dur_m else None,
                    })

            # Parse summary line: "3 passed, 1 failed, 2 skipped in 0.42s"
            counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
            _sum_re = _re.compile(r"(\d+)\s+(passed|failed|error|skipped)")
            for chunk in _sum_re.findall(combined):
                key = chunk[1] if chunk[1] in counts else None
                if key:
                    counts[key] += int(chunk[0])

            return {
                "filename": filename,
                "returncode": proc.returncode,
                **counts,
                "output": combined,
                "tests": tests,
            }

        return app
