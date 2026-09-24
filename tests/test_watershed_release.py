#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公開の境界（docs/watershed/ に出してよいもの）と、QA が「画面が描く輪」を検査していることの試験。

公開可能 = 個別承認済み・QA pass・輪郭方式 exact・承認時の格子版と輪の版が今と一致。
それ以外の公開候補は data/watershed/staged/ に残り、docs/watershed/ には出ない。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watershed_qa as wq  # noqa: E402
import ws_pipeline as wp  # noqa: E402
from test_watershed_pipeline import _run_jscript, fake_dam, fake_gen, ring_of  # noqa: E402

APP_JS = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")


def public_files(root: Path) -> list[str]:
    out = root / "docs" / "watershed"
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) if out.exists() else []


def write_release(root: Path, approvals: dict) -> None:
    p = root / wp.RELEASE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": wp.RELEASE_SCHEMA, "approvals": approvals}, ensure_ascii=False),
                 encoding="utf-8")


def approval_for(store: wp.Store, dam_id: str, **over) -> dict:
    """その時点の公開候補と一致する承認（over で一部を書き換えられる）。"""
    pub = store.load_state(dam_id)["published"]
    a = {"flow_version": pub["flow_version"], "ring_version": wp.ring_version(pub["ring"]),
         "outline_method": pub["outline_method"], "approved_by": "試験", "approved_on": "2026-09-24"}
    a.update(over)
    return a


class TestPublicBoundary(unittest.TestCase):
    """合成データ。公開物は承認・exact・版の一致がそろったダムの分だけ。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = wp.Store(self.tmp)
        self.dams = [fake_dam("t-a1"), fake_dam("t-b2")]
        for d in self.dams:
            self.publish(d)
        self.approvals = {}

    def publish(self, dam, seed=0, outline="exact"):
        gen = fake_gen(dam, seed)
        ring = ring_of(gen["rec"])
        judged = wp.judge(gen, ring, dam["id"])
        self.assertEqual(judged["gate"]["status"], "pass")
        wp.apply_result(self.store, dam, judged, gen, ring, {"id": dam["id"]}, outline_method=outline)

    def approve(self, dam_id, **over):
        self.approvals[dam_id] = approval_for(self.store, dam_id, **over)
        write_release(self.tmp, self.approvals)

    def assemble(self):
        return wp.assemble(self.store, self.dams)

    def public_index(self):
        return json.loads((self.tmp / "docs/watershed/index.json").read_text(encoding="utf-8"))

    def candidate_status(self):
        idx = json.loads((self.tmp / "data/watershed/staged/index.json").read_text(encoding="utf-8"))
        return {d["id"]: d["release_status"] for d in idx["dams"]}

    def pub_bytes(self, rel):
        return (self.tmp / "docs/watershed" / rel).read_bytes()

    def staged_bytes(self, dam_id):
        return (self.tmp / "data/watershed/staged/flow" / f"{dam_id}.json").read_bytes()

    # ---- 承認0件
    def test_zero_approvals_publish_an_empty_but_valid_index(self):
        plan = self.assemble()
        self.assertEqual(plan["public_ids"], [])
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "index.json"])
        idx = self.public_index()
        self.assertEqual((idx["release_schema"], idx["dams"], idx["excluded"]), (wp.RELEASE_SCHEMA, [], {}))
        gj = json.loads(self.pub_bytes("basins.geojson"))
        self.assertEqual((gj["features"], gj["version"]), ([], idx["basins_version"]))
        self.assertEqual(self.candidate_status(), {"t-a1": "unapproved", "t-b2": "unapproved"})
        before = {r: self.pub_bytes(r) for r in public_files(self.tmp)}
        self.assertEqual(self.assemble()["written"], [])                 # 変わらなければ書き換えない
        self.assertEqual(before, {r: self.pub_bytes(r) for r in public_files(self.tmp)})

    # ---- 公開できる条件
    def test_only_the_approved_exact_dam_is_published(self):
        self.approve("t-a1")
        plan = self.assemble()
        self.assertEqual(plan["public_ids"], ["t-a1"])
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "flow/t-a1.json", "index.json"])
        self.assertEqual(self.pub_bytes("flow/t-a1.json"), self.staged_bytes("t-a1"))   # 候補と同じバイト列
        idx = self.public_index()
        self.assertEqual([d["id"] for d in idx["dams"]], ["t-a1"])
        d = idx["dams"][0]
        self.assertEqual((d["release_status"], d["qa_status"], d["outline_method"]), ("approved", "pass", "exact"))
        # 下流の案内も公開するダムの中だけ（未公開の t-b2 を名指ししない）
        for k in ("downstream_candidates", "downstream_reaches", "downstream_unverified"):
            self.assertNotIn("t-b2", d[k])
        gj = json.loads(self.pub_bytes("basins.geojson"))
        self.assertEqual([f["properties"]["id"] for f in gj["features"]], ["t-a1"])

    def test_legacy_is_rejected_even_if_the_approval_is_filled_in(self):
        for d in self.dams:
            self.publish(d, outline="legacy")
        self.approve("t-a1")                                  # 版は一致・outline_method=legacy
        self.approve("t-b2", outline_method="exact")          # 承認側だけ exact と書いても通らない
        plan = self.assemble()
        self.assertEqual(plan["public_ids"], [])
        self.assertEqual(self.candidate_status(), {"t-a1": "not_exact", "t-b2": "not_exact"})
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "index.json"])

    def test_unknown_outline_is_rejected(self):
        self.publish(self.dams[0], outline=None)
        self.approve("t-a1", outline_method="exact")
        self.assemble()
        self.assertEqual(self.candidate_status()["t-a1"], "not_exact")
        self.assertNotIn("flow/t-a1.json", public_files(self.tmp))

    def test_version_or_outline_mismatch_is_stale_and_not_published(self):
        self.approve("t-a1", flow_version="000000000000")
        self.approve("t-b2", ring_version="000000000000")
        self.assemble()
        self.assertEqual(self.candidate_status(), {"t-a1": "stale", "t-b2": "stale"})
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "index.json"])
        self.approve("t-a1", outline_method="legacy")        # 承認が legacy を指している
        self.assemble()
        self.assertEqual(self.candidate_status()["t-a1"], "stale")

    # ---- 1基だけの版更新
    def test_single_dam_version_update_touches_only_that_dam(self):
        self.approve("t-a1")
        self.approve("t-b2")
        self.assemble()
        b2_pub, b2_stage = self.pub_bytes("flow/t-b2.json"), self.staged_bytes("t-b2")
        a1_old = self.pub_bytes("flow/t-a1.json")
        v0 = {d["id"]: d["v"] for d in self.public_index()["dams"]}

        self.publish(self.dams[0], seed=1)                   # a1 だけ作り直す（承認は古い版を指す）
        plan = self.assemble()
        self.assertEqual(plan["release"]["stale"], ["t-a1"])
        self.assertEqual(plan["public_ids"], ["t-b2"])
        # 古い承認版は公開から外れる。候補はもう新しい版なので、消さずに隔離して残す
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["quarantined"], ["data/watershed/staged/quarantine/flow/t-a1.json"])
        self.assertEqual((self.tmp / plan["quarantined"][0]).read_bytes(), a1_old)
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "flow/t-b2.json", "index.json"])
        self.assertEqual(self.pub_bytes("flow/t-b2.json"), b2_pub)     # 他のダムは1バイトも変わらない
        self.assertEqual(self.staged_bytes("t-b2"), b2_stage)
        self.assertNotEqual(self.staged_bytes("t-a1"), a1_old)         # 新しい版は候補にある

        self.approve("t-a1")                                           # 新しい版を承認し直す
        plan = self.assemble()
        self.assertEqual(plan["public_ids"], ["t-a1", "t-b2"])
        self.assertIn("flow/t-a1.json", plan["written"])
        self.assertNotIn("flow/t-b2.json", plan["written"])
        self.assertEqual(self.pub_bytes("flow/t-a1.json"), self.staged_bytes("t-a1"))
        self.assertEqual(self.pub_bytes("flow/t-b2.json"), b2_pub)
        v1 = {d["id"]: d["v"] for d in self.public_index()["dams"]}
        self.assertNotEqual(v0["t-a1"], v1["t-a1"])
        self.assertEqual(v0["t-b2"], v1["t-b2"])

    # ---- 未承認ファイルの混入防止
    def test_stray_and_unapproved_files_never_stay_public(self):
        self.approve("t-a1")
        self.assemble()
        out = self.tmp / "docs/watershed"
        planted = {
            "flow/t-b2.json": self.staged_bytes("t-b2"),        # 未承認ダムの格子を手で置いた
            "flow/t-zz.json": b'{"id":"t-zz"}',                 # 由来不明
            "flow/_local/t-b2.json": b"{}",                     # 旧配置の保留退避
            "index.local.json": b"{}",                          # 旧配置のローカル索引
            "_check/x.png": b"\x89PNG",                         # 旧配置の点検画像
        }
        for rel, data in planted.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(data)
        plan = self.assemble()
        self.assertEqual(public_files(self.tmp), ["basins.geojson", "flow/t-a1.json", "index.json"])
        self.assertEqual(plan["removed"], ["flow/t-b2.json"])            # 候補と同一 → 消すだけ
        self.assertEqual(self.staged_bytes("t-b2"), planted["flow/t-b2.json"])   # 候補は残っている
        q = self.tmp / "data/watershed/staged/quarantine"
        for rel in ("flow/t-zz.json", "flow/_local/t-b2.json", "index.local.json", "_check/x.png"):
            self.assertEqual((q / rel).read_bytes(), planted[rel], rel)   # 消さずに隔離
        self.assertFalse((out / "_check").exists())
        self.assertFalse((out / "flow/_local").exists())

    def test_tampered_public_copy_is_replaced_from_staged(self):
        self.approve("t-a1")
        self.assemble()
        (self.tmp / "docs/watershed/flow/t-a1.json").write_bytes(b'{"tampered":true}')
        plan = self.assemble()
        self.assertIn("flow/t-a1.json", plan["written"])
        self.assertEqual(self.pub_bytes("flow/t-a1.json"), self.staged_bytes("t-a1"))

    def test_malformed_release_file_aborts_without_writing(self):
        self.assemble()
        before = public_files(self.tmp)
        p = self.tmp / wp.RELEASE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        for doc in ({"schema": "other", "approvals": {}},
                    {"schema": wp.RELEASE_SCHEMA, "approvals": []},
                    {"schema": wp.RELEASE_SCHEMA, "approvals": {"t-a1": {"flow_version": "x"}}},
                    {"schema": wp.RELEASE_SCHEMA, "approvals": {"t-zz": dict.fromkeys(wp.RELEASE_KEYS, "x")}}):
            p.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(wp.PipelineError):
                self.assemble()
            self.assertEqual(public_files(self.tmp), before)


class TestRepoPublicBoundary(unittest.TestCase):
    """リポジトリの実データ。"""

    def setUp(self):
        self.dams = json.loads((ROOT / "docs/data/dams.json").read_text(encoding="utf-8"))["dams"]
        self.store = wp.Store(ROOT)

    def test_public_docs_hold_only_releasable_dams(self):
        idx = json.loads((ROOT / "docs/watershed/index.json").read_text(encoding="utf-8"))
        ids = [d["id"] for d in idx["dams"]]
        self.assertEqual(public_files(ROOT),
                         sorted(["basins.geojson", "index.json"] + [f"flow/{i}.json" for i in ids]))
        release = json.loads((ROOT / wp.RELEASE_FILE).read_text(encoding="utf-8"))["approvals"]
        for d in idx["dams"]:
            self.assertEqual((d["release_status"], d["qa_status"], d["outline_method"]),
                             ("approved", "pass", "exact"), d["id"])
            pub = self.store.load_state(d["id"])["published"]
            self.assertEqual(wp.release_status(pub, release.get(d["id"])), "approved")
            self.assertEqual((ROOT / "docs/watershed/flow" / f"{d['id']}.json").read_bytes(),
                             self.store.flow_path(d["id"], True).read_bytes())
        gj = json.loads((ROOT / "docs/watershed/basins.geojson").read_text(encoding="utf-8"))
        self.assertEqual([f["properties"]["id"] for f in gj["features"]], ids)
        # 組み直しても公開物は変わらない（リポジトリの docs が状態・承認と食い違っていない）
        plan = wp.assemble(self.store, self.dams, write=False)
        self.assertEqual(plan["public_ids"], ids)
        self.assertEqual(plan["index_version"], idx["version"])

    def test_exact_candidates_keep_legacy_grids_and_archive(self):
        """exact 候補は legacy と同じ格子（版が同じ）で、輪だけが変わる。legacy 候補は保存してある。"""
        arc = ROOT / "data/watershed/staged/legacy"
        for i, s in self.store.all_states().items():
            pub = s.get("published")
            if not pub:
                continue
            self.assertEqual(pub["outline_method"], "exact", i)
            self.assertEqual(s["latest"]["status"], "pass", i)
            leg = json.loads((arc / f"{i}.json").read_text(encoding="utf-8"))["published"]
            self.assertEqual(leg["outline_method"], "legacy", i)
            self.assertEqual(pub["flow_version"], leg["flow_version"], i)
            self.assertNotEqual(wp.ring_version(pub["ring"]), wp.ring_version(leg["ring"]), i)
            self.assertEqual((arc / "flow" / f"{i}.json").read_bytes(),
                             self.store.flow_path(i, True).read_bytes(), i)
            # 承認は ring_version に結び付くので、legacy の輪への承認は exact 候補に通らない
            legacy_approval = approval_for(self.store, i, ring_version=wp.ring_version(leg["ring"]))
            self.assertEqual(wp.release_status(pub, legacy_approval), "stale", i)

    def test_candidates_are_kept_outside_docs(self):
        states = self.store.all_states()
        cands = sorted(i for i, s in states.items() if s.get("published"))
        self.assertEqual(len(cands), 21)
        staged = sorted(p.stem for p in self.store.flow.glob("*.json"))
        self.assertEqual(staged, cands)
        wp.assemble(self.store, self.dams, write=False)      # 版の整合（食い違えば PipelineError）
        for i in ("toyama-shiraiwagawa", "toyama-kubusugawa"):
            self.assertIsNone(states[i]["published"])        # HOLD の2基は候補にもいない
        self.assertIn("toyama-shiraiwagawa", wp.EXCLUDED)

    def test_legacy_21_cannot_be_published_even_with_matching_approvals(self):
        """exact へ作り直す前の legacy 候補21基（data/watershed/staged/legacy/ に保存）を候補に戻して試す。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        arc = ROOT / "data/watershed/staged/legacy"
        store = wp.Store(tmp)
        for p in sorted(arc.glob("toyama-*.json")):
            a = json.loads(p.read_text(encoding="utf-8"))
            store.write_state(a["id"], {"id": a["id"], "name": a["name"], "published": a["published"]})
        shutil.copytree(arc / "flow", tmp / "data/watershed/staged/flow")
        cands = [i for i, s in store.all_states().items() if s.get("published")]
        self.assertEqual(len(cands), 21)
        self.assertEqual({store.load_state(i)["published"]["outline_method"] for i in cands}, {"legacy"})
        for claim in ("legacy", "exact"):
            write_release(tmp, {i: approval_for(store, i, outline_method=claim) for i in cands})
            plan = wp.assemble(store, self.dams)
            self.assertEqual(plan["public_ids"], [], claim)
            self.assertEqual(sorted(plan["release"]["not_exact"]), sorted(cands), claim)
            self.assertEqual(public_files(tmp), ["basins.geojson", "index.json"], claim)


@unittest.skipUnless(os.name == "nt" and shutil.which("cscript"), "Windows の cscript がある環境のみ")
class TestAppJsReleaseGate(unittest.TestCase):
    """app.js の releasedDams（実コード）。公開索引をもう一度確かめる。"""

    def run_gate(self, idx) -> str:
        a = APP_JS.index("function releasedDams(idx) {")
        fn = APP_JS[a:APP_JS.index("api.init = function", a)]
        prog = (fn + chr(10) + "var r=releasedDams(" + json.dumps(idx) + ");var s=[];"
                "for(var i=0;i<r.length;i++){s.push(r[i].id);}WScript.Echo(s.join(','));")
        return _run_jscript(prog).strip()

    def test_gate(self):
        ok = {"release_status": "approved", "qa_status": "pass", "outline_method": "exact"}
        dams = [dict(ok, id="a"),
                dict(ok, id="b", release_status="unapproved"),
                dict(ok, id="c", release_status="stale"),
                dict(ok, id="d", qa_status="hold"),
                dict(ok, id="e", outline_method="legacy"),
                dict(ok, id="f", release_status="not_exact"),
                {"id": "g", "qa_status": "pass"}]
        self.assertEqual(self.run_gate({"release_schema": "ws-release/1", "dams": dams}), "a")
        # 承認の仕組みを持たない索引（P0 まで）・別の版の仕組みでは1基も出さない
        self.assertEqual(self.run_gate({"dams": dams}), "")
        self.assertEqual(self.run_gate({"release_schema": "ws-release/2", "dams": dams}), "")
        self.assertEqual(self.run_gate({"release_schema": "ws-release/1", "dams": []}), "")

    def test_repo_public_index_passes_the_gate_unchanged(self):
        idx = json.loads((ROOT / "docs/watershed/index.json").read_text(encoding="utf-8"))
        got = [x for x in self.run_gate(idx).split(",") if x]
        self.assertEqual(got, [d["id"] for d in idx["dams"]])


class TestQaChecksTheDrawnRing(unittest.TestCase):
    """QA が検査する輪 = 画面が描く輪（どちらも smoothRing(..., 2)）。片方だけ変えるとここで落ちる。"""

    def test_same_smoothing_iterations(self):
        m = re.search(r'var ring = smoothRing\(f\.geometry\.coordinates\[0\], (\d+)\);\s*'
                      r'map\.getSource\("ws-basin"\)\.setData', APP_JS)
        self.assertIsNotNone(m, "app.js が集水域の輪を smoothRing を通して描いていない")
        src = (ROOT / "scripts" / "watershed_qa.py").read_text(encoding="utf-8")
        q = re.search(r"drawn = wc\.smooth_ring\(ring, (\d+)\)", src)
        self.assertIsNotNone(q)
        self.assertEqual((m.group(1), q.group(1)), ("2", "2"))
        # 外側を暗くする穴も同じ輪
        self.assertRegex(APP_JS, r'\[\[-180, -85\], \[180, -85\], \[180, 85\], \[-180, 85\], \[-180, -85\]\], ring\]')

    def test_recorded_qa_reproduces_on_candidate_data(self):
        """公開候補の各ダムについて、記録済みの空間QAが候補の格子＋輪から再現する。"""
        store = wp.Store(ROOT)
        pubs = {i: s for i, s in store.all_states().items() if s.get("published")}
        if not pubs:
            self.skipTest("公開候補がない")
        keys = ("mask_far_outside_pct", "ring_far_only_pct", "path_outside_pct",
                "ring_self_intersections_drawn", "iou", "unreached_pct")
        for i, s in pubs.items():
            sp = wq.spatial_metrics(store.read_flow(i), s["published"]["ring"])
            for k in keys:
                self.assertEqual(sp[k], s["published"]["qa"]["spatial"][k], (i, k))


if __name__ == "__main__":
    unittest.main()
