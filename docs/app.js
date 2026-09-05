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
    var illust = illustFor(dam);
    var C = 2 * Math.PI * GAUGE_R;
    var pct = v === null ? 0 : Math.max(0, Math.min(100, v)) / 100;

    var track = v === null ? "#ccd4dc" : hexA(color, 0.22);
    var arc = v === null ? "" :
      '<circle cx="50" cy="50" r="' + GAUGE_R + '" fill="none" stroke="' + color +
      '" stroke-width="' + GAUGE_W + '" stroke-linecap="round" stroke-dasharray="' +
      (pct * C).toFixed(1) + " " + C.toFixed(1) + '" transform="rotate(-90 50 50)"/>';

    var svg =
      '<svg class="dam-pin__gauge" viewBox="0 0 100 100" aria-hidden="true">' +
        '<circle cx="50" cy="50" r="' + GAUGE_R + '" fill="none" stroke="#fff" stroke-width="' + (GAUGE_W + 4) + '"/>' +
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
