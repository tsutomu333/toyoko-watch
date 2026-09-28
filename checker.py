"""東横イン 空室ウォッチャー（監視・通知本体）

watchlist.json に登録された条件で東横イン公式サイトの空室を確認し、
「満室 → 空室あり」に変わったときに Gmail で通知する。

使い方:
    python checker.py            # 1回だけチェック（GitHub Actions から呼ばれる）
    python checker.py --loop 60  # 60分間、3分おきにチェックし続ける（自分のPCで動かす場合）

環境変数（GitHub Secrets / 自分のPCなら .env でも可）:
    GMAIL_ADDRESS       送信に使う Gmail アドレス
    GMAIL_APP_PASSWORD  Gmail のアプリパスワード（16桁）
    NOTIFY_TO           通知先（省略時は GMAIL_ADDRESS）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
STATE = BASE / "state.json"

JST = timezone(timedelta(hours=9))
SITE = "https://www.toyoko-inn.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
REQUEST_GAP_SEC = 3          # 1リクエストごとの間隔（サイトに負荷をかけない）
EXCLUDE_PRICE_PLANS = ("学割",)
BLOCK_ALERT_AFTER = 5        # 連続でこの回数取得に失敗したら「監視が止まっている」メール


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- 取得・解析
def now_jst() -> datetime:
    return datetime.now(JST)


def end_date(watch: dict) -> str:
    d = date.fromisoformat(watch["start"]) + timedelta(days=int(watch.get("nights", 1)))
    return d.isoformat()


def fmt_day(s: str) -> str:
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}({'月火水木金土日'[d.weekday()]})"


def load_hotels() -> dict[str, dict]:
    try:
        return {h["code"]: h for h in json.loads((BASE / "hotels.json").read_text(encoding="utf-8"))}
    except Exception:  # noqa: BLE001
        return {}


HOTELS = load_hotels()
PREF_CODES = (  # 都道府県コード順（公式サイトの prefecture= の番号）
    "北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
    "新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 "
    "奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 "
    "熊本県 大分県 宮崎県 鹿児島県 沖縄県"
).split()


def is_multi(watch: dict) -> bool:
    """複数ホテル（県内すべて等）をまとめて監視する条件か"""
    return bool(watch.get("hotels"))


def title(watch: dict) -> str:
    if is_multi(watch):
        return watch.get("label") or f"{len(watch['hotels'])}軒"
    return watch.get("hotel_name", watch.get("hotel", ""))


def hotel_name(code: str) -> str:
    return HOTELS.get(code, {}).get("name", code)


def booking_url(watch: dict, hotel: str | None = None) -> str:
    q = {
        "hotel": hotel or watch.get("hotel") or watch["hotels"][0],
        "people": watch.get("people", 1),
        "room": watch.get("rooms", 1),
        "smoking": watch.get("smoking", "all"),
        "start": watch["start"],
        "end": end_date(watch),
    }
    return f"{SITE}/search/result/room_plan/?" + urllib.parse.urlencode(q)


def area_url(watch: dict) -> str:
    """公式サイトの県別の検索結果一覧（複数ホテル監視の「一覧」ボタン用）"""
    q = {"people": watch.get("people", 1), "room": watch.get("rooms", 1),
         "smoking": watch.get("smoking", "all"), "start": watch["start"], "end": end_date(watch)}
    pref = watch.get("pref")
    if pref in PREF_CODES:
        q = {"prefecture": PREF_CODES.index(pref) + 1, **q}
    return f"{SITE}/search/result/?" + urllib.parse.urlencode(q)


def fetch_plan(watch: dict) -> dict:
    """公式サイトの検索結果ページを取得し、埋め込まれた空室データ(planResponse)を返す"""
    req = urllib.request.Request(
        booking_url(watch),
        headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            html = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"通信エラー: {e}") from e

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise FetchError("ページ構造が想定と違う（ブロック or サイト変更の可能性）")
    data = json.loads(m.group(1))
    plan = data.get("props", {}).get("pageProps", {}).get("planResponse")
    if not plan or "roomTypeList" not in plan:
        raise FetchError("空室データが見つからない")
    return plan


def evaluate(watch: dict, plan: dict) -> dict:
    """条件に合う部屋タイプごとの空室数を集計する"""
    smoking = watch.get("smoking", "all")
    keywords = [k for k in watch.get("room_keywords", []) if k]
    member = bool(watch.get("member", False))
    vkey = "membershipVacantRoom" if member else "generalVacantRoom"
    pkey = "membershipPrice" if member else "generalPrice"

    rooms = []
    for rt in plan.get("roomTypeList", []):
        is_smoking = bool(rt.get("specs", {}).get("isSmoking"))
        if smoking == "noSmoking" and is_smoking:
            continue
        if smoking == "smoking" and not is_smoking:
            continue
        name = rt.get("roomTypeName", "")
        if keywords and not any(k in name for k in keywords):
            continue
        best_vac, best_price, best_plan = 0, None, None
        for p in rt.get("plans", []):
            v = int((p.get("vacant") or {}).get(vkey) or 0)
            price = (p.get("price") or {}).get(pkey)
            # 学割など条件付きプランは料金表示に使わない（空室数の判定には含める）
            special = any(x in (p.get("planName") or "") for x in EXCLUDE_PRICE_PLANS)
            if v > 0 and not special and (best_price is None or (price or 10**9) < best_price):
                best_price, best_plan = price, p.get("planName")
            best_vac = max(best_vac, v)
        rooms.append({
            "type": name,
            "smoking": is_smoking,
            "vacant": best_vac,
            "price": best_price,
            "plan": best_plan,
        })
    total = sum(r["vacant"] for r in rooms)
    return {
        "vacant": total,
        "rooms": rooms,
        "hotel_title": plan.get("hotelTitle", ""),
        "can_reservation": plan.get("canReservation", True),
    }


def fetch_prices(watch: dict) -> dict[str, dict]:
    """複数ホテルの空室有無と最安値を1回の問い合わせでまとめて取得（公式サイトの検索結果一覧と同じ仕組み）"""
    start = date.fromisoformat(watch["start"])
    end = date.fromisoformat(end_date(watch))
    iso = lambda d: f"{(d - timedelta(days=1)).isoformat()}T15:00:00.000Z"  # 日本時間0時をUTCで表す
    out: dict[str, dict] = {}
    codes = list(watch["hotels"])
    for i in range(0, len(codes), 60):
        inp = {"0": {"json": {
            "hotelCodes": codes[i:i + 60],
            "checkinDate": iso(start), "checkoutDate": iso(end),
            "numberOfPeople": int(watch.get("people", 1)),
            "numberOfRoom": int(watch.get("rooms", 1)),
            "smokingType": watch.get("smoking", "all"),
        }, "meta": {"values": {"checkinDate": ["Date"], "checkoutDate": ["Date"]}}}}
        url = f"{SITE}/api/trpc/hotels.availabilities.prices?batch=1&input=" + urllib.parse.quote(
            json.dumps(inp, separators=(",", ":")))
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as res:
                data = json.loads(res.read().decode("utf-8"))
            prices = data[0]["result"]["data"]["json"]["prices"]
        except urllib.error.HTTPError as e:
            raise FetchError(f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"空室一覧の取得に失敗: {e}") from e
        for code, v in prices.items():
            out[code] = {
                "vacant": bool(v.get("existEnoughVacantRooms")) and not v.get("isUnderMaintenance"),
                "price": v.get("lowestPrice") or None,
            }
        if i + 60 < len(codes):
            time.sleep(REQUEST_GAP_SEC)
    return out


def check_multi(watch: dict) -> dict:
    """複数ホテルの条件をチェック。部屋タイプ指定や会員枠の指定がある時は、空いたホテルだけ詳しく確認する"""
    prices = fetch_prices(watch)
    detailed = bool(watch.get("room_keywords")) or bool(watch.get("member"))
    hotels = []
    for code in watch["hotels"]:
        p = prices.get(code)
        if not p or not p["vacant"]:
            continue
        item = {"code": code, "name": hotel_name(code), "price": p["price"], "url": booking_url(watch, code)}
        if detailed:
            time.sleep(REQUEST_GAP_SEC)
            r = evaluate(watch, fetch_plan({**watch, "hotel": code}))
            if r["vacant"] <= 0:
                continue
            prices_ok = [x["price"] for x in r["rooms"] if x["vacant"] > 0 and x["price"]]
            item.update({"price": min(prices_ok) if prices_ok else None, "rooms": r["vacant"]})
        hotels.append(item)
    return {"vacant": len(hotels), "hotels": hotels, "checked": len(prices)}


def check_watch(watch: dict) -> dict:
    """1件の条件をチェックして結果を返す（Streamlit画面からも使う）"""
    if is_multi(watch):
        return check_multi(watch)
    return evaluate(watch, fetch_plan({**watch, "hotel": watch["hotel"]}))


# ---------------------------------------------------------------- 通知
def send_mail(subject: str, body: str) -> bool:
    user = os.environ.get("GMAIL_ADDRESS", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("NOTIFY_TO", "").strip() or user
    if not (user and pw):
        print("[mail] GMAIL_ADDRESS / GMAIL_APP_PASSWORD 未設定のため送信せず\n" + subject + "\n" + body)
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("東横イン空室ウォッチ", user))
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"[mail] 送信: {subject}")
    return True


def send_push(title_: str, message: str, url: str | None = None, links: list[tuple[str, str]] | None = None) -> bool:
    """ntfy アプリにプッシュ通知（数秒でロック画面に出る）。NTFY_TOPIC 未設定なら何もしない"""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    body = {"topic": topic, "title": title_, "message": message, "priority": 5, "tags": ["hotel"]}
    if url:
        body["click"] = url
    if links:
        body["actions"] = [{"action": "view", "label": lbl[:20], "url": u} for lbl, u in links[:3]]
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "toyoko-watch"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as res:
        res.read()
    print(f"[push] 送信: {title_}")
    return True


def notify(subject: str, body: str, push_title: str, push_msg: str,
           url: str | None = None, links: list[tuple[str, str]] | None = None) -> bool:
    """プッシュ通知（最優先）とメールの両方を送る。どちらか届けば True"""
    ok = False
    try:
        ok = send_push(push_title, push_msg, url, links) or ok
    except Exception as e:  # noqa: BLE001
        print(f"[push] 送信失敗: {e}")
    try:
        ok = send_mail(subject, body) or ok
    except Exception as e:  # noqa: BLE001
        print(f"[mail] 送信失敗: {e}")
    return ok


def smoking_label(s: str) -> str:
    return {"noSmoking": "禁煙", "smoking": "喫煙", "all": "禁煙・喫煙どちらでも"}.get(s, s)


def describe(watch: dict) -> str:
    kw = "・".join(watch.get("room_keywords") or []) or "指定なし"
    return (
        f"{title(watch)}\n"
        f"日程: {watch['start']} から {watch.get('nights', 1)}泊\n"
        f"人数: {watch.get('people', 1)}名 / {watch.get('rooms', 1)}室\n"
        f"禁煙/喫煙: {smoking_label(watch.get('smoking', 'all'))}\n"
        f"部屋タイプ: {kw}\n"
        f"料金区分: {'会員' if watch.get('member') else '一般'}"
    )


def multi_mail(watch: dict, result: dict, new_codes: list[str]) -> tuple[str, str]:
    def line(h):
        price = f"{h['price']:,}円〜" if h.get("price") else ""
        return f"■ {h['name']}  {price}\n  {h['url']}"
    new = [h for h in result["hotels"] if h["code"] in new_codes]
    others = [h for h in result["hotels"] if h["code"] not in new_codes]
    subject = f"【空室あり】{title(watch)} {watch['start']}（{len(new)}軒）"
    body = (
        "キャンセルが出ました。早い者勝ちなので、すぐ予約してください。\n\n"
        "▼ 新しく空いたホテル（リンクをタップで予約ページ）\n" + "\n".join(line(h) for h in new) + "\n\n"
        + ("▼ ほかに空いているホテル\n" + "\n".join(line(h) for h in others) + "\n\n" if others else "")
        + "▼ 登録条件\n" + describe(watch) + "\n\n"
        f"確認時刻: {now_jst():%Y-%m-%d %H:%M} (JST)\n"
    )
    return subject, body


def vacancy_mail(watch: dict, result: dict) -> tuple[str, str]:
    lines = []
    for r in result["rooms"]:
        if r["vacant"] > 0:
            price = f"{r['price']:,}円" if r["price"] else "-"
            lines.append(f"  ・{r['type']}（{'喫煙' if r['smoking'] else '禁煙'}）残り{r['vacant']}室  {price}〜  {r['plan'] or ''}")
    subject = f"【空室あり】{title(watch)} {watch['start']}"
    body = (
        "キャンセルが出ました。早い者勝ちなので、すぐ予約してください。\n\n"
        f"▼ 予約ページ（タップで開く）\n{booking_url(watch)}\n\n"
        "▼ 空いている部屋\n" + "\n".join(lines) + "\n\n"
        "▼ 登録条件\n" + describe(watch) + "\n\n"
        f"確認時刻: {now_jst():%Y-%m-%d %H:%M} (JST)\n"
    )
    return subject, body


# ---------------------------------------------------------------- 状態管理
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def run_once() -> bool:
    """全条件を1巡チェック。state.json を保存したら True"""
    watches = load_json(WATCHLIST, [])
    state = json.loads(json.dumps(load_json(STATE, {})))
    meta = state.setdefault("_meta", {"fail_streak": 0, "block_alerted": False})
    today = now_jst().date()
    cache: dict[str, tuple[dict | None, str | None]] = {}
    ok_count = fail_count = 0

    active_ids = set()
    for w in watches:
        wid = w["id"]
        active_ids.add(wid)
        st = state.setdefault(wid, {})
        if not w.get("active", True):
            continue
        if date.fromisoformat(w["start"]) < today:
            st["status"] = "expired"
            continue

        if is_multi(w):
            ts = now_jst().isoformat(timespec="seconds")
            try:
                result = check_multi(w)
            except FetchError as e:
                fail_count += 1
                st.update({"status": "error", "error": str(e), "last_checked": ts})
                print(f"[{wid}] 取得失敗: {e}")
                continue
            finally:
                time.sleep(REQUEST_GAP_SEC)
            ok_count += 1
            prev_codes = set(st.get("vacant_hotels") or [])
            now_codes = [h["code"] for h in result["hotels"]]
            new_codes = [c for c in now_codes if c not in prev_codes]
            st.update({
                "status": "vacant" if now_codes else "full",
                "vacant": len(now_codes),
                "vacant_hotels": now_codes,
                "hotels": result["hotels"],
                "last_checked": ts,
                "error": None,
            })
            print(f"[{wid}] {title(w)} {w['start']} 空きホテル {len(prev_codes)} -> {len(now_codes)}")
            if new_codes:
                subject, body = multi_mail(w, result, new_codes)
                new = [h for h in result["hotels"] if h["code"] in new_codes]
                short = lambda h: h["name"].replace("東横INN", "")
                msg = "\n".join(f"{short(h)}" + (f" {h['price']:,}円〜" if h.get("price") else "") for h in new)
                if notify(subject, body, f"空室あり {fmt_day(w['start'])} {len(new)}軒", msg,
                          new[0]["url"], [(short(h), h["url"]) for h in new]):
                    st["last_notified"] = ts
            continue

        key = booking_url(w)
        if key not in cache:
            try:
                cache[key] = (fetch_plan(w), None)
            except FetchError as e:
                cache[key] = (None, str(e))
            time.sleep(REQUEST_GAP_SEC)
        plan, err = cache[key]
        ts = now_jst().isoformat(timespec="seconds")

        if err:
            fail_count += 1
            st.update({"status": "error", "error": err, "last_checked": ts})
            print(f"[{wid}] 取得失敗: {err}")
            continue

        ok_count += 1
        result = evaluate(w, plan)
        prev = st.get("vacant")
        st.update({
            "status": "vacant" if result["vacant"] > 0 else "full",
            "vacant": result["vacant"],
            "rooms": result["rooms"],
            "last_checked": ts,
            "error": None,
        })
        print(f"[{wid}] {title(w)} {w['start']} 空室 {prev} -> {result['vacant']}")

        if result["vacant"] > 0 and not prev:
            subject, body = vacancy_mail(w, result)
            rooms = "・".join(r["type"] for r in result["rooms"] if r["vacant"] > 0)
            if notify(subject, body, f"空室あり {fmt_day(w['start'])} {title(w).replace('東横INN', '')}",
                      f"{rooms}（残り{result['vacant']}室）\nタップで予約ページ", booking_url(w)):
                st["last_notified"] = ts

    # 削除された条件の状態は掃除する
    for k in list(state.keys()):
        if k != "_meta" and k not in active_ids:
            del state[k]

    # 連続失敗の監視（GitHub側のIPがブロックされた等）
    if fail_count and not ok_count:
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
    elif ok_count:
        meta["fail_streak"] = 0
        meta["block_alerted"] = False
    if meta["fail_streak"] >= BLOCK_ALERT_AFTER and not meta.get("block_alerted"):
        try:
            send_push("⚠️ 空室ウォッチ停止中", "空室データを取得できていません。メールを確認してください。")
        except Exception as e:  # noqa: BLE001
            print(f"[push] 送信失敗: {e}")
        try:
            send_mail(
                "【要確認】東横イン空室ウォッチが空室を取得できていません",
                f"{BLOCK_ALERT_AFTER}回連続で空室データの取得に失敗しています。\n"
                "サイト側の仕様変更か、アクセス制限の可能性があります。\n"
                "監視は続けますが、復旧するまで通知は届きません。\n",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[mail] 送信失敗: {e}")
        meta["block_alerted"] = True
    meta["last_run"] = now_jst().isoformat(timespec="seconds")

    # 空室状況が変わった時か、15分に1回だけ保存（GitHubの履歴が増えすぎないように）
    old = load_json(STATE, {})
    last_saved = old.get("_meta", {}).get("last_saved")
    stale = (not last_saved) or (now_jst() - datetime.fromisoformat(last_saved)).total_seconds() >= 15 * 60
    if signature(old) != signature(state) or stale or changed_meta(old, state):
        meta["last_saved"] = meta["last_run"]
        save_json(STATE, state)
        return True
    return False


def signature(state: dict) -> dict:
    return {k: (v.get("status"), v.get("vacant"), v.get("error"), tuple(v.get("vacant_hotels") or ()))
            for k, v in state.items() if k != "_meta"}


def changed_meta(old: dict, new: dict) -> bool:
    o, n = old.get("_meta", {}), new.get("_meta", {})
    return any(o.get(k) != n.get(k) for k in ("block_alerted", "fail_streak"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, default=0, help="この分数だけ繰り返す（0なら1回）")
    ap.add_argument("--interval", type=int, default=180, help="繰り返し間隔（秒）")
    args = ap.parse_args()

    if not args.loop:
        run_once()
        return
    until = time.time() + args.loop * 60
    while True:
        started = time.time()
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"[error] {e}", file=sys.stderr)
        if time.time() + args.interval > until:
            break
        time.sleep(max(0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
