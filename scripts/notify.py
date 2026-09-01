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

# 실거래 표. 구역 페이지에 이미 서버렌더링돼 있어 추가 요청이 필요 없다.
# 행 하나가 <tr class="hover-item"> 이고 셀 다섯 개다 (2026-09-01 실측):
#   [0] 26.08.22
#   [1] 다세대 2001 년
#   [2] 오금동 65-17 예일빌리지(B동) 4층 전용 12.18 평
#   [3] 3.3 억 공주가 1.5 억          ← '공주가'(공시가격) 앞이 실거래가
#   [4] 6,357 만/평 5.19 평           ← 마지막 평수가 대지지분
RE_TX_ROW = re.compile(r'<tr[^>]*class="[^"]*hover-item[^"]*"[^>]*>([\s\S]*?)</tr>')
RE_TX_CELL = re.compile(r'<td[^>]*>([\s\S]*?)</td>')
RE_TX_DATE = re.compile(r'^\d\d\.\d\d\.\d\d$')
RE_TX_AREA = re.compile(r'전용\s*([\d.]+)\s*평')
RE_TX_PYEONG = re.compile(r'([\d.]+)\s*평')


def cell_text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse_transactions(page):
    """구역 페이지의 실거래 행들을 뽑는다.

    실거래는 매물 알림에 얹는 덤이므로, 표 구조가 바뀌어도 여기서 예외를
    내지 않고 빈 리스트를 돌려준다 — 이것 때문에 매물 알림까지 죽으면 안 된다.
    """
    txs = []
    for row in RE_TX_ROW.findall(page):
        cells = [cell_text(c) for c in RE_TX_CELL.findall(row)]
        if len(cells) < 4 or not RE_TX_DATE.match(cells[0]):
            continue
        kind = re.sub(r"\s*\d{4}\s*년\s*", "", cells[1]).strip()
        where = re.sub(r"\s*전용\s*[\d.]+\s*평\s*$", "", cells[2]).strip()
        m = RE_TX_AREA.search(cells[2])
        area = m.group(1) if m else None
        # 가격: '공주가'(공시가격) 앞쪽 토큰이 실거래가
        price = price_value(cells[3].split("공주가")[0].strip().replace(" ", ""))
        land = None
        if len(cells) > 4:
            found = RE_TX_PYEONG.findall(cells[4])
            land = found[-1] if found else None
        txs.append({"date": cells[0], "kind": kind, "where": where,
                    "area": area, "land": land, "price": price})
    return txs


def tx_date(tx):
    """'26.08.22' → date. 못 읽으면 None."""
    try:
        y, m, d = (int(x) for x in tx["date"].split("."))
        return datetime(2000 + y, m, d).date()
    except (ValueError, AttributeError):
        return None


def recent_cheap_txs(txs, today):
    """최근 TX_DAYS 안의 5억 이하 실거래만 (최신이 위로) 고른다.

    예전에는 '지난 실행 이후 새로 잡힌 것'만 냈는데, 실거래는 구역당 한 달에
    몇 건뿐이라 대부분의 실행에서 한 줄도 안 나왔다. 매물 개수를 볼 때
    "이 동네가 실제로 얼마에 팔리고 있나"를 같이 보는 게 목적이므로,
    최근 것을 매번 그대로 붙인다.
    """
    cutoff = today - timedelta(days=TX_DAYS)
    out = []
    for t in txs:
        if t["price"] is None or t["price"] > HIGHLIGHT_MAX:
            continue
        d = tx_date(t)
        if d is None or d < cutoff:
            continue
        out.append(t)
    return out


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
        "txs": parse_transactions(page),
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

HIGHLIGHT_MAX = 5.0  # 억 단위. 최저가 강조 기준이자 실거래 표시 상한
TX_DAYS = 30         # 실거래는 최근 이 기간 안의 것만 붙인다
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
    """5억 이하면 숫자 부분만 <code>로 감싸 다른 색으로 보이게 한다."""
    if not s:
        return "-"
    v = price_value(s)
    m = RE_PRICE.match(s)
    if v is None or m is None or v > HIGHLIGHT_MAX:
        return esc(s)
    return "<code>%s</code>%s" % (esc(m.group(1)), esc(m.group(2) or ""))


def title_icon(min_price):
    """저가 매물은 벨 대신 경고 아이콘으로 제목에서 먼저 눈에 띄게 한다."""
    return "🚨" if is_low_price(min_price) else "🔔"


def price_text(v):
    return v if v else "-"


# where 는 "오금동 65-17 예일빌리지(B동) 4층" 꼴이다. 이걸 동·번지·건물명으로
# 쪼개, 알림에는 "3.3억 예일빌리지(B동) 오금동 65-17" 처럼 짧게 낸다.
# 번지를 [\d-]+ 로 못박아야 한다 — \S+? 로 두면 "오금동 6" + "5-17..." 처럼
# 번지 한가운데가 잘린다.
RE_WHERE = re.compile(r'^(\S*동)\s+([\d-]+)\s*(.*?)\s*(?:\d+층)?$')


def tx_line(tx):
    """실거래 한 줄. 짧게 — 가격 · 건물명 · 번지.

    유형(다세대)은 적지 않는다: 5억 이하는 사실상 전부 다세대라 매 줄에
    같은 말이 반복될 뿐이다. 다른 유형일 때만 적는다.
    """
    parts = []
    if tx["kind"] and tx["kind"] != "다세대":
        parts.append(esc(tx["kind"]))
    parts.append("%s억" % fmt_num(tx["price"]))

    m = RE_WHERE.match(tx["where"] or "")
    if m:
        building = m.group(3).strip()
        if building:
            parts.append(esc(building))
        parts.append(esc("%s %s" % (m.group(1), m.group(2))))
    elif tx["where"]:
        parts.append(esc(tx["where"]))
    return " ".join(parts)


def fmt_num(v):
    """3.30 -> 3.3, 3.00 -> 3 처럼 불필요한 끝자리 0을 없앤다."""
    return ("%.2f" % v).rstrip("0").rstrip(".")


def zone_block(cur, prev, recent_txs):
    """구역 1개 블록. 변동이 있으면 변동 줄을 얹고, 없어도 현재 상태는 항상 낸다."""
    hid = cur["id"]
    lines = ['%s <b><a href="%s/develops/%d">%s</a></b>'
             % (title_icon(cur.get("min_price")), BASE, hid, esc(cur["name"]))]

    if prev:
        if prev.get("asks") != cur["asks"]:
            diff = cur["asks"] - (prev.get("asks") or 0)
            lines.append("매물 %d개 → %d개 (%+d)"
                         % (prev.get("asks") or 0, cur["asks"], diff))
        if prev.get("min_price") != cur["min_price"]:
            lines.append("최저가 %s → %s" % (esc(price_text(prev.get("min_price"))),
                                              price_html(cur["min_price"])))
        if prev.get("auctions") != cur["auctions"]:
            lines.append("경매 %d건 → %d건" % (prev.get("auctions") or 0, cur["auctions"]))

    now = "매물 %d개" % cur["asks"]
    if cur["min_price"]:
        now += "(%s)" % price_html(cur["min_price"])
    if cur["auctions"] > 0:
        now += " 경매 %d건" % cur["auctions"]
    lines.append(now)

    links = []
    if cur["asks"] > 0:
        links.append('<a href="%s/develops/%d/asks">매물 %d개 보기</a>'
                     % (BASE, hid, cur["asks"]))
    if cur["auctions"] > 0:
        links.append('<a href="%s/develops/%d/auctions">경매 %d건 보기</a>'
                     % (BASE, hid, cur["auctions"]))
    if links:
        lines.append(" · ".join(links))

    # 실거래는 매물 링크 바로 아래에 붙인다 — 그 구역 매물을 보는 김에
    # 실제로 얼마에 팔렸는지 같은 자리에서 확인하려는 것이다.
    for tx in recent_txs:
        lines.append(tx_line(tx))
    return "\n".join(lines)


def status_message(results, prev_regions, tx_map, when):
    """매 실행마다 보내는 전 구역 현황.

    예전에는 변동 있는 구역만 보내고 전체 현황은 하루 한 번(09시)이었다.
    변동이 없어도 그 구역이 지금 어떤 상태인지가 알고 싶은 정보라 매번
    전부 낸다. 매물이 0개인 구역은 여전히 생략하되, 최근 실거래가 있으면
    그건 볼 값어치가 있으므로 포함한다.
    """
    blocks = []
    empty = 0
    for r in results:
        txs = tx_map.get(r["id"], [])
        if r["asks"] <= 0 and not txs:
            empty += 1
            continue
        blocks.append(zone_block(r, prev_regions.get(str(r["id"])), txs))

    header = "📋 <b>관심구역 현황</b> (%s)" % when
    parts = [header] + blocks
    if empty:
        parts.append("매물·최근 실거래 없는 구역 %d개는 생략" % empty)
    return "\n\n".join(parts)


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
    with open(REGIONS_PATH, "r", encoding="utf-8") as f:
        regions = json.load(f)

    state, _had_state = load_state()
    prev_regions = state.get("regions", {})
    # prev_regions 는 아래 루프에서 그 자리에서 갱신된다. 메시지에 "18개 → 19개"
    # 를 쓰려면 갱신 전 값이 필요하므로 여기서 한 벌 떠 둔다.
    before = dict((k, dict(v)) for k, v in prev_regions.items())

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    results, failures = [], []
    tx_map = {}

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

        # 실거래: 최근 한 달 안의 5억 이하만. cur["txs"] 는 페이지 표 순서
        # 그대로(최신 계약일이 위)라 별도 정렬이 필요 없다.
        tx_map[hid] = recent_cheap_txs(cur["txs"], now.date())

        prev_regions[str(hid)] = {
            "name": cur["name"],
            "asks": cur["asks"],
            "auctions": cur["auctions"],
            "min_price": cur["min_price"],
        }

    # 매 실행마다 전 구역 현황을 낸다. 변동 있는 구역만 골라 보내던 것을
    # 그만둔 것이라, 변동 줄은 zone_block 안에서 있을 때만 얹힌다.
    if results:
        send(status_message(results, before, tx_map,
                            now.strftime("%Y-%m-%d %H:%M")))
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
