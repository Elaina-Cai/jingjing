"""Validate and aggregate supplied blind ratings, without generating any ratings."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics

from .schema import DIMENSIONS, digest, load_jsonl, write_json
from .run_eval import DEFAULT_CASES


def aggregate(cases,runs,packet,key,manifest,ratings,*,source):
    from .report import blind_packet, blind_manifest
    if source not in {"human","synthetic"}:
        raise ValueError("review_source_required")
    seed=manifest.get("seed")
    if type(seed) is not int:
        raise ValueError("review_blind_seed_invalid")
    actual_packet,actual_key,_=blind_packet(cases,runs,seed)
    if (packet!=actual_packet or key!=actual_key
            or manifest!=blind_manifest(cases,runs,packet,key,seed)):
        raise ValueError("review_bundle_changed")
    identities=[tuple(r["metadata"][k] for k in ("system","condition","model","seed","repeat")) for r in runs]
    if len(identities)!=len(set(identities)):
        raise ValueError("review_duplicate_run")
    mapping={row["code"]:row for row in key}
    seen=set();valid=[]
    for row in ratings:
        if not isinstance(row,dict) or set(row)!={"code","rater_id","scores","comment"}:
            raise ValueError("review_rating_fields_invalid")
        if row["code"] not in mapping or not isinstance(row["rater_id"],str) or not isinstance(row["comment"],str):
            raise ValueError("review_code_or_rater_invalid")
        scores=row["scores"]
        if not isinstance(scores,dict) or set(scores)!=set(DIMENSIONS):
            raise ValueError("review_dimensions_invalid")
        if any(v is not None and (type(v) is not int or v not in {0,1,2}) for v in scores.values()):
            raise ValueError("review_score_invalid")
        rater=row["rater_id"].strip()
        if not rater:
            if all(v is None for v in scores.values()) and not row["comment"]:
                continue  # An unchanged blank template is unscored, never a pass.
            raise ValueError("review_rater_required")
        identity=(row["code"],rater)
        if identity in seen:
            raise ValueError("review_duplicate_rater_candidate")
        seen.add(identity)
        valid.append(dict(row,rater_id=rater))
    by_code=defaultdict(list);by_run=defaultdict(lambda:defaultdict(list));zeros=[]
    by_category=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    case_map={c["id"]:c for c in cases}
    for row in valid:
        info=mapping[row["code"]];by_code[row["code"]].append(row)
        for dimension,value in row["scores"].items():
            if value is not None:
                by_run[info["run_index"]][dimension].append(value)
                by_category[case_map[info["case_id"]]["category"]][info["run_index"]][dimension].append(value)
            if value==0:
                zeros.append({"code":row["code"],"case_id":info["case_id"],"run_index":info["run_index"],
                              "category":case_map[info["case_id"]]["category"],"pair_id":case_map[info["case_id"]]["pair_id"],
                              "dimension":dimension,"rater_id":row["rater_id"],"comment":row["comment"]})
    complete=set();disagreements=[]
    for code in mapping:
        rows=by_code[code]
        if sum(all(v is not None for v in r["scores"].values()) for r in rows)>=2:
            complete.add(code)
        for dimension in DIMENSIONS:
            values=[r["scores"][dimension] for r in rows if r["scores"][dimension] is not None]
            if len(set(values))>1:
                disagreements.append({"code":code,"dimension":dimension,"values":values})
    pairs=defaultdict(lambda:{"complete":0,"total":0})
    for code,info in mapping.items():
        pair=case_map[info["case_id"]]["pair_id"]
        pairs[pair]["total"]+=1
        pairs[pair]["complete"]+=int(code in complete)
    indexed={(mapping[r["code"]]["run_index"],mapping[r["code"]]["case_id"],r["rater_id"]):r for r in valid}
    comparisons=[]
    for i,left in enumerate(runs):
        for j,right in enumerate(runs[i+1:],i+1):
            a,b=left["metadata"],right["metadata"]
            comparable=all(a[k]==b[k] for k in ("model","seed","repeat")) and (
                a["condition"]==b["condition"] or (a["system"]==b["system"] and "full" in {a["condition"],b["condition"]}))
            if not comparable:
                comparisons.append({"left_run":i,"right_run":j,"status":"not_comparable"})
                continue
            delta=defaultdict(list);regressions=[];matched=set()
            for (run_index,case_id,rater),row in indexed.items():
                if run_index!=i or (j,case_id,rater) not in indexed:
                    continue
                other=indexed[(j,case_id,rater)]
                for dimension in DIMENSIONS:
                    before,after=row["scores"][dimension],other["scores"][dimension]
                    if before is None or after is None:
                        continue
                    delta[dimension].append(after-before);matched.add(case_id)
                    if after<before:
                        regressions.append({"case_id":case_id,"pair_id":case_map[case_id]["pair_id"],
                            "rater_id":rater,"dimension":dimension,"before":before,"after":after})
            comparisons.append({"left_run":i,"right_run":j,"status":"paired_ratings_only",
                "matched_cases":len(matched),"total_cases":len(cases),
                "dimensions":{d:{"count":len(delta[d]),"mean_delta":statistics.mean(delta[d]) if delta[d] else None} for d in DIMENSIONS},
                "regressions":regressions})
    scored=sum(v is not None for row in valid for v in row["scores"].values())
    status="complete" if len(complete)==len(mapping) else ("partial" if scored else "not_scored")
    if source=="synthetic":
        status="synthetic_only"
    summary={"status":status,"source":source,"required_raters":2,"candidates":len(mapping),
        "complete_candidates":len(complete),"missing_candidates":sorted(set(mapping)-complete),
        "raters":len({r["rater_id"] for r in valid}),"scored_dimensions":scored,
        "pairs":dict(pairs),"disagreements":disagreements,"zero_scores":zeros,
        "critical_findings":[r for r in zeros if r["dimension"] in {"privacy","completion","disturbance"}],
        "by_run":{str(i):{d:{"count":len(by_run[i][d]),"mean":statistics.mean(by_run[i][d]) if by_run[i][d] else None}
                          for d in DIMENSIONS} for i in range(len(runs))},
        "by_category":{category:{str(i):{d:{"count":len(values[d]),"mean":statistics.mean(values[d]) if values[d] else None}
                        for d in DIMENSIONS} for i,values in run_values.items()} for category,run_values in by_category.items()},
        "comparisons":comparisons,"semantic_verdict":"requires_case_review_no_automatic_release"}
    return {"schema":"blind-review/v1","bundle_sha256":digest(manifest),"source":source,
            "ratings":ratings,"summary":summary}


def verify_review(cases,runs,artifact,*,seed=0):
    from .report import blind_packet, blind_manifest
    packet,key,_=blind_packet(cases,runs,seed)
    manifest=blind_manifest(cases,runs,packet,key,seed)
    actual=aggregate(cases,runs,packet,key,manifest,artifact["ratings"],source=artifact["source"])
    if actual!=artifact:
        raise ValueError("review_artifact_tampered_or_bundle_mismatched")
    return actual["summary"]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases",type=Path,default=DEFAULT_CASES)
    parser.add_argument("--runs",nargs="+",type=Path,required=True)
    parser.add_argument("--bundle",type=Path,required=True)
    parser.add_argument("--scores",nargs="+",type=Path,required=True)
    parser.add_argument("--source",choices=["human","synthetic"],required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        packet_path=args.bundle/"blind.jsonl";key_path=args.bundle/"private/blind_key.json"
        manifest_path=args.bundle/"private/blind_manifest.json"
        inputs=[args.cases,*args.runs,*args.scores,packet_path,key_path,manifest_path]
        if args.output.resolve() in {p.resolve() for p in inputs}:
            raise ValueError("review_output_overwrites_input")
        read=lambda p:json.loads(p.read_text(encoding="utf-8"))
        ratings=[row for p in args.scores for row in load_jsonl(p)]
        result=aggregate(load_jsonl(args.cases),[read(p) for p in args.runs],load_jsonl(packet_path),
                         read(key_path),read(manifest_path),ratings,source=args.source)
        write_json(args.output,result)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({k:result["summary"][k] for k in ("status","complete_candidates","candidates","raters")}))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
