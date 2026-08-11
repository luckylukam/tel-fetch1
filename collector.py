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
    "kurdconfig", "Configir98", "YamYamProxy", "FreeConfigForYou", "begoo_vpn_gp","iranconnecting",
    "Zed_NetMeli", "on_proxy1", "Spotify_Porteghali", "oxnet_ir", "proxy_station", "bygfw" , "ezaccess1",
    "appxa", "v2rayyngvpn", "sparrk_vpn", "amir_webstudio"
]

# ── External subscription URLs ────────────────────────────────────────────────
# Add any v2ray (base64) or Clash (YAML) subscription URLs here.
# The script auto-detects the format and merges configs into both outputs.
EXTERNAL_SUB_URLS: list[str] = [
    # "https://example.com/v2ray-sub",       # v2ray base64 subscription
    # "https://example.com/clash-sub.yaml",  # Clash YAML subscription
]

PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")

OUTPUT_FILE       = Path("output/configs.txt")
CLASH_OUTPUT_FILE = Path("output/clash.yaml")

# ── Tested-only LeastPing balancer config (NEW, additive) ─────────────────────
# Separate output: a single importable Xray JSON config containing ONLY configs
# that passed a live TCP reachability test, wired into a "leastPing" balancer +
# observatory so the client (v2rayNG "custom configuration") auto-switches to
# whichever tested server currently responds fastest — no manual re-picking.
LEASTPING_OUTPUT_FILE   = Path("output/xray_leastping.json")
TEST_TIMEOUT_SECONDS    = 5      # per-server TCP connect timeout
TEST_CONCURRENCY        = 60     # parallel TCP tests in flight
MAX_BALANCER_SERVERS    = 40     # cap on how many tested servers go into the balancer
OBSERVATORY_PROBE_INTERVAL = "1s"  # lowest practical value — see note in build_xray_leastping_config()

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
    try:
        padded  = text + "=" * (-len(text) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        decoded = text  # maybe already plain text

    configs: list[str] = []
    for line in decoded.splitlines():
        line = line.strip()
        if any(line.startswith(p) for p in PROTOCOLS):
            configs.append(line)
    return configs


# ── Minimal YAML parser for Clash proxy blocks (no external deps) ─────────────

def _parse_clash_yaml_proxies(text: str) -> list[dict]:
    """
    Extract the 'proxies:' list from a Clash YAML without using PyYAML.
    Each proxy is a block of '  - key: value' lines.  Nested dicts (ws-opts,
    grpc-opts, reality-opts, headers) are also handled one level deep.
    Returns a list of dicts.
    """
    proxies_block_match = re.search(
        r'^proxies\s*:\s*\n(.*?)(?=^\S|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not proxies_block_match:
        return []

    block = proxies_block_match.group(1)
    proxies: list[dict] = []
    current: dict | None = None
    current_nested_key: str | None = None
    current_nested: dict | None = None

    for raw_line in block.splitlines():
        item_start = re.match(r'^\s{0,4}-\s+(\w[\w-]*):\s*(.*)', raw_line)
        if item_start:
            if current is not None:
                if current_nested is not None and current_nested_key:
                    current[current_nested_key] = current_nested
                proxies.append(current)
            current = {}
            current_nested_key = None
            current_nested = None
            key   = item_start.group(1)
            value = item_start.group(2).strip().strip('"\'')
            current[key] = _cast(value)
            continue

        if current is None:
            continue

        nested_start = re.match(r'^\s{4,6}([\w-]+)\s*:\s*$', raw_line)
        if nested_start:
            if current_nested is not None and current_nested_key:
                current[current_nested_key] = current_nested
            current_nested_key = nested_start.group(1)
            current_nested = {}
            continue

        if current_nested is not None:
            sub = re.match(r'^\s{6,8}([\w-]+)\s*:\s*(.*)', raw_line)
            if sub:
                current_nested[sub.group(1)] = _cast(sub.group(2).strip().strip('"\''))
                continue
            else:
                current[current_nested_key] = current_nested
                current_nested_key = None
                current_nested = None

        kv = re.match(r'^\s{4,6}([\w-]+)\s*:\s*(.*)', raw_line)
        if kv:
            current[kv.group(1)] = _cast(kv.group(2).strip().strip('"\''))

    if current is not None:
        if current_nested is not None and current_nested_key:
            current[current_nested_key] = current_nested
        proxies.append(current)

    return proxies


def _cast(value: str):
    """Best-effort cast a YAML scalar string to int / bool / str."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    return value


def _clash_proxy_to_uri(proxy: dict) -> str | None:
    """
    Convert a single Clash proxy dict back to a proxy URI string.
    Supports vmess, vless, trojan, ss, hysteria2.
    Returns None for unsupported / unparseable entries.
    """
    ptype = str(proxy.get("type", "")).lower()
    name  = str(proxy.get("name", "proxy"))
    host  = str(proxy.get("server", ""))
    port  = proxy.get("port", 443)

    try:
        if ptype == "vmess":
            raw = {
                "v":    "2",
                "ps":   name,
                "add":  host,
                "port": str(port),
                "id":   str(proxy.get("uuid", "")),
                "aid":  str(proxy.get("alterId", 0)),
                "scy":  str(proxy.get("cipher", "auto")),
                "net":  "tcp",
                "type": "none",
                "tls":  "tls" if proxy.get("tls") else "",
                "sni":  str(proxy.get("servername", "")),
            }
            ws = proxy.get("ws-opts", {})
            if proxy.get("network") == "ws":
                raw["net"]  = "ws"
                raw["path"] = str(ws.get("path", "/"))
                raw["host"] = str((ws.get("headers") or {}).get("Host", host))
            elif proxy.get("network") == "grpc":
                raw["net"]  = "grpc"
                raw["path"] = str((proxy.get("grpc-opts") or {}).get("grpc-service-name", ""))
            b64 = base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()
            return f"vmess://{b64}"

        if ptype == "vless":
            uuid   = str(proxy.get("uuid", ""))
            params: list[str] = []
            reality = proxy.get("reality-opts") or {}
            if reality:
                params.append("security=reality")
                params.append(f"pbk={reality.get('public-key', '')}")
                params.append(f"sid={reality.get('short-id', '')}")
            elif proxy.get("tls"):
                params.append("security=tls")
            if proxy.get("servername"):
                params.append(f"sni={proxy['servername']}")
            if proxy.get("network") == "ws":
                ws = proxy.get("ws-opts") or {}
                params.append("type=ws")
                params.append(f"path={ws.get('path', '/')}")
                params.append(f"host={(ws.get('headers') or {}).get('Host', host)}")
            elif proxy.get("network") == "grpc":
                params.append("type=grpc")
                params.append(f"serviceName={(proxy.get('grpc-opts') or {}).get('grpc-service-name', '')}")
            qs = "?" + "&".join(params) if params else ""
            return f"vless://{uuid}@{host}:{port}{qs}#{name}"

        if ptype == "trojan":
            password = str(proxy.get("password", ""))
            params: list[str] = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            if proxy.get("network") == "ws":
                ws = proxy.get("ws-opts") or {}
                params.append("type=ws")
                params.append(f"path={ws.get('path', '/')}")
            qs = "?" + "&".join(params) if params else ""
            return f"trojan://{password}@{host}:{port}{qs}#{name}"

        if ptype == "ss":
            method   = str(proxy.get("cipher", "aes-256-gcm"))
            password = str(proxy.get("password", ""))
            userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
            return f"ss://{userinfo}@{host}:{port}#{name}"

        if ptype in ("hysteria2", "hy2"):
            password = str(proxy.get("password", ""))
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



# ── Live reachability testing (NEW, additive) ──────────────────────────────────

async def _tcp_ping(host: str, port: int, timeout: float = TEST_TIMEOUT_SECONDS) -> float | None:
    """Try a raw TCP connect to host:port. Returns latency in ms, or None if dead."""
    loop  = asyncio.get_event_loop()
    start = loop.time()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        latency_ms = (loop.time() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return latency_ms
    except Exception:
        return None


async def test_configs(configs: list[str]) -> list[tuple[str, dict, float]]:
    """
    Actively test every config with a TCP connect to its host:port.
    Returns only the ones that responded, as (config_uri, parsed_proxy_dict, latency_ms),
    sorted fastest-first.
    """
    sem = asyncio.Semaphore(TEST_CONCURRENCY)
    results: list[tuple[str, dict, float]] = []

    async def _check(cfg: str) -> None:
        proxy = config_to_clash_proxy(cfg, "test")
        if not proxy or not proxy.get("server") or not proxy.get("port"):
            return
        async with sem:
            latency = await _tcp_ping(str(proxy["server"]), int(proxy["port"]))
        if latency is not None:
            results.append((cfg, proxy, latency))

    await asyncio.gather(*(_check(c) for c in configs))
    results.sort(key=lambda r: r[2])
    return results


# ── Xray "leastPing" balancer config builder (NEW, additive) ──────────────────

def clash_proxy_to_xray_outbound(proxy: dict, tag: str) -> dict | None:
    """
    Convert an already-parsed clash-style proxy dict (from config_to_clash_proxy)
    into a raw Xray-core outbound. Supports vmess / vless / trojan / shadowsocks —
    the protocols Xray-core's own outbound + balancer machinery natively handles.
    (hysteria2/hy2/ssr/tuic are skipped here; they still work fine in configs.txt
    and clash.yaml, untouched, they just can't sit in this particular balancer.)
    """
    ptype   = str(proxy.get("type", "")).lower()
    host    = str(proxy.get("server", ""))
    port    = int(proxy.get("port", 443))
    network = str(proxy.get("network", "tcp"))

    stream: dict = {"network": network if network in ("tcp", "ws", "grpc", "xhttp", "httpupgrade") else "tcp"}

    if network == "ws":
        ws = proxy.get("ws-opts") or {}
        stream["wsSettings"] = {"path": ws.get("path", "/"), "headers": ws.get("headers") or {}}
    elif network == "grpc":
        grpc = proxy.get("grpc-opts") or {}
        stream["grpcSettings"] = {"serviceName": grpc.get("grpc-service-name", "")}
    elif network == "xhttp":
        xh = proxy.get("xhttp-opts") or {}
        stream["xhttpSettings"] = {"path": xh.get("path", "/"), "host": xh.get("host", "")}
    elif network == "httpupgrade":
        hu = proxy.get("httpupgrade-opts") or {}
        stream["httpupgradeSettings"] = {"path": hu.get("path", "/"), "host": hu.get("host", "")}

    reality = proxy.get("reality-opts")
    if reality:
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName":  proxy.get("servername", ""),
            "publicKey":   reality.get("public-key", ""),
            "shortId":     reality.get("short-id", ""),
            "fingerprint": "chrome",
        }
    elif proxy.get("tls") or ptype == "trojan":
        # FIX: trojan is TLS-by-design (that's the whole point of the protocol —
        # it disguises itself as HTTPS). The parsed proxy dict never carries an
        # explicit "tls" key for trojan (Clash's own schema doesn't expose one
        # either, since it's implicit there), so without this OR-condition every
        # trojan outbound below was silently built with security:"none" — sent
        # plaintext to a server expecting a TLS handshake, i.e. exactly the
        # "TLS handshake timeout" v2rayNG reports.
        stream["security"]    = "tls"
        stream["tlsSettings"] = {
            "serverName":    proxy.get("servername") or proxy.get("sni") or host,
            "allowInsecure": bool(proxy.get("skip-cert-verify", False)),
        }
    else:
        stream["security"] = "none"

    try:
        if ptype == "vmess":
            return {
                "tag": tag, "protocol": "vmess",
                "settings": {"vnext": [{
                    "address": host, "port": port,
                    "users": [{
                        "id": str(proxy.get("uuid", "")),
                        "alterId": int(proxy.get("alterId", 0)),
                        "security": str(proxy.get("cipher", "auto")),
                    }],
                }]},
                "streamSettings": stream,
            }
        if ptype == "vless":
            return {
                "tag": tag, "protocol": "vless",
                "settings": {"vnext": [{
                    "address": host, "port": port,
                    "users": [{"id": str(proxy.get("uuid", "")), "encryption": "none"}],
                }]},
                "streamSettings": stream,
            }
        if ptype == "trojan":
            return {
                "tag": tag, "protocol": "trojan",
                "settings": {"servers": [{
                    "address": host, "port": port,
                    "password": str(proxy.get("password", "")),
                }]},
                "streamSettings": stream,
            }
        if ptype == "ss":
            return {
                "tag": tag, "protocol": "shadowsocks",
                "settings": {"servers": [{
                    "address": host, "port": port,
                    "method": str(proxy.get("cipher", "aes-256-gcm")),
                    "password": str(proxy.get("password", "")),
                }]},
                "streamSettings": {"network": "tcp", "security": "none"},
            }
    except Exception:
        return None

    return None


def build_xray_leastping_config(tested: list[tuple[str, dict, float]]) -> dict:
    """
    Build a single, ready-to-import Xray JSON config containing only the tested,
    currently-reachable servers, wired into a leastPing balancer + observatory.
    v2rayNG: import this as a "custom configuration" and it will keep pinging
    every server in the background and route through whichever is fastest/alive,
    switching automatically without you touching the app.
    """
    outbounds: list[dict] = []
    for i, (_cfg, proxy, _latency) in enumerate(tested[:MAX_BALANCER_SERVERS], start=1):
        ob = clash_proxy_to_xray_outbound(proxy, tag=f"p{i}")
        if ob:
            outbounds.append(ob)

    proxy_tags = [ob["tag"] for ob in outbounds]  # e.g. ["p1", "p2", ...] before direct/block are appended

    outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    balancer: dict = {"tag": "auto", "selector": ["p"], "strategy": {"type": "leastPing"}}
    if proxy_tags:
        # FIX: without a fallbackTag, outbound selection is undefined during the
        # window before the first observatory probe finishes (right when you hit
        # Connect) — this pins it to a known proxy instead of failing/blocking.
        balancer["fallbackTag"] = proxy_tags[0]

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http"},
        ],
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [balancer],
            "rules": [
                {"type": "field", "network": "tcp,udp", "balancerTag": "auto"}
            ],
        },
        "observatory": {
            "subjectSelector":    ["p"],
            "probeURL":           "https://www.gstatic.com/generate_204",
            # Set to OBSERVATORY_PROBE_INTERVAL (lowest practical value) so a dead
            # server drops out of rotation as fast as possible for the *next*
            # connection. This does NOT save an already-open connection whose
            # server died mid-session — nothing can, that TCP/TLS session is just
            # gone. It only shortens how long a dead server stays eligible to be
            # picked again. At "1s" with ~MAX_BALANCER_SERVERS outbounds probed
            # concurrently every cycle, expect real background battery/data use —
            # raise this (e.g. "5s") in one place here if that's noticeable.
            "probeInterval":      OBSERVATORY_PROBE_INTERVAL,
            "enableConcurrency":  True,
        },
    }


def _extract_configs_from_clash_sub(text: str) -> list[str]:
    """Parse a Clash YAML subscription and convert each proxy back to a URI."""
    proxies = _parse_clash_yaml_proxies(text)
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
        tg_tasks  = [fetch_channel(client, ch) for ch in CHANNELS]
        sub_tasks = [fetch_external_sub(client, url) for url in EXTERNAL_SUB_URLS]

        tg_results  = await asyncio.gather(*tg_tasks)
        sub_results = await asyncio.gather(*sub_tasks)

    seen: set[str] = set()
    all_configs: list[str] = []

    for batch in (*tg_results, *sub_results):
        for cfg in batch:
            # FIX 1: dedup on URI only (strip remark) so the same proxy posted
            # in two channels or with different remark labels isn't duplicated.
            uri = cfg.split("#")[0]
            if uri not in seen:
                seen.add(uri)
                all_configs.append(cfg)

    return all_configs

# ── Rename remarks ────────────────────────────────────────────────────────────

def rename_remarks(configs: list[str]) -> list[str]:
    renamed = []
    for i, cfg in enumerate(configs, start=1):
        # FIX 2: strip trailing '?' left by empty query strings (e.g. ss://...@host:port?)
        base = cfg.split("#")[0].rstrip("?") if "#" in cfg else cfg.rstrip("?")
        renamed.append(f"{base}#mn_conf{i}")
    return renamed

# ── Clash conversion ──────────────────────────────────────────────────────────

def _decode_vmess(uri: str) -> dict | None:
    try:
        b64  = uri[len("vmess://"):]
        b64 += "=" * (-len(b64) % 4)
        data = json.loads(base64.b64decode(b64).decode())
        return data
    except Exception:
        return None


def _parse_userinfo_host(uri: str, scheme: str) -> tuple[str, str, int, str] | None:
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
            proxy["network"]   = "grpc"
            proxy["grpc-opts"] = {"grpc-service-name": str(raw.get("path", ""))}
        # FIX 3: handle xhttp and httpupgrade transport for VMess
        elif net == "xhttp":
            proxy["network"]    = "xhttp"
            proxy["xhttp-opts"] = {
                "path": str(raw.get("path", "/")),
                "host": str(raw.get("host", proxy["server"])),
            }
        elif net == "httpupgrade":
            proxy["network"]           = "httpupgrade"
            proxy["httpupgrade-opts"]  = {
                "path": str(raw.get("path", "/")),
                "host": str(raw.get("host", proxy["server"])),
            }
        if str(raw.get("tls", "")) == "tls":
            proxy["tls"] = True
            sni = str(raw.get("sni", raw.get("host", "")))
            if sni:
                proxy["servername"] = sni
        return proxy

    if cfg.startswith("vless://"):
        try:
            body       = cfg[len("vless://"):]
            if "#" in body:
                body, _ = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            uuid, hostport = body.split("@", 1)
            host, port_str = hostport.rsplit(":", 1)
            port   = int(port_str)
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
        except Exception:
            return None
        proxy = {"name": name, "type": "vless", "server": host, "port": port, "uuid": uuid, "udp": True}
        if params.get("security") == "tls":
            proxy["tls"] = True
            if params.get("sni"):
                proxy["servername"] = params["sni"]
        if params.get("security") == "reality":
            proxy["tls"] = True
            proxy["reality-opts"] = {"public-key": params.get("pbk", ""), "short-id": params.get("sid", "")}
            if params.get("sni"):
                proxy["servername"] = params["sni"]
        net = params.get("type", "tcp")
        if net == "ws":
            proxy["network"]  = "ws"
            proxy["ws-opts"]  = {"path": params.get("path", "/"), "headers": {"Host": params.get("host", host)}}
        elif net == "grpc":
            proxy["network"]   = "grpc"
            proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
        # FIX 3: handle xhttp and httpupgrade transport for VLESS
        elif net == "xhttp":
            proxy["network"]    = "xhttp"
            proxy["xhttp-opts"] = {
                "path": params.get("path", "/"),
                "host": params.get("host", host),
            }
        elif net == "httpupgrade":
            proxy["network"]          = "httpupgrade"
            proxy["httpupgrade-opts"] = {
                "path": params.get("path", "/"),
                "host": params.get("host", host),
            }
        return proxy

    if cfg.startswith("trojan://"):
        parsed = _parse_userinfo_host(cfg, "trojan://")
        if not parsed:
            return None
        password, host, port, _ = parsed
        proxy = {"name": name, "type": "trojan", "server": host, "port": port, "password": password, "udp": True}
        try:
            params_str = cfg.split("?", 1)[1].split("#")[0] if "?" in cfg else ""
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
            if params.get("sni"):
                proxy["sni"] = params["sni"]
            net = params.get("type", "tcp")
            if net == "ws":
                proxy["network"]  = "ws"
                proxy["ws-opts"]  = {"path": params.get("path", "/")}
            elif net == "grpc":
                proxy["network"]   = "grpc"
                proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
            # FIX 3: handle xhttp and httpupgrade transport for Trojan
            elif net == "xhttp":
                proxy["network"]    = "xhttp"
                proxy["xhttp-opts"] = {
                    "path": params.get("path", "/"),
                    "host": params.get("host", host),
                }
            elif net == "httpupgrade":
                proxy["network"]          = "httpupgrade"
                proxy["httpupgrade-opts"] = {
                    "path": params.get("path", "/"),
                    "host": params.get("host", host),
                }
        except Exception:
            pass
        return proxy

    if cfg.startswith("ss://"):
        try:
            body = cfg[len("ss://"):]
            if "#" in body:
                body, _ = body.split("#", 1)
            # FIX 2 (also in Clash parser): strip trailing '?' from query-less ss URIs
            body = body.rstrip("?")
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
        return {"name": name, "type": "ss", "server": host, "port": port, "cipher": method, "password": password, "udp": True}

    if cfg.startswith("hysteria2://") or cfg.startswith("hy2://"):
        scheme = "hysteria2://" if cfg.startswith("hysteria2://") else "hy2://"
        try:
            body = cfg[len(scheme):]
            if "#" in body:
                body, _ = body.split("#", 1)
            params_str = ""
            if "?" in body:
                body, params_str = body.split("?", 1)
            password, hostport = body.split("@", 1)
            host, port_str     = hostport.rsplit(":", 1)
            port               = int(port_str)
            params = dict(p.split("=", 1) for p in params_str.split("&") if "=" in p)
        except Exception:
            return None
        proxy: dict = {"name": name, "type": "hysteria2", "server": host, "port": port, "password": password, "udp": True}
        if params.get("sni"):
            proxy["sni"] = params["sni"]
        if params.get("insecure", "0") == "1":
            proxy["skip-cert-verify"] = True
        return proxy

    return None


def build_clash_yaml(configs: list[str]) -> str:
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
    lines.append("rules:")
    lines.append("  - MATCH,AUTO")
    lines.append("")

    return "\n".join(lines)

# ── Save ──────────────────────────────────────────────────────────────────────

def save(configs: list[str]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    configs = rename_remarks(configs)

    raw     = "\n".join(configs)
    encoded = base64.b64encode(raw.encode()).decode()
    OUTPUT_FILE.write_text(encoded)
    Path("output/configs_plain.txt").write_text(raw)
    print(f"\n✅ Saved {len(configs)} unique configs → {OUTPUT_FILE}")
    print(f"   Base64 length: {len(encoded)} chars")

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

    # ── NEW, additive: test every config live, build the tested-only ──────────
    # leastPing balancer config as a separate file. Doesn't touch configs.txt
    # or clash.yaml above.
    print(f"\n🧪 Testing {len(configs)} configs for live reachability (timeout {TEST_TIMEOUT_SECONDS}s)...")
    tested = asyncio.run(test_configs(configs))
    print(f"   ✅ {len(tested)}/{len(configs)} configs responded")

    if tested:
        leastping_cfg = build_xray_leastping_config(tested)
        LEASTPING_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEASTPING_OUTPUT_FILE.write_text(json.dumps(leastping_cfg, indent=2))
        included = min(len(tested), MAX_BALANCER_SERVERS)
        print(f"✅ Saved tested LeastPing config → {LEASTPING_OUTPUT_FILE} ({included} servers, auto-switching)")
    else:
        print("⚠️  No configs passed the reachability test — skipping LeastPing config.")


if __name__ == "__main__":
    main()
