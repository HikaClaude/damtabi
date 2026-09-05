/* ダム旅 / DAM TABI — Service Worker
 *
 * 目的は 2 つ。
 *   1. ホーム画面に追加できるようにする（PWA のインストール要件）
 *   2. 山間部で電波が弱いときも、直前に見た内容を読めるようにする
 *
 * 最優先の原則: **古い貯水率を現在値のように見せない。**
 *
 * そのため、貯水率が載っているもの（data/*.json と HTML ページ本体）は
 * すべてネットワーク優先にしてある。キャッシュを返すのは通信できないときだけで、
 * そのときは必ず「古い内容である」ことを画面に出す。
 *   - JSON  … stale フラグを立てて返す（地図画面が見出しに警告を出す）
 *   - HTML  … <!--STALE-SLOT--> を警告バーに差し替えて返す
 *
 * CSS / JS / 画像 / アイコンのように古くても害のないものだけキャッシュ優先。
 */

var VERSION = "v6";   // style.css / app.js を更新したら上げる（旧キャッシュを破棄させるため）
var SHELL = "shell-" + VERSION;
var PAGES = "pages-" + VERSION;
var DATA = "data-" + VERSION;

/* 値を含まない静的アセットだけ。HTML はここに入れない（値が埋まっているため） */
var SHELL_FILES = [
  "./style.css",
  "./app.js",
  "./page.css",
  "./manifest.json",
  "./img/icon.svg"
];

var STALE_MARKER = "<!--STALE-SLOT-->";
var STALE_BAR =
  '<p class="stale-bar" role="status">オフラインのため、端末に保存された内容を表示しています。' +
  '貯水率は最新ではない可能性があります。</p>';

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(SHELL).then(function (c) {
      // 1 つでも失敗すると install ごと失敗するので、個別に入れる
      return Promise.all(SHELL_FILES.map(function (u) {
        return c.add(u).catch(function () {});
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== SHELL && k !== PAGES && k !== DATA) return caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

/** キャッシュ済み HTML に「古い内容です」バーを差し込んで返す。 */
function staleHtml(res) {
  return res.text().then(function (html) {
    return new Response(html.replace(STALE_MARKER, STALE_BAR), {
      status: 200,
      headers: { "Content-Type": "text/html; charset=utf-8" }
    });
  });
}

/** キャッシュ済み JSON に stale フラグを立てて返す。 */
function staleJson(res) {
  return res.json().then(function (j) {
    j.stale = true;
    return new Response(JSON.stringify(j), {
      headers: { "Content-Type": "application/json" }
    });
  }).catch(function () { return res; });
}

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // 地図タイル・CDN はブラウザ任せ

  var isDoc = req.mode === "navigate" ||
    (req.headers.get("accept") || "").indexOf("text/html") !== -1;
  var isData = url.pathname.indexOf("/data/") !== -1;

  // --- 貯水率を含むもの（JSON と HTML）はネットワーク優先
  if (isData || isDoc) {
    e.respondWith(
      fetch(req).then(function (res) {
        if (res && res.status === 200) {
          var copy = res.clone();
          caches.open(isData ? DATA : PAGES).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () {
        return caches.match(req).then(function (hit) {
          if (!hit) {
            return isData
              ? new Response('{"error":"offline"}',
                  { status: 503, headers: { "Content-Type": "application/json" } })
              : new Response(
                  "<!DOCTYPE html><meta charset=utf-8><p style=\"font:16px sans-serif;padding:24px\">" +
                  "オフラインです。このページはまだ端末に保存されていません。</p>",
                  { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
          }
          return isData ? staleJson(hit) : staleHtml(hit);
        });
      })
    );
    return;
  }

  // --- CSS / JS / 画像などはキャッシュ優先（裏で更新）
  e.respondWith(
    caches.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        if (res && res.status === 200 && res.type === "basic") {
          var copy = res.clone();
          caches.open(SHELL).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () { return hit; });
      return hit || net;
    })
  );
});
