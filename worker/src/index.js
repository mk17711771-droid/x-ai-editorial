// LINE Webhook 受信用 Cloudflare Worker（無料プラン）
//
// - 承認/却下ボタン（postback） → GitHub repository_dispatch で decide-draft ワークフローを起動
// - 「停止」 → GitHubリポジトリ変数 BOT_ENABLED=false（自動投稿・承認投稿をすべて止める）
// - 「再開」 → BOT_ENABLED=true ＋ 異常停止状態を解除
// - 「全自動」/「承認制」 → GitHubリポジトリ変数 APPROVAL_MODE を切り替え
// - 「状態」 → 現在の稼働状態を返信
// - 「ID」   → 送信者のLINEユーザーIDを返信（初期設定用）
// - GET /note/<記事ID>?k=<管理者ID> → noteの記事をコピーしやすく表示（タイトル・本文・画像）
//
// - 定時（Cron Triggers）→ 投稿枠の時刻に GitHub の generate-drafts を起動
//   （GitHubの定時実行は数時間遅れたり飛ばされたりするため、時刻はCloudflareが受け持つ）
//
// 返信（Reply API）はLINEの無料通数にカウントされない。

const GH_API = "https://api.github.com";
// 環境変数が消えても動くよう、リポジトリ名は既定値を持つ（秘密情報ではない）
const repoOf = (env) => env.GITHUB_REPO || "mk17711771-droid/x-ai-editorial";

export default {
  async scheduled(event, env, ctx) {
    // 5分おきに呼ばれ、その日のその枠の「投稿する時刻」に当たったときだけGitHubを起動する。
    // 枠の判定（平日/週末・どの枠か）はGitHub側で現在時刻から行う
    const t = new Date(event.scheduledTime);
    if (isPostingTick(t)) ctx.waitUntil(dispatch(env, "generate", { at: t.toISOString() }));
    // note記事: 水・土の日本時間7:40（UTCでは火・金の22:40）
    if ([2, 5].includes(t.getUTCDay()) && t.getUTCHours() === 22 && t.getUTCMinutes() === 40) {
      ctx.waitUntil(dispatch(env, "note_article", { at: t.toISOString() }));
    }
  },

  async fetch(request, env) {
    if (request.method === "GET") {
      const url = new URL(request.url);
      if (url.pathname.startsWith("/note/")) return notePage(url, env);
      if (url.pathname === "/health") return health(env);
      return new Response("ok");
    }
    if (request.method !== "POST") return new Response("ok");
    const body = await request.text();
    if (!(await verifySignature(body, request.headers.get("x-line-signature"), env.LINE_CHANNEL_SECRET))) {
      return new Response("invalid signature", { status: 401 });
    }
    const { events = [] } = JSON.parse(body);
    for (const ev of events) {
      try {
        await handle(ev, env);
      } catch (e) {
        if (ev.replyToken) await reply(env, ev.replyToken, `エラーが発生しました: ${e.message}`);
      }
    }
    return new Response("ok");
  },
};

async function handle(ev, env) {
  const userId = ev.source?.userId;
  const text = ev.type === "message" && ev.message.type === "text" ? ev.message.text.trim() : "";

  if (text.toUpperCase() === "ID") {
    return reply(env, ev.replyToken, `あなたのLINEユーザーID:\n${userId}`);
  }
  // 管理者以外の操作はすべて無視する
  if (!env.LINE_ADMIN_USER_ID || userId !== env.LINE_ADMIN_USER_ID) return;

  if (ev.type === "postback") {
    const p = new URLSearchParams(ev.postback.data);
    const action = p.get("a");
    const id = p.get("id");
    // 操作の中身はGitHub側（Python）で検証する。ここでは形式だけ確認して転送する
    if (!/^[a-z_]+$/.test(action || "") || !/^[\w-]+$/.test(id || "")) {
      return reply(env, ev.replyToken, "不正な操作です");
    }
    await dispatch(env, "draft_decision", { id, action });
    const msg = {
      approve: "✅ 承認しました。投稿直前の最終確認をしてから投稿します（1〜2分）。",
      delete: "🗑 削除を受け付けました（1〜2分）。",
    }[action] || "却下しました。理由は今後の採点基準の改善に使います。";
    return reply(env, ev.replyToken, msg);
  }

  if (text === "停止") {
    await setVariable(env, "BOT_ENABLED", "false");
    return reply(env, ev.replyToken, "⏸ 自動運用を停止しました。再開するときは「再開」と送ってください。");
  }
  if (text === "再開") {
    await setVariable(env, "BOT_ENABLED", "true");
    await dispatch(env, "resume", {});
    return reply(env, ev.replyToken, "▶ 自動運用を再開します。");
  }
  if (text === "全自動") {
    await setVariable(env, "APPROVAL_MODE", "false");
    return reply(env, ev.replyToken, "🤖 全自動モードにしました。80点以上の案を自動で投稿し、投稿後にLINEでお知らせします（削除ボタン付き）。\n承認制に戻すときは「承認制」と送ってください。");
  }
  if (text === "承認制") {
    await setVariable(env, "APPROVAL_MODE", "true");
    return reply(env, ev.replyToken, "✋ 承認制にしました。投稿前にLINEで承認をお願いします。");
  }
  if (text === "状態") {
    const v = await getVariable(env, "BOT_ENABLED");
    const mode = await getVariable(env, "APPROVAL_MODE");
    return reply(env, ev.replyToken,
      `稼働: ${v === "false" ? "停止中" : "稼働中"}\nモード: ${mode === "false" ? "全自動" : "承認制"}`);
  }
  if (text) {
    return reply(env, ev.replyToken, "使えるコマンド: 停止 / 再開 / 全自動 / 承認制 / 状態 / ID");
  }
}

async function verifySignature(body, signature, secret) {
  if (!signature || !secret) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));
  return timingSafeEqual(expected, signature);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

async function reply(env, replyToken, text) {
  if (!replyToken) return;
  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}` },
    body: JSON.stringify({ replyToken, messages: [{ type: "text", text }] }),
  });
}

function gh(env, path, init = {}) {
  return fetch(`${GH_API}/repos/${repoOf(env)}${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "x-ai-editorial-worker",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
}

// 投稿枠ごとの「起動してよい時間帯」（UTC・分）。枠の時刻（日本時間）の約22分前から50分間。
// その中のどの5分刻みで起動するかを、日付と枠から決まる乱数で毎日変える（毎日同じ時刻に投稿しない）
const POST_WINDOWS = [
  { start: 22 * 60 + 20, len: 50 }, // 07:42枠 → 07:20〜08:10
  { start: 0 * 60 + 55, len: 50 },  // 10:17枠 → 09:55〜10:45
  { start: 3 * 60 + 5, len: 50 },   // 12:26枠 → 12:05〜12:55
  { start: 11 * 60 + 25, len: 50 }, // 20:47枠 → 20:25〜21:15
  { start: 13 * 60 + 50, len: 50 }, // 23:13枠 → 22:50〜23:40
];

function dailyRandom(key) {
  let h = 2166136261;
  for (const c of key) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0;
  return h;
}

function isPostingTick(t) {
  const minute = t.getUTCHours() * 60 + t.getUTCMinutes();
  const jstDate = new Date(t.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 10);
  return POST_WINDOWS.some((w, i) => {
    const off = (minute - w.start + 1440) % 1440;
    if (off >= w.len) return false;
    const target = dailyRandom(`${jstDate}-${i}`) % (w.len / 5);
    return Math.floor(off / 5) === target;
  });
}

// 稼働確認（デプロイ後にGitHub Actionsから呼ぶ）。秘密の値そのものは返さない
async function health(env) {
  const gh = await fetch(`${GH_API}/repos/${repoOf(env)}`, {
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, "User-Agent": "x-ai-editorial-line", Accept: "application/vnd.github+json" },
  });
  const body = {
    github_token: gh.status,
    line_secret: Boolean(env.LINE_CHANNEL_SECRET),
    line_token: Boolean(env.LINE_CHANNEL_ACCESS_TOKEN),
    admin_id: Boolean(env.LINE_ADMIN_USER_ID),
    posting_tick_now: isPostingTick(new Date()),
  };
  const ok = gh.status === 200 && body.line_secret && body.line_token && body.admin_id;
  return new Response(JSON.stringify({ ok, ...body }), { status: ok ? 200 : 500, headers: { "Content-Type": "application/json" } });
}

async function dispatch(env, eventType, payload) {
  const r = await gh(env, "/dispatches", {
    method: "POST",
    body: JSON.stringify({ event_type: eventType, client_payload: payload }),
  });
  if (!r.ok) throw new Error(`GitHub dispatch失敗 ${r.status}`);
}

async function getVariable(env, name) {
  const r = await gh(env, `/actions/variables/${name}`);
  if (r.status === 404) return null;
  return (await r.json()).value;
}

async function setVariable(env, name, value) {
  let r = await gh(env, `/actions/variables/${name}`, { method: "PATCH", body: JSON.stringify({ name, value }) });
  if (r.status === 404) {
    r = await gh(env, "/actions/variables", { method: "POST", body: JSON.stringify({ name, value }) });
  }
  if (!r.ok) throw new Error(`GitHub変数の更新失敗 ${r.status}`);
}

// ---------------------------------------------------------------- noteの記事のコピー用ページ

async function ghRaw(env, path) {
  const r = await fetch(`${GH_API}/repos/${repoOf(env)}/contents/${path}`, {
    headers: {
      Accept: "application/vnd.github.raw+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "x-ai-editorial-worker",
    },
  });
  return r.ok ? r : null;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function notePage(url, env) {
  const m = url.pathname.match(/^\/note\/(\d{8}-\d{4})(?:\/(image\d+\.png))?$/);
  const key = url.searchParams.get("k");
  if (!m || !env.LINE_ADMIN_USER_ID || key !== env.LINE_ADMIN_USER_ID) {
    return new Response("not found", { status: 404 });
  }
  const [, id, image] = m;
  const dir = `data/note_articles/${id}`;
  if (image) {
    const r = await ghRaw(env, `${dir}/${image}`);
    if (!r) return new Response("not found", { status: 404 });
    return new Response(r.body, { headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=3600" } });
  }
  const [metaR, htmlR] = await Promise.all([ghRaw(env, `${dir}/meta.json`), ghRaw(env, `${dir}/article.html`)]);
  if (!metaR || !htmlR) return new Response("記事が見つかりません", { status: 404 });
  const meta = await metaR.json();
  const body = await htmlR.text();
  const q = `?k=${encodeURIComponent(key)}`;
  const images = meta.images.map((im, i) => `
    <figure><img src="/note/${id}/${im.file}${q}" alt="${esc(im.label)}">
    <figcaption>画像${i + 1}：${esc(im.label)}（長押しまたは右クリックで保存）</figcaption></figure>`).join("");
  const tags = meta.hashtags.map((t) => "#" + t).join(" ");
  const html = `<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>${esc(meta.title)}</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--text:#e6edf3;--muted:#8b949e;--accent:#3fb950;--line:#30363d}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;line-height:1.8}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
h1{font-size:1.25rem;margin:0 0 8px}
button{background:var(--accent);color:#0d1117;border:0;border-radius:8px;padding:12px 16px;font-size:1rem;font-weight:700;width:100%;margin-top:8px}
.muted{color:var(--muted);font-size:.9rem}
#body{background:#fff;color:#111;border-radius:8px;padding:16px;max-height:60vh;overflow:auto}
img{max-width:100%;border-radius:8px}
ol{padding-left:1.2em}
</style></head><body><main>
<p class="muted">noteに貼り付けるための原稿です（${meta.chars}字・図${meta.images.length}枚）</p>
<div class="card"><h1 id="title">${esc(meta.title)}</h1><button onclick="copyText('title',this)">① タイトルをコピー</button></div>
<div class="card"><div id="body">${body}</div><button onclick="copyRich(this)">② 本文をコピー（見出し・太字つき）</button></div>
<div class="card"><p>③ 本文中の「▼ここに画像」の場所に、次の画像を入れてください。</p>${images}</div>
<div class="card"><p>④ 公開設定でハッシュタグを入れて公開</p><p id="tags">${esc(tags)}</p><button onclick="copyText('tags',this)">ハッシュタグをコピー</button></div>
<div class="card muted"><ol><li>noteで「投稿」→「テキスト」を開く</li><li>タイトル欄にタイトル、本文欄に本文を貼り付け</li>
<li>「▼ここに画像」の行を消して、そこに画像を挿入</li><li>内容をざっと確認して「公開に進む」→ハッシュタグ→「投稿する」</li></ol></div>
</main><script>
function done(b){const t=b.textContent;b.textContent="コピーしました ✓";setTimeout(()=>b.textContent=t,1500)}
async function copyText(id,b){await navigator.clipboard.writeText(document.getElementById(id).innerText);done(b)}
async function copyRich(b){const el=document.getElementById("body");
 try{await navigator.clipboard.write([new ClipboardItem({"text/html":new Blob([el.innerHTML],{type:"text/html"}),"text/plain":new Blob([el.innerText],{type:"text/plain"})})])}
 catch(e){const r=document.createRange();r.selectNodeContents(el);const s=getSelection();s.removeAllRanges();s.addRange(r);document.execCommand("copy");s.removeAllRanges()}
 done(b)}
</script></body></html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "private, no-store" } });
}
