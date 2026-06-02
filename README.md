# V2Ray Config Collector

Automatically fetches V2Ray / Xray configs from public Telegram channels every 6 hours and commits a ready-to-use **base64-encoded subscription file** to this repo.

## Subscription URL

After you push this repo to GitHub and the first Action runs, your sub URL will be:

```
https://raw.githubusercontent.com/<YOUR_USERNAME>/<YOUR_REPO>/main/output/configs.txt
```

Paste that URL into **v2rayNG**, **NekoBox**, **Hiddify**, **Clash Meta**, or any client that accepts base64 subscriptions.

---

## Setup (< 2 minutes)

1. **Fork / push** this repo to your GitHub account.
2. Go to **Actions → Enable workflows** if prompted.
3. Click **Run workflow** to trigger the first collection immediately.
4. Copy your raw `output/configs.txt` URL and add it to your client.

The workflow runs automatically every 6 hours after that.

---

## Customise

Edit the `CHANNELS` list at the top of `collector.py`:

```python
CHANNELS = [
    "v2rayng_config",
    "v2ray_configs_pool",
    # add or remove channel usernames (no @ needed)
]
```

Change the schedule in `.github/workflows/collect.yml`:

```yaml
- cron: "0 */6 * * *"   # every 6 hours — adjust as needed
```

---

## Output files

| File | Description |
|------|-------------|
| `output/configs.txt` | Base64-encoded subscription (import this URL into your client) |
| `output/configs_plain.txt` | One config per line, plain text (for debugging) |

---

## How it works

`collector.py` scrapes the public web preview (`t.me/s/<channel>`) of each Telegram channel with `httpx`, extracts all strings matching V2Ray protocol prefixes (`vmess://`, `vless://`, `trojan://`, `ss://`, `tuic://`, `hysteria2://`, …), deduplicates them, base64-encodes the result, and writes it to `output/configs.txt`.

No Telegram API key or login required — only public channels are supported.
