"""零成本验证「生成进度 + 防重复计费账本」全链路。

**不碰真实 Suno、不花一分钱**：脚本自己起一个本地假上游（ThreadingHTTPServer），
把 SUNO_API_BASE 指过去、SUNO_API_KEY 换成假 key，账本 / 落盘目录 / clip 索引全部改到临时目录，
音频素材用仓库里**已经付过费**的静态 WAV 当假上游的下载负载。

跑法（容器内，宿主机没有 Python 依赖）：
    docker exec museblocks-backend python scripts/verify_generate_ledger.py

覆盖的分支都是「花过钱之后最贵的事故」：
    首次建单 / 同指纹命中缓存 / 并发点两下只建一单 / 轮询超时后免费重查 /
    上游判死后才允许再付费 / WAV 被删不重新扣费 / 后端重启账本仍在 /
    丢拍不判死 / 双生第二条下载失败只补差集 / 指纹口径（载荷而非 raw prompt）/
    submitted 票据永不淘汰 / 同步端点共用账本。

每条断言都打印实测计数（submit / fetch / download 次数），失败时退出码非 0。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from api import generate as generate_api  # noqa: E402
from utils import clip_index, generate_cache, generate_jobs  # noqa: E402

# 已扣费的短片段（5.6 MB / 约 32 s），当假上游的下载素材刚好，转码也快
SOURCE_WAV = BACKEND_DIR / "static" / "output" / "suno_1793677292_700f18.wav"

PROMPT = "dream pop, female vocal\n[Verse 1]\n月光落在窗台上\n[Chorus]\n我们一起唱"
# 与 PROMPT 掰成同一份上游载荷：lo-fi 被去掉、[Intro] 被换成 [Verse 1]
PROMPT_SAME_PAYLOAD = "dream pop, female vocal, lo-fi\n[Intro]\n月光落在窗台上\n[Chorus]\n我们一起唱"
PROMPT_OTHER = "city pop, male vocal\n[Verse 1]\n霓虹在夜里流动"


class Upstream:
    """假 Suno 网关：行为由 script / mode 注入，所有请求都计数。"""

    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.lock = threading.Lock()
        self.submits = 0
        self.fetches = 0
        self.downloads = 0
        self.payloads: list[dict[str, Any]] = []
        self.submit_mode = "ok"
        self.fetch_script: list[str] = ["complete"]
        self.download_fail: set[str] = set()
        self.port = 0
        self._task_seq = 0

    def next_fetch(self) -> str:
        with self.lock:
            self.fetches += 1
            if len(self.fetch_script) > 1:
                return self.fetch_script.pop(0)
            return self.fetch_script[0]

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {"submit": self.submits, "fetch": self.fetches, "download": self.downloads}


def make_handler(upstream: Upstream) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # 静默，脚本自己打印计数
            return None

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if self.path != "/suno/submit/music":
                self._json(404, {"code": 404, "message": "no such route"})
                return
            with upstream.lock:
                upstream.submits += 1
                upstream.payloads.append(json.loads(raw or b"{}"))
                upstream._task_seq += 1
                seq = upstream._task_seq
            if upstream.submit_mode == "http500":
                self._json(500, {"code": 500, "message": "gateway exploded"})
                return
            if upstream.submit_mode == "reject":
                self._json(200, {"code": 400, "message": "quota exceeded"})
                return
            self._json(200, {"code": "success", "data": f"fake-task-{seq}"})

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/suno/fetch/"):
                token = upstream.next_fetch()
                if token == "close":
                    # 模拟代理掐 TLS：不回任何字节直接断开 → httpx.RemoteProtocolError
                    self.close_connection = True
                    self.connection.close()
                    return
                if token == "503":
                    self._json(503, {"code": 503, "message": "busy"})
                    return
                if token == "pending":
                    self._json(200, {"code": 200, "data": {"status": "PENDING"}})
                    return
                if token == "failed":
                    self._json(
                        200,
                        {"code": 200, "data": {"status": "FAILED", "fail_reason": "上游判死：内容审核"}},
                    )
                    return
                if token == "complete_no_audio":
                    self._json(200, {"code": 200, "data": {"status": "SUCCESS", "data": []}})
                    return
                clips = [
                    {
                        "clip_id": f"fake-clip-{index}",
                        "status": "SUCCESS",
                        "audio_url": f"http://127.0.0.1:{upstream.port}/audio/{index}.wav",
                    }
                    for index in range(2)
                ]
                self._json(200, {"code": 200, "data": {"status": "SUCCESS", "data": clips}})
                return

            if self.path.startswith("/audio/"):
                name = self.path.rsplit("/", 1)[-1]
                with upstream.lock:
                    upstream.downloads += 1
                if name in upstream.download_fail:
                    self._json(500, {"code": 500, "message": "cdn missing"})
                    return
                self._send(200, upstream.audio, "audio/wav")
                return

            self._json(404, {"code": 404, "message": "no such route"})

    return Handler


@contextmanager
def sandbox() -> Iterator[tuple[TestClient, Upstream, Path]]:
    """把账本、落盘目录、clip 索引、上游地址全部隔离到临时环境。"""
    audio = SOURCE_WAV.read_bytes()
    upstream = Upstream(audio)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(upstream))
    upstream.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    tmp = Path(tempfile.mkdtemp(prefix="museblocks-ledger-"))
    output = tmp / "output"
    output.mkdir()
    saved = {
        "ledger": generate_cache.LEDGER_PATH,
        "cache_output": generate_cache.OUTPUT_DIR,
        "api_output": generate_api.OUTPUT_DIR,
        "index": clip_index.INDEX_PATH,
        "table": generate_jobs._table,
        "env": dict(os.environ),
    }
    generate_cache.LEDGER_PATH = tmp / "generate_cache.json"
    generate_cache.OUTPUT_DIR = output
    generate_api.OUTPUT_DIR = output
    clip_index.INDEX_PATH = tmp / "clip_index.json"
    generate_jobs._table = None
    os.environ.update(
        {
            "SUNO_API_BASE": f"http://127.0.0.1:{upstream.port}",
            "SUNO_API_KEY": "zero-cost-test-key",
            "SUNO_POLL_INTERVAL": "0.05",
            "SUNO_TIMEOUT": "10",
            "SUNO_MODEL": "chirp-test",
            "SUNO_VERBOSE_LOG": "0",
        }
    )
    try:
        with TestClient(main.app) as client:
            yield client, upstream, tmp
    finally:
        os.environ.clear()
        os.environ.update(saved["env"])
        generate_cache.LEDGER_PATH = saved["ledger"]
        generate_cache.OUTPUT_DIR = saved["cache_output"]
        generate_api.OUTPUT_DIR = saved["api_output"]
        clip_index.INDEX_PATH = saved["index"]
        generate_jobs._table = saved["table"]
        server.shutdown()
        server.server_close()


CHECKS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, evidence: str = "") -> None:
    CHECKS.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'} | {label}{(' | ' + evidence) if evidence else ''}")


def submit(client: TestClient, prompt: str = PROMPT, max_clips: int = 2) -> tuple[int, dict]:
    response = client.post(
        "/api/generate/jobs",
        json={"prompt": prompt, "max_clips": max_clips},
    )
    return response.status_code, response.json()


def wait_final(client: TestClient, job_id: str, budget: float = 40.0) -> dict:
    started = time.monotonic()
    while time.monotonic() - started < budget:
        body = client.get(f"/api/generate/jobs/{job_id}").json()
        if body.get("final"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 在 {budget}s 内没到终态：{body}")


def run_job(client: TestClient, prompt: str = PROMPT, max_clips: int = 2) -> dict:
    _, job = submit(client, prompt, max_clips)
    return wait_final(client, job["job_id"])


def ledger_entries() -> dict:
    return json.loads(generate_cache.LEDGER_PATH.read_text(encoding="utf-8"))["entries"]


def main_case() -> None:
    with sandbox() as (client, upstream, tmp):
        print(f"== 沙箱：{tmp} 假上游 127.0.0.1:{upstream.port} 素材 {SOURCE_WAV.name} ==")

        # 1. 首次生成：真的建单，双生两条都落盘
        upstream.fetch_script = ["pending", "pending", "complete"]
        job = run_job(client)
        counts = upstream.counts()
        check(
            job["status"] == "done" and job["source"] == "submit" and job["clip_count"] == 2,
            "首次生成建单并落盘双生",
            f"status={job['status']} source={job['source']} clips={job['clip_count']} "
            f"poll_ticks={job['progress'].get('poll_ticks')} counts={counts}",
        )
        check(counts["submit"] == 1, "首次生成只建一单", f"counts={counts}")
        files = sorted(p.name for p in (tmp / "output").glob("*.wav"))
        check(len(files) == 2, "两个 WAV 真的在盘上", f"files={files}")
        entries = ledger_entries()
        first_fp = job["fingerprint"]
        check(
            len(entries) == 1 and entries[first_fp]["state"] == "ready",
            "账本记为 ready",
            f"entries={len(entries)} state={entries[first_fp]['state']}",
        )
        check(
            job["clips"][0]["audio_url"].startswith("/static/output/"),
            "候选回的是落盘地址而不是 blob",
            f"url={job['clips'][0]['audio_url']}",
        )
        durations = [clip["duration"] for clip in job["clips"]]
        check(all(d > 30 for d in durations), "落盘时长与素材一致", f"durations={durations}")

        # 2. 同指纹再提交：命中缓存，一个上游请求都不发
        before = upstream.counts()
        job2 = run_job(client)
        after = upstream.counts()
        check(
            job2["source"] == "cache" and job2["reused"] is True and after == before,
            "同指纹命中缓存且零上游请求",
            f"source={job2['source']} before={before} after={after}",
        )

        # 3. 并发点两下：同一个 job，只建一单
        upstream.fetch_script = ["pending"] * 30 + ["complete"]
        status_a, job_a = submit(client, PROMPT_OTHER)
        status_b, job_b = submit(client, PROMPT_OTHER)
        final_a = wait_final(client, job_a["job_id"])
        counts = upstream.counts()
        check(
            job_a["job_id"] == job_b["job_id"] and status_b == 200 and final_a["source"] == "submit",
            "并发同指纹复用同一个 job",
            f"job_a={job_a['job_id']} job_b={job_b['job_id']} http={status_a}/{status_b}",
        )
        check(counts["submit"] == 2, "两个不同指纹各建一单，没有第三单", f"counts={counts}")

        # 4. 轮询超时：票据留在 submitted，重试免费重查
        upstream.fetch_script = ["pending"]
        timeout_job = run_job(client, "ambient, piano\n[Verse 1]\n雨停之后")
        entries = ledger_entries()
        timeout_fp = timeout_job["fingerprint"]
        check(
            timeout_job["status"] == "failed"
            and "免费重查" in timeout_job.get("detail", "")
            and entries[timeout_fp]["state"] == "submitted",
            "本地超时不判死票据",
            f"detail={timeout_job.get('detail', '')[:90]} state={entries[timeout_fp]['state']}",
        )
        before = upstream.counts()
        upstream.fetch_script = ["complete"]
        refetch = run_job(client, "ambient, piano\n[Verse 1]\n雨停之后")
        after = upstream.counts()
        check(
            refetch["source"] == "refetch" and after["submit"] == before["submit"],
            "超时后重试只免费重查，不再建单",
            f"source={refetch['source']} submit={before['submit']}→{after['submit']} "
            f"fetch={before['fetch']}→{after['fetch']}",
        )

        # 5. 上游判死：只有这种才允许再付费
        upstream.fetch_script = ["failed"]
        dead = run_job(client, "rock, drums\n[Verse 1]\n被判死的一次")
        entries = ledger_entries()
        check(
            dead["status"] == "failed" and entries[dead["fingerprint"]]["state"] == "failed",
            "上游 FAILED 写进账本",
            f"detail={dead.get('detail', '')[:80]}",
        )
        before = upstream.counts()
        upstream.fetch_script = ["complete"]
        again = run_job(client, "rock, drums\n[Verse 1]\n被判死的一次")
        after = upstream.counts()
        check(
            again["source"] == "submit" and after["submit"] == before["submit"] + 1,
            "判死之后才允许再次付费提交",
            f"source={again['source']} submit={before['submit']}→{after['submit']}",
        )

        # 6. WAV 被删：摘掉死片段但保留票据，重查免费补回来
        target_fp = again["fingerprint"]
        for clip in ledger_entries()[target_fp]["clips"]:
            (tmp / "output" / clip["filename"]).unlink()
        before = upstream.counts()
        restored = run_job(client, "rock, drums\n[Verse 1]\n被判死的一次")
        after = upstream.counts()
        check(
            restored["source"] == "refetch"
            and restored["clip_count"] == 2
            and after["submit"] == before["submit"],
            "文件被清后免费重下，不重新扣费",
            f"source={restored['source']} clips={restored['clip_count']} "
            f"submit={before['submit']}→{after['submit']} download={before['download']}→{after['download']}",
        )

        # 7. 后端重启：任务表清空、job_id 404，但账本还在盘上
        old_job_id = restored["job_id"]
        generate_jobs._table = None
        missing = client.get(f"/api/generate/jobs/{old_job_id}")
        before = upstream.counts()
        after_restart = run_job(client, "rock, drums\n[Verse 1]\n被判死的一次")
        after = upstream.counts()
        check(
            missing.status_code == 404 and after_restart["source"] == "cache" and after == before,
            "重启后 job 404，但账本命中仍不花钱",
            f"http={missing.status_code} source={after_restart['source']} counts={after}",
        )

        # 8. 掐 TLS 的两层容错：一次断连在 tick 内被退避吸收；连续断连才作废整拍
        upstream.fetch_script = ["close", "complete"]
        before = upstream.counts()
        absorbed = run_job(client, "folk, guitar\n[Verse 1]\n掐一次连接")
        after = upstream.counts()
        check(
            absorbed["status"] == "done"
            and absorbed["progress"].get("lost_ticks", 0) == 0
            and after["fetch"] >= before["fetch"] + 2,
            "单次断连在 tick 内退避吸收，不算丢拍",
            f"status={absorbed['status']} lost_ticks={absorbed['progress'].get('lost_ticks')} "
            f"fetch={before['fetch']}→{after['fetch']}",
        )
        # 退避阶梯 (1,2,4) 共 4 次尝试，连续 5 次断连必然击穿 → PollTickLost
        upstream.fetch_script = ["close"] * 5 + ["complete"]
        before = upstream.counts()
        lost = run_job(client, "folk, banjo\n[Verse 1]\n连续掐连接")
        after = upstream.counts()
        check(
            lost["status"] == "done" and lost["progress"].get("lost_ticks", 0) >= 1,
            "击穿退避只作废一拍，已扣费任务照样完成",
            f"status={lost['status']} lost_ticks={lost['progress'].get('lost_ticks')} "
            f"poll_ticks={lost['progress'].get('poll_ticks')} fetch={before['fetch']}→{after['fetch']}",
        )

        # 9. 双生第二条下载失败：已落盘的第一条保留，重试只补差集
        upstream.fetch_script = ["complete"]
        upstream.download_fail = {"1.wav"}
        partial = run_job(client, "jazz, sax\n[Verse 1]\n第二条下不来")
        entries = ledger_entries()
        partial_entry = entries[partial["fingerprint"]]
        landed = [c["filename"] for c in partial_entry["clips"]]
        check(
            partial["status"] == "failed" and len(landed) == 1,
            "第二条下载失败时第一条已兑现进账本",
            f"status={partial['status']} state={partial_entry['state']} landed={landed} "
            f"detail={partial.get('detail', '')[:70]}",
        )
        upstream.download_fail = set()
        before = upstream.counts()
        filled = run_job(client, "jazz, sax\n[Verse 1]\n第二条下不来")
        after = upstream.counts()
        check(
            filled["source"] == "refetch"
            and filled["clip_count"] == 2
            and after["download"] == before["download"] + 1
            and after["submit"] == before["submit"],
            "重试只下载缺的那一条",
            f"source={filled['source']} clips={filled['clip_count']} "
            f"download={before['download']}→{after['download']} submit={after['submit']}",
        )

        # 10. 指纹口径：不同 raw prompt 掰成同一份载荷就该命中同一条票据
        fp_a = generate_cache.fingerprint(generate_api.build_payload(PROMPT))
        fp_b = generate_cache.fingerprint(generate_api.build_payload(PROMPT_SAME_PAYLOAD))
        check(fp_a == fp_b, "lo-fi / [Intro] 归一后指纹相同", f"{fp_a} == {fp_b}")
        payload = generate_api.build_payload(PROMPT)
        fp_wait = generate_cache.fingerprint({**payload, "wait_audio": True})
        check(
            generate_cache.fingerprint(payload) == fp_wait,
            "传输开关 wait_audio 不进指纹",
            f"fp={fp_wait}",
        )
        fp_mv = generate_cache.fingerprint({**payload, "mv": "chirp-other"})
        check(fp_mv != fp_a, "换模型是新指纹", f"{fp_a} != {fp_mv}")
        before = upstream.counts()
        same = run_job(client, PROMPT_SAME_PAYLOAD)
        after = upstream.counts()
        check(
            same["source"] == "cache" and after == before,
            "归一后的另一份 prompt 直接命中缓存",
            f"source={same['source']} counts={after}",
        )

        # 11. submitted 永不淘汰：塞满 ready 之后新票据仍不能被挤掉
        entries = ledger_entries()
        now = time.time()
        for index in range(generate_cache._MAX_ENTRIES + 8):
            entries[f"filler-{index}"] = {
                "task_id": f"filler-task-{index}",
                "state": "ready",
                "clips": [],
                "created_at": now - 1000 + index,
                "updated_at": now - 1000 + index,
            }
        entries["paid-ticket"] = {
            "task_id": "paid-task",
            "state": "submitted",
            "clips": [],
            "created_at": now - 99999,
            "updated_at": now - 99999,
        }
        generate_cache._write(entries)
        generate_cache.mark_submitted("new-ticket", "new-task", {"prompt": "x", "tags": "y"})
        pruned = ledger_entries()
        check(
            "paid-ticket" in pruned and "new-ticket" in pruned and len(pruned) <= generate_cache._MAX_ENTRIES,
            "淘汰只动 ready/failed，已付费票据留着",
            f"entries={len(pruned)} cap={generate_cache._MAX_ENTRIES}",
        )

        # 12. 同步端点共用同一本账：命中缓存时不再建单
        before = upstream.counts()
        sync = client.post("/api/generate", data={"prompt": PROMPT, "max_clips": "2"})
        after = upstream.counts()
        check(
            sync.status_code == 200
            and sync.headers.get("x-suno-source") == "cache"
            and after["submit"] == before["submit"],
            "同步 /api/generate 命中账本不建单",
            f"http={sync.status_code} source={sync.headers.get('x-suno-source')} "
            f"task={sync.headers.get('x-suno-task-id')} bytes={len(sync.content)} counts={after}",
        )
        extras = sync.headers.get("x-suno-extra-clips", "")
        decoded = json.loads(base64.b64decode(extras)) if extras else []
        check(len(decoded) == 1, "同步端点仍回传双生第二条", f"extras={decoded}")

        # 13. 提交被拒 / 网关 5xx：没建单就不该有票据
        before_entries = len(ledger_entries())
        upstream.submit_mode = "reject"
        rejected = run_job(client, "test, reject\n[Verse 1]\n被拒的一次")
        upstream.submit_mode = "http500"
        exploded = run_job(client, "test, explode\n[Verse 1]\n网关炸了一次")
        upstream.submit_mode = "ok"
        check(
            rejected["status"] == "failed"
            and exploded["status"] == "failed"
            and len(ledger_entries()) == before_entries,
            "建单失败不留票据（没花钱就不该记）",
            f"reject={rejected.get('detail', '')[:50]} http500={exploded.get('detail', '')[:50]} "
            f"entries={before_entries}→{len(ledger_entries())}",
        )

        print(f"== 总计数 {upstream.counts()} 账本 {generate_cache.summary()} ==")


if __name__ == "__main__":
    main_case()
    failed = [label for ok, label in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        for label in failed:
            print(f"  未通过：{label}")
        sys.exit(1)