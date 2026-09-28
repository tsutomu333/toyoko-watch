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
    assert r["rooms"][0]["price"] == 7100  # 学割は除外してスタンダード
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

# ---- 複数ホテル（県内すべて）
PRICES = {"00152":{"vacant":True,"price":7470},"00153":{"vacant":True,"price":6570},"00017":{"vacant":False,"price":None},"00032":{"vacant":False,"price":None}}

def test_multi(tmp):
    import importlib; importlib.reload(checker)
    checker.WATCHLIST = tmp/"watchlist.json"; checker.STATE = tmp/"state.json"; checker.REQUEST_GAP_SEC = 0
    sent = []
    checker.send_mail = lambda s,b: sent.append((s,b)) or True
    cur = {"p": {k:{**v,"vacant":False} for k,v in PRICES.items()}}
    checker.fetch_prices = lambda w: cur["p"]
    mw = {"id":"m1","hotels":list(PRICES),"label":"福岡県（全4軒）","start":"2099-11-17","nights":1,"people":1,"rooms":1,"smoking":"noSmoking","room_keywords":[],"member":False,"active":True}
    checker.save_json(checker.WATCHLIST, [mw])
    checker.run_once(); assert sent == []
    cur["p"] = {**cur["p"], "00153": PRICES["00153"]}
    checker.run_once(); assert len(sent) == 1 and "00153" in sent[0][1] and "（1軒）" in sent[0][0]
    cur["p"] = dict(PRICES)
    checker.run_once(); assert len(sent) == 2 and "ほかに空いている" in sent[1][1]   # 00152が新たに空いた
    checker.run_once(); assert len(sent) == 2
    # 部屋タイプ指定あり: 空いたホテルは詳細確認（ツイン無しなので除外される）
    mw2 = {**mw, "id":"m2", "room_keywords":["ツイン"]}
    checker.fetch_plan = lambda w: FIX
    checker.save_json(checker.WATCHLIST, [mw2]); checker.run_once()
    st = checker.load_json(checker.STATE, {})
    assert st["m2"]["status"] == "full", st["m2"]
    mw3 = {**mw, "id":"m3", "room_keywords":["ダブル"]}
    checker.save_json(checker.WATCHLIST, [mw3]); checker.run_once()
    st = checker.load_json(checker.STATE, {})
    assert st["m3"]["vacant"] == 2 and st["m3"]["hotels"][0]["price"] == 9100
    print("multi ok\n" + sent[1][0] + "\n" + sent[1][1])

with tempfile.TemporaryDirectory() as d: test_multi(Path(d))
