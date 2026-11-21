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
:root{color-scheme:light dark;font-family:system-ui,sans-serif;--bg:#f3f6f4;--panel:#fff;--text:#18281f;
--muted:#53655a;--line:#dce7df;--soft:#eaf3ed;--accent:#075f48;--ink:#fff;--warn:#765015;
--warn-bg:#fff3da;--shadow:0 10px 35px #19362a0b;--radius:18px}
@media(prefers-color-scheme:dark){:root{--bg:#121b16;--panel:#1b2820;--text:#e7f2ea;--muted:#abbfb1;
--line:#34473b;--soft:#243a2d;--accent:#83e2b9;--ink:#11271c;--warn:#f2d18b;--warn-bg:#3d321d;
--shadow:0 10px 35px #0003}}
*{box-sizing:border-box}body{max-width:1100px;margin:0 auto;padding:0 24px 40px;background:var(--bg);color:var(--text);
line-height:1.5;overflow-wrap:anywhere}a{color:var(--accent);text-underline-offset:.18em}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
h1,h2,h3,p{margin:0}h1,h2,h3{line-height:1.15}h1{font-size:clamp(2rem,5vw,3.25rem);letter-spacing:-.06em}
h2{font-size:1.3rem;letter-spacing:-.025em}.muted,small{color:var(--muted)}.eyebrow{color:var(--accent);
font-size:.71rem;letter-spacing:.14em;font-weight:750;margin-bottom:8px}
.topbar,.actions,nav,.filters,.section-heading{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
.topbar{justify-content:space-between;min-height:68px;margin-bottom:15px;border-bottom:1px solid var(--line)}
.brand{font-weight:780;color:var(--text);display:inline-flex;align-items:center;gap:9px;text-decoration:none}
.mark{display:inline-grid;place-items:center;width:32px;height:32px;background:var(--accent);color:var(--ink);
border-radius:10px;font-weight:850;font-size:1.1rem}.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,.8fr);
align-items:center;gap:20px;margin-bottom:16px}.hero p{margin-top:9px}.hero-text{padding:9px 0}
.panel,.quick-add,.tile,.login-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.panel,.quick-add{padding:clamp(15px,3vw,24px);margin-bottom:16px}.quick-add{margin:0}
.quick-add h2{font-size:1.07rem}.inline-form{display:flex;align-items:flex-end;gap:8px;margin-top:12px}
.inline-form label{flex:1;min-width:0}.inline-form input{width:100%}.quick-add small{display:block;margin-top:8px}
input,select,button,.button{font:inherit}input,select{background:var(--bg);color:var(--text);max-width:100%;
border:1px solid var(--line);border-radius:10px;padding:9px 11px;min-height:41px}
button,.button{display:inline-flex;gap:.4em;align-items:center;justify-content:center;padding:8px 12px;min-height:40px;
border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;
text-align:center;text-decoration:none;font-weight:650;white-space:nowrap}
button:hover,.button:hover{background:var(--soft)}.primary{background:var(--accent);color:var(--ink);border-color:var(--accent)}
.primary:hover{background:var(--accent);color:var(--ink);filter:brightness(1.08)}
.tiles,.publishers{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.tile{padding:12px 15px}.tile b{display:block;font-size:1.65rem;line-height:1.2;letter-spacing:-.05em}.tile span{font-size:.86rem;color:var(--muted)}
.notice{display:flex;flex-wrap:wrap;gap:6px 14px;background:var(--soft);padding:11px 14px;border-radius:12px;margin-bottom:16px}
.notice.warn{background:var(--warn-bg);color:var(--warn)}.section-heading{justify-content:space-between;margin-bottom:13px}
.tabs{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0 12px;border-bottom:1px solid var(--line)}
.tabs a{display:inline-block;padding:8px 11px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent}
.tabs a[aria-current=page]{color:var(--accent);font-weight:750;border-bottom-color:var(--accent)}
.entries{list-style:none;padding:0;margin:12px 0}.entries li{padding:12px 0;border-bottom:1px solid var(--line)}
.entries li:last-child{border-bottom:none}.entry-title{font-weight:720;color:var(--text);text-decoration:none}
.entry-title:hover{text-decoration:underline}.entry-top,.feed-row{display:flex;align-items:start;justify-content:space-between;gap:9px}
.badge{flex-shrink:0;display:inline-block;padding:2px 8px;border-radius:99px;background:var(--soft);color:var(--muted);font-size:.75rem;font-weight:690}
.badge.saved{color:var(--accent)}.badge.wait{background:var(--warn-bg);color:var(--warn)}
.pager{display:flex;flex-wrap:wrap;align-items:center;gap:10px;color:var(--muted);font-size:.9rem}
.publisher{padding:12px;border:1px solid var(--line);border-radius:12px}.publishers{margin:12px 0 6px}
progress{display:block;width:100%;height:10px;margin:6px 0 9px;accent-color:var(--accent)}
summary{cursor:pointer;font-weight:720}.details-list .entries{margin-bottom:0}
.reader-article{max-width:78ch;margin:0 auto}.reader-article h1{margin-bottom:12px}
.reader-content{margin-top:22px}.reader-content img{max-width:100%;height:auto;border-radius:9px}
.reader-content p{margin:.7em 0}.reader-content pre{white-space:pre-wrap}
.login{padding-bottom:0}.login-wrap{display:grid;min-height:90svh;place-items:center}
.login-card{width:min(100%,410px);padding:30px}.login-card h1{font-size:2.25rem}.login-card .mark{width:48px;height:48px;
margin-bottom:20px;font-size:1.55rem}.login-card .muted{margin-top:8px}.login-form{display:grid;gap:9px;margin-top:24px}
.login-form label{font-size:.9rem;font-weight:700}.login-form button{justify-content:space-between;margin-top:4px}
[role=alert]{margin-top:12px;color:var(--warn);background:var(--warn-bg);padding:9px;border-radius:8px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
@media(max-width:640px){body{padding:0 13px 25px}.topbar{min-height:59px}.hero{grid-template-columns:1fr;gap:10px}
.tiles,.publishers{grid-template-columns:repeat(2,minmax(0,1fr))}.panel,.quick-add{padding:14px}.feed-row{align-items:start;flex-direction:column}}
@media(max-width:360px){.inline-form{flex-wrap:wrap}.inline-form label{flex:1 0 100%}.entry-top{flex-direction:column}}
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


def page(title, body, *, body_class=""):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style><body class="{escape(body_class)}">{body}</body></html>""".encode()


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
    tiles = "".join(f'<div class="tile"><b>{value}</b><span>{label}</span></div>' for label, value in (
        ("Saved articles", t["saved"]), ("Discovered links", t["discovered"]),
        ("Tracked publishers", len(data["publishers"])), ("Cached images", t["cached_images"])))
    bars = "".join(f"""
        <div class="publisher"><strong>{escape(p["publisher"])}</strong>
        <progress value="{p["matched"]}" max="{max(1,p["discovered"])}" aria-label="Discovered links matched to saved articles"></progress>
        <small>{p["matched"]} / {p["discovered"]} discovered links matched · {p["saved"]} saved bodies</small><br>
        <small>{timestamp(p["oldest_discovered"], date_only=True) + " to " + timestamp(p["latest_discovered"], date_only=True)
               if p["oldest_discovered"] else "No dated links yet"}</small><br>
        <a href="{escape(p["id"])}.xml">Archive RSS</a>
        · <a href="discovered/{escape(p["id"])}.xml">Headline RSS</a></div>""" for p in data["publishers"])
    cooldown = t["public_fetch_paused_until"]
    public_status = ("Web capture paused until " + timestamp(cooldown) + ".") if cooldown else "Web capture is available."
    records = []
    for row in entries:
        title, publisher, url = escape(row["title"]), escape(row["publisher"] or "Not identified"), escape(row["url"])
        date = timestamp(row["published"], date_only=True)
        badge = STATUSES.get(row["status"], "Not saved")
        if row["id"] is not None:
            title = f'<a class="entry-title" href="article/{row["id"]}">{title}</a>'
            detail = f' · {row["image_count"]} cached images'
            if row["missing_images"]:
                detail += f' · {row["missing_images"]} missing'
        else:
            title = f'<a class="entry-title" href="{url}" rel="noreferrer">{title}</a>'
            detail = ""
        badge_class = {"saved": "saved", "verification-or-layout": "wait", "queued": "wait"}.get(row["status"], "")
        date_label = f" · {date}" if row["published"] else ""
        records.append(f'<li><div class="entry-top">{title}<span class="badge {badge_class}">{badge}</span></div>'
                       f'<small>{publisher}{date_label}{detail}</small></li>')
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
    tabs = []
    for key, name in (("saved", "Saved articles"), ("pending", "Needs capture"), ("all", "All entries")):
        href = escape(urlencode({"view": key, "publisher": slug}))
        current = ' aria-current="page"' if key == view else ""
        tabs.append(f'<a href="?{href}#inventory"{current}>{name}</a>')

    def link(number, label):
        query = escape(urlencode({"view": view, "publisher": slug, "page": number}))
        return f'<a href="?{query}#inventory">{label}</a>'

    nav = " ".join(part for part in (link(curr-1, "← Previous") if curr > 1 else "", f"<span>Page {curr} of {pages}</span>",
                                      link(curr+1, "Next →") if curr < pages else "") if part)
    alerts = "Configured" if data["alerts_configured"] else "Not configured"
    submission = {"saved": "That URL is already archived.", "queued": "URL added to the capture queue.",
                  "full": "The capture queue is full.", "invalid": "Enter an official WeChat article URL."}.get(selected("submission", ""))
    banner = f'<div class="notice" role="status">{escape(submission)}</div>' if web and submission else ""
    controls = """<aside class="quick-add" aria-labelledby="add-heading"><h2 id="add-heading">Add an article</h2>
    <form class="inline-form" method="post" action="submit">
    <label><span class="sr">WeChat article URL</span>
    <input type="url" name="url" placeholder="https://mp.weixin.qq.com/s/…" maxlength="8192" required></label>
    <button class="primary" type="submit">Queue article</button></form>""" + (
        f'<small role="status">{escape(submission)}</small>' if submission else
        '<small>Paste an official-account article link. Protected posts may need the signed-in viewer.</small>'
    ) + "</aside>" if web else ""
    header_actions = ('<a class="button" href="feeds">RSS feeds</a><form method="post" action="logout">'
                      '<button type="submit">Sign out</button></form>') if web else (
        '<a class="button" href="all.xml">Archive RSS</a><a class="button" href="discovered/all.xml">Headline RSS</a>')
    tracking = (f'{t["active_profile_publishers"]} active official accounts in the synced profile; '
                'following changes are picked up on the next legacy discovery run.') if config.get("track_follows") else "Using the configured publisher list."
    refresh = next((r for r in data["recent_runs"] if r["task"].startswith("discover") and r["outcome"] == "ok"), None)
    last_synced = f'Last link scan: {timestamp(refresh["finished"])}' if refresh else "No completed link scan recorded"
    contents = f"""<header class="topbar"><a href="dashboard" class="brand"><span class="mark" aria-hidden="true">W</span>WeChat Archive</a>
    <nav aria-label="Archive links">{header_actions}</nav></header>
    <main><section class="hero"><div class="hero-text"><div class="eyebrow">OFFICIAL ACCOUNT READER</div>
    <h1>Article library</h1><p class="muted">{last_synced}</p></div>{controls}</section>
    <div class="tiles">{tiles}</div>
    <div class="notice{' warn' if cooldown else ''}"><strong>{escape(public_status)}</strong>
    <span>{t["queued"]} queued · {t["needs_viewer"]} need the viewer · {t["missing_images"]} missing image references</span></div>
    <section id="inventory" class="panel"><div class="section-heading"><div><div class="eyebrow">BROWSE</div><h2>Inventory</h2></div>
    <form class="filters" method="get" action="dashboard#inventory">
    <input type="hidden" name="view" value="{view}"><label><span class="sr">Publisher</span>
    <select name="publisher">{options}</select></label><button>Filter</button></form></div>
    <nav class="tabs" aria-label="Filter by article status">{"".join(tabs)}</nav>{banner}
    <div class="pager"><strong>{count} {"entry" if count == 1 else "entries"}</strong>{nav}</div>
    {"<ul class='entries'>"+"".join(records)+"</ul>" if records else "<p>No entries match these filters.</p>"}
    {f'<div class="pager">{nav}</div>' if pages > 1 else ''}</section>
    <details class="panel details-list"><summary>Coverage by publisher ({len(data["publishers"])})</summary>
    <p class="muted">{tracking} Counts cover delivered links, not full publisher history.</p>
    <div class="publishers">{bars}</div></details>
    <details id="runs" class="panel details-list"><summary>Collector runs</summary>
    <p class="muted">Most recent 20 runs · {data["desktop_pages_per_check"]} visible desktop pages per check · Alerts: {alerts.lower()}</p>
    {"<ul class='entries'>"+"".join(runs)+"</ul>" if runs else "<p>No runs recorded yet.</p>"}</details></main>"""
    return page("WeChat article archive", contents)


def render_article(store, article_id):
    with closing(store.open()) as db:
        article = db.execute("SELECT * FROM articles WHERE rowid=?", (article_id,)).fetchone()
    if article is None:
        return None
    title = escape(article["title"])
    return page(article["title"], f"""<header class="topbar"><a class="brand" href="../dashboard">← WeChat Archive</a>
        <a class="button" href="{escape(article['url'])}" rel="noreferrer">Original in WeChat</a></header>
        <article class="panel reader-article"><div class="eyebrow">{escape(article["publisher"])}</div>
        <h1>{title}</h1><p class="muted">Published {timestamp(article["published"])}
        · {article["image_count"]} cached images · {article["missing_images"]} missing</p>
        <div class="reader-content">{archived_markup(article, "../image")}</div></article>""")
