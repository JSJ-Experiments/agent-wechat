# Official-account RSS collector

An independent archive, not an adapter to another RSS service. The collector discovers received official-account article cards from an allowlist of publishers, extracts opened articles as rich HTML, downloads their public image objects, and publishes the full sanitized body with locally served images. No publisher credentials, third-party RSS service, or n8n are used.

## Set up on a host with the existing legacy desktop

The inbox source currently supports the legacy Agent WeChat container with its English 980×710 desktop layout, a signed-in profile, Docker, Tesseract (`chi_sim` and `eng`), and Python 3.12. The separate native daemon is **not** a source for the Official Accounts inbox. Never run two clients against the same official profile.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
# Set token_file, container, and the exact names of the publishers you want.
.venv/bin/python collector.py status
.venv/bin/python collector.py discover          # Read-only, no desktop or network actions
.venv/bin/python collector.py check --activate  # deliberate manual use of the shared desktop
.venv/bin/python collector.py serve
```

Run `.venv/bin/python collector.py url my-publisher` or `... url all` to get a feed URL. It includes a private key for the RSS and its images, so do not share it in chat, commit it, or put it in a shared log. The listener binds only to loopback. To subscribe from a different computer, tunnel the port with SSH and point a local reader at it. The server never fetches an upstream image in response to an RSS reader request.

### Dashboard

Run `.venv/bin/python collector.py dashboard-url` and open the result privately. The local dashboard uses the loopback listener, token, and optional SSH tunnel. There is no public index page. The dashboard can open saved articles, filter and page through the inventory, and show which URLs still need capture. To get the overview as JSON without opening a browser, run `.venv/bin/python collector.py stats`.

- **Discovered links** are unique URLs seen in delivered cards, not the contents of an account's entire archive. The coverage bars count how many of those URLs match a saved body. Matching uses the exact URL or the same publisher and title within two days, the same as the discovery queue.
- **Saved bodies** can include articles imported directly that were never found in delivered cards. Image counts describe local image files and missing references separately.
- **Pages scanned** counts actual inbox views read by the desktop collector, not publisher history pages or dashboard pagination. The run log retains the most recent 100 recorded steps and shows the latest 20. It starts when this update is installed; old console logs are not imported. Dashboard requests never trigger collection.

### Private HTTPS and RSS readers

To add an HTML password form at `/wx/oa/`, import [`caddy-private.caddy`](caddy-private.caddy) at the top level of your Caddy configuration and put the loopback-only Caddy listener on a private HTTPS ingress, such as Tailscale Serve. Configure `web.origin` to the exact HTTPS origin (including the port, no path) and `web.password_hash_file` to a private verifier file. Do not publish the dashboard on public password-only ingress. Set the verifier interactively in the collector directory:

```sh
umask 077
.venv/bin/python -c 'from getpass import getpass; from pathlib import Path; from secrets import token_bytes; from web_access import password_key; s=token_bytes(16); Path("state/dashboard-password").write_text(s.hex()+":"+password_key(getpass(), s).hex()+"\n")'
```

Restart the RSS service after changing the verifier or origin. Sign in as `owner`. The **RSS reader URLs** view includes separate bearer URLs for **full archive** and **discovered headlines**. Feed keys grant access to feeds and cached images, not the dashboard. The headline feed updates even while the article body is unavailable, and does not claim to contain full text. Do not publish or log reader URLs.

The authenticated dashboard can submit official WeChat article URLs. Submissions are deduplicated, given priority over discovered links, and processed by `collector.py process-submissions` (at most one public request per run). You can schedule it separately on a 15-minute timer. Neither submitting a URL nor using the RSS reader contacts WeChat. Public requests still honor the shared cooldown, and a held URL may need to be opened manually in the authorized viewer.

The article cache is a separate SQLite database plus digest-named images in `state/`. Back up that directory privately. Multinode access, public ingress, and long-term image pruning are not configured here. Sanitization retains semantic formatting and local images, not original fonts, scripts, adverts, video embeds, or custom styling.

Article text is saved before image requests start. Allowlisted CDN images that couldn't be downloaded can be restored with `collector.py repair-images` without another article request or desktop interaction. This is bounded and delays repeat image failures for 12 hours. Until a valid image is cached, the feed shows a placeholder and never sends the reader to the CDN. Articles captured before the repair queue was introduced need to be recaptured if they have missing images.

### Supplied article URLs

`.venv/bin/python collector.py add URL` makes one normal request to a public WeChat article. If WeChat serves verification instead, the collector holds that URL; it does not attempt to work around the prompt. Open an authorized article in the existing desktop viewer and run `.venv/bin/python collector.py capture-open` to collect rich HTML and its canonical link from the desktop. Files with previously copied rich HTML can also be imported with `collector.py ingest URL FILE`.

From the latest native CLI, a supplied link card can be passed to the same HTTP path:

```sh
wx --json --account ACCOUNT messages list filehelper --limit 1 | .venv/bin/python collector.py from-native
```

This reads only explicit article link cards in the supplied result. It does not use native credentials for web articles or automatically import a chat's URLs. Verify the official client can open a protected article instead of repeatedly retrying its public URL.

### Scheduling and alerts

Use a systemd user timer scheduled every six hours to run `collector.py discover --fetch 1`, `collector.py check --skip-busy`, `collector.py repair-images`, and `collector.py notify --if-configured` as separate steps. Discovery reads only selected publishers' delivered cards, including linked stories in an observed multi-article card. It does not query an arbitrary publisher's archive. Legacy discovery reads the existing encrypted profile read-only inside the legacy container; it neither extracts keys from process memory nor copies keys or the profile out of the container. If a public fetch is refused, further automatic article requests pause for 48 hours.

Set `"track_follows": true` to refresh the list of active official accounts from the synced legacy profile on each discovery run, in addition to any explicitly configured publishers. This reads only verified official-account contacts marked active in the supported desktop store. It does not initiate a sync, change your follows, enumerate your private contacts, or promise the phone's list before it syncs. The result includes accounts with no delivered posts in the local lookback. Publisher display-name collisions are reported and skipped during card discovery rather than conflated. Unfollowing stops new automatic capture from that publisher; previously archived records remain intact.

For the native daemon, `discover --source native` can consume persisted link cards in explicitly configured `gh_` publisher threads using its read-only messages API. Set `native.server`, `native.token_file`, `native.account`, and `native.publisher_threads` (publisher ID to thread ID). It cannot enable delivery of posts that the native daemon does not receive, list the phone's follows, or open authenticated article pages. This source needs live migration validation before replacing the tested legacy source.

The desktop HTML fallback scans a verified, foreground Official Accounts pane and skips if the shared desktop is elsewhere. For unattended checks it also needs the X11 idle counter to report at least two minutes without input (configurable through `minimum_desktop_idle_seconds`). It will not force the pane open in the background. Only selected publishers and a bounded number of visible cards are scanned; getting the full body of an article outside those pages or inside a multi-article card may require manually opening it. A discovered card is not reported as a full RSS item until its body is archived.

For optional direct Discord alerts, add `"discord_webhook_file": "/private/path/to/webhook"` to private config and run `collector.py notify` once to baseline the archive. The separate notification step submits new entries in batches without mass mentions, even if desktop collection was skipped. A failure keeps entries pending; a lost acknowledgment can cause a duplicate. No destination is configured by default.

Test locally with `.venv/bin/python -m unittest -v`. No test posts to Discord or reads account credentials.
