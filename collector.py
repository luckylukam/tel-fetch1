import re
import base64
import httpx
import asyncio
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
CHANNELS = [
    "kurdconfig",
    "Configir98",
    "YamYamProxy",
    "FreeConfigForYou",
    "Zed_NetMeli",
    "on_proxy1",
    "Spotify_Porteghali",
    "oxnet_ir"
    # Add more channel usernames here (without @)
]

PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")
OUTPUT_FILE = Path("output/configs.txt")
MAX_MESSAGES = 50   # how many recent messages to scan per channel
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATTERN = re.compile(
    r'(?:vmess|vless|trojan|ss|ssr|tuic|hysteria2|hy2)://[^\s<>\"\'\]\[)(\u0000-\u001F]+'
)


async def fetch_channel(client: httpx.AsyncClient, channel: str) -> list[str]:
    """Scrape a public Telegram channel's web preview for configs."""
    configs: list[str] = []
    url = f"https://t.me/s/{channel}"
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        found = CONFIG_PATTERN.findall(r.text)
        for c in found:
            c = c.strip().rstrip(".,;)")
            if any(c.startswith(p) for p in PROTOCOLS):
                configs.append(c)
        print(f"  ✔ {channel}: {len(configs)} configs found")
    except Exception as e:
        print(f"  ✘ {channel}: {e}")
    return configs


async def collect_all() -> list[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [fetch_channel(client, ch) for ch in CHANNELS]
        results = await asyncio.gather(*tasks)

    all_configs: list[str] = []
    seen: set[str] = set()
    for batch in results:
        for cfg in batch:
            if cfg not in seen:
                seen.add(cfg)
                all_configs.append(cfg)
    return all_configs


def save(configs: list[str]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = "\n".join(configs)
    encoded = base64.b64encode(raw.encode()).decode()
    OUTPUT_FILE.write_text(encoded)
    # Also save a plain (unencoded) version for debugging
    Path("output/configs_plain.txt").write_text(raw)
    print(f"\n✅ Saved {len(configs)} unique configs → {OUTPUT_FILE}")
    print(f"   Base64 length: {len(encoded)} chars")


def main() -> None:
    print(f"🔍 Collecting V2Ray configs  [{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]")
    print(f"   Channels  : {len(CHANNELS)}")
    print(f"   Protocols : {', '.join(p.rstrip('://') for p in PROTOCOLS)}\n")

    configs = asyncio.run(collect_all())

    if not configs:
        print("⚠️  No configs found. Channels may have changed or be empty.")
        # Write empty base64 so the file still exists
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text("")
        return

    save(configs)


if __name__ == "__main__":
    main()
