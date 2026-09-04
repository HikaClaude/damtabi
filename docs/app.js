/* ダム旅 / DAM TABI — 地図画面
 *
 * docs/data/dams.json（バッチ生成）を読んで MapLibre GL JS + 地理院タイルで表示する。
 * サーバレス。ビルド不要。fetch できる場所に置けば動く。
 *
 * 表示の原則: 値が無いものは「—」と理由を出す。埋めない・丸めない。
 */
(function () {
  "use strict";

  var DATA_URL = "./data/dams.json";
  var ILLUST_URL = "./data/illustrations.json"; // 後からイラストを足すための差分ファイル
  var GSI_TILE = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png";
  // 出典は #credits に常時表示している（MapLibre の attribution は畳まれる可能性があるので使わない）

  var state = {
    data: null,
    illust: {},
    basis: "irrigation",
    markers: {},   // id -> {marker, el, dam}
    activeId: null
  };

  var $ = function (sel) { return document.querySelector(sel); };

  // ------------------------------------------------------------ 値の扱い

  /** {value,status,reason} を安全に読む。value があるときだけ数値を返す。 */
  function val(item) {
    return item && item.status === "ok" && typeof item.value === "number" ? item.value : null;
  }

  function reasonOf(item) {
    if (!item) return "データがありません";
    return item.reason || "データがありません";
  }

  function fmtNum(n, digits) {
    if (typeof n !== "number") return null;
    return n.toFixed(digits == null ? 1 : digits);
  }

  /** 観測日時 "2026/09/04 20:20" を読みやすく。 */
  function fmtObsTime(s) {
    if (!s) return null;
    var m = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2})$/.exec(s);
    if (!m) return s;
    return m[1] + "年" + (+m[2]) + "月" + (+m[3]) + "日 " + m[4] + ":" + m[5];
  }

  // ------------------------------------------------------------ 色

  function basisDef() {
    return state.data.thresholds.bases[state.basis];
  }

  function binFor(v, bins) {
    for (var i = 0; i < bins.length; i++) {
      var b = bins[i];
      var okMin = b.min === null || v >= b.min;
      var okMax = b.max === null || v < b.max;
      if (okMin && okMax) return b;
    }
    return null;
  }

  function colorFor(dam) {
    var def = basisDef();
    var v = val(dam[def.field]);
    if (v === null) return state.data.thresholds.no_data_color;
    var b = binFor(v, def.bins);
    return b ? b.color : state.data.thresholds.no_data_color;
  }

  // ------------------------------------------------------------ 地図

  function buildMap() {
    var map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        sources: {
          gsi: {
            type: "raster",
            tiles: [GSI_TILE],
            tileSize: 256,
            minzoom: 5,
            maxzoom: 18
          }
        },
        layers: [{ id: "gsi", type: "raster", source: "gsi" }]
      },
      center: [137.21, 36.62],
      zoom: 9.1,
      minZoom: 7,
      maxZoom: 16,
      // attributionControl:false にすると MapLibre 4.7.1 で load が発火しないため
      // コントロール自体は残し、CSS で隠して #credits を常時表示に使う
      attributionControl: { compact: false }
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 90, unit: "metric" }), "top-left");
    return map;
  }

  function makePin(dam) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "dam-pin";
    el.title = dam.name;
    el.setAttribute("aria-label", dam.name);
    paintPin(el, dam);
    el.addEventListener("click", function (ev) {
      ev.stopPropagation();
      select(dam.id);
    });
    return el;
  }

  function paintPin(el, dam) {
    var def = basisDef();
    var v = val(dam[def.field]);
    el.style.setProperty("--pin", colorFor(dam));
    if (v === null) {
      el.classList.add("is-nodata");
      el.textContent = "—";
    } else {
      el.classList.remove("is-nodata");
      // 100 は "100"、それ以外は整数に丸めて表示（正確な値はパネルで出す）
      el.textContent = String(Math.round(v));
    }
  }

  function repaintAll() {
    Object.keys(state.markers).forEach(function (id) {
      var m = state.markers[id];
      paintPin(m.el, m.dam);
    });
    renderLegend();
  }

  // ------------------------------------------------------------ 凡例

  function renderLegend() {
    var def = basisDef();
    var ul = $("#legend");
    ul.innerHTML = "";
    def.bins.forEach(function (b) {
      var li = document.createElement("li");
      li.innerHTML =
        '<span class="sw" style="background:' + b.color + '"></span><span>' + b.label + "</span>";
      ul.appendChild(li);
    });
    var li = document.createElement("li");
    li.innerHTML = '<span class="sw nodata"></span><span>データなし（理由を表示）</span>';
    ul.appendChild(li);
  }

  // ------------------------------------------------------------ パネル

  function illustFor(dam) {
    return state.illust[dam.id] || state.illust[dam.name] || dam.illustration || null;
  }

  function illustBlock(dam) {
    var src = illustFor(dam);
    if (src) {
      return '<div class="dam-illust"><img src="' + esc(src) + '" alt="' + esc(dam.name) + 'のイラスト"></div>';
    }
    // 差し込み前のプレースホルダ。領域はここで確保してある。
    return (
      '<div class="dam-illust" data-illust-slot="' + esc(dam.id) + '">' +
        '<div class="placeholder">' +
          '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4">' +
            '<path d="M3 17c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2" transform="translate(-2 0)"/>' +
            '<path d="M4 14V6l16 3v5"/><path d="M4 6l16 3"/>' +
          "</svg>" +
          "イラスト準備中<br>" + esc(dam.name) +
        "</div>" +
      "</div>"
    );
  }

  function rateCard(label, item, basisKey) {
    var v = val(item);
    if (v === null) {
      return (
        '<div class="rate is-nodata">' +
          '<span class="k">' + esc(label) + "</span>" +
          '<span class="v">—</span>' +
          '<div class="why">' + esc(reasonOf(item)) + "</div>" +
        "</div>"
      );
    }
    var bins = state.data.thresholds.bases[basisKey].bins;
    var b = binFor(v, bins);
    var color = b ? b.color : "#94a3b8";
    var w = Math.max(0, Math.min(100, v));
    return (
      '<div class="rate">' +
        '<span class="k">' + esc(label) + "</span>" +
        '<span class="v">' + fmtNum(v, 1) + '<span class="unit">%</span></span>' +
        '<div class="bar"><i style="width:' + w + "%;background:" + color + '"></i></div>' +
      "</div>"
    );
  }

  function factRow(label, item, unit, digits) {
    var v = val(item);
    if (v === null) {
      return "<tr><th>" + esc(label) + '</th><td class="muted">—<br><small>' +
        esc(reasonOf(item)) + "</small></td></tr>";
    }
    return "<tr><th>" + esc(label) + "</th><td>" + fmtNum(v, digits) + " " + esc(unit) + "</td></tr>";
  }

  function renderPanel(dam) {
    var sub = [dam.water_system + "水系", dam.river, dam.manager]
      .filter(Boolean)
      .map(esc)
      .join('<span class="sep">/</span>');

    var html = illustBlock(dam);
    html += '<div class="panel-inner">';
    html += '<h2 class="dam-name">' + esc(dam.name) + "</h2>";
    html += '<p class="dam-sub">' + sub + "</p>";
    if (dam.slug && dam.pref) {
      html += '<a class="detail-link" href="./dam/' + encodeURIComponent(dam.pref) +
              "/" + encodeURIComponent(dam.slug) + '/">' + esc(dam.name) + "の詳細ページ →</a>";
    }

    html += '<div class="rates">';
    html += rateCard("利水貯水率", dam.rate_irrigation, "irrigation");
    html += rateCard("有効貯水率", dam.rate_effective, "effective");
    html += "</div>";

    // 値のすぐ下に観測時刻を出す。古い値を現在値と誤読させないため。
    html += '<p class="panel-freshness">' +
      (dam.obs_time ? "<strong>" + esc(fmtObsTime(dam.obs_time)) + "</strong> 観測の値です"
                    : "観測値の配信がありません") + "</p>";

    html += '<table class="facts">';
    html += "<tr><th>観測日時</th><td>" +
      (dam.obs_time ? esc(fmtObsTime(dam.obs_time)) : '<span class="muted">—<br><small>観測値の配信がありません</small></span>') +
      "</td></tr>";
    html += factRow("貯水位", dam.storage_level_m, "m", 2);
    html += factRow("貯水量", dam.storage_capacity_1000m3, "千m³", 0);
    html += factRow("全流入量", dam.inflow_m3s, "m³/s", 2);
    html += factRow("全放流量", dam.outflow_m3s, "m³/s", 2);
    if (dam.manager_office) {
      html += "<tr><th>管理事務所</th><td>" + esc(dam.manager_office) + "</td></tr>";
    }
    html += "</table>";

    // 洪水期は利水100%・有効低が「正常」なので、そう書く
    if (dam.in_flood_season && dam.flood_season) {
      html +=
        '<div class="callout">現在は<strong>洪水期</strong>（' +
        esc(dam.flood_season.start_md.replace("-", "/")) + "〜" +
        esc(dam.flood_season.end_md.replace("-", "/")) +
        "）。洪水にそなえて水位を制限水位（" + esc(String(dam.flood_season.limit_level_m)) +
        "m）まで下げて運用するため、利水貯水率が高くても有効貯水率は低く出ます。異常ではありません。</div>";
    }

    if (dam.note) {
      html += '<div class="callout note">' + esc(dam.note) + "</div>";
    }

    html += '<div class="src">';
    if (dam.data_status === "no_source") {
      html += "出典: なし。" + esc(dam.name) +
        "は発電専用ダムで、事業者が貯水位・貯水量を公開していません。" +
        "確認先: " + esc((dam.source && dam.source.checked) || "—");
    } else {
      html += "出典: 国土交通省 川の防災情報（事務所コード " +
        esc(String(dam.source.ofc_cd)) + " / 観測所コード " + esc(String(dam.source.obs_cd)) + "）";
    }
    html += "</div></div>";

    $("#panel-body").innerHTML = html;
    $("#panel").classList.remove("is-hidden");
  }

  function select(id, updateHash) {
    if (state.activeId && state.markers[state.activeId]) {
      state.markers[state.activeId].el.classList.remove("is-active");
    }
    state.activeId = id;
    var m = state.markers[id];
    if (!m) return;
    m.el.classList.add("is-active");
    renderPanel(m.dam);
    if (updateHash !== false) writeHash();
  }

  function closePanel() {
    $("#panel").classList.add("is-hidden");
    if (state.activeId && state.markers[state.activeId]) {
      state.markers[state.activeId].el.classList.remove("is-active");
    }
    state.activeId = null;
    writeHash();
  }

  /** 表示状態を共有できる URL にしておく: #basis=effective&dam=宇奈月ダム */
  function writeHash() {
    var parts = [];
    if (state.basis !== state.data.thresholds.default_basis) parts.push("basis=" + state.basis);
    if (state.activeId && state.markers[state.activeId]) {
      parts.push("dam=" + encodeURIComponent(state.markers[state.activeId].dam.name));
    }
    var h = parts.length ? "#" + parts.join("&") : location.pathname + location.search;
    history.replaceState(null, "", h);
  }

  /** #basis=effective があれば色分けの基準を切り替える（起動時のみ）。 */
  function basisFromHash() {
    var m = /[#&]basis=([a-z]+)/.exec(location.hash || "");
    if (m && state.data.thresholds.bases[m[1]]) {
      state.basis = m[1];
      var r = document.querySelector('input[name="basis"][value="' + m[1] + '"]');
      if (r) r.checked = true;
    }
  }

  /** #dam=<ダム名> があればそのダムを開く。 */
  function selectFromHash() {
    var m = /[#&]dam=([^&]+)/.exec(location.hash || "");
    if (!m) return;
    var name = decodeURIComponent(m[1]);
    var hit = state.data.dams.filter(function (d) { return d.name === name; })[0];
    if (hit) select(hit.id, false);
  }

  // ------------------------------------------------------------ ヘッダ

  /** "2026/09/04 20:20" → Date（JST）。 */
  function parseObs(s) {
    var m = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}):(\d{2})$/.exec(s || "");
    if (!m) return null;
    return new Date(m[1] + "-" + m[2] + "-" + m[3] + "T" + m[4] + ":" + m[5] + ":00+09:00");
  }

  /**
   * 貯水率の古さを見出しに出す。
   *
   * 経過日数は閾値に関係なく常に出す（原則に穴を作らない）。
   * 注意・警告は通常の更新間隔を超えたときだけ段階的に強める
   * （毎回警告を出すと読み飛ばされてしまい、かえって危ない）。
   */
  function renderAge(d) {
    var el = $("#obs-time");
    var obs = parseObs(d.base_obs_time);
    if (!obs) return;

    var fr = (d.thresholds && d.thresholds.freshness) || {};
    var noticeDays = fr.notice_days || 7;
    var warnDays = fr.warn_days || 14;
    var days = (Date.now() - obs.getTime()) / 86400000;

    var age = document.createElement("span");
    age.className = "age";
    age.textContent = days < 1 ? "（本日の値）" : "（約" + Math.floor(days) + "日前の値）";
    el.appendChild(age);

    if (days < noticeDays) return;
    var w = document.createElement("span");
    w.className = days >= warnDays ? "warn strong" : "warn";
    w.textContent = (days >= warnDays ? "⚠ " : "※ ")
      + "更新から約" + Math.floor(days) + "日経過しています。現在の貯水率は変わっている可能性があります。";
    el.appendChild(w);
  }

  function renderMeta() {
    var d = state.data;
    $("#obs-time").textContent = "観測 " + (fmtObsTime(d.base_obs_time) || d.base_obs_time) + " 時点";
    renderAge(d);

    if (d.stale) {
      var st = document.createElement("span");
      st.className = "warn";
      st.textContent = "（オフライン: 保存済みの内容を表示中）";
      $("#obs-time").appendChild(st);
    }

    var by = d.summary.by_status || {};
    var total = d.summary.total;
    var ok = by.ok || 0;
    var missing = total - ok;
    var parts = [];
    Object.keys(by).forEach(function (k) {
      if (k === "ok") return;
      parts.push((d.status_labels[k] || k) + " " + by[k] + "基");
    });

    var el = $("#status-summary");
    el.innerHTML = "";
    var span = document.createElement("span");
    span.textContent = "全" + total + "基中 " + ok + "基が取得済み";
    el.appendChild(span);
    if (missing > 0) {
      var w = document.createElement("span");
      w.className = "warn";
      w.textContent = "／数値なし " + missing + "基（" + parts.join("・") + "）";
      el.appendChild(w);
    }
  }

  // ------------------------------------------------------------ 起動

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fail(msg) {
    document.getElementById("obs-time").textContent = msg;
    var m = document.getElementById("map");
    m.innerHTML = '<p style="padding:24px;font-size:14px;line-height:1.8">' + esc(msg) +
      "<br><small>docs/data/dams.json が生成されているか、" +
      "file:// ではなくローカルサーバ経由で開いているかを確認してください。</small></p>";
  }

  function start() {
    // イラスト差分は無くても動く（任意）
    var illustP = fetch(ILLUST_URL, { cache: "no-cache" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });

    Promise.all([
      fetch(DATA_URL, { cache: "no-cache" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }),
      illustP
    ]).then(function (res) {
      state.data = res[0];
      state.illust = res[1] || {};

      renderMeta();
      basisFromHash();
      renderLegend();

      var map = buildMap();
      map.on("error", function (ev) {
        // タイル取得失敗などは地図が白くなるだけで気づきにくいので記録する
        console.warn("[map]", (ev && ev.error && ev.error.message) || ev);
      });

      // ピンは DOM オーバーレイなので style の読み込みを待つ必要がない。
      // load を待たずに置くことで、タイルが出ない環境（オフライン・タイル配信の不調）でも
      // ダムの位置と貯水率は読める。
      state.data.dams.forEach(function (dam) {
        var el = makePin(dam);
        var marker = new maplibregl.Marker({ element: el })
          .setLngLat([dam.lon, dam.lat])
          .addTo(map);
        state.markers[dam.id] = { marker: marker, el: el, dam: dam };
      });
      selectFromHash();
      map.on("click", closePanel);

      document.querySelectorAll('input[name="basis"]').forEach(function (r) {
        r.addEventListener("change", function () {
          state.basis = r.value;
          repaintAll();
          writeHash();
        });
      });
      $("#panel-close").addEventListener("click", closePanel);
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closePanel();
      });
    }).catch(function (e) {
      fail("データを読み込めませんでした（" + e.message + "）");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
