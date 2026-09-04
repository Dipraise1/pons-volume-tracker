"""Telegram Bot API transport."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class Telegram:
    def __init__(self, token: str, timeout: int = 40):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def api(self, method: str, params: dict | None = None,
            timeout: int | None = None):
        data = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}
        ).encode()
        req = urllib.request.Request(
            f"{self.base}/{method}", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req,
                                        timeout=timeout or self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())
            except Exception:
                return {"ok": False, "description": f"HTTP {exc.code}"}
        except Exception as exc:
            return {"ok": False, "description": str(exc)}

    IMG_MARK = "\x01IMG\x01"

    def send_photo(self, chat_id, photo_url: str, caption: str):
        return self.api("sendPhoto", {
            "chat_id": chat_id, "photo": photo_url,
            "caption": caption[:1024], "parse_mode": "HTML"})

    def send(self, chat_id, text: str, preview: bool = False):
        if not chat_id:
            return {"ok": False, "description": "no chat_id"}
        # image-tagged message: "\x01IMG\x01<url>\x01<text>".
        # The image is shown as a pinned large link-preview (keeps the full card
        # intact, unlike a photo caption which is capped at 1024 chars).
        image_url = None
        if text.startswith(self.IMG_MARK):
            image_url, _, text = text[len(self.IMG_MARK):].partition("\x01")
        # With an image: if the whole card fits a caption and has no block-level
        # HTML (which can't be safely split), send it as one photo. Otherwise
        # send the image with the headline as caption, then the FULL card as one
        # clean text message — never split HTML tags across messages.
        if image_url:
            if len(text) <= 1024 and "<blockquote" not in text and "<pre" not in text:
                res = self.send_photo(chat_id, image_url, text)
                if res.get("ok"):
                    return res
            else:
                headline = text.split("\n", 1)[0][:1024]
                res = self.send_photo(chat_id, image_url, headline)
                if res.get("ok"):
                    return self._send_text(chat_id, text, preview)
            # photo failed (bad gateway / unfetchable) -> plain text fallback
        return self._send_text(chat_id, text, preview)

    def _send_text(self, chat_id, text: str, preview: bool = False):
        for part in _split(text, 3900):
            res = self.api("sendMessage", {
                "chat_id": chat_id, "text": part, "parse_mode": "HTML",
                "disable_web_page_preview": "false" if preview else "true"})
            if not res.get("ok"):
                return res
            time.sleep(0.05)
        return {"ok": True}

    def get_updates(self, offset: int | None, timeout: int = 30):
        res = self.api("getUpdates",
                       {"offset": offset, "timeout": timeout,
                        "allowed_updates": json.dumps(["message"])},
                       timeout=timeout + 15)
        return res.get("result", []) if res.get("ok") else []

    def me(self):
        return self.api("getMe").get("result") or {}


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
