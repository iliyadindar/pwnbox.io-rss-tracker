I built a clean Telegram bot that polls the pwnbox.io RSS feed and notifies subscribers about new challenges. 

- [bot.py](bot.py) — single-file bot (~250 lines)
- [requirements.txt](requirements.txt) — `pyTelegramBotAPI` + `feedparser`
- [.gitignore](.gitignore) — ignores `state.json` and Python cache

**Run it:**
```bash
pip install -r requirements.txt
python bot.py
```

Then open Telegram, find your bot, and send `/start`.

**What you get:**
- `/start` — subscribe yourself (and anyone else who finds the bot) to alerts
- `/stop` — unsubscribe
- `/latest` — preview the most recent challenge on demand
- `/topics` — view available topic categories and your current filter
- `/filter ssrf xss` — subscribe only to selected challenge topics
- `/filter all` — clear topic filters and receive every challenge
- `/status` — feed URL, poll interval, subscriber count, tracked entries
- `/help` — command list

**Design notes worth knowing:**
- **First-run protection** — on the very first launch, the bot silently marks every existing feed entry as "seen" so you don't get blasted with the entire backlog. From then on, only genuinely new challenges trigger notifications.
- **OWASP-style topic tags** — each challenge is tagged from its RSS title and summary with likely topics like SSRF, XSS, CORS, SQLi, auth, RCE, or `other`.
- **Per-chat topic filters** — subscribers can keep all alerts or limit notifications to the categories they care about.
- **Persistent state** — subscribers and seen entry IDs are kept in `state.json`, so restarts don't lose anything or re-notify.
- **Self-healing subscribers** — if a user blocks the bot, Telegram returns 403 and that chat is auto-pruned.
- **Beautiful messages** — HTML-formatted with emojis, the description is HTML-stripped and truncated to 400 chars, and dates are normalized to UTC.
- **Threaded poller** — feed checks every 5 minutes in the background; the main thread keeps the bot responsive to commands.
