/* ブラウザ回帰試験用の計測スクリプト（tests/test_browser_pin_ws.py が index.html に差し込んで使う。公開物には入らない）。
 *
 * ?mode=ws   … 集水域を開閉したときのパネルの見え方（スクロール位置・入口の位置）を記録する
 * ?mode=pin  … ズームとピンの大きさ・押せる範囲を記録する
 * 結果は ?sink= の URL へ JSON で POST する。
 */
(function () {
  "use strict";
  var P = new URLSearchParams(location.search);
  var MODE = P.get("mode");
  var A = P.get("a"), B = P.get("b"), B_NAME = P.get("bname");
  var out = { mode: MODE, width: innerWidth, height: innerHeight, cases: {}, errors: [] };

  window.addEventListener("error", function (e) { out.errors.push(String(e.message)); });

  function $(s) { return document.querySelector(s); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function until(fn, ms) {
    var t0 = Date.now();
    return new Promise(function (res, rej) {
      (function poll() {
        var v; try { v = fn(); } catch (e) { v = null; }
        if (v) return res(v);
        if (Date.now() - t0 > (ms || 20000)) return rej(new Error("timeout: " + fn));
        setTimeout(poll, 50);
      })();
    });
  }
  function send() {
    return fetch(P.get("sink"), { method: "POST", body: JSON.stringify(out) });
  }

  // ------------------------------------------------------------ 集水域

  function view() {
    var p = $("#panel"), h = p.querySelector(".dam-name"), e = $("#ws-entry"), pr = p.getBoundingClientRect();
    return {
      scrollTop: Math.round(p.scrollTop),
      hidden: p.classList.contains("is-hidden"),
      compact: p.classList.contains("is-ws-compact"),
      name: h ? h.textContent : null,
      nameY: h ? Math.round(h.getBoundingClientRect().top - pr.top) : null,
      entryY: e ? Math.round(e.getBoundingClientRect().top - pr.top) : null,
      wsActive: !!$("#ws-stop"),
      dimmed: document.querySelectorAll(".dam-pin.is-ws-dim").length
    };
  }
  function goHash(h) { location.hash = h; }
  function openPanelFor(id) {
    goHash("dam_id=" + id);
    return until(function () { return $("#ws-go") && $(".dam-name"); });
  }
  function openWs() {
    $("#ws-go").click();
    // fitBounds(900ms) と自動送りが終わるまで待つ（計測のための待ちで、アプリ側には待ちを入れていない）
    return until(function () { return $("#ws-stop"); }).then(function () { return sleep(1500); });
  }
  function closeWs() { $("#ws-stop").click(); }

  function wsCase(name, pre, mid) {
    var c = { steps: {} };
    out.cases[name] = c;
    var p = $("#panel");
    p.scrollTop = pre;
    return sleep(200).then(function () {
      c.steps.before = view();
      return openWs();
    }).then(function () {
      c.steps.open = view();
      if (mid) p.scrollTop += mid;
      c.steps.scrolled = view();
      closeWs();
      c.steps.closeSync = view();       // 待たずに読んだ値（復元が同期で終わっているか）
      return sleep(400);
    }).then(function () {
      c.steps.close = view();
    });
  }

  function runWs() {
    return openPanelFor(A)
      .then(function () { return wsCase("top", 0, 0); })
      .then(function () { return wsCase("pre120", 120, 0); })
      .then(function () { return wsCase("top_mid", 0, P.get("mid") | 0); })
      .then(function () { return wsCase("pre120_mid", 120, P.get("mid") | 0); })
      .then(function () {
        // 「もう一度 雨を降らせる」の後でも、閉じれば開く前へ戻る
        var c = { steps: {} }; out.cases.again = c;
        $("#panel").scrollTop = 60;
        return sleep(200).then(function () {
          c.steps.before = view();
          return openWs();
        }).then(function () {
          $("#ws-again").click();
          return sleep(600);
        }).then(function () {
          c.steps.open = view();
          closeWs();
          c.steps.close = view();
        });
      })
      .then(function () {
        // 別のダムを選ぶ（ピンを押す）。A の位置を B へ持ち越さない
        var c = { steps: {} }; out.cases.switchDam = c;
        $("#panel").scrollTop = 120;
        return sleep(200).then(function () {
          c.steps.before = view();
          return openWs();
        }).then(function () {
          c.steps.open = view();
          var pin = Array.prototype.filter.call(document.querySelectorAll(".dam-pin"), function (el) {
            return el.title.indexOf(B_NAME) === 0;
          })[0];
          pin.click();
          return until(function () { return $(".dam-name").textContent === B_NAME && $("#ws-go"); });
        }).then(function () {
          c.steps.switched = view();
          return wsCase("switchDam_thenB", 30, 0);
        });
      })
      .then(function () {
        // パネルを閉じる。開き直したあとで古い位置を使わない
        var c = { steps: {} }; out.cases.closePanel = c;
        return openPanelFor(A).then(function () {
          $("#panel").scrollTop = 120;
          return sleep(200);
        }).then(function () {
          c.steps.before = view();
          return openWs();
        }).then(function () {
          c.steps.open = view();
          $("#panel-close").click();
          return sleep(400);
        }).then(function () {
          c.steps.closed = view();
          return openPanelFor(A);
        }).then(function () {
          return sleep(300);
        }).then(function () {
          c.steps.reopened = view();
          return wsCase("closePanel_thenA", 50, 0);
        });
      })
      .then(function () {
        // 共有リンク（#dam_id=）で別のダムへ。状態を持ち越さない
        var c = { steps: {} }; out.cases.shareLink = c;
        $("#panel").scrollTop = 120;
        return sleep(200).then(function () {
          c.steps.before = view();
          return openWs();
        }).then(function () {
          c.steps.open = view();
          return openPanelFor(B);
        }).then(function () {
          return sleep(300);
        }).then(function () {
          c.steps.switched = view();
          return wsCase("shareLink_thenB", 70, 0);
        });
      })
      .then(function () {
        // 「見つかりません」へ切り替わる
        var c = { steps: {} }; out.cases.notFound = c;
        return openPanelFor(A).then(function () {
          $("#panel").scrollTop = 120;
          return sleep(200);
        }).then(function () {
          return openWs();
        }).then(function () {
          goHash("dam_id=__no_such_dam__");
          return until(function () { return $(".panel-msg"); });
        }).then(function () {
          c.steps.notFound = view();
          return openPanelFor(A);
        }).then(function () {
          return sleep(300);
        }).then(function () {
          c.steps.reopened = view();
          return wsCase("notFound_thenA", 40, 0);
        });
      });
  }

  // ------------------------------------------------------------ ピン

  function pinInfo() {
    var map = $("#map"), pins = document.querySelectorAll(".dam-pin");
    var el = pins[0], r = el.getBoundingClientRect(), g = el.querySelector(".dam-pin__gauge").getBoundingClientRect();
    var num = el.querySelector(".dam-pin__num");
    return {
      zoomVar: map.style.getPropertyValue("--map-zoom"),
      w: +r.width.toFixed(2), h: +r.height.toFixed(2),
      gaugeW: +g.width.toFixed(2),
      font: num ? parseFloat(getComputedStyle(num).fontSize) : null,
      allSame: Array.prototype.every.call(pins, function (p) {
        return Math.abs(p.getBoundingClientRect().width - r.width) < 0.01;
      })
    };
  }

  /** 見た目の円の内側（中心から半径の 90%）を押すとそのピンに当たり、外側（115%）では当たらないか。
   *  広域ではピンが重なり合うので、1基ずつ他のピンを隠して調べる */
  function hitTest() {
    var hits = 0, tried = 0, pins = Array.prototype.slice.call(document.querySelectorAll(".dam-pin"));
    var n = 0;
    pins.forEach(function (el) {
      if (n >= 12) return;
      var r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2, R = r.width / 2;
      if (cx - 1.2 * R < 0 || cy - 1.2 * R < 0 || cx + 1.2 * R > innerWidth || cy + 1.2 * R > innerHeight) return;
      pins.forEach(function (o) { if (o !== el) o.style.visibility = "hidden"; });
      var c0 = document.elementFromPoint(cx, cy);
      var o = document.elementFromPoint(cx + 1.15 * R, cy);
      // 操作部品などの下にあるピンは使わない
      if (c0 && c0.closest(".dam-pin") === el && !(o && o.closest("#controls, #topbar, .maplibregl-ctrl"))) {
        n++;
        [[0, 0], [0.9, 0], [-0.9, 0], [0, 0.9], [0, -0.9], [0.63, 0.63], [-0.63, -0.63]].forEach(function (d) {
          var t = document.elementFromPoint(cx + d[0] * R, cy + d[1] * R);
          tried++;
          if (t && t.closest(".dam-pin") === el) hits++;
        });
        [[1.15, 0], [-1.15, 0], [0, 1.15], [0, -1.15]].forEach(function (d) {
          var t = document.elementFromPoint(cx + d[0] * R, cy + d[1] * R);
          tried++;
          if (!(t && t.closest(".dam-pin") === el)) hits++;
        });
      }
      pins.forEach(function (o) { o.style.visibility = ""; });
    });
    return { pins: n, tried: tried, ok: hits };
  }

  function runPin() {
    var map = $("#map");
    out.cases.table = {};
    // 1) 指定したズームでの大きさ（CSS の式そのもの）。--map-zoom を直接置いて読む
    return until(function () { return document.querySelector(".dam-pin") && map.style.getPropertyValue("--map-zoom"); })
      .then(function () {
        out.cases.initial = pinInfo();
        var saved = map.style.getPropertyValue("--map-zoom");
        [6, 8, 10, 11, 11.5, 12, 12.5, 13, 13.5, 14, 15, 16].forEach(function (z) {
          map.style.setProperty("--map-zoom", String(z));
          out.cases.table[z] = pinInfo();
        });
        map.style.setProperty("--map-zoom", saved);
        return sleep(100);
      })
      .then(function () {
        // 2) 実際の地図を「+」で拡大し、途中の大きさを毎フレーム記録する（飛びがないか）
        var frames = [], zin = $(".maplibregl-ctrl-zoom-in"), zout = $(".maplibregl-ctrl-zoom-out");
        out.cases.frames = frames;
        var rec = true;
        (function tick() {
          if (!rec) return;
          var i = pinInfo();
          frames.push([+i.zoomVar, i.w]);
          requestAnimationFrame(tick);
        })();
        var clicks = [];
        for (var k = 0; k < 10; k++) clicks.push(zin);
        for (k = 0; k < 10; k++) clicks.push(zout);
        return clicks.reduce(function (pr, btn) {
          return pr.then(function () { btn.click(); return sleep(400); });
        }, Promise.resolve()).then(function () {
          rec = false;
          out.cases.afterZoom = pinInfo();
        });
      })
      .then(function () {
        // 3) 押せる範囲。実際のズームを最大まで上げた状態と、初期状態で調べる
        out.cases.hitInitial = hitTest();
        map.style.setProperty("--map-zoom", "16");
        out.cases.hitMax = hitTest();
        out.cases.maxInfo = pinInfo();
        // 選択中（scale 1.16）の見た目の大きさ
        var el = document.querySelector(".dam-pin");
        el.classList.add("is-active");
        return sleep(300).then(function () {
          map.style.setProperty("--map-zoom", "16");   // 地図の終わりぎわの zoom で書き戻されていても最大にそろえる
          out.cases.activeBaseW = pinInfo().w;
          out.cases.activeMaxW = +el.querySelector(".dam-pin__inner").getBoundingClientRect().width.toFixed(2);
          el.classList.remove("is-active");
        });
      });
  }

  addEventListener("load", function () {
    var run = MODE === "pin" ? runPin : runWs;
    until(function () { return typeof maplibregl !== "undefined" && document.querySelector(".dam-pin"); }, 30000)
      .then(run)
      .catch(function (e) { out.errors.push("harness: " + (e && e.message || e)); })
      .then(send);
  });
})();
