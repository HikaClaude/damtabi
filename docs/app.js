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
  var ILLUST_URL = "./data/illustrations.json"; // カード用イラスト（3:2）の差分ファイル
  var ICON_URL = "./data/dam-icons.json";       // 地図ピン用イラスト（正方形）の差分ファイル
  var SPOTS_URL = "./data/spots.json";          // ダムごとの「行ったら何がある？」
  var GSI_TILE = "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png";
  // 出典は #credits に常時表示している（MapLibre の attribution は畳まれる可能性があるので使わない）

  var state = {
    data: null,
    illust: {},
    icons: {},
    visits: {},        // 訪れたダム（この端末にだけ残る）
    spots: {},         // ダムごとの見どころ（無くても地図は動く）
    tripTint: true,    // 旅の色分け（訪問済みだけ色を残す）
    basis: "irrigation",
    markers: {},   // id -> {marker, el, dam}
    activeId: null
  };

  var $ = function (sel) { return document.querySelector(sel); };

  // 用語のヘルプ。静的ページ(build_site.py の HELP_TEXT)と同じ文言。
  // 「利水」と「有効」の違いはこのアプリの肝なので、用語だけで済ませない。
  var HELP_TEXT = {
    irrigation:
      "<b>使える水がどれだけ残っているか</b>の割合です。" +
      "ダムの容量のうち、水道・農業・工業などに使うために確保された分（利水容量）に対して、" +
      "いま何%たまっているかを表します。渇水のときに注目される数字です。",
    effective:
      "<b>ダムの容量全体に対して、いまどれだけ水が入っているか</b>の割合です。" +
      "利水容量に加えて、洪水にそなえて空けておく容量（洪水調節容量）も分母に含みます。" +
      "洪水期は上側を意図的に空けて運用するため、低い値になるのが普通です。" +
      "そのため利水貯水率が100%でも、有効貯水率は低いことがあります。"
  };

  /** ラベルの隣に置く「?」。JS なしでも開閉できるよう details/summary を使う。 */
  function helpToggle(key, label) {
    if (!HELP_TEXT[key]) return "";
    return '<details class="help">' +
      '<summary aria-label="' + esc(label) + 'とは"><span aria-hidden="true">?</span></summary>' +
      '<div class="help-body">' + HELP_TEXT[key] + "</div>" +
      "</details>";
  }

  // ------------------------------------------------------------ 値の扱い

  /** {value,status,reason} を安全に読む。value があるときだけ数値を返す。 */
  function val(item) {
    return item && item.status === "ok" && typeof item.value === "number" ? item.value : null;
  }

  /** ダム本体の公式な水系・河川。表示に使うのは常にこちら。 */
  function official(dam) { return dam.official || {}; }

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

  var PIN_SIZE = 48;          // ゲージのぶん、塗りつぶし円より少し大きくする
  var GAUGE_R = 43;           // viewBox 100 基準
  var GAUGE_W = 11;

  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function shade(hex, amt) {
    var n = parseInt(hex.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    function f(c) { return Math.max(0, Math.min(255, Math.round(c + 255 * amt))); }
    return "rgb(" + f(r) + "," + f(g) + "," + f(b) + ")";
  }

  function makePin(dam) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "dam-pin";
    el.style.width = el.style.height = PIN_SIZE + "px";
    el.title = dam.name;
    el.setAttribute("aria-label", dam.name);

    // MapLibre はこの el の transform を毎フレーム書き換える。
    // 見た目とアニメーションは内側に持たせ、el 側には transition を付けない。
    var inner = document.createElement("span");
    inner.className = "dam-pin__inner";
    el.appendChild(inner);

    paintPin(el, dam);
    el.addEventListener("click", function (ev) {
      ev.stopPropagation();
      select(dam.id);
    });
    return el;
  }

  /**
   * ピンを描く。
   *
   * リングは「色」だけでなく「弧の長さ」でも残量を示す（二重符号化）。
   * 色が見分けにくい環境でも量が読めるようにするため。
   * 中身はイラストがあればイラスト、無ければ淡い同系色。
   * 中を濃く塗るとリングの弧が読めなくなるので、あえて彩度を落としている。
   */
  function paintPin(el, dam) {
    var def = basisDef();
    var v = val(dam[def.field]);
    var color = colorFor(dam);
    var illust = iconFor(dam);

    // 旅の記録による見え方。貯水率のリングと数字には一切影響させない。
    var been = trip.has(dam.id);
    var tint = trip.tinting();
    el.classList.toggle("is-visited", been);
    el.classList.toggle("is-unvisited", tint && !been);
    // 訪問済みは外周の白い縁を琥珀に替える。大きさは変えないので地図は混まない
    var rim = been ? "#e0a41c" : "#fff";
    var C = 2 * Math.PI * GAUGE_R;
    var pct = v === null ? 0 : Math.max(0, Math.min(100, v)) / 100;

    var track = v === null ? "#ccd4dc" : hexA(color, 0.22);
    var arc = v === null ? "" :
      '<circle cx="50" cy="50" r="' + GAUGE_R + '" fill="none" stroke="' + color +
      '" stroke-width="' + GAUGE_W + '" stroke-linecap="round" stroke-dasharray="' +
      (pct * C).toFixed(1) + " " + C.toFixed(1) + '" transform="rotate(-90 50 50)"/>';

    var svg =
      '<svg class="dam-pin__gauge" viewBox="0 0 100 100" aria-hidden="true">' +
        '<circle cx="50" cy="50" r="' + GAUGE_R + '" fill="none" stroke="' + rim + '" stroke-width="' + (GAUGE_W + 4) + '"/>' +
        '<circle cx="50" cy="50" r="' + GAUGE_R + '" fill="none" stroke="' + track + '" stroke-width="' + GAUGE_W + '"/>' +
        arc +
      "</svg>";

    var body, numCls, numColor, fs;
    if (illust) {
      body = '<img src="' + esc(illust) + '" alt=""><span class="dam-pin__scrim"></span>';
      numCls = "dam-pin__num";
      numColor = "#fff";
      fs = Math.round(PIN_SIZE * 0.27);
    } else if (v === null) {
      body = '<span class="dam-pin__fill is-nodata"></span>';
      numCls = "dam-pin__num is-center";
      numColor = "#5b6875";
      fs = Math.round(PIN_SIZE * 0.34);
    } else {
      body = '<span class="dam-pin__fill" style="background:' + hexA(color, 0.2) + '"></span>';
      numCls = "dam-pin__num is-center";
      numColor = shade(color, -0.28);
      fs = Math.round(PIN_SIZE * 0.34);
    }

    var num = v === null ? "—" : String(Math.round(v));
    el.firstChild.innerHTML =
      svg +
      '<span class="dam-pin__disc" style="inset:' + (GAUGE_W * 0.6) + '%">' + body +
      '<span class="' + numCls + '" style="font-size:' + fs + "px;color:" + numColor + '">' +
      num + "</span></span>";
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

    var note = document.createElement("li");
    note.className = "legend-note";
    note.textContent = "輪の色と長さが残量を表します";
    ul.appendChild(note);
  }

  // ------------------------------------------------------------ パネル

  function illustFor(dam) {
    return state.illust[dam.id] || state.illust[dam.name] || dam.illustration || null;
  }

  /* ピンの中身に使う正方形イラスト。
   * カード（3:2）を丸に切ると左右が落ちて何のダムか分からなくなるので、
   * ピンには正方形で描き起こしたアイコンを使う。
   * 無いダムはカードで代用し、それも無ければ従来どおり数字だけになる。 */
  function iconFor(dam) {
    return state.icons[dam.id] || state.icons[dam.name] || illustFor(dam);
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
    // ラベルは details を内包するので span ではなく div
    var head = '<div class="k"><span>' + esc(label) + "</span>" +
      helpToggle(basisKey, label) + "</div>";
    var v = val(item);
    if (v === null) {
      return (
        '<div class="rate is-nodata">' + head +
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
      '<div class="rate">' + head +
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
    var off = official(dam);
    var sub = [(off.water_system || "") + "水系", off.river, dam.manager]
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

    html += '<div id="trip-slot" class="trip-slot"></div>';
    html += spotsBlock(dam);

    html += '<table class="facts">';
    html += "<tr><th>水系 / 河川</th><td>" + esc(off.water_system) + "水系 " + esc(off.river) + "</td></tr>";
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

    // 出典は「ダムの基本情報」と「貯水率データ」を分けて書く。
    // 貯水率が取れないことと、ダム本体の情報に出典が無いことは別の話。
    html += '<div class="src">';
    html += "<b>ダムの基本情報</b>：" + esc(off.source || "—");
    var ob = dam.observation;
    if (dam.data_status === "no_source") {
      html += "<br><b>貯水率データ</b>：現在、ダム旅が利用している公開情報源では取得できません。" +
        "<br>確認した情報源: " + esc((dam.source && dam.source.checked) || "—");
    } else if (ob) {
      html += "<br><b>貯水率データ</b>：国土交通省 川の防災情報" +
        "（事務所コード " + esc(String(ob.ofc_cd)) + " / 観測所コード " + esc(String(ob.obs_cd)) + "）";
      if (ob.water_system !== off.water_system || ob.river !== off.river) {
        html += "<br>この観測所での分類: " + esc(ob.water_system) + "水系 " + esc(ob.river) +
          "（ダム本体の水系・河川とは分類が異なります）";
      }
    }
    html += "</div></div>";

    $("#panel-body").innerHTML = html;
    $("#panel").classList.remove("is-hidden");
    wireHelp();
    trip.renderSlot(dam);
  }

  /** 用語ヘルプ: 1つ開いたら他は閉じる。パネルを描き直すたびに呼ぶ。 */
  function wireHelp() {
    var helps = $("#panel-body").querySelectorAll("details.help");
    Array.prototype.forEach.call(helps, function (d) {
      d.addEventListener("toggle", function () {
        if (!d.open) return;
        Array.prototype.forEach.call(helps, function (o) { if (o !== d) o.open = false; });
      });
      // ヘルプ内のクリックでパネルが閉じたり地図が反応したりしないように
      d.addEventListener("click", function (ev) { ev.stopPropagation(); });
    });
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
    var optional = function (url) {
      return fetch(url, { cache: "no-cache" })
        .then(function (r) { return r.ok ? r.json() : {}; })
        .catch(function () { return {}; });
    };
    var illustP = optional(ILLUST_URL);
    var iconP = optional(ICON_URL);
    // 見どころも任意。置いていなければ欄ごと出ない
    var spotsP = optional(SPOTS_URL);

    Promise.all([
      fetch(DATA_URL, { cache: "no-cache" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }),
      illustP,
      iconP,
      spotsP
    ]).then(function (res) {
      state.data = res[0];
      state.illust = res[1] || {};
      state.icons = res[2] || {};
      state.spots = (res[3] && res[3].dams) || {};
      trip.load();

      renderMeta();
      basisFromHash();
      renderLegend();

      var map = buildMap();
      // 地図を動かしている間は、指の下のピンが拡大して見えるのを防ぐ
      map.on("movestart", function () { document.body.classList.add("is-moving"); });
      map.on("moveend", function () { document.body.classList.remove("is-moving"); });

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
      trip.wire();
      $("#panel-close").addEventListener("click", closePanel);
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closePanel();
      });
    }).catch(function (e) {
      fail("データを読み込めませんでした（" + e.message + "）");
    });
  }


  // ------------------------------------------------------------ 旅の記録

  /**
   * 訪れたダムを、この端末にだけ残す。
   *
   * 考え方:
   *   ダムを探す・貯水率を見るという本来の使い方は、訪問の有無で変わらない。
   *   変わるのは地図の「色」だけ。1基でも訪れると、訪れた場所だけが色を保ち、
   *   まだの場所は静かに色が引く。旅を重ねるほど自分の地図になっていく。
   *   1基も訪れていない間は今までどおり全部が色つき（初めての人を損させない）。
   *
   * 記録は localStorage のみ。サーバへは何も送らない。位置情報も保存しない。
   */
  var trip = (function () {
    var KEY = "damtabi.visits.v1";
    var TINT_KEY = "damtabi.trip-tint.v1";

    // 判定の半径。ダムの座標は堤体を指すので、駐車場や展望所からでも届く広さにする。
    // 「堤体の一点に立たないと記録できない」のは現地では危険で不便。
    var BASE_M = 600;
    var ACC_ALLOW_M = 300;   // GPS 誤差ぶんの上乗せ（最大）
    // 最も近い2基（上市川 / 上市川第二）でも 1,944m 離れているので、
    // 最大 900m まで広げても取り違えは起きない。判定は常に「最も近い1基」だけ。

    var api = {};
    var busy = false;

    // ---------------------------------------- 保存

    function read() {
      try {
        return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      } catch (e) {
        return {};
      }
    }

    function write() {
      try {
        localStorage.setItem(KEY, JSON.stringify(state.visits));
      } catch (e) {
        console.warn("[trip] 保存できませんでした", e);
      }
    }

    api.load = function () {
      state.visits = read();
      try {
        state.tripTint = localStorage.getItem(TINT_KEY) !== "off";
      } catch (e) {
        state.tripTint = true;
      }
    };

    api.has = function (id) { return !!state.visits[id]; };
    api.count = function () { return Object.keys(state.visits).length; };

    /** 色分けを効かせるか。1基も訪れていなければ今までどおり全部色つき。 */
    api.tinting = function () { return state.tripTint && api.count() > 0; };

    // ---------------------------------------- 距離

    function distanceM(lat1, lon1, lat2, lon2) {
      var R = 6371000, r = Math.PI / 180;
      var dLat = (lat2 - lat1) * r, dLon = (lon2 - lon1) * r;
      var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
              Math.cos(lat1 * r) * Math.cos(lat2 * r) *
              Math.sin(dLon / 2) * Math.sin(dLon / 2);
      return 2 * R * Math.asin(Math.sqrt(a));
    }

    function nearest(lat, lon) {
      var best = null;
      state.data.dams.forEach(function (d) {
        var m = distanceM(lat, lon, d.lat, d.lon);
        if (!best || m < best.m) best = { dam: d, m: m };
      });
      return best;
    }

    function allowance(accuracy) {
      var acc = (typeof accuracy === "number" && accuracy > 0) ? accuracy : 0;
      return BASE_M + Math.min(acc, ACC_ALLOW_M);
    }

    function fmtM(m) {
      if (m >= 1000) return (m / 1000).toFixed(1) + " km";
      if (m < 50) return "50 m 以内";      // GPS の誤差以下を数字で言い切らない
      return Math.round(m / 10) * 10 + " m";
    }

    // ---------------------------------------- 記録する

    /** 訪問時に画面で見えていた観測値を、観測日時ごと控える。 */
    function snapshot(dam) {
      return {
        obs_time: dam.obs_time || null,
        data_status: dam.data_status,
        rate_irrigation: dam.rate_irrigation,
        rate_effective: dam.rate_effective,
        storage_level_m: dam.storage_level_m
      };
    }

    function save(dam, m, accuracy) {
      state.visits[dam.id] = {
        visited_at: new Date().toISOString(),
        seen: snapshot(dam),
        distance_m: Math.round(m),
        accuracy_m: accuracy ? Math.round(accuracy) : null
      };
      write();
      repaintAll();
      renderTripCount();
    }

    api.forget = function (id) {
      delete state.visits[id];
      write();
      repaintAll();
      renderTripCount();
    };

    // ---------------------------------------- 表示

    function fmtDate(iso) {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日";
    }

    function seenLine(rec) {
      var s = rec.seen || {};
      if (!s.obs_time) return "";
      var parts = [];
      if (s.rate_irrigation && s.rate_irrigation.status === "ok") {
        parts.push("利水 " + s.rate_irrigation.value + "%");
      }
      if (s.rate_effective && s.rate_effective.status === "ok") {
        parts.push("有効 " + s.rate_effective.value + "%");
      }
      if (!parts.length) return "";
      // 「訪問した瞬間の貯水率」ではない。画面に出ていた観測値だと分かるように書く
      return '<p class="trip-seen">訪れた日にダム旅で見えていた値<br>' +
        "<strong>" + esc(fmtObsTime(s.obs_time)) + "</strong> 観測 ／ " +
        esc(parts.join(" ・ ")) + "</p>";
    }

    /** パネル内の「旅の記録」欄。 */
    api.renderSlot = function (dam) {
      var box = $("#trip-slot");
      if (!box) return;
      var rec = state.visits[dam.id];

      if (rec) {
        box.innerHTML =
          '<div class="trip-card is-been">' +
            '<p class="trip-been"><span class="trip-mark">訪</span>' +
              esc(fmtDate(rec.visited_at)) + " に訪れました</p>" +
            seenLine(rec) +
            '<button type="button" class="trip-undo" id="trip-undo">この記録を消す</button>' +
          "</div>";
        $("#trip-undo").addEventListener("click", function (ev) {
          ev.stopPropagation();
          api.forget(dam.id);
          api.renderSlot(dam);
        });
      } else {
        box.innerHTML =
          '<div class="trip-card">' +
            '<button type="button" class="trip-go" id="trip-go">現地で訪問を記録する' +
              '<span class="trip-go__sub">ダムの近くにいるとき、現在地で確認します</span>' +
            "</button>" +
            '<p class="trip-note" id="trip-msg">記録はこの端末の中だけに残ります。' +
              "位置情報はどこにも送信しません。</p>" +
          "</div>";
        $("#trip-go").addEventListener("click", function (ev) {
          ev.stopPropagation();
          checkIn(dam);
        });
      }
      box.addEventListener("click", function (ev) { ev.stopPropagation(); });
    };

    function msg(html, kind) {
      var el = $("#trip-msg");
      if (el) {
        el.innerHTML = html;
        el.className = "trip-note" + (kind ? " is-" + kind : "");
      }
    }

    // ---------------------------------------- 現在地の確認

    function checkIn(dam) {
      if (busy) return;
      if (!navigator.geolocation) {
        msg("このブラウザでは現在地を利用できません。", "warn");
        return;
      }
      busy = true;
      var btn = $("#trip-go");
      if (btn) { btn.disabled = true; btn.classList.add("is-busy"); }
      msg("現在地を確認しています…");

      navigator.geolocation.getCurrentPosition(function (pos) {
        busy = false;
        if (btn) { btn.disabled = false; btn.classList.remove("is-busy"); }

        var lat = pos.coords.latitude, lon = pos.coords.longitude;
        var acc = pos.coords.accuracy;
        var here = distanceM(lat, lon, dam.lat, dam.lon);
        var limit = allowance(acc);

        if (here <= limit) {
          save(dam, here, acc);
          api.renderSlot(dam);
          bloom(dam.id);
          return;
        }

        // 別のダムの近くにいるなら、そちらを案内する（歩き回らせない）
        var near = nearest(lat, lon);
        if (near && near.dam.id !== dam.id && near.m <= allowance(acc)) {
          msg("いまは <strong>" + esc(near.dam.name) + "</strong> の近くにいるようです（約 " +
              fmtM(near.m) + "）。<br>そのダムを開いて記録できます。", "warn");
          return;
        }

        var extra = acc > 500
          ? "<br>位置の精度が粗いようです（誤差 約" + fmtM(acc) + "）。屋外でしばらく待つと安定します。"
          : "";
        msg("ここから <strong>" + esc(dam.name) + "</strong> まで約 " + fmtM(here) +
            " あります。<br>現地に着いてからもう一度お試しください。" + extra, "warn");

      }, function (err) {
        busy = false;
        if (btn) { btn.disabled = false; btn.classList.remove("is-busy"); }
        if (err.code === 1) {
          msg("位置情報の利用が許可されていません。<br>" +
              "ブラウザの設定でこのサイトの位置情報を「許可」にしてから、もう一度お試しください。", "warn");
        } else if (err.code === 3) {
          msg("現在地を確認できませんでした（時間切れ）。<br>" +
              "空の見える場所で、もう一度お試しください。", "warn");
        } else {
          msg("現在地を確認できませんでした。<br>" +
              "電波や空の見え方の良い場所で、もう一度お試しください。", "warn");
        }
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    }

    /** 記録した瞬間、そのピンに静かに色が戻る。 */
    function bloom(id) {
      var m = state.markers[id];
      if (!m) return;
      var inner = m.el.querySelector(".dam-pin__inner");
      if (!inner) return;
      inner.classList.remove("is-bloom");
      void inner.offsetWidth;          // アニメーションをやり直させる
      inner.classList.add("is-bloom");
      setTimeout(function () { inner.classList.remove("is-bloom"); }, 1600);
    }

    // ---------------------------------------- 旅の記録一覧

    function renderTripCount() {
      var b = $("#trip-open");
      if (!b) return;
      var n = api.count();
      b.textContent = "旅の記録 " + n + "/" + state.data.dams.length;
      b.classList.toggle("has-visits", n > 0);
      var t = $("#tint-switch");
      if (t) t.hidden = n === 0;
    }

    function renderTripList() {
      var ids = Object.keys(state.visits).sort(function (a, b) {
        return (state.visits[b].visited_at || "").localeCompare(state.visits[a].visited_at || "");
      });
      var html = '<h2 class="trip-title">旅の記録</h2>';
      html += '<p class="trip-sub">' + ids.length + " / " + state.data.dams.length +
              "基を訪れました</p>";
      if (!ids.length) {
        html += '<p class="trip-empty">まだ記録はありません。<br>' +
          "ダムに着いたら、そのダムの画面から現在地で記録できます。</p>";
      } else {
        html += '<ul class="trip-list">';
        ids.forEach(function (id) {
          var dam = state.data.dams.filter(function (d) { return d.id === id; })[0];
          if (!dam) return;
          var rec = state.visits[id];
          var icon = iconFor(dam);
          html += "<li>" +
            (icon ? '<img src="' + esc(icon) + '" alt="">' : '<span class="noimg"></span>') +
            "<span class=\"trip-list__name\">" + esc(dam.name) + "</span>" +
            '<span class="trip-list__date">' + esc(fmtDate(rec.visited_at)) + "</span>" +
            "</li>";
        });
        html += "</ul>";
      }
      html += '<p class="trip-warn">記録はこの端末のブラウザにだけ保存されています。' +
        "ブラウザのデータを消したり、別の端末・別のブラウザで開いたりすると残りません。</p>";
      $("#trip-body").innerHTML = html;
    }

    // ---------------------------------------- 組み込み

    api.wire = function () {
      renderTripCount();

      $("#trip-open").addEventListener("click", function () {
        renderTripList();
        $("#trip").classList.remove("is-hidden");
      });
      $("#trip-close").addEventListener("click", function () {
        $("#trip").classList.add("is-hidden");
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") $("#trip").classList.add("is-hidden");
      });

      var tint = $("#tint-toggle");
      tint.checked = state.tripTint;
      tint.addEventListener("change", function () {
        state.tripTint = tint.checked;
        try {
          localStorage.setItem(TINT_KEY, tint.checked ? "on" : "off");
        } catch (e) { /* 保存できなくても表示は切り替わる */ }
        repaintAll();
      });
    };

    return api;
  })();



  // ------------------------------------------------------------ 行ったら何がある？

  /**
   * そのダムだからこそある寄り道を、事実だけで短く出す。
   *
   * 「行ってみようかな」と「実際に訪れた」の間を埋めるための欄。
   * 貯水率とダムの基本情報の下に置き、訪問の記録欄のすぐ後に続ける。
   *
   * 書いてよいのは、公式が出している事実と、ダム旅が自分で書いた短い説明だけ。
   * 他所の紹介文や写真は使わない。時間・期間・料金のように変わるものは、
   * 必ず「どこの情報か」と「いつ確認したか」を添えて出す。
   */

  var SPOT_KINDS = {
    collect: { label: "集める", icon: "◆" },
    eat:     { label: "食べる", icon: "●" },
    see:     { label: "見る",   icon: "■" },
    buy:     { label: "買う",   icon: "▲" }
  };

  function spotsFor(dam) {
    var list = state.spots[dam.id];
    return (list && list.length) ? list : null;
  }

  /** 「確認日」を読める形に。2026-09-07 → 2026年9月7日 */
  function fmtChecked(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    return m ? m[1] + "年" + Number(m[2]) + "月" + Number(m[3]) + "日" : "";
  }

  function hostOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (e) {
      return url;
    }
  }

  function spotsBlock(dam) {
    var list = spotsFor(dam);
    if (!list) return "";

    var html = '<section class="spots">';
    html += '<h3 class="spots__head">行ったら、こんなのもあります</h3>';

    list.forEach(function (sp) {
      var kind = SPOT_KINDS[sp.kind] || { label: "", icon: "・" };
      html += '<article class="spot">';
      html += '<p class="spot__top"><span class="spot__kind is-' + esc(sp.kind) + '">' +
        kind.icon + " " + esc(kind.label) + "</span>" +
        '<span class="spot__title">' + esc(sp.title) + "</span></p>";
      if (sp.summary) html += '<p class="spot__sum">' + esc(sp.summary) + "</p>";

      if (sp.facts && sp.facts.length) {
        html += '<dl class="spot__facts">';
        sp.facts.forEach(function (f) {
          html += "<dt>" + esc(f.label) + "</dt><dd>" + esc(f.value) + "</dd>";
        });
        html += "</dl>";
      }
      if (sp.note) html += '<p class="spot__note">' + esc(sp.note) + "</p>";

      // 出典と確認日は必ず出す。ここを省くと「いつの情報か」が分からなくなる
      html += '<p class="spot__src">';
      if (sp.source) {
        html += '<a href="' + esc(sp.source) + '" target="_blank" rel="noopener">' +
          esc(sp.source_name || hostOf(sp.source)) + " →</a>";
      } else if (sp.source_name) {
        html += esc(sp.source_name);
      }
      if (sp.checked) html += "<span>" + esc(fmtChecked(sp.checked)) + " 確認</span>";
      html += "</p>";
      html += "</article>";
    });

    html += '<p class="spots__caution">' +
      "時間・期間・料金は変わることがあります。出かける前に、上の公式ページでお確かめください。" +
      "</p>";
    html += "</section>";
    return html;
  }


  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
