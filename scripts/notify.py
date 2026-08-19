#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""재개발닷컴 관심구역 매물/최저가/경매 변동 감시 및 텔레그램 알림.

표준 라이브러리만 사용한다. 재개발닷컴 API와 찜하기 페이지는 사용하지 않고
공개 서버렌더링 HTML(https://jaegebal.com/develops/{id})만 읽는다.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://jaegebal.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGIONS_PATH = os.path.join(ROOT, "config", "regions.json")
STATE_PATH = os.path.join(ROOT, "state.json")

KST = timezone(timedelta(hours=9))
CHAT_ID = "-1003963005534"
MAX_LEN = 3500          # 텔레그램 메시지 분할 기준
CRAWL_DELAY = 1.2       # robots.txt Crawl-delay 준수


# ---------------------------------------------------------------- 페이지 수집

def fetch(url):
    """구역 페이지 HTML을 가져온다. 실패 시 2/4/6초 백오프로 최대 3회 재시도."""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # 네트워크/HTTP 오류 모두 재시도 대상
            last_err = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("요청 실패: %s (%s)" % (url, last_err))


# ---------------------------------------------------------------- HTML 파싱

RE_PILL_ASKS = re.compile(
    r'/develops/\d+/asks"[\s\S]{0,600}?develop-header-pill-count[^>]*>\s*([\d,]+)\s*개')
RE_PILL_AUCTIONS = re.compile(
    r'/develops/\d+/auctions"[\s\S]{0,600}?develop-header-pill-count[^>]*>\s*([\d,]+)\s*건')
RE_META_DESC = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]*)"', re.I)
RE_MIN_PRICE = re.compile(r'매물\s*[\d,]+\s*건\s*([^\s·]+)')
RE_NAME_SPAN = re.compile(
    r'<span[^>]*style="[^"]*white-space:\s*nowrap[^"]*font-size:\s*20px[^"]*"[^>]*>([^<]+)</span>')
RE_TITLE = re.compile(r'<title[^>]*>([^<]*)</title>', re.I)
RE_STAGE = re.compile(r'develop-meta-item--stage[^>]*>\s*([^<]*)')
RE_STAGE_DATE = re.compile(
    r'develop-meta-item--stage[^>]*>[^<]*<span[^>]*develop-meta-suffix[^>]*>\s*\(([^)]*)\)')


def parse(hid, page):
    """구역 HTML에서 이름/매물수/경매건수/최저가/단계/단계일자를 뽑는다."""
    m = RE_PILL_ASKS.search(page)
    if not m:
        raise ValueError("매물 개수 파싱 실패")
    asks = int(m.group(1).replace(",", ""))

    m = RE_PILL_AUCTIONS.search(page)
    if not m:
        raise ValueError("경매 건수 파싱 실패")
    auctions = int(m.group(1).replace(",", ""))

    # 이름: 사업유형 접두어가 붙은 헤더 span, 없으면 <title>의 " | " 앞부분
    name = None
    m = RE_NAME_SPAN.search(page)
    if m:
        name = html.unescape(m.group(1)).strip()
    if not name:
        m = RE_TITLE.search(page)
        if m:
            name = html.unescape(m.group(1)).split(" | ")[0].strip()
    if not name:
        raise ValueError("구역명 파싱 실패")

    # 최저가: meta description의 "매물 N건 3억~"에서 가격 토큰
    min_price = None
    m = RE_META_DESC.search(page)
    if m:
        desc = html.unescape(m.group(1))
        p = RE_MIN_PRICE.search(desc)
        if p:
            min_price = p.group(1).rstrip("~").strip() or None

    # 단계 / 단계일자
    stage = stage_date = None
    m = RE_STAGE.search(page)
    if m:
        stage = html.unescape(m.group(1)).strip() or None
    m = RE_STAGE_DATE.search(page)
    if m:
        stage_date = m.group(1).strip() or None

    return {
        "id": hid,
        "name": name,
        "asks": asks,
        "auctions": auctions,
        "min_price": min_price,
        "stage": stage,
        "stage_date": stage_date,
    }


# ---------------------------------------------------------------- 텔레그램

def send(text):
    """텔레그램 전송. 3500자 초과 시 줄 단위로 나눠 여러 번 보낸다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    for chunk in split_chunks(text):
        if not token:
            # 토큰이 없으면 로컬 테스트로 보고 본문만 출력
            print(chunk)
            print("-" * 40)
            continue
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                break
            except Exception as exc:
                if attempt == 2:
                    print("텔레그램 전송 실패: %s" % exc, file=sys.stderr)
                else:
                    time.sleep(2 * (attempt + 1))
        time.sleep(0.5)


def split_chunks(text):
    """MAX_LEN 이하로 나눈다. 빈 줄로 구분된 구역 블록은 쪼개지 않는다."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        cand = block if not cur else cur + "\n\n" + block
        if len(cand) <= MAX_LEN:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(block) <= MAX_LEN:
            cur = block
            continue
        # 블록 하나가 한도를 넘으면 줄 단위로 다시 나눈다
        for line in block.split("\n"):
            cand = line if not cur else cur + "\n" + line
            if len(cand) > MAX_LEN and cur:
                chunks.append(cur)
                cur = line
            else:
                cur = cand
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- 메시지 작성

HIGHLIGHT_MAX = 5.0  # 억 단위. 이하 최저가는 눈에 띄게 강조한다
RE_PRICE = re.compile(r'^\s*([\d.,]+)\s*(억|만)?\s*$')


def esc(s):
    return html.escape(s or "")


def price_value(s):
    """'3.2억' → 3.2, '9,000만' → 0.9 처럼 억 단위 숫자로 바꾼다."""
    if not s:
        return None
    m = RE_PRICE.match(s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return num / 10000.0 if m.group(2) == "만" else num


def is_low_price(s):
    """알림 강조 대상(최저가 5억 이하)인지 판별한다."""
    v = price_value(s)
    return v is not None and v <= HIGHLIGHT_MAX


def price_html(s):
    """5억 이하면 이모지+코드 포맷으로 더 튀게 강조한다."""
    if not s:
        return "-"
    v = price_value(s)
    m = RE_PRICE.match(s)
    if v is None or m is None or v > HIGHLIGHT_MAX:
        return esc(s)
    return "🟥<code>%s</code>%s" % (esc(m.group(1)), esc(m.group(2) or ""))


def title_icon(min_price):
    """저가 매물은 벨 대신 경고 아이콘으로 제목에서 먼저 눈에 띄게 한다."""
    return "🚨" if is_low_price(min_price) else "🔔"


def price_text(v):
    return v if v else "-"


def change_block(cur, prev):
    """변동 알림 1개 구역 블록."""
    hid = cur["id"]
    lines = ["%s <b>%s</b>" % (title_icon(cur.get("min_price")), esc(cur["name"]))]

    if prev.get("asks") != cur["asks"]:
        diff = cur["asks"] - (prev.get("asks") or 0)
        lines.append("매물 %d개 → %d개 (%+d)" % (prev.get("asks") or 0, cur["asks"], diff))
    if prev.get("min_price") != cur["min_price"]:
        lines.append("최저가 %s → %s" % (esc(price_text(prev.get("min_price"))),
                                          price_html(cur["min_price"])))
    if prev.get("auctions") != cur["auctions"]:
        lines.append("경매 %d건 → %d건" % (prev.get("auctions") or 0, cur["auctions"]))

    now = "현재: 매물 %d개" % cur["asks"]
    if cur["min_price"]:
        now += "(%s)" % price_html(cur["min_price"])
    now += " 경매 %d건" % cur["auctions"]
    lines.append(now)

    links = ['<a href="%s/develops/%d">구역 보기</a>' % (BASE, hid),
             '<a href="%s/develops/%d/asks">매물 %d개 보기</a>' % (BASE, hid, cur["asks"])]
    if cur["auctions"] > 0:
        links.append('<a href="%s/develops/%d/auctions">경매 %d건 보기</a>'
                     % (BASE, hid, cur["auctions"]))
    lines.append(" · ".join(links))
    return "\n".join(lines)


def daily_message(results, today):
    """일일 현황 메시지. 구역명 줄과 수치 줄을 나눠 왼쪽 끝을 맞춘다."""
    blocks = []
    empty = 0
    for r in results:
        if r["asks"] <= 0:
            empty += 1
            continue
        head = '%s <b><a href="%s/develops/%d">%s</a></b>' % (
            title_icon(r.get("min_price")), BASE, r["id"], esc(r["name"]))
        body = "매물 %d개" % r["asks"]
        if r["min_price"]:
            body += "(%s)" % price_html(r["min_price"])
        if r["auctions"] > 0:
            body += " 경매 %d건" % r["auctions"]
        blocks.append(head + "\n" + body)

    header = "📋 <b>관심구역 현황</b> (%s)" % today
    footer = "매물 없는 구역 %d개는 생략" % empty
    return "\n\n".join([header] + blocks + [footer])


# ---------------------------------------------------------------- 상태 파일

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "regions" in data:
            return data, True
    except (IOError, OSError, ValueError):
        pass
    return {"regions": {}, "last_daily": ""}, False


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- 메인

def main():
    force = "--force" in sys.argv[1:]

    with open(REGIONS_PATH, "r", encoding="utf-8") as f:
        regions = json.load(f)

    state, had_state = load_state()
    prev_regions = state.get("regions", {})

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    results, failures, changed = [], [], []

    for idx, region in enumerate(regions):
        hid = int(region["id"])
        if idx:
            time.sleep(CRAWL_DELAY)
        try:
            cur = parse(hid, fetch("%s/develops/%d" % (BASE, hid)))
        except Exception as exc:
            failures.append((hid, region.get("name", ""), str(exc)))
            print("[FAIL] %s %s - %s" % (hid, region.get("name", ""), exc), file=sys.stderr)
            continue

        results.append(cur)
        print("[OK] %s %s 매물 %d개 최저가 %s 경매 %d건 단계 %s %s" % (
            hid, cur["name"], cur["asks"], price_text(cur["min_price"]),
            cur["auctions"], cur["stage"] or "-", cur["stage_date"] or ""))

        prev = prev_regions.get(str(hid))
        if prev and (prev.get("asks") != cur["asks"]
                     or prev.get("auctions") != cur["auctions"]
                     or prev.get("min_price") != cur["min_price"]):
            changed.append((cur, prev))

        prev_regions[str(hid)] = {
            "name": cur["name"],
            "asks": cur["asks"],
            "auctions": cur["auctions"],
            "min_price": cur["min_price"],
        }

    # 첫 실행이면 변동 알림 대신 일일 현황 1건만 보낸다
    if not had_state:
        changed = []

    send_daily = force or not had_state or (
        now.hour == 9 and state.get("last_daily") != today)

    if changed:
        send("\n\n".join(change_block(c, p) for c, p in changed))

    if send_daily and results:
        send(daily_message(results, today))
        state["last_daily"] = today

    state["regions"] = prev_regions
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S KST")
    save_state(state)

    if failures:
        lines = ["⚠️ <b>재개발닷컴 조회 실패</b> (%d개)" % len(failures)]
        for hid, name, err in failures:
            lines.append("• %s (%s) - %s" % (esc(name), hid, esc(err)))
        send("\n".join(lines))
        sys.exit(1)


if __name__ == "__main__":
    main()
