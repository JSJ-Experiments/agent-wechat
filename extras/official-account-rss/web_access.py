"""Optional browser login and reader-only RSS access, behind private HTTPS."""
from collections import deque
from html import escape
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from collector import Refused, SLUG, feed_route
from dashboard import page

PREFIX = "/wx/oa/"
COOKIE = "wxoa_session"
SESSION_AGE = 12 * 3600
HTML_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Robots-Tag": "noindex, nofollow",
}


def password_key(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)


class WebAccess:
    def __init__(self, config, token):
        self.origin = config["origin"].rstrip("/")
        uri = urlsplit(self.origin)
        if uri.scheme != "https" or not uri.hostname or uri.username is not None or uri.path or uri.query or uri.fragment:
            raise Refused("Browser access requires a fixed private HTTPS origin.")
        verifier = Path(config["password_hash_file"]).expanduser().read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{128}", verifier):
            raise Refused("Invalid dashboard password verifier.")
        self.salt, self.digest = map(bytes.fromhex, verifier.split(":"))
        key = bytes.fromhex(token)
        self.reader = hmac.digest(key, b"reader key for wxoa v1", "sha256").hex()
        self.signing_key = hmac.digest(key, b"cookie key for wxoa v1:" + self.digest, "sha256")
        self.failed = deque()

    def _signature(self, body):
        return hmac.digest(self.signing_key, body.encode(), "sha256").hex()

    def issue(self, now=None):
        body = f"{int(time.time() if now is None else now) + SESSION_AGE}.{secrets.token_hex(16)}"
        return body + "." + self._signature(body)

    def valid(self, cookies, now=None):
        jar = SimpleCookie()
        try:
            jar.load(cookies or "")
        except CookieError:
            return False
        cookie = jar.get(COOKIE)
        value = cookie.value if cookie else ""
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", value):
            return False
        body, mac = value.rsplit(".", 1)
        remaining = int(body.split(".", 1)[0]) - (time.time() if now is None else now)
        return 0 < remaining <= SESSION_AGE and hmac.compare_digest(mac, self._signature(body))

    def cookie(self, value, age):
        return f"{COOKIE}={value}; Path={PREFIX}; Max-Age={age}; Secure; HttpOnly; SameSite=Strict"

    def auth(self, password):
        cutoff = time.monotonic() - 15 * 60
        while self.failed and self.failed[0] < cutoff:
            self.failed.popleft()
        if len(self.failed) >= 8:
            return 429
        if len(password) <= 256 and hmac.compare_digest(password_key(password, self.salt), self.digest):
            self.failed.clear()
            return 303
        self.failed.append(time.monotonic())
        return 401

    @staticmethod
    def respond(handler, status, body=b"", **headers):
        handler.send_response(status)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if body:
            handler.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in (HTML_HEADERS | headers).items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD" and body:
            handler.wfile.write(body)

    def login(self, handler, status=200, notice=""):
        message = f'<p role="alert">{escape(notice)}</p>' if notice else ""
        body = page("WeChat archive sign in", f"""<main><h1>WeChat article archive</h1>
        <p>Sign in to browse the private archive.</p>{message}
        <form method="post" action="{PREFIX}login">
        <p><label>Username <input name="username" autocomplete="username" value="owner" required></label></p>
        <p><label>Password <input type="password" name="password" autocomplete="current-password" required autofocus></label></p>
        <button type="submit">Sign in</button></form></main>""")
        self.respond(handler, status, body, **({"Retry-After": "900"} if status == 429 else {}))

    def reader_site(self):
        return f"{self.origin}{PREFIX}reader/{self.reader}"

    def route(self, handler, path, store, publishers):
        """Return an authorized content route, or handle the request here."""
        route = path[len(PREFIX):]
        method = handler.command
        authenticated = self.valid(handler.headers.get("Cookie"))
        if method == "POST":
            if handler.headers.get("Origin") != self.origin:
                self.respond(handler, 403)
                return None
            length = handler.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,5}", length) or not 0 <= int(length) <= 12000 \
                    or handler.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.respond(handler, 400)
                return None
            try:
                fields = parse_qs(handler.rfile.read(int(length)).decode(), max_num_fields=6)
            except (UnicodeError, ValueError):
                self.respond(handler, 400)
                return None
            if route == "login":
                result = self.auth(fields.get("password", [""])[0] if fields.get("username") == ["owner"] else "")
                if result == 303:
                    self.respond(handler, 303, **{"Location": PREFIX, "Set-Cookie": self.cookie(self.issue(), SESSION_AGE)})
                else:
                    self.login(handler, result, "Too many attempts; try again later." if result == 429 else "Invalid username or password.")
            elif not authenticated:
                self.respond(handler, 403)
            elif route == "logout":
                self.respond(handler, 303, **{"Location": PREFIX, "Set-Cookie": self.cookie("", 0)})
            elif route == "submit":
                try:
                    state = store.submit_url(fields.get("url", [""])[0])
                except (ValueError, Refused):
                    state = "invalid"
                destination = PREFIX + "?view=pending&submission=" + state + "#inventory"
                if state == "saved":
                    destination = PREFIX + "?view=saved&submission=saved#inventory"
                self.respond(handler, 303, **{"Location": destination})
            else:
                self.respond(handler, 404)
            return None
        if method not in ("GET", "HEAD"):
            self.respond(handler, 405)
            return None
        if route.startswith("reader/"):
            key, _, reader_route = route.removeprefix("reader/").partition("/")
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not hmac.compare_digest(key, self.reader):
                self.respond(handler, 404)
                return None
            if feed_route(reader_route, publishers) or reader_route.startswith("image/"):
                return reader_route
            self.respond(handler, 404)
            return None
        if not authenticated:
            if route in ("", "dashboard", "login"):
                self.login(handler)
            else:
                self.respond(handler, 404)
            return None
        if route == "feeds":
            links = "".join(f'<li>{escape(name)}: <a href="{self.reader_site()}/{slug}.xml">Full archive</a>'
                            f' · <a href="{self.reader_site()}/discovered/{slug}.xml">Discovered headlines</a></li>'
                            for slug, name in [("all", "Combined"), *publishers.items()] if SLUG.fullmatch(slug))
            body = page("Private RSS reader URLs", f'<p><a href="{PREFIX}">Back to archive</a></p><h1>RSS reader URLs</h1>'
                        f'<p>Copy these private bearer URLs into your RSS reader. The reader must be on the tailnet. Do not share them.</p><ul>{links}</ul>')
            self.respond(handler, 200, body)
            return None
        if route == "login":
            self.respond(handler, 303, **{"Location": PREFIX})
            return None
        return route or "dashboard"
