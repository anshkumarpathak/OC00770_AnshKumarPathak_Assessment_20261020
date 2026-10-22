"""Tables and charts drawn as plain HTML/SVG.

Streamlit's own st.dataframe / st.bar_chart need pyarrow, whose DLLs fail to
load on Windows when the project sits in a very long folder path. Plain HTML
works everywhere and keeps the dashboard dependency-free.
"""
import html

import pandas as pd
import streamlit as st

_CSS = """<style>
.w-table {border-collapse: collapse; width: 100%; font-size: 0.85rem;}
.w-table th, .w-table td {border-bottom: 1px solid rgba(128,128,128,.25); padding: 4px 8px; text-align: left;
  vertical-align: top;}
.w-table th {opacity: .7; font-weight: 600;}
.w-bar {display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 0.85rem;}
.w-bar .label {width: 42%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
.w-bar .track {flex: 1; background: rgba(128,128,128,.15); border-radius: 3px; height: 14px;}
.w-bar .fill {background: #4c8bf5; height: 14px; border-radius: 3px;}
.w-bar .n {width: 36px; text-align: right; opacity: .8;}
</style>"""


def _css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def table(df: pd.DataFrame, max_rows: int = 50) -> None:
    _css()
    df = df.head(max_rows)
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(v))}</td>" for v in row) + "</tr>"
                   for row in df.itertuples(index=False))
    st.markdown(f"<div style='overflow-x:auto'><table class='w-table'><tr>{head}</tr>{rows}</table></div>",
                unsafe_allow_html=True)


def bars(series: pd.Series, color: str = "#4c8bf5") -> None:
    _css()
    if series.empty:
        st.caption("No data")
        return
    top = max(series.max(), 1)
    out = "".join(
        f"<div class='w-bar'><span class='label'>{html.escape(str(k))}</span><span class='track'>"
        f"<div class='fill' style='width:{100 * v / top:.0f}%;background:{color}'></div></span>"
        f"<span class='n'>{v}</span></div>" for k, v in series.items())
    st.markdown(out, unsafe_allow_html=True)


def line(values: list[float], height: int = 140, unit: str = "ms") -> None:
    if not values:
        st.caption("No data")
        return
    w, h, top = 600, height, max(max(values), 1)
    step = w / max(len(values) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{h - 10 - (v / top) * (h - 20):.1f}" for i, v in enumerate(values))
    st.markdown(
        f"<svg viewBox='0 0 {w} {h}' width='100%' height='{h}' preserveAspectRatio='none'>"
        f"<polyline points='{pts}' fill='none' stroke='#4c8bf5' stroke-width='2'/></svg>"
        f"<div style='font-size:.75rem;opacity:.7'>max {top:,.0f} {unit} · {len(values)} turns, oldest → newest</div>",
        unsafe_allow_html=True)


def flow(steps: list[str]) -> None:
    """A workflow as a row of boxes; the last step (the human decision) is highlighted."""
    boxes = []
    for i, s in enumerate(steps):
        human = i == len(steps) - 1
        style = ("border:2px solid #e8833a;background:rgba(232,131,58,.12)" if human
                 else "border:1px solid rgba(128,128,128,.4);background:rgba(76,139,245,.08)")
        label = ("&#128100; " if human else f"{i + 1}. ") + html.escape(s)
        boxes.append(f"<div style='{style};border-radius:8px;padding:8px 12px;font-size:.85rem'>{label}</div>")
    arrow = "<div style='align-self:center;opacity:.6'>&#8594;</div>"
    st.markdown("<div style='display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 12px'>" + arrow.join(boxes) + "</div>",
                unsafe_allow_html=True)


def _fmt(v) -> str:
    if isinstance(v, float) and v != v:        # NaN: pandas' stand-in for a missing value
        return ""
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v))
    return "" if v is None else str(v)
