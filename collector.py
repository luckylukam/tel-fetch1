import re
import base64
import json
import httpx
import asyncio
from html import unescape
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
CHANNELS = [
    "kurdconfig",     "Configir98",     "YamYamProxy",     "FreeConfigForYou",
    "Zed_NetMeli",    "on_proxy1",      "Spotify_Porteghali",  "oxnet_ir"
]
PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")
OUTPUT_FILE       = Path("output/configs.txt")
CLASH_OUTPUT_FILE = Path("output/clash.yaml")
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATTERN = re.compile(
    r'(?:vmess|vless|trojan|ss|ssr|tuic|hysteria2|hy2)://[^\s<>"\'`]+'
)

# ── Fetch ─────────────────────────────────────────────────────────────────────

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


async def collect_all() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [fetch_channel(client, ch) for ch in CHANNELS]
        results = await asyncio.gather(*tasks)
    seen: set[str] = set()
    all_configs: list[str] = []
    for batch in results:
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
        b64 = uri[len("vmess://"):]
        # Add padding if needed
        b64 += "=" * (-len(b64) % 4)
        data = json.loads(base64.b64decode(b64).decode())
        return data
    except Exception:
        return None


def _parse_userinfo_host(uri: str, scheme: str) -> tuple[str, str, int, str] | None:
    """
    Parse  scheme://user@host:port#remark  (used by vless, trojan, ss, hy2…).
    Returns (user, host, port, remark) or None.
    """
    try:
        body = uri[len(scheme):]
        remark = ""
        if "#" in body:
            body, remark = body.split("#", 1)
        # strip query string
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
            proxy["network"] = "ws"
            proxy["ws-opts"] = {
                "path": str(raw.get("path", "/")),
                "headers": {"Host": str(raw.get("host", proxy["server"]))},
            }
        elif net == "grpc":
            proxy["network"] = "grpc"
            proxy["grpc-opts"] = {"grpc-service-name": str(raw.get("path", ""))}
        tls = str(raw.get("tls", ""))
        if tls == "tls":
            proxy["tls"] = True
            sni = str(raw.get("sni", raw.get("host", "")))
            if sni:
                proxy["servername"] = sni
        return proxy

    # ── VLESS ──────────────────────────────────────────────────────────────
    if cfg.startswith("vless://"):
        # vless://uuid@host:port?params#remark
        try:
            body = cfg[len("vless://"):]
            remark = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            uuid, hostport = body.split("@", 1)
            host, port_str = hostport.rsplit(":", 1)
            port = int(port_str)
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
            proxy["network"] = "ws"
            proxy["ws-opts"] = {
                "path":    params.get("path", "/"),
                "headers": {"Host": params.get("host", host)},
            }
        elif net == "grpc":
            proxy["network"] = "grpc"
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
        # parse optional params
        try:
            params_str = cfg.split("?", 1)[1].split("#")[0] if "?" in cfg else ""
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
            sni = params.get("sni", "")
            if sni:
                proxy["sni"] = sni
            if params.get("type") == "ws":
                proxy["network"] = "ws"
                proxy["ws-opts"] = {"path": params.get("path", "/")}
        except Exception:
            pass
        return proxy

    # ── Shadowsocks ────────────────────────────────────────────────────────
    if cfg.startswith("ss://"):
        try:
            body = cfg[len("ss://"):]
            remark = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            # Two encodings: plain  method:pass@host:port  OR  base64@host:port
            if "@" in body:
                userinfo, hostport = body.rsplit("@", 1)
                host, port_str = hostport.rsplit(":", 1)
                port = int(port_str)
                # userinfo may itself be base64-encoded
                if ":" in userinfo:
                    method, password = userinfo.split(":", 1)
                else:
                    decoded = base64.b64decode(userinfo + "==").decode()
                    method, password = decoded.split(":", 1)
            else:
                decoded = base64.b64decode(body + "==").decode()
                method_pass, hostport = decoded.split("@", 1)
                method, password = method_pass.split(":", 1)
                host, port_str = hostport.rsplit(":", 1)
                port = int(port_str)
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
            body = cfg[len(scheme):]
            remark = ""
            if "#" in body:
                body, remark = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            password, hostport = body.split("@", 1)
            host, port_str = hostport.rsplit(":", 1)
            port = int(port_str)
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

    return None  # unsupported protocol


def build_clash_yaml(configs: list[str]) -> str:
    """
    Convert renamed configs to a Clash-compatible subscription YAML string.
    Only successfully parsed proxies are included.
    """
    proxies: list[dict] = []
    proxy_names: list[str] = []

    for cfg in configs:
        # Extract name from the remark (#mn_confN)
        name = cfg.split("#")[-1] if "#" in cfg else f"proxy_{len(proxies)+1}"
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

    # ── proxies block ──────────────────────────────────────────────────────
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

    # ── proxy-groups block ─────────────────────────────────────────────────
    lines.append("proxy-groups:")
    lines.append("  - name: \"PROXY\"")
    lines.append("    type: select")
    lines.append("    proxies:")
    for n in proxy_names:
        lines.append(f"      - \"{n}\"")
    lines.append("")
    lines.append("  - name: \"AUTO\"")
    lines.append("    type: url-test")
    lines.append("    url: http://www.gstatic.com/generate_204")
    lines.append("    interval: 300")
    lines.append("    proxies:")
    for n in proxy_names:
        lines.append(f"      - \"{n}\"")
    lines.append("")

    # ── rules block ───────────────────────────────────────────────────────
    lines.append("rules:")
    lines.append("  - MATCH,PROXY")
    lines.append("")

    return "\n".join(lines)

# ── Save ──────────────────────────────────────────────────────────────────────

def save(configs: list[str]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    configs = rename_remarks(configs)

    # ── Original outputs (unchanged) ──────────────────────────────────────
    raw = "\n".join(configs)
    encoded = base64.b64encode(raw.encode()).decode()
    OUTPUT_FILE.write_text(encoded)
    Path("output/configs_plain.txt").write_text(raw)
    print(f"\n✅ Saved {len(configs)} unique configs → {OUTPUT_FILE}")
    print(f"   Base64 length: {len(encoded)} chars")

    # ── Clash subscription file ───────────────────────────────────────────
    clash_yaml = build_clash_yaml(configs)
    CLASH_OUTPUT_FILE.write_text(clash_yaml, encoding="utf-8")
    clash_count = clash_yaml.count("\n  - name:")
    print(f"✅ Saved Clash subscription   → {CLASH_OUTPUT_FILE}  ({clash_count} proxies)")

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"🔍 Collecting V2Ray configs  [{datetime.now().strftime('%Y-%m-%d %H:%M UTC')}]")
    print(f"   Channels  : {len(CHANNELS)}")
    print(f"   Protocols : {', '.join(p.rstrip('://') for p in PROTOCOLS)}\n")
    configs = asyncio.run(collect_all())
    if not configs:
        print("⚠️  No configs found. Channels may have changed or be empty.")
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text("")
        CLASH_OUTPUT_FILE.write_text("proxies: []\n")
        return
    save(configs)


if __name__ == "__main__":
    main()
