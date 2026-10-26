// LINE Webhook 受信用 Cloudflare Worker（無料プラン）
//
// - 承認/却下ボタン（postback） → GitHub repository_dispatch で decide-draft ワークフローを起動
// - 「停止」 → GitHubリポジトリ変数 BOT_ENABLED=false（自動投稿・承認投稿をすべて止める）
// - 「再開」 → BOT_ENABLED=true ＋ 異常停止状態を解除
// - 「状態」 → 現在の稼働状態を返信
// - 「ID」   → 送信者のLINEユーザーIDを返信（初期設定用）
//
// 返信（Reply API）はLINEの無料通数にカウントされない。

const GH_API = "https://api.github.com";

export default {
  async fetch(request, env) {
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
    if (!["approve", "reject_quality", "reject_inaccurate"].includes(action) || !/^[\w-]+$/.test(id || "")) {
      return reply(env, ev.replyToken, "不正な操作です");
    }
    await dispatch(env, "draft_decision", { id, action });
    const msg = action === "approve"
      ? "✅ 承認しました。投稿直前の最終確認をしてから投稿します（1〜2分）。"
      : "却下しました。理由は今後の採点基準の改善に使います。";
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
  if (text === "状態") {
    const v = await getVariable(env, "BOT_ENABLED");
    const mode = await getVariable(env, "APPROVAL_MODE");
    return reply(env, ev.replyToken,
      `稼働: ${v === "false" ? "停止中" : "稼働中"}\nモード: ${mode === "false" ? "全自動" : "承認制"}`);
  }
  if (text) {
    return reply(env, ev.replyToken, "使えるコマンド: 停止 / 再開 / 状態 / ID");
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
  return fetch(`${GH_API}/repos/${env.GITHUB_REPO}${path}`, {
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
