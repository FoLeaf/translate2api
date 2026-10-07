/* TransHub 后台公共 JS */
async function api(path, opts = {}) {
  const o = Object.assign({ headers: { "X-Requested-With": "fetch" } }, opts);
  if (o.body && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.includes("/admin/api/login")) {
    location.href = "/admin/login";
    throw new Error("unauthorized");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok && data && data.detail) data.ok = false;
  return data || { ok: r.ok };
}

let _toastTimer = null;
function toast(msg, ms = 2600) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div"); el.id = "toast"; document.body.appendChild(el);
  }
  el.textContent = msg; el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), ms);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板");
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
    toast("已复制到剪贴板");
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function statusBadge(ok) {
  return ok ? '<span class="badge ok">正常</span>' : '<span class="badge bad">未就绪</span>';
}
