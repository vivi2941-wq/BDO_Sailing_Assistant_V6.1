"""把玩家已確認的通用OCR知識去個資後升級為公開種子。

預設只產生審核報告；加 --apply 才會寫入三份公開種子。
不讀取庫存、路線、設定、原始圖片或未確認身份。
"""
import argparse
import copy
import json
import re
import unicodedata
from pathlib import Path


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return copy.deepcopy(default)


def norm(value):
    return "".join(unicodedata.normalize("NFKC", str(value or "")).strip().lower().split())


def public_number(item_id):
    match = re.fullmatch(r"ITEM_PUBLIC_(\d+)", str(item_id or ""))
    return int(match.group(1)) if match else 0


def clean_registry_record(record, item_id):
    names=[];seen=set()
    for value in [record.get("confirmed_name"), *(record.get("names") or [])]:
        value=str(value or "").strip();key=norm(value)
        if value and value not in ("?", "？") and key not in seen:
            seen.add(key);names.append(value)
    return {
        "item_id":item_id,
        "names":names,
        "kind":str(record.get("kind") or "未分類"),
        "stage":str(record.get("stage") or ""),
        "identity_status":"公共已確認",
        "confirmed_name":str(record.get("confirmed_name") or (names[0] if names else "")),
        "evidence_count":0,
    }


def build(shared_dir, project_dir):
    shared_dir=Path(shared_dir);project_dir=Path(project_dir)
    seed_registry=load_json(project_dir/"data"/"observed_item_registry_seed.json", {"items":[],"next_misc_no":1})
    player_registry=load_json(shared_dir/"observed_item_registry.json", {"items":[]})
    master=load_json(project_dir/"data"/"master_data.json", {})
    master_ids=set()
    for value in master.values():
        if isinstance(value,list):
            master_ids.update(str(x.get("id") or "") for x in value if isinstance(x,dict))

    public_items=[copy.deepcopy(x) for x in seed_registry.get("items",[]) if isinstance(x,dict)]
    name_owner={}
    for rec in public_items:
        for name in rec.get("names",[]) or []:name_owner.setdefault(norm(name),set()).add(str(rec.get("item_id") or ""))

    rejected=[];excluded=[];promoted=[];id_map={};next_no=max([public_number(x.get("item_id")) for x in public_items]+[0])+1
    candidates=sorted((x for x in player_registry.get("items",[]) if isinstance(x,dict) and
                       x.get("identity_status")=="玩家已確認" and str(x.get("item_id") or "").startswith("ITEM_MISC_")),
                      key=lambda x:(norm(x.get("confirmed_name")),str(x.get("item_id"))))
    for rec in candidates:
        names=[str(x or "").strip() for x in [rec.get("confirmed_name"),*(rec.get("names") or [])] if str(x or "").strip()]
        owners=set().union(*(name_owner.get(norm(x),set()) for x in names)) if names else set()
        if len(owners)>1:
            rejected.append({"type":"item","source_id":rec.get("item_id"),"reason":"名稱已對應多個公共身份"});continue
        if owners:
            target=next(iter(owners));id_map[str(rec.get("item_id"))]=target
            continue
        target=f"ITEM_PUBLIC_{next_no:04d}";next_no+=1
        clean=clean_registry_record(rec,target)
        if not clean["names"]:
            rejected.append({"type":"item","source_id":rec.get("item_id"),"reason":"沒有可公開的正式名稱"});continue
        public_items.append(clean);id_map[str(rec.get("item_id"))]=target;promoted.append(clean)
        for name in clean["names"]:name_owner.setdefault(norm(name),set()).add(target)

    valid_ids=master_ids|{str(x.get("item_id") or "") for x in public_items}
    aliases=load_json(project_dir/"ocr_public_alias_seed.json", [])
    alias_owner={}
    for rec in public_items:
        if isinstance(rec,dict):
            for name in rec.get("names",[]) or []:alias_owner.setdefault(norm(name),set()).add(str(rec.get("item_id") or ""))
    for rec in aliases:
        if isinstance(rec,dict):alias_owner.setdefault(norm(rec.get("alias")),set()).add(str(rec.get("id") or ""))
    corrections=load_json(shared_dir/"ocr_evidence_identity_corrections.json", [])
    alias_candidates={}
    for rec in corrections:
        if not isinstance(rec,dict) or rec.get("side") not in ("input","output"):continue
        raw=str(rec.get("raw") or "").strip();target=id_map.get(str(rec.get("id") or ""),str(rec.get("id") or ""))
        if not raw or raw in ("?","？") or target not in valid_ids:continue
        if norm(raw)==norm(rec.get("name")):continue
        if len(norm(raw))<4:
            excluded.append({"type":"alias","value":raw,"reason":"字串過短，公開後可能誤套到相似品名；保留圖示教材"});continue
        alias_candidates.setdefault(norm(raw),{"alias":raw,"targets":set()})["targets"].add(target)
    promoted_aliases=[]
    for key,data in sorted(alias_candidates.items()):
        targets=data["targets"]|alias_owner.get(key,set())
        if len(targets)!=1:
            # 單一 OCR 錯字曾被人工指向不同品項時，不適合成為全體玩家共用的
            # 強制別名；保留其他可安全升級的知識，不讓一條歧義阻擋整批封版。
            excluded.append({"type":"alias","value":data["alias"],"reason":"同一OCR原文有多個答案；不公開強制套用"});continue
        target=next(iter(targets))
        if target in alias_owner.get(key,set()):continue
        row={"object_type":"品項","alias":data["alias"],"id":target,"source":"公共OCR字典"}
        if target.startswith("ITEM_PUBLIC_"):
            target_rec=next((x for x in public_items if str(x.get("item_id") or "")==target),None)
            if target_rec is None:
                rejected.append({"type":"alias","value":data["alias"],"reason":"找不到公共身份"});continue
            target_rec.setdefault("names",[]).append(data["alias"])
        else:
            aliases.append(row)
        promoted_aliases.append(row);alias_owner.setdefault(key,set()).add(target)

    visual_seed=load_json(project_dir/"data"/"ocr_visual_item_memory_seed.json", [])
    visual_seen={(str(x.get("item_id") or ""),str(x.get("side") or ""),json.dumps(x.get("signature") or {},sort_keys=True))
                 for x in visual_seed if isinstance(x,dict)}
    sig_owner={}
    for rec in visual_seed:
        if isinstance(rec,dict):sig_owner.setdefault(json.dumps(rec.get("signature") or {},sort_keys=True),set()).add(str(rec.get("item_id") or ""))
    visual_candidates=[]
    for rec in load_json(shared_dir/"ocr_visual_item_memory.json", []):
        if not isinstance(rec,dict):continue
        target=id_map.get(str(rec.get("item_id") or ""),str(rec.get("item_id") or ""));sig=rec.get("signature")
        if target not in valid_ids or rec.get("side") not in ("input","output") or not isinstance(sig,dict):continue
        sig_key=json.dumps(sig,sort_keys=True);sig_owner.setdefault(sig_key,set()).add(target)
        visual_candidates.append((rec,target,sig_key))
    promoted_visual=[]
    for rec,target,sig_key in visual_candidates:
        if len(sig_owner[sig_key])!=1:
            rejected.append({"type":"visual","item_id":target,"reason":"同一圖示特徵對應多個身份"});continue
        key=(target,str(rec.get("side")),sig_key)
        if key in visual_seen:continue
        row={"item_id":target,"name":str(rec.get("name") or ""),"side":str(rec.get("side")),
             "stage":str(rec.get("stage") or ""),"kind":str(rec.get("kind") or "未分類"),
             "signature":copy.deepcopy(rec["signature"]),"source":"公共人工驗證教材"}
        visual_seed.append(row);promoted_visual.append(row);visual_seen.add(key)

    output_registry={"format":"BDO_PUBLIC_ITEM_REGISTRY_V1","next_misc_no":1,"items":public_items}
    report={"format":"BDO_PUBLIC_KNOWLEDGE_PROMOTION_V1","safe_to_apply":not rejected,
            "promoted_items":promoted,"id_map":id_map,"promoted_aliases":promoted_aliases,
            "promoted_visual_count":len(promoted_visual),"excluded":excluded,"rejected":rejected,
            "privacy":"輸出只含名稱、穩定ID、OCR別名與不可逆圖示特徵；不含檔名、日期、路徑、圖片、庫存、路線或設定。"}
    return report,output_registry,aliases,visual_seed


def write_json(path,data):
    with open(path,"w",encoding="utf-8") as handle:json.dump(data,handle,ensure_ascii=False,indent=2)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("shared_dir");parser.add_argument("project_dir")
    parser.add_argument("--report",default="PUBLIC_KNOWLEDGE_PROMOTION_REPORT.json")
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args();project=Path(args.project_dir).resolve()
    report,registry,aliases,visual=build(Path(args.shared_dir).resolve(),project)
    write_json(Path(args.report).resolve(),report)
    if args.apply:
        if report["rejected"]:raise SystemExit("審核有衝突，未寫入公開種子")
        write_json(project/"data"/"observed_item_registry_seed.json",registry)
        write_json(project/"ocr_public_alias_seed.json",aliases)
        write_json(project/"data"/"ocr_visual_item_memory_seed.json",visual)
    print(json.dumps({"report":str(Path(args.report).resolve()),"applied":args.apply,
                      "items":len(report["promoted_items"]),"aliases":len(report["promoted_aliases"]),
                      "visuals":report["promoted_visual_count"],"rejected":len(report["rejected"])},ensure_ascii=False,indent=2))


if __name__=="__main__":main()
