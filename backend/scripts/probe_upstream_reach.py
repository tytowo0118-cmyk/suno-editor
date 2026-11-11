"""上游可达性分层探针：把「无法连接 Suno 代理：ConnectError」落到 DNS / TCP / TLS / HTTP 哪一层。

背景：提交是扣费操作，`api/generate.py` 只对**请求还没离开本机**的失败重试
（`PRE_SEND_ERRORS = ConnectTimeout / ConnectError / PoolTimeout`，退避 1s、3s）。
所以看到 `无法连接 Suno 代理：ConnectError（尝试 3 次 / 19.1s）` 时，真正要回答的是
「是哪一层断的、现在通不通、这次有没有花钱」，而不是直接再点一次生成。

本机链路不是直连：macOS 上系统代理 127.0.0.1:7890 + TUN 网关 198.18.0.1，
容器里的 DNS 也拿到 fake-IP（`key.34ku.com → 198.18.1.49`）。于是 TCP 连一定秒成功
（fake-IP 由 TUN 驱动本地接受），真正决定成败的是代理节点向源站拨号那一段。
**代理的 dial 超时（实测约 5 s）比 httpx 的 connect 超时（10 s）先到**，
所以失败会表现为「ConnectError after 5.0x s」而不是「ConnectTimeout after 10 s」——
这个区分就是本脚本要给的判据。

用法（容器里跑，与后端同一网络栈；全程零成本，不打提交端点、不带 key）：

    docker exec -i museblocks-backend python /app/scripts/probe_upstream_reach.py
    docker exec -i -e PROBE_ROUNDS=20 museblocks-backend python /app/scripts/probe_upstream_reach.py --http

`--http` 只对 base 根路径发一次**无凭据 GET**（不是 `/suno/submit/music`，不会建单不会扣费），
用来确认 HTTP 层也活着。

2026-11-11 实测：容器内 15/15 轮 DNS 5–24 ms、TCP 1–9 ms、TLS 握手 1.28–1.52 s 全成功；
宿主机走同一代理但拿到的是 TLSv1.2 CHACHA20（代理中继），握手 1.79–2.82 s。
02:25 那次失败是这 15 轮之外的瞬时窗口，三次尝试各在 5.02/5.04 s 被掐，
账本 sha 与 mtime 未变、`task=-` ⇒ 没建单、没扣费。
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time

HOST = os.environ.get("PROBE_HOST", "")
ROUNDS = int(os.environ.get("PROBE_ROUNDS", "5"))
CONNECT_TIMEOUT = 12.0


def resolve_base_host() -> str:
    """默认取后端真正在用的 base（SUNO_API_BASE），保证探针和生产同域名同解析路径。"""
    base = os.environ.get("SUNO_API_BASE", "https://key.34ku.com")
    return base.split("://", 1)[-1].split("/", 1)[0]


def one_round(index: int, host: str) -> bool:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        dns_ms = (time.perf_counter() - t0) * 1000
        ips = sorted({i[4][0] for i in infos})
        print(f"[{index:2d}] dns={dns_ms:7.1f}ms ips={ips}")
    except OSError as exc:
        print(f"[{index:2d}] dns=FAIL {(time.perf_counter() - t0) * 1000:7.1f}ms "
              f"{type(exc).__name__}: {exc}")
        return False

    ip = infos[0][4][0]
    t1 = time.perf_counter()
    try:
        sock = socket.create_connection((ip, 443), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        print(f"     tcp=FAIL {(time.perf_counter() - t1) * 1000:7.1f}ms "
              f"{type(exc).__name__}: {exc} (ip={ip})")
        return False
    print(f"     tcp={(time.perf_counter() - t1) * 1000:7.1f}ms peer={sock.getpeername()}")

    t2 = time.perf_counter()
    try:
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        print(f"     tls={(time.perf_counter() - t2) * 1000:7.1f}ms "
              f"{tls.version()} {tls.cipher()[0]}")
        tls.close()
        return True
    except OSError as exc:
        print(f"     tls=FAIL {(time.perf_counter() - t2) * 1000:7.1f}ms "
              f"{type(exc).__name__}: {exc}")
        sock.close()
        return False


def http_probe(host: str) -> None:
    """无凭据 GET 根路径：只验 HTTP 层，绝不碰 /suno/submit/music。"""
    import httpx

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
    started = time.perf_counter()
    try:
        resp = httpx.get(f"https://{host}/", timeout=timeout, follow_redirects=False)
        print(f"http={(time.perf_counter() - started) * 1000:7.1f}ms "
              f"status={resp.status_code} (无凭据 GET {host}/，不建单不扣费)")
    except httpx.HTTPError as exc:
        print(f"http=FAIL {(time.perf_counter() - started) * 1000:7.1f}ms "
              f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="store_true", help="额外发一次无凭据 GET 验 HTTP 层")
    args = parser.parse_args()

    host = HOST or resolve_base_host()
    print(f"host={host} rounds={ROUNDS} connect_timeout={CONNECT_TIMEOUT}s "
          f"(只测 DNS/TCP/TLS，不发业务请求)")
    ok = 0
    for index in range(ROUNDS):
        if one_round(index, host):
            ok += 1
        time.sleep(0.4)
    print(f"\n成功 {ok}/{ROUNDS} 轮")
    if args.http:
        http_probe(host)
    return 0 if ok == ROUNDS else 1


if __name__ == "__main__":
    raise SystemExit(main())
