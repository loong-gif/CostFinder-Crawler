#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


def money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"${value:,.0f}"


def pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.1f}%"


def load_data(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    for col in [
        "original_price",
        "discount_price",
        "membership_price",
        "discount_amount",
        "discount_percent",
        "min_unit",
        "delivered_unit",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["start_date", "end_date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    bool_map = {"true": True, "false": False}
    for col in ["is_package", "is_membership_required"]:
        df[col] = (
            df[col]
            .astype(str)
            .str.strip()
            .str.lower()
            .map(bool_map)
        )

    for col in [
        "source_name",
        "service_category",
        "service_name",
        "template_type",
        "unit_type",
    ]:
        df[col] = df[col].fillna("Unknown")

    return df


def top_records(df: pd.DataFrame, sort_col: str, columns: list[str], limit: int = 10) -> list[dict]:
    subset = df[df[sort_col].notna()].sort_values(sort_col, ascending=False).head(limit)
    rows = []
    for _, row in subset.iterrows():
        item = {}
        for col in columns:
            value = row[col]
            if isinstance(value, pd.Timestamp):
                item[col] = value.strftime("%Y-%m-%d")
            elif pd.isna(value):
                item[col] = ""
            else:
                item[col] = value
        rows.append(item)
    return rows


def build_payload(df: pd.DataFrame, csv_path: Path) -> dict:
    total_rows = len(df)
    unique_sources = int(df["source_name"].nunique())
    unique_services = int(df["service_name"].nunique())
    membership_share = float(df["is_membership_required"].mean() * 100)
    package_share = float(df["is_package"].mean() * 100)
    dated_share = float(df["end_date"].notna().mean() * 100)

    discount_price_nonzero = df.loc[df["discount_price"] > 0, "discount_price"]
    discount_amount_nonzero = df.loc[df["discount_amount"] > 0, "discount_amount"]
    discount_percent_nonzero = df.loc[df["discount_percent"] > 0, "discount_percent"]
    membership_price_nonzero = df.loc[df["membership_price"] > 0, "membership_price"]

    template_counts = (
        df["template_type"].value_counts()
        .rename_axis("name")
        .reset_index(name="value")
        .to_dict(orient="records")
    )
    category_counts = (
        df["service_category"].value_counts()
        .rename_axis("name")
        .reset_index(name="value")
        .to_dict(orient="records")
    )
    service_top = (
        df["service_name"].value_counts().head(12)
        .rename_axis("name")
        .reset_index(name="value")
        .to_dict(orient="records")
    )

    source_top_df = (
        df.groupby("source_name")
        .agg(
            record_count=("source_name", "size"),
            membership_share=("is_membership_required", "mean"),
            avg_discount_price=("discount_price", "mean"),
        )
        .sort_values("record_count", ascending=False)
        .head(12)
        .reset_index()
    )
    source_top_df["membership_share"] = (source_top_df["membership_share"] * 100).round(1)
    source_top_df["avg_discount_price"] = source_top_df["avg_discount_price"].round(1)

    category_template_df = pd.crosstab(df["service_category"], df["template_type"]).reset_index()
    membership_template_df = (
        pd.crosstab(df["template_type"], df["is_membership_required"])
        .reset_index()
        .rename(columns={False: "not_required", True: "required"})
    )
    if "required" not in membership_template_df.columns:
        membership_template_df["required"] = 0
    if "not_required" not in membership_template_df.columns:
        membership_template_df["not_required"] = 0

    scatter_df = (
        df.loc[df["discount_price"].notna() | df["discount_amount"].notna(), [
            "service_category",
            "service_name",
            "template_type",
            "discount_price",
            "discount_amount",
            "discount_percent",
            "source_name",
        ]]
        .fillna("")
        .head(250)
    )

    date_series = df["end_date"].dropna()
    end_month_df = (
        date_series.dt.to_period("M").astype(str).value_counts().sort_index().reset_index()
        if not date_series.empty
        else pd.DataFrame(columns=["index", "end_date"])
    )
    if not end_month_df.empty:
        end_month_df.columns = ["month", "value"]

    biggest_discount_rows = top_records(
        df,
        "discount_amount",
        [
            "source_name",
            "service_category",
            "service_name",
            "template_type",
            "discount_price",
            "discount_amount",
            "discount_percent",
        ],
    )

    highest_membership_rows = top_records(
        df,
        "membership_price",
        [
            "source_name",
            "service_name",
            "membership_name",
            "membership_price",
            "billing_period",
            "minimum_term",
        ],
    )

    top_template = df["template_type"].value_counts().idxmax()
    top_category = df["service_category"].value_counts().idxmax()
    top_service = df["service_name"].value_counts().idxmax()

    insights = [
        f"样本共 {total_rows} 条，覆盖 {unique_sources} 家机构、{unique_services} 个服务名称，供给高度集中在 {top_category}。",
        f"{top_template} 是最主要的报价模板，说明这批数据更偏向直接标价，而不是活动型折扣表达。",
        f"出现频率最高的服务是 {top_service}，适合作为后续标准化命名和价格监控的重点锚点。",
        f"仅有 {pct(dated_share)} 的记录带有结束日期，时间字段缺失明显，做促销时效分析时需要谨慎。",
        f"会员门槛相关记录占比 {pct(membership_share)}，其中 MEMBERSHIP 模板几乎全部依赖会员机制。",
    ]

    return {
        "meta": {
            "source_file": str(csv_path),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "summary": {
            "total_rows": total_rows,
            "unique_sources": unique_sources,
            "unique_services": unique_services,
            "membership_share": round(membership_share, 1),
            "package_share": round(package_share, 1),
            "dated_share": round(dated_share, 1),
            "median_discount_price": money(discount_price_nonzero.median() if not discount_price_nonzero.empty else None),
            "median_discount_amount": money(discount_amount_nonzero.median() if not discount_amount_nonzero.empty else None),
            "median_discount_percent": pct(discount_percent_nonzero.median() if not discount_percent_nonzero.empty else None),
            "median_membership_price": money(membership_price_nonzero.median() if not membership_price_nonzero.empty else None),
        },
        "insights": insights,
        "charts": {
            "template_counts": template_counts,
            "category_counts": category_counts,
            "service_top": service_top,
            "source_top": source_top_df.to_dict(orient="records"),
            "category_template": category_template_df.to_dict(orient="records"),
            "membership_template": membership_template_df.to_dict(orient="records"),
            "scatter": scatter_df.to_dict(orient="records"),
            "end_month": end_month_df.to_dict(orient="records"),
        },
        "tables": {
            "biggest_discount_rows": biggest_discount_rows,
            "highest_membership_rows": highest_membership_rows,
        },
    }


def render_html(payload: dict) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CF 数据分析可视化报告</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: rgba(255, 250, 244, 0.88);
      --panel-strong: #fff9f2;
      --line: rgba(118, 89, 60, 0.16);
      --text: #2d241c;
      --muted: #76624f;
      --accent: #bb5a3c;
      --accent-2: #d7a24d;
      --accent-3: #295b66;
      --shadow: 0 18px 50px rgba(77, 53, 30, 0.12);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(215, 162, 77, 0.24), transparent 28%),
        radial-gradient(circle at right 20%, rgba(41, 91, 102, 0.14), transparent 22%),
        linear-gradient(180deg, #f6f1ea 0%, #f1ebe3 45%, #efe6dc 100%);
      min-height: 100vh;
    }}
    .wrap {{
      width: min(1400px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(255, 248, 238, 0.92), rgba(247, 237, 225, 0.9));
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 32px;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      right: -60px;
      top: -60px;
      width: 220px;
      height: 220px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(187, 90, 60, 0.18) 0%, rgba(187, 90, 60, 0) 72%);
    }}
    .eyebrow {{
      font-size: 12px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 10px;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(28px, 5vw, 46px);
      line-height: 1.06;
      max-width: 860px;
    }}
    .sub {{
      margin-top: 14px;
      max-width: 860px;
      color: var(--muted);
      line-height: 1.7;
      font-size: 15px;
    }}
    .meta {{
      margin-top: 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta span {{
      background: rgba(255,255,255,0.58);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
      margin-top: 20px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }}
    .metric {{
      grid-column: span 3;
      min-height: 148px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .metric .label {{
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.04em;
    }}
    .metric .value {{
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1;
      font-weight: 700;
      margin-top: 10px;
    }}
    .metric .note {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }}
    .section_title {{
      margin: 0 0 14px;
      font-size: 18px;
    }}
    .section_desc {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
      margin: 0 0 16px;
    }}
    .span-4 {{ grid-column: span 4; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .span-7 {{ grid-column: span 7; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    .plot {{
      width: 100%;
      height: 360px;
    }}
    .plot.tall {{
      height: 420px;
    }}
    .insight_list {{
      margin: 0;
      padding-left: 18px;
      color: var(--text);
      line-height: 1.8;
    }}
    .insight_list li + li {{
      margin-top: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: rgba(255,255,255,0.45);
    }}
    tbody tr:hover {{
      background: rgba(255,255,255,0.42);
    }}
    .table_wrap {{
      overflow-x: auto;
    }}
    .footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }}
    @media (max-width: 1100px) {{
      .metric {{ grid-column: span 6; }}
      .span-4, .span-5, .span-6, .span-7, .span-8 {{ grid-column: span 12; }}
    }}
    @media (max-width: 700px) {{
      .wrap {{ width: min(100% - 20px, 1400px); padding-top: 20px; }}
      .hero {{ padding: 22px; border-radius: 22px; }}
      .card {{ border-radius: 20px; padding: 16px; }}
      .metric {{ grid-column: span 12; min-height: 132px; }}
      .plot, .plot.tall {{ height: 320px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="eyebrow">Costfinder Visual Report</div>
      <h1>CF 报价数据分析可视化报告</h1>
      <p class="sub">
        这份报告基于 QA 对齐后的最终 CSV 输出，聚焦服务类别结构、报价模板分布、机构覆盖、折扣强度与会员机制。
        页面中的图表均可交互悬停查看明细，适合用于快速浏览、团队汇报和后续字段治理讨论。
      </p>
      <div class="meta" id="meta"></div>
    </section>

    <section class="grid" id="summary_grid"></section>

    <section class="grid">
      <div class="card span-5">
        <h2 class="section_title">关键洞察</h2>
        <p class="section_desc">从当前样本结构中提炼出的重点观察。</p>
        <ul class="insight_list" id="insights"></ul>
      </div>
      <div class="card span-7">
        <h2 class="section_title">模板类型分布</h2>
        <p class="section_desc">FIXED_PRICE 是绝对主流，其次是 DISCOUNT 与 MEMBERSHIP。</p>
        <div id="template_pie" class="plot"></div>
      </div>
    </section>

    <section class="grid">
      <div class="card span-6">
        <h2 class="section_title">服务类别体量</h2>
        <p class="section_desc">样本基本由 Neurotoxins 与 Fillers 两大板块构成。</p>
        <div id="category_bar" class="plot"></div>
      </div>
      <div class="card span-6">
        <h2 class="section_title">高频服务名称 Top 12</h2>
        <p class="section_desc">可帮助识别标准化命名和价格追踪的核心对象。</p>
        <div id="service_bar" class="plot"></div>
      </div>
    </section>

    <section class="grid">
      <div class="card span-7">
        <h2 class="section_title">机构记录量与会员依赖度</h2>
        <p class="section_desc">柱状代表记录量，折线代表该机构中需要会员条件的记录占比。</p>
        <div id="source_combo" class="plot tall"></div>
      </div>
      <div class="card span-5">
        <h2 class="section_title">类别与模板交叉矩阵</h2>
        <p class="section_desc">快速判断不同品类更偏向哪种报价表达方式。</p>
        <div id="category_template_heatmap" class="plot tall"></div>
      </div>
    </section>

    <section class="grid">
      <div class="card span-5">
        <h2 class="section_title">会员要求与模板类型</h2>
        <p class="section_desc">MEMBERSHIP 模板与会员门槛高度绑定，其他模板多数不依赖会员。</p>
        <div id="membership_stack" class="plot"></div>
      </div>
      <div class="card span-7">
        <h2 class="section_title">价格与折扣强度散点</h2>
        <p class="section_desc">横轴为折后价，纵轴为折扣金额，用于观察不同服务的优惠密度和异常值。</p>
        <div id="discount_scatter" class="plot tall"></div>
      </div>
    </section>

    <section class="grid">
      <div class="card span-4">
        <h2 class="section_title">促销结束月份分布</h2>
        <p class="section_desc">仅基于存在结束日期的少量记录，主要用于核查时间字段完整性。</p>
        <div id="end_month_bar" class="plot"></div>
      </div>
      <div class="card span-8">
        <h2 class="section_title">大额折扣记录 Top 10</h2>
        <p class="section_desc">按 `discount_amount` 排序，适合快速巡检极端优惠样本。</p>
        <div class="table_wrap">
          <table id="discount_table"></table>
        </div>
      </div>
    </section>

    <section class="grid">
      <div class="card span-12">
        <h2 class="section_title">会员价格 Top 10</h2>
        <p class="section_desc">按 `membership_price` 排序，可用于识别高价会员计划和补充核查。</p>
        <div class="table_wrap">
          <table id="membership_table"></table>
        </div>
      </div>
    </section>

    <div class="footer">Generated by Codex HTML report generator</div>
  </div>

  <script>
    const payload = {payload_json};
    const plotBg = 'rgba(0,0,0,0)';
    const paperBg = 'rgba(0,0,0,0)';
    const gridColor = 'rgba(118, 89, 60, 0.12)';
    const fontColor = '#2d241c';
    const accent = ['#bb5a3c', '#d7a24d', '#295b66', '#8a6b4f', '#d7794f', '#4e7d68', '#7c8cc4'];

    function renderMeta() {{
      const meta = document.getElementById('meta');
      const items = [
        `源文件: ${{payload.meta.source_file}}`,
        `生成时间: ${{payload.meta.generated_at}}`,
        `总记录: ${{payload.summary.total_rows}}`,
        `机构数: ${{payload.summary.unique_sources}}`
      ];
      meta.innerHTML = items.map(item => `<span>${{item}}</span>`).join('');
    }}

    function renderSummary() {{
      const cards = [
        ['总记录数', payload.summary.total_rows, '本次分析覆盖的报价记录总量'],
        ['机构覆盖数', payload.summary.unique_sources, '出现过报价记录的独立机构数量'],
        ['服务名称数', payload.summary.unique_services, '去重后的 service_name 数量'],
        ['会员要求占比', `${{payload.summary.membership_share}}%`, '记录中需要会员资格或会员计划的比例'],
        ['套餐记录占比', `${{payload.summary.package_share}}%`, '被标记为 package 的记录比例'],
        ['中位折后价', payload.summary.median_discount_price, '仅统计 discount_price > 0 的记录'],
        ['中位折扣额', payload.summary.median_discount_amount, '仅统计 discount_amount > 0 的记录'],
        ['中位会员价', payload.summary.median_membership_price, '仅统计 membership_price > 0 的记录'],
      ];
      const grid = document.getElementById('summary_grid');
      grid.innerHTML = cards.map(([label, value, note]) => `
        <div class="card metric">
          <div class="label">${{label}}</div>
          <div class="value">${{value}}</div>
          <div class="note">${{note}}</div>
        </div>
      `).join('');
    }}

    function renderInsights() {{
      document.getElementById('insights').innerHTML = payload.insights
        .map(item => `<li>${{item}}</li>`)
        .join('');
    }}

    function renderTable(tableId, rows) {{
      const table = document.getElementById(tableId);
      if (!rows.length) {{
        table.innerHTML = '<tbody><tr><td>暂无数据</td></tr></tbody>';
        return;
      }}
      const headers = Object.keys(rows[0]);
      const thead = `<thead><tr>${{headers.map(h => `<th>${{h}}</th>`).join('')}}</tr></thead>`;
      const tbody = `<tbody>${{rows.map(row => `<tr>${{headers.map(h => `<td>${{row[h] ?? ''}}</td>`).join('')}}</tr>`).join('')}}</tbody>`;
      table.innerHTML = thead + tbody;
    }}

    function baseLayout(extra = {{}}) {{
      return Object.assign({{
        paper_bgcolor: paperBg,
        plot_bgcolor: plotBg,
        font: {{ color: fontColor, family: '"Avenir Next", "PingFang SC", sans-serif' }},
        margin: {{ l: 56, r: 20, t: 24, b: 44 }},
        xaxis: {{ gridcolor: gridColor, zerolinecolor: gridColor }},
        yaxis: {{ gridcolor: gridColor, zerolinecolor: gridColor }},
      }}, extra);
    }}

    function renderPlots() {{
      Plotly.newPlot('template_pie', [{{
        type: 'pie',
        labels: payload.charts.template_counts.map(d => d.name),
        values: payload.charts.template_counts.map(d => d.value),
        hole: 0.54,
        marker: {{ colors: accent }},
        textinfo: 'label+percent',
        sort: false
      }}], baseLayout({{
        margin: {{ l: 10, r: 10, t: 10, b: 10 }},
        showlegend: false
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('category_bar', [{{
        type: 'bar',
        orientation: 'h',
        x: payload.charts.category_counts.map(d => d.value),
        y: payload.charts.category_counts.map(d => d.name),
        marker: {{ color: ['#295b66', '#bb5a3c', '#d7a24d'] }},
        text: payload.charts.category_counts.map(d => d.value),
        textposition: 'outside',
        cliponaxis: false
      }}], baseLayout({{
        margin: {{ l: 170, r: 20, t: 20, b: 40 }}
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('service_bar', [{{
        type: 'bar',
        x: payload.charts.service_top.map(d => d.name),
        y: payload.charts.service_top.map(d => d.value),
        marker: {{ color: '#d7794f' }}
      }}], baseLayout({{
        xaxis: {{ tickangle: -35, gridcolor: 'rgba(0,0,0,0)' }}
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('source_combo', [
        {{
          type: 'bar',
          name: '记录量',
          x: payload.charts.source_top.map(d => d.source_name),
          y: payload.charts.source_top.map(d => d.record_count),
          marker: {{ color: '#bb5a3c' }}
        }},
        {{
          type: 'scatter',
          mode: 'lines+markers',
          name: '会员要求占比',
          x: payload.charts.source_top.map(d => d.source_name),
          y: payload.charts.source_top.map(d => d.membership_share),
          yaxis: 'y2',
          line: {{ color: '#295b66', width: 3 }},
          marker: {{ size: 8 }}
        }}
      ], baseLayout({{
        xaxis: {{ tickangle: -30, gridcolor: 'rgba(0,0,0,0)' }},
        yaxis: {{ title: '记录量', gridcolor: gridColor }},
        yaxis2: {{
          title: '会员要求占比 (%)',
          overlaying: 'y',
          side: 'right',
          rangemode: 'tozero'
        }},
        legend: {{ orientation: 'h', y: 1.16 }}
      }}), {{ responsive: true, displayModeBar: false }});

      const ct = payload.charts.category_template;
      const categoryNames = ct.map(d => d.service_category);
      const templateNames = Object.keys(ct[0] || {{}}).filter(key => key !== 'service_category');
      Plotly.newPlot('category_template_heatmap', [{{
        type: 'heatmap',
        x: templateNames,
        y: categoryNames,
        z: ct.map(row => templateNames.map(t => row[t] || 0)),
        colorscale: [
          [0, '#fff2df'],
          [0.5, '#d7a24d'],
          [1, '#7c3f2a']
        ]
      }}], baseLayout({{
        margin: {{ l: 140, r: 20, t: 20, b: 60 }}
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('membership_stack', [
        {{
          type: 'bar',
          name: '无需会员',
          x: payload.charts.membership_template.map(d => d.template_type),
          y: payload.charts.membership_template.map(d => d.not_required || 0),
          marker: {{ color: '#d7a24d' }}
        }},
        {{
          type: 'bar',
          name: '需要会员',
          x: payload.charts.membership_template.map(d => d.template_type),
          y: payload.charts.membership_template.map(d => d.required || 0),
          marker: {{ color: '#295b66' }}
        }}
      ], baseLayout({{
        barmode: 'stack',
        legend: {{ orientation: 'h', y: 1.14 }}
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('discount_scatter', [{{
        type: 'scatter',
        mode: 'markers',
        x: payload.charts.scatter.map(d => d.discount_price || null),
        y: payload.charts.scatter.map(d => d.discount_amount || null),
        text: payload.charts.scatter.map(d => `${{d.source_name}}<br>${{d.service_name}}<br>${{d.template_type}}`),
        hovertemplate: '%{{text}}<br>折后价: %{{x}}<br>折扣额: %{{y}}<extra></extra>',
        marker: {{
          size: payload.charts.scatter.map(d => Math.max(8, Number(d.discount_percent || 0) / 3 + 8)),
          color: payload.charts.scatter.map(d => d.service_category),
          colorscale: 'Portland',
          line: {{ width: 1, color: 'rgba(255,255,255,0.65)' }},
          opacity: 0.78
        }}
      }}], baseLayout({{
        xaxis: {{ title: 'discount_price', gridcolor: gridColor }},
        yaxis: {{ title: 'discount_amount', gridcolor: gridColor }}
      }}), {{ responsive: true, displayModeBar: false }});

      Plotly.newPlot('end_month_bar', [{{
        type: 'bar',
        x: payload.charts.end_month.map(d => d.month),
        y: payload.charts.end_month.map(d => d.value),
        marker: {{ color: '#8a6b4f' }}
      }}], baseLayout({{
        xaxis: {{ tickangle: -35, gridcolor: 'rgba(0,0,0,0)' }}
      }}), {{ responsive: true, displayModeBar: false }});
    }}

    renderMeta();
    renderSummary();
    renderInsights();
    renderPlots();
    renderTable('discount_table', payload.tables.biggest_discount_rows);
    renderTable('membership_table', payload.tables.highest_membership_rows);
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an HTML visualization report from a Costfinder CSV file.")
    parser.add_argument("csv_path", type=Path, help="Path to the source CSV file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/cf_qa_service_analysis_report.html"),
        help="Output HTML path",
    )
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df = load_data(args.csv_path)
    payload = build_payload(df, args.csv_path)
    html = render_html(payload)
    args.output.write_text(html, encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
