"""Read-only views for the private official-account archive."""
from contextlib import closing
from datetime import UTC, datetime
from html import escape
import json
from math import ceil
import time
from urllib.parse import urlencode

from collector import CARD_MATCH, archived_markup

PAGE_SIZE = 20
INVENTORY = f"""
WITH cards AS (
    SELECT d.*, EXISTS(SELECT 1 FROM articles a WHERE {CARD_MATCH}) AS saved,
           t.state AS attempt
    FROM discovered_cards d LEFT JOIN attempts t ON t.url=d.url
), inventory AS (
    SELECT a.rowid AS id, a.url, a.publisher, a.title, a.published, a.found, a.image_count,
           a.missing_images, 'saved' AS status FROM articles a
    UNION ALL
    SELECT NULL, url, publisher, title, published, found, 0, 0, coalesce(attempt, 'link-only')
    FROM cards WHERE NOT saved
    UNION ALL
    SELECT NULL, s.url, NULL, 'Submitted article URL', NULL, s.submitted, 0, 0, coalesce(t.state,'queued')
    FROM submissions s LEFT JOIN attempts t ON t.url=s.url
    WHERE NOT EXISTS(SELECT 1 FROM discovered_cards d WHERE d.url=s.url)
    AND NOT EXISTS(SELECT 1 FROM articles a WHERE a.url=s.url)
    UNION ALL
    SELECT NULL, t.url, NULL, 'Supplied article URL', NULL, t.updated, 0, 0, t.state
    FROM attempts t WHERE NOT EXISTS(SELECT 1 FROM discovered_cards d WHERE d.url=t.url)
    AND NOT EXISTS(SELECT 1 FROM articles a WHERE a.url=t.url)
    AND NOT EXISTS(SELECT 1 FROM submissions s WHERE s.url=t.url)
)
"""
CSS = """
:root{color-scheme:light dark;font-family:system-ui,sans-serif;--line:#8885;--subtle:#8881}
body{max-width:1050px;margin:28px auto;padding:0 16px;line-height:1.55;overflow-wrap:anywhere}
a{color:LinkText}h1,h2,h3{line-height:1.2}h1{margin:8px 0}h2{margin:0 0 12px}
nav{display:flex;gap:12px;align-items:center;flex-wrap:wrap}header,section{margin-bottom:24px}
.tiles,.publishers{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile,.publisher,section,.entries li{border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:1.6rem}.muted,small{color:inherit;opacity:.73}
progress{display:block;width:100%;height:12px;margin:6px 0 10px;accent-color:#4484ed}
.entries{list-style:none;padding:0;margin:16px 0}.entries li{margin-bottom:8px}
.badge{display:inline-block;padding:2px 8px;border-radius:100px;background:var(--subtle);font-size:.86em}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}select,button{font:inherit;max-width:100%}
article{max-width:72ch;margin:0 auto}article img{max-width:100%;height:auto}
article pre{white-space:pre-wrap}summary{cursor:pointer}dt{font-weight:bold}dd{margin:0 0 10px}
"""
STATUSES = {
    "saved": "Saved body",
    "link-only": "Link only",
    "queued": "Queued",
    "verification-or-layout": "Needs viewer",
    "temporarily-unavailable": "Temporarily unavailable",
}
METRICS = {
    "links_read": "link occurrences read", "discovered": "new unique links", "web_requests": "web attempts",
    "held_this_run": "unsuccessful web attempts", "new_articles": "new bodies",
    "pages_scanned": "desktop pages scanned", "matched_visible_cards": "matching visible cards",
    "article_views_opened": "viewers opened", "resolved_image_references": "image references repaired",
    "delivered": "alerts sent",
    "followed_publishers": "active official accounts",
    "ambiguous_names": "ambiguous publisher names",
}


def read_overview(db, config):
    subscribed = config.get("publishers", {})
    rows = {name: {"id": key, "publisher": name, "discovered": 0, "matched": 0, "saved": 0,
                   "missing_images": 0, "oldest_discovered": None, "latest_discovered": None}
            for key, name in subscribed.items()}
    for row in db.execute(INVENTORY + """SELECT publisher, count(*) AS discovered, sum(saved) AS matched,
                          min(published) AS oldest_discovered, max(published) AS latest_discovered
                          FROM cards GROUP BY publisher"""):
        if row["publisher"] in rows:
            rows[row["publisher"]].update({k: row[k] for k in row.keys() if k != "publisher"})
    for row in db.execute("""SELECT publisher,count(*) AS saved,sum(missing_images) AS missing_images
                           FROM articles GROUP BY publisher"""):
        if row["publisher"] in rows:
            rows[row["publisher"]].update(saved=row["saved"], missing_images=row["missing_images"])
    counts = dict(db.execute(INVENTORY + "SELECT status,count(*) FROM inventory GROUP BY status"))
    totals = dict(db.execute(INVENTORY + """SELECT count(*) AS discovered,coalesce(sum(saved),0) AS matched,
                        min(published) AS oldest_discovered,max(published) AS latest_discovered FROM cards""").fetchone())
    totals.update(saved=counts.get("saved", 0), link_only=counts.get("link-only", 0),
                  needs_viewer=counts.get("verification-or-layout", 0),
                  queued=counts.get("queued", 0),
                  temporarily_unavailable=counts.get("temporarily-unavailable", 0))
    totals.update(db.execute("""SELECT
        (SELECT count(DISTINCT name) FROM assets) AS cached_images,
        (SELECT coalesce(sum(missing_images),0) FROM articles) AS missing_images,
        (SELECT count(*) FROM image_repair) AS queued_image_articles,
        (SELECT max(updated)+172800 FROM attempts WHERE state='verification-or-layout') AS cooldown_until,
        (SELECT count(*) FROM followed_publishers) AS active_profile_publishers
    """).fetchone())
    totals["public_fetch_paused_until"] = totals.pop("cooldown_until")
    if (totals["public_fetch_paused_until"] or 0) <= time.time():
        totals["public_fetch_paused_until"] = None
    recent = [dict(row) | {"metrics": json.loads(row["metrics"])} for row in db.execute(
        "SELECT task,finished,duration_ms,outcome,metrics,note FROM collection_runs ORDER BY id DESC LIMIT 20")]
    pages = 0
    try:
        pages = max(1, min(4, int(config.get("visible_pages", 1))))
    except (ValueError, TypeError):
        pass
    return {"totals": totals, "publishers": list(rows.values()), "recent_runs": recent,
            "desktop_pages_per_check": pages, "alerts_configured": bool(config.get("discord_webhook_file"))}


def overview(store, config):
    with closing(store.open()) as db, db:
        db.execute("BEGIN")
        return read_overview(db, config)


def timestamp(value, *, date_only=False):
    if value is None:
        return "Unknown"
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M UTC")


def page(title, body):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style><body>{body}</body></html>""".encode()


def select_option(value, label, selected):
    return f'<option value="{escape(value)}"{" selected" if value == selected else ""}>{escape(label)}</option>'


def render_dashboard(store, config, params, *, web=False):
    def selected(key, default):
        return params.get(key, [default])[0]

    view, slug, page_number = selected("view", "saved"), selected("publisher", ""), selected("page", "1")
    view = view if view in ("all", "saved", "pending") else "saved"
    filters = config.get("publishers", {})
    slug = slug if slug in filters else ""
    page_number = int(page_number) if page_number.isascii() and page_number.isdigit() and len(page_number) <= 8 else 1
    where, args = [], []
    if view != "all":
        where.append("status " + ("=" if view == "saved" else "!=") + " 'saved'")
    if slug:
        where.append("publisher = ?")
        args.append(filters[slug])
    condition = " WHERE " + " AND ".join(where) if where else ""
    with closing(store.open()) as db, db:
        db.execute("BEGIN")
        data = read_overview(db, config)
        count = db.execute(INVENTORY + "SELECT count(*) FROM inventory" + condition, args).fetchone()[0]
        pages = max(1, ceil(count / PAGE_SIZE))
        curr = max(1, min(page_number, pages))
        entries = db.execute(INVENTORY + """SELECT * FROM inventory""" + condition + """
            ORDER BY coalesce(published,found) DESC, url LIMIT ? OFFSET ?""", (*args, PAGE_SIZE, (curr-1)*PAGE_SIZE)).fetchall()

    t = data["totals"]
    tiles = "".join(f'<div class="tile"><b>{value}</b>{label}</div>' for label, value in (
        ("Saved article bodies", t["saved"]), ("Discovered links", t["discovered"]),
        ("Discovered, not saved", t["discovered"]-t["matched"]), ("Unique cached images", t["cached_images"]),
        ("Missing image references", t["missing_images"]), ("Needs viewer", t["needs_viewer"])))
    bars = "".join(f"""
        <div class="publisher"><strong>{escape(p["publisher"])}</strong>
        <progress value="{p["matched"]}" max="{max(1,p["discovered"])}" aria-label="Discovered links matched to saved articles"></progress>
        <small>{p["matched"]} / {p["discovered"]} discovered links matched · {p["saved"]} saved bodies</small><br>
        <small>{timestamp(p["oldest_discovered"], date_only=True) + " to " + timestamp(p["latest_discovered"], date_only=True)
               if p["oldest_discovered"] else "No dated links yet"}</small><br>
        <a href="{escape(p["id"])}.xml">Archive RSS</a>
        · <a href="discovered/{escape(p["id"])}.xml">Headline RSS</a></div>""" for p in data["publishers"])
    cooldown = t["public_fetch_paused_until"]
    public_status = ("Public requests paused until " + timestamp(cooldown) + ".") if cooldown else "No active pause on public requests."
    records = []
    for row in entries:
        title, publisher, url = escape(row["title"]), escape(row["publisher"] or "Not identified"), escape(row["url"])
        date = timestamp(row["published"], date_only=True)
        badge = STATUSES.get(row["status"], "Not saved")
        if row["id"] is not None:
            title = f'<a href="article/{row["id"]}">{title}</a>'
            detail = f' · {row["image_count"]} cached image files · {row["missing_images"]} missing'
        else:
            title = f'<a href="{url}" rel="noreferrer">{title}</a>'
            detail = ""
        records.append(f'<li><span class="badge">{badge}</span> {title}<br>'
                       f'<small>{publisher} · Published: {date}{detail}</small></li>')
    runs = []
    for run in data["recent_runs"]:
        metrics = [f"{run['metrics'][key]} {label}" for key, label in METRICS.items() if key in run["metrics"]]
        if run["task"] == "alerts" and run["metrics"].get("configured") is False:
            metrics = ["No destination configured"]
        details = escape(" · ".join(metrics))
        note = escape(run["note"])
        runs.append(f'<li><strong>{escape(run["task"])}</strong> · {escape(run["outcome"])} '
                    f'· <small>{timestamp(run["finished"])} · {run["duration_ms"]/1000:.1f}s</small>'
                    f'<br>{details} {note}</li>')
    options = "".join(select_option(key, name, slug) for key, name in [("", "All publishers"), *filters.items()])
    views = "".join(select_option(key, name, view) for key, name in
                    (("saved", "Saved articles"), ("pending", "Needs capture"), ("all", "All entries")))

    def link(number, label):
        query = escape(urlencode({"view": view, "publisher": slug, "page": number}))
        return f'<a href="?{query}#inventory">{label}</a>'

    nav = " · ".join(part for part in (link(curr-1, "Previous") if curr > 1 else "", f"Page {curr} of {pages}",
                                       link(curr+1, "Next") if curr < pages else "") if part)
    alerts = "Configured" if data["alerts_configured"] else "Not configured"
    submission = {"saved": "That URL is already archived.", "queued": "URL added to the capture queue.",
                  "full": "The capture queue is full.", "invalid": "Enter an official WeChat article URL."}.get(selected("submission", ""))
    controls = f"""<p><a href="feeds">RSS reader URLs</a></p>
    <form class="filters" method="post" action="submit">
    <label>Article URL <input type="url" name="url" placeholder="https://mp.weixin.qq.com/s/…" maxlength="8192" required></label>
    <button type="submit">Queue article</button></form>
    <p class="muted">Only WeChat official-account article URLs. If WeChat requires verification, open the link in the signed-in viewer.</p>
    <form method="post" action="logout"><button type="submit">Sign out</button></form>""" if web else ""
    if web and submission:
        controls = f'<p role="status"><b>{escape(submission)}</b></p>' + controls
    tracking = (f'{t["active_profile_publishers"]} active official accounts in the synced profile; '
                'following changes are picked up on the next legacy discovery run.') if config.get("track_follows") else "Using the configured publisher list."
    publisher_block = f'<div class="publishers">{bars}</div>'
    if len(data["publishers"]) > 8:
        publisher_block = f'<details><summary>Coverage by publisher ({len(data["publishers"])})</summary>{publisher_block}</details>'
    contents = f"""<header><nav><a href="all.xml">Archive RSS</a> <a href="discovered/all.xml">Headline RSS</a> <a href="#inventory">Inventory</a>
    <a href="#runs">Run history</a></nav><h1>WeChat article archive</h1>
    <p class="muted">Private archive. Link discovery is not a publisher history crawl.</p>{controls}</header>
    <main><section><h2>Archive status</h2><div class="tiles">{tiles}</div>
    <p>{escape(public_status)} {t["queued"]} submitted URLs queued; {t["temporarily_unavailable"]} temporarily unavailable;
    {t["queued_image_articles"]} saved articles have queued image repairs.</p><p>{tracking}</p>
    {publisher_block}
    <p class="muted">Bars compare saved matches with links found in delivered messages only.
    Full articles captured separately are counted under saved bodies, even when no discovered link matches.
    Dates cover discovered links with a known date, not the publisher’s entire history.</p></section>
    <section id="inventory"><h2>Inventory</h2><form class="filters" method="get" action="dashboard">
    <label>Show <select name="view">{views}</select></label>
    <label>Publisher <select name="publisher">{options}</select></label><button>Apply</button></form>
    <p>{count} {'entry' if count == 1 else 'entries'} · {nav}</p>
    {"<ul class='entries'>"+"".join(records)+"</ul>" if records else "<p>No entries match these filters.</p>"}
    <p>{nav}</p></section>
    <section id="runs"><h2>Collector runs</h2>
    <p>Desktop limit: {data["desktop_pages_per_check"]} visible inbox pages per check. Alerts: {alerts}.</p>
    <p class="muted">Most recent 20 recorded runs. Page counts are actual inbox views, not article history pages.
    Runs before tracking was installed are not reconstructed.</p>
    {"<ul class='entries'>"+"".join(runs)+"</ul>" if runs else "<p>No runs recorded yet.</p>"}</section></main>"""
    return page("WeChat article archive", contents)


def render_article(store, article_id):
    with closing(store.open()) as db:
        article = db.execute("SELECT * FROM articles WHERE rowid=?", (article_id,)).fetchone()
    if article is None:
        return None
    title = escape(article["title"])
    return page(article["title"], f"""<nav><a href="../dashboard">Back to archive</a>
        <a href="{escape(article['url'])}" rel="noreferrer">Original in WeChat</a></nav>
        <article><h1>{title}</h1><p>{escape(article["publisher"])} · Published {timestamp(article["published"])}
        · {article["image_count"]} cached image files · {article["missing_images"]} missing</p>
        {archived_markup(article, "../image")}</article>""")
