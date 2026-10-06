import hashlib
import json
import os
import re
import sys

FORBIDDEN_JSON={
    "state.json","current_batch_ocr.json","current_exchange_pool.json","exchange_pool_ui.json",
    "inventory_ledger.json","inventory_baseline.json","inventory_test.json","inventory_test_ledger.json",
    "player_settings.json","route_templates.json","route_learning_history.json","route_timing_history.json",
    "seal_suggestion_evaluations.json","player_route_ui.json","player_point_groups.json",
    "player_point_preferences.json","player_item_aliases.json","player_special_tabs.json",
    "observed_item_registry.json","manual_confirmations.json","manual_repairs.json","ocr_aliases.json",
    "crop_layouts.json","layout_assignments.json","legacy_ocr_review_queue.json","pending_search_aliases.json",
    "search_aliases.json","window_states.json","page_states.json","candidate_choice_log.json",
}


def audit(root):
    root=os.path.abspath(root);issues=[];files=[]
    for base,dirs,names in os.walk(root):
        dirs[:]=[d for d in dirs if d not in {
            "__pycache__",".build-venv","build","dist","安全備份","問題回報包"
        }]
        for name in names:
            path=os.path.join(base,name);rel=os.path.relpath(path,root).replace("\\","/");files.append(rel)
            if name in FORBIDDEN_JSON and not rel.startswith("data/mature_knowledge/"):
                issues.append(f"含玩家個人資料檔：{rel}")
            if name.lower().endswith((".json",".txt",".md",".py",".bat",".cmd",".spec")):
                try:text=open(path,"r",encoding="utf-8-sig").read()
                except Exception:continue
                if re.search(r"[A-Za-z]:\\Users\\[^\\\r\n]+",text,re.I):issues.append(f"含電腦使用者絕對路徑：{rel}")
    inv=os.path.join(root,"inventory.json")
    if not os.path.isfile(inv):issues.append("缺少乾淨 inventory.json 初始母表")
    else:
        try:
            data=json.load(open(inv,encoding="utf-8-sig"))
            nonempty=[k for k,v in data.items() if v is not None]
            if nonempty:issues.append(f"初始庫存不是全未知：{len(nonempty)}項仍有數值")
        except Exception as ex:issues.append(f"inventory.json 無法解析：{ex}")
    for required in ("app.py","data/master_data.json","data/crop_layouts_seed.json","data/observed_item_registry_seed.json","data/ocr_visual_item_memory_seed.json","data/mature_knowledge/ocr_aliases.json","data/mature_knowledge/manual_confirmations.json","data/mature_knowledge/manual_repairs.json","data/mature_knowledge/ocr_visual_item_memory.json","data/mature_knowledge/ocr_visual_quantity_memory.json","data/mature_knowledge/ratio_manual_overrides.json","ocr_public_alias_seed.json","ratio_confirmations_seed.json","assets/sailing_assistant.ico","PUBLIC_RELEASE_MANIFEST.json","PUBLIC_PLAYER_RELEASE.json","release_tools/audit_public_release.py"):
        if not os.path.isfile(os.path.join(root,required)):issues.append(f"缺少公開版必要檔案：{required}")
    return {"ok":not issues,"root":root,"file_count":len(files),"issues":issues,"files":sorted(files)}


if __name__=="__main__":
    result=audit(sys.argv[1] if len(sys.argv)>1 else ".")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["ok"] else 1)
