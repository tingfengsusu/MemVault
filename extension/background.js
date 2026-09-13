// MemVault 采集助手 — MV3 Service Worker
// 点击插件图标 → 抓取当前页信息(选段/正文/商品/视频)→ POST 本地 MemVault
const API_BASE = "http://127.0.0.1:8765";
let healthy = false;

async function setBadge(text, color) {
  await chrome.action.setBadgeText({ text });
  if (color) await chrome.action.setBadgeBackgroundColor({ color });
}

async function checkHealth() {
  try {
    const r = await fetch(API_BASE + "/api/health");
    healthy = r.ok;
  } catch (e) {
    healthy = false;
  }
  await setBadge(healthy ? "" : "×", "#9ca3af");
}

function buildCapture(info) {
  const body = { type: info.type };
  if (info.url) body.url = info.url;
  if (info.title) body.title = info.title;
  if (info.type === "selection" || info.type === "page") body.text = info.text;
  if (info.type === "product") body.product = info.product;
  if (info.type === "video") body.video = info.video;
  return body;
}

chrome.action.onClicked.addListener(async (tab) => {
  await checkHealth();
  if (!healthy) {
    await setBadge("×", "#9ca3af");
    return;
  }
  if (!tab.id) return;
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: gatherPage,
    });
    const info = res && res.result;
    if (!info || info.error) {
      await setBadge("!", "#f59e0b");
    } else {
      const r = await fetch(API_BASE + "/api/capture", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildCapture(info)),
      });
      await setBadge(r.ok ? "✓" : "!", r.ok ? "#10b981" : "#ef4444");
    }
  } catch (e) {
    await setBadge("!", "#ef4444");
  }
  setTimeout(() => setBadge(healthy ? "" : "×"), 2500);
});

// 在页面上下文执行(必须自包含,不能引用外部变量)
function gatherPage() {
  try {
    const sel = (window.getSelection ? String(window.getSelection()) : "").trim();
    const u = location.href;
    const t = document.title;
    const v = document.querySelector("video");

    // B站视频页
    if (/bilibili\.com\/video|b23\.tv/.test(u)) {
      const m = u.match(/BV[0-9A-Za-z]{10}/);
      return {
        type: "video", url: u, title: t,
        video: { platform: "bilibili", id: m ? m[0] : "", t: v ? Math.floor(v.currentTime) : 0 },
      };
    }

    const og = (p) => {
      const el = document.querySelector(`meta[property="${p}"]`);
      return el ? el.content : "";
    };
    const host = location.host.replace(/^www\./, "");

    // 京东商品页
    if (/(^|\.)jd\.com$/.test(host)) {
      const nameEl = document.querySelector(".sku-name");
      const name = (nameEl ? nameEl.innerText : t).trim();
      const priceEl = document.querySelector(".price");
      const price = priceEl ? priceEl.innerText.trim().replace(/\s+/g, " ").slice(0, 20) : "";
      const imgEl = document.querySelector("#spec-img") || document.querySelector("img");
      return {
        type: "product", url: u, title: name,
        product: { name, price, image_url: imgEl ? imgEl.src || "" : "", shop: "京东" },
      };
    }

    // 淘宝 / 天猫
    if (/(^|\.)taobao\.com$/.test(host) || /(^|\.)tmall\.com$/.test(host)) {
      const nameEl = document.querySelector("h1");
      const name = (nameEl ? nameEl.innerText : t).trim();
      return {
        type: "product", url: u, title: name,
        product: { name, price: "", image_url: og("og:image"), shop: host.includes("tmall") ? "天猫" : "淘宝" },
      };
    }

    // 通用:有选中文字→选段,否则→整页正文
    if (sel.length > 10) return { type: "selection", url: u, title: t, text: sel };
    return { type: "page", url: u, title: t, text: (document.body ? document.body.innerText : "").slice(0, 8000) };
  } catch (e) {
    return { error: String(e) };
  }
}

chrome.runtime.onInstalled.addListener(checkHealth);
chrome.runtime.onStartup.addListener(checkHealth);
checkHealth();
setInterval(checkHealth, 30000);
