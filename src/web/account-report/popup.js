const TEXT = {
  es: { heading: "Reportar cuenta", account: "Cuenta", status: "Estado observado", note: "Nota (opcional)",
    send: "Enviar reporte", sending: "Guardando…", sent: "Reporte enviado a Cuentas.",
    inhabilitado: "Inhabilitado", suspendido: "Suspendido", videoselfie: "Videoselfie", codigo: "Código de teléfono/SMS/WhatsApp",
    choose: "Selecciona una cuenta", unknown: "Sin cuenta guardada · reportar perfil y red",
    unsupported: "Abre Facebook, Instagram, TikTok, X, Gmail u Outlook para reportar.",
    failed: "No se pudo guardar el reporte. Conserva este formulario y reintenta.",
    changed: "La cuenta o el permiso cambió. Reabre el perfil antes de reportar." },
  en: { heading: "Report account", account: "Account", status: "Observed status", note: "Note (optional)",
    send: "Send report", sending: "Saving…", sent: "Report sent to Accounts.",
    inhabilitado: "Disabled", suspendido: "Suspended", videoselfie: "Video selfie", codigo: "Phone/SMS/WhatsApp code",
    choose: "Select an account", unknown: "No saved account · report profile and platform",
    unsupported: "Open Facebook, Instagram, TikTok, X, Gmail or Outlook to report.",
    failed: "The report could not be saved. Keep this form open and retry.",
    changed: "The account or permission changed. Reopen the profile before reporting." },
  vi: { heading: "Báo cáo tài khoản", account: "Tài khoản", status: "Trạng thái quan sát", note: "Ghi chú (tuỳ chọn)",
    send: "Gửi báo cáo", sending: "Đang lưu…", sent: "Đã gửi báo cáo cho bộ phận tài khoản.",
    inhabilitado: "Bị vô hiệu hoá", suspendido: "Bị tạm ngưng", videoselfie: "Yêu cầu video selfie", codigo: "Mã điện thoại/SMS/WhatsApp",
    choose: "Chọn tài khoản", unknown: "Chưa lưu tài khoản · báo cáo profile và nền tảng",
    unsupported: "Mở Facebook, Instagram, TikTok, X, Gmail hoặc Outlook để báo cáo.",
    failed: "Không lưu được báo cáo. Giữ biểu mẫu này và thử lại.",
    changed: "Tài khoản hoặc quyền đã thay đổi. Mở lại profile trước khi báo cáo." }
};
const PLATFORMS = { facebook: "Facebook", instagram: "Instagram", tiktok: "TikTok", x: "X", gmail: "Gmail", hotmail: "Outlook" };

function platformFor(url) {
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
  const host = parsed.hostname;
  for (const [platform, domains] of Object.entries({
    facebook: ["facebook.com"], instagram: ["instagram.com"], tiktok: ["tiktok.com"],
    x: ["x.com", "twitter.com"], gmail: ["mail.google.com", "accounts.google.com", "myaccount.google.com"],
    hotmail: ["outlook.live.com", "outlook.office.com", "outlook.office365.com", "login.live.com", "login.microsoftonline.com"]
  })) {
    if (domains.some((domain) => host === domain || host.endsWith("." + domain))) return platform;
  }
  return null;
}

async function init() {
  const config = await (await fetch("config.json", { cache: "no-store" })).json();
  const words = TEXT[config.lang] || TEXT.en;
  document.documentElement.lang = config.lang || "en";
  const $ = (id) => document.getElementById(id);
  for (const [id, key] of Object.entries({ heading: "heading", "account-label": "account", "status-label": "status", "note-label": "note", send: "send" })) {
    $(id).textContent = words[key];
  }
  $("profile").textContent = config.profile;
  const ext = typeof browser === "undefined" ? chrome : browser;
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  const platform = platformFor(tab && tab.url);
  if (!platform) { $("message").textContent = words.unsupported; return; }
  $("network").textContent = PLATFORMS[platform];
  const accounts = config.accounts.filter((a) => a.platform === platform);
  const add = (select, value, text) => {
    const option = document.createElement("option");
    option.value = value; option.textContent = text; select.append(option);
  };
  if (accounts.length > 1) add($("account"), "", words.choose);
  for (const account of accounts) add($("account"), account.id, account.username);
  if (!accounts.length) { add($("account"), "", words.unknown); $("account").required = false; }
  add($("status"), "", words.choose);
  for (const status of ["inhabilitado", "suspendido", "videoselfie", "codigo"]) add($("status"), status, words[status]);
  $("report-form").hidden = false;
  let attempt = null;
  $("report-form").addEventListener("input", () => {
    $("send").disabled = false; $("message").textContent = "";
  });
  $("report-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = { platform, account_id: $("account").value || null, status: $("status").value, note: $("note").value };
    const signature = JSON.stringify(fields);
    if (!attempt || attempt.signature !== signature) attempt = { signature, id: crypto.randomUUID().replaceAll("-", "") };
    $("send").disabled = true; $("send").textContent = words.sending;
    $("message").textContent = "";
    // Freeze the submitted fields until the server acknowledges this exact report.
    for (const field of [$("account"), $("status"), $("note")]) field.disabled = true;
    try {
      const response = await fetch(config.url, { method: "POST", headers: { "Content-Type": "application/json", "X-Report-Token": config.token },
        body: JSON.stringify({ ...fields, request_id: attempt.id }), signal: AbortSignal.timeout(20000) });
      if (!response.ok) throw new Error([400, 401, 403, 404, 409, 422].includes(response.status) ? words.changed : words.failed);
      const receipt = await response.json();
      if (receipt.id !== attempt.id) throw new Error(words.failed);
      $("message").dataset.error = "false"; $("message").textContent = words.sent;
      attempt = null;
    } catch (error) {
      $("message").dataset.error = "true";
      $("message").textContent = error.name === "Error" ? error.message : words.failed;
      $("send").disabled = false;
    } finally {
      $("send").textContent = words.send;
      for (const field of [$("account"), $("status"), $("note")]) field.disabled = false;
    }
  });
}
init().catch(() => {
  document.getElementById("message").textContent = "FoxProfile: no se pudo abrir el formulario / could not open the form.";
});
