"""Use the signed-in, legacy Linux desktop viewer via ordinary GUI actions."""
from collections import defaultdict
from datetime import UTC
from difflib import SequenceMatcher
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image
from collector import DesktopBusy, Refused, StrictRedirect, article_url, parse_article, tidy

# Read the container X server's input-idle counter. Never reset it or send
# input on a scheduled run if the counter cannot be read.
READ_X11_IDLE = """
from ctypes import CDLL, POINTER, Structure, byref, c_char_p, c_int, c_ulong, c_void_p

class Info(Structure):
    _fields_ = [("window", c_ulong), ("state", c_int), ("kind", c_int),
                ("til_or_since", c_ulong), ("idle", c_ulong), ("event_mask", c_ulong)]

x11 = CDLL("libX11.so.6")
xss = CDLL("libXss.so.1")
x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [c_char_p], c_void_p
x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [c_void_p], c_ulong
x11.XCloseDisplay.argtypes = [c_void_p]
xss.XScreenSaverQueryInfo.argtypes = [c_void_p, c_ulong, POINTER(Info)]
display = x11.XOpenDisplay(None)
if not display:
    raise SystemExit(1)
try:
    info = Info()
    if not xss.XScreenSaverQueryInfo(display, x11.XDefaultRootWindow(display), byref(info)):
        raise SystemExit(1)
    print(info.idle)
finally:
    x11.XCloseDisplay(display)
"""


def checked(args, *, data=None, timeout=12, allowed=(0,)):
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout,
                                env=os.environ | {"OMP_THREAD_LIMIT": "1"})
    except (subprocess.TimeoutExpired, OSError) as err:
        raise Refused("A desktop or OCR command was unavailable or timed out.") from err
    if result.returncode not in allowed:
        raise Refused("The desktop changed focus or a GUI/OCR command failed.")
    return result.stdout


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def publisher_match(label, names):
    key = normalize(label)
    if not key:
        return None
    candidates = []
    for name in names:
        wanted = normalize(name)
        if key == wanted:
            return name
        score = SequenceMatcher(None, key, wanted).ratio()
        if len(wanted) >= 6 and score >= .82:
            candidates.append((score, name))
    return candidates[0][1] if len(candidates) == 1 else None


def card_bands(image):
    image = image.convert("RGB")
    w, h = image.size
    px = image.load()
    start = None
    for y in range(45, h + 1):
        on = y < h and min(px[round(w * .092), y]) >= 248 and min(px[round(w * .902), y]) >= 248
        if on and start is None:
            start = y
        if not on and start is not None:
            if 79 <= y - start <= 400 and start >= 50:
                yield start, y
            start = None


def png_bytes(im):
    output = io.BytesIO()
    im.save(output, format="PNG")
    return output.getvalue()


def ocr(im, languages="chi_sim+eng", mode=6, tsv=False):
    data = png_bytes(im.resize((im.width * 2, im.height * 2)))
    command = ["tesseract", "stdin", "stdout", "-l", languages, "--psm", str(mode)]
    if tsv:
        command.append("tsv")
    return checked(command, data=data).decode("utf-8")


def menu_copy_point(im):
    groups = defaultdict(list)
    for line in ocr(im, "eng", 11, tsv=True).splitlines()[1:]:
        cols = line.split("\t", 11)
        if len(cols) != 12 or cols[0] != "5":
            continue
        groups[tuple(cols[1:5])].append((cols[11], *map(int, cols[6:10])))
    for group in groups.values():
        if " ".join(row[0] for row in group).strip().lower() == "copy link":
            lo = min(row[1] for row in group)
            hi = max(row[1] + row[3] for row in group)
            return (lo + hi) // 4, (group[0][2] * 2 + group[0][4]) // 4
    raise Refused("The article viewer's Copy Link menu action was not visible.")


class Desktop:
    def __init__(self, config):
        self.config = config
        d = config.get("desktop", {})
        self.container = d.get("container", "agent-wechat")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.container):
            raise Refused("Invalid desktop container name.")
        self.url = d.get("api_url", "http://127.0.0.1:6174")
        local = urlsplit(self.url)
        if local.scheme != "http" or local.hostname not in ("127.0.0.1", "::1") or local.path or local.query or local.username:
            raise Refused("The desktop compatibility API must be on loopback.")
        self.token_path = Path(d.get("token_file", "")).expanduser()
        self.opener = build_opener(ProxyHandler({}), StrictRedirect(lambda _: None))

    def docker(self, *args, data=None, timeout=12, allowed=(0,)):
        return checked(["docker", "exec", *(["-i"] if data is not None else []), self.container, *map(str, args)],
                       data=data, timeout=timeout, allowed=allowed)

    def get(self, path):
        token = self.token_path.read_text().strip()
        req = Request(self.url + path, headers={"Authorization": f"Bearer {token}"})
        with self.opener.open(req, timeout=10) as response:
            return json.loads(response.read(18_000_000))

    def active(self):
        win = self.docker("xdotool", "getactivewindow").decode().strip()
        if not re.fullmatch(r"[0-9]+", win):
            raise Refused("No verified active desktop window.")
        return win

    def require_idle(self, minimum_seconds):
        try:
            elapsed_ms = int(self.docker("python3", "-c", READ_X11_IDLE, timeout=4))
        except (Refused, ValueError) as error:
            raise DesktopBusy("Cannot verify desktop idle time; skipping all GUI actions.") from error
        if elapsed_ms < minimum_seconds * 1000:
            raise DesktopBusy("Recent input on the shared desktop; skipping all GUI actions.")

    def geometry(self, win):
        value = self.docker("xdotool", "getwindowgeometry", "--shell", win).decode()
        parsed = dict(line.split("=", 1) for line in value.splitlines() if "=" in line)
        return tuple(int(parsed[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))

    def title(self, win):
        return self.docker("xdotool", "getwindowname", win).decode().strip()

    def check_focus(self, win):
        if self.active() != win:
            raise DesktopBusy("Desktop focus changed; no further actions were taken.")

    def click(self, win, x, y):
        self.docker("sh", "-c", 'test "$(xdotool getactivewindow)" = "$1" && xdotool mousemove "$2" "$3" click 1',
                    "collector", win, x, y)

    def screenshot(self):
        result = self.get("/api/debug/screenshot")
        data = base64.b64decode(result["base64"], validate=True)
        return Image.open(io.BytesIO(data)).convert("RGB")

    def write_clip(self, data):
        self.docker("sh", "-c", "xclip -selection clipboard -in -t UTF8_STRING >/dev/null 2>&1",
                    data=data, timeout=3)

    def read_clip(self, target):
        return self.docker("timeout", "3", "xclip", "-selection", "clipboard", "-out", "-t", target,
                           timeout=4, allowed=(0, 1, 124))

    def main(self):
        result = self.docker("xdotool", "search", "--onlyvisible", "--class", "^wechat$", allowed=(0, 1)).decode()
        windows = [w for w in result.split() if self.title(w) == "WeChat"]
        if len(windows) != 1:
            raise Refused("Could not identify the single legacy WeChat main window.")
        return windows[0]

    @staticmethod
    def labels(tree):
        if isinstance(tree, dict):
            yield tree
            for child in tree.get("children") or []:
                yield from Desktop.labels(child)

    def pane(self, main, activate=False):
        self.check_focus(main)
        x, y, w, h = self.geometry(main)
        if (w, h) != (980, 710):
            raise Refused("Desktop collector currently requires the tested 980×710 WeChat layout.")
        all_nodes = list(self.labels(self.get("/api/debug/a11y").get("tree")))
        heading = [n for n in all_nodes if n.get("role") == "label" and n.get("name") == "Official Accounts"
                   and n.get("bounds") and abs(n["bounds"]["x"] - x - 289) <= 8]
        if not heading and activate:
            rows = [n for n in all_nodes if n.get("role") == "list-item"
                    and n.get("name", "").startswith("Official Accounts ") and n.get("bounds")]
            rows = [r for r in rows if abs(r["bounds"]["x"] - x - 62) <= 10
                    and y + 67 <= r["bounds"]["y"] <= y + h - 50]
            if len(rows) == 1:
                b = rows[0]["bounds"]
                self.click(main, round(b["x"] + 80), round(b["y"] + 28))
                time.sleep(1.4)
                return self.pane(main)
        if not heading:
            raise DesktopBusy("Official Accounts is not the active main pane.")
        return x + 273, y + 27, x + w - 3, y + h - 4

    def close(self, win):
        if self.active() == win:
            self.docker("sh", "-c", 'test "$(xdotool getactivewindow)" = "$1" && xdotool key --clearmodifiers alt+F4',
                        "collector", win)
            time.sleep(.55)

    def capture(self):
        main, win = self.main(), self.active()
        if win == main or self.title(win) != "WeChat":
            raise Refused("Open a WeChat article in its own desktop viewer before capturing.")
        x, y, w, h = self.geometry(win)
        if not 720 <= w <= 1200 or not 500 <= h <= 800:
            raise Refused("The foreground window does not match the article viewer layout.")
        previous = self.read_clip("UTF8_STRING")
        captured = b""
        try:
            captured = self.docker("sh", "-c", '''set -e
test "$(xdotool getactivewindow)" = "$1"
xdotool mousemove "$2" "$3" click 1 key --clearmodifiers ctrl+a ctrl+c
sleep .25
test "$(xdotool getactivewindow)" = "$1"
timeout 3 xclip -selection clipboard -out -t text/html
''', "collector", win, x + w - 25, y + 355)
            # Capture immediately in the same desktop command; don't rely on
            # clipboard contents surviving another foreground operation.
            self.check_focus(win)
            source = captured.decode("utf-8")
            parse_article(source, UTC)
            self.click(win, x + w - 117, y + 20)
            time.sleep(.25)
            left, top = max(0, x + w - 385), max(0, y + 29)
            snippet = self.screenshot().crop((left, top, x + w - 25, y + 440))
            px, py = menu_copy_point(snippet)
            self.click(win, left + px, top + py)
            time.sleep(.20)
            self.check_focus(win)
            url = article_url(self.read_clip("UTF8_STRING").decode().strip())
            return url, source
        finally:
            try:
                if self.active() == win:
                    self.docker("sh", "-c", 'test "$(xdotool getactivewindow)" = "$1" && xdotool key --clearmodifiers Escape',
                                "collector", win)
                    if previous and captured:
                        self.write_clip(previous)
            except Refused:
                pass

    def check(self, store, zone, activate, minimum_idle=0, metrics=None):
        metrics = {} if metrics is None else metrics
        metrics.update(pages_scanned=0, matched_visible_cards=0, article_views_opened=0, new_articles=0)
        if minimum_idle:
            self.require_idle(minimum_idle)
        if self.get("/api/status/auth").get("status") != "logged_in":
            raise DesktopBusy("Legacy WeChat is not logged in; no scraping was attempted.")
        names = tuple(self.config.get("publishers", {}).values())
        if not names:
            raise Refused("Configure at least one publisher before discovery.")
        main = self.main()
        pages = max(1, min(int(self.config.get("visible_pages", 1)), 4))
        limit = max(1, min(int(self.config.get("articles_per_check", 2)), 5))
        visited = set()
        for page in range(pages):
            rect = self.pane(main, activate=activate and page == 0)
            feed = self.screenshot().crop(rect)
            metrics["pages_scanned"] += 1
            for y, end in card_bands(feed):
                publisher = publisher_match(ocr(feed.crop((96, y + 15, 369, y + 42))).strip(), names)
                if not publisher:
                    continue
                metrics["matched_visible_cards"] += 1
                max_y = min(y + 88, end - 5)
                title = normalize(ocr(feed.crop((73, y + 44, 424, max_y))))
                if len(title) < 4:
                    continue
                key = hashlib.sha256(f"{publisher}\0{title}".encode()).hexdigest()
                if key in visited or store.has_card(key):
                    continue
                visited.add(key)
                # The foreground and feed heading must still match, and the
                # candidate crop must not have moved while OCR was running.
                fresh = self.screenshot().crop(rect)
                region = (70, y + 15, 430, max_y)
                if fresh.crop(region).tobytes() != feed.crop(region).tobytes() or self.pane(main) != rect:
                    raise DesktopBusy("The official-account inbox changed during discovery; will retry later.")
                if minimum_idle and metrics["article_views_opened"] == 0 and page == 0:
                    self.require_idle(minimum_idle)
                self.click(main, rect[0] + 145, rect[1] + y + 54)
                time.sleep(2.2)
                win = self.active()
                if win == main or self.title(win) != "WeChat":
                    raise Refused("The selected story did not open an article viewer.")
                metrics["article_views_opened"] += 1
                try:
                    url, markup = self.capture()
                finally:
                    self.close(win)
                _, observed, _, _, _ = parse_article(markup, zone)
                if tidy(observed) != tidy(publisher):
                    raise Refused("The story did not belong to the selected official account.")
                from collector import show
                article, added = store.ingest(url, markup, publisher, zone)
                show((article, added))
                store.mark_card(key)  # Image repair has its own durable work queue.
                metrics["new_articles"] += int(added)
                if metrics["article_views_opened"] >= limit:
                    break
                time.sleep(1.0)
            if metrics["article_views_opened"] >= limit or page + 1 >= pages:
                break
            self.pane(main)
            if minimum_idle and metrics["article_views_opened"] == 0 and page == 0:
                self.require_idle(minimum_idle)
            self.docker("sh", "-c", 'test "$(xdotool getactivewindow)" = "$1" && xdotool mousemove "$2" "$3" click --repeat 4 5',
                        "collector", main, rect[0] + 320, rect[1] + 470)
            time.sleep(.9)
        print(json.dumps(metrics))
