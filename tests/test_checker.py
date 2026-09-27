import json, sys, copy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checker

FIX = json.loads((Path(__file__).parent / "fixture_00153.json").read_text(encoding="utf-8"))

def w(**kw):
    base = {"id":"t1","hotel":"00153","hotel_name":"東横INN西鉄久留米駅東口","start":"2099-11-17","nights":1,"people":1,"rooms":1,"smoking":"all","room_keywords":[],"member":False,"active":True}
    base.update(kw); return base

def test_filters():
    assert checker.evaluate(w(), FIX)["vacant"] == 36+33+7+6+3+4
    assert checker.evaluate(w(smoking="noSmoking"), FIX)["vacant"] == 33+6+4
    assert checker.evaluate(w(smoking="smoking"), FIX)["vacant"] == 36+7+3
    # 「ダブル」はエコノミーダブルも含む部分一致
    assert checker.evaluate(w(smoking="noSmoking", room_keywords=["ダブル"]), FIX)["vacant"] == 6+4
    assert checker.evaluate(w(room_keywords=["ツイン"]), FIX)["vacant"] == 0
    r = checker.evaluate(w(smoking="noSmoking", room_keywords=["シングル"]), FIX)
    assert r["rooms"][0]["price"] == 6570  # 空きのある最安プラン(学割)
    print("filters ok")

def test_transitions(tmp):
    checker.WATCHLIST = tmp/"watchlist.json"; checker.STATE = tmp/"state.json"
    checker.REQUEST_GAP_SEC = 0
    sent = []
    checker.send_mail = lambda s,b: sent.append(s) or True
    full = copy.deepcopy(FIX)
    for rt in full["roomTypeList"]:
        for p in rt["plans"]:
            p["vacant"] = {"generalVacantRoom":0,"membershipVacantRoom":0}
    plans = {"cur": full}
    checker.fetch_plan = lambda watch: plans["cur"]
    checker.save_json(checker.WATCHLIST, [w(), w(id="old", start="2000-01-01")])
    checker.run_once(); assert sent == []                   # 満室: 通知なし
    plans["cur"] = FIX; checker.run_once(); assert len(sent) == 1   # 空室発生: 通知
    checker.run_once(); assert len(sent) == 1               # 空室継続: 再通知しない
    plans["cur"] = full; checker.run_once()
    plans["cur"] = FIX; checker.run_once(); assert len(sent) == 2   # 再び空いた: 通知
    st = checker.load_json(checker.STATE, {})
    assert st["old"]["status"] == "expired"
    # 取得失敗が続いたら警告
    def boom(watch): raise checker.FetchError("HTTP 403")
    checker.fetch_plan = boom
    for _ in range(6): checker.run_once()
    assert sum("要確認" in s for s in sent) == 1
    # 条件削除で状態も消える
    checker.save_json(checker.WATCHLIST, []); checker.run_once()
    assert list(checker.load_json(checker.STATE, {}).keys()) == ["_meta"]
    print("transitions ok", sent[0])

import tempfile
test_filters()
with tempfile.TemporaryDirectory() as d: test_transitions(Path(d))
