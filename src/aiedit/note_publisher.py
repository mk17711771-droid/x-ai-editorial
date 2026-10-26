"""noteへの記事公開（ブラウザ自動操作 / Playwright）。

noteには一般向けの投稿APIがないため、人と同じようにブラウザで操作する。
画面の変更で動かなくなることがあるので、各段階の画面写真とボタン一覧を data/note/debug/ に残す。
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

import markdown as md

from . import store
from .common import env, log, now_utc

LOGIN_URL = "https://note.com/login"
NEW_URL = "https://note.com/notes/new"
CHART_MARK = re.compile(r"!\[[^\]]*\]\((chart:[\w-]+|file:[^)]+)\)")


class NotePublishError(Exception):
    pass


class _Debug:
    def __init__(self, page):
        self.page = page
        self.steps: list[dict] = []

    def snap(self, name: str, shot: bool = True) -> None:
        info = {"step": name, "url": self.page.url, "at": now_utc().isoformat()}
        try:
            info["buttons"] = [b.strip() for b in self.page.locator("button").all_inner_texts() if b.strip()][:40]
        except Exception:  # noqa: BLE001
            pass
        if shot:
            path = f"note/debug/{len(self.steps):02d}_{name}.png"
            store.write_bytes(path, self.page.screenshot(full_page=False))
            info["screenshot"] = path
        self.steps.append(info)
        store.write_json("note/debug/steps.json", self.steps)


def _first(page, selectors: list[str], timeout: int = 15000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return loc.first
        page.wait_for_timeout(300)
    raise NotePublishError(f"画面の要素が見つかりません: {selectors}")


def _click_text(page, texts: list[str], timeout: int = 15000) -> None:
    sels = [f'button:has-text("{t}")' for t in texts] + [f'a:has-text("{t}")' for t in texts]
    _first(page, sels, timeout).click()


def _paste(page, target, html: str | None = None, png: bytes | None = None) -> None:
    """エディタに貼り付けイベントを送る（人がコピー＆ペーストするのと同じ）。"""
    target.click()
    page.keyboard.press("End")
    if html is not None:
        page.evaluate(
            """([sel, html]) => {
                const el = document.querySelector(sel);
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            }""",
            [".ProseMirror", html],
        )
    if png is not None:
        page.evaluate(
            """([sel, b64]) => {
                const el = document.querySelector(sel);
                const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                const file = new File([bin], 'chart.png', {type: 'image/png'});
                const dt = new DataTransfer();
                dt.items.add(file);
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            }""",
            [".ProseMirror", base64.b64encode(png).decode()],
        )
        page.wait_for_timeout(5000)  # 画像のアップロード待ち


def split_article(markdown_text: str) -> tuple[str, list[tuple[str, str]]]:
    """1行目の見出しをタイトルにし、本文を「文章」と「画像」の順番のリストにする。"""
    lines = markdown_text.strip().splitlines()
    title = lines[0].lstrip("# ").strip()
    body = "\n".join(lines[1:]).strip()
    parts: list[tuple[str, str]] = []
    pos = 0
    for m in CHART_MARK.finditer(body):
        if body[pos:m.start()].strip():
            parts.append(("html", md.markdown(body[pos:m.start()])))
        parts.append(("image", m.group(1)))
        pos = m.end()
    if body[pos:].strip():
        parts.append(("html", md.markdown(body[pos:])))
    return title, parts


def publish(markdown_text: str, images: dict[str, bytes], hashtags: list[str], draft_only: bool = False) -> str:
    """記事を作成して公開する（draft_only=Trueなら下書き保存まで）。記事のURLを返す。"""
    from playwright.sync_api import sync_playwright

    title, parts = split_article(markdown_text)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="ja-JP")
        dbg = _Debug(page)
        try:
            session = env("NOTE_SESSION", required=False)
            if session:
                # サーバーからのログインはnoteのボット対策（reCAPTCHA）で拒否されるため、
                # 普段のブラウザでログインしたときのセッションを使う
                page.context.add_cookies([{"name": "_note_session_v5", "value": session.strip(),
                                           "domain": ".note.com", "path": "/", "secure": True, "httpOnly": True}])
            else:
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                _first(page, ["#email", 'input[name="login"]', 'input[type="email"]']).fill(env("NOTE_EMAIL"))
                _first(page, ["#password", 'input[type="password"]']).fill(env("NOTE_PASSWORD"))
                _click_text(page, ["ログイン"])
                page.wait_for_timeout(5000)
                # ログイン画面は入力内容が写るので画面写真は残さない
                dbg.snap("after_login", shot=False)

            page.goto(NEW_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            if "login" in page.url:
                raise NotePublishError("noteにログインできていません（セッションの期限切れの可能性）。"
                                       "ブラウザでnoteにログインし直し、NOTE_SESSION を更新してください")
            dbg.snap("editor_opened")
            _first(page, ['textarea[placeholder*="タイトル"]', 'textarea']).fill(title)
            editor = _first(page, [".ProseMirror"])
            for kind, value in parts:
                if kind == "html":
                    _paste(page, editor, html=value)
                elif value in images:
                    _paste(page, editor, png=images[value])
                page.wait_for_timeout(800)
            dbg.snap("body_filled")

            if draft_only:
                _click_text(page, ["下書き保存"])
                page.wait_for_timeout(3000)
                dbg.snap("draft_saved")
                return page.url

            _click_text(page, ["公開に進む", "公開設定"])
            page.wait_for_timeout(3000)
            dbg.snap("publish_settings")
            for tag in hashtags[:5]:
                try:
                    box = _first(page, ['input[placeholder*="ハッシュタグ"]'], timeout=3000)
                    box.fill(tag)
                    page.keyboard.press("Enter")
                except NotePublishError:
                    break
            _click_text(page, ["投稿する", "公開する"])
            page.wait_for_timeout(6000)
            dbg.snap("published")
            return page.url
        except Exception as e:
            try:
                dbg.snap("error")
            except Exception:  # noqa: BLE001
                pass
            raise NotePublishError(f"{type(e).__name__}: {e}") from e
        finally:
            browser.close()


def publish_file(path: str, draft_only: bool = True) -> str:
    """content/note/*.md を公開する。chart:smart_money などの画像は最新データから作る。"""
    from . import charts

    text = Path(path).read_text(encoding="utf-8")
    images: dict[str, bytes] = {}
    if "chart:smart_money" in text:
        snap = store.read_json("traders/latest.json")
        if snap:
            images["chart:smart_money"] = charts.smart_money_chart(snap)
    url = publish(text, images, hashtags=["Hyperliquid", "HYPE", "暗号資産", "米国株", "投資"], draft_only=draft_only)
    store.append_jsonl("note/published.jsonl", {"at": now_utc().isoformat(), "file": path, "url": url, "draft_only": draft_only})
    log.info("note: %s", url)
    return url
