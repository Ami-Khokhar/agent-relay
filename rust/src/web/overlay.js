/* Overlay injection for reviewable artifacts. Runs inside the artifact page.
 * Adds a floating comment control and comment blobs, and talks to the review
 * view (parent frame) with postMessage, same origin only.
 */
(function () {
  "use strict";
  var script = document.currentScript || {};
  var cfg = {
    slug: script.dataset ? script.dataset.slug : "",
    version: script.dataset ? Number(script.dataset.version) : 0,
    parent: script.dataset ? script.dataset.parent || window.location.origin : ""
  };
  var SLUG = cfg.slug || "";
  var VERSION = cfg.version || 0;
  if (!SLUG || window.parent === window) return;

  var LAYER_ID = "__artifacts_layer";
  var BAR_ID = "__artifacts_bar";
  var BLUE = "#2a5db0";
  var RED = "#a4442a";
  var mode = false;
  var pinned = 0;

  function layer() {
    var el = document.getElementById(LAYER_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = LAYER_ID;
    el.setAttribute("aria-hidden", "true");
    el.style.cssText =
      "position:absolute;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483000";
    document.body.appendChild(el);
    return el;
  }

  function bar() {
    var el = document.getElementById(BAR_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = BAR_ID;
    el.style.cssText =
      "position:fixed;right:14px;bottom:14px;z-index:2147483001;display:flex;align-items:center;" +
      "gap:8px;font:600 12px/1 ui-sans-serif,system-ui,sans-serif";
    var button = document.createElement("button");
    button.type = "button";
    button.id = "__artifacts_toggle";
    button.style.cssText =
      "padding:9px 14px;border-radius:999px;border:1px solid rgba(255,255,255,.5);color:#fff;" +
      "background:" + BLUE + ";box-shadow:0 3px 10px rgba(0,0,0,.28);cursor:pointer;" +
      "font:600 12px/1 ui-sans-serif,system-ui,sans-serif";
    button.textContent = "Comment";
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      setMode(!mode);
      post({ type: "mode", on: mode });
    });
    el.appendChild(button);
    var hint = document.createElement("div");
    hint.id = "__artifacts_hint";
    if (window.innerWidth < 720) return el;
    hint.textContent = "Click Comment, then click anything on the page";
    hint.style.cssText =
      "position:fixed;right:14px;bottom:52px;z-index:2147483001;max-width:240px;" +
      "padding:8px 11px;border-radius:8px;background:#16181d;color:#fff;opacity:.92;" +
      "font:500 12px/1.4 ui-sans-serif,system-ui,sans-serif;box-shadow:0 3px 12px rgba(0,0,0,.3)";
    el.appendChild(hint);
    var dismiss = function () {
      hint.remove();
      document.removeEventListener("click", dismiss, true);
      window.setTimeout(function () {}, 0);
    };
    document.addEventListener("click", dismiss, true);
    window.setTimeout(function () {
      if (hint.parentNode) hint.remove();
    }, 15000);
    document.body.appendChild(el);
    return el;
  }

  function setBar() {
    var button = document.getElementById("__artifacts_toggle");
    if (!button) return;
    button.textContent = mode ? "Click an element · Done" : "Comment";
    button.style.background = mode ? RED : BLUE;
    button.setAttribute("aria-pressed", String(mode));
  }

  function selectorFor(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id && document.querySelectorAll("#" + CSS.escape(el.id)).length === 1) {
      return "#" + CSS.escape(el.id);
    }
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node === document.body) {
        parts.unshift("body");
        break;
      }
      if (node.id && document.querySelectorAll("#" + CSS.escape(node.id)).length === 1) {
        parts.unshift("#" + CSS.escape(node.id));
        break;
      }
      var index = 1;
      var sibling = node;
      while ((sibling = sibling.previousElementSibling)) {
        if (sibling.tagName === node.tagName) index += 1;
      }
      parts.unshift(node.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function quoteFor(el) {
    var selection = window.getSelection();
    var text = selection && String(selection).trim();
    if (text) return text.slice(0, 200);
    var own = (el.textContent || "").replace(/\s+/g, " ").trim();
    return own.slice(0, 200);
  }

  function textOf(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  // A selector alone can land on a different element after a re-render, so the
  // quote has to still be there; otherwise the comment is reported as lost
  // rather than pinned to the wrong place.
  function matchesQuote(el, quote) {
    var probe = (quote || "").trim().slice(0, 24);
    if (!probe) return true;
    return textOf(el).indexOf(probe) !== -1;
  }

  function findElement(anchor) {
    if (!anchor) return null;
    if (anchor.selector) {
      try {
        var found = document.querySelector(anchor.selector);
        if (found && matchesQuote(found, anchor.quote)) return found;
      } catch (error) {
        /* invalid selector: fall through to the quote */
      }
    }
    var quote = (anchor.quote || "").trim();
    if (quote.length >= 8) {
      var probe = quote.slice(0, 40);
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        if (node.nodeValue && node.nodeValue.indexOf(probe) !== -1) {
          return node.parentElement;
        }
      }
    }
    return null;
  }

  // Blobs sit in the margin beside the element, never on top of the text, so a
  // second comment can always be placed on the same element.
  function positionFor(anchor, el) {
    var rect = el.getBoundingClientRect();
    var fy = typeof anchor.y === "number" ? anchor.y : 0.5;
    var top = rect.top + window.scrollY + rect.height * Math.min(Math.max(fy, 0.15), 0.85);
    // Stay inside the visible viewport: a wide document must not push the blob
    // out of sight where the person cannot click it.
    var edge = 15;
    var viewLeft = window.scrollX + edge;
    var viewRight = window.scrollX + window.innerWidth - edge;
    var leftGutter = rect.left + window.scrollX - 22;
    var rightGutter = rect.right + window.scrollX + 22;
    if (leftGutter >= viewLeft) return { left: leftGutter, top: top };
    if (rightGutter <= viewRight) return { left: rightGutter, top: top };
    // No margin on either side (a phone): hug the right edge of the viewport,
    // never the first words of the line and never the element above it.
    return { left: viewRight, top: top };
  }

  // Two comments on the same element collide; push each later one down so both
  // stay clickable.
  function collides(used, spot) {
    for (var index = 0; index < used.length; index += 1) {
      if (Math.abs(used[index].left - spot.left) < 26 &&
          Math.abs(used[index].top - spot.top) < 20) return true;
    }
    return false;
  }

  function place(anchor, el, used) {
    var spot = positionFor(anchor, el);
    var guard = 0;
    while (collides(used, spot) && guard < 12) {
      spot.top += 26;
      guard += 1;
    }
    used.push(spot);
    return spot;
  }

  function makeBlob(comment, number, spot) {
    var anchor = comment.anchor || {};
    var blob = document.createElement("button");
    blob.type = "button";
    blob.textContent = String(number);
    var base =
      "position:absolute;transform:translate(-50%,-50%);min-width:26px;height:24px;" +
      "padding:0 6px;border-radius:12px 12px 12px 3px;border:2px solid #fff;cursor:pointer;" +
      "font:650 12px/20px ui-sans-serif,system-ui,sans-serif;pointer-events:auto;" +
      "box-shadow:0 2px 6px rgba(0,0,0,.32);color:#fff;";
    if (!spot) {
      // The anchor element is gone in this version. Park the blob in the corner
      // of the viewport so it stays visible and reachable.
      blob.style.cssText = base + "position:fixed;right:10px;transform:none;" +
        "background:#8a8f88;border-style:dashed;opacity:.9;top:calc(10px + " +
        (number - 1) * 30 + "px);";
      blob.title = "Anchor lost in this version: " + ((comment.body || "").slice(0, 100));
    } else {
      var transform = spot.above ? "translate(-50%,-100%)" : "translate(-50%,-50%)";
      blob.style.cssText = base + "transform:" + transform + ";left:" + spot.left +
        "px;top:" + spot.top + "px;";
      blob.style.background = comment.status === "resolved" ? "#8a8f88" : BLUE;
      blob.title = (comment.body || "").slice(0, 120);
    }
    blob.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      post({ type: "open", id: comment.id });
    });
    return blob;
  }

  function post(message) {
    message.slug = SLUG;
    // The artifact lives on its own origin, so the review page is named
    // explicitly instead of trusting where this document came from.
    window.parent.postMessage(message, cfg.parent || "*");
  }

  function draw(comments) {
    var host = layer();
    host.textContent = "";
    pinned = comments.length;
    var used = [];
    comments.forEach(function (comment, index) {
      var el = findElement(comment.anchor || {});
      var spot = el ? place(comment.anchor || {}, el, used) : null;
      var number = typeof comment.number === "number" ? comment.number : index + 1;
      host.appendChild(makeBlob(comment, number, spot));
    });
    setPinsInert(mode);
    setBar();
  }

  function setPinsInert(inert) {
    var host = document.getElementById(LAYER_ID);
    if (!host) return;
    var blobs = host.querySelectorAll("button");
    for (var index = 0; index < blobs.length; index += 1) {
      blobs[index].style.pointerEvents = inert ? "none" : "auto";
      blobs[index].style.opacity = inert ? "0.35" : "1";
    }
  }

  function setMode(next) {
    mode = !!next;
    setPinsInert(mode);
    document.documentElement.style.cursor = mode ? "crosshair" : "";
    if (mode) {
      document.addEventListener("mouseover", mark, true);
    } else {
      document.removeEventListener("mouseover", mark, true);
      clearMark();
    }
    setBar();
  }

  var marked = null;

  function mark(event) {
    if (!mode || !event.target || event.target.closest("#" + BAR_ID)) return;
    if (marked && marked !== event.target) clearMark();
    marked = event.target;
    marked.style.outline = "2px solid " + RED;
    marked.style.outlineOffset = "2px";
  }

  function clearMark() {
    if (marked) {
      marked.style.outline = "";
      marked.style.outlineOffset = "";
      marked = null;
    }
  }

  function onClick(event) {
    if (!mode) return;
    var el = event.target;
    if (!el || el.closest("#" + LAYER_ID) || el.closest("#" + BAR_ID)) return;
    event.preventDefault();
    event.stopPropagation();
    var rect = el.getBoundingClientRect();
    post({
      type: "pick",
      version: VERSION,
      anchor: {
        selector: selectorFor(el),
        quote: quoteFor(el),
        x: rect.width ? (event.clientX - rect.left) / rect.width : 0.5,
        y: rect.height ? (event.clientY - rect.top) / rect.height : 0.5,
      },
    });
    setMode(false);
    post({ type: "mode", on: false });
  }

  function highlight(anchor) {
    var el = findElement(anchor);
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    var previous = el.style.outline;
    el.style.outline = "3px solid " + BLUE;
    el.style.outlineOffset = "2px";
    setTimeout(function () {
      el.style.outline = previous;
    }, 1600);
  }

  window.addEventListener(
    "message",
    function (event) {
      if (cfg.parent && event.origin !== cfg.parent) return;
      var data = event.data || {};
      switch (data.type) {
        case "mode":
          setMode(data.on);
          break;
        case "comments":
          draw(data.comments || []);
          break;
        case "focus":
          highlight(data.anchor || {});
          break;
        default:
          break;
      }
    },
    false
  );

  document.addEventListener("click", onClick, true);
  bar();
  setBar();
  post({ type: "ready", version: VERSION });
})();
