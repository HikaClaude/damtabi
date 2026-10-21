/* ブラウザ回帰試験用の計測スクリプト（tests/test_browser_panel_obs.py が index.html に差し込んで使う。公開物には入らない）。
 *
 * ?mode=panel&ids=a,b,c … 各ダムの詳細パネルを、利水基準・有効基準の両方で記録する
 * ?mode=ws&a=..&b=..&c=..&x=.. … 集水域の開閉・ダム切替・基準切替で、流入量の表示が正しく入れ替わるか記録する
 * 結果は ?sink= の URL へ JSON で POST する。
 */
(function () {
  "use strict";
  var P = new URLSearchParams(location.search);
  var MODE = P.get("mode");
  var out = { mode: MODE, width: innerWidth, cases: {}, errors: [] };
  var names = {};

  window.addEventListener("error", function (e) { out.errors.push(String(e.message)); });

  function $(s) { return document.querySelector(s); }
  function all(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
  function txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : null; }
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
  function send() { return fetch(P.get("sink"), { method: "POST", body: JSON.stringify(out) }); }

  function snap() {
    var inner = $("#panel-body .panel-inner");
    return {
      name: txt($(".dam-name")),
      hidden: $("#panel").classList.contains("is-hidden"),
      order: inner ? Array.prototype.map.call(inner.children, function (c) {
        return (c.className || c.tagName.toLowerCase()).split(" ")[0];
      }) : [],
      obs: all("#panel-body .obs-card").map(function (c) {
        return { cls: c.className, k: txt(c.querySelector(".k")), v: txt(c.querySelector(".v")),
                 sub: txt(c.querySelector(".sub")), w: Math.round(c.getBoundingClientRect().width) };
      }),
      flow: all("#panel-body .flow-cell").map(function (c) {
        var r = c.getBoundingClientRect();
        return { cls: c.className, badge: txt(c.querySelector(".badge")),
                 k: txt(c.querySelector(".k")), v: txt(c.querySelector(".v")),
                 w: Math.round(r.width), x: Math.round(r.left), y: Math.round(r.top),
                 clipped: c.scrollWidth > c.clientWidth + 1 };
      }),
      flowSrc: txt($("#panel-body .flow-src")),
      wsGoY: $("#ws-go") ? Math.round($("#ws-go").getBoundingClientRect().top) : null,
      flowBottom: $("#panel-body .flow") ? Math.round($("#panel-body .flow").getBoundingClientRect().bottom) : null,
      freshY: $("#panel-body .panel-freshness") ? Math.round($("#panel-body .panel-freshness").getBoundingClientRect().top) : null,
      wsGoBottom: $("#ws-go") ? Math.round($("#ws-go").getBoundingClientRect().bottom) : null,
      obsNone: txt($("#panel-body .obs-none")),
      lack: all("#panel-body .obs-lack li").map(txt),
      rateLack: all("#panel-body .rate-lack li").map(txt),
      rateLackDiff: !!$("#panel-body details.rate-lack--diff"),
      rateCards: all("#panel-body .rates .rate").map(function (c) {
        return txt(c.querySelector(".k span")) + "|" + txt(c.querySelector(".v"));
      }),
      bigDash: all("#panel-body .rate.is-nodata").length,
      facts: all("#panel-body .facts th").map(txt),
      factsText: txt($("#panel-body .facts")),
      fresh: txt($("#panel-body .panel-freshness")),
      wsGo: !!$("#ws-go"),
      wsStop: !!$("#ws-stop"),
      inflow: txt($("#ws-entry .ws-inflow")),
      wsText: txt($("#ws-entry")),
      overflowX: document.documentElement.scrollWidth > innerWidth ||
        ($("#panel").scrollWidth > $("#panel").clientWidth + 1)
    };
  }

  function setBasis(b) {
    var r = document.querySelector('input[name="basis"][value="' + b + '"]');
    r.checked = true;
    r.dispatchEvent(new Event("change"));
  }
  function open(id) {
    location.hash = "dam_id=" + id;
    return until(function () {
      return txt($(".dam-name")) === names[id] && !$("#panel").classList.contains("is-hidden");
    }).then(function () { return sleep(150); });
  }

  function panelMode() {
    var ids = (P.get("ids") || "").split(",").filter(Boolean);
    var chain = Promise.resolve();
    ids.forEach(function (id) {
      chain = chain.then(function () { return open(id); }).then(function () {
        var c = { irrigation: snap() };
        setBasis("effective");
        return sleep(100).then(function () {
          c.effective = snap();
          setBasis("irrigation");
          return sleep(100);
        }).then(function () {
          c.back = snap();
          out.cases[id] = c;
        });
      });
    });
    return chain;
  }

  function wsMode() {
    var A = P.get("a"), B = P.get("b"), C = P.get("c"), X = P.get("x");
    var c = out.cases;
    return open(A)
      .then(function () { c.a_before = snap(); $("#ws-go").click(); return until(function () { return $("#ws-stop"); }); })
      .then(function () { return sleep(1200); })
      .then(function () {
        c.a_open = snap();
        setBasis("effective");               // 集水域を開いたまま基準を切り替える
        return sleep(150);
      })
      .then(function () { c.a_open_effective = snap(); setBasis("irrigation"); return sleep(100); })
      .then(function () { return open(B); })   // 開いたまま別のダムへ
      .then(function () { c.b_switched = snap(); $("#ws-go").click(); return until(function () { return $("#ws-stop"); }); })
      .then(function () { return sleep(1200); })
      .then(function () { c.b_open = snap(); $("#ws-stop").click(); return sleep(300); })
      .then(function () { c.b_closed = snap(); return open(C); })
      .then(function () { $("#ws-go").click(); return until(function () { return $("#ws-stop"); }); })
      .then(function () { return sleep(1200); })
      .then(function () { c.c_open = snap(); $("#panel-close").click(); return sleep(300); })
      .then(function () { c.c_closed_panel = snap(); return open(X); })
      .then(function () { c.x_unsupported = snap(); return open(A); })
      .then(function () { c.a_again = snap(); });
  }

  function pinsShown() {
    return all(".dam-pin__num").filter(function (n) { return txt(n) !== "—"; }).length;
  }
  function pinsRef() {
    return all(".dam-pin__calc-mark").length;
  }
  function headerMode() {
    var c = out.cases;
    c.irrigation = txt($("#status-summary"));
    c.irrigation_pins = pinsShown();
    c.irrigation_ref = pinsRef();
    setBasis("effective");
    return sleep(200).then(function () {
      c.effective = txt($("#status-summary"));
      c.effective_pins = pinsShown();
      setBasis("irrigation");
      return sleep(200);
    }).then(function () {
      c.back = txt($("#status-summary"));
      c.back_pins = pinsShown();
    });
  }

  function boot() {
    until(function () { return window.maplibregl && document.querySelector(".dam-pin"); }, 60000)
      .then(function () { return fetch("./data/dams.json").then(function (r) { return r.json(); }); })
      .then(function (d) { d.dams.forEach(function (x) { names[x.id] = x.name; }); })
      .then(function () { return MODE === "ws" ? wsMode() : MODE === "header" ? headerMode() : panelMode(); })
      ["catch"](function (e) { out.errors.push(String(e && e.message || e)); })
      .then(send);
  }
  if (document.readyState === "complete") boot(); else window.addEventListener("load", boot);
})();
