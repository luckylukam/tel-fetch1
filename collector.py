import re
import base64
import json
import uuid as _uuid
import httpx
import asyncio
from html import unescape
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, parse_qs, unquote

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
    "https://raw.githubusercontent.com/patterniha/Free-Configs/main/configs.txt"
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

# ── Iran-reachability re-test (NEW, additive) ──────────────────────────────────
# Second, separate output: takes only the configs that already passed the plain
# TCP reachability test above, then re-checks each one from vantage points
# actually inside Iran, using check-host.net's public network-check API
# (https://check-host.net/about/api). check-host.net runs monitoring nodes
# hosted inside Iranian networks (e.g. "ir1.node.check-host.net", city Tehran/
# Tabriz/etc.) that open a real TCP connection to the server FROM Iran and
# report back success/failure. That's a genuine "is this reachable from inside
# Iran right now" signal — unlike a DNS lookup (which only tells you whether a
# *domain* resolves, says nothing about whether the IP itself is filtered, and
# is meaningless for the majority of these configs that use a bare IP as the
# server address with no hostname involved at all). Only configs confirmed
# reachable from Iran are written to IRAN_WORKING_OUTPUT_FILE.

IRAN_WORKING_OUTPUT_FILE      = Path("output/configs_iran_working.txt")
IRAN_WORKING_B64_OUTPUT_FILE  = Path("output/configs_iran_working_base64.txt")
IRAN_CHECK_MAX_CANDIDATES     = 60    # only re-test the top N fastest TCP-alive configs
IRAN_CHECK_CONCURRENCY        = 3     # parallel check-host.net requests in flight (be polite to a free public API)
IRAN_CHECK_MAX_NODES          = 4     # how many Iranian check-host.net nodes to query per config
IRAN_CHECK_POLL_INTERVAL      = 2     # seconds between result polls
IRAN_CHECK_POLL_ATTEMPTS      = 6     # ~12s max wait per config before giving up
IRAN_CHECK_MIN_SUCCESS_RATIO  = 0.5   # require a majority of responding Iran nodes to succeed

# ── sing-box output settings (NEW, additive) ──────────────────────────────────
# Outputs a sing-box JSON profile usable as a "Remote profile" in the sing-box
# Android app (SFA, available on F-Droid). See the sing-box section below.

SINGBOX_OUTPUT_FILE       = Path("output/singbox.json")
SINGBOX_IRAN_OUTPUT_FILE  = Path("output/singbox_iran_working.json")
MAX_SINGBOX_SERVERS       = 40    # cap on servers inside the urltest group
SINGBOX_TEST_URL          = "https://www.gstatic.com/generate_204"
SINGBOX_TEST_INTERVAL     = "1m"

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

# ── Iran-reachability re-test via check-host.net (NEW, additive) ──────────────
# See the config block near the top of the file for why check-host.net was
# chosen over a plain DNS check.

CHECK_HOST_API = "https://check-host.net"


async def _fetch_iran_nodes(client: httpx.AsyncClient) -> list[str]:
    """
    Ask check-host.net for its current node list and return the hostnames of
    nodes physically located in Iran (location country-code == 'ir'). Fetched
    live instead of hardcoded because check-host.net's node fleet changes over
    time — a hardcoded node name can silently go stale/offline.
    """
    try:
        r = await client.get(
            f"{CHECK_HOST_API}/nodes/hosts",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        nodes = data.get("nodes", {}) if isinstance(data, dict) else {}
        iran_nodes = []
        for name, info in nodes.items():
            if not isinstance(info, dict):
                continue
            location = info.get("location") or []
            country_code = str(location[0]).lower() if location else ""
            if country_code == "ir":
                iran_nodes.append(name)
        return iran_nodes
    except Exception as e:
        print(f"  ⚠️  Could not fetch check-host.net node list: {e}")
        return []


async def _check_host_submit(
    client: httpx.AsyncClient, host: str, port: int, nodes: list[str]
) -> str | None:
    """Kick off a check-host.net TCP check for host:port on the given nodes. Returns a request_id."""
    params = [("host", f"{host}:{port}")]
    for n in nodes[:IRAN_CHECK_MAX_NODES]:
        params.append(("node", n))
    try:
        r = await client.get(
            f"{CHECK_HOST_API}/check-tcp",
            params=params,
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            return data.get("request_id")
    except Exception:
        pass
    return None


async def _check_host_poll(client: httpx.AsyncClient, request_id: str) -> dict:
    """
    Poll check-host.net for results until every queried node has responded
    (or we run out of attempts). Per-node result is either:
      - a list of dicts, e.g. [{"time": 0.03, "address": "1.2.3.4"}]  → success
      - a list of dicts, e.g. [{"error": "Connection timed out"}]     → failure
      - null                                                          → still running
    """
    last: dict = {}
    for _ in range(IRAN_CHECK_POLL_ATTEMPTS):
        await asyncio.sleep(IRAN_CHECK_POLL_INTERVAL)
        try:
            r = await client.get(
                f"{CHECK_HOST_API}/check-result/{request_id}",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            last = r.json() or {}
            if all(v is not None for v in last.values()):
                break
        except Exception:
            continue
    return last


def _check_host_success_ratio(result: dict) -> float:
    """Fraction of node results that report a successful TCP connect."""
    total = 0
    ok = 0
    for _node, entries in result.items():
        if not entries:
            continue  # still pending / node never answered — excluded, not counted as failure
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            total += 1
            if "error" not in entry:
                ok += 1
    return (ok / total) if total else 0.0


async def test_iran_reachability(
    tested: list[tuple[str, dict, float]],
) -> list[tuple[str, dict, float]]:
    """
    Second-stage test: of the configs that already passed the plain TCP check,
    re-verify each one is reachable from real vantage points inside Iran via
    check-host.net. Returns only the configs confirmed reachable, fastest-first.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        iran_nodes = await _fetch_iran_nodes(client)
        if not iran_nodes:
            print("  ⚠️  No check-host.net Iran nodes available right now — skipping Iran-reachability stage.")
            return []
        print(f"  🇮🇷 Using {len(iran_nodes)} check-host.net Iran vantage node(s)")

        candidates = tested[:IRAN_CHECK_MAX_CANDIDATES]
        sem = asyncio.Semaphore(IRAN_CHECK_CONCURRENCY)
        passed: list[tuple[str, dict, float]] = []

        async def _check(item: tuple[str, dict, float]) -> None:
            cfg, proxy, latency = item
            host = str(proxy.get("server", ""))
            port = proxy.get("port")
            if not host or not port:
                return
            async with sem:
                request_id = await _check_host_submit(client, host, int(port), iran_nodes)
                if not request_id:
                    return
                result = await _check_host_poll(client, request_id)
            if _check_host_success_ratio(result) >= IRAN_CHECK_MIN_SUCCESS_RATIO:
                passed.append((cfg, proxy, latency))

        await asyncio.gather(*(_check(c) for c in candidates))

    passed.sort(key=lambda r: r[2])
    return passed


def save_iran_working(iran_ok: list[tuple[str, dict, float]]) -> None:
    """Write only the Iran-confirmed-reachable configs, plain text + base64 (mirrors configs.txt / configs_plain.txt)."""
    configs = [cfg for cfg, _proxy, _latency in iran_ok]
    IRAN_WORKING_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = "\n".join(configs)
    IRAN_WORKING_OUTPUT_FILE.write_text(raw)
    encoded = base64.b64encode(raw.encode()).decode()
    IRAN_WORKING_B64_OUTPUT_FILE.write_text(encoded)
    print(f"✅ Saved {len(configs)} Iran-confirmed-working configs → {IRAN_WORKING_OUTPUT_FILE}")
    print(f"✅ Saved base64 subscription → {IRAN_WORKING_B64_OUTPUT_FILE}")

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

# ── sing-box output (NEW, additive) ───────────────────────────────────────────
# Builds ONE sing-box JSON profile that the sing-box Android app (SFA) can load
# as a "Remote profile" URL and auto-update. Contains only the servers passed in
# (TCP-tested / Iran-confirmed), wired into a urltest group so sing-box keeps
# picking the fastest live server by itself.
#
# Unlike the Clash-dict -> Xray path above, this parses the ORIGINAL URIs, so it
# keeps fields the Clash dict drops: VLESS `flow` (xtls-rprx-vision), uTLS
# fingerprint (`fp`), `alpn`, ws early-data, hysteria2 obfs, etc.

# Ciphers sing-box's shadowsocks outbound accepts. Anything else would make the
# WHOLE profile fail to load, so unknown ones are skipped instead.
_SB_SS_METHODS = {
    "none", "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb", "chacha20-ietf",
}
_SB_VMESS_CIPHERS = {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}
_HEX = set("0123456789abcdefABCDEF")


def _q(params: dict, key: str, default: str = "") -> str:
    v = params.get(key)
    return v[0] if v else default


def _sb_valid_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except Exception:
        return False


def _sb_valid_reality(pbk: str, sid: str) -> bool:
    # public key: 32 bytes base64url (43 chars, no padding); short id: 0-16 hex, even length
    if len(pbk) != 43:
        return False
    if len(sid) > 16 or len(sid) % 2 or any(c not in _HEX for c in sid):
        return False
    return True


def _sb_tls(params: dict, server: str, force: bool = False) -> dict | None:
    security = _q(params, "security", "tls" if force else "none")
    if security not in ("tls", "reality"):
        return None
    sni = _q(params, "sni") or _q(params, "peer") or _q(params, "host") or server
    tls: dict = {"enabled": True, "server_name": sni}
    if _q(params, "allowInsecure") in ("1", "true") or _q(params, "insecure") in ("1", "true"):
        tls["insecure"] = True
    alpn = _q(params, "alpn")
    if alpn:
        tls["alpn"] = [a for a in unquote(alpn).split(",") if a]
    fp = _q(params, "fp")
    if security == "reality":
        pbk, sid = _q(params, "pbk"), _q(params, "sid")
        if not _sb_valid_reality(pbk, sid):
            return None
        tls["reality"] = {"enabled": True, "public_key": pbk, "short_id": sid}
        tls["utls"] = {"enabled": True, "fingerprint": fp or "chrome"}
    elif fp and fp != "randomized":
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    return tls


def _sb_transport(net: str, params: dict, server: str) -> tuple[bool, dict | None]:
    """Return (supported, transport_dict_or_None). xhttp/kcp/quic aren't supported -> (False, None)."""
    host = _q(params, "host")
    path = unquote(_q(params, "path", "/")) or "/"
    if net in ("", "tcp"):
        if _q(params, "headerType") not in ("", "none"):
            return False, None          # tcp+http header obfuscation: skip
        return True, None
    if net == "ws":
        t: dict = {"type": "ws", "path": path}
        if "?ed=" in path or "&ed=" in path:
            base, _, query = path.partition("?")
            kv = parse_qs(query)
            if kv.get("ed", [""])[0].isdigit():
                t["path"] = base or "/"
                t["max_early_data"] = int(kv["ed"][0])
                t["early_data_header_name"] = "Sec-WebSocket-Protocol"
        if host:
            t["headers"] = {"Host": host}
        return True, t
    if net == "grpc":
        return True, {"type": "grpc", "service_name": _q(params, "serviceName") or _q(params, "path")}
    if net == "httpupgrade":
        t = {"type": "httpupgrade", "path": path}
        if host:
            t["host"] = host
        return True, t
    return False, None


def _sb_parse_hostport(hostport: str) -> tuple[str, int] | None:
    try:
        if hostport.startswith("["):                      # [ipv6]:port
            host, _, port = hostport[1:].partition("]:")
        else:
            host, _, port = hostport.rpartition(":")
        p = int(port)
        if not host or not 1 <= p <= 65535:
            return None
        return host, p
    except Exception:
        return None


def uri_to_singbox_outbound(cfg: str, tag: str) -> dict | None:
    """Convert one proxy URI to a sing-box outbound dict, or None if unsupported/invalid."""
    try:
        base = cfg.split("#")[0]

        if base.startswith("vmess://"):
            b64 = base[len("vmess://"):]
            raw = json.loads(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode())
            server, port = str(raw.get("add", "")), int(raw.get("port", 0))
            uid = str(raw.get("id", ""))
            if not server or not 1 <= port <= 65535 or not _sb_valid_uuid(uid):
                return None
            params = {
                "host": [str(raw.get("host", ""))], "path": [str(raw.get("path", "/"))],
                "serviceName": [str(raw.get("path", ""))], "sni": [str(raw.get("sni", ""))],
                "alpn": [str(raw.get("alpn", ""))], "fp": [str(raw.get("fp", ""))],
                "security": ["tls" if str(raw.get("tls", "")) == "tls" else "none"],
                "headerType": [str(raw.get("type", "none"))],
            }
            ok, transport = _sb_transport(str(raw.get("net", "tcp")), params, server)
            if not ok:
                return None
            cipher = str(raw.get("scy") or raw.get("security") or "auto")
            ob: dict = {
                "type": "vmess", "tag": tag, "server": server, "server_port": port,
                "uuid": uid, "security": cipher if cipher in _SB_VMESS_CIPHERS else "auto",
                "alter_id": int(raw.get("aid", 0) or 0),
            }
            tls = _sb_tls(params, server)
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if base.startswith("ss://"):
            body = base[len("ss://"):]
            if "?" in body:
                body, query = body.split("?", 1)
                if "plugin" in parse_qs(query):
                    return None                           # plugin-based ss: skip
            body = unquote(body)
            if "@" in body:
                userinfo, hostport = body.rsplit("@", 1)
                if ":" not in userinfo:
                    userinfo = base64.urlsafe_b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
            else:
                decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
                userinfo, hostport = decoded.rsplit("@", 1)
            method, password = userinfo.split(":", 1)
            hp = _sb_parse_hostport(hostport)
            if not hp or method not in _SB_SS_METHODS:
                return None
            return {"type": "shadowsocks", "tag": tag, "server": hp[0], "server_port": hp[1],
                    "method": method, "password": password}

        # vless / trojan / hysteria2 share URL-style parsing
        for scheme in ("vless://", "trojan://", "hysteria2://", "hy2://"):
            if base.startswith(scheme):
                break
        else:
            return None
        u = urlsplit(base)
        params = parse_qs(u.query)
        user = unquote(u.username or "")
        server, port = u.hostname or "", u.port or 0
        if not server or not 1 <= port <= 65535 or not user:
            return None

        if scheme == "vless://":
            if not _sb_valid_uuid(user) or _q(params, "encryption", "none") != "none":
                return None
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            ob = {"type": "vless", "tag": tag, "server": server, "server_port": port, "uuid": user}
            flow = _q(params, "flow")
            if flow:
                # sing-box only knows plain vision; Xray's "-udp443" variant maps to it
                if flow not in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
                    return None
                ob["flow"] = "xtls-rprx-vision"
            tls = _sb_tls(params, server)
            if _q(params, "security") in ("tls", "reality") and not tls:
                return None                                # broken reality/tls params
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if scheme == "trojan://":
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            tls = _sb_tls(params, server, force=True)     # trojan is always TLS
            if not tls:
                return None
            ob = {"type": "trojan", "tag": tag, "server": server, "server_port": port,
                  "password": user, "tls": tls}
            if transport:
                ob["transport"] = transport
            return ob

        # hysteria2 / hy2
        if _q(params, "mport"):
            return None                                    # port-hopping: skip
        tls = _sb_tls({**params, "security": ["tls"]}, server) or {"enabled": True, "server_name": server}
        ob = {"type": "hysteria2", "tag": tag, "server": server, "server_port": port,
              "password": user, "tls": tls}
        if _q(params, "obfs") == "salamander":
            ob["obfs"] = {"type": "salamander", "password": _q(params, "obfs-password")}
        return ob
    except Exception:
        return None


def build_singbox_config(tested: list) -> dict | None:
    """
    tested: list of (config_uri, proxy_dict, latency_ms) fastest-first (what
    test_configs() / test_iran_reachability() return). Returns a full sing-box
    profile dict, or None if no config could be converted.
    """
    outbounds: list[dict] = []
    for cfg, _proxy, _lat in tested:
        if len(outbounds) >= MAX_SINGBOX_SERVERS:
            break
        ob = uri_to_singbox_outbound(cfg, tag=f"s{len(outbounds) + 1}")
        if ob:
            outbounds.append(ob)
    if not outbounds:
        return None

    tags = [o["tag"] for o in outbounds]
    return {
        "log": {"level": "warn"},
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-remote", "server": "1.1.1.1", "detour": "proxy"},
                {"type": "local", "tag": "dns-local"},
            ],
            "final": "dns-remote",
            "strategy": "ipv4_only",
        },
        "inbounds": [{
            "type": "tun", "tag": "tun-in",
            "address": ["172.19.0.1/30"],
            "auto_route": True, "strict_route": True, "stack": "mixed",
        }],
        "outbounds": [
            {"type": "selector", "tag": "proxy", "outbounds": ["auto", *tags], "default": "auto"},
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": SINGBOX_TEST_URL, "interval": SINGBOX_TEST_INTERVAL, "tolerance": 50},
            *outbounds,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-local",
        },
    }


def save_singbox(tested: list, path: Path = SINGBOX_OUTPUT_FILE) -> None:
    """Write a sing-box profile for the given tested configs. Does nothing if none convert."""
    cfg = build_singbox_config(tested)
    if cfg is None:
        print(f"⚠️ sing-box: no convertible configs, {path} not written")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    n = len(cfg["outbounds"]) - 3          # minus selector, urltest, direct
    print(f"✅ Saved sing-box profile with {n} servers → {path}")


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

        # sing-box profile from the same TCP-tested list
        save_singbox(tested, SINGBOX_OUTPUT_FILE)
    else:
        print("⚠️  No configs passed the reachability test — skipping LeastPing config.")

    # ── NEW, additive: re-test the TCP-alive configs from inside Iran ─────────
    # Separate output again: doesn't touch configs.txt, clash.yaml, or
    # xray_leastping.json above.
    if tested:
        candidate_count = min(len(tested), IRAN_CHECK_MAX_CANDIDATES)
        print(f"\n🇮🇷 Re-testing {candidate_count} TCP-alive configs for reachability from inside Iran (via check-host.net)...")
        iran_ok = asyncio.run(test_iran_reachability(tested))
        print(f"   ✅ {len(iran_ok)}/{candidate_count} configs confirmed reachable from Iran")
        if iran_ok:
            save_iran_working(iran_ok)
            # sing-box profile from the Iran-confirmed list
            save_singbox(iran_ok, SINGBOX_IRAN_OUTPUT_FILE)
        else:
            print(f"⚠️  No configs confirmed reachable from Iran this run — leaving {IRAN_WORKING_OUTPUT_FILE} untouched.")


if __name__ == "__main__":
    main()
