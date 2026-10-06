import json, os, shutil, sys, unicodedata

BASE=os.path.dirname(os.path.abspath(__file__))
APP_DIR=os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys,"frozen",False) else BASE
PARENT=os.path.dirname(APP_DIR)
LEGACY_SHARED=os.path.join(PARENT,"黑沙航海助手_共用資料")
LOCAL_APPDATA=os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"),"AppData","Local")
PUBLIC_PLAYER_RELEASE=os.path.isfile(os.path.join(BASE,"PUBLIC_PLAYER_RELEASE.json"))
# 舊玩家繼續使用版本資料夾上一層的共用資料；全新 EXE 安裝則使用可寫入的 LocalAppData。
# 正式玩家交付版使用獨立資料區：不讀取開發者／既有玩家版的共用資料，
# 但玩家日後更新公開版時仍會沿用自己的庫存、設定與學習成果。
if os.environ.get("BDO_SAILING_SHARED_DIR"):
    SHARED=os.environ["BDO_SAILING_SHARED_DIR"]
elif PUBLIC_PLAYER_RELEASE:
    SHARED=os.path.join(LOCAL_APPDATA,"BDO_Sailing_Assistant_Player","data")
else:
    SHARED=LEGACY_SHARED if os.path.isdir(LEGACY_SHARED) else os.path.join(LOCAL_APPDATA,"BDO_Sailing_Assistant","data")
LOCAL_DATA=os.path.join(BASE,"data")

def _norm_name(value):
    return "".join(unicodedata.normalize("NFKC",str(value or "")).strip().lower().split())

def _repair_mature_identity_trust_v5906(knowledge_dir):
    """只信任人工答案，不把 V5.85.7 尚未確認的 OCR 殘字升格成成熟身份。"""
    marker=os.path.join(SHARED,"mature_identity_evidence_repair_v5906.json")
    mature_path=os.path.join(knowledge_dir,"observed_item_registry.json")
    live_path=os.path.join(SHARED,"observed_item_registry.json")
    evidence_path=os.path.join(SHARED,"ocr_evidence_identity_corrections.json")
    if os.path.exists(marker) or not (os.path.isfile(mature_path) and os.path.isfile(live_path)):return
    try:
        mature=json.load(open(mature_path,encoding="utf-8"));live=json.load(open(live_path,encoding="utf-8"))
        try:evidence=json.load(open(evidence_path,encoding="utf-8"))
        except Exception:evidence=[]
        mature_by_id={str(x.get("item_id") or ""):x for x in mature.get("items",[]) if isinstance(x,dict)}
        # 同一欄位 OCR 原文若曾指向兩個答案，保持歧義，絕不自動合併。
        raw_targets={}
        target_names={}
        for rec in evidence if isinstance(evidence,list) else []:
            if not isinstance(rec,dict) or rec.get("side") not in ("input","output"):continue
            raw=_norm_name(rec.get("raw"));iid=str(rec.get("id") or "");name=str(rec.get("name") or "").strip()
            if raw and iid and name:
                raw_targets.setdefault(raw,set()).add(iid);target_names[iid]=name
        unique={raw:next(iter(ids)) for raw,ids in raw_targets.items() if len(ids)==1}
        target_ids=set(unique.values())
        items=[dict(x) for x in live.get("items",[]) if isinstance(x,dict)]
        demoted=[];merged=[]
        # 撤銷 V5.90.2/3 的過度信任；玩家在其後親自確認的內容仍保留。
        for rec in items:
            iid=str(rec.get("item_id") or "");original=mature_by_id.get(iid) or {}
            if rec.get("identity_status")=="成熟庫已確認" and original.get("identity_status") not in ("玩家已確認","公共已確認","成熟庫已確認") and iid not in target_ids:
                rec["identity_status"]=str(original.get("identity_status") or "OCR待確認")
                if not original.get("confirmed_name"):rec.pop("confirmed_name",None)
                demoted.append(iid)
            if iid in target_ids and rec.get("identity_status") not in ("玩家已確認","公共已確認"):
                rec["identity_status"]="成熟庫已確認"
                if target_names.get(iid):rec["confirmed_name"]=target_names[iid]
        by_id={str(x.get("item_id") or ""):x for x in items}
        # 把已有人眼答案的 OCR 錯字收進正確戶籍；只刪除未經玩家確認的空殼暫存 ID。
        remove=set()
        for raw,target_id in unique.items():
            target=by_id.get(target_id)
            if not target:continue
            spelling=next((str(r.get("raw") or "").strip() for r in evidence
                           if isinstance(r,dict) and _norm_name(r.get("raw"))==raw and str(r.get("id") or "")==target_id),"")
            if spelling and raw not in {_norm_name(n) for n in target.get("names",[]) or []}:
                target.setdefault("names",[]).append(spelling);merged.append({"raw":spelling,"target_id":target_id})
            for rec in items:
                iid=str(rec.get("item_id") or "")
                if iid==target_id or rec.get("identity_status")=="玩家已確認":continue
                old_names=list(rec.get("names",[]) or [])
                rec["names"]=[n for n in old_names if _norm_name(n)!=raw]
                if old_names!=rec["names"] and not rec["names"]:remove.add(iid)
        if remove:items=[x for x in items if str(x.get("item_id") or "") not in remove]
        live["items"]=items
        tmp=live_path+".v5906tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(live,f,ensure_ascii=False,indent=2)
        os.replace(tmp,live_path)
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.90.6","demoted_false_trust":demoted,"merged_exact_ocr_aliases":merged,
                       "retired_empty_pending_ids":sorted(remove),"ambiguous_raw_count":sum(len(v)>1 for v in raw_targets.values()),
                       "policy":"成熟體系保留；只有人工答案升格，待確認OCR殘字不算成熟身份"},f,ensure_ascii=False,indent=2)
    except Exception:return

def _consolidate_duplicate_item_ids_v5907():
    """把公共種子、成熟戶籍與官方改名造成的同物異 ID 合回既有玩家穩定 ID。"""
    marker=os.path.join(SHARED,"duplicate_item_identity_consolidation_v5907.json")
    registry_path=os.path.join(SHARED,"observed_item_registry.json")
    if os.path.exists(marker) or not os.path.isfile(registry_path):return
    try:
        data=json.load(open(registry_path,encoding="utf-8"));items=[dict(x) for x in data.get("items",[]) if isinstance(x,dict)]
        by_id={str(x.get("item_id") or ""):x for x in items};redirect={}
        # 官方改名：新舊名稱是同一個 7 階品，不另發臨時 ID。
        if "ITEM_MISC_0095" in by_id:redirect["ITEM_MISC_0095"]="ITEM_7_012"
        # PUBLIC 是發布時的同一份成熟資料，不應與既有玩家 ITEM_MISC 再並存一套。
        nonpublic=[x for x in items if not str(x.get("item_id") or "").startswith("ITEM_PUBLIC_")]
        for pub in [x for x in items if str(x.get("item_id") or "").startswith("ITEM_PUBLIC_")]:
            pname=_norm_name(pub.get("confirmed_name") or "")
            candidates=[x for x in nonpublic if pname and _norm_name(x.get("confirmed_name") or "")==pname]
            if candidates:
                keep=max(candidates,key=lambda x:int(x.get("evidence_count") or 0))
                redirect[str(pub.get("item_id") or "")]=str(keep.get("item_id") or "")
        # 少量舊 OCR 錯字曾被手動當成正式名稱；若它已是另一高證據戶籍的別名，併回高證據戶籍。
        active=[x for x in nonpublic if str(x.get("item_id") or "") not in redirect]
        for rec in active:
            iid=str(rec.get("item_id") or "");cname=_norm_name(rec.get("confirmed_name") or "")
            if not cname:continue
            candidates=[x for x in active if str(x.get("item_id") or "")!=iid and
                        cname in {_norm_name(n) for n in x.get("names",[]) or []} and
                        int(x.get("evidence_count") or 0)>int(rec.get("evidence_count") or 0)]
            if candidates:
                keep=max(candidates,key=lambda x:int(x.get("evidence_count") or 0))
                redirect[iid]=str(keep.get("item_id") or "")
        def final_id(iid):
            seen=set();iid=str(iid or "")
            while iid in redirect and iid not in seen:seen.add(iid);iid=redirect[iid]
            return iid
        redirect={k:final_id(v) for k,v in redirect.items() if k!=final_id(v)}
        # 先把戶籍名稱與證據數併入保留者；官方主表 ID 不需要另建觀察戶籍。
        for old,new in redirect.items():
            src=by_id.get(old);dst=by_id.get(new)
            if not src or not dst:continue
            known={_norm_name(n) for n in dst.get("names",[]) or []}
            for name in src.get("names",[]) or []:
                if _norm_name(name) not in known:dst.setdefault("names",[]).append(name);known.add(_norm_name(name))
            dst["evidence_count"]=max(int(dst.get("evidence_count") or 0),int(src.get("evidence_count") or 0))
        data["items"]=[x for x in items if str(x.get("item_id") or "") not in redirect]
        def replace_ids(obj):
            if isinstance(obj,list):return [replace_ids(x) for x in obj]
            if isinstance(obj,dict):
                out={}
                for k,v in obj.items():
                    nk=final_id(k) if k in redirect else k
                    nv=replace_ids(v)
                    if nk in out and isinstance(out[nk],(int,float)) and isinstance(nv,(int,float)):out[nk]=max(out[nk],nv)
                    elif nk not in out or out[nk] in (None,"",0):out[nk]=nv
                return out
            return final_id(obj) if isinstance(obj,str) and obj in redirect else obj
        def clean_official_rename_conflict(obj):
            if isinstance(obj,list):
                for x in obj:clean_official_rename_conflict(x)
            elif isinstance(obj,dict):
                if str(obj.get("output_item_id") or "")=="ITEM_7_012" and _norm_name(obj.get("output_raw")) in {
                        _norm_name("塔利波咒術缸子"),_norm_name("塔利波咒術缸"),_norm_name("塔利波的魔法缸")}:
                    obj["output_name"]="塔利波咒術缸子"
                    obj["issues"]=[q for q in obj.get("issues",[]) if "OUTPUT文字身份與圖片學習衝突" not in str(q)]
                    if not obj.get("issues") and obj.get("point_id") and obj.get("input_item_id"):obj["status"]="候選可用"
                for v in obj.values():clean_official_rename_conflict(v)
        data=replace_ids(data)
        tmp=registry_path+".v5907tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
        os.replace(tmp,registry_path)
        changed_files=[]
        for name in ("ocr_evidence_identity_corrections.json","ocr_visual_item_memory.json","manual_confirmations.json",
                     "ratio_confirmations.json","ratio_manual_overrides.json","ratio_current_batch_corrections.json",
                     "current_batch_ocr.json","state.json","inventory.json","inventory_test.json","inventory_baseline.json",
                     "inventory_ledger.json","inventory_test_ledger.json"):
            path=os.path.join(SHARED,name)
            if not os.path.isfile(path):continue
            try:
                old=json.load(open(path,encoding="utf-8"));new=replace_ids(old)
                if name in ("current_batch_ocr.json","state.json"):clean_official_rename_conflict(new)
                if new!=old:
                    with open(path,"w",encoding="utf-8") as f:json.dump(new,f,ensure_ascii=False,indent=2)
                    changed_files.append(name)
            except Exception:continue
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.90.7","redirected_ids":redirect,"changed_files":changed_files,
                       "policy":"保留玩家成熟ID；PUBLIC重複戶籍合併；官方改名沿用官方ID"},f,ensure_ascii=False,indent=2)
    except Exception:return

def _next_free_misc_id(items,next_no=1):
    used={str(x.get("item_id") or "") for x in items if isinstance(x,dict)}
    no=max(1,int(next_no or 1))
    while f"ITEM_MISC_{no:04d}" in used:no+=1
    return f"ITEM_MISC_{no:04d}",no+1

def _repair_mature_registry_collisions(knowledge_dir):
    """V5.90.3：成熟庫 ID 是基準；把撞號的玩家新品搬到新 ID，不能混成同一物品。"""
    marker=os.path.join(SHARED,"mature_id_collision_repair_v5903.json")
    mature_path=os.path.join(knowledge_dir,"observed_item_registry.json")
    live_path=os.path.join(SHARED,"observed_item_registry.json")
    if os.path.exists(marker) or not (os.path.isfile(mature_path) and os.path.isfile(live_path)):return
    try:
        mature=json.load(open(mature_path,encoding="utf-8"));live=json.load(open(live_path,encoding="utf-8"))
        mature_items=[dict(x) for x in mature.get("items",[]) if isinstance(x,dict)]
        live_items=[dict(x) for x in live.get("items",[]) if isinstance(x,dict)]
        mature_by_id={str(x.get("item_id") or ""):x for x in mature_items}
        mature_name_id={_norm_name(n):str(x.get("item_id") or "") for x in mature_items for n in x.get("names",[]) if _norm_name(n)}
        live_by_id={str(x.get("item_id") or ""):x for x in live_items}
        preserved=[x for x in live_items if str(x.get("item_id") or "") not in mature_by_id]
        output=mature_items+preserved
        next_no=max(int(mature.get("next_misc_no") or 1),int(live.get("next_misc_no") or 1))
        moved=[]
        for iid,base in mature_by_id.items():
            old=live_by_id.get(iid)
            if not old:continue
            base_names={_norm_name(n) for n in base.get("names",[]) if _norm_name(n)}
            # 撞進來的名稱若本來就屬於另一個成熟 ID，教材直接改回那個成熟 ID。
            redirected={}
            for name in old.get("names",[]) or []:
                nk=_norm_name(name);target=mature_name_id.get(nk)
                if target and target!=iid:redirected.setdefault(target,[]).append(name)
            for target,names in redirected.items():
                moved.append({"old_id":iid,"new_id":target,"names":list(dict.fromkeys(names)),"kind":"restore_mature_id"})
            confirmed=str(old.get("confirmed_name") or "").strip();ck=_norm_name(confirmed)
            # 只有玩家親自確認、且不是成熟庫任何既有名稱的內容，才視為撞號新品。
            if old.get("identity_status")!="玩家已確認" or not ck or ck in base_names or ck in mature_name_id:continue
            new_id,next_no=_next_free_misc_id(output,next_no)
            extra=[n for n in old.get("names",[]) if _norm_name(n) not in mature_name_id]
            names=list(dict.fromkeys([confirmed]+extra))
            new_rec={k:v for k,v in old.items() if k not in ("item_id","names")}
            new_rec.update({"item_id":new_id,"names":names,"confirmed_name":confirmed,"identity_status":"玩家已確認"})
            output.append(new_rec);moved.append({"old_id":iid,"new_id":new_id,"names":names,"kind":"allocate_player_id"})
        # 只恢復成熟戶籍；是否可信由人工證據判定，不能把待確認 OCR 殘字全數升格。
        for rec in output:
            iid=str(rec.get("item_id") or "");original=mature_by_id.get(iid) or {}
            if iid in mature_by_id and original.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認") and rec.get("identity_status") not in ("玩家已確認","公共已確認"):
                rec["identity_status"]="成熟庫已確認"
        live["items"]=output;live["next_misc_no"]=next_no
        tmp=live_path+".v5903tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(live,f,ensure_ascii=False,indent=2)
        os.replace(tmp,live_path)
        # 只改「ID 與正式名稱同時吻合」的教材，絕不全域替換仍屬成熟物品的舊 ID。
        for filename,id_key,name_key in (("ocr_evidence_identity_corrections.json","id","name"),
                                         ("ocr_visual_item_memory.json","item_id","name")):
            path=os.path.join(SHARED,filename)
            if not os.path.isfile(path):continue
            try:data=json.load(open(path,encoding="utf-8"))
            except Exception:continue
            changed=False
            for rec in data if isinstance(data,list) else []:
                for mv in moved:
                    allowed={_norm_name(n) for n in mv["names"]}
                    if str(rec.get(id_key) or "")==mv["old_id"] and _norm_name(rec.get(name_key)) in allowed:
                        rec[id_key]=mv["new_id"];changed=True;break
            if changed:
                with open(path,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.90.3","moved_collisions":moved,"policy":"成熟ID優先；玩家新品撞號時另發ID"},f,ensure_ascii=False,indent=2)
    except Exception:return

def ensure_shared():
    os.makedirs(SHARED,exist_ok=True)
    # V5.90.1：以待確認 0/1 的 V5.85.7 玩家實測庫作為成熟大腦。
    # 這裡只併入 OCR/ID/比例/裁切知識，不含庫存、船隻、路線或視窗等個人存檔。
    knowledge_dir=os.path.join(LOCAL_DATA,"mature_knowledge")
    knowledge_marker=os.path.join(SHARED,"mature_knowledge_v5901.json")
    if os.path.isdir(knowledge_dir) and not os.path.exists(knowledge_marker):
        names=("crop_layouts.json","layout_assignments.json","manual_confirmations.json","manual_repairs.json",
               "observed_item_registry.json","ocr_aliases.json","ocr_evidence_identity_corrections.json",
               "ocr_rule_v585_migration.json","ocr_visual_item_memory.json","ocr_visual_quantity_memory.json",
               "ratio_confirmations.json","ratio_manual_overrides.json")
        imported=[]
        for name in names:
            src=os.path.join(knowledge_dir,name);dst=os.path.join(SHARED,name)
            if not os.path.isfile(src):continue
            try:
                seed=json.load(open(src,encoding="utf-8"))
                current=json.load(open(dst,encoding="utf-8")) if os.path.exists(dst) else None
                if isinstance(seed,list):
                    if not isinstance(current,list):current=[]
                    seen={json.dumps(x,ensure_ascii=False,sort_keys=True) for x in seed}
                    merged=list(seed)+[x for x in current if json.dumps(x,ensure_ascii=False,sort_keys=True) not in seen]
                elif isinstance(seed,dict):
                    if name=="observed_item_registry.json":
                        current=current if isinstance(current,dict) else {}
                        byid={str(x.get("item_id") or ""):x for x in seed.get("items",[]) if isinstance(x,dict)}
                        for rec in current.get("items",[]):
                            if not isinstance(rec,dict):continue
                            iid=str(rec.get("item_id") or "")
                            if iid in byid:
                                old=byid[iid];names2=list(dict.fromkeys((old.get("names") or [])+(rec.get("names") or [])))
                                old.update(rec);old["names"]=names2
                            else:byid[iid]=rec
                        merged=dict(seed);merged.update({k:v for k,v in current.items() if k!="items"})
                        merged["items"]=list(byid.values())
                        merged["next_misc_no"]=max(int(seed.get("next_misc_no") or 1),int(current.get("next_misc_no") or 1))
                    else:
                        merged=dict(seed)
                        if isinstance(current,dict):merged.update(current)
                else:merged=seed
                with open(dst,"w",encoding="utf-8") as f:json.dump(merged,f,ensure_ascii=False,indent=2)
                imported.append(name)
            except Exception:continue
        try:
            with open(knowledge_marker,"w",encoding="utf-8") as f:
                json.dump({"version":"V5.90.1","source":"V5.85.7玩家實測成熟庫","files":imported},f,ensure_ascii=False,indent=2)
        except Exception:pass
    # V5.90.2：V5.85.7 成熟庫中已重複使用的 ID 不可被新版重新降級為新品項。
    trust_marker=os.path.join(SHARED,"mature_identity_trust_v5902.json")
    mature_registry=os.path.join(knowledge_dir,"observed_item_registry.json")
    live_registry=os.path.join(SHARED,"observed_item_registry.json")
    if os.path.isfile(mature_registry) and os.path.isfile(live_registry) and not os.path.exists(trust_marker):
        try:
            mature=json.load(open(mature_registry,encoding="utf-8"));live=json.load(open(live_registry,encoding="utf-8"))
            mids={str(x.get("item_id") or "") for x in mature.get("items",[]) if isinstance(x,dict) and x.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認")}
            changed=[]
            for rec in live.get("items",[]):
                if str(rec.get("item_id") or "") in mids and rec.get("identity_status") not in ("玩家已確認","公共已確認","成熟庫已確認"):
                    rec["identity_status"]="成熟庫已確認";changed.append(str(rec.get("item_id") or ""))
            if changed:
                with open(live_registry,"w",encoding="utf-8") as f:json.dump(live,f,ensure_ascii=False,indent=2)
            with open(trust_marker,"w",encoding="utf-8") as f:
                json.dump({"version":"V5.90.2","trusted_ids":changed},f,ensure_ascii=False,indent=2)
        except Exception:pass
    _repair_mature_registry_collisions(knowledge_dir)
    _repair_mature_identity_trust_v5906(knowledge_dir)
    public_layout=os.path.join(LOCAL_DATA,"crop_layouts_seed.json")
    player_layout=os.path.join(SHARED,"crop_layouts.json")
    if os.path.exists(public_layout) and not os.path.exists(player_layout):
        try:shutil.copy2(public_layout,player_layout)
        except Exception:pass
    public_registry=os.path.join(LOCAL_DATA,"observed_item_registry_seed.json")
    player_registry=os.path.join(SHARED,"observed_item_registry.json")
    if os.path.exists(public_registry) and not os.path.exists(player_registry):
        try:shutil.copy2(public_registry,player_registry)
        except Exception:pass
    elif os.path.exists(public_registry) and os.path.exists(player_registry):
        try:
            seed=json.load(open(public_registry,encoding="utf-8"));current=json.load(open(player_registry,encoding="utf-8"))
            items=[x for x in current.get("items",[]) if isinstance(x,dict)]
            def norm(x):return "".join(str(x or "").strip().lower().split())
            changed=False
            for pub in seed.get("items",[]):
                pnames={norm(x) for x in pub.get("names",[]) if norm(x)}
                overlaps=[x for x in items if pnames & {norm(n) for n in x.get("names",[]) if norm(n)}]
                trusted=next((x for x in overlaps if x.get("identity_status")=="玩家已確認"),None)
                if trusted is None:
                    trusted=next((x for x in overlaps if x.get("identity_status") in ("成熟庫已確認","公共已確認")),None)
                if trusted:
                    for n in pub.get("names",[]):
                        if norm(n) not in {norm(x) for x in trusted.get("names",[])}:trusted.setdefault("names",[]).append(n);changed=True
                    continue
                if overlaps:items=[x for x in items if x not in overlaps];changed=True
                if not any(str(x.get("item_id"))==str(pub.get("item_id")) for x in items):items.append(pub);changed=True
            # 公共／成熟身份已涵蓋的 OCR 臨時錯字戶籍直接撤銷，避免後建的臨時 ID 搶走名稱索引。
            trusted_names={_norm_name(n) for x in items
                           if x.get("identity_status") in ("玩家已確認","成熟庫已確認","公共已確認")
                           for n in x.get("names",[]) if _norm_name(n)}
            cleaned=[]
            for rec in items:
                rnames={_norm_name(n) for n in rec.get("names",[]) if _norm_name(n)}
                if rec.get("identity_status") not in ("玩家已確認","成熟庫已確認","公共已確認") and rnames and rnames<=trusted_names:
                    changed=True;continue
                cleaned.append(rec)
            items=cleaned
            if changed:
                current["items"]=items;tmp=player_registry+".tmp"
                with open(tmp,"w",encoding="utf-8") as f:json.dump(current,f,ensure_ascii=False,indent=2)
                os.replace(tmp,player_registry)
        except Exception:pass
    _consolidate_duplicate_item_ids_v5907()
    public_ratios=os.path.join(BASE,"ratio_confirmations_seed.json")
    player_ratios=os.path.join(SHARED,"ratio_confirmations.json")
    if os.path.exists(public_ratios):
        try:
            seed=json.load(open(public_ratios,encoding="utf-8"))
            current=json.load(open(player_ratios,encoding="utf-8")) if os.path.exists(player_ratios) else []
            if not isinstance(seed,list):seed=[]
            if not isinstance(current,list):current=[]
            def rkey(x):
                i=x.get("identity",x) if isinstance(x,dict) else {}
                return tuple(str(i.get(k) or "") for k in ("point_id","input_item_id","output_item_id","exchange_type"))
            known={rkey(x) for x in current}
            merged=current+[x for x in seed if rkey(x) not in known]
            if merged!=current:
                with open(player_ratios,"w",encoding="utf-8") as f:json.dump(merged,f,ensure_ascii=False,indent=2)
        except Exception:pass
    # First run migration: copy the small player-owned settings if they exist locally.
    for name in ("crop_layouts.json","layout_assignments.json","state.json","ocr_aliases.json","player_settings.json","candidate_choice_log.json","manual_confirmations.json","ratio_confirmations.json","ratio_manual_overrides.json"):
        src=os.path.join(LOCAL_DATA,name); dst=os.path.join(SHARED,name)
        if os.path.exists(src) and not os.path.exists(dst):
            try: shutil.copy2(src,dst)
            except Exception: pass
    return SHARED

def shared_file(name, fallback_local=True):
    ensure_shared()
    p=os.path.join(SHARED,name)
    if os.path.exists(p) or not fallback_local:
        return p
    return os.path.join(LOCAL_DATA,name)
