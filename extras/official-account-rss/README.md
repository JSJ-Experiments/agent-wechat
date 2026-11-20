# Official-account RSS collector

An independent archive, not an adapter to another RSS service. The collector reads the signed-in WeChat Linux desktop client's Official Accounts view, extracts each opened article as rich HTML, downloads the referenced public image objects, and publishes the full sanitized body with locally served images. No publisher credentials, third-party RSS service, or n8n are used.

## Set up on a host with the existing legacy desktop

The inbox source currently supports the legacy Agent WeChat container with its English 980×710 desktop layout, a signed-in profile, Docker, Tesseract (`chi_sim` and `eng`), and Python 3.12. The separate native daemon is **not** a source for the Official Accounts inbox. Never run two clients against the same official profile.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
# Set token_file, container, and the exact names of the publishers you want.
.venv/bin/python collector.py status
.venv/bin/python collector.py check --activate  # deliberate manual use of the shared desktop
.venv/bin/python collector.py serve
```

Run `.venv/bin/python collector.py url my-publisher` or `... url all` to get a feed URL. It includes a private key for the RSS and its images, so do not share it in chat, commit it, or put it in a shared log. The listener binds only to loopback. To subscribe from a different computer, tunnel the port with SSH and point a local reader at it. The server never fetches an upstream image in response to an RSS reader request.

The article cache is a separate SQLite database plus digest-named images in `state/`. Back up that directory privately. Multinode access, public ingress, and long-term image pruning are not configured here. Sanitization retains semantic formatting and local images, not original fonts, scripts, adverts, video embeds, or custom styling.

### Supplied article URLs

`.venv/bin/python collector.py add URL` makes one normal request to a public WeChat article. If WeChat serves verification instead, the collector holds that URL; it does not attempt to work around the prompt. Open an authorized article in the existing desktop viewer and run `.venv/bin/python collector.py capture-open` to collect rich HTML and its canonical link from the desktop. Files with previously copied rich HTML can also be imported with `collector.py ingest URL FILE`.

From the latest native CLI, a supplied link card can be passed to the same HTTP path:

```sh
wx --json --account ACCOUNT messages list filehelper --limit 1 | .venv/bin/python collector.py from-native
```

This reads only explicit article link cards in the supplied result. It does not use native credentials for web articles or automatically import a chat's URLs. Verify the official client can open a protected article instead of repeatedly retrying its public URL.

### Scheduling and alerts

Use `collector.py check --skip-busy` in a systemd user timer scheduled every six hours. It only scans a verified, foreground Official Accounts pane, and skips if the shared desktop is elsewhere. It will not force the pane open in the background. Only selected publishers and a bounded number of visible cards are scanned; newly published articles outside the loaded pages, alternate inbox layouts, multi-article cards, and discovery from an arbitrary publisher's historical archive are not guaranteed. If the shared desktop is frequently busy, use manual runs rather than automating focus takeover.

For optional direct Discord alerts, add `"discord_webhook_file": "/private/path/to/webhook"` to private config and run `collector.py notify` once to baseline the archive. Future successful checks submit new entries in batches without mass mentions. A failure keeps entries pending; a lost acknowledgment can cause a duplicate. No destination is configured by default.

Test locally with `.venv/bin/python -m unittest -v`. No test posts to Discord or reads account credentials.
