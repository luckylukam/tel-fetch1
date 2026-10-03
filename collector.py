import re
import ssl
import base64
import json
import ipaddress
import uuid as _uuid
import httpx
import asyncio
from html import unescape
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs, unquote, quote

try:                      # optional: pip install pyyaml  (script works without it)
    import yaml
except ImportError:
    yaml = None

# ── Config ────────────────────────────────────────────────────────────────────

CHANNELS = [
    "kurdconfig", "Configir98", "YamYamProxy", "FreeConfigForYou", "begoo_vpn_gp", "iranconnecting",
    "Zed_NetMeli", "on_proxy1", "Spotify_Porteghali", "oxnet_ir", "proxy_station", "bygfw", "ezaccess1",
    "appxa", "v2rayyngvpn", "sparrk_vpn", "amir_webstudio"
]

TELEGRAM_PAGES = 3   # t.me/s/<channel> only shows ~20 posts per page; fetch this many pages back

# ── External subscription URLs ────────────────────────────────────────────────
# Add any v2ray (plain or base64) or Clash (YAML, block or inline style) sub URLs.

EXTERNAL_SUB_URLS: list[str] = [
    "https://raw.githubusercontent.com/patterniha/Free-Configs/main/configs.txt",
    # "https://example.com/v2ray-sub",       # v2ray base64 / plain subscription
    # "https://example.com/clash-sub.yaml",  # Clash YAML subscription
]

PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")
UDP_SCHEMES = ("hysteria2", "hy2", "tuic")   # QUIC/UDP: a TCP connect test is meaningless for these

OUTPUT_FILE       = Path("output/configs.txt")
PLAIN_OUTPUT_FILE = Path("output/configs_plain.txt")
CLASH_OUTPUT_FILE = Path("output/clash.yaml")

# ── Live tests ────────────────────────────────────────────────────────────────

TEST_TIMEOUT_SECONDS  = 5      # per-server TCP connect timeout
TEST_CONCURRENCY      = 60     # parallel TCP tests in flight
TLS_TIMEOUT_SECONDS   = 5      # TLS handshake timeout (TLS/Reality/Trojan configs)
TLS_CONCURRENCY       = 40

# ── Small verified list ───────────────────────────────────────────────────────
# Deduped by server:port, TCP-tested (+ TLS handshake where the config uses TLS).
# Configs that CAN'T be TCP-tested (hysteria2/tuic/ssr/...) are kept at the end,
# checked as far as possible (DNS resolves) and labelled "untested".

SMALL_TESTED_OUTPUT_FILE     = Path("output/configs_tested_small.txt")
SMALL_TESTED_B64_OUTPUT_FILE = Path("output/configs_tested_small_base64.txt")
SMALL_TESTED_MAX             = 50     # cap on fully-tested configs
SMALL_UNTESTED_MAX           = None   # cap on untestable ones; None = keep them all

# ── Tested-only LeastPing balancer config ─────────────────────────────────────
LEASTPING_OUTPUT_FILE      = Path("output/xray_leastping.json")
MAX_BALANCER_SERVERS       = 40
OBSERVATORY_PROBE_INTERVAL = "1s"   # raise to e.g. "5s" if battery/data use is noticeable

# ── Iran-reachability re-test via check-host.net ──────────────────────────────
IRAN_WORKING_OUTPUT_FILE      = Path("output/configs_iran_working.txt")
IRAN_WORKING_B64_OUTPUT_FILE  = Path("output/configs_iran_working_base64.txt")
IRAN_CHECK_MAX_CANDIDATES     = 60
IRAN_CHECK_CONCURRENCY        = 3
IRAN_CHECK_MAX_NODES          = 4
IRAN_CHECK_POLL_INTERVAL      = 2
IRAN_CHECK_POLL_ATTEMPTS      = 6
IRAN_CHECK_MIN_SUCCESS_RATIO  = 0.5

# ── sing-box output settings ──────────────────────────────────────────────────
SINGBOX_OUTPUT_FILE       = Path("output/singbox.json")
SINGBOX_IRAN_OUTPUT_FILE  = Path("output/singbox_iran_working.json")
MAX_SINGBOX_SERVERS       = 40
SINGBOX_TEST_URL          = "https://www.gstatic.com/generate_204"
SINGBOX_TEST_INTERVAL     = "1m"

# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])(?:vmess|vless|trojan|ssr|ss|tuic|hysteria2|hy2)://[^\s<>"\'`]+'
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

# ── Small shared helpers ──────────────────────────────────────────────────────

def _b64d(s: str) -> str:
    """Decode standard OR urlsafe base64, with or without padding."""
    s = s.strip().replace("-", "+").replace("_", "/").rstrip("=")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", errors="ignore")


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _scheme_of(cfg: str) -> str:
    return cfg.split("://", 1)[0].lower()


def _find_uris(text: str) -> list[str]:
    """All proxy URIs found anywhere in text, order-preserving, exact-dupes removed."""
    seen: set[str] = set()
    out: list[str] = []
    for m in CONFIG_PATTERN.findall(text):
        c = m.strip().rstrip(".,;)")
        if c not in seen and any(c.startswith(p) for p in PROTOCOLS):
            seen.add(c)
            out.append(c)
    return out

# ── Telegram fetch (with pagination) ──────────────────────────────────────────

async def fetch_channel(client: httpx.AsyncClient, channel: str) -> list[str]:
    configs: list[str] = []
    before: int | None = None
    try:
        for _ in range(TELEGRAM_PAGES):
            url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
            r = await client.get(url, timeout=20)
            r.raise_for_status()
            configs.extend(_find_uris(unescape(r.text)))
            ids = [int(x) for x in re.findall(r'data-post="[^"/]+/(\d+)"', r.text)]
            if not ids or min(ids) <= 1 or min(ids) == before:
                break
            before = min(ids)
        print(f"  ✔ {channel}: {len(configs)} configs found")
    except Exception as e:
        print(f"  ✘ {channel}: {e}" + (f" (kept {len(configs)} from earlier pages)" if configs else ""))
    return configs

# ── External subscription fetch ───────────────────────────────────────────────

def _is_clash_yaml(text: str) -> bool:
    return bool(re.search(r'^\s*proxies\s*:', text, re.MULTILINE))


def _extract_configs_from_v2ray_sub(text: str) -> list[str]:
    """
    Plain-text list first (b64decode never fails on plain text — it just returns
    garbage — so base64 must be the FALLBACK, not the first attempt), then base64.
    """
    text = text.strip().lstrip("\ufeff")
    found = _find_uris(text)
    if found:
        return found
    compact = re.sub(r"\s+", "", text)      # base64 blobs are often wrapped across lines
    try:
        return _find_uris(_b64d(compact))
    except Exception:
        return []

# ── Clash YAML parsing (PyYAML if installed, otherwise built-in fallback) ─────

def _cast(value: str):
    """Best-effort cast a YAML scalar string to int / bool / str."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth, quote_ch = 0, None
    for ch in s:
        if quote_ch:
            cur.append(ch)
            if ch == quote_ch:
                quote_ch = None
            continue
        if ch in "\"'":
            quote_ch = ch
            cur.append(ch)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _parse_inline_value(v: str):
    v = v.strip()
    if v.startswith("{") and v.endswith("}"):
        return _parse_inline_dict(v)
    if v.startswith("[") and v.endswith("]"):
        return [_parse_inline_value(x) for x in _split_top_level(v[1:-1]) if x.strip()]
    return _cast(v.strip("\"'"))


def _parse_inline_dict(s: str) -> dict:
    """Parse a YAML flow mapping:  {name: x, type: vmess, ws-opts: {path: /}}"""
    d: dict = {}
    for part in _split_top_level(s.strip()[1:-1]):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        d[k.strip().strip("\"'")] = _parse_inline_value(v)
    return d


def _parse_clash_block_fallback(block: str) -> list[dict]:
    """
    Indentation-aware mini parser for the `proxies:` list. Handles block-style
    items, inline `- {a: b}` items, nested dicts (ws-opts, reality-opts, ...),
    and block lists (alpn). Works for any indent style, including list items
    at column 0 (`proxies:\\n- name: x`).
    """
    lines = [l.rstrip() for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    dash_indent = None
    for l in lines:
        if l.lstrip().startswith("-"):
            dash_indent = len(l) - len(l.lstrip())
            break
    if dash_indent is None:
        return []

    proxies: list[dict] = []
    stack: list[tuple[int, dict]] = []                  # (exclusive_indent, dict)
    pending: tuple[dict, str, int] | None = None        # (container, key, key_indent) awaiting children

    def kv(content: str, ind: int) -> None:
        nonlocal pending
        m = re.match(r'^["\']?([\w.\-]+)["\']?\s*:(?:\s+(.*))?$', content)
        if not m or not stack:
            return
        key, val = m.group(1), (m.group(2) or "").strip()
        target = stack[-1][1]
        if val == "":
            target[key] = None
            pending = (target, key, ind)
        else:
            target[key] = _parse_inline_value(val)
            pending = None

    for l in lines:
        ind = len(l) - len(l.lstrip())
        s = l.strip()
        if s.startswith("-") and (len(s) == 1 or s[1] in " \t"):
            rest = s[1:].strip()
            if ind <= dash_indent:                       # new proxy item
                pending = None
                if rest.startswith("{"):
                    proxies.append(_parse_inline_dict(rest))
                    stack = []
                    continue
                cur: dict = {}
                proxies.append(cur)
                content_indent = ind + (len(s) - len(s[1:].lstrip()))
                stack = [(content_indent - 1, cur)]
                if rest:
                    kv(rest, content_indent)
            elif pending:                                # list item under a key
                cont, key, _ = pending
                if not isinstance(cont.get(key), list):
                    cont[key] = []
                cont[key].append(_parse_inline_value(rest))
            continue
        if not stack:
            continue
        while len(stack) > 1 and stack[-1][0] >= ind:
            stack.pop()
        if pending and ind > pending[2] and pending[0].get(pending[1]) is None:
            child: dict = {}
            pending[0][pending[1]] = child
            stack.append((pending[2], child))
        kv(s, ind)

    return proxies


def _parse_clash_yaml_proxies(text: str) -> list[dict]:
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return [p for p in data["proxies"] if isinstance(p, dict)]
        except Exception:
            pass
    m = re.search(r'^proxies\s*:[ \t]*(?:#.*)?\n(.*?)(?=^[^\s#-]|\Z)', text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return _parse_clash_block_fallback(m.group(1))

# ── URI building helpers (Clash -> URI) ───────────────────────────────────────

def _qs(pairs) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs if v not in (None, ""))


def _hp(host: str, port) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


def _transport_pairs(proxy: dict, host: str) -> list[tuple[str, str]]:
    net = str(proxy.get("network", "tcp"))
    pairs: list[tuple[str, str]] = [("type", net)]
    if net == "ws":
        ws = proxy.get("ws-opts") or {}
        pairs += [("path", ws.get("path", "/")), ("host", (ws.get("headers") or {}).get("Host", host))]
    elif net == "grpc":
        pairs.append(("serviceName", (proxy.get("grpc-opts") or {}).get("grpc-service-name", "")))
    elif net in ("xhttp", "httpupgrade"):
        o = proxy.get(f"{net}-opts") or {}
        pairs += [("path", o.get("path", "/")), ("host", o.get("host", host))]
    return pairs


def _clash_proxy_to_uri(proxy: dict) -> str | None:
    """Convert one Clash proxy dict back to a share URI. None if unsupported."""
    ptype = str(proxy.get("type", "")).lower()
    name  = str(proxy.get("name", "proxy"))
    host  = str(proxy.get("server", ""))
    port  = proxy.get("port", 443)
    if not host:
        return None
    frag = "#" + quote(name, safe="")

    try:
        if ptype == "vmess":
            raw = {
                "v": "2", "ps": name, "add": host, "port": str(port),
                "id": str(proxy.get("uuid", "")), "aid": str(proxy.get("alterId", 0)),
                "scy": str(proxy.get("cipher", "auto")), "net": "tcp", "type": "none",
                "tls": "tls" if proxy.get("tls") else "", "sni": str(proxy.get("servername", "")),
                "fp": str(proxy.get("client-fingerprint", "")),
            }
            net = str(proxy.get("network", "tcp"))
            if net == "ws":
                ws = proxy.get("ws-opts") or {}
                raw.update(net="ws", path=str(ws.get("path", "/")),
                           host=str((ws.get("headers") or {}).get("Host", host)))
            elif net == "grpc":
                raw.update(net="grpc", path=str((proxy.get("grpc-opts") or {}).get("grpc-service-name", "")))
            elif net in ("xhttp", "httpupgrade"):
                o = proxy.get(f"{net}-opts") or {}
                raw.update(net=net, path=str(o.get("path", "/")), host=str(o.get("host", host)))
            return "vmess://" + base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()

        if ptype == "vless":
            reality = proxy.get("reality-opts") or {}
            pairs: list[tuple[str, str]] = [("encryption", "none")]
            if reality:
                pairs += [("security", "reality"), ("pbk", reality.get("public-key", "")),
                          ("sid", reality.get("short-id", ""))]
            elif proxy.get("tls"):
                pairs.append(("security", "tls"))
            alpn = proxy.get("alpn")
            pairs += [("sni", proxy.get("servername")), ("fp", proxy.get("client-fingerprint")),
                      ("flow", proxy.get("flow")),
                      ("alpn", ",".join(alpn) if isinstance(alpn, list) else alpn)]
            if proxy.get("skip-cert-verify"):
                pairs.append(("allowInsecure", "1"))
            pairs += _transport_pairs(proxy, host)
            return f"vless://{quote(str(proxy.get('uuid', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"

        if ptype == "trojan":
            pairs = [("sni", proxy.get("sni") or proxy.get("servername"))]
            if proxy.get("skip-cert-verify"):
                pairs.append(("allowInsecure", "1"))
            pairs += _transport_pairs(proxy, "")
            return f"trojan://{quote(str(proxy.get('password', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"

        if ptype == "ss":
            if proxy.get("plugin"):
                return None
            method   = str(proxy.get("cipher", "aes-256-gcm"))
            password = str(proxy.get("password", ""))
            userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
            return f"ss://{userinfo}@{_hp(host, port)}{frag}"

        if ptype in ("hysteria2", "hy2"):
            pairs = [("sni", proxy.get("sni")), ("obfs", proxy.get("obfs")),
                     ("obfs-password", proxy.get("obfs-password"))]
            if proxy.get("skip-cert-verify"):
                pairs.append(("insecure", "1"))
            return f"hysteria2://{quote(str(proxy.get('password', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"
    except Exception:
        pass
    return None


def _extract_configs_from_clash_sub(text: str) -> list[str]:
    configs: list[str] = []
    for proxy in _parse_clash_yaml_proxies(text):
        uri = _clash_proxy_to_uri(proxy)
        if uri:
            configs.append(uri)
    return configs


async def fetch_external_sub(client: httpx.AsyncClient, url: str) -> list[str]:
    """Fetch one external subscription URL (plain list, base64, or Clash YAML)."""
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        text = r.text.strip()

        if _is_clash_yaml(text):
            configs = _extract_configs_from_clash_sub(text) or _extract_configs_from_v2ray_sub(text)
            kind = "Clash sub"
        else:
            configs = _extract_configs_from_v2ray_sub(text) or _extract_configs_from_clash_sub(text)
            kind = "V2Ray sub"

        if configs:
            print(f"  ✔ [{kind}] {url}: {len(configs)} configs found")
        else:
            preview = text[:120].replace("\n", " ")
            print(f"  ⚠ [{kind}] {url}: 0 configs. Response starts with: {preview!r}")
        return configs
    except Exception as e:
        print(f"  ✘ [External sub] {url}: {e}")
        return []

# ── URI parsing (-> Clash-style dicts) ────────────────────────────────────────

def _decode_vmess(uri: str) -> dict | None:
    try:
        data = json.loads(_b64d(uri[len("vmess://"):]))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_ss(uri: str) -> dict | None:
    """Parse ss:// in SIP002 (base64 userinfo or plain) and legacy (whole-body base64) forms."""
    try:
        body  = uri.split("#", 1)[0][len("ss://"):]
        query = ""
        if "?" in body:
            body, query = body.split("?", 1)
        body = unquote(body).rstrip("/")
        if "@" in body:
            userinfo, hostport = body.rsplit("@", 1)
            if ":" not in userinfo:
                userinfo = _b64d(userinfo)
        else:
            userinfo, hostport = _b64d(body).rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        host, port_str   = hostport.rsplit(":", 1)
        return {"method": method, "password": password, "host": host.strip("[]"),
                "port": int(port_str), "plugin": "plugin=" in query}
    except Exception:
        return None


def _uri_parts(uri: str):
    """urlsplit-based parse for vless/trojan/hysteria2/tuic. -> (user, host, port, params) or None.
    Query values are percent-decoded (so path=%2Fws becomes /ws) but '+' is kept as-is."""
    try:
        u    = urlsplit(uri.split("#", 1)[0])
        host = u.hostname or ""
        try:
            port = u.port or 443
        except ValueError:                        # e.g. hysteria2 port-hopping "443,5000-6000"
            m    = re.search(r':(\d+)', u.netloc.rsplit("@", 1)[-1])
            port = int(m.group(1)) if m else 443
        user = unquote(u.username or "")
        if u.password is not None:
            user += ":" + unquote(u.password)
        params: dict[str, str] = {}
        for p in u.query.split("&"):
            k, _, v = p.partition("=")
            if k:
                params[k] = unquote(v)
        return user, host, port, params
    except Exception:
        return None


def _apply_transport(proxy: dict, net: str, params: dict, default_host: str) -> None:
    """Fill network/ws-opts/grpc-opts/... on a Clash proxy dict."""
    if net == "ws":
        proxy["network"] = "ws"
        opts: dict = {"path": params.get("path") or "/"}
        h = params.get("host") or default_host
        if h:
            opts["headers"] = {"Host": h}
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["network"]   = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
    elif net in ("xhttp", "httpupgrade"):
        proxy["network"]       = net
        proxy[f"{net}-opts"]   = {"path": params.get("path") or "/",
                                  "host": params.get("host") or default_host}


def _config_to_clash_proxy(cfg: str, name: str) -> dict | None:
    if cfg.startswith("vmess://"):
        raw = _decode_vmess(cfg.split("#")[0])
        if not raw:
            return None
        server = str(raw.get("add", "")).strip()
        port   = _int(raw.get("port"), 0)
        if not server or not 1 <= port <= 65535:
            return None
        proxy: dict = {
            "name": name, "type": "vmess", "server": server, "port": port,
            "uuid": str(raw.get("id", "")), "alterId": _int(raw.get("aid"), 0),
            "cipher": str(raw.get("scy") or raw.get("security") or "auto"), "udp": True,
        }
        path = str(raw.get("path", "") or "")
        _apply_transport(proxy, str(raw.get("net", "tcp") or "tcp"),
                         {"path": path, "host": str(raw.get("host", "") or ""), "serviceName": path}, server)
        if str(raw.get("tls", "")) == "tls":
            proxy["tls"] = True
            sni = str(raw.get("sni") or raw.get("host") or "")
            if sni:
                proxy["servername"] = sni
            if raw.get("fp"):
                proxy["client-fingerprint"] = str(raw["fp"])
        return proxy

    if cfg.startswith("vless://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        uuid, host, port, params = parts
        proxy = {"name": name, "type": "vless", "server": host, "port": port, "uuid": uuid, "udp": True}
        security = params.get("security", "")
        sni = params.get("sni") or params.get("peer")
        if security in ("tls", "reality"):
            proxy["tls"] = True
            if sni:
                proxy["servername"] = sni
        if security == "reality":
            proxy["reality-opts"] = {"public-key": params.get("pbk", ""), "short-id": params.get("sid", "")}
        if params.get("fp"):
            proxy["client-fingerprint"] = params["fp"]
        if params.get("alpn"):
            proxy["alpn"] = [a for a in params["alpn"].split(",") if a]
        if params.get("flow"):
            proxy["flow"] = params["flow"]
        if params.get("allowInsecure") in ("1", "true") or params.get("insecure") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        _apply_transport(proxy, params.get("type", "tcp"), params, host)
        return proxy

    if cfg.startswith("trojan://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        password, host, port, params = parts
        proxy = {"name": name, "type": "trojan", "server": host, "port": port, "password": password, "udp": True}
        sni = params.get("sni") or params.get("peer")
        if sni:
            proxy["sni"] = sni
        if params.get("alpn"):
            proxy["alpn"] = [a for a in params["alpn"].split(",") if a]
        if params.get("fp"):
            proxy["client-fingerprint"] = params["fp"]
        if params.get("allowInsecure") in ("1", "true") or params.get("insecure") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        _apply_transport(proxy, params.get("type", "tcp"), params, "")
        return proxy

    if cfg.startswith("ss://"):
        ss = _parse_ss(cfg)
        if not ss or ss["plugin"]:          # plugin-based ss can't be expressed here
            return None
        return {"name": name, "type": "ss", "server": ss["host"], "port": ss["port"],
                "cipher": ss["method"], "password": ss["password"], "udp": True}

    if cfg.startswith("hysteria2://") or cfg.startswith("hy2://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        password, host, port, params = parts
        proxy = {"name": name, "type": "hysteria2", "server": host, "port": port, "password": password, "udp": True}
        if params.get("sni"):
            proxy["sni"] = params["sni"]
        if params.get("insecure", "0") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        if params.get("obfs"):
            proxy["obfs"] = params["obfs"]
            proxy["obfs-password"] = params.get("obfs-password", "")
        return proxy

    return None


def config_to_clash_proxy(cfg: str, name: str) -> dict | None:
    """Safe wrapper: a malformed config returns None instead of crashing the run."""
    try:
        return _config_to_clash_proxy(cfg, name)
    except Exception:
        return None


def _endpoint_of(cfg: str) -> tuple[str, int] | None:
    """host/port for ANY supported URI, including ones the Clash parser can't represent."""
    scheme = _scheme_of(cfg)
    base   = cfg.split("#", 1)[0]
    try:
        if scheme == "vmess":
            raw = _decode_vmess(base)
            return (str(raw["add"]), int(raw["port"])) if raw else None
        if scheme == "ss":
            ss = _parse_ss(base)
            return (ss["host"], ss["port"]) if ss else None
        if scheme == "ssr":
            main  = _b64d(base[len("ssr://"):]).split("/?")[0]
            parts = main.split(":")                 # host:port:protocol:method:obfs:pass
            return ":".join(parts[:-5]), int(parts[-5])
        parts = _uri_parts(base)
        if parts and parts[1]:
            return parts[1], parts[2]
    except Exception:
        pass
    return None

# ── Full dedup ────────────────────────────────────────────────────────────────

def _dedup_key(cfg: str) -> str:
    """
    Canonical identity of a config, ignoring remark/label and cosmetic differences:
      - vmess: the remark lives INSIDE the base64 JSON ("ps"), so strings differ
        even for the same server — compare the decoded JSON minus "ps".
      - ss: normalises the base64-vs-plain userinfo forms.
      - others: scheme + credentials + host + port + query params (order-insensitive).
    """
    base = cfg.split("#", 1)[0].rstrip("?")
    try:
        scheme = _scheme_of(base)
        if scheme == "vmess":
            raw = _decode_vmess(base)
            if raw:
                return "vmess|" + json.dumps({k: str(v) for k, v in raw.items() if k != "ps"}, sort_keys=True)
        elif scheme == "ss":
            ss = _parse_ss(base)
            if ss:
                return f"ss|{ss['method']}|{ss['password']}|{ss['host'].lower()}|{ss['port']}|{ss['plugin']}"
        elif scheme != "ssr":
            parts = _uri_parts(base)
            if parts and parts[1]:
                user, host, port, params = parts
                return f"{scheme}|{user}|{host.lower()}|{port}|{sorted(params.items())}"
    except Exception:
        pass
    return base

# ── Collect ───────────────────────────────────────────────────────────────────

async def collect_all() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tg_results  = await asyncio.gather(*(fetch_channel(client, ch) for ch in CHANNELS))
        sub_results = await asyncio.gather(*(fetch_external_sub(client, u) for u in EXTERNAL_SUB_URLS))

    seen: set[str] = set()
    all_configs: list[str] = []
    total = 0
    for batch in (*tg_results, *sub_results):
        for cfg in batch:
            total += 1
            key = _dedup_key(cfg)
            if key not in seen:
                seen.add(key)
                all_configs.append(cfg)
    print(f"   Collected {total} configs, {len(all_configs)} unique after full dedup")
    return all_configs

# ── Rename remarks ────────────────────────────────────────────────────────────

def _with_remark(cfg: str, remark: str) -> str:
    """Replace a config's remark. For vmess the remark is the JSON "ps" field, so set that too."""
    base = cfg.split("#", 1)[0].rstrip("?")
    if base.startswith("vmess://"):
        raw = _decode_vmess(base)
        if raw:
            raw["ps"] = remark
            base = "vmess://" + base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()
    return f"{base}#{remark}"


def rename_remarks(configs: list[str]) -> list[str]:
    return [_with_remark(cfg, f"mn_conf{i}") for i, cfg in enumerate(configs, start=1)]

# ── Clash YAML output ─────────────────────────────────────────────────────────

def _yaml_emit(lines: list[str], key: str, value, indent: int) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return
        lines.append(f"{pad}{key}:")
        for k, v in value.items():
            _yaml_emit(lines, k, v, indent + 2)
    elif isinstance(value, bool):
        lines.append(f"{pad}{key}: {str(value).lower()}")
    elif isinstance(value, (int, float)):
        lines.append(f"{pad}{key}: {value}")
    elif isinstance(value, list):
        lines.append(f"{pad}{key}: {json.dumps(value, ensure_ascii=False)}")
    else:
        lines.append(f"{pad}{key}: {json.dumps(str(value), ensure_ascii=False)}")


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

    lines: list[str] = [
        "# Clash subscription — auto-generated",
        f"# Generated: {utc_now()}",
        f"# Total proxies: {len(proxies)}",
        "",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "",
        "proxies:",
    ]
    for p in proxies:
        lines.append(f"  - name: {json.dumps(p['name'], ensure_ascii=False)}")
        for k, v in p.items():
            if k != "name":
                _yaml_emit(lines, k, v, 4)
        lines.append("")

    lines += ["proxy-groups:", '  - name: "AUTO"', "    type: url-test",
              "    url: http://www.gstatic.com/generate_204", "    interval: 300", "    proxies:"]
    lines += [f"      - {json.dumps(n)}" for n in proxy_names]
    lines += ["", '  - name: "PROXY"', "    type: select", "    proxies:", '      - "AUTO"']
    lines += [f"      - {json.dumps(n)}" for n in proxy_names]
    lines += ["", "rules:", "  - MATCH,AUTO", ""]
    return "\n".join(lines)

# ── Save main outputs ─────────────────────────────────────────────────────────

def save(configs: list[str]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    configs = rename_remarks(configs)
    raw     = "\n".join(configs)
    encoded = base64.b64encode(raw.encode()).decode()
    OUTPUT_FILE.write_text(encoded)
    PLAIN_OUTPUT_FILE.write_text(raw)
    print(f"\n✅ Saved {len(configs)} unique configs → {OUTPUT_FILE}")
    print(f"   Base64 length: {len(encoded)} chars")

    clash_yaml  = build_clash_yaml(configs)
    CLASH_OUTPUT_FILE.write_text(clash_yaml, encoding="utf-8")
    clash_count = clash_yaml.count("\n  - name:")
    print(f"✅ Saved Clash subscription → {CLASH_OUTPUT_FILE} ({clash_count} proxies)")

# ── Live testing ──────────────────────────────────────────────────────────────

async def _tcp_ping(host: str, port: int, timeout: float = TEST_TIMEOUT_SECONDS) -> float | None:
    """Raw TCP connect. Returns latency in ms, or None if dead."""
    loop  = asyncio.get_running_loop()
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


async def _tls_handshake(host: str, port: int, sni: str) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni or None),
            timeout=TLS_TIMEOUT_SECONDS,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _resolves(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True                                   # literal IP, nothing to resolve
    except ValueError:
        pass
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), timeout=5)
        return True
    except Exception:
        return False


async def test_configs(configs: list[str]):
    """
    Returns (alive, untestable):
      alive      — [(uri, clash_proxy_dict, latency_ms)] that answered a TCP connect, fastest first
      untestable — [(uri, host, port)] configs a TCP connect can't judge (hysteria2/tuic are
                   UDP/QUIC; ssr / unparseable ones) — kept, NOT discarded
    TCP-testable configs that fail the connect are dead and dropped.
    """
    sem = asyncio.Semaphore(TEST_CONCURRENCY)
    alive: list[tuple[str, dict, float]] = []
    untestable: list[tuple[str, str, int]] = []

    async def _check(cfg: str) -> None:
        scheme = _scheme_of(cfg)
        proxy  = None if (scheme in UDP_SCHEMES or scheme == "ssr") else config_to_clash_proxy(cfg, "test")
        if proxy and proxy.get("server") and proxy.get("port"):
            async with sem:
                latency = await _tcp_ping(str(proxy["server"]), int(proxy["port"]))
            if latency is not None:
                alive.append((cfg, proxy, latency))
            return
        ep = _endpoint_of(cfg)
        if ep:
            untestable.append((cfg, ep[0], ep[1]))

    await asyncio.gather(*(_check(c) for c in configs))
    alive.sort(key=lambda r: r[2])
    return alive, untestable


async def verify_and_dedupe(alive: list[tuple[str, dict, float]]) -> list[tuple[str, dict, float]]:
    """
    Confirm a TLS handshake for every config that uses TLS/Reality/Trojan (a TCP
    connect alone doesn't prove the TLS layer works; non-TLS configs pass on the
    TCP result), THEN keep only the fastest survivor per server:port. Verifying
    before deduping means a failed TLS config can't knock out a working config
    that shares its endpoint. Identical (host, port, sni) handshakes are cached.
    """
    sem = asyncio.Semaphore(TLS_CONCURRENCY)
    cache: dict[tuple[str, int, str], asyncio.Task] = {}

    async def _handshake(host: str, port: int, sni: str) -> bool:
        async with sem:
            return await _tls_handshake(host, port, sni)

    async def _verify(item):
        proxy = item[1]
        if not (proxy.get("tls") or proxy.get("type") == "trojan"):
            return item
        key = (str(proxy["server"]), int(proxy["port"]), str(proxy.get("servername") or proxy.get("sni") or ""))
        if key not in cache:
            cache[key] = asyncio.ensure_future(_handshake(*key))
        return item if await cache[key] else None

    passed = [r for r in await asyncio.gather(*(_verify(i) for i in alive)) if r]
    passed.sort(key=lambda r: r[2])                     # fastest first

    seen: set[tuple[str, int]] = set()
    verified: list[tuple[str, dict, float]] = []
    for item in passed:
        key = (str(item[1]["server"]).lower(), int(item[1]["port"]))
        if key not in seen:
            seen.add(key)
            verified.append(item)
    print(f"   {len(alive)} TCP-alive → {len(passed)} after TLS check → {len(verified)} unique servers")
    return verified


async def check_untestable(items: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    """Dedupe untestable configs by host:port and drop only those whose hostname doesn't resolve."""
    seen: set[tuple[str, int, bool]] = set()
    unique: list[tuple[str, str, int]] = []
    for cfg, host, port in items:
        key = (host.lower(), port, _scheme_of(cfg) in UDP_SCHEMES)
        if key not in seen:
            seen.add(key)
            unique.append((cfg, host, port))

    sem = asyncio.Semaphore(TEST_CONCURRENCY)

    async def _r(item):
        async with sem:
            return item if await _resolves(item[1]) else None

    kept = [r for r in await asyncio.gather(*(_r(i) for i in unique)) if r]
    print(f"   {len(items)} untestable → {len(unique)} unique → {len(kept)} with resolvable host (kept, marked untested)")
    return kept


async def run_local_tests(configs: list[str]):
    alive, untestable = await test_configs(configs)
    print(f"   ✅ {len(alive)}/{len(configs)} answered TCP; {len(untestable)} can't be TCP-tested (UDP/other)")
    verified = await verify_and_dedupe(alive)
    untested = await check_untestable(untestable)
    return verified, untested


def save_small_list(verified: list[tuple[str, dict, float]], untested: list[tuple[str, str, int]]) -> None:
    lines: list[str] = []
    for i, (cfg, _p, lat) in enumerate(verified[:SMALL_TESTED_MAX], start=1):
        lines.append(_with_remark(cfg, f"ok{i}_{int(lat)}ms"))
    n_ok = len(lines)
    pool = untested if SMALL_UNTESTED_MAX is None else untested[:SMALL_UNTESTED_MAX]
    for j, (cfg, _h, _p) in enumerate(pool, start=1):
        lines.append(_with_remark(cfg, f"untested{j}_{_scheme_of(cfg)}"))
    if not lines:
        print("⚠️  Nothing for the small verified list — leaving it untouched.")
        return
    raw = "\n".join(lines)
    SMALL_TESTED_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SMALL_TESTED_OUTPUT_FILE.write_text(raw)
    SMALL_TESTED_B64_OUTPUT_FILE.write_text(base64.b64encode(raw.encode()).decode())
    print(f"✅ Saved small list → {SMALL_TESTED_OUTPUT_FILE}: {n_ok} tested + {len(lines) - n_ok} untested (+ base64 version)")

# ── Iran-reachability re-test via check-host.net ──────────────────────────────

CHECK_HOST_API = "https://check-host.net"


async def _fetch_iran_nodes(client: httpx.AsyncClient) -> list[str]:
    """Live list of check-host.net nodes located in Iran."""
    try:
        r = await client.get(f"{CHECK_HOST_API}/nodes/hosts", headers={"Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        data  = r.json()
        nodes = data.get("nodes", {}) if isinstance(data, dict) else {}
        out = []
        for name, info in nodes.items():
            if not isinstance(info, dict):
                continue
            location = info.get("location") or []
            if (str(location[0]).lower() if location else "") == "ir":
                out.append(name)
        return out
    except Exception as e:
        print(f"  ⚠️  Could not fetch check-host.net node list: {e}")
        return []


async def _check_host_submit(client: httpx.AsyncClient, host: str, port: int, nodes: list[str]) -> str | None:
    params = [("host", f"{host}:{port}")] + [("node", n) for n in nodes[:IRAN_CHECK_MAX_NODES]]
    for attempt in range(2):                      # one retry (rate limits / transient errors)
        try:
            r = await client.get(f"{CHECK_HOST_API}/check-tcp", params=params,
                                 headers={"Accept": "application/json"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data.get("request_id")
        except Exception:
            pass
        if attempt == 0:
            await asyncio.sleep(3)
    return None


async def _check_host_poll(client: httpx.AsyncClient, request_id: str) -> dict:
    last: dict = {}
    for _ in range(IRAN_CHECK_POLL_ATTEMPTS):
        await asyncio.sleep(IRAN_CHECK_POLL_INTERVAL)
        try:
            r = await client.get(f"{CHECK_HOST_API}/check-result/{request_id}",
                                 headers={"Accept": "application/json"}, timeout=15)
            r.raise_for_status()
            last = r.json() or {}
            if all(v is not None for v in last.values()):
                break
        except Exception:
            continue
    return last


def _check_host_success_ratio(result: dict) -> float:
    total = ok = 0
    for _node, entries in result.items():
        if not entries:
            continue                              # pending / never answered: excluded, not a failure
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            total += 1
            if "error" not in entry:
                ok += 1
    return (ok / total) if total else 0.0


async def test_iran_reachability(tested: list[tuple[str, dict, float]]) -> list[tuple[str, dict, float]]:
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
            _cfg, proxy, _lat = item
            host, port = str(proxy.get("server", "")), proxy.get("port")
            if not host or not port:
                return
            async with sem:
                request_id = await _check_host_submit(client, host, int(port), iran_nodes)
                if not request_id:
                    return
                result = await _check_host_poll(client, request_id)
            if _check_host_success_ratio(result) >= IRAN_CHECK_MIN_SUCCESS_RATIO:
                passed.append(item)

        await asyncio.gather(*(_check(c) for c in candidates))

    passed.sort(key=lambda r: r[2])
    return passed


def save_iran_working(iran_ok: list[tuple[str, dict, float]]) -> None:
    configs = [cfg for cfg, _p, _l in iran_ok]
    IRAN_WORKING_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = "\n".join(configs)
    IRAN_WORKING_OUTPUT_FILE.write_text(raw)
    IRAN_WORKING_B64_OUTPUT_FILE.write_text(base64.b64encode(raw.encode()).decode())
    print(f"✅ Saved {len(configs)} Iran-confirmed-working configs → {IRAN_WORKING_OUTPUT_FILE}")
    print(f"✅ Saved base64 subscription → {IRAN_WORKING_B64_OUTPUT_FILE}")

# ── Xray "leastPing" balancer config ──────────────────────────────────────────

_XRAY_VMESS_SECURITY = {"auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"}


def clash_proxy_to_xray_outbound(proxy: dict, tag: str) -> dict | None:
    """Clash-style proxy dict -> raw Xray outbound (vmess / vless / trojan / shadowsocks)."""
    ptype   = str(proxy.get("type", "")).lower()
    host    = str(proxy.get("server", ""))
    port    = int(proxy.get("port", 443))
    network = str(proxy.get("network", "tcp"))

    stream: dict = {"network": network if network in ("tcp", "ws", "grpc", "xhttp", "httpupgrade") else "tcp"}
    if network == "ws":
        ws = proxy.get("ws-opts") or {}
        stream["wsSettings"] = {"path": ws.get("path", "/"), "headers": ws.get("headers") or {}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": (proxy.get("grpc-opts") or {}).get("grpc-service-name", "")}
    elif network == "xhttp":
        xh = proxy.get("xhttp-opts") or {}
        stream["xhttpSettings"] = {"path": xh.get("path", "/"), "host": xh.get("host", "")}
    elif network == "httpupgrade":
        hu = proxy.get("httpupgrade-opts") or {}
        stream["httpupgradeSettings"] = {"path": hu.get("path", "/"), "host": hu.get("host", "")}

    fingerprint = proxy.get("client-fingerprint")
    reality     = proxy.get("reality-opts")
    if reality:
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName":  proxy.get("servername", ""),
            "publicKey":   reality.get("public-key", ""),
            "shortId":     reality.get("short-id", ""),
            "fingerprint": fingerprint or "chrome",
        }
    elif proxy.get("tls") or ptype == "trojan":      # trojan is TLS by design
        stream["security"] = "tls"
        tls: dict = {
            "serverName":    proxy.get("servername") or proxy.get("sni") or host,
            "allowInsecure": bool(proxy.get("skip-cert-verify", False)),
        }
        if proxy.get("alpn"):
            tls["alpn"] = proxy["alpn"]
        if fingerprint:
            tls["fingerprint"] = fingerprint
        stream["tlsSettings"] = tls
    else:
        stream["security"] = "none"

    try:
        if ptype == "vmess":
            cipher = str(proxy.get("cipher", "auto"))
            return {
                "tag": tag, "protocol": "vmess",
                "settings": {"vnext": [{"address": host, "port": port, "users": [{
                    "id": str(proxy.get("uuid", "")), "alterId": int(proxy.get("alterId", 0)),
                    "security": cipher if cipher in _XRAY_VMESS_SECURITY else "auto",
                }]}]},
                "streamSettings": stream,
            }
        if ptype == "vless":
            user = {"id": str(proxy.get("uuid", "")), "encryption": "none"}
            if proxy.get("flow"):                     # without this, Reality+Vision servers reject the connection
                user["flow"] = str(proxy["flow"])
            return {
                "tag": tag, "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": stream,
            }
        if ptype == "trojan":
            return {
                "tag": tag, "protocol": "trojan",
                "settings": {"servers": [{"address": host, "port": port,
                                          "password": str(proxy.get("password", ""))}]},
                "streamSettings": stream,
            }
        if ptype == "ss":
            return {
                "tag": tag, "protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port,
                                          "method": str(proxy.get("cipher", "aes-256-gcm")),
                                          "password": str(proxy.get("password", ""))}]},
                "streamSettings": {"network": "tcp", "security": "none"},
            }
    except Exception:
        return None
    return None


def build_xray_leastping_config(tested: list[tuple[str, dict, float]]) -> dict:
    outbounds: list[dict] = []
    for _cfg, proxy, _lat in tested:
        if len(outbounds) >= MAX_BALANCER_SERVERS:
            break
        ob = clash_proxy_to_xray_outbound(proxy, tag=f"p{len(outbounds) + 1}")
        if ob:
            outbounds.append(ob)

    proxy_tags = [ob["tag"] for ob in outbounds]
    outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    balancer: dict = {"tag": "auto", "selector": ["p"], "strategy": {"type": "leastPing"}}
    if proxy_tags:
        balancer["fallbackTag"] = proxy_tags[0]       # defined behaviour before the first probe finishes

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
             "settings": {"auth": "noauth", "udp": True},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http"},
        ],
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [balancer],
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "auto"}],
        },
        "observatory": {
            "subjectSelector":   ["p"],
            "probeURL":          "https://www.gstatic.com/generate_204",
            "probeInterval":     OBSERVATORY_PROBE_INTERVAL,
            "enableConcurrency": True,
        },
    }

# ── sing-box output ───────────────────────────────────────────────────────────
# Parses the ORIGINAL URIs, so it keeps fields the Clash dict drops (VLESS flow,
# uTLS fingerprint, alpn, ws early-data, hysteria2 obfs, ...).

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
            return False, None
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
        if hostport.startswith("["):
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
            ss = _parse_ss(base)
            if not ss or ss["plugin"] or ss["method"] not in _SB_SS_METHODS:
                return None
            if not 1 <= ss["port"] <= 65535:
                return None
            return {"type": "shadowsocks", "tag": tag, "server": ss["host"], "server_port": ss["port"],
                    "method": ss["method"], "password": ss["password"]}

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
                if flow not in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
                    return None
                ob["flow"] = "xtls-rprx-vision"
            tls = _sb_tls(params, server)
            if _q(params, "security") in ("tls", "reality") and not tls:
                return None
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if scheme == "trojan://":
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            tls = _sb_tls(params, server, force=True)
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
    print(f"🔍 Collecting V2Ray configs [{utc_now()}]")
    print(f"   Channels      : {len(CHANNELS)}")
    print(f"   External subs : {len(EXTERNAL_SUB_URLS)}")
    print(f"   Protocols     : {', '.join(p.removesuffix('://') for p in PROTOCOLS)}")
    print(f"   Clash parser  : {'PyYAML' if yaml else 'built-in fallback'}\n")

    configs = asyncio.run(collect_all())

    if not configs:
        print("⚠️  No configs found.")
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text("")
        CLASH_OUTPUT_FILE.write_text("proxies: []\n")
        return

    save(configs)

    # Live tests: TCP connect (+ TLS handshake for TLS configs), deduped by server:port.
    # Configs that can't be TCP-tested are kept separately, not discarded.
    print(f"\n🧪 Testing {len(configs)} configs (TCP timeout {TEST_TIMEOUT_SECONDS}s)...")
    verified, untested = asyncio.run(run_local_tests(configs))

    save_small_list(verified, untested)

    if verified:
        leastping_cfg = build_xray_leastping_config(verified)
        LEASTPING_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEASTPING_OUTPUT_FILE.write_text(json.dumps(leastping_cfg, indent=2))
        included = len([o for o in leastping_cfg["outbounds"] if o["tag"].startswith("p")])
        print(f"✅ Saved tested LeastPing config → {LEASTPING_OUTPUT_FILE} ({included} servers, auto-switching)")
        save_singbox(verified, SINGBOX_OUTPUT_FILE)
    else:
        print("⚠️  No configs passed the reachability test — skipping LeastPing / sing-box configs.")

    # Re-test the verified configs from inside Iran (check-host.net).
    if verified:
        candidate_count = min(len(verified), IRAN_CHECK_MAX_CANDIDATES)
        print(f"\n🇮🇷 Re-testing {candidate_count} verified configs for reachability from inside Iran (via check-host.net)...")
        iran_ok = asyncio.run(test_iran_reachability(verified))
        print(f"   ✅ {len(iran_ok)}/{candidate_count} configs confirmed reachable from Iran")
        if iran_ok:
            save_iran_working(iran_ok)
            save_singbox(iran_ok, SINGBOX_IRAN_OUTPUT_FILE)
        else:
            print(f"⚠️  No configs confirmed reachable from Iran this run — leaving {IRAN_WORKING_OUTPUT_FILE} untouched.")


if __name__ == "__main__":
    main()
