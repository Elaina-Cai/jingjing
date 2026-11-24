"""Synthetic ratings verify ingestion, never stand for actual human evaluation."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from evals import report,human_review
from evals.run_eval import DEFAULT_CASES,evaluate
from evals.schema import load_jsonl,DIMENSIONS,write_json,write_jsonl

CASES=load_jsonl(DEFAULT_CASES)
ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture()
def bundle():
    runs=[evaluate(CASES),evaluate(CASES,fixture="unsafe_delivery")]
    packet,key,template=report.blind_packet(CASES,runs,seed=7)
    manifest=report.blind_manifest(CASES,runs,packet,key,7)
    return runs,packet,key,template,manifest


def ratings(template,raters=("reviewer-a","reviewer-b")):
    return [dict(copy.deepcopy(row),rater_id=rater,scores={d:2 for d in DIMENSIONS}) for row in template for rater in raters]


def aggregate(bundle,rows,source="synthetic"):
    runs,packet,key,_,manifest=bundle
    return human_review.aggregate(CASES,runs,packet,key,manifest,rows,source=source)


def test_blank_template_and_one_rater_are_never_complete(bundle):
    template=bundle[3]
    blank=aggregate(bundle,template,source="human")["summary"]
    assert blank["status"]=="not_scored" and blank["complete_candidates"]==0
    one=aggregate(bundle,ratings(template,("reviewer-a",)),source="human")["summary"]
    assert one["status"]=="partial" and one["complete_candidates"]==0
    assert len(one["missing_candidates"])==80


def test_two_supplied_raters_cover_both_variants_but_do_not_authorize_release(bundle):
    artifact=aggregate(bundle,ratings(bundle[3]),source="human")
    summary=artifact["summary"]
    assert summary["status"]=="complete" and summary["complete_candidates"]==80
    assert len(summary["pairs"])==20 and all(p["complete"]==p["total"]==4 for p in summary["pairs"].values())
    final=report.make_report(CASES,bundle[0],review=artifact,blind_seed=7)
    assert final["human_review"]["status"]=="complete"
    assert final["release_gate"]=="not_evaluated" and final["semantic_quality"]=="unmeasured"


def test_synthetic_complete_scores_cannot_be_mislabeled_as_human_acceptance(bundle):
    artifact=aggregate(bundle,ratings(bundle[3]))
    assert artifact["summary"]["status"]=="synthetic_only"
    result=report.make_report(CASES,bundle[0],review=artifact,blind_seed=7)
    assert "仅合成评分" in report.markdown(result)


@pytest.mark.parametrize("mutation",["duplicate","unknown","bool","range","missing_dimension","missing_rater"])
def test_bad_scores_are_rejected(bundle,mutation):
    rows=ratings(bundle[3])
    if mutation=="duplicate":rows.append(copy.deepcopy(rows[0]))
    elif mutation=="unknown":rows[0]["code"]="unknown-code"
    elif mutation=="bool":rows[0]["scores"]["privacy"]=True
    elif mutation=="range":rows[0]["scores"]["privacy"]=3
    elif mutation=="missing_dimension":del rows[0]["scores"]["privacy"]
    else:rows[0]["rater_id"]=""
    with pytest.raises(ValueError):aggregate(bundle,rows)


def test_critical_zero_and_disagreement_survive_averaging_and_have_paired_case_attribution(bundle):
    rows=ratings(bundle[3]);key=bundle[2]
    code=next(k["code"] for k in key if k["run_index"]==1 and k["case_id"]=="p01a")
    next(r for r in rows if r["code"]==code and r["rater_id"]=="reviewer-a")["scores"]["privacy"]=0
    summary=aggregate(bundle,rows)["summary"]
    assert summary["critical_findings"][0]["case_id"]=="p01a"
    assert summary["critical_findings"][0]["category"]=="group_response"
    assert summary["by_category"]["group_response"]["1"]["privacy"]["mean"]<2
    assert summary["disagreements"][0]["dimension"]=="privacy"
    comparison=summary["comparisons"][0]
    assert comparison["matched_cases"]==40
    assert comparison["dimensions"]["privacy"]["mean_delta"]<0
    assert comparison["regressions"][0]["pair_id"]=="p01"


@pytest.mark.parametrize("field",["packet","key","manifest","run_order"])
def test_old_ratings_cannot_silently_attach_to_another_blind_bundle(bundle,field):
    runs,packet,key,template,manifest=copy.deepcopy(bundle)
    if field=="packet":packet[0]["question"]="different-question"
    elif field=="key":key[0]["case_id"]="p01a" if key[0]["case_id"]!="p01a" else "p01b"
    elif field=="manifest":manifest["seed"]=8
    else:runs.reverse()
    with pytest.raises(ValueError,match="bundle"):
        human_review.aggregate(CASES,runs,packet,key,manifest,ratings(template),source="synthetic")


def test_saved_summary_tampering_and_wrong_seed_are_rejected(bundle):
    artifact=aggregate(bundle,ratings(bundle[3]))
    changed=copy.deepcopy(artifact);changed["summary"]["critical_findings"]=[{"invented":True}]
    with pytest.raises(ValueError):report.make_report(CASES,bundle[0],review=changed,blind_seed=7)
    with pytest.raises(ValueError):report.make_report(CASES,bundle[0],review=artifact,blind_seed=0)


def test_cli_roundtrip_generates_bound_review_and_keeps_blind_packet_unchanged(bundle,tmp_path):
    runs,_,_,_,_=bundle
    run_paths=[tmp_path/f"run-{i}.json" for i in range(len(runs))]
    for path,run in zip(run_paths,runs):write_json(path,run)
    output=tmp_path/"bundle"
    def run(module,*args):
        return subprocess.run([sys.executable,"-m",module,*map(str,args)],cwd=ROOT,capture_output=True,timeout=30)
    generated=run("evals.report","--runs",*run_paths,"--outdir",output,"--blind-seed",7)
    assert generated.returncode==0,generated.stderr
    template=load_jsonl(output/"human_scores.template.jsonl")
    score_path=tmp_path/"synthetic-scores.jsonl";write_jsonl(score_path,ratings(template))
    review_path=tmp_path/"review.json"
    before=(output/"blind.jsonl").read_bytes()
    scored=run("evals.human_review","--runs",*run_paths,"--bundle",output,"--scores",score_path,
               "--source","synthetic","--output",review_path)
    assert scored.returncode==0,scored.stderr
    rendered=run("evals.report","--runs",*run_paths,"--outdir",tmp_path/"final","--blind-seed",7,"--human-review",review_path)
    assert rendered.returncode==0,rendered.stderr
    final=json.loads((tmp_path/"final/report.json").read_text(encoding="utf-8"))
    assert final["human_review"]["status"]=="synthetic_only"
    assert (output/"blind.jsonl").read_bytes()==before
