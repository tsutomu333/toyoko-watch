"""東横イン 空室ウォッチ — スマホ用 登録・確認画面（Streamlit）

・満室で取れなかった日程/ホテル/条件を登録 → GitHub上の watchlist.json に保存
・GitHub Actions の監視プログラム(checker.py)が3分おきに確認し、空いたら Gmail に通知
・この画面では登録中の条件と最新の空室状況の確認、「今すぐ確認」ができる

Streamlit Cloud の Secrets に設定するもの:
    GITHUB_TOKEN = "github_pat_..."   # このリポジトリの Contents 読み書き権限
    GITHUB_REPO  = "tsutomu333/toyoko-watch"
    APP_PASSWORD = "好きな合言葉"       # 画面のロック（省略可）
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st

import checker

BASE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
WD = "月火水木金土日"
ROOM_TYPES = ["シングル", "ダブル", "ツイン", "トリプル", "ハートフル", "プレミアム"]
SMOKING = {"禁煙": "noSmoking", "喫煙": "smoking", "どちらでも": "all"}
SMOKING_R = {v: k for k, v in SMOKING.items()}

st.set_page_config(page_title="東横イン空室ウォッチ", page_icon="🏨", layout="centered")


# ------------------------------------------------------------ 合言葉ロック
def secret(key: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(key, default))
    except Exception:  # noqa: BLE001  (secrets.toml が無いローカル実行)
        return default


pw = secret("APP_PASSWORD")
if pw and not st.session_state.get("authed"):
    st.markdown("### 🏨 東横イン空室ウォッチ")
    v = st.text_input("合言葉", type="password")
    if v == pw:
        st.session_state.authed = True
        st.rerun()
    elif v:
        st.error("合言葉が違います")
    st.stop()


# ------------------------------------------------------------ データの読み書き
class Store:
    """GitHub リポジトリ（Secrets 未設定ならローカルファイル）に JSON を保存"""

    def __init__(self) -> None:
        self.token = secret("GITHUB_TOKEN")
        self.repo = secret("GITHUB_REPO")
        self.remote = bool(self.token and self.repo)

    def _url(self, name: str) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{name}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}

    def read(self, name: str, default):
        if not self.remote:
            return checker.load_json(BASE / name, default), None
        r = requests.get(self._url(name), headers=self._headers(), timeout=20)
        if r.status_code == 404:
            return default, None
        r.raise_for_status()
        j = r.json()
        return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]

    def write(self, name: str, data, sha: str | None, message: str) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        if not self.remote:
            (BASE / name).write_text(text, encoding="utf-8")
            return
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(self._url(name), headers=self._headers(), json=body, timeout=20)
        r.raise_for_status()


store = Store()


def update_watchlist(fn, message: str) -> None:
    """最新の watchlist を読み直してから変更して保存（同時更新で消えないように）"""
    for _ in range(3):
        wl, sha = store.read("watchlist.json", [])
        new = fn(wl)
        try:
            store.write("watchlist.json", new, sha, message)
            return
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                continue
            raise
    raise RuntimeError("保存が競合しました。もう一度お試しください。")


@st.cache_data
def load_hotels() -> list[dict]:
    return json.loads((BASE / "hotels.json").read_text(encoding="utf-8"))


def fmt_date(s: str) -> str:
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def fmt_ts(s: str | None) -> str:
    if not s:
        return "未確認"
    t = datetime.fromisoformat(s).astimezone(JST)
    mins = int((datetime.now(JST) - t).total_seconds() // 60)
    ago = "たった今" if mins < 1 else (f"{mins}分前" if mins < 60 else f"{mins // 60}時間前")
    return f"{t:%m/%d %H:%M}（{ago}）"


def show_rooms(rooms: list[dict]) -> None:
    rows = [
        {
            "部屋": f"{r['type']}（{'喫煙' if r['smoking'] else '禁煙'}）",
            "空室": f"{r['vacant']}室" if r["vacant"] else "満室",
            "料金": f"{r['price']:,}円〜" if r.get("price") else "-",
        }
        for r in rooms
    ]
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.caption("条件に合う部屋タイプがこのホテルにありません（部屋タイプの指定を見直してください）")


def show_hotels(hotels: list[dict]) -> None:
    """複数ホテル監視の結果（空いているホテル）を予約リンク付きで表示"""
    for h in hotels:
        price = f"　{h['price']:,}円〜" if h.get("price") else ""
        st.markdown(f"🟢 [{h['name'].replace('東横INN', '')}]({h['url']}){price}")


def show_result(w: dict, res: dict) -> None:
    if checker.is_multi(w):
        if res["vacant"] > 0:
            st.success(f"いま {res['vacant']}軒 空いています（ホテル名をタップで予約ページ）")
            show_hotels(res["hotels"])
        else:
            st.info(f"いまは{len(w['hotels'])}軒すべて満室です")
    else:
        if res["vacant"] > 0:
            st.success(f"いま空いています（{res['vacant']}室）")
            st.link_button("予約ページを開く", checker.booking_url(w))
        else:
            st.info("いまは満室です")
        show_rooms(res["rooms"])


# ------------------------------------------------------------ 画面
st.markdown("### 🏨 東横イン空室ウォッチ")

try:
    watchlist, _ = store.read("watchlist.json", [])
    state, _ = store.read("state.json", {})
except Exception as e:  # noqa: BLE001
    st.error(f"データを読み込めませんでした: {e}")
    st.stop()

meta = state.get("_meta", {})
last_run = meta.get("last_run")
if last_run:
    st.caption(f"監視プログラムの最終チェック: {fmt_ts(last_run)}")
if meta.get("fail_streak", 0) >= checker.BLOCK_ALERT_AFTER:
    st.warning("監視プログラムが空室データを取得できていません（アクセス制限の可能性）。")

if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

tab_list, tab_add = st.tabs(["📋 登録中", "➕ 新しく登録"])

# ---------------- 登録中の一覧
with tab_list:
    if not watchlist:
        st.info("まだ登録がありません。「新しく登録」タブから追加してください。")
    order = {"vacant": 0, "full": 1, "error": 2, None: 3, "expired": 4}
    watchlist_sorted = sorted(
        watchlist, key=lambda w: (order.get(state.get(w["id"], {}).get("status"), 3), w["start"])
    )
    for w in watchlist_sorted:
        s = state.get(w["id"], {})
        status = s.get("status")
        paused = not w.get("active", True)
        if paused:
            badge = "⏸ 一時停止中"
        elif status == "vacant":
            unit = "軒" if checker.is_multi(w) else "室"
            badge = f"🟢 空室あり {s.get('vacant', 0)}{unit}"
        elif status == "full":
            badge = "🔴 満室（監視中）"
        elif status == "expired":
            badge = "⚪ 宿泊日を過ぎました"
        elif status == "error":
            badge = "⚠️ 取得エラー"
        else:
            badge = "⏳ 初回チェック待ち"

        kw = "・".join(w.get("room_keywords") or []) or "部屋指定なし"
        with st.container(border=True):
            st.markdown(f"**{checker.title(w)}**  \n{badge}")
            st.caption(
                f"{fmt_date(w['start'])}から{w.get('nights', 1)}泊 / {w.get('people', 1)}名・{w.get('rooms', 1)}室 / "
                f"{SMOKING_R.get(w.get('smoking', 'all'))} / {kw}"
                f"{' / 会員料金' if w.get('member') else ''}  \n最終確認: {fmt_ts(s.get('last_checked'))}"
            )
            if status == "error" and s.get("error"):
                st.caption(f"エラー内容: {s['error']}")

            c1, c2, c3, c4 = st.columns(4)
            if checker.is_multi(w):
                c1.link_button("一覧", checker.area_url(w), use_container_width=True)
            else:
                c1.link_button("予約", checker.booking_url(w), use_container_width=True)
            if c2.button("今すぐ", key=f"chk_{w['id']}", use_container_width=True):
                with st.spinner("公式サイトを確認中…"):
                    try:
                        st.session_state[f"live_{w['id']}"] = checker.check_watch(w)
                    except Exception as e:  # noqa: BLE001
                        st.session_state[f"live_{w['id']}"] = {"error": str(e)}
            if c3.button("再開" if paused else "停止", key=f"pause_{w['id']}", use_container_width=True):
                wid = w["id"]

                def toggle(wl, wid=wid):
                    for x in wl:
                        if x["id"] == wid:
                            x["active"] = not x.get("active", True)
                    return wl

                update_watchlist(toggle, f"toggle {wid}")
                st.rerun()
            if c4.button("削除", key=f"del_{w['id']}", use_container_width=True):
                wid = w["id"]
                update_watchlist(lambda wl, wid=wid: [x for x in wl if x["id"] != wid], f"delete {wid}")
                st.rerun()

            live = st.session_state.get(f"live_{w['id']}")
            if live:
                if "error" in live:
                    st.error(f"確認できませんでした: {live['error']}")
                else:
                    show_result(w, live)
            elif checker.is_multi(w) and status == "vacant" and s.get("hotels"):
                show_hotels(s["hotels"])
            elif s.get("rooms") and status in ("vacant", "full"):
                with st.expander("前回チェック時の部屋別状況"):
                    show_rooms(s["rooms"])

# ---------------- 新規登録
with tab_add:
    hotels = load_hotels()
    prefs = list(dict.fromkeys(h["pref"] for h in hotels))
    default_pref = prefs.index("福岡県") if "福岡県" in prefs else 0
    pref = st.selectbox("都道府県", prefs, index=default_pref)
    cand = [h for h in hotels if h["pref"] == pref]
    picked = st.multiselect(
        f"ホテル（空欄なら{pref}の全{len(cand)}軒をまとめて監視）", cand,
        format_func=lambda h: h["name"].replace("東横INN", ""), placeholder="すべてのホテル",
    )

    c1, c2 = st.columns(2)
    start = c1.date_input("チェックイン", value=datetime.now(JST).date() + timedelta(days=1), min_value=datetime.now(JST).date())
    nights = c2.number_input("泊数", 1, 7, 1)
    c3, c4 = st.columns(2)
    people = c3.number_input("1室の人数", 1, 4, 1)
    rooms = c4.number_input("部屋数", 1, 5, 1)
    smoking = st.radio("禁煙・喫煙", list(SMOKING), horizontal=True)
    kws = st.multiselect(
        "部屋タイプ（空欄ならどれでもOK）", ROOM_TYPES,
        help="「ダブル」ならエコノミーダブルも含みます。部屋名に含まれる文字で判定します。",
    )
    member = st.checkbox("会員料金・会員枠で判定する（東横INNクラブカード会員の人）")

    base = {"id": uuid.uuid4().hex[:8]}
    if len(picked) == 1:
        base.update({"hotel": picked[0]["code"], "hotel_name": picked[0]["name"]})
    else:
        targets = picked or cand
        label = f"{pref}（全{len(cand)}軒）" if not picked else "・".join(
            h["name"].replace("東横INN", "") for h in picked)
        base.update({"hotels": [h["code"] for h in targets], "label": label, "pref": pref})
    watch = {
        **base,
        "start": start.isoformat(),
        "nights": int(nights),
        "people": int(people),
        "rooms": int(rooms),
        "smoking": SMOKING[smoking],
        "room_keywords": kws,
        "member": member,
        "active": True,
        "created": datetime.now(JST).isoformat(timespec="seconds"),
    }

    b1, b2 = st.columns(2)
    if b1.button("🔍 いまの空室を見る", use_container_width=True):
        with st.spinner("公式サイトを確認中…"):
            try:
                res = checker.check_watch(watch)
                show_result(watch, res)
                if res["vacant"] == 0:
                    st.caption("「監視を登録」を押すと、空いた時にメールします。")
            except Exception as e:  # noqa: BLE001
                st.error(f"確認できませんでした: {e}")

    if b2.button("🔔 監視を登録", type="primary", use_container_width=True):
        try:
            update_watchlist(lambda wl: wl + [watch], f"add {checker.title(watch)} {watch['start']}")
            st.session_state.flash = f"「{checker.title(watch)}」を登録しました。3分おきに確認し、空いたら Gmail でお知らせします。"
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"保存できませんでした: {e}")

st.divider()
st.caption("空室データは東横イン公式サイトの検索結果をもとにしています。予約は必ず公式サイトで行ってください。")
