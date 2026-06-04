import re
import base64
import json
import httpx
import asyncio
import yaml
from html import unescape
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

CHANNELS = [
    "kurdconfig", "Configir98", "YamYamProxy", "FreeConfigForYou",
    "Zed_NetMeli", "on_proxy1", "Spotify_Porteghali", "oxnet_ir"
]

# ── External subscription URLs ────────────────────────────────────────────────
# Add any v2ray (base64) or Clash (YAML) subscription URLs here.
# The script auto-detects the format and merges configs into both outputs.
EXTERNAL_SUB_URLS: list[str] = [
   "https://raw.githubusercontent.com/mahdibland/ShadowsocksAggregator/master/Eternity.yml"# "https://example.com/v2ray-sub",        # v2ray base64 subscription
    # "https://example.com/clash-sub.yaml",   # Clash YAML subscription
]

PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")

OUTPUT_FILE       = Path("output/configs.txt")
CLASH_OUTPUT_FILE = Path("output/clash.yaml")

# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATTERN = re.compile(
    r'(?:vmess|vless|trojan|ss|ssr|tuic|hysteria2|hy2)://[^\s<>"\'`]+'
)

# ── Telegram fetch ────────────────────────────────────────────────────────────

async def fetch_channel(client: httpx.AsyncClient, channel: str) -> list[str]:
    configs: list[str] = []
    url = f"https://t.me/s/{channel}"
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        text = unescape(r.text)
        for m in CONFIG_PATTERN.findall(text):
            c = m.strip().rstrip(".,;)")
            if any(c.startswith(p) for p in PROTOCOLS):
                configs.append(c)
        print(f"  ✔ {channel}: {len(configs)} configs found")
    except Exception as e:
        print(f"  ✘ {channel}: {e}")
    return configs

# ── External subscription fetch ───────────────────────────────────────────────

def _is_clash_yaml(text: str) -> bool:
    """Heuristic: a Clash sub contains a 'proxies:' key near the top."""
    return bool(re.search(r'^\s*proxies\s*:', text, re.MULTILINE))


def _extract_configs_from_v2ray_sub(text: str) -> list[str]:
    """
    Decode a v2ray/xray base64 subscription.
    The payload is a base64-encoded block of newline-separated proxy URIs.
    """
    text = text.strip()
    # Try base64 decode
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        decoded = text  # maybe it's already plain text

    configs: list[str] = []
    for line in decoded.splitlines():
        line = line.strip()
        if any(line.startswith(p) for p in PROTOCOLS):
            configs.append(line)
    return configs


def _clash_proxy_to_uri(proxy: dict) -> str | None:
    """
    Convert a single Clash proxy dict back to a proxy URI string.
    Supports vmess, vless, trojan, ss, hysteria2.
    Returns None for unsupported/unparseable entries.
    """
    ptype = proxy.get("type", "").lower()
    name  = proxy.get("name", "proxy")
    host  = proxy.get("server", "")
    port  = proxy.get("port", 443)

    try:
        # ── VMess ──────────────────────────────────────────────────────
        if ptype == "vmess":
            raw = {
                "v":    "2",
                "ps":   name,
                "add":  host,
                "port": str(port),
                "id":   proxy.get("uuid", ""),
                "aid":  str(proxy.get("alterId", 0)),
                "scy":  proxy.get("cipher", "auto"),
                "net":  "tcp",
                "type": "none",
                "tls":  "tls" if proxy.get("tls") else "",
                "sni":  proxy.get("servername", ""),
            }
            ws_opts = proxy.get("ws-opts", {})
            if proxy.get("network") == "ws":
                raw["net"]  = "ws"
                raw["path"] = ws_opts.get("path", "/")
                raw["host"] = ws_opts.get("headers", {}).get("Host", host)
            elif proxy.get("network") == "grpc":
                raw["net"]  = "grpc"
                raw["path"] = proxy.get("grpc-opts", {}).get("grpc-service-name", "")
            b64 = base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()
            return f"vmess://{b64}"

        # ── VLESS ──────────────────────────────────────────────────────
        if ptype == "vless":
            uuid   = proxy.get("uuid", "")
            params: list[str] = []
            if proxy.get("tls"):
                reality = proxy.get("reality-opts", {})
                if reality:
                    params.append("security=reality")
                    params.append(f"pbk={reality.get('public-key', '')}")
                    params.append(f"sid={reality.get('short-id', '')}")
                else:
                    params.append("security=tls")
            if proxy.get("servername"):
                params.append(f"sni={proxy['servername']}")
            if proxy.get("network") == "ws":
                params.append("type=ws")
                ws = proxy.get("ws-opts", {})
                params.append(f"path={ws.get('path', '/')}")
                params.append(f"host={ws.get('headers', {}).get('Host', host)}")
            elif proxy.get("network") == "grpc":
                params.append("type=grpc")
                params.append(f"serviceName={proxy.get('grpc-opts', {}).get('grpc-service-name', '')}")
            qs = "?" + "&".join(params) if params else ""
            return f"vless://{uuid}@{host}:{port}{qs}#{name}"

        # ── Trojan ─────────────────────────────────────────────────────
        if ptype == "trojan":
            password = proxy.get("password", "")
            params: list[str] = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            if proxy.get("network") == "ws":
                params.append("type=ws")
                ws = proxy.get("ws-opts", {})
                params.append(f"path={ws.get('path', '/')}")
            qs = "?" + "&".join(params) if params else ""
            return f"trojan://{password}@{host}:{port}{qs}#{name}"

        # ── Shadowsocks ────────────────────────────────────────────────
        if ptype == "ss":
            method   = proxy.get("cipher", "aes-256-gcm")
            password = proxy.get("password", "")
            userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
            return f"ss://{userinfo}@{host}:{port}#{name}"

        # ── Hysteria2 ──────────────────────────────────────────────────
        if ptype in ("hysteria2", "hy2"):
            password = proxy.get("password", "")
            params: list[str] = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            if proxy.get("skip-cert-verify"):
                params.append("insecure=1")
            qs = "?" + "&".join(params) if params else ""
            return f"hysteria2://{password}@{host}:{port}{qs}#{name}"

    except Exception:
        pass

    return None


def _extract_configs_from_clash_sub(text: str) -> list[str]:
    """Parse a Clash YAML subscription and convert each proxy back to a URI."""
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        print(f"    ⚠ Failed to parse Clash YAML: {e}")
        return []

    proxies = data.get("proxies", []) or []
    configs: list[str] = []
    for proxy in proxies:
        uri = _clash_proxy_to_uri(proxy)
        if uri:
            configs.append(uri)
    return configs


async def fetch_external_sub(client: httpx.AsyncClient, url: str) -> list[str]:
    """Fetch one external subscription URL (v2ray base64 or Clash YAML)."""
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        text = r.text.strip()

        if _is_clash_yaml(text):
            configs = _extract_configs_from_clash_sub(text)
            print(f"  ✔ [Clash sub] {url}: {len(configs)} proxies converted")
        else:
            configs = _extract_configs_from_v2ray_sub(text)
            print(f"  ✔ [V2Ray sub] {url}: {len(configs)} configs found")

        return configs

    except Exception as e:
        print(f"  ✘ [External sub] {url}: {e}")
        return []


async def collect_all() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # Telegram channels
        tg_tasks  = [fetch_channel(client, ch) for ch in CHANNELS]
        # External subscriptions
        sub_tasks = [fetch_external_sub(client, url) for url in EXTERNAL_SUB_URLS]

        tg_results  = await asyncio.gather(*tg_tasks)
        sub_results = await asyncio.gather(*sub_tasks)

    seen: set[str] = set()
    all_configs: list[str] = []

    for batch in (*tg_results, *sub_results):
        for cfg in batch:
            if cfg not in seen:
                seen.add(cfg)
                all_configs.append(cfg)

    return all_configs

# ── Rename remarks ────────────────────────────────────────────────────────────

def rename_remarks(configs: list[str]) -> list[str]:
    """Replace whatever remark is after # with mn_conf<N>."""
    renamed = []
    for i, cfg in enumerate(configs, start=1):
        base = cfg.split("#")[0] if "#" in cfg else cfg
        renamed.append(f"{base}#mn_conf{i}")
    return renamed

# ── Clash conversion ──────────────────────────────────────────────────────────

def _decode_vmess(uri: str) -> dict | None:
    """Decode a vmess:// URI and return a raw config dict, or None on failure."""
    try:
        b64  = uri[len("vmess://"):]
        b64 += "=" * (-len(b64) % 4)
        data = json.loads(base64.b64decode(b64).decode())
        return data
    except Exception:
        return None


def _parse_userinfo_host(uri: str, scheme: str) -> tuple[str, str, int, str] | None:
    """
    Parse scheme://user@host:port#remark (used by vless, trojan, ss, hy2…).
    Returns (user, host, port, remark) or None.
    """
    try:
        body   = uri[len(scheme):]
        remark = ""
        if "#" in body:
            body, remark = body.split("#", 1)
        if "?" in body:
            body, _ = body.split("?", 1)
        if "@" in body:
            userinfo, hostport = body.rsplit("@", 1)
        else:
            userinfo, hostport = "", body
        if ":" in hostport:
            host, port_str = hostport.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = hostport, 443
        return userinfo, host, port, remark
    except Exception:
        return None


def config_to_clash_proxy(cfg: str, name: str) -> dict | None:
    """
    Convert a single proxy URI to a Clash proxy dict.
    Supported: vmess, vless, trojan, ss, hy2/hysteria2.
    Returns None for unsupported / unparseable configs.
    """

    # ── VMess ──────────────────────────────────────────────────────────────
    if cfg.startswith("vmess://"):
        raw = _decode_vmess(cfg.split("#")[0])
        if not raw:
            return None
        proxy: dict = {
            "name":    name,
            "type":    "vmess",
            "server":  str(raw.get("add", "")),
            "port":    int(raw.get("port", 443)),
            "uuid":    str(raw.get("id", "")),
            "alterId": int(raw.get("aid", 0)),
            "cipher":  str(raw.get("scy", raw.get("security", "auto"))),
            "udp":     True,
        }
        net = str(raw.get("net", "tcp"))
        if net == "ws":
            proxy["network"]  = "ws"
            proxy["ws-opts"]  = {
                "path":    str(raw.get("path", "/")),
                "headers": {"Host": str(raw.get("host", proxy["server"]))},
            }
        elif net == "grpc":
            proxy["network"]    = "grpc"
            proxy["grpc-opts"]  = {"grpc-service-name": str(raw.get("path", ""))}
        tls = str(raw.get("tls", ""))
        if tls == "tls":
            proxy["tls"] = True
            sni = str(raw.get("sni", raw.get("host", "")))
            if sni:
                proxy["servername"] = sni
        return proxy

    # ── VLESS ──────────────────────────────────────────────────────────────
    if cfg.startswith("vless://"):
        try:
            body       = cfg[len("vless://"):]
            remark     = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            uuid, hostport = body.split("@", 1)
            host, port_str = hostport.rsplit(":", 1)
            port   = int(port_str)
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
        except Exception:
            return None

        proxy = {
            "name":   name,
            "type":   "vless",
            "server": host,
            "port":   port,
            "uuid":   uuid,
            "udp":    True,
        }
        if params.get("security") == "tls":
            proxy["tls"] = True
            sni = params.get("sni", "")
            if sni:
                proxy["servername"] = sni
        if params.get("security") == "reality":
            proxy["tls"] = True
            proxy["reality-opts"] = {
                "public-key": params.get("pbk", ""),
                "short-id":   params.get("sid", ""),
            }
            sni = params.get("sni", "")
            if sni:
                proxy["servername"] = sni
        net = params.get("type", "tcp")
        if net == "ws":
            proxy["network"]  = "ws"
            proxy["ws-opts"]  = {
                "path":    params.get("path", "/"),
                "headers": {"Host": params.get("host", host)},
            }
        elif net == "grpc":
            proxy["network"]   = "grpc"
            proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
        return proxy

    # ── Trojan ─────────────────────────────────────────────────────────────
    if cfg.startswith("trojan://"):
        parsed = _parse_userinfo_host(cfg, "trojan://")
        if not parsed:
            return None
        password, host, port, _ = parsed
        proxy = {
            "name":     name,
            "type":     "trojan",
            "server":   host,
            "port":     port,
            "password": password,
            "udp":      True,
        }
        try:
            params_str = cfg.split("?", 1)[1].split("#")[0] if "?" in cfg else ""
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
            sni = params.get("sni", "")
            if sni:
                proxy["sni"] = sni
            if params.get("type") == "ws":
                proxy["network"]  = "ws"
                proxy["ws-opts"]  = {"path": params.get("path", "/")}
        except Exception:
            pass
        return proxy

    # ── Shadowsocks ────────────────────────────────────────────────────────
    if cfg.startswith("ss://"):
        try:
            body   = cfg[len("ss://"):]
            remark = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            if "@" in body:
                userinfo, hostport = body.rsplit("@", 1)
                host, port_str     = hostport.rsplit(":", 1)
                port               = int(port_str)
                if ":" in userinfo:
                    method, password = userinfo.split(":", 1)
                else:
                    decoded  = base64.b64decode(userinfo + "==").decode()
                    method, password = decoded.split(":", 1)
            else:
                decoded            = base64.b64decode(body + "==").decode()
                method_pass, hostport = decoded.split("@", 1)
                method, password   = method_pass.split(":", 1)
                host, port_str     = hostport.rsplit(":", 1)
                port               = int(port_str)
        except Exception:
            return None
        return {
            "name":     name,
            "type":     "ss",
            "server":   host,
            "port":     port,
            "cipher":   method,
            "password": password,
            "udp":      True,
        }

    # ── Hysteria2 / hy2 ────────────────────────────────────────────────────
    if cfg.startswith("hysteria2://") or cfg.startswith("hy2://"):
        scheme = "hysteria2://" if cfg.startswith("hysteria2://") else "hy2://"
        try:
            body       = cfg[len(scheme):]
            remark     = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            password, hostport = body.split("@", 1)
            host, port_str     = hostport.rsplit(":", 1)
            port               = int(port_str)
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
        except Exception:
            return None
        proxy: dict = {
            "name":     name,
            "type":     "hysteria2",
            "server":   host,
            "port":     port,
            "password": password,
            "udp":      True,
        }
        sni = params.get("sni", "")
        if sni:
            proxy["sni"] = sni
        if params.get("insecure", "0") == "1":
            proxy["skip-cert-verify"] = True
        return proxy

    return None   # unsupported protocol


def build_clash_yaml(configs: list[str]) -> str:
    """
    Convert renamed configs to a Clash-compatible subscription YAML string.
    Only successfully parsed proxies are included.
    """
    proxies:     list[dict] = []
    proxy_names: list[str]  = []

    for cfg in configs:
        name  = cfg.split("#")[-1] if "#" in cfg else f"proxy_{len(proxies)+1}"
        proxy = config_to_clash_proxy(cfg, name)
        if proxy:
            proxies.append(proxy)
            proxy_names.append(name)

    if not proxies:
        return "# No parseable proxies found\nproxies: []\n"

    lines: list[str] = []
    lines.append("# Clash subscription — auto-generated")
    lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"# Total proxies: {len(proxies)}")
    lines.append("")
    lines.append("mixed-port: 7890")
    lines.append("allow-lan: false")
    lines.append("mode: rule")
    lines.append("log-level: info")
    lines.append("")

    # ── proxies block ──────────────────────────────────────────────────
    lines.append("proxies:")
    for p in proxies:
        lines.append(f"  - name: \"{p['name']}\"")
        for k, v in p.items():
            if k == "name":
                continue
            if isinstance(v, dict):
                lines.append(f"    {k}:")
                for dk, dv in v.items():
                    if isinstance(dv, dict):
                        lines.append(f"      {dk}:")
                        for ddk, ddv in dv.items():
                            lines.append(f"        {ddk}: \"{ddv}\"")
                    else:
                        lines.append(f"      {dk}: \"{dv}\"")
            elif isinstance(v, bool):
                lines.append(f"    {k}: {str(v).lower()}")
            elif isinstance(v, str):
                lines.append(f"    {k}: \"{v}\"")
            else:
                lines.append(f"    {k}: {v}")
        lines.append("")

    # ── proxy-groups block ─────────────────────────────────────────────
    lines.append("proxy-groups:")
    lines.append("  - name: \"AUTO\"")
    lines.append("    type: url-test")
    lines.append("    url: http://www.gstatic.com/generate_204")
    lines.append("    interval: 300")
    lines.append("    proxies:")
    for n in proxy_names:
        lines.append(f"      - \"{n}\"")
    lines.append("")
    lines.append("  - name: \"PROXY\"")
    lines.append("    type: select")
    lines.append("    proxies:")
    lines.append("      - \"AUTO\"")
    for n in proxy_names:
        lines.append(f"      - \"{n}\"")
    lines.append("")

    # ── rules block ───────────────────────────────────────────────────
    lines.append("rules:")
    lines.append("  - MATCH,AUTO")
    lines.append("")

    return "\n".join(lines)

# ── Save ──────────────────────────────────────────────────────────────────────

def save(configs: list[str]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    configs = rename_remarks(configs)

    # ── v2ray output ──────────────────────────────────────────────────
    raw     = "\n".join(configs)
    encoded = base64.b64encode(raw.encode()).decode()
    OUTPUT_FILE.write_text(encoded)
    Path("output/configs_plain.txt").write_text(raw)
    print(f"\n✅ Saved {len(configs)} unique configs → {OUTPUT_FILE}")
    print(f"   Base64 length: {len(encoded)} chars")

    # ── Clash output ──────────────────────────────────────────────────
    clash_yaml  = build_clash_yaml(configs)
    CLASH_OUTPUT_FILE.write_text(clash_yaml, encoding="utf-8")
    clash_count = clash_yaml.count("\n  - name:")
    print(f"✅ Saved Clash subscription → {CLASH_OUTPUT_FILE} ({clash_count} proxies)")

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"🔍 Collecting V2Ray configs [{datetime.now().strftime('%Y-%m-%d %H:%M UTC')}]")
    print(f"   Channels      : {len(CHANNELS)}")
    print(f"   External subs : {len(EXTERNAL_SUB_URLS)}")
    print(f"   Protocols     : {', '.join(p.rstrip('://') for p in PROTOCOLS)}\n")

    configs = asyncio.run(collect_all())

    if not configs:
        print("⚠️  No configs found.")
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text("")
        CLASH_OUTPUT_FILE.write_text("proxies: []\n")
        return

    save(configs)


if __name__ == "__main__":
    main()
