/* 4案のピンを同じコードから作る。実装用ではなく比較用。 */
window.PinLab = (function () {
  function binColor(v, bins, nodata) {
    if (v === null || v === undefined) return nodata;
    for (var i = 0; i < bins.length; i++) {
      var b = bins[i];
      if ((b.min === null || v >= b.min) && (b.max === null || v < b.max)) return b.color;
    }
    return nodata;
  }

  /* --- E案: 円形イラスト + 円弧ゲージ + 下部に小さく数字 ---------------
     ・リングを「色」だけでなく「長さ」で残量を示す（二重符号化）
     ・数字は画像を潰さないよう、下端に小さいスクリムだけ敷いて重ねる
     ・イラストが無い基も同じゲージを付けるので、混在しても言語が揃う   */
  function makeE(size, illust, v, color, opt) {
    opt = opt || {};
    var s = size, el = document.createElement("div");
    el.className = "pin"; el.style.width = el.style.height = s + "px";

    var R = 43, C = 2 * Math.PI * R;
    var pct = (v === null) ? 0 : Math.max(0, Math.min(100, v)) / 100;
    var sw = opt.stroke || 11;
    var hatch = "repeating-linear-gradient(-45deg,#c3ccd5,#c3ccd5 3px,#dde3ea 3px,#dde3ea 6px)";

    // 「減った分」が見えるよう、トラックは薄い同系色にする
    var trackCol = (v === null) ? "#ccd4dc" : hexA(color, .22);

    var svg =
      '<svg class="gauge" viewBox="0 0 100 100">' +
        '<circle cx="50" cy="50" r="' + R + '" fill="none" stroke="#fff" stroke-width="' + (sw + 4) + '"/>' +
        '<circle cx="50" cy="50" r="' + R + '" fill="none" stroke="' + trackCol + '" stroke-width="' + sw + '"/>' +
        (v === null ? "" :
        '<circle cx="50" cy="50" r="' + R + '" fill="none" stroke="' + color + '" stroke-width="' + sw +
        '" stroke-linecap="round" stroke-dasharray="' + (pct * C).toFixed(1) + " " + C.toFixed(1) +
        '" transform="rotate(-90 50 50)"/>') +
      "</svg>";

    // 中身: イラスト、無ければ淡い同系色（リングの弧が読めるよう彩度を落とす）
    var inner, numColor;
    if (illust) {
      inner = '<img src="' + illust + '"><span class="scrim"></span>';
      numColor = "#fff";
    } else if (v === null) {
      inner = '<div class="solid" style="background:' + hatch + '"></div>';
      numColor = "#5b6875";
    } else {
      inner = '<div class="solid" style="background:' + hexA(color, .20) + '"></div>';
      numColor = shade(color, -.28);
    }

    var num = (v === null) ? "—" : String(Math.round(v));
    var fs = Math.round(s * (illust ? 0.27 : 0.34));
    var pos = illust ? "num" : "num center";

    el.innerHTML = svg +
      '<div class="disc" style="inset:' + (sw * 0.60) + '%">' + inner +
      '<span class="' + pos + '" style="font-size:' + fs + 'px;color:' + numColor + '">' + num + "</span></div>";
    return el;
  }

  function shade(hex, amt) {
    var n = parseInt(hex.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    var f = function (c) { return Math.max(0, Math.min(255, Math.round(c + 255 * amt))); };
    return "rgb(" + f(r) + "," + f(g) + "," + f(b) + ")";
  }

  // variant: A|B|C|D  size: px  illust: URL or null  v: 数値 or null
  function make(variant, size, illust, v, color) {
    var s = size, el = document.createElement("div");
    el.className = "pin";
    el.style.width = el.style.height = s + "px";
    var num = (v === null) ? "—" : String(Math.round(v));
    var fs = Math.round(s * 0.32);
    var bw = Math.max(2, Math.round(s * 0.065));       // 白ふち
    var ring = Math.max(3, Math.round(s * 0.11));      // 色リング
    var hatch = "repeating-linear-gradient(-45deg,#94a3b8,#94a3b8 4px,#b6c0cb 4px,#b6c0cb 8px)";
    var solid = (v === null) ? hatch : color;

    // イラストが無い場合は、どの案も現状(D)と同じ見え方になる
    if (variant === "E") return makeE(size, illust, v, color);

    if (!illust || variant === "D") {
      el.innerHTML =
        '<div class="body" style="background:' + solid + ';border:' + bw + 'px solid #fff;' +
        'font-size:' + (v === null ? Math.round(s * 0.42) : fs) + 'px">' + num + "</div>";
      if (variant === "C" && illust) {
        // C はイラストが無くても badge は付けない（無いので）
      }
      return el;
    }

    if (variant === "A") {
      // 円形イラスト + 外周に貯水率の色リング（数字なし）
      el.innerHTML =
        '<div class="body" style="border:' + ring + "px solid " + (v === null ? "#94a3b8" : color) +
        ';box-shadow:0 0 0 ' + Math.max(1, Math.round(s * 0.035)) + 'px #fff inset,' +
        '0 0 0 ' + Math.max(1, Math.round(s * 0.03)) + 'px #fff;overflow:hidden">' +
        '<img src="' + illust + '">' +
        (v === null ? '<span class="nd">—</span>' : "") + "</div>";
    } else if (variant === "B") {
      // イラスト + 下部に貯水率色の半透明帯 + 数字
      var bandH = Math.round(s * 0.42);
      el.innerHTML =
        '<div class="body" style="border:' + bw + 'px solid #fff;overflow:hidden">' +
        '<img src="' + illust + '">' +
        '<div class="band" style="height:' + bandH + "px;background:" +
        (v === null ? "rgba(120,132,146,.88)" : hexA(color, .88)) +
        ";font-size:" + Math.round(s * 0.3) + 'px">' + num + "</div></div>";
    } else if (variant === "C") {
      // 色付き円が主体 + 右下に小さくイラスト
      var bs = Math.round(s * 0.46);
      el.innerHTML =
        '<div class="body" style="background:' + solid + ";border:" + bw +
        'px solid #fff;font-size:' + fs + 'px">' + num + "</div>" +
        '<div class="badge" style="width:' + bs + "px;height:" + bs +
        'px;border:' + Math.max(1.5, Math.round(s * 0.04)) + 'px solid #fff">' +
        '<img src="' + illust + '"></div>';
    }
    return el;
  }
  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }
  return { make: make, binColor: binColor };
})();
