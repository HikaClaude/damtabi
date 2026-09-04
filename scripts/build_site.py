#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docs/data/dams.json から、ダム 1 基につき 1 枚の静的 HTML を生成する。

生成物
------
  docs/dam/{slug}/index.html   ダムごとの静的ページ（値をサーバ側で埋め込み済み）
  docs/dam/index.html          ダム一覧
  docs/sitemap.xml
  docs/robots.txt

検索エンジンにインデックスさせることが目的なので、
* title / meta description はダムごとに個別
* canonical / OGP / JSON-LD を各ページに付与
* JS を使わずに本文が読める（値は HTML に直接埋め込む）
ようにしている。

貯水率が取れていないダムは、ここでも数値を作らない。「—」と理由を出す。

依存ライブラリなし（標準ライブラリのみ）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data" / "dams.json"

SITE_JSON = ROOT / "site.json"
DEFAULT_BASE_URL = "https://example.github.io/dam-map"

def load_site() -> dict:
    """公開URLやサイト名は site.json だけを見る。ここが唯一の情報源。"""
    cfg = {"base_url": DEFAULT_BASE_URL, "site_name": "ダム旅", "short_name": "ダム旅",
           "name_en": "DAM TABI", "producer": "DAM TABI LAB",
           "home_title": "ダム旅", "region": "富山県", "tagline": "",
           "theme_color": "#14507d", "background_color": "#f6f7f9",
           "freshness": {"notice_days": 7, "warn_days": 14}}
    if SITE_JSON.exists():
        cfg.update({k: v for k, v in json.loads(SITE_JSON.read_text(encoding="utf-8")).items()
                    if not k.startswith("_")})
    return cfg

SITE = load_site()
SITE_NAME = SITE["site_name"]          # 「ダム旅」
SITE_TAGLINE = SITE.get("tagline") or ""
REGION = SITE.get("region") or ""      # 「富山県」（当面の対象範囲）
PRODUCER = SITE.get("producer") or ""  # 「DAM TABI LAB」

JST = dt.timezone(dt.timedelta(hours=9))

# 国土交通省の記載例（取り扱い上の注意 2.1)イ）に合わせた文言。
#   「国土交通省　川の防災情報ホームページ（当該ページのURL）を加工して作成」
CREDIT_MLIT = (
    '<a href="https://www.river.go.jp/kawabou/pcfull/tm" target="_blank" '
    'rel="noopener">国土交通省　川の防災情報ホームページ</a>'
    '（https://www.river.go.jp/kawabou/pcfull/tm）を加工して作成'
)
CREDIT_GSI = (
    '地図：<a href="https://maps.gsi.go.jp/development/ichiran.html" '
    'target="_blank" rel="noopener">地理院タイル</a>（国土地理院）'
)


# 用語のヘルプ。地図パネル（app.js）と同じ文言を使う。
# 「利水」と「有効」の差はこのアプリの肝なので、用語だけで済ませない。
HELP_TEXT = {
    "irrigation": (
        "<b>使える水がどれだけ残っているか</b>の割合です。"
        "ダムの容量のうち、水道・農業・工業などに使うために確保された分（利水容量）に対して、"
        "いま何%たまっているかを表します。渇水のときに注目される数字です。"
    ),
    "effective": (
        "<b>ダムの容量全体に対して、いまどれだけ水が入っているか</b>の割合です。"
        "利水容量に加えて、洪水にそなえて空けておく容量（洪水調節容量）も分母に含みます。"
        "洪水期は上側を意図的に空けて運用するため、低い値になるのが普通です。"
        "そのため利水貯水率が100%でも、有効貯水率は低いことがあります。"
    ),
}


def help_toggle(key: str, label: str) -> str:
    """ラベルの隣に置く「?」。JS なしでも開閉できるよう details/summary を使う。"""
    body = HELP_TEXT.get(key)
    if not body:
        return ""
    return (
        '<details class="help">'
        f'<summary aria-label="{e(label)}とは"><span aria-hidden="true">?</span></summary>'
        f'<div class="help-body">{body}</div>'
        "</details>"
    )


def e(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# ---------------------------------------------------------------- 値

def val(item: dict | None):
    if item and item.get("status") == "ok" and isinstance(item.get("value"), (int, float)):
        return item["value"]
    return None


def reason(item: dict | None) -> str:
    if not item:
        return "データがありません"
    return item.get("reason") or "データがありません"


def fmt(v, digits=1) -> str:
    return f"{v:.{digits}f}"


def rate_text(item: dict | None) -> str:
    """description や OGP に入れる短い文言。値が無ければ理由の頭を出す。"""
    v = val(item)
    if v is None:
        r = reason(item)
        return "—（" + r.split("（")[0] + "）"
    return fmt(v) + "%"


def bin_color(v: float, bins: list[dict]) -> str:
    for b in bins:
        if (b["min"] is None or v >= b["min"]) and (b["max"] is None or v < b["max"]):
            return b["color"]
    return "#94a3b8"


def obs_time_jp(s: str | None) -> str | None:
    if not s:
        return None
    try:
        d = dt.datetime.strptime(s, "%Y/%m/%d %H:%M")
    except ValueError:
        return s
    return f"{d.year}年{d.month}月{d.day}日 {d:%H:%M}"


def obs_time_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y/%m/%d %H:%M").replace(tzinfo=JST).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- 部品

def head_common(base: str, rel_root: str) -> str:
    """全ページ共通の head。rel_root は各ページから docs/ ルートへの相対パス。"""
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="{rel_root}manifest.json">
<link rel="icon" href="{rel_root}img/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{rel_root}img/icon-180.png">
<meta name="theme-color" content="{SITE["theme_color"]}">
<meta property="og:site_name" content="{e(SITE_NAME)}">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary_large_image">"""


def credits_block() -> str:
    return (
        '<footer class="credits">'
        f"<p>{CREDIT_MLIT}</p>"
        f"<p>{CREDIT_GSI}</p>"
        "<p>貯水率は観測所の公表値をそのまま転記しています。欠測・未提供の値は推定していません。</p>"
        + (f'<p class="producer">{e(PRODUCER)}</p>' if PRODUCER else "")
        + "</footer>"
    )


def rate_card(label: str, item: dict | None, bins: list[dict], key: str = "") -> str:
    # ラベルは details を内包するので span ではなく div
    head = f'<div class="k"><span>{e(label)}</span>{help_toggle(key, label)}</div>'
    v = val(item)
    if v is None:
        return (
            '<div class="rate is-nodata">'
            + head
            + '<span class="v">—</span>'
            f'<div class="why">{e(reason(item))}</div>'
            "</div>"
        )
    color = bin_color(v, bins)
    w = max(0.0, min(100.0, float(v)))
    return (
        '<div class="rate">'
        + head
        + f'<span class="v">{fmt(v)}<span class="unit">%</span></span>'
        f'<div class="bar"><i style="width:{w}%;background:{color}"></i></div>'
        "</div>"
    )


def fact_row(label: str, item: dict | None, unit: str, digits: int) -> str:
    v = val(item)
    if v is None:
        return (
            f"<tr><th>{e(label)}</th>"
            f'<td class="muted">—<br><small>{e(reason(item))}</small></td></tr>'
        )
    return f"<tr><th>{e(label)}</th><td>{fmt(v, digits)} {e(unit)}</td></tr>"


# ---------------------------------------------------------------- ダムページ

def dam_page(dam: dict, data: dict, base: str) -> str:
    th = data["thresholds"]["bases"]
    slug = dam["slug"]
    url = f"{base}/dam/{dam['pref']}/{slug}/"
    name = dam["name"]

    irr = rate_text(dam.get("rate_irrigation"))
    eff = rate_text(dam.get("rate_effective"))
    when = obs_time_jp(dam.get("obs_time"))

    # --- title / description はダムごとに個別に組み立てる
    #
    # 貯水率の「数値」は meta description に入れない。
    # 検索結果のスニペットは長期間キャッシュされるため、数か月前の値が
    # 「現在の値」のように表示されてしまう。これはこのアプリが最も避けたい失敗。
    # 数値はページ本文に観測日時つきで載せる。
    title = f"{name}｜{SITE_NAME}"
    where = dam.get("address") or REGION
    sub = f"{dam['water_system']}水系{dam['river']}"
    manager = dam.get("manager_office") or dam.get("manager") or ""

    if dam["data_status"] == "no_source":
        desc = (
            f"{name}（{where}）。{sub}。{manager}が管理する発電専用ダムです。"
            "貯水位・貯水量が公開されていないため貯水率は掲載していません。"
            f"地図での位置とダムの基本情報をまとめています。"
        )
    elif dam["data_status"] in ("missing", "not_provided", "closed"):
        desc = (
            f"{name}（{where}）。{sub}、管理は{manager}。"
            "地図での位置とダムの基本情報をまとめています。"
            "貯水率は観測所から公表されていないため、理由を添えて「—」と表示しています。"
        )
    else:
        desc = (
            f"{name}（{where}）。{sub}、管理は{manager}。"
            "地図での位置、ダムの基本情報、公表されている貯水状況"
            "（利水貯水率・有効貯水率）を観測日時とあわせて掲載しています。"
        )
    desc = desc[:300]

    og_img = f"{base}/img/og/{dam['id']}.png"

    # --- JSON-LD（場所として構造化）
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": name,
        "url": url,
        "additionalType": "https://www.wikidata.org/wiki/Q12323",
        "geo": {"@type": "GeoCoordinates", "latitude": dam["lat"], "longitude": dam["lon"]},
        "address": {"@type": "PostalAddress", "addressRegion": "富山県",
                    "addressCountry": "JP", "streetAddress": dam.get("address") or ""},
        "image": og_img,
    }
    if dam.get("kana"):
        ld["alternateName"] = dam["kana"]

    props = []
    for label, item, unit in (
        ("利水貯水率", dam.get("rate_irrigation"), "PERCENT"),
        ("有効貯水率", dam.get("rate_effective"), "PERCENT"),
        ("貯水位", dam.get("storage_level_m"), "MTR"),
    ):
        v = val(item)
        if v is not None:
            props.append({"@type": "PropertyValue", "name": label,
                          "value": v, "unitCode": unit})
    if props:
        ld["additionalProperty"] = props

    # --- パンくず
    crumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "ダム一覧", "item": base + "/dam/"},
            {"@type": "ListItem", "position": 3, "name": name, "item": url},
        ],
    }

    body = []
    body.append('<nav class="crumbs" aria-label="パンくず">')
    body.append(f'<a href="{e(base)}/">{e(SITE_NAME)}</a> › <a href="{e(base)}/dam/">ダム一覧</a> › <span>{e(name)}</span>')
    body.append("</nav>")

    body.append('<article class="dam-page">')
    body.append(f"<h1>{e(name)}" + (f'<span class="kana">{e(dam["kana"])}</span>' if dam.get("kana") else "") + "</h1>")

    sub = " / ".join(filter(None, [dam["water_system"] + "水系", dam["river"], dam.get("manager")]))
    body.append(f'<p class="dam-sub">{e(sub)}</p>')

    # イラスト差し込み領域（地図画面と同じ場所を確保しておく）
    illust = dam.get("illustration")
    if illust:
        body.append(f'<div class="dam-illust"><img src="{e(illust)}" alt="{e(name)}のイラスト"></div>')
    else:
        body.append(
            f'<div class="dam-illust" data-illust-slot="{e(dam["id"])}">'
            f'<div class="placeholder">イラスト準備中<br>{e(name)}</div></div>'
        )

    body.append('<div class="rates">')
    body.append(rate_card("利水貯水率", dam.get("rate_irrigation"),
                          th["irrigation"]["bins"], "irrigation"))
    body.append(rate_card("有効貯水率", dam.get("rate_effective"),
                          th["effective"]["bins"], "effective"))
    body.append("</div>")

    # 貯水率は変わる値なので、「いつの値か」を数値のすぐ下に必ず出す。
    # JS なしでも読めるように、観測時刻とページ生成時刻を本文に埋め込む。
    gen_iso = data["generated_at"]
    gen_jp = gen_iso[:16].replace("T", " ")
    fr = SITE.get("freshness") or {}
    src = dam.get("source") or {}
    live = ""
    if src.get("ofc_cd") and src.get("obs_cd") is not None:
        live = ("https://www.river.go.jp/kawabou/pcfull/tm?itmkndCd=7"
                f"&ofcCd={src['ofc_cd']}&obsCd={src['obs_cd']}&isCurrent=true&fld=0")
    if when:
        body.append(
            f'<p class="freshness" data-obs="{e(obs_time_iso(dam.get("obs_time")))}"'
            f' data-notice-days="{e(fr.get("notice_days", 7))}"'
            f' data-warn-days="{e(fr.get("warn_days", 14))}"'
            + (f' data-live="{e(live)}"' if live else "") + ">"
            f'<strong>{e(when)}</strong> 観測の値です'
            f'<span class="gen">（このページの生成: {e(gen_jp)} JST）</span></p>'
        )
    else:
        body.append(
            f'<p class="freshness"><span class="gen">このページの生成: {e(gen_jp)} JST</span></p>'
        )

    if dam["data_status"] not in ("ok", "partial"):
        body.append(
            '<p class="nodata-note">このダムの貯水率は表示できません。'
            f"理由：{e(reason(dam.get('rate_irrigation')))}"
            "<br>値を推定して埋めることはしていません。</p>"
        )

    body.append('<table class="facts">')
    if when:
        iso = obs_time_iso(dam.get("obs_time"))
        body.append(f'<tr><th>観測日時</th><td><time datetime="{e(iso)}">{e(when)}</time></td></tr>')
    else:
        body.append('<tr><th>観測日時</th><td class="muted">—<br><small>観測値の配信がありません</small></td></tr>')
    body.append(f"<tr><th>所在地</th><td>{e(dam.get('address') or '—')}</td></tr>")
    body.append(f"<tr><th>水系 / 河川</th><td>{e(dam['water_system'])}水系 {e(dam['river'])}</td></tr>")
    body.append(f"<tr><th>管理者</th><td>{e(dam.get('manager_office') or dam.get('manager') or '—')}</td></tr>")
    body.append(fact_row("貯水位", dam.get("storage_level_m"), "m", 2))
    body.append(fact_row("貯水量", dam.get("storage_capacity_1000m3"), "千m³", 0))
    body.append(fact_row("全流入量", dam.get("inflow_m3s"), "m³/s", 2))
    body.append(fact_row("全放流量", dam.get("outflow_m3s"), "m³/s", 2))
    body.append(f'<tr><th>緯度 / 経度</th><td>{dam["lat"]:.6f} / {dam["lon"]:.6f}</td></tr>')
    body.append("</table>")

    fs = dam.get("flood_season")
    if dam.get("in_flood_season") and fs:
        body.append(
            '<div class="callout">現在は<strong>洪水期</strong>（'
            + e(fs["start_md"].replace("-", "/")) + "〜" + e(fs["end_md"].replace("-", "/"))
            + "）。洪水にそなえて水位を制限水位（" + e(fs["limit_level_m"])
            + "m）まで下げて運用するため、利水貯水率が高くても有効貯水率は低く出ます。異常ではありません。</div>"
        )
    if dam.get("note"):
        body.append(f'<div class="callout note">{e(dam["note"])}</div>')

    if dam["data_status"] == "no_source":
        body.append(
            '<div class="callout note">出典：なし。' + e(name)
            + "は発電専用ダムで、事業者が貯水位・貯水量を公開していません。確認先: "
            + e((dam.get("source") or {}).get("checked") or "—") + "</div>"
        )
    else:
        src = dam.get("source") or {}
        body.append(
            '<p class="obs-src">観測所：事務所コード ' + e(src.get("ofc_cd"))
            + " / 観測所コード " + e(src.get("obs_cd")) + "（川の防災情報）</p>"
        )

    body.append(f'<p class="backlink"><a href="{e(base)}/#dam={e(name)}">地図でこのダムの位置を見る →</a></p>')
    body.append("</article>")

    return page_shell(
        title=title, desc=desc, url=url, og_image=og_img,
        rel_root="../../../", body="\n".join(body), base=base,
        extra_head=(
            f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>\n'
            f'<script type="application/ld+json">{json.dumps(crumbs, ensure_ascii=False)}</script>'
        ),
        updated=data["generated_at"],
    )


FRESHNESS_JS = """
// 貯水率の「古さ」の見せ方。
//
// 方針:
//   1) 経過日数は閾値に関係なく **常に** 出す。ここに抜け穴を作らない。
//      （JS が動かない環境でも、本文に観測日時とページ生成日時が書いてある）
//   2) 通常の更新リズムを超えたときだけ、注意 → 警告と段階的に強める。
//      毎回警告を出すと読み飛ばされるようになり、かえって危ない。
// 用語ヘルプ: 1つ開いたら他は閉じる。外側をクリックしても閉じる。
// （details なので JS が無くても開閉自体は動く）
(function () {
  var helps = document.querySelectorAll('details.help');
  if (!helps.length) return;
  helps.forEach(function (d) {
    d.addEventListener('toggle', function () {
      if (!d.open) return;
      helps.forEach(function (o) { if (o !== d) o.open = false; });
    });
  });
  document.addEventListener('click', function (ev) {
    helps.forEach(function (d) { if (d.open && !d.contains(ev.target)) d.open = false; });
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') helps.forEach(function (d) { d.open = false; });
  });
})();

(function () {
  var el = document.querySelector('.freshness[data-obs]');
  if (!el) return;
  var obs = new Date(el.getAttribute('data-obs'));
  if (isNaN(obs)) return;

  var noticeDays = parseFloat(el.getAttribute('data-notice-days')) || 7;
  var warnDays = parseFloat(el.getAttribute('data-warn-days')) || 14;
  var days = (Date.now() - obs.getTime()) / 86400000;

  // --- 経過日数は常に表示
  var age = document.createElement('span');
  age.className = 'age';
  age.textContent = days < 1
    ? '（本日の観測値です）'
    : '（約' + Math.floor(days) + '日前の観測値です）';
  el.appendChild(age);

  if (days < noticeDays) return;

  // --- 通常の更新間隔を超えたときだけ注意を出す
  var box = document.createElement('span');
  var live = el.getAttribute('data-live');
  var link = live
    ? ' <a href="' + live + '" target="_blank" rel="noopener">川の防災情報で最新値を見る</a>'
    : '';
  if (days >= warnDays) {
    box.className = 'stale warn';
    box.innerHTML = '⚠ この値は約' + Math.floor(days) + '日前のものです。'
      + '現在の貯水率は大きく変わっている可能性があります。' + link;
  } else {
    box.className = 'stale';
    box.innerHTML = '※ この値は約' + Math.floor(days) + '日前のものです。'
      + '最新の貯水率は変わっている可能性があります。' + link;
  }
  el.appendChild(box);
})();
"""


def page_shell(title, desc, url, og_image, rel_root, body, base, extra_head="", updated=None) -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
{head_common(base, rel_root)}
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
<meta property="og:image" content="{e(og_image)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
<meta name="twitter:image" content="{e(og_image)}">
<link rel="stylesheet" href="{rel_root}page.css">
{extra_head}
</head>
<body class="doc">
<div class="wrap">
<!--STALE-SLOT-->
{body}
{credits_block()}
{'<p class="updated">最終更新: ' + e(updated) + "</p>" if updated else ""}
</div>
<script>{FRESHNESS_JS}</script>
<script>
  if ("serviceWorker" in navigator) {{
    addEventListener("load", function () {{
      navigator.serviceWorker.register("{rel_root}sw.js").catch(function () {{}});
    }});
  }}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 一覧ページ

def index_page(data: dict, base: str) -> str:
    dams = data["dams"]
    ok = data["summary"]["by_status"].get("ok", 0)
    rows = []
    for d in sorted(dams, key=lambda x: (x["water_system"], x["name"])):
        irr = rate_text(d.get("rate_irrigation"))
        eff = rate_text(d.get("rate_effective"))
        cls = "" if val(d.get("rate_irrigation")) is not None else ' class="muted"'
        rows.append(
            f'<tr><td><a href="./{e(d["pref"])}/{e(d["slug"])}/">{e(d["name"])}</a></td>'
            f'<td>{e(d["water_system"])}水系 {e(d["river"])}</td>'
            f"<td{cls}>{e(irr)}</td><td{cls}>{e(eff)}</td>"
            f"<td>{e(d.get('manager') or '—')}</td></tr>"
        )

    body = [
        '<nav class="crumbs" aria-label="パンくず">'
        f'<a href="{e(base)}/">{e(SITE_NAME)}</a> › <span>ダム一覧</span></nav>',
        f"<h1>{e(REGION)}のダム一覧（全{len(dams)}基）</h1>",
        f'<p class="lead">観測 {e(obs_time_jp(data["base_obs_time"]))} 時点。'
        f"{ok}基は数値を取得できています。残りは欠測・未提供・データ提供なしで、"
        "推定値では埋めず「—」と理由を表示しています。<br>"
        "<small>このサイトは自動更新ではありません。表示中の値がいつのものかは、"
        "各ダムのページで確認できます。</small></p>",
        '<p class="backlink"><a href="' + e(base) + '/">地図で見る →</a></p>',
        '<div class="table-scroll"><table class="list">',
        "<thead><tr><th>ダム名</th><th>水系 / 河川</th><th>利水貯水率</th><th>有効貯水率</th><th>管理者</th></tr></thead>",
        "<tbody>" + "".join(rows) + "</tbody></table></div>",
    ]

    title = f"ダム一覧｜{SITE_NAME}"
    desc = (
        f"{REGION}の{len(dams)}基のダムを一覧にしています。"
        "水系・河川・管理者などの基本情報と、公表されている場合は貯水状況を掲載。"
        "気になるダムのページから、地図での位置や見どころを確認できます。"
    )
    return page_shell(
        title=title, desc=desc, url=f"{base}/dam/", og_image=f"{base}/img/og/site.png",
        rel_root="../", body="\n".join(body), base=base, updated=data["generated_at"],
    )


# ---------------------------------------------------------------- sitemap 他

def sitemap(data: dict, base: str) -> str:
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    urls = [(base + "/", "hourly", "1.0"), (base + "/dam/", "daily", "0.8")]
    urls += [(f"{base}/dam/{d['pref']}/{d['slug']}/", "daily", "0.7")
             for d in data["dams"] if d.get("slug") and d.get("pref")]
    items = "".join(
        f"<url><loc>{e(u)}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{c}</changefreq><priority>{p}</priority></url>"
        for u, c, p in urls
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + items + "</urlset>\n")


def robots(base: str) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n"


INDEX_META_BEGIN = "<!-- BEGIN generated-site-meta -->"
INDEX_META_END = "<!-- END generated-site-meta -->"


def index_meta(base: str, data: dict) -> str:
    """地図画面(docs/index.html)の URL 依存メタを site.json から作り直す。

    手で書いた index.html にも公開URLが必要だが、二重管理すると必ずズレるので
    マーカーで囲った範囲だけをここが生成する。
    """
    # 情報鮮度を保証するように読める語（リアルタイム / 現在の / 今 など）は入れない。
    title = SITE.get("home_title") or SITE_NAME
    total = data["summary"]["total"]
    desc = (
        f"{REGION}の{total}基のダムを地図から探せます。"
        "水系・河川・管理者といった基本情報に加えて、公表されている場合は"
        "貯水状況（利水貯水率・有効貯水率）も観測日時つきで確認できます。"
        "ダムを知って、旅の寄り道に見に行くきっかけに。"
    )
    og = f"{base}/img/og/site.png"
    return "\n".join([
        INDEX_META_BEGIN,
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(desc)}">',
        f'<link rel="canonical" href="{e(base)}/">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{e(SITE_NAME)}">',
        '<meta property="og:locale" content="ja_JP">',
        f'<meta property="og:title" content="{e(SITE_NAME)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:url" content="{e(base)}/">',
        f'<meta property="og:image" content="{e(og)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(SITE_NAME)}">',
        f'<meta name="twitter:description" content="{e(desc)}">',
        f'<meta name="twitter:image" content="{e(og)}">',
        f'<meta name="theme-color" content="{e(SITE["theme_color"])}">',
        f'<meta name="apple-mobile-web-app-title" content="{e(SITE["short_name"])}">',
        f'<meta name="author" content="{e(PRODUCER)}">' if PRODUCER else "",
        INDEX_META_END,
    ])


def write_index_meta(base: str, data: dict) -> bool:
    f = DOCS / "index.html"
    src = f.read_text(encoding="utf-8")
    i, j = src.find(INDEX_META_BEGIN), src.find(INDEX_META_END)
    if i < 0 or j < 0:
        print("[build_site] 警告: docs/index.html に generated-site-meta マーカーがありません",
              file=sys.stderr)
        return False
    new = src[:i] + index_meta(base, data) + src[j + len(INDEX_META_END):]
    if new != src:
        f.write_text(new, encoding="utf-8", newline="\n")
    return True


def manifest(base: str) -> str:
    return json.dumps({
        "name": SITE_NAME,
        "short_name": SITE["short_name"],
        "description": SITE_TAGLINE,
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "orientation": "any",
        "background_color": SITE["background_color"],
        "theme_color": SITE["theme_color"],
        "categories": ["travel", "utilities", "weather"],
        "lang": "ja",
        "icons": [
            {"src": "./img/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "./img/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "./img/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
            {"src": "./img/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
        "shortcuts": [
            {"name": "ダム一覧", "url": "./dam/"},
        ],
    }, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="ダムごとの静的ページ・sitemap・manifest を生成")
    ap.add_argument("--base-url",
                    default=os.environ.get("SITE_BASE_URL") or SITE["base_url"],
                    help="公開先の絶対URL。既定は site.json の base_url")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if base == DEFAULT_BASE_URL:
        print(f"[build_site] 警告: --base-url が既定値のままです（{base}）。"
              "OGP と sitemap を有効にするには公開先URLを指定してください。", file=sys.stderr)

    if not DATA.exists():
        print(f"[build_site] {DATA} がありません。先に fetch_dams.py を実行してください。", file=sys.stderr)
        return 1
    data = json.loads(DATA.read_text(encoding="utf-8"))

    n = 0
    for dam in data["dams"]:
        slug = dam.get("slug")
        if not slug:
            print(f"[build_site] 警告: {dam['name']} に slug がありません。dam_slugs.csv を確認してください。",
                  file=sys.stderr)
            continue
        out = DOCS / "dam" / dam["pref"] / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dam_page(dam, data, base), encoding="utf-8", newline="\n")
        n += 1

    (DOCS / "dam" / "index.html").write_text(index_page(data, base), encoding="utf-8", newline="\n")
    (DOCS / "sitemap.xml").write_text(sitemap(data, base), encoding="utf-8", newline="\n")
    (DOCS / "robots.txt").write_text(robots(base), encoding="utf-8", newline="\n")
    (DOCS / "manifest.json").write_text(manifest(base), encoding="utf-8", newline="\n")
    write_index_meta(base, data)

    print(f"[build_site] ダムページ {n} 枚 / 一覧 / sitemap.xml / robots.txt / manifest.json / index.html のメタ を生成")
    print(f"[build_site] base-url = {base}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
