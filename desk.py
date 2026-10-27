#!/usr/bin/env python3
"""LINAK DPI1C 책상 블루투스 제어 (맥용, 비공식)

  desk.py scan          책상 찾아서 저장 (처음 한 번)
  desk.py status        현재 높이 / 저장된 프리셋
  desk.py to 110        110cm로 이동
  desk.py preset 1      패널에 저장된 프리셋 1~4로 이동
  desk.py stop          정지
  desk.py serve         시리/단축어용 서버 (http://<맥IP>:8765/...)

이동 중 Ctrl+C를 누르면 바로 멈춥니다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import signal
import socket
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak이 필요해요:  pip install bleak")

# ── DPI1C BLE 프로토콜 ─────────────────────────────────────────────
# 프로토콜 출처: https://github.com/kkqq9320/ha-linak-dpi1c-control ❤️
SERVICE = "99fa0001-338a-1024-8a49-009c0215f78a"
CTRL = "99fa0002-338a-1024-8a49-009c0215f78a"    # 정지
CONFIG = "99fa0011-338a-1024-8a49-009c0215f78a"  # 설정 읽기/쓰기 (+ 응답 notify)
STATUS = "99fa0021-338a-1024-8a49-009c0215f78a"  # 높이/속도 notify
MOVE = "99fa0031-338a-1024-8a49-009c0215f78a"    # 목표 높이 (반복 전송)

LINAK_MFR_ID = 0x00A4               # 광고 패킷의 LINAK 제조사 ID
TRAVEL_RAW = 6500                   # 이동 범위 65cm (raw = cm × 100)
STOP = bytes([0xFF, 0x00])
HEARTBEAT = bytes([0x01, 0x80])
READ_NAME = bytes.fromhex("7f2600")
READ_BASE = bytes.fromhex("7f8100")  # 최저 높이(보정값)
MOVE_INTERVAL = 0.05                 # 공식 앱도 50ms마다 목표를 다시 보냄

CONF = Path.home() / ".config" / "linak-dpi1c" / "config.json"
PAIR_HINT = (
    "처음 연결이면 패널 뒤/밑 버튼을 2~3초 눌러 화면에 ID가 뜨게 한 뒤 다시 해보세요.\n"
    "폰의 Desk Connect 앱이 연결돼 있으면 앱을 완전히 꺼주세요."
)
RESULT_TEXT = {
    "arrived": "도착",
    "already": "이미 그 높이예요",
    "manual": "패널 조작이 감지돼서 손 뗐어요",
    "stalled": "책상이 안 움직여서 멈췄어요 (장애물이나 패널 에러 확인)",
    "timeout": "시간이 너무 걸려서 멈췄어요",
}


def load_conf() -> dict:
    try:
        return json.loads(CONF.read_text())
    except (OSError, ValueError):
        return {}


def save_conf(conf: dict) -> None:
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(conf, indent=2))


class Desk:
    def __init__(self, address: str) -> None:
        self.address = address
        self.client: BleakClient | None = None
        self.base = 60.0              # raw 0일 때 높이(cm). 연결하면 책상에서 읽어옴
        self.raw: int | None = None
        self.speed = 0.0
        self.name: str | None = None
        self.presets: list[int | None] = [None] * 4
        self.on_progress = None       # CLI 진행 표시용 콜백
        self._base_seen = False
        self._presets_seen: set[int] = set()
        self._move_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._gen = 0

    # ── 상태 ──
    @property
    def height(self) -> float | None:
        return None if self.raw is None else round(self.base + self.raw / 100, 1)

    @property
    def max_height(self) -> float:
        return self.base + TRAVEL_RAW / 100

    def clamp(self, cm: float) -> float:
        return round(min(max(cm, self.base), self.max_height), 1)

    def preset_cm(self, n: int) -> float | None:
        raw = self.presets[n - 1]
        return None if raw is None else round(self.base + raw / 100, 1)

    def _on_status(self, _sender, data) -> None:
        d = bytes(data)
        if len(d) < 4:
            return
        raw = int.from_bytes(d[0:2], "little")
        if raw > TRAVEL_RAW:
            return
        self.raw = raw
        self.speed = int.from_bytes(d[2:4], "little", signed=True) / 100

    def _on_config(self, _sender, data) -> None:
        d = bytes(data)
        if len(d) < 2 or d[0] != 0x01:
            return
        t = d[1]
        if t == 0x05 and len(d) >= 7 and d[2] < 4:          # 프리셋 값
            raw = int.from_bytes(d[5:7], "little")
            self.presets[d[2]] = raw if raw <= TRAVEL_RAW else None
            self._presets_seen.add(d[2])
        elif t == 0x03 and len(d) >= 5 and d[4] == 0x00 and d[2] < 4:  # 빈 프리셋
            self.presets[d[2]] = None
            self._presets_seen.add(d[2])
        elif t == 0x03 and len(d) == 5 and d[2] == 0x01:     # 최저 높이(보정값)
            self.base = int.from_bytes(d[3:5], "little") / 100
            self._base_seen = True
        elif len(d) > 2 and t == len(d) - 2 and all(0x20 <= b <= 0x7E for b in d[2:]):
            self.name = d[2:].decode("ascii")

    # ── 연결 ──
    async def connect(self) -> None:
        if self.client and self.client.is_connected:
            return
        self.client = BleakClient(self.address, timeout=20)
        step = "책상 연결"
        try:
            await self.client.connect()
            step = "알림 켜기(페어링)"
            await self.client.start_notify(STATUS, self._on_status)
            await self.client.start_notify(CONFIG, self._on_config)
            step = "높이 읽기"
            self._on_status(None, await self.client.read_gatt_char(STATUS))
            step = "설정 읽기"
            self._base_seen = False
            self._presets_seen.clear()
            for cmd in (READ_NAME, READ_BASE, *(bytes([0x7F, 0x99, 0x00, s]) for s in range(4))):
                await self._write(CONFIG, cmd)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (
                self._base_seen and len(self._presets_seen) == 4
            ):
                await asyncio.sleep(0.05)
        except Exception as e:
            await self.disconnect()
            raise RuntimeError(f"{step} 단계에서 실패 ({type(e).__name__}: {e})") from e
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _write(self, char: str, data: bytes) -> None:
        await self.client.write_gatt_char(char, data, response=False)

    # ── 이동 ──
    def _arrived(self, target: float) -> bool:
        h = self.height
        return h is not None and abs(self.speed) < 0.1 and abs(target - h) < 0.5

    def _opposing(self, target: float) -> bool:
        h = self.height
        if h is None or abs(self.speed) < 0.1 or abs(target - h) < 0.5:
            return False
        return (target - h) * self.speed < 0

    async def _halt(self) -> None:
        try:
            await self._write(CTRL, STOP)
            await asyncio.sleep(0.1)
            await self._write(MOVE, HEARTBEAT)
        except Exception:
            pass

    async def move_to(self, target_cm: float) -> str:
        await self.connect()
        target = self.clamp(target_cm)
        payload = int(round((target - self.base) * 100)).to_bytes(2, "little")
        if self._arrived(target):
            return "already"
        start = last_progress = time.monotonic()
        last_h = self.height
        try:
            while True:
                await self._write(MOVE, payload)
                await asyncio.sleep(MOVE_INTERVAL)
                h, now = self.height, time.monotonic()
                if self.on_progress:
                    self.on_progress(h, target)
                if self._arrived(target):
                    await self._write(CTRL, STOP)
                    return "arrived"
                if self._opposing(target):
                    return "manual"
                if h is not None and (last_h is None or abs(h - last_h) >= 0.1):
                    last_h, last_progress = h, now
                if now - start > 45:
                    await self._halt()
                    return "timeout"
                if now - start > 2 and now - last_progress > 3:
                    await self._halt()
                    return "stalled"
        except asyncio.CancelledError:
            await self._halt()
            raise

    # ── 서버용: 바로 응답하고 뒤에서 이동 ──
    async def request_move(self, target_cm: float) -> bool:
        self._gen += 1
        gen = self._gen
        async with self._lock:
            if gen != self._gen:
                return False
            await self.connect()
            if self._move_task and not self._move_task.done():
                await self._cancel_move()
                await asyncio.sleep(2)  # 컨트롤러가 새 목표를 받을 준비하는 시간
                if gen != self._gen:
                    return False
            self._move_task = asyncio.create_task(self.move_to(target_cm))
            self._move_task.add_done_callback(_log_move)
            return True

    async def _cancel_move(self) -> None:
        task = self._move_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    async def stop(self) -> None:
        self._gen += 1
        await self._cancel_move()
        await self.connect()
        await self._halt()

    async def close(self) -> None:
        await self._cancel_move()
        await self.disconnect()


def _log_move(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    err = task.exception()
    msg = f"이동 실패: {err}" if err else RESULT_TEXT.get(task.result(), task.result())
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ── 시리/단축어용 HTTP 서버 ───────────────────────────────────────
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Error"}


async def route(desk: Desk, key: str, path: str) -> tuple[int, str]:
    u = urlparse(path)
    seg = [s for s in u.path.split("/") if s]
    if not seg:
        return 200, "status | to/<cm> | preset/<1-4> | stop   (?key=... 필요)"
    given = parse_qs(u.query).get("key", [""])[0]
    if not secrets.compare_digest(given.encode(), key.encode()):
        return 403, "키가 틀렸어요"
    if seg == ["status"]:
        await desk.connect()
        return 200, f"지금 {desk.height:.1f}cm"
    if seg == ["stop"]:
        await desk.stop()
        return 200, "멈췄어요"
    if len(seg) == 2 and seg[0] == "to":
        try:
            cm = float(seg[1])
        except ValueError:
            return 400, "높이는 숫자로 (예: to/110)"
        await desk.connect()
        target = desk.clamp(cm)
        await desk.request_move(target)
        return 200, f"{target:g}cm로 움직여요"
    if len(seg) == 2 and seg[0] == "preset" and seg[1] in ("1", "2", "3", "4"):
        await desk.connect()
        n = int(seg[1])
        target = desk.preset_cm(n)
        if target is None:
            return 400, f"프리셋 {n}이 비어있어요"
        await desk.request_move(target)
        return 200, f"프리셋 {n}, {target:g}cm로 움직여요"
    return 404, "없는 명령이에요"


async def handle(desk: Desk, key: str, reader, writer) -> None:
    path = "/"
    try:
        req = await asyncio.wait_for(reader.readline(), 5)
        while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
            pass
        parts = req.decode("latin-1").split()
        path = parts[1] if len(parts) > 1 else "/"
        code, text = await route(desk, key, path)
    except Exception as e:
        code, text = 500, f"오류: {str(e) or type(e).__name__}"
    body = text.encode()
    head = (
        f"HTTP/1.1 {code} {REASONS.get(code, 'OK')}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    try:
        writer.write(head.encode() + body)
        await writer.drain()
    finally:
        writer.close()
    print(f"[{time.strftime('%H:%M:%S')}] {path.split('?')[0]} → {text}")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def serve(desk: Desk, conf: dict, port: int) -> None:
    key = conf.get("key")
    if not key:
        key = conf["key"] = secrets.token_hex(4)
        save_conf(conf)
    try:
        await desk.connect()
        print(f"연결됨: {desk.name or desk.address} / 지금 {desk.height:.1f}cm")
    except Exception as e:
        print(f"아직 연결 안 됨 ({str(e) or type(e).__name__}). 요청이 오면 다시 시도해요.\n{PAIR_HINT}")
    server = await asyncio.start_server(lambda r, w: handle(desk, key, r, w), "0.0.0.0", port)
    ip = lan_ip()
    print("\n단축어 'URL 내용 가져오기'에 넣을 주소 (아이폰은 같은 와이파이여야 해요):")
    for p in ("preset/1", "preset/2", "to/110", "status", "stop"):
        print(f"  http://{ip}:{port}/{p}?key={key}")
    print("\n끄려면 Ctrl+C")
    async with server:
        await server.serve_forever()


# ── CLI ───────────────────────────────────────────────────────────
async def scan(conf: dict) -> int:
    print("책상 찾는 중... (10초)")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    hits, others = [], []
    for dev, adv in found.values():
        name = adv.local_name or dev.name or ""
        if (
            SERVICE in [u.lower() for u in adv.service_uuids]
            or name.upper().startswith("DESK")       # 'DESK 1234', 'DESKER' 등
            or LINAK_MFR_ID in adv.manufacturer_data
        ):
            hits.append((adv.rssi, dev.address, name))
        elif name:
            others.append((adv.rssi, name))
    if not hits:
        print("못 찾았어요. 패널 버튼을 2~3초 눌러 ID가 떠 있는 동안 바로 다시 해보세요.")
        if others:
            print("근처에서 보인 기기:")
            for rssi, name in sorted(others, reverse=True)[:10]:
                print(f"  {name}  신호 {rssi}")
        return 1
    hits.sort(reverse=True)
    for rssi, addr, name in hits:
        print(f"  {name or '(이름 없음)'}  {addr}  신호 {rssi}")
    conf["address"] = hits[0][1]
    save_conf(conf)
    print(f"저장했어요 → 이제 'desk.py status'로 확인해보세요")
    return 0


def progress(h: float | None, target: float) -> None:
    if h is not None:
        print(f"\r  {h:5.1f}cm → {target:.1f}cm ", end="", flush=True)


async def run(args) -> int:
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, asyncio.current_task().cancel)
    conf = load_conf()
    if args.cmd == "scan":
        return await scan(conf)

    address = args.address or conf.get("address")
    if not address:
        print("저장된 책상이 없어요. 먼저 'desk.py scan'을 해주세요.")
        return 1
    desk = Desk(address)
    try:
        if args.cmd == "serve":
            await serve(desk, conf, args.port)
            return 0
        await desk.connect()
        if args.cmd == "status":
            print(f"{desk.name or '책상'}: 지금 {desk.height:.1f}cm "
                  f"(범위 {desk.base:.1f}~{desk.max_height:.1f}cm)")
            for n in range(1, 5):
                cm = desk.preset_cm(n)
                print(f"  프리셋 {n}: {f'{cm:.1f}cm' if cm is not None else '-'}")
            return 0
        if args.cmd == "stop":
            await desk.stop()
            print("멈췄어요")
            return 0
        if args.cmd == "preset":
            target = desk.preset_cm(args.n)
            if target is None:
                print(f"프리셋 {args.n}이 비어있어요")
                return 1
        else:
            target = desk.clamp(args.cm)
        desk.on_progress = progress
        result = await desk.move_to(target)
        print(f"\n{RESULT_TEXT[result]}: 지금 {desk.height:.1f}cm")
        return 0 if result in ("arrived", "already") else 1
    finally:
        await desk.close()


def main() -> None:
    p = argparse.ArgumentParser(description="LINAK DPI1C 책상 제어")
    p.add_argument("--address", help="책상 주소 (scan으로 저장한 값 대신 사용)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="책상 찾아서 저장")
    sub.add_parser("status", help="현재 높이")
    sub.add_parser("stop", help="정지")
    to = sub.add_parser("to", help="높이(cm)로 이동")
    to.add_argument("cm", type=float)
    pr = sub.add_parser("preset", help="프리셋 1~4로 이동")
    pr.add_argument("n", type=int, choices=[1, 2, 3, 4])
    sv = sub.add_parser("serve", help="시리/단축어용 서버")
    sv.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    try:
        code = asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n중단했어요 (책상 정지 명령 보냄)")
        code = 130
    except Exception as e:
        print(f"\n연결/통신 실패: {str(e) or type(e).__name__}\n{PAIR_HINT}")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
