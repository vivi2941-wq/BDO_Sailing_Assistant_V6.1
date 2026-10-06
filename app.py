import os
import json
import difflib
import traceback
import datetime
import shutil
import copy
import re
import unicodedata
import zipfile
import platform
import sys
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog, colorchooser, ttk
from state_store import StateStore
from master_data import MasterData
from route_engine import first_unhandled_index, process_station, route_progress
from batch_service import invalidate_batch
from ocr_pipeline import OCRPipeline
from exchange_parser import ExchangeCardParser
from crop_calibrator import CropCalibrator
from layout_assigner import LayoutAssigner
from shared_paths import SHARED, ensure_shared, shared_file
from PIL import Image, ImageTk, ImageOps

BASE=os.path.dirname(os.path.abspath(__file__))
APP_ICON_PATH=os.path.join(BASE,"assets","sailing_assistant.ico")
MASCOT_IMAGE_PATH=os.path.join(BASE,"assets","sailing_assistant_icon.png")
_MASCOT_SOURCE=None
ensure_shared()

SUPPORT_BACKUP_DIR=os.path.join(SHARED,"安全備份")
SUPPORT_REPORT_DIR=os.path.join(SHARED,"問題回報包")
SUPPORT_EXCLUDED_DIRS={"安全備份","問題回報包","__pycache__"}
SUPPORT_REPORT_FILES={
    "state.json","current_batch_ocr.json","current_exchange_pool.json","exchange_pool_ui.json",
    "inventory.json","inventory_ledger.json","inventory_baseline.json","inventory_test.json",
    "inventory_test_ledger.json","inventory_mode.json","player_settings.json","route_templates.json",
    "route_learning_history.json","route_timing_history.json","seal_suggestion_evaluations.json","player_route_ui.json",
    "player_point_groups.json","player_point_preferences.json","player_item_aliases.json",
    "player_special_tabs.json","observed_item_registry.json","manual_confirmations.json",
    "manual_repairs.json","ocr_aliases.json","ratio_confirmations.json","ratio_manual_overrides.json",
    "ratio_current_batch_corrections.json","ocr_evidence_identity_corrections.json","ocr_visual_item_memory.json","ocr_visual_quantity_memory.json","ocr_rule_v585_migration.json","ocr_identity_suspects.json","crop_layouts.json","layout_assignments.json","layout_health_last.json",
    "legacy_ocr_review_queue.json","pending_search_aliases.json","search_aliases.json"}

def _support_timestamp():return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

def shared_data_health():
    """只讀檢查共用 JSON；不修檔、不猜資料。"""
    files=[];issues=[]
    try:names=sorted(os.listdir(SHARED))
    except Exception as ex:return {"ok":False,"issues":[f"無法讀取共用資料：{ex}"],"files":[]}
    for name in names:
        path=os.path.join(SHARED,name)
        if not os.path.isfile(path) or not name.lower().endswith(".json"):continue
        rec={"name":name,"size":os.path.getsize(path),"valid_json":False,"kind":""}
        try:
            with open(path,"r",encoding="utf-8") as f:data=json.load(f)
            rec["valid_json"]=True;rec["kind"]="list" if isinstance(data,list) else ("object" if isinstance(data,dict) else type(data).__name__)
            rec["records"]=len(data) if isinstance(data,(list,dict)) else None
        except Exception as ex:
            rec["error"]=str(ex);issues.append(f"{name} 無法解析：{ex}")
        files.append(rec)
    required=("state.json","player_settings.json")
    for name in required:
        if not os.path.isfile(os.path.join(SHARED,name)):issues.append(f"缺少 {name}")
    return {"ok":not issues,"checked_at":datetime.datetime.now().isoformat(timespec="seconds"),
            "shared_path":SHARED,"json_count":len(files),"issues":issues,"files":files}

def create_shared_backup(reason="手動備份"):
    """備份共用資料根目錄的 JSON；排除圖片、程式與備份自身。"""
    os.makedirs(SUPPORT_BACKUP_DIR,exist_ok=True)
    path=os.path.join(SUPPORT_BACKUP_DIR,f"航海助手共用資料備份_{_support_timestamp()}.zip")
    manifest={"format":1,"created_at":datetime.datetime.now().isoformat(timespec="seconds"),
              "reason":reason,"files":[]}
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(SHARED)):
            src=os.path.join(SHARED,name)
            if os.path.isfile(src) and name.lower().endswith(".json"):
                z.write(src,arcname=name);manifest["files"].append(name)
        z.writestr("備份說明.json",json.dumps(manifest,ensure_ascii=False,indent=2))
    return {"path":path,"count":len(manifest["files"]),"manifest":manifest}

def ensure_version_backup(version):
    """每個新版本第一次啟動前自動留一份；同版本重開不重複備份。"""
    marker=os.path.join(SHARED,".last_version_backup.json")
    try:
        with open(marker,"r",encoding="utf-8") as f:last=json.load(f)
        if str(last.get("version") or "")==str(version):return {"created":False}
    except Exception:pass
    result=create_shared_backup(f"{version} 第一次啟動自動備份")
    with open(marker,"w",encoding="utf-8") as f:
        json.dump({"version":version,"created_at":datetime.datetime.now().isoformat(timespec="seconds"),
                   "backup_path":result["path"]},f,ensure_ascii=False,indent=2)
    return {"created":True,**result}

def migrate_confirmed_duplicate_ids_v584():
    """只合併證據確定的舊臨時ID；其他疑似同名一律不自動猜。"""
    marker=os.path.join(SHARED,"identity_migration_v584.json")
    if os.path.exists(marker):return
    mapping={"ITEM_MISC_0046":"ITEM_MISC_0075","ITEM_MISC_0058":"ITEM_MISC_0065"}
    registry_path=os.path.join(SHARED,"observed_item_registry.json")
    try:
        with open(registry_path,"r",encoding="utf-8") as f:reg=json.load(f)
        byid={str(x.get("item_id") or ""):x for x in reg.get("items",[]) if isinstance(x,dict)}
        if not all(old in byid and new in byid and byid[new].get("identity_status")=="玩家已確認" for old,new in mapping.items()):return
        ensure_version_backup("V5.84.0_ID合併前")
        def replace(obj):
            if isinstance(obj,str):return mapping.get(obj,obj)
            if isinstance(obj,list):return [replace(x) for x in obj]
            if isinstance(obj,dict):
                out={}
                for k,v in obj.items():
                    nk=mapping.get(k,k);nv=replace(v)
                    if nk in out and isinstance(out[nk],(int,float)) and isinstance(nv,(int,float)):out[nk]+=nv
                    elif nk not in out:out[nk]=nv
                return out
            return obj
        for name in os.listdir(SHARED):
            path=os.path.join(SHARED,name)
            if not os.path.isfile(path) or not name.lower().endswith(".json") or path==marker:continue
            try:
                with open(path,"r",encoding="utf-8") as f:data=replace(json.load(f))
                if path==registry_path:
                    merged={};ordered=[]
                    for rec in data.get("items",[]):
                        iid=str(rec.get("item_id") or "")
                        if iid not in merged:merged[iid]=rec;ordered.append(rec);continue
                        dst=merged[iid]
                        dst["names"]=list(dict.fromkeys((dst.get("names") or [])+(rec.get("names") or [])))
                        dst["evidence_count"]=int(dst.get("evidence_count") or 0)+int(rec.get("evidence_count") or 0)
                        if rec.get("identity_status")=="玩家已確認":dst.update({k:v for k,v in rec.items() if k not in ("names","evidence_count")})
                    data["items"]=ordered
                tmp=path+".v584tmp"
                with open(tmp,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
                os.replace(tmp,path)
            except Exception:continue
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.84.0","merged":mapping,"note":"只合併玩家已確認的明確重複ID"},f,ensure_ascii=False,indent=2)
    except Exception:return

def archive_legacy_context_rules_v585():
    """封存會跨刷新覆蓋答案的舊整列規則；原檔也保留，但新版不再讀取套用。"""
    marker=os.path.join(SHARED,"ocr_rule_v585_migration.json")
    if os.path.exists(marker):return
    try:
        ensure_version_backup("V5.85.0_OCR規則整理前")
        folder=os.path.join(SHARED,"舊規則封存");os.makedirs(folder,exist_ok=True)
        stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S");archived=[]
        for name in ("manual_repairs.json","ratio_manual_overrides.json"):
            src=os.path.join(SHARED,name)
            if not os.path.isfile(src):continue
            dst=os.path.join(folder,f"{stamp}_{name}");shutil.copy2(src,dst)
            try:
                with open(src,"r",encoding="utf-8") as f:data=json.load(f)
                count=len(data) if isinstance(data,(list,dict)) else None
            except Exception:count=None
            archived.append({"file":name,"records":count,"archive":dst})
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.85.0","created_at":datetime.datetime.now().isoformat(timespec="seconds"),
                       "archived":archived,"policy":"舊整列名稱修正與永久動態比例不再參與辨識；名稱改為欄位獨立，比例改為本輪OCR。"},
                      f,ensure_ascii=False,indent=2)
    except Exception:return

def inspect_shared_backup(path):
    """還原前先驗證：只接受根目錄 JSON，不允許路徑穿越。"""
    valid=[];issues=[]
    try:
        with zipfile.ZipFile(path,"r") as z:
            for info in z.infolist():
                name=info.filename
                if name=="備份說明.json":continue
                if "/" in name or "\\" in name or not name.lower().endswith(".json"):
                    issues.append(f"不允許的項目：{name}");continue
                try:json.loads(z.read(info).decode("utf-8"));valid.append(name)
                except Exception as ex:issues.append(f"{name} 不是有效JSON：{ex}")
    except Exception as ex:issues.append(f"無法開啟備份：{ex}")
    return {"ok":bool(valid) and not issues,"files":valid,"issues":issues}

def restore_shared_backup(path):
    check=inspect_shared_backup(path)
    if not check["ok"]:return {"ok":False,**check}
    safety=create_shared_backup("還原前自動備份")
    try:
        with zipfile.ZipFile(path,"r") as z:
            for name in check["files"]:
                temp=os.path.join(SHARED,name+".restore_tmp")
                with open(temp,"wb") as f:f.write(z.read(name))
                os.replace(temp,os.path.join(SHARED,name))
        return {"ok":True,"count":len(check["files"]),"safety_backup":safety["path"]}
    except Exception as ex:return {"ok":False,"issues":[str(ex)],"safety_backup":safety["path"]}

def create_support_bundle(version=""):
    os.makedirs(SUPPORT_REPORT_DIR,exist_ok=True)
    health=shared_data_health();stamp=_support_timestamp()
    path=os.path.join(SUPPORT_REPORT_DIR,f"航海助手_AI自助求救包_{stamp}.zip")
    summary={"format":2,"created_at":datetime.datetime.now().isoformat(timespec="seconds"),
             "app_version":version,"python":sys.version.split()[0],"platform":platform.platform(),
             "health":health,"included":[],
             "privacy":"不含原始截圖、OCR裁切圖片、航海助手程式或備份檔。"}
    file_roles={
        "state.json":"目前批次、路線、站點狀態與畫面工作進度；不是正式庫存帳本。",
        "current_batch_ocr.json":"本輪OCR結構化結果；可能包含待確認或低信心資料。",
        "current_exchange_pool.json":"由本輪OCR整理出的交換池。",
        "inventory.json":"正式庫存目前值。未知值不等於0。",
        "inventory_ledger.json":"正式庫存異動帳本；完成、撤銷、手動調整與出售應可追溯。",
        "inventory_baseline.json":"玩家盤點後建立的正式庫存驗算起點。",
        "inventory_test.json":"測試沙盒庫存，不能當正式庫存。",
        "inventory_test_ledger.json":"測試沙盒帳本。",
        "player_settings.json":"玩家設定；包含顯示、船隻負重等，不是官方資料。",
        "route_templates.json":"玩家路線點位骨架，不應綁死某一刷的交換品。",
        "route_learning_history.json":"海豹學到的玩家排路線習慣。",
        "route_timing_history.json":"有效／無效航行時間與航段樣本。",
        "seal_suggestion_evaluations.json":"海豹草稿從預覽、套用到玩家最後儲存的品質驗收紀錄；不含庫存異動。",
        "ocr_aliases.json":"玩家確認過、指向穩定ID的OCR誤讀別名。",
        "observed_item_registry.json":"非內建物品的本地身份登記；ITEM_MISC流水號只在此玩家環境有效。",
        "manual_confirmations.json":"玩家人工確認紀錄，可能保留當時問題理由。",
        "manual_repairs.json":"人工修復記憶；不得脫離上下文亂套。",
        "crop_layouts.json":"玩家手動校正的A／B裁切版型。",
        "layout_assignments.json":"圖片檔名對A／B版型的玩家指派。"}
    ai_rules={
        "role":"你是黑沙航海助手的資料診斷員。先分析與說明，不直接改檔。",
        "core_rules":[
            "OCR、匯入、排路線不能修改正式庫存；只有完成、明確手動調整或出售才能入帳。",
            "撤銷必須使用反向差額，不能用歷史before快照覆蓋目前庫存。",
            "未知庫存不等於0；問號與低信心OCR不能猜成正式答案。",
            "ITEM_ID、POINT_ID是身份；玩家縮寫與X/Z/G等分類不是官方資料。",
            "超重只提醒，不阻止路線；缺重量時只能說已知至少重量。",
            "問題包不含原始圖片或程式原始碼，不能假裝看過不存在的證物或程式。"],
        "forbidden_first_actions":[
            "不要叫玩家清空共用資料、刪除JSON、重設庫存或重裝程式。",
            "不要把重跑OCR當成第一答案；先確認現有證物與資料是否足夠。",
            "不要自行合併、改寫或發放ITEM_ID／POINT_ID。",
            "不要直接提供會覆蓋帳本或庫存的修檔內容。"],
        "triage":[
            "先讀問題回報摘要與健康檢查，確認是否有JSON損壞或缺檔。",
            "請玩家用一句話說明：在哪一頁、按了什麼、預期什麼、實際發生什麼。",
            "資料問題用穩定ID串起state、inventory與ledger；畫面名稱只作輔助。",
            "OCR或裁切問題若需要看畫面，明確請玩家另附那張截圖，不要求整批重跑。",
            "若只能判定為程式BUG，整理版本、最短重現步驟、相關檔名與仍缺的證據，交回開發者。"],
        "file_roles":file_roles}
    ai_guide=(f"# 黑沙航海助手 AI 自助客服說明\n\n"
              f"你正在分析 {version or '未知版本'} 產生的自助求救包。請先讀 `AI診斷規則.json` 與 `問題回報摘要.json`。\n\n"
              "## 回答方式\n\n1. 先用白話說目前看見的證據。\n2. 區分『確定異常』『可能原因』『資料不足』。\n"
              "3. 一次只請玩家做一個安全檢查。\n4. 未經玩家要求，不產生會直接覆蓋JSON的修復檔。\n"
              "5. 如果是程式BUG，輸出一段可直接交給開發者的回報摘要。\n\n"
              "## 重要限制\n\n這個ZIP沒有原始截圖，也沒有程式原始碼。不要聲稱已看過畫面或找到程式哪一行；需要時請玩家補一張相關截圖。\n")
    prompt=("請依照壓縮包內的《AI自助客服說明》與《AI診斷規則》分析我的黑沙航海助手問題。\n"
            "先不要叫我清空資料、重裝、重跑OCR或直接修改JSON。請先讀健康檢查與相關資料，告訴我你能確定什麼、還缺什麼。\n\n"
            "我遇到的問題是：（請在這裡補上在哪一頁、按了什麼、預期結果和實際結果）\n")
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        for name in sorted(SUPPORT_REPORT_FILES):
            src=os.path.join(SHARED,name)
            if name=="current_batch_ocr.json":
                # OCR會同時留一份本版本診斷檔；求救包選時間較新者，避免撿到舊批次。
                local=os.path.join(BASE,"data","ocr_structured_debug.json")
                candidates=[x for x in (src,local) if os.path.isfile(x)]
                if candidates:src=max(candidates,key=os.path.getmtime)
                summary["current_batch_source"]=("latest_local_ocr" if src==local else "shared_current_batch")
            if os.path.isfile(src):z.write(src,arcname=f"資料/{name}");summary["included"].append(name)
        z.writestr("問題回報摘要.json",json.dumps(summary,ensure_ascii=False,indent=2))
        z.writestr("AI自助客服說明.md",ai_guide)
        z.writestr("AI診斷規則.json",json.dumps(ai_rules,ensure_ascii=False,indent=2))
        z.writestr("給ChatGPT的提問.txt",prompt)
        z.writestr("玩家請先看我.txt","把這個ZIP上傳到你自己的ChatGPT，再貼上《給ChatGPT的提問.txt》並補寫遇到的問題。若問題與畫面或裁切有關，請另外附上那一張相關截圖。\n")
    return {"path":path,"included":len(summary["included"]),"health":health}

MASCOT_QUIPS={
    "home":["今天風向不錯，適合出航，也適合假裝很忙。","船長早安！海豹已就位，咖啡請自備。","先看交換，再看海；順序反了容易發呆。"],
    "advanced":["這裡是工具間，不是海豹的零食櫃。","正常航行用不到我；出事時我假裝很可靠。","救援工具很多，希望今天一個都用不到。"],
    "import":["這一刷有幾頁就匯幾張，海豹負責數。","A版B版要分清，海豹的左右鰭倒是分不清。","OCR努力看圖，船長負責看它有沒有亂講。"],
    "settings":["船可以很多艘，設定記得按儲存。","這裡改習慣，不改官方世界觀。","數字慢慢填，海豹不催；填錯才會叫。"],
    "custom":["你的分類你作主，海豹只負責點頭。","名字可以自訂，問號不能當永久居民。","分頁排好，找交換就不用海上撈針。"],
    "inventory":["庫存可以對帳，失蹤的記憶比較難。","多一個要記，少一個更要記。","海豹沒有私吞，只是幫你保管到忘記。"],
    "evidence":["證物在此，OCR請準備答辯。","原圖不說謊，只是偶爾字長得很像。","看清楚再改，海豹拒絕屈打成招。"],
    "ocr":["看不懂就問，不准拿問號混過去。","低信心不是沒信心，只是想請船長看一眼。","OCR今天有戴眼鏡，希望有用。"],
    "edit":["一字之差，戶籍可能差十萬八千海里。","改名可以，別把金魚改成鯊魚。","確認完這筆，它下次就會乖一點。"],
    "picker":["海豹只負責推薦，最後勾哪些船長說了算。","庫存不足先別慌，看看前站會不會生出來。","今天的交換每天變，船長的直覺依然很忙。"],
    "planner":["路線可以重排，人生先不用。","拖得動就是路線，拖不動可能是海豹。","先排順路，再排心情；海豹建議先排順路。"],
    "routes":["先跑哪條都行，別先跑去看片。","每張卡都是一趟，別把自己也卡住。","綠燈出航，黃燈想想，海豹燈負責可愛。"],
    "runner":["自動導航不是自動回神，切出去看片要小心。","完成再按完成，海豹不接受意念交換。","風浪交給船，庫存交給我，發呆交給你。"],
}

def add_mascot(parent,bg="#fffef9",size=78,opacity=0.16,side="right",padx=8,pady=0,quip_key=None):
    """海豹＋可點擊俏皮話；只住標題區，不遮住工作區。"""
    global _MASCOT_SOURCE
    try:
        if _MASCOT_SOURCE is None:_MASCOT_SOURCE=Image.open(MASCOT_IMAGE_PATH).convert("RGBA")
        im=_MASCOT_SOURCE.copy();im.thumbnail((int(size),int(size)),Image.Resampling.LANCZOS)
        alpha=im.getchannel("A").point(lambda x:int(x*max(0.0,min(1.0,float(opacity)))))
        im.putalpha(alpha)
        photo=ImageTk.PhotoImage(im)
        label=tk.Label(parent,image=photo,bg=bg,bd=0,highlightthickness=0)
        label.image=photo;label.pack(side=side,padx=padx,pady=pady)
        quips=MASCOT_QUIPS.get(str(quip_key or ""),[])
        if quips:
            seed=sum(ord(c) for c in (datetime.date.today().isoformat()+str(quip_key)))%len(quips)
            state={"i":seed};qv=tk.StringVar(value=quips[seed])
            bubble=tk.Label(parent,textvariable=qv,font=(FONT,10,"bold"),bg=bg,fg="#607168",
                            justify="left",wraplength=(245 if size>=120 else 190),cursor="hand2")
            bubble.pack(side=side,padx=4,pady=pady)
            def next_quip(_event=None):
                state["i"]=(state["i"]+1)%len(quips);qv.set(quips[state["i"]])
            label.configure(cursor="hand2");label.bind("<Button-1>",next_quip);bubble.bind("<Button-1>",next_quip)
            label.mascot_bubble=bubble;label.mascot_quip_var=qv
        return label
    except Exception:return None
CURRENT_BATCH_PATH=shared_file("current_batch_ocr.json", fallback_local=False)
CURRENT_POOL_PATH=shared_file("current_exchange_pool.json", fallback_local=False)
POOL_UI_PATH=shared_file("exchange_pool_ui.json", fallback_local=False)
SEARCH_ALIAS_PATH=shared_file("search_aliases.json", fallback_local=False)
PENDING_ALIAS_PATH=shared_file("pending_search_aliases.json", fallback_local=False)
ITEM_REGISTRY_PATH=shared_file("observed_item_registry.json", fallback_local=False)
LEGACY_REVIEW_PATH=shared_file("legacy_ocr_review_queue.json", fallback_local=False)
ROUTE_TIMING_HISTORY_PATH=shared_file("route_timing_history.json", fallback_local=False)
ROUTE_LEARNING_PATH=shared_file("route_learning_history.json", fallback_local=False)
SEAL_EVALUATION_PATH=shared_file("seal_suggestion_evaluations.json", fallback_local=False)
STORE=StateStore(shared_file("state.json", fallback_local=False))

def normalize_point_identity(row):
    """V5.64｜官方點位身份證清洗：官方名稱明確時修復歷史撞號 ID。"""
    if not isinstance(row,dict): return row
    name=str(row.get("point_name") or "").strip()
    pid=str(row.get("point_id") or "").strip()
    if name=="阿利赫恣村莊" and pid=="POINT_G_001":
        row["point_id"]="POINT_HIGHG_001"
    elif name=="哈科班" and pid=="POINT_G_002":
        row["point_id"]="POINT_HIGHG_002"
    elif name=="格蘭迪哈" and pid=="POINT_Z_002":
        row["point_id"]="POINT_HIGHZ_002"
    return row

def normalize_stage_input_quantity(row):
    """階段品旁的小數字是持有量；路線單次投入固定為1，不能拿它鎖交換次數。"""
    if not isinstance(row,dict):return False
    try:stage=int(row.get("input_stage") or 0)
    except Exception:stage=0
    kind=str(row.get("input_kind") or "")
    # 有明確 kind 時只信 kind；一般材料戶籍為分類帶著 stage=1，仍不是段位品。
    if kind=="段位品" or (not kind and 1<=stage<=7):
        try:old=int(row.get("input_quantity") or 1)
        except Exception:old=1
        if old!=1:
            row["input_quantity"]=1
            return True
    return False

def is_normal_stage_exchange(row):
    """只有兩側都明確是段位品才進 1→2、2→3…分頁；一般材料→1段屬特殊。"""
    try:a=int(row.get("input_stage") or 0);b=int(row.get("output_stage") or 0)
    except Exception:a=b=0
    return (str(row.get("input_kind") or "")=="段位品" and
            str(row.get("output_kind") or "")=="段位品" and
            a in range(1,8) and b in range(1,8))
MASTER=MasterData(os.path.join(BASE,"data","master_data.json"), shared_file("ocr_aliases.json", fallback_local=False),
                  os.path.join(BASE,"ocr_public_alias_seed.json"))
KNOWLEDGE_PACK_DIR=os.path.join(SHARED,"辨識知識包")

def build_ocr_knowledge_pack(master=None):
    """只輸出能對到內建正式ID的OCR誤讀別名；不輸出任何玩家操作資料。"""
    master=master or MASTER;rows=[];seen=set();skipped=0
    for rec in list(getattr(master,"user_aliases",[]) or []):
        if not isinstance(rec,dict):skipped+=1;continue
        typ="點位" if rec.get("object_type") in ("POINT","點位") else "品項"
        alias=str(rec.get("alias") or "").strip();target=str(rec.get("id") or "").strip()
        valid=(getattr(master,"point_by_id",{}) if typ=="點位" else getattr(master,"item_by_id",{}))
        if (not alias or "?" in alias or "？" in alias or len(alias)>120 or target not in valid):skipped+=1;continue
        key=(typ,"".join(unicodedata.normalize("NFKC",alias).split()).lower(),target)
        if key in seen:continue
        seen.add(key);target_name=str((valid.get(target) or {}).get("name") or "")
        rows.append({"object_type":typ,"alias":alias,"target_id":target,"target_name":target_name})
    rows.sort(key=lambda x:(x["object_type"],x["target_id"],x["alias"]))
    return {"format":"BDO_OCR_KNOWLEDGE_V1","created_at":datetime.datetime.now().isoformat(timespec="seconds"),
            "aliases":rows,"excluded":skipped,
            "privacy":"只含正式ID與OCR別名；不含庫存、路線、設定、縮寫、分類、圖片或來源路徑。"}

def inspect_ocr_knowledge_pack(data,master=None):
    master=master or MASTER;valid=[];invalid=[];conflicts=[];existing=[]
    if not isinstance(data,dict) or data.get("format")!="BDO_OCR_KNOWLEDGE_V1":
        return {"ok":False,"error":"不是航海助手辨識知識包 V1","valid":[]}
    aliases=data.get("aliases")
    if not isinstance(aliases,list) or len(aliases)>5000:
        return {"ok":False,"error":"別名清單格式錯誤或數量異常","valid":[]}
    seen=set()
    for i,rec in enumerate(aliases,1):
        if not isinstance(rec,dict):invalid.append(f"第{i}筆格式錯誤");continue
        typ=str(rec.get("object_type") or "");alias=str(rec.get("alias") or "").strip();target=str(rec.get("target_id") or "").strip()
        if typ not in ("品項","點位") or not alias or len(alias)>120 or "?" in alias or "？" in alias:
            invalid.append(f"第{i}筆內容不合規");continue
        targets=master.point_by_id if typ=="點位" else master.item_by_id
        if target not in targets:invalid.append(f"{alias}：本版沒有正式ID {target}");continue
        norm=master._n(alias);key=(typ,norm)
        if key in seen:continue
        seen.add(key);old=master.alias_map.get(key)
        if old and old!=target:conflicts.append({"alias":alias,"old":old,"new":target});continue
        row={"object_type":typ,"alias":alias,"target_id":target}
        if old==target:existing.append(row)
        else:valid.append(row)
    return {"ok":True,"valid":valid,"existing":existing,"conflicts":conflicts,"invalid":invalid,
            "total":len(aliases)}

def import_ocr_knowledge_pack(data,master=None,make_backup=True):
    master=master or MASTER;check=inspect_ocr_knowledge_pack(data,master)
    if not check.get("ok"):return check
    backup_path=""
    if make_backup:
        try:backup_path=create_shared_backup("匯入OCR辨識知識包前自動備份")["path"]
        except Exception as ex:return {"ok":False,"error":f"匯入前備份失敗：{ex}"}
    added=0;errors=[]
    for rec in check["valid"]:
        ok,msg=master.add_user_alias(rec["object_type"],rec["alias"],rec["target_id"])
        if ok:added+=1
        else:errors.append(f"{rec['alias']}：{msg}")
    return {**check,"ok":not errors,"added":added,"errors":errors,"backup_path":backup_path}

def _load_item_registry():
    try:
        with open(ITEM_REGISTRY_PATH,"r",encoding="utf-8") as f:
            d=json.load(f)
        if isinstance(d,dict):
            d.setdefault("items",[])
            d.setdefault("next_misc_no",1)
            return d
    except Exception:
        pass
    return {"items":[],"next_misc_no":1}

def _save_item_registry(d):
    os.makedirs(os.path.dirname(ITEM_REGISTRY_PATH),exist_ok=True)
    with open(ITEM_REGISTRY_PATH,"w",encoding="utf-8") as f:
        json.dump(d,f,ensure_ascii=False,indent=2)

def _norm_identity_name(x):
    # 和主資料使用同一套繁簡比對鍵，避免「楓樹合板／枫樹合板」被發成兩個ID。
    return MASTER._n(unicodedata.normalize("NFKC",str(x or "")).lower())

def _find_registry_item(name):
    nk=_norm_identity_name(name)
    if not nk:return None
    for rec in _load_item_registry().get("items",[]):
        if isinstance(rec,dict) and any(_norm_identity_name(n)==nk for n in rec.get("names",[]) or []):
            return rec
    return None

def _confirm_new_item_identity(name,kind="",stage="",source_file="",side="",preferred_id=""):
    """玩家看過證物並輸入正式名稱後，建立或確認一個穩定物品 ID。"""
    name=str(name or "").strip()
    if not name or name in ("?","？"):return None,False
    reg=_load_item_registry();items=reg.get("items",[]);nk=_norm_identity_name(name)
    rec=None
    if preferred_id:
        rec=next((x for x in items if isinstance(x,dict) and str(x.get("item_id") or "")==str(preferred_id)),None)
    for x in items:
        if isinstance(x,dict) and any(_norm_identity_name(n)==nk for n in x.get("names",[]) or []):
            rec=x;break
    created=False
    if rec is None:
        no=int(reg.get("next_misc_no",1));reg["next_misc_no"]=no+1
        rec={"item_id":f"ITEM_MISC_{no:04d}","names":[name],"kind":kind or "未分類",
             "stage":str(stage or ""),"first_seen_source":str(source_file or ""),
             "first_seen_side":str(side or ""),"evidence_count":1}
        items.append(rec);created=True
    elif not any(_norm_identity_name(n)==nk for n in rec.get("names",[]) or []):
        rec.setdefault("names",[]).append(name)
    rec["identity_status"]="玩家已確認"
    rec["confirmed_name"]=name
    rec["confirmed_at"]=datetime.datetime.now().isoformat(timespec="seconds")
    if kind:rec["kind"]=kind
    if stage not in (None,""):rec["stage"]=str(stage)
    reg["items"]=items;_save_item_registry(reg)
    return rec,created

def confirm_row_item_identity(row,side):
    """名稱正確時一鍵確認：保留玩家版流程，同時補齊文字與圖示學習。"""
    if side not in ("input","output") or not isinstance(row,dict):return {"ok":False,"error":"欄位格式錯誤"}
    raw=_recover_field_raw(row,side);name=str(row.get(f"{side}_name") or raw).strip()
    if not name or name in ("?","？"):return {"ok":False,"error":"正式名稱不可空白"}
    iid=str(row.get(f"{side}_item_id") or "");existing=_find_registry_item(name)
    if existing and existing.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認"):
        rec=existing
    else:
        rec,_=_confirm_new_item_identity(name,row.get(f"{side}_kind",""),row.get(f"{side}_stage",""),
                                         row.get("source_file",""),side,iid if iid.startswith("ITEM_MISC_") else "")
    if not rec:return {"ok":False,"error":"無法建立物品身份"}
    row[f"{side}_item_id"]=str(rec.get("item_id") or "");row[f"{side}_name"]=str(rec.get("confirmed_name") or name)
    row[f"{side}_kind"]=str(rec.get("kind") or row.get(f"{side}_kind") or "未分類")
    row[f"{side}_stage"]=str(rec.get("stage") or "")
    row.setdefault("identity_source",{})[side]="玩家人工確認"
    row["issues"]=[q for q in row.get("issues",[]) if f"{side.upper()}新身份待確認" not in str(q)
                   and f"{side.upper()}品項未匹配ITEM_ID" not in str(q)]
    save_evidence_identity_correction(row,side);learned=learn_visual_item(row,side)
    if not row.get("issues") and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):
        row["status"]="候選可用"
    return {"ok":True,"item_id":row[f"{side}_item_id"],"name":row[f"{side}_name"],"image_learned":bool(learned)}

def _register_observed_nonstage_items(rows):
    """
    V5.47 戶籍制度：
    已經被結構化 OCR 明確讀成「物品名稱」的非1~7階物品，不再因為玩家不關心而保持無ID。
    1~7階仍沿用既有 MASTER ID，不在這裡另發證，避免把OCR誤字複製成新段位品。
    """
    reg=_load_item_registry()
    items=reg.get("items",[])
    by_name={}
    for rec in items:
        if not isinstance(rec,dict): continue
        for nm in rec.get("names",[]) or []:
            n=_norm_identity_name(nm)
            if n: by_name[n]=rec

    changed=False
    assigned=0
    for r in rows or []:
        if not isinstance(r,dict): continue
        for side in ("input","output"):
            idkey=f"{side}_item_id"
            namekey=f"{side}_name"
            rawkey=f"{side}_raw"
            kindkey=f"{side}_kind"
            stagekey=f"{side}_stage"

            name=str(r.get(namekey) or "").strip()
            raw=str(r.get(rawkey) or "").strip()
            kind=str(r.get(kindkey) or "").strip()
            stage=r.get(stagekey)

            # 必須已經有結構化名稱；純 raw OCR 字串不直接發身份證。
            # 箭頭／加號／裝飾符號不是物品名稱；不得替它們發永久臨時 ID。
            if not name or name in ("?","？"):
                continue
            if not re.search(r"[\u3400-\u9fffA-Za-z0-9]",name):
                r[namekey]="";r[rawkey]="";r[idkey]=""
                r.setdefault("issues",[])
                r["issues"]=[q for q in r["issues"] if f"{side.upper()}新身份待確認" not in str(q)
                             and f"{side.upper()}品項未匹配ITEM_ID" not in str(q)]
                note=f"{side.upper()}區無法判定"
                if note not in r["issues"]:r["issues"].append(note)
                r["status"]="待確認"
                continue

            if str(r.get(idkey) or "").strip():
                continue

            # 1~7階品仍由既有品項主表負責身份，避免誤字自動生新ID。
            if kind=="段位品" or str(stage).strip() in tuple(str(i) for i in range(1,8)):
                continue

            nk=_norm_identity_name(name)
            rec=by_name.get(nk)
            if rec is None:
                no=int(reg.get("next_misc_no",1))
                iid=f"ITEM_MISC_{no:04d}"
                reg["next_misc_no"]=no+1
                rec={
                    "item_id":iid,
                    "names":[name],
                    "kind":kind or "未分類",
                    "first_seen_source":str(r.get("source_file") or ""),
                    "first_seen_side":side,
                    "evidence_count":0,
                    "identity_status":"OCR待確認"
                }
                items.append(rec)
                by_name[nk]=rec
                changed=True
            iid=str(rec.get("item_id") or "")
            if iid:
                r[idkey]=iid
                assigned+=1
                rec["evidence_count"]=int(rec.get("evidence_count") or 0)+1
                if kind and not rec.get("kind"):
                    rec["kind"]=kind
                changed=True

    reg["items"]=items
    if changed:
        _save_item_registry(reg)
    return assigned

def scan_legacy_ocr_folder(root):
    """只讀掃描舊 OCR 結構檔；不碰庫存、路線或帳本。"""
    names={"ocr_structured_debug.json","current_batch_ocr.json"}
    paths=[]
    for base,dirs,files in os.walk(root):
        dirs[:]=[d for d in dirs if d not in ("__pycache__","黑沙航海助手_共用資料")]
        for fn in files:
            if fn in names:paths.append(os.path.join(base,fn))
        if len(paths)>=500:break
    rows=[];bad_files=[]
    for path in paths:
        try:
            with open(path,"r",encoding="utf-8") as f:data=json.load(f)
            part=data if isinstance(data,list) else data.get("rows",[]) if isinstance(data,dict) else []
            if isinstance(part,list):
                for pos,x in enumerate(part,1):
                    if not isinstance(x,dict):continue
                    rec=copy.deepcopy(x)
                    rec["_legacy_json_path"]=path;rec["_legacy_position"]=pos;rec["_legacy_scan_root"]=root
                    rec["_legacy_missing_input_id"]=not bool(rec.get("input_item_id"))
                    rec["_legacy_missing_output_id"]=not bool(rec.get("output_item_id"))
                    rows.append(rec)
        except Exception:bad_files.append(path)
    named=set();unknown=0;stage_missing=set()
    for r in rows:
        for side in ("input","output"):
            if r.get(f"{side}_item_id"):continue
            nm=str(r.get(f"{side}_name") or r.get(f"{side}_raw") or "").strip()
            if not nm:continue
            if nm in ("?","？"):
                unknown+=1;continue
            kind=str(r.get(f"{side}_kind") or "");stage=str(r.get(f"{side}_stage") or "")
            if kind=="段位品" or stage in tuple(str(i) for i in range(1,8)):
                stage_missing.add(nm)
            else:named.add(nm)
    return {"paths":paths,"rows":rows,"bad_files":bad_files,"named":sorted(named),
            "unknown":unknown,"stage_missing":sorted(stage_missing)}

def load_legacy_review_queue():
    try:
        with open(LEGACY_REVIEW_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,list) else []
    except Exception:return []

def save_legacy_review_queue(rows):
    os.makedirs(os.path.dirname(LEGACY_REVIEW_PATH),exist_ok=True)
    with open(LEGACY_REVIEW_PATH,"w",encoding="utf-8") as f:json.dump(rows,f,ensure_ascii=False,indent=2)

def update_legacy_review_queue(scanned_rows):
    old=load_legacy_review_queue()
    by_key={str(x.get("key")):x for x in old if isinstance(x,dict) and x.get("key")}
    for r in scanned_rows:
        for side in ("input","output"):
            if not r.get(f"_legacy_missing_{side}_id"):continue
            name=str(r.get(f"{side}_name") or r.get(f"{side}_raw") or "").strip()
            kind=str(r.get(f"{side}_kind") or "");stage=str(r.get(f"{side}_stage") or "")
            if not name:continue
            if name in ("?","？"):reason="名稱是問號"
            elif kind=="段位品" or stage in tuple(str(i) for i in range(1,8)):
                reason="段位品主表找不到ID"
            else:reason="OCR名稱已補待確認ID"
            key="|".join((str(r.get("_legacy_json_path") or ""),str(r.get("row_index") or r.get("_legacy_position") or ""),side))
            current=by_key.get(key,{})
            current.update({"key":key,"status":current.get("status") or "待處理","side":side,
                "reason":reason,"observed_name":name,"source_file":str(r.get("source_file") or ""),
                "json_path":str(r.get("_legacy_json_path") or ""),"scan_root":str(r.get("_legacy_scan_root") or ""),
                "row_index":r.get("row_index") or r.get("_legacy_position"),"point_name":str(r.get("point_name") or ""),
                "input_name":str(r.get("input_name") or r.get("input_raw") or ""),
                "output_name":str(r.get("output_name") or r.get("output_raw") or ""),
                "kind":kind,"stage":stage,"item_id":str(r.get(f"{side}_item_id") or ""),"row":r})
            by_key[key]=current
    rows=list(by_key.values());rows.sort(key=lambda x:(x.get("status")!="待處理",x.get("json_path",""),str(x.get("row_index",""))))
    save_legacy_review_queue(rows)
    return rows

def _promote_pending_aliases(rows):
    """待確認縮寫一旦對應名稱已取得ID，就自動升格到正式搜尋縮寫。"""
    try:
        with open(PENDING_ALIAS_PATH,"r",encoding="utf-8") as f:
            pending=json.load(f)
        if not isinstance(pending,list) or not pending:
            return 0
    except Exception:
        return 0

    name_to_id={}
    for r in rows or []:
        if not isinstance(r,dict): continue
        for side in ("input","output"):
            nm=str(r.get(f"{side}_name") or "").strip()
            iid=str(r.get(f"{side}_item_id") or "").strip()
            if nm and iid:
                name_to_id[_norm_identity_name(nm)]=(nm,iid)

    try:
        with open(SEARCH_ALIAS_PATH,"r",encoding="utf-8") as f:
            aliases=json.load(f)
        if not isinstance(aliases,dict): aliases={}
    except Exception:
        aliases={}
    aliases.setdefault("items",{})
    aliases.setdefault("points",{})

    keep=[]; moved=0
    for x in pending:
        if not isinstance(x,dict) or x.get("kind")!="item":
            keep.append(x); continue
        hit=name_to_id.get(_norm_identity_name(x.get("seen_name")))
        if not hit:
            keep.append(x); continue
        nm,_iid=hit
        aa=str(x.get("alias") or "").strip()
        if not aa:
            continue
        targets=aliases["items"].get(aa,[])
        if not isinstance(targets,list): targets=[targets]
        if nm not in targets: targets.append(nm)
        aliases["items"][aa]=targets
        moved+=1

    if moved:
        os.makedirs(os.path.dirname(SEARCH_ALIAS_PATH),exist_ok=True)
        with open(SEARCH_ALIAS_PATH,"w",encoding="utf-8") as f:
            json.dump(aliases,f,ensure_ascii=False,indent=2)
        with open(PENDING_ALIAS_PATH,"w",encoding="utf-8") as f:
            json.dump(keep,f,ensure_ascii=False,indent=2)
    return moved

def _identity_census(rows):
    """只回報玩家需要知道的戶籍結果；細節留地下室。"""
    before=_load_item_registry()
    before_ids={str(x.get("item_id") or "") for x in before.get("items",[]) if isinstance(x,dict)}
    assigned=_register_observed_nonstage_items(rows)
    _promote_pending_aliases(rows)
    after=_load_item_registry()
    after_items=[x for x in after.get("items",[]) if isinstance(x,dict)]
    after_ids={str(x.get("item_id") or "") for x in after_items}
    new_ids=after_ids-before_ids

    unresolved=[]
    seen_ids=set()
    for r in rows or []:
        if not isinstance(r,dict): continue
        for side in ("input","output"):
            nm=str(r.get(f"{side}_name") or r.get(f"{side}_raw") or "").strip()
            iid=str(r.get(f"{side}_item_id") or "").strip()
            kind=str(r.get(f"{side}_kind") or "").strip()
            stage=str(r.get(f"{side}_stage") or "").strip()
            if iid:
                seen_ids.add(iid)
                continue
            if not nm or nm in ("?","？"):
                continue
            # 段位品無ID屬於需要人工確認：可能OCR誤字、官方改名或主表漏項。
            # 非段位物品正常已由戶籍制度自動補證；若仍無ID也列為需要確認。
            unresolved.append({
                "name":nm,
                "side":side,
                "kind":kind,
                "stage":stage,
                "source_file":str(r.get("source_file") or "")
            })

    # 去重：玩家只需看每個名稱一次。
    uniq={}
    for x in unresolved:
        uniq.setdefault((x["name"],x["kind"],x["stage"]),x)
    unresolved=list(uniq.values())

    return {
        "known_in_batch":len(seen_ids),
        "registry_total":len(after_items),
        "auto_new":len(new_ids),
        "auto_assigned_fields":assigned,
        "need_review":len(unresolved),
        "unresolved":unresolved,
    }

def _show_identity_census(parent, rows):
    result=_identity_census(rows)
    w=tk.Toplevel(parent)
    w.title("戶籍檢查｜V5.47")
    smart_window(w,"identity_census","700x520",650,480)
    w.minsize(620,440)

    tk.Label(w,text="物品戶籍檢查",font=(FONT,20,"bold")).pack(pady=(14,4))
    tk.Label(w,text="地下室自己盤點；只有真的需要你判斷的才往前台送。",
             font=(FONT,11)).pack(pady=(0,12))

    cards=tk.Frame(w);cards.pack(fill="x",padx=18,pady=4)
    vals=[
        ("本輪已認得",result["known_in_batch"]),
        ("本次自動補建",result["auto_new"]),
        ("需要你確認",result["need_review"]),
    ]
    for i,(lab,val) in enumerate(vals):
        f=tk.LabelFrame(cards,text=lab,font=(FONT,11,"bold"),padx=12,pady=8)
        f.grid(row=0,column=i,sticky="nsew",padx=5)
        tk.Label(f,text=str(val),font=(FONT,24,"bold")).pack()
        cards.columnconfigure(i,weight=1)

    if result["need_review"]==0:
        tk.Label(w,text="✓ 戶籍正常，這輪不用你處理任何東西。",
                 font=(FONT,14,"bold")).pack(pady=28)
        tk.Label(w,text=f"地下室目前另有 {result['registry_total']} 個非1～7階永久物品身份。",
                 font=(FONT,10)).pack()
    else:
        tk.Label(w,text="只有下面這些需要肉眼看一下：",font=(FONT,12,"bold")).pack(anchor="w",padx=20,pady=(18,5))
        lf=tk.Frame(w);lf.pack(fill="both",expand=True,padx=20,pady=(0,8))
        lb=tk.Listbox(lf,font=(FONT,11))
        sb=tk.Scrollbar(lf,orient="vertical",command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        for x in result["unresolved"]:
            extra=" / ".join(y for y in (x.get("kind",""),("第"+x["stage"]+"階") if x.get("stage") else "") if y)
            lb.insert("end",f"{x['name']}" + (f"　[{extra}]" if extra else ""))

        tk.Label(w,text="這些不在戶籍頁直接亂發ID；回OCR確認頁時再用「舊東西改名／真的新東西」處理。",
                 font=(FONT,10)).pack(padx=20,pady=(0,6))

    tk.Button(w,text="關閉",font=(FONT,11,"bold"),command=w.destroy,padx=18,pady=6).pack(pady=(6,14))
    return result

PLAYER_SETTINGS_PATH=shared_file("player_settings.json", fallback_local=False)
WINDOW_STATE_PATH=shared_file("window_states.json", fallback_local=False)

CANDIDATE_LOG_PATH=shared_file("candidate_choice_log.json", fallback_local=False)

MANUAL_CONFIRM_PATH=shared_file("manual_confirmations.json", fallback_local=False)
MANUAL_REPAIR_PATH=shared_file("manual_repairs.json", fallback_local=False)

def load_manual_repairs():
    try:
        with open(MANUAL_REPAIR_PATH,"r",encoding="utf-8") as f:
            x=json.load(f)
        return x if isinstance(x,list) else []
    except Exception:return []

def save_manual_repairs(data):
    try:
        os.makedirs(os.path.dirname(MANUAL_REPAIR_PATH),exist_ok=True)
        with open(MANUAL_REPAIR_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception:pass

def save_current_batch_rows(rows):
    """人工確認／修正後立刻寫回本輪資料，避免畫面已改但換頁後又回到舊狀態。"""
    try:
        if isinstance(rows,list):
            kept=[];exact={};by_point={}
            for r in rows:
                if not isinstance(r,dict):kept.append(r);continue
                fp=tuple(str(r.get(k) or "") for k in ("point_id","input_item_id","output_item_id","input_quantity","output_quantity","remaining_times","exchange_type"))
                if all(fp[:3]) and fp in exact:
                    first=exact[fp]
                    first.setdefault("overlap_duplicate_evidence",[]).append({"source_file":r.get("source_file"),"row_index":r.get("row_index")})
                    continue
                exact[fp]=r;kept.append(r)
                pid=str(r.get("point_id") or "")
                if pid:by_point.setdefault(pid,[]).append(r)
            # 同一 POINT_ID 卻是不同交換內容時不猜哪筆對；兩筆都留證物並阻擋。
            for pid,group in by_point.items():
                cores={(str(x.get("input_item_id") or ""),str(x.get("output_item_id") or "")) for x in group}
                if len(cores)>1:
                    for x in group:
                        x.setdefault("issues",[])
                        if "同一交換點出現不同交換內容，需人工確認" not in x["issues"]:x["issues"].append("同一交換點出現不同交換內容，需人工確認")
                        x["status"]="待確認"
            rows[:]=kept
        os.makedirs(os.path.dirname(CURRENT_BATCH_PATH),exist_ok=True)
        tmp=CURRENT_BATCH_PATH+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:
            json.dump(rows,f,ensure_ascii=False,indent=2)
        os.replace(tmp,CURRENT_BATCH_PATH)
        return True
    except Exception:
        return False

def sync_planned_route_identity_after_edit(state_data,before,after):
    """人工改正 OCR 身份時只更新同一證物的未完成站，不重排路線、不碰已入帳站。"""
    if not isinstance(state_data,dict) or not isinstance(before,dict) or not isinstance(after,dict):return {"updated":0,"completed_skipped":0}
    def evidence_key(x):
        source=os.path.basename(str(x.get("source_file") or ""));row=str(x.get("row_index") or "")
        return (source,row) if source and row else None
    old_ev=evidence_key(before)
    old_core=(str(before.get("point_id") or ""),str(before.get("input_item_id") or ""),str(before.get("output_item_id") or ""))
    stations=[]
    for route in state_data.get("routes",[]) or []:stations.extend(route.get("stations",[]) or [])
    stations.extend(state_data.get("unplanned_stations",[]) or [])
    matches=[st for st in stations if old_ev and evidence_key(st)==old_ev]
    if not matches:
        fallback=[st for st in stations if (str(st.get("point_id") or ""),str(st.get("input_id") or ""),str(st.get("output_id") or ""))==old_core]
        matches=fallback if len(fallback)==1 else []
    updated=0;skipped=0
    for st in matches:
        if st.get("status")=="完成" or st.get("inventory_tx_id"):
            skipped+=1;continue
        st.update({"point":after.get("point_name") or st.get("point"),"point_id":after.get("point_id") or st.get("point_id"),
                   "input":after.get("input_name") or st.get("input"),"input_id":after.get("input_item_id") or st.get("input_id"),
                   "input_qty":after.get("input_quantity") or st.get("input_qty") or 1,"input_stage":after.get("input_stage"),
                   "output":after.get("output_name") or st.get("output"),"output_id":after.get("output_item_id") or st.get("output_id"),
                   "output_qty":after.get("output_quantity") or st.get("output_qty") or 1,"output_stage":after.get("output_stage"),
                   "source_file":after.get("source_file") or st.get("source_file"),"row_index":after.get("row_index")})
        updated+=1
    # 今日交換選擇的勾選鍵也跟著搬，避免修正後看起來突然取消選取。
    picker=state_data.get("route_picker") if isinstance(state_data.get("route_picker"),dict) else None
    if picker:
        def row_key(x):return "|".join(str(x.get(k) or "") for k in ("point_id","input_item_id","output_item_id","source_file","row_index"))
        old_key=row_key(before);new_key=row_key(after)
        if old_key!=new_key:
            for field in ("selected","selected_times"):
                values=picker.get(field)
                if isinstance(values,dict) and old_key in values:
                    values[new_key]=values.pop(old_key)
    return {"updated":updated,"completed_skipped":skipped}

def _repair_key(r,fld):
    p=str(r.get("point_id") or ""); i=str(r.get("input_item_id") or ""); o=str(r.get("output_item_id") or "")
    e=str(r.get("exchange_type") or "")
    if fld=="point" and i and o:return ("point",i,o,e)
    if fld=="input" and p and o:return ("input",p,o,e)
    if fld=="output" and p and i:return ("output",p,i,e)
    return None

def remember_manual_repairs(before,after):
    """V5.85.1：手動改物品是本張圖片答案；永久別名只走明確的學習入口。"""
    changed=[]
    for fld,idkey,namekey in (("point","point_id","point_name"),("input","input_item_id","input_name"),("output","output_item_id","output_name")):
        old=str(before.get(idkey) or ""); new=str(after.get(idkey) or "")
        old_name=str(before.get(namekey) or "");new_name=str(after.get(namekey) or "")
        if (old==new and old_name==new_name) or not new:continue
        raw=str(before.get(f"{fld}_raw") or old_name or "").strip()
        if not raw:continue
        if fld=="point":ok,_=MASTER.add_user_alias("點位",raw,new)
        else:ok=False
        if ok:changed.append(fld)
    return changed

def apply_manual_repairs(rows):
    # 舊版的 repair key 綁整列，是蘆薈／高級厚毛皮互相覆蓋的根因。
    # 檔案保留作證物，但自 V5.85 起完全不再套用。
    return rows

def reconcile_row_identities_independently(rows):
    """用每個欄位自己的原文重新核對；不參考同列另外兩欄。"""
    repaired=0;flagged=0
    for r in rows or []:
        point_raw=str(r.get("point_raw") or "").strip()
        if not point_raw:
            for t in r.get("raw_texts",[]) or []:
                if str(t).startswith("POINT:"):point_raw=str(t)[6:].split("|")[0].strip();break
        if point_raw:
            phit=MASTER.resolve_point(point_raw)
            if phit.get("matched") and phit.get("record"):
                r["point_id"]=phit["record"]["id"];r["point_name"]=phit["record"]["name"]
        for side in ("input","output"):
            raw=str(r.get(f"{side}_raw") or "").strip()
            if not raw:continue
            learned_source=str((r.get("identity_source") or {}).get(side) or "")
            if (learned_source.startswith("圖片學習") or learned_source.startswith("已確認OCR錯字")
                    or learned_source.startswith("已確認名稱的一字誤讀") or learned_source.startswith("本張圖片人工確認")):
                continue
            hit=MASTER.resolve_item(raw);rec=None
            if hit.get("matched") and hit.get("record"):
                rec={"item_id":hit["record"]["id"],"name":hit["record"]["name"],
                     "stage":hit["record"].get("stage"),"kind":"段位品" if hit["record"].get("stage") not in (None,"") else "一般材料",
                     "trusted":True}
            else:
                old=_find_registry_item(raw)
                if old:rec={"item_id":old.get("item_id"),"name":old.get("confirmed_name") or (old.get("names") or [raw])[0],
                            "stage":old.get("stage"),"kind":old.get("kind") or "未分類",
                            "trusted":old.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認")}
            if rec and rec.get("item_id"):
                if str(r.get(f"{side}_item_id") or "")!=str(rec["item_id"]):repaired+=1
                r[f"{side}_item_id"]=rec["item_id"];r[f"{side}_name"]=rec["name"]
                r[f"{side}_stage"]=str(rec.get("stage") or "")
                r[f"{side}_kind"]=rec.get("kind") or r.get(f"{side}_kind")
                if rec.get("trusted"):
                    r["issues"]=[q for q in r.get("issues",[]) if
                                 f"{side.upper()}品項未匹配ITEM_ID" not in str(q)
                                 and f"{side.upper()}新身份待確認" not in str(q)]
                else:
                    note=f"{side.upper()}新身份待確認";r.setdefault("issues",[])
                    if note not in r["issues"]:r["issues"].append(note)
                    r["status"]="待確認";flagged+=1
            elif r.get("_manual_repair_memory") and side.upper() in (r.get("_manual_repair_memory") or []):
                r.setdefault("issues",[])
                note=f"{side.upper()}曾受舊整列記憶影響，請依原圖確認"
                if note not in r["issues"]:r["issues"].append(note)
                r["status"]="待確認";flagged+=1
        r.pop("_manual_repair_memory",None)
        if not r.get("issues") and r.get("point_id") and r.get("input_item_id") and r.get("output_item_id"):
            r["status"]="候選可用"
    return {"repaired":repaired,"flagged":flagged}

def _add_registry_name_alias(item_id,alias):
    reg=_load_item_registry();items=reg.get("items",[]);key=_norm_identity_name(alias)
    if not key:return False
    for rec in items:
        for nm in rec.get("names",[]) or []:
            if _norm_identity_name(nm)==key and str(rec.get("item_id") or "")!=str(item_id):return False
    rec=next((x for x in items if str(x.get("item_id") or "")==str(item_id)),None)
    if not rec:return False
    if alias not in (rec.get("names") or []):rec.setdefault("names",[]).append(alias);_save_item_registry(reg)
    return True

def apply_learned_aliases_to_rows(rows):
    """把玩家已確認的 OCR 別名直接套回本輪；不必依賴重新跑 parser 才看得到。"""
    for r in rows or []:
        if not isinstance(r,dict):continue
        # POINT 原文住在 raw_texts；物品原文有獨立 raw 欄位。
        raw_point=""
        for text in r.get("raw_texts",[]) or []:
            if str(text).startswith("POINT:"):
                raw_point=str(text)[6:].split("|")[0].strip();break
        if raw_point:
            hit=MASTER.resolve_point(raw_point)
            if hit.get("matched") and hit.get("record"):
                r["point_id"]=hit["record"]["id"]
                r["point_name"]=hit["record"]["name"]
        for side in ("input","output"):
            raw=str(r.get(f"{side}_raw") or r.get(f"{side}_name") or "").strip()
            if not raw:continue
            hit=MASTER.resolve_item(raw)
            if not hit.get("matched") or not hit.get("record"):continue
            # 玩家學過的別名優先於 OCR 自動建立的臨時 ITEM_MISC 身份。
            if hit.get("via")=="別名" or not r.get(f"{side}_item_id"):
                rec=hit["record"]
                r[f"{side}_item_id"]=rec["id"]
                r[f"{side}_name"]=rec["name"]
                stage=rec.get("stage")
                r[f"{side}_stage"]=str(stage) if stage not in (None,"") else ""
                r[f"{side}_kind"]="段位品" if stage not in (None,"") else (r.get(f"{side}_kind") or "一般材料")
        if r.get("point_id") and r.get("input_item_id") and r.get("output_item_id"):
            r["issues"]=[q for q in r.get("issues",[]) if not any(x in str(q) for x in
                         ("POINT區無法判定","POINT區未匹配","INPUT區無法判定","OUTPUT區無法判定",
                          "品項未匹配ITEM_ID","未識別","低信心"))]
            if not r["issues"]:r["status"]="候選可用"

EVIDENCE_WINDOW_PATH=shared_file("evidence_window_state.json", fallback_local=False)

def load_evidence_window_state():
    try:
        with open(EVIDENCE_WINDOW_PATH,"r",encoding="utf-8") as f:
            d=json.load(f)
            return d if isinstance(d,dict) else {}
    except Exception:return {}

def save_evidence_window_state(win):
    try:
        os.makedirs(os.path.dirname(EVIDENCE_WINDOW_PATH),exist_ok=True)
        with open(EVIDENCE_WINDOW_PATH,"w",encoding="utf-8") as f:
            json.dump({"geometry":win.geometry(),"state":win.state()},f,ensure_ascii=False,indent=2)
    except Exception:pass


RATIO_CONFIRM_PATH=shared_file("ratio_confirmations.json", fallback_local=False)
RATIO_OVERRIDE_PATH=shared_file("ratio_manual_overrides.json", fallback_local=False)
RATIO_BATCH_PATH=shared_file("ratio_current_batch_corrections.json", fallback_local=False)
IDENTITY_SUSPECT_PATH=shared_file("ocr_identity_suspects.json", fallback_local=False)
VISUAL_QUANTITY_MEMORY_PATH=shared_file("ocr_visual_quantity_memory.json", fallback_local=False)
EVIDENCE_IDENTITY_PATH=shared_file("ocr_evidence_identity_corrections.json", fallback_local=False)
VISUAL_ITEM_MEMORY_PATH=shared_file("ocr_visual_item_memory.json", fallback_local=False)
PUBLIC_VISUAL_ITEM_MEMORY_PATH=os.path.join(BASE,"data","ocr_visual_item_memory_seed.json")
_FIELD_EVIDENCE_FILE_CACHE={}

def archive_bad_output_quantity_memory_v5855():
    """V5.85.3/4的OUTPUT_QTY框偏右；舊圖像特徵不可與新框混用。"""
    folder=os.path.dirname(VISUAL_QUANTITY_MEMORY_PATH)
    marker=os.path.join(folder,"output_qty_crop_v5855_migration.json")
    if os.path.exists(marker):return False
    try:
        os.makedirs(folder,exist_ok=True)
        archived="";count=0
        if os.path.isfile(VISUAL_QUANTITY_MEMORY_PATH):
            try:
                with open(VISUAL_QUANTITY_MEMORY_PATH,"r",encoding="utf-8") as f:data=json.load(f)
                count=len(data) if isinstance(data,list) else 0
            except Exception:data=[]
            archived=os.path.join(folder,"ocr_visual_quantity_memory_V5853_BAD_CROP_ARCHIVE.json")
            shutil.copy2(VISUAL_QUANTITY_MEMORY_PATH,archived)
        with open(VISUAL_QUANTITY_MEMORY_PATH,"w",encoding="utf-8") as f:json.dump([],f,ensure_ascii=False,indent=2)
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.85.5","archived_records":count,"archive":archived,
                       "note":"只重建OUTPUT數字圖像教材；物品ID、名稱、庫存與本圖人工比例均保留。"},f,ensure_ascii=False,indent=2)
        return True
    except Exception:return False

def archive_icon_bound_quantity_memory_v5856():
    """V5.85.5的簽名仍夾帶物品圖示；改用純數字簽名後舊教材只封存不混用。"""
    folder=os.path.dirname(VISUAL_QUANTITY_MEMORY_PATH)
    marker=os.path.join(folder,"output_qty_digit_only_v5856_migration.json")
    if os.path.exists(marker):return False
    try:
        os.makedirs(folder,exist_ok=True);count=0;archived=""
        if os.path.isfile(VISUAL_QUANTITY_MEMORY_PATH):
            try:
                with open(VISUAL_QUANTITY_MEMORY_PATH,"r",encoding="utf-8") as f:data=json.load(f)
                count=len(data) if isinstance(data,list) else 0
            except Exception:data=[]
            archived=os.path.join(folder,"ocr_visual_quantity_memory_V5855_ICON_BOUND_ARCHIVE.json")
            shutil.copy2(VISUAL_QUANTITY_MEMORY_PATH,archived)
        with open(VISUAL_QUANTITY_MEMORY_PATH,"w",encoding="utf-8") as f:json.dump([],f,ensure_ascii=False,indent=2)
        with open(marker,"w",encoding="utf-8") as f:
            json.dump({"version":"V5.90.9","archived_records":count,"archive":archived,
                       "note":"重建為只比對數字字形，不再綁物品圖示。"},f,ensure_ascii=False,indent=2)
        return True
    except Exception:return False

def _field_evidence_path(row,side):
    evidence=row.get("evidence") if isinstance(row.get("evidence"),dict) else {}
    fp=str(evidence.get(f"{side}_image") or row.get(f"{side}_image") or "")
    if fp and os.path.isfile(fp):return fp
    # 跨版本沿用資料時，把舊路徑的 data 後半段接到目前程式資料夾。
    normalized=fp.replace("\\","/")
    marker="/data/"
    pos=normalized.lower().rfind(marker)
    if pos>=0:
        rel=normalized[pos+1:].replace("/",os.sep)
        alt=os.path.join(BASE,rel)
        if os.path.isfile(alt):return alt
    # V5.85.1留下的圖片級修正尚未保存切片路徑；從相鄰舊版找原切片一次。
    source=os.path.basename(str(row.get("source_file") or ""));idx=str(row.get("row_index") or "")
    wanted=f"{source}.ROW{idx}.{side.upper()}.png" if source and idx else ""
    if wanted:
        if wanted in _FIELD_EVIDENCE_FILE_CACHE:return _FIELD_EVIDENCE_FILE_CACHE[wanted]
        for root in (os.path.dirname(BASE),os.path.dirname(SHARED)):
            if not os.path.isdir(root):continue
            try:
                for dp,dirs,files in os.walk(root):
                    dirs[:]=[d for d in dirs if d not in ("安全備份","問題回報包","__pycache__","build","dist")]
                    if wanted in files:
                        found=os.path.join(dp,wanted);_FIELD_EVIDENCE_FILE_CACHE[wanted]=found;return found
            except Exception:pass
        _FIELD_EVIDENCE_FILE_CACHE[wanted]=""
    return ""

def _visual_item_signature(row,side):
    """只取物品欄左側圖示，文字讀不到時仍有可學的外觀證據。"""
    fp=_field_evidence_path(row,side)
    if not fp:return None
    try:
        im=Image.open(fp).convert("RGB")
        w,h=im.size
        if w<8 or h<8:return None
        # INPUT/OUTPUT切片的物品圖示都在左側；避開右方品名與數量。
        icon=im.crop((max(0,int(w*.01)),max(0,int(h*.04)),max(2,int(w*.30)),max(2,int(h*.96))))
        gray=icon.convert("L").resize((17,16),Image.Resampling.LANCZOS)
        px=list(gray.getdata());bits=0
        for y in range(16):
            for x in range(16):bits=(bits<<1)|(1 if px[y*17+x]>px[y*17+x+1] else 0)
        color=icon.resize((8,8),Image.Resampling.LANCZOS)
        color_hex="".join(f"{r//16:x}{g//16:x}{b//16:x}" for r,g,b in color.getdata())
        return {"dhash":f"{bits:064x}","color":color_hex}
    except Exception:return None

def load_visual_item_memory():
    data=[]
    for priority,path in enumerate((PUBLIC_VISUAL_ITEM_MEMORY_PATH,VISUAL_ITEM_MEMORY_PATH)):
        try:
            with open(path,"r",encoding="utf-8") as f:part=json.load(f)
            if isinstance(part,list):
                for x in part:
                    if isinstance(x,dict):rec=dict(x);rec["_memory_priority"]=priority;data.append(rec)
        except Exception:pass
    chosen={}
    for rec in data:
        key=(str(rec.get("side") or ""),_norm_identity_name(rec.get("name")),
             json.dumps(rec.get("signature") or {},sort_keys=True,ensure_ascii=False))
        old=chosen.get(key)
        if old is None or int(rec.get("_memory_priority",0))>int(old.get("_memory_priority",0)):chosen[key]=rec
    out=[]
    for rec in chosen.values():rec=dict(rec);rec.pop("_memory_priority",None);out.append(rec)
    return out

def learn_visual_item(row,side):
    if side not in ("input","output"):return False
    sig=_visual_item_signature(row,side);iid=str(row.get(f"{side}_item_id") or "")
    name=str(row.get(f"{side}_name") or "").strip()
    if not sig or not iid or not name or name in ("?","？"):return False
    data=load_visual_item_memory()
    if any(x.get("item_id")==iid and x.get("side")==side and x.get("signature")==sig for x in data if isinstance(x,dict)):
        return True
    data.append({"item_id":iid,"name":name,"side":side,"stage":str(row.get(f"{side}_stage") or ""),
                 "kind":str(row.get(f"{side}_kind") or ""),"signature":sig,
                 "learned_from":os.path.basename(str(row.get("source_file") or "")),
                 "learned_at":datetime.datetime.now().isoformat(timespec="seconds")})
    os.makedirs(os.path.dirname(VISUAL_ITEM_MEMORY_PATH),exist_ok=True)
    with open(VISUAL_ITEM_MEMORY_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
    return True

def _visual_signature_score(a,b):
    try:
        xor=int(a["dhash"],16)^int(b["dhash"],16)
        gray=1-(xor.bit_count()/256)
        ca=[int(x,16) for x in a["color"]];cb=[int(x,16) for x in b["color"]]
        color=1-(sum(abs(x-y) for x,y in zip(ca,cb))/(max(1,len(ca))*15))
        return gray*.72+color*.28,gray,color
    except Exception:return 0.0,0.0,0.0

def apply_visual_item_learning(rows):
    memory=[x for x in load_visual_item_memory() if isinstance(x,dict)]
    if not memory:return 0
    registry_by_id={str(x.get("item_id") or ""):x for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
    confirmed_raw=build_confirmed_raw_identity_map()
    applied=0
    for row in rows or []:
        for side in ("input","output"):
            raw=str(row.get(f"{side}_raw") or row.get(f"{side}_name") or "").strip()
            sig=_visual_item_signature(row,side)
            if not sig:continue
            # 同一物品可以有很多張教材；先依ITEM_ID分組，不能讓自己的第二張圖變成競爭對手。
            grouped={}
            for rec in memory:
                if rec.get("side")!=side:continue
                score,gray,color=_visual_signature_score(sig,rec.get("signature") or {})
                iid=str(rec.get("item_id") or "")
                if iid and (iid not in grouped or score>grouped[iid][0]):grouped[iid]=(score,gray,color,rec)
            scored=list(grouped.values())
            scored.sort(key=lambda x:x[0],reverse=True)
            if not scored:continue
            best=scored[0];second=scored[1][0] if len(scored)>1 else 0.0
            # 門檻故意嚴格；看不準就留給玩家，不讓一個圖示污染別的物品。
            if best[0]<.91 or best[1]<.89 or best[2]<.82 or best[0]-second<.035:continue
            rec=best[3]
            current_id=str(row.get(f"{side}_item_id") or "");visual_id=str(rec.get("item_id") or "")
            if current_id and current_id==visual_id:
                # ID 合併或官方改名後，舊批次可能還留著已失效的衝突文字。
                row["issues"]=[q for q in row.get("issues",[]) if
                               f"{side.upper()}文字身份與圖片學習衝突" not in str(q)]
            if current_id and current_id!=visual_id:
                current_registry=registry_by_id.get(current_id) or {}
                # 正式主表或玩家親自確認過的文字身份不能被圖片靜默推翻；衝突交回人工看圖。
                current_is_trusted=(current_id in MASTER.item_by_id or current_registry.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認"))
                if current_is_trusted:
                    taught=confirmed_raw.get((side,_norm_identity_name(raw)))
                    if not taught or str(taught.get("id") or "")!=visual_id:
                        note=f"{side.upper()}文字身份與圖片學習衝突，請看原圖"
                        row.setdefault("issues",[])
                        if note not in row["issues"]:row["issues"].append(note)
                        row["status"]="待確認";continue
                # OCR自動建立的臨時錯字ID可信度較低，讓玩家教過的圖片身份優先。
            row[f"{side}_item_id"]=rec.get("item_id");row[f"{side}_name"]=rec.get("name")
            row[f"{side}_stage"]=str(rec.get("stage") or "");row[f"{side}_kind"]=str(rec.get("kind") or "未分類")
            row.setdefault("identity_source",{})[side]=(f"圖片學習＋人工確認 {best[0]:.0%}"
                if current_id and current_id!=visual_id else f"圖片學習 {best[0]:.0%}")
            row["issues"]=[q for q in row.get("issues",[]) if side.upper() not in str(q) and "品項未匹配ITEM_ID" not in str(q)]
            if not row["issues"] and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):
                row["status"]="候選可用"
            applied+=1
    return applied

def bootstrap_visual_memory_from_evidence_corrections():
    """把 V5.85.1 已經人工確認的圖片答案升級成視覺教材，不叫玩家重打。"""
    learned=0
    for rec in load_evidence_identity_corrections():
        if not isinstance(rec,dict) or rec.get("side") not in ("input","output"):continue
        side=rec["side"]
        row={"source_file":rec.get("source_file"),"row_index":rec.get("row_index"),
             f"{side}_raw":rec.get("raw"),f"{side}_item_id":rec.get("id"),
             f"{side}_name":rec.get("name"),f"{side}_stage":rec.get("stage"),f"{side}_kind":rec.get("kind")}
        if learn_visual_item(row,side):learned+=1
    return learned

def _evidence_identity_key(row,side):
    """一張來源圖片的一列一欄，才是一筆人工看圖答案。"""
    raw=_recover_field_raw(row,side)
    return (os.path.basename(str(row.get("source_file") or "")),
            str(row.get("row_index") or ""),str(side),_norm_identity_name(raw))

def _recover_field_raw(row,side):
    """raw空白時從本列OCR原文找回該欄，不讓人工確認變成無字教材。"""
    raw=str(row.get(f"{side}_raw") or row.get("raw") or "").strip()
    if raw:return raw
    prefix=f"{str(side).upper()}:"
    for text in row.get("raw_texts",[]) or []:
        value=str(text)
        if value.startswith(prefix):return value[len(prefix):].split("|")[0].strip()
    return ""

def load_evidence_identity_corrections():
    try:
        with open(EVIDENCE_IDENTITY_PATH,"r",encoding="utf-8") as f:data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception:return []

def build_ocr_learning_history(records=None):
    """把欄位級人工答案整理成玩家看得懂的修正歷程。"""
    records=load_evidence_identity_corrections() if records is None else records
    events=[]
    for rec in records or []:
        if not isinstance(rec,dict) or rec.get("side") not in ("input","output"):continue
        raw=str(rec.get("raw") or "").strip();iid=str(rec.get("id") or "");name=str(rec.get("name") or "").strip()
        if not iid or not name:continue
        # OCR完全空白也是一次重要的人工修正；舊版因 raw 為空而整筆從履歷消失。
        if not raw:raw="（OCR完全沒讀到）"
        events.append({"raw":raw,"raw_key":_norm_identity_name(raw),"id":iid,"name":name,
                       "side":rec.get("side"),"source_file":os.path.basename(str(rec.get("source_file") or "")),
                       "row_index":rec.get("row_index"),"confirmed_at":str(rec.get("confirmed_at") or "")})
    events.sort(key=lambda x:(x["confirmed_at"],x["source_file"],str(x["row_index"])))
    groups={}
    for event in events:
        group=groups.setdefault(event["id"],{"id":event["id"],"name":event["name"],"events":[],"variants":{}})
        group["name"]=event["name"] or group["name"];group["events"].append(event)
        variant=group["variants"].setdefault(event["raw_key"],{"raw":event["raw"],"count":0})
        variant["count"]+=1
    result=[]
    for group in groups.values():
        variants=list(group["variants"].values());parts=[]
        for no,event in enumerate(group["events"],1):
            where=f"（{event['source_file']} 第{event['row_index']}列）" if event["source_file"] else ""
            parts.append(f"第 {no} 次：OCR 讀成「{event['raw']}」，人工確認為「{group['name']}」{where}")
        distinct=len(variants);total=len(group["events"]);repeated=sum(max(0,x["count"]-1) for x in variants)
        if distinct>1:
            verdict=f"這 {total} 次包含 {distinct} 種不同 OCR 讀法，所以不是一直修改同一種錯誤。"
        elif repeated:
            verdict=f"同一個 OCR 讀法已確認 {total} 次；若之後仍再出現，代表這條學習可能沒有成功套用。"
        else:verdict="這個 OCR 讀法目前只人工確認過一次。"
        group.update({"variant_count":distinct,"total":total,"repeat_count":repeated,
                      "variant_text":"、".join(f"{x['raw']}×{x['count']}" for x in variants),
                      "explanation":f"「{group['name']}」的人工修正紀錄\n\n"+"\n".join(parts)+"\n\n判讀："+verdict})
        result.append(group)
    result.sort(key=lambda x:(-x["total"],x["name"]))
    return {"items":result,"item_count":len(result),"event_count":len(events),
            "variant_count":sum(x["variant_count"] for x in result),
            "repeat_item_count":sum(bool(x["repeat_count"]) for x in result)}

def build_confirmed_raw_identity_map():
    """V5.85.4：只學習「同一OCR原文只有一個已確認答案」的錯字修正。"""
    registry={str(x.get("item_id") or ""):x for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
    choices={}
    for rec in load_evidence_identity_corrections():
        if not isinstance(rec,dict):continue
        side=str(rec.get("side") or "")
        if side not in ("input","output"):continue
        raw_key=_norm_identity_name(rec.get("raw"))
        iid=str(rec.get("id") or "");name=str(rec.get("name") or "").strip()
        if not raw_key or not iid or not name:continue
        target=registry.get(iid) or {}
        if iid not in MASTER.item_by_id and target.get("identity_status") not in ("玩家已確認","公共已確認","成熟庫已確認"):continue
        choices.setdefault((side,raw_key),{})[(iid,name)]=rec
    learned={}
    for key,targets in choices.items():
        # 同一原文曾被確認成多種物品時，絕不自動套用。
        if len(targets)==1:
            (iid,name),rec=next(iter(targets.items()))
            learned[key]={"id":iid,"name":name,"stage":str(rec.get("stage") or ""),
                          "kind":str(rec.get("kind") or "未分類")}
    return learned

def apply_confirmed_raw_identity_learning(rows):
    learned=build_confirmed_raw_identity_map()
    if not learned:return 0
    registry={str(x.get("item_id") or ""):x for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
    applied=0
    for row in rows or []:
        for side in ("input","output"):
            raw_key=_norm_identity_name(row.get(f"{side}_raw"))
            rec=learned.get((side,raw_key))
            if not rec:continue
            current_id=str(row.get(f"{side}_item_id") or "")
            if current_id and current_id!=rec["id"]:
                current=registry.get(current_id) or {}
                if current_id in MASTER.item_by_id or current.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認"):continue
            changed=(current_id!=rec["id"] or str(row.get(f"{side}_name") or "")!=rec["name"])
            row[f"{side}_item_id"]=rec["id"];row[f"{side}_name"]=rec["name"]
            row[f"{side}_stage"]=rec["stage"];row[f"{side}_kind"]=rec["kind"]
            row.setdefault("identity_source",{})[side]="已確認OCR錯字"
            row["issues"]=[q for q in row.get("issues",[]) if side.upper() not in str(q) and "品項未匹配ITEM_ID" not in str(q)]
            if not row["issues"] and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):
                row["status"]="候選可用"
            if changed:applied+=1
    return applied

def build_confirmed_blank_context_map():
    """空白欄位不能靠文字學習；只用兩個穩定欄位組成的唯一上下文補回身份。"""
    choices={}
    registry={str(x.get("item_id") or ""):x for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
    def add(side,point_id,anchor_id,exchange_type,iid,name,stage="",kind="未分類"):
        if side not in ("input","output") or not point_id or not anchor_id or not iid or not name:return
        target=registry.get(str(iid)) or {}
        if str(iid) not in MASTER.item_by_id and target.get("identity_status") not in ("玩家已確認","公共已確認","成熟庫已確認"):return
        key=(side,str(point_id),str(anchor_id),str(exchange_type or ""))
        choices.setdefault(key,{})[(str(iid),str(name))]={"id":str(iid),"name":str(name),
            "stage":str(stage or ""),"kind":str(kind or "未分類")}
    # 新紀錄會直接保留當時整列上下文。
    for rec in load_evidence_identity_corrections():
        if not isinstance(rec,dict) or not _identity_text_is_effectively_blank(rec.get("raw")):continue
        side=str(rec.get("side") or "")
        anchor=rec.get("output_item_id") if side=="input" else rec.get("input_item_id")
        add(side,rec.get("point_id"),anchor,rec.get("exchange_type"),rec.get("id"),rec.get("name"),rec.get("stage"),rec.get("kind"))
    # 舊版曾留下安全的三欄精確上下文；只在同一上下文答案唯一時沿用。
    for rec in load_manual_repairs():
        if not isinstance(rec,dict) or rec.get("field") not in ("input","output"):continue
        ctx=rec.get("context") or []
        if len(ctx)<3:continue
        add(str(rec.get("field")),ctx[0],ctx[1],ctx[2],rec.get("resolved_id"),rec.get("resolved_name"))
    # V5.92.4 實機證物：斯泰藍、未知的古代壁畫這列，INPUT 品名只剩數字10；
    # 玩家已確認畫面上的物品是鉛鑄塊。用完整兩錨點限定，不跨點位、不看相似圖示猜測。
    add("input","POINT_A_003","ITEM_1_010","一般材料→1段",
        "ITEM_MISC_0082","鉛鑄塊","","一般材料")
    return {key:next(iter(targets.values())) for key,targets in choices.items() if len(targets)==1}

def _identity_text_is_effectively_blank(raw):
    """只有數量／標點不算品名；例如 INPUT OCR 只留下「10」時仍是身份空白。"""
    text=str(raw or "").strip()
    return not bool(re.search(r"[A-Za-z\u4e00-\u9fff]",text))

def apply_confirmed_blank_context_learning(rows):
    """圖示相同或近似時，以交換點＋另一側品項的已確認唯一答案處理空白欄。"""
    learned=build_confirmed_blank_context_map()
    if not learned:return 0
    applied=0
    for row in rows or []:
        for side in ("input","output"):
            if not _identity_text_is_effectively_blank(_recover_field_raw(row,side)):continue
            anchor=row.get("output_item_id") if side=="input" else row.get("input_item_id")
            key=(side,str(row.get("point_id") or ""),str(anchor or ""),str(row.get("exchange_type") or ""))
            rec=learned.get(key)
            if rec is None:
                # 補回身份前 exchange_type 可能仍是待判定；兩個穩定錨點唯一時仍可用。
                candidates=[v for k,v in learned.items() if k[:3]==key[:3]]
                unique={(x["id"],x["name"]):x for x in candidates}
                rec=next(iter(unique.values())) if len(unique)==1 else None
            if not rec:continue
            current=str(row.get(f"{side}_item_id") or "")
            if current and current!=rec["id"]:continue
            row[f"{side}_item_id"]=rec["id"];row[f"{side}_name"]=rec["name"]
            row[f"{side}_stage"]=rec["stage"];row[f"{side}_kind"]=rec["kind"]
            row.setdefault("identity_source",{})[side]="已確認空白欄上下文"
            row["issues"]=[q for q in row.get("issues",[]) if side.upper() not in str(q) and "品項未匹配ITEM_ID" not in str(q)]
            if not row["issues"] and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):row["status"]="候選可用"
            applied+=1
    return applied

def apply_conservative_registry_typo_learning(rows):
    """只修一字誤讀且答案唯一的已確認一般材料；不做廣義模糊猜測。"""
    confirmed=[]
    for rec in _load_item_registry().get("items",[]):
        if not isinstance(rec,dict) or rec.get("identity_status") not in ("玩家已確認","公共已確認","成熟庫已確認"):continue
        for name in rec.get("names",[]) or []:
            key=_norm_identity_name(name)
            if len(key)>=3:confirmed.append((key,rec))
    applied=0
    for row in rows or []:
        for side in ("input","output"):
            if str(row.get(f"{side}_kind") or "") in ("段位品","特殊交換品"):continue
            raw=str(row.get(f"{side}_raw") or "").strip();key=_norm_identity_name(raw)
            if len(key)<2:continue
            candidates=[]
            for known,rec in confirmed:
                if len(known)==len(key) and len(key)>=4:
                    diffs=[i for i,(a,b) in enumerate(zip(key,known)) if a!=b]
                    # 至少保留連續兩字尾碼，且只能有一個字不同。
                    if len(diffs)==1 and key[-2:]==known[-2:]:candidates.append(rec)
                elif len(known)==len(key)+1 and len(key)>=2:
                    # 已確認名稱只被 OCR 吃掉中間一字：銅鑄塊→銅塊。
                    # 必須首尾相同、短字串是完整名稱的順序子序列，且最終答案唯一。
                    it=iter(known)
                    if key[0]==known[0] and key[-1]==known[-1] and all(ch in it for ch in key):
                        candidates.append(rec)
            unique={str(x.get("item_id") or ""):x for x in candidates}
            if len(unique)!=1:continue
            rec=next(iter(unique.values()));iid=str(rec.get("item_id") or "")
            current=str(row.get(f"{side}_item_id") or "")
            current_rec=next((x for x in _load_item_registry().get("items",[]) if str(x.get("item_id") or "")==current),{})
            if current and current!=iid and current_rec.get("identity_status") in ("玩家已確認","公共已確認","成熟庫已確認"):continue
            row[f"{side}_item_id"]=iid
            row[f"{side}_name"]=str(rec.get("confirmed_name") or (rec.get("names") or [raw])[0])
            row[f"{side}_stage"]=str(rec.get("stage") or "")
            row[f"{side}_kind"]=str(rec.get("kind") or row.get(f"{side}_kind") or "一般材料")
            row.setdefault("identity_source",{})[side]="已確認名稱的一字誤讀"
            row["issues"]=[q for q in row.get("issues",[]) if side.upper() not in str(q) and "品項未匹配ITEM_ID" not in str(q)]
            if not row["issues"] and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):row["status"]="候選可用"
            applied+=1
    return applied

def save_evidence_identity_correction(row,side):
    """人工修改只記住這張證物，不把同一OCR文字永久改成另一種物品。"""
    key=_evidence_identity_key(row,side)
    if not key[0] or not key[1] or side not in ("point","input","output"):return False
    try:
        data=load_evidence_identity_corrections()
        data=[x for x in data if _evidence_identity_key(x,str(x.get("side") or ""))!=key]
        rec={"source_file":str(row.get("source_file") or ""),"row_index":row.get("row_index"),
             "side":side,"raw":_recover_field_raw(row,side),
             "id":str(row.get("point_id" if side=="point" else f"{side}_item_id") or ""),
             "name":str(row.get(f"{side}_name") or ""),
             "stage":str(row.get(f"{side}_stage") or "") if side!="point" else "",
             "kind":str(row.get(f"{side}_kind") or "") if side!="point" else "",
             "point_id":str(row.get("point_id") or ""),
             "input_item_id":str(row.get("input_item_id") or ""),
             "output_item_id":str(row.get("output_item_id") or ""),
             "exchange_type":str(row.get("exchange_type") or ""),
             "confirmed_at":datetime.datetime.now().isoformat(timespec="seconds")}
        data.append(rec)
        os.makedirs(os.path.dirname(EVIDENCE_IDENTITY_PATH),exist_ok=True)
        with open(EVIDENCE_IDENTITY_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
        return True
    except Exception:return False

def apply_evidence_identity_corrections(rows):
    lookup={_evidence_identity_key(x,str(x.get("side") or "")):x
            for x in load_evidence_identity_corrections() if isinstance(x,dict)}
    applied=0
    for row in rows or []:
        for side in ("point","input","output"):
            rec=lookup.get(_evidence_identity_key(row,side))
            if not rec or not rec.get("id") or not rec.get("name"):continue
            if side=="point":row["point_id"]=rec["id"];row["point_name"]=rec["name"]
            else:
                row[f"{side}_item_id"]=rec["id"];row[f"{side}_name"]=rec["name"]
                row[f"{side}_stage"]=str(rec.get("stage") or "")
                row[f"{side}_kind"]=str(rec.get("kind") or row.get(f"{side}_kind") or "未分類")
            row.setdefault("identity_source",{})[side]="本張圖片人工確認"
            applied+=1
    return applied

def load_ratio_overrides():
    try:
        if os.path.exists(RATIO_OVERRIDE_PATH):
            with open(RATIO_OVERRIDE_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,list): return x
    except Exception: pass
    return []

def ratio_identity(r):
    """V5.22：比例用穩定交換身份記憶，不再綁截圖檔名與列號。"""
    return {
        "point_id":str(r.get("point_id") or ""),
        "input_item_id":str(r.get("input_item_id") or ""),
        "output_item_id":str(r.get("output_item_id") or ""),
        "exchange_type":str(r.get("exchange_type") or "")
    }

def _ratio_identity_key(x):
    if not isinstance(x,dict): return None
    ident=x.get("identity") if isinstance(x.get("identity"),dict) else x
    p=str(ident.get("point_id") or "")
    i=str(ident.get("input_item_id") or "")
    o=str(ident.get("output_item_id") or "")
    e=str(ident.get("exchange_type") or "")
    if not (p and i and o): return None
    return (p,i,o,e)

def save_ratio_override(row,value,input_value=1):
    """人工確認同時保留本批證據與穩定交換身份記憶。"""
    try:
        with open(RATIO_BATCH_PATH,"r",encoding="utf-8") as f:data=json.load(f)
        if not isinstance(data,list):data=[]
    except Exception:data=[]
    key=(os.path.basename(str(row.get("source_file") or "")),str(row.get("row_index") or ""))
    data=[x for x in data if (os.path.basename(str(x.get("source_file") or "")),str(x.get("row_index") or ""))!=key]
    data.append({"source_file":str(row.get("source_file") or ""),"row_index":row.get("row_index"),
                 "input_qty":int(input_value),"output_qty":int(value),"confirmed_by":"manual_current_batch"})
    os.makedirs(os.path.dirname(RATIO_BATCH_PATH),exist_ok=True)
    with open(RATIO_BATCH_PATH,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
    # 以點位＋投入＋產出的穩定身份跨批次套用；同一身份後來再確認時以新值更新。
    ident=ratio_identity(row);ikey=_ratio_identity_key({"identity":ident})
    if ikey:
        learned=load_ratio_confirmations()
        learned=[x for x in learned if _ratio_identity_key(x)!=ikey]
        learned.append({"identity":ident,"input_qty":int(input_value),"output_qty":int(value),
                        "confirmed_by":"player","confirmed_at":datetime.datetime.now().isoformat(timespec="seconds")})
        save_ratio_confirmations(learned)
    learn_visual_quantity(row,int(value))

def apply_ratio_confirmations(rows):
    lookup={_ratio_identity_key(x):x for x in load_ratio_confirmations() if _ratio_identity_key(x)}
    applied=0
    for r in rows or []:
        rec=lookup.get(_ratio_identity_key(r))
        if not rec:continue
        try:iq=int(rec.get("input_qty"));oq=int(rec.get("output_qty"))
        except Exception:continue
        if iq<=0 or oq<=0:continue
        # V6.1修復：人工修正直接套用，不檢查OCR信任度
        # 因為人工修正優先於OCR，且玩家可以再修改修正打錯字的情況
        src=dict(r.get("exchange_ratio_source") or {})
        r["input_quantity"]=iq;r["exchange_ratio_input"]=iq
        r["output_quantity"]=oq;r["exchange_ratio_output"]=oq
        src.update({"input_token":str(iq),"input_confidence":1.0,"input_reason":"玩家永久比例學習"})
        src.update({"output_token":str(oq),"output_confidence":1.0,"output_reason":"玩家永久比例學習"})
        r["exchange_ratio_source"]=src
        r["_ratio_from_memory"]=True;applied+=1
    return applied

def apply_confirmed_material_input_quantity(rows):
    """同一一般材料有至少兩次一致人工答案時，跨點位補回投入量。"""
    values={}
    for rec in load_ratio_confirmations():
        ident=rec.get("identity") if isinstance(rec,dict) else None
        if not isinstance(ident,dict) or ident.get("exchange_type")!="一般材料→1段":continue
        iid=str(ident.get("input_item_id") or "")
        try:value=int(rec.get("input_qty") or 0)
        except Exception:continue
        if iid and value>0:values.setdefault(iid,[]).append(value)
    stable={iid:vals[0] for iid,vals in values.items() if len(vals)>=2 and len(set(vals))==1}
    applied=0
    for row in rows or []:
        if str(row.get("exchange_type") or "")!="一般材料→1段":continue
        value=stable.get(str(row.get("input_item_id") or ""))
        if not value:continue
        src=dict(row.get("exchange_ratio_source") or {})
        token=str(src.get("input_token") or "");conf=float(src.get("input_confidence") or 0)
        if token.isdigit() and int(token)>0 and conf>=.80:continue
        row["input_quantity"]=value;row["exchange_ratio_input"]=value
        src.update({"input_token":str(value),"input_confidence":1.0,"input_reason":"同品項兩次人工比例一致"})
        row["exchange_ratio_source"]=src;row["_material_input_from_memory"]=True;applied+=1
    return applied

def recover_structured_special_quantity_source(rows):
    """舊結構檔已保存特殊品數量、但來源欄遺失時，從同列 OUTPUT 原文接回。"""
    applied=0
    for row in rows or []:
        if str(row.get("output_kind") or "")!="特殊交換品":continue
        try:value=int(row.get("output_quantity") or 0)
        except Exception:value=0
        if value<=0:continue
        src=dict(row.get("exchange_ratio_source") or {})
        if str(src.get("output_token") or "").isdigit() and float(src.get("output_confidence") or 0)>0:continue
        output_lines=[str(x) for x in row.get("raw_texts",[]) or [] if str(x).startswith("OUTPUT:")]
        # 必須是 OUTPUT 欄最後一個獨立數字，不能拿圖示0或交涉力冒充數量。
        if not any(re.search(rf"(?:^|[|｜]\s*){value}\s*$",line) for line in output_lines):continue
        src.update({"output_token":str(value),"output_confidence":.93,
                    "output_reason":"OUTPUT原文獨立數字補回"})
        row["exchange_ratio_source"]=src;row["exchange_ratio_output"]=value;applied+=1
    return applied

def recover_remaining_times_from_batch_twins(rows):
    """同批完全相同交換出現兩次時，用另一筆唯一的剩餘次數補空白，不處理互相矛盾者。"""
    groups={}
    for row in rows or []:
        key=(str(row.get("point_id") or ""),str(row.get("input_item_id") or ""),str(row.get("output_item_id") or ""))
        if all(key):groups.setdefault(key,[]).append(row)
    applied=0
    for group in groups.values():
        known=set()
        for row in group:
            try:value=int(row.get("remaining_times") or 0)
            except Exception:value=0
            if value>0:known.add(value)
        if len(known)!=1:continue
        value=next(iter(known))
        for row in group:
            try:missing=int(row.get("remaining_times") or 0)<=0
            except Exception:missing=True
            if not missing:continue
            row["remaining_times"]=value;row["remaining_times_source"]="同批相同交換補回"
            row["issues"]=[q for q in row.get("issues",[]) if "找不到剩餘交換次數" not in str(q)]
            applied+=1
    return applied

def _visual_quantity_signature(row):
    fp=_field_evidence_path(row,"output_qty")
    if not fp:return None
    try:
        im=Image.open(fp).convert("RGB");w,h=im.size
        if w<8 or h<8:return None
        # V5.85.6：只看圖示右下的數字疊圖，不再把物品圖示本身當成數字教材。
        digit=im.crop((int(w*.32),int(h*.52),w,h))
        gray=ImageOps.autocontrast(digit.convert("L")).resize((24,16),Image.Resampling.LANCZOS)
        binary=gray.point(lambda x:255 if x>=165 else 0)
        glyph_bits=0
        for value in binary.getdata():glyph_bits=(glyph_bits<<1)|(1 if value else 0)
        small=gray.resize((17,16),Image.Resampling.LANCZOS);px=list(small.getdata());bits=0
        for y in range(16):
            for x in range(16):bits=(bits<<1)|(1 if px[y*17+x]>px[y*17+x+1] else 0)
        return {"version":2,"glyph":f"{glyph_bits:096x}","dhash":f"{bits:064x}"}
    except Exception:return None

def load_visual_quantity_memory():
    try:
        with open(VISUAL_QUANTITY_MEMORY_PATH,"r",encoding="utf-8") as f:data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception:return []

def learn_visual_quantity(row,value):
    sig=_visual_quantity_signature(row)
    try:value=int(value)
    except Exception:return False
    if not sig or value<=0 or value>999:return False
    data=load_visual_quantity_memory()
    if any(int(x.get("value") or 0)==value and x.get("signature")==sig for x in data if isinstance(x,dict)):return True
    data.append({"value":value,"signature":sig,"learned_from":os.path.basename(str(row.get("source_file") or "")),
                 "row_index":row.get("row_index"),"learned_at":datetime.datetime.now().isoformat(timespec="seconds")})
    os.makedirs(os.path.dirname(VISUAL_QUANTITY_MEMORY_PATH),exist_ok=True)
    with open(VISUAL_QUANTITY_MEMORY_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)
    return True

def apply_visual_quantity_learning(rows):
    memory=[x for x in load_visual_quantity_memory() if isinstance(x,dict)]
    if not memory:return 0
    applied=0
    for row in rows or []:
        src=row.get("exchange_ratio_source") if isinstance(row.get("exchange_ratio_source"),dict) else {}
        token=str(src.get("output_token") or "").strip();conf=float(src.get("output_confidence") or 0)
        if token.isdigit() and conf>=.92:continue
        sig=_visual_quantity_signature(row)
        if not sig:continue
        grouped={}
        for rec in memory:
            other=rec.get("signature") or {}
            if sig.get("version")==2 and other.get("version")==2 and sig.get("glyph") and other.get("glyph"):
                xor=int(sig["glyph"],16)^int(other["glyph"],16)
                glyph=1-(xor.bit_count()/384)
                score=gray=glyph;color=1.0
            else:score,gray,color=0.0,0.0,0.0
            value=int(rec.get("value") or 0)
            if value and (value not in grouped or score>grouped[value][0]):grouped[value]=(score,gray,color,rec)
        scored=sorted(grouped.values(),key=lambda x:x[0],reverse=True)
        if not scored:continue
        best=scored[0];second=scored[1][0] if len(scored)>1 else 0.0
        if best[0]<.93 or best[0]-second<.035:continue
        value=int(best[3]["value"])
        src=dict(src);src.update({"output_token":str(value),"output_confidence":round(best[0],4),
                                  "output_reason":"人工數字圖片學習"})
        row["exchange_ratio_source"]=src;row["output_quantity"]=value;row["exchange_ratio_output"]=value
        row["_quantity_visual_learned"]=True;applied+=1
    return applied

def load_current_ratio_corrections():
    try:
        with open(RATIO_BATCH_PATH,"r",encoding="utf-8") as f:data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception:return []

def clear_current_ratio_corrections():
    try:
        os.makedirs(os.path.dirname(RATIO_BATCH_PATH),exist_ok=True)
        with open(RATIO_BATCH_PATH,"w",encoding="utf-8") as f:json.dump([],f,ensure_ascii=False,indent=2)
    except Exception:pass

def normalize_dynamic_ratio_status(r):
    """固定比例走規則；動態比例信任本次OCR，人工答案只限本張圖。"""
    try:a=int(r.get("input_stage") or 0);b=int(r.get("output_stage") or 0)
    except Exception:a=b=0
    # 一般材料戶籍可能為了分類帶有 stage=1，但它不是 1 階段位品。
    # 不可因此把畫面讀到的投入量 20 改回段位品固定投入 1。
    if r.get("input_kind") and str(r.get("input_kind"))!="段位品":a=0
    if r.get("output_kind") and str(r.get("output_kind"))!="段位品":b=0
    if a and b and (a,b) in ((3,4),(4,5),(5,6),(6,7)):
        normalize_fixed_stage_output_quantity(r);r["exchange_ratio_needs_ocr"]=False
        r["exchange_ratio_auto_accepted"]=True;r["exchange_ratio_review_reason"]="固定規則";return False
    needs=bool((b in (2,3)) or not(a and b));r["exchange_ratio_needs_ocr"]=needs
    if not needs:return False
    special=not(a and b);r["exchange_ratio_manual_both"]=special
    src=dict(r.get("exchange_ratio_source") or {})
    # V5.92.5 實機整圖已確認：藍迪斯的刺樹合板→海上戰鬥糧食是10:1。
    # INPUT小切片把10截尾成1且僅1/5版本命中；用三個正式ID限定這張證據，不擴散到其他材料。
    verified_input_qty={
        ("POINT_X_008","ITEM_PUBLIC_0145","ITEM_1_014"):10,
    }.get((str(r.get("point_id") or ""),str(r.get("input_item_id") or ""),str(r.get("output_item_id") or "")))
    input_reason=str(src.get("input_reason") or "")
    if verified_input_qty and str(src.get("input_token") or "")=="1" and re.search(r"1/\d+版本共識",input_reason):
        src.update({"input_token":str(verified_input_qty),"input_confidence":1.0,
                    "input_reason":"實機完整交換證物：10被小切片截尾成1"})
        r["exchange_ratio_source"]=src
        r["input_quantity"]=verified_input_qty;r["exchange_ratio_input"]=verified_input_qty
    reason=str(src.get("output_reason") or "")
    fixed_special=_fixed_special_output_quantity(r)
    if fixed_special:
        src=dict(src);src.update({"output_token":str(fixed_special),"output_confidence":1.0,
                                  "output_reason":"已確認固定產出規則"})
        r["exchange_ratio_source"]=src;reason=src["output_reason"]
    oq_token=str(src.get("output_token") or "").strip();oq_conf=float(src.get("output_confidence") or 0)
    # 遊戲圖示數量為 1 時通常不顯示數字。段位產出與已確認單個特殊品以 1 補齊，
    # 烏鴉硬幣等多位數動態報酬仍必須真正讀到數字。
    if not oq_token.isdigit() or int(oq_token or 0)<=0:
        single_when_unmarked=bool(r.get("output_stage") or str(r.get("output_name") or "") in
                                  ("遺失的貿易品箱子","純粹的珍珠結晶","華麗的珍珠結晶"))
        if single_when_unmarked:
            oq_token="1";oq_conf=1.0;src=dict(src);src.update({"output_token":"1","output_confidence":1.0,
                                                               "output_reason":"圖示未標數量即為1"})
            r["exchange_ratio_source"]=src;reason=src["output_reason"]
    crow_coin_incomplete=bool(str(r.get("output_name") or "")=="烏鴉硬幣" and oq_token.isdigit() and int(oq_token)<10)
    output_trusted=bool(oq_token.isdigit() and int(oq_token)>0 and
                        not crow_coin_incomplete and
                        (oq_conf>=0.92 or re.search(r"[2-9]/\d+版本共識",reason)))
    if output_trusted:
        r["output_quantity"]=int(oq_token);r["exchange_ratio_output"]=int(oq_token)
    if a:input_trusted=True;r["input_quantity"]=1;r["exchange_ratio_input"]=1
    else:
        # 比例欄是本次 OCR 的原始交換需求；身份重整不應用舊的 input_quantity=1 蓋掉它。
        iq=str(r.get("exchange_ratio_input") or r.get("input_quantity") or src.get("input_token") or "")
        iq_conf=float(src.get("input_confidence") or 0)
        input_trusted=bool(iq.isdigit() and int(iq)>0 and iq_conf>=0.80)
        if input_trusted:r["input_quantity"]=int(iq);r["exchange_ratio_input"]=int(iq)
    accepted=bool(input_trusted and output_trusted)
    r["exchange_ratio_auto_accepted"]=accepted
    if accepted:
        r["exchange_ratio"]=f"{r['exchange_ratio_input']} : {r['exchange_ratio_output']}（OCR自動採用）"
        r["exchange_ratio_review_reason"]="OCR數字清楚，已自動採用"
        r["issues"]=[q for q in r.get("issues",[]) if str(q)!="交換數量待確認"]
        if not r["issues"] and r.get("point_id") and r.get("input_item_id") and r.get("output_item_id"):r["status"]="候選可用"
    else:
        why=("INPUT數量未讀到或信心不足" if not input_trusted else
             ("烏鴉硬幣只讀到個位數，可能被截掉前段" if crow_coin_incomplete else "OUTPUT數量未讀到或共識不足"))
        r["exchange_ratio"]=f"⚠ {r.get('exchange_ratio_input') or '?'} : {r.get('exchange_ratio_output') or '?'}（{why}）"
        r["exchange_ratio_review_reason"]=why
        r.setdefault("issues",[])
        if "交換數量待確認" not in r["issues"]:r["issues"].append("交換數量待確認")
        r["status"]="待確認"
    return accepted

def _fixed_special_output_quantity(r):
    """只列入玩家已確認永遠產出1的特殊品；隨機獎勵不可在此猜測。"""
    if str(r.get("output_kind") or "")!="特殊交換品":return None
    name=str(r.get("output_name") or "").strip()
    if name.startswith("[大洋]"):return 1
    if name in ("遺失的貿易品箱子","純粹的珍珠結晶","華麗的珍珠結晶"):return 1
    # 奧基魯阿之花、波浪黑石、烏鴉硬幣與未知特殊品都是隨機量。
    return None

def apply_current_ratio_corrections(rows):
    lookup={(os.path.basename(str(x.get("source_file") or "")),str(x.get("row_index") or "")):x
            for x in load_current_ratio_corrections() if isinstance(x,dict)}
    for r in rows or []:
        rec=lookup.get((os.path.basename(str(r.get("source_file") or "")),str(r.get("row_index") or "")))
        if not rec:continue
        try:iq=int(rec.get("input_qty"));oq=int(rec.get("output_qty"))
        except Exception:continue
        if iq<=0 or oq<=0:continue
        # V6.1修復：本輪人工修正直接套用，不檢查OCR信任度
        # 因為這是玩家當前確認的值，優先於任何OCR判斷
        r["input_quantity"]=iq;r["exchange_ratio_input"]=iq
        r["output_quantity"]=oq;r["exchange_ratio_output"]=oq
        final_i=int(r.get("exchange_ratio_input") or r.get("input_quantity") or iq)
        final_o=int(r.get("exchange_ratio_output") or r.get("output_quantity") or oq)
        r["exchange_ratio"]=f"{final_i} : {final_o}（本輪人工確認補缺）"
        r["exchange_ratio_auto_accepted"]=True;r["_ratio_confirmed_current_batch"]=True
        r["issues"]=[q for q in r.get("issues",[]) if str(q)!="交換數量待確認"]
        if not r["issues"] and r.get("point_id") and r.get("input_item_id") and r.get("output_item_id"):r["status"]="候選可用"
    return rows

def build_identity_suspect_report(rows=None):
    """只列疑點，不自動合併或拆ID。"""
    if rows is None:
        try:
            with open(CURRENT_BATCH_PATH,"r",encoding="utf-8") as f:rows=json.load(f)
        except Exception:rows=[]
    suspects=[]
    for idx,r in enumerate(rows or [],1):
        for side in ("input","output"):
            raw=str(r.get(f"{side}_raw") or "").strip();current=str(r.get(f"{side}_item_id") or "")
            if not raw or not current:continue
            hit=MASTER.resolve_item(raw);exact_id=str((hit.get("record") or {}).get("id") or "") if hit.get("matched") else ""
            reg=_find_registry_item(raw);reg_id=str((reg or {}).get("item_id") or "")
            expected=exact_id or reg_id
            if expected and expected!=current:
                suspects.append({"type":"欄位ID與自身文字衝突","row":idx,"side":side.upper(),"raw":raw,
                                 "current_id":current,"text_resolves_to":expected,"source_file":r.get("source_file"),"row_index":r.get("row_index")})
        for issue in r.get("issues",[]) or []:
            if "曾受舊整列記憶影響" in str(issue):
                suspects.append({"type":"舊整列記憶無法安全自動還原","row":idx,"side":"待確認","raw":str(issue),
                                 "current_id":"","text_resolves_to":"","source_file":r.get("source_file"),"row_index":r.get("row_index")})
    report={"version":"V6.1","generated_at":datetime.datetime.now().isoformat(timespec="seconds"),
            "policy":"只報告，不自動改ID；請依原始切片確認。","count":len(suspects),"suspects":suspects}
    try:
        with open(IDENTITY_SUSPECT_PATH,"w",encoding="utf-8") as f:json.dump(report,f,ensure_ascii=False,indent=2)
    except Exception:pass
    return report


def load_ratio_confirmations():
    try:
        if os.path.exists(RATIO_CONFIRM_PATH):
            with open(RATIO_CONFIRM_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,list): return x
    except Exception: pass
    return []

def save_ratio_confirmations(data):
    try:
        os.makedirs(os.path.dirname(RATIO_CONFIRM_PATH),exist_ok=True)
        with open(RATIO_CONFIRM_PATH,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception: pass


def load_manual_confirmations():
    try:
        if os.path.exists(MANUAL_CONFIRM_PATH):
            with open(MANUAL_CONFIRM_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,list): return x
    except Exception: pass
    return []

def save_manual_confirmations(data):
    try:
        os.makedirs(os.path.dirname(MANUAL_CONFIRM_PATH),exist_ok=True)
        with open(MANUAL_CONFIRM_PATH,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception: pass

def core_confirm_signature(r):
    """V5.47：跨重跑核心身份。
    一般品項用正式ID；特殊/無ITEM_ID輸出再保存已解析的穩定OUTPUT名稱，
    避免確認後因OCR低信心每次重跑又回待確認。
    """
    return {
        "point_id":r.get("point_id"),
        "input_item_id":r.get("input_item_id"),
        "output_item_id":r.get("output_item_id"),
        "output_name":str(r.get("output_name") or "").strip(),
        "exchange_type":r.get("exchange_type")
    }

def _ignoreable_core_ocr_issue(issue):
    """玩家已看圖確認三個核心身份後，可忽略的 OCR／圖片候選差異。"""
    text=str(issue or "")
    return any(token in text for token in
               ("無法判定","未識別","低信心","文字身份與圖片學習衝突"))

def _issues_after_core_confirmation(row):
    """核心確認可忽略 OCR 差異；剩餘次數已有數值時也不再被舊錯誤文字卡住。"""
    recover_remaining_times_from_raw(row)
    try:has_remaining=int(row.get("remaining_times") or 0)>0
    except Exception:has_remaining=False
    return [q for q in row.get("issues",[]) if not _ignoreable_core_ocr_issue(q)
            and not (has_remaining and "找不到剩餘交換次數" in str(q))]

def recover_remaining_times_from_raw(row):
    """OCR 偶爾輸出「剩餘交換次數数：10次」；原文有數字就補回結構欄位。"""
    try:
        if int(row.get("remaining_times") or 0)>0:return False
    except Exception:pass
    text=" | ".join(str(x) for x in row.get("raw_texts",[]) or [])
    flat=re.sub(r"[|｜\s]","",text)
    match=re.search(r"剩[餘余]交[換换]次[數数]+[:：]?(\d+)次?",flat)
    if not match:return False
    value=int(match.group(1))
    if value<=0:return False
    row["remaining_times"]=value
    row["issues"]=[q for q in row.get("issues",[]) if "找不到剩餘交換次數" not in str(q)]
    if not row.get("issues") and row.get("point_id") and row.get("input_item_id") and row.get("output_item_id"):
        row["status"]="候選可用"
    row["remaining_times_source"]="OCR原文容錯補回"
    return True

def recover_batch_remaining_times(rows):
    return sum(1 for row in rows or [] if isinstance(row,dict) and recover_remaining_times_from_raw(row))

def _stable_core_key(sig):
    """相容舊版manual_confirmations：舊簽章雖含OCR原文，仍可抽出三個正式ID。"""
    if not isinstance(sig,dict): return None
    p=str(sig.get("point_id") or "")
    i=str(sig.get("input_item_id") or "")
    o=str(sig.get("output_item_id") or "")
    if not (p and i and o): return None
    return (p,i,o)

def apply_learned_core_confirmations(rows):
    saved=load_manual_confirmations()

    # V5.22：人工確認分兩種身份。
    # 1) 一般段位交換：POINT + INPUT_ID + OUTPUT_ID
    # 2) 特殊/非段位輸出：OUTPUT本來就可能沒有正式ITEM_ID，
    #    改用 POINT + INPUT_ID + exchange_type 記住玩家已確認過的這筆交換。
    full_keys=set()
    partial_keys=set()
    named_partial_keys=set()

    for x in saved:
        if not isinstance(x,dict):
            continue
        sig=x.get("signature") if isinstance(x.get("signature"),dict) else x
        p=str(sig.get("point_id") or "")
        i=str(sig.get("input_item_id") or "")
        o=str(sig.get("output_item_id") or "")
        e=str(sig.get("exchange_type") or "")
        n=str(sig.get("output_name") or "").strip()
        if p and i and o:
            full_keys.add((p,i,o))
        elif p and i and n:
            named_partial_keys.add((p,i,n))
        elif p and i and e:
            partial_keys.add((p,i,e))

    for r in rows or []:
        if r.get("status")=="候選可用":
            continue

        p=str(r.get("point_id") or "")
        i=str(r.get("input_item_id") or "")
        o=str(r.get("output_item_id") or "")
        e=str(r.get("exchange_type") or "")
        out_text=str(r.get("output_name") or r.get("output_raw") or "").strip()

        trusted=False

        # 一般交換：三個正式ID完整才可放行。
        if p and i and o and (p,i,o) in full_keys:
            trusted=True

        # 特殊/非段位輸出：沒有OUTPUT_ITEM_ID是資料型態本身，不是失敗。
        # 仍要求 POINT、INPUT_ID、exchange_type 完整，而且這次真的有讀到OUTPUT文字。
        elif p and i and (not o) and out_text not in ("","?","？") and (p,i,out_text) in named_partial_keys:
            trusted=True
        elif p and i and (not o) and e and out_text not in ("","?","？") and (p,i,e) in partial_keys:
            trusted=True

        if trusted:
            r["issues"]=_issues_after_core_confirmation(r)
            r["status"]="候選可用" if not r["issues"] else "待確認"
            r["_manual_confirmed"]=True
            r["_core_trusted"]=True
            r["_core_trusted_from_memory"]=True

    return rows

def append_candidate_log(entry):
    try:
        data=[]
        if os.path.exists(CANDIDATE_LOG_PATH):
            with open(CANDIDATE_LOG_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,list): data=x
        data.append(entry)
        os.makedirs(os.path.dirname(CANDIDATE_LOG_PATH),exist_ok=True)
        with open(CANDIDATE_LOG_PATH,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception:
        pass


def _load_window_states():
    try:
        if os.path.exists(WINDOW_STATE_PATH):
            with open(WINDOW_STATE_PATH,"r",encoding="utf-8") as f:
                d=json.load(f)
            if isinstance(d,dict): return d
    except Exception:
        pass
    return {}

def _save_window_states(d):
    try:
        os.makedirs(os.path.dirname(WINDOW_STATE_PATH),exist_ok=True)
        with open(WINDOW_STATE_PATH,"w",encoding="utf-8") as f:
            json.dump(d,f,ensure_ascii=False,indent=2)
    except Exception:
        pass

def remember_window(win,key,default_geometry=None):
    """V5.47：記住視窗上次位置/大小/最大化；跨版本共用。"""
    states=_load_window_states()
    st=states.get(key,{}) if isinstance(states.get(key,{}),dict) else {}
    geo=st.get("geometry") or default_geometry
    if geo:
        try: win.geometry(geo)
        except Exception: pass
    if st.get("state")=="zoomed":
        try: win.after(80,lambda: win.state("zoomed") if win.winfo_exists() else None)
        except Exception: pass

    cache={"state":st.get("state") or "normal","geometry":st.get("geometry") or geo}
    def capture_now():
        try:
            cur=win.state();cache["state"]=cur
            if cur=="normal":cache["geometry"]=win.geometry()
        except Exception:pass
    def save_now(event=None):
        try:
            capture_now()
            old=_load_window_states()
            rec=old.get(key,{}) if isinstance(old.get(key,{}),dict) else {}
            rec["state"]=cache.get("state") or "normal"
            if cache.get("geometry"):rec["geometry"]=cache["geometry"]
            old[key]=rec
            _save_window_states(old)
        except Exception:
            pass

    # 移動/縮放後稍微延遲保存，避免每個像素都狂寫檔。
    timer={"id":None}
    def schedule_save(event=None):
        try:
            capture_now()
            if timer["id"] is not None:
                win.after_cancel(timer["id"])
            timer["id"]=win.after(350,save_now)
        except Exception:
            pass
    win.bind("<Configure>",schedule_save,add="+")
    win.bind("<Destroy>",lambda e:save_now() if e.widget is win else None,add="+")
    return win

PAGE_STATE_PATH=shared_file("page_states.json", fallback_local=False)
def smart_window(win,key,default_geometry,min_w=900,min_h=650):
    """記憶玩家視窗，但新版內容需要更大時自動長大；同時不超出螢幕。"""
    remember_window(win,key,default_geometry)
    try:
        win.update_idletasks()
        sw=max(640,win.winfo_screenwidth()-80)
        sh=max(480,win.winfo_screenheight()-120)
        min_w=min(min_w,sw); min_h=min(min_h,sh)
        win.minsize(min_w,min_h)

        state=win.state()
        if state=="normal":
            geo=win.geometry()
            m=re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",geo)
            if m:
                w,h,x,y=map(int,m.groups())
                nw=min(max(w,min_w),sw)
                nh=min(max(h,min_h),sh)
                x=max(0,min(x,sw-nw))
                y=max(0,min(y,sh-nh))
                if (nw,nh,x,y)!=(w,h,x,y):
                    win.geometry(f"{nw}x{nh}+{x}+{y}")
    except Exception:
        pass

def make_reliable_tree(parent,columns,labels,widths,height=24):
    """共用可靠表格：固定 grid + scrollbar，避免幾何管理器把內容吃掉。"""
    shell=tk.Frame(parent)
    shell.grid_rowconfigure(0,weight=1)
    shell.grid_columnconfigure(0,weight=1)
    tree=ttk.Treeview(shell,columns=columns,show="headings",height=height)
    for c in columns:
        tree.heading(c,text=labels.get(c,c))
        anchor="e" if ("delta" in c or c in ("expected","current","diff")) else "w"
        tree.column(c,width=widths.get(c,120),anchor=anchor)
    sb=tk.Scrollbar(shell,orient="vertical",command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.grid(row=0,column=0,sticky="nsew")
    sb.grid(row=0,column=1,sticky="ns")
    # 子視窗內的表格優先接管滾輪，避免路線頁殘留的 bind_all 把滾動吃走。
    def _tree_wheel(event):
        delta=getattr(event,"delta",0)
        if delta:tree.yview_scroll(-3 if delta>0 else 3,"units")
        elif getattr(event,"num",None)==4:tree.yview_scroll(-3,"units")
        elif getattr(event,"num",None)==5:tree.yview_scroll(3,"units")
        return "break"
    tree.bind("<MouseWheel>",_tree_wheel,add="+")
    tree.bind("<Button-4>",_tree_wheel,add="+")
    tree.bind("<Button-5>",_tree_wheel,add="+")
    return shell,tree


def load_page_states():
    try:
        with open(PAGE_STATE_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,dict) else {}
    except Exception:return {}
def save_page_state(key, **kwargs):
    try:
        d=load_page_states(); rec=d.get(key,{}) if isinstance(d.get(key),dict) else {}
        rec.update(kwargs); d[key]=rec
        os.makedirs(os.path.dirname(PAGE_STATE_PATH),exist_ok=True)
        with open(PAGE_STATE_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)
    except Exception:pass
def get_page_state(key):
    d=load_page_states(); return d.get(key,{}) if isinstance(d.get(key),dict) else {}

def load_player_settings():
    d={"negotiation_discount_percent":0.0,"normal_barter_base_power":14286,
       "high_barter_base_power":21650,"display_size":"大字",
       "show_stage_input_qty":False,"show_nonstage_input_qty":True,
       "show_lowstage_output_qty":True,"show_highstage_output_qty":False,
       "show_nonstage_output_qty":True,
       "route_learning_enabled":True,
       "ui_show_timing":True,"ui_show_weight":True,"ui_show_ship_switch":True,
       "ui_show_route_requirements":True,"ui_show_daze_card":True,
       "ui_show_seal_features":True,"ui_show_negotiation_power":True,
       "t5_keep_cap_enabled":True,"t5_keep_cap":8,
       "ship_weight_capacity":0,"ships":[],"active_ship_id":""}
    try:
        if os.path.exists(PLAYER_SETTINGS_PATH):
            with open(PLAYER_SETTINGS_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,dict): d.update(x)
    except Exception: pass
    ships=d.get("ships") if isinstance(d.get("ships"),list) else []
    ships=[x for x in ships if isinstance(x,dict) and x.get("id")]
    if not ships:
        try:oldcap=float(d.get("ship_weight_capacity",0) or 0)
        except Exception:oldcap=0.0
        ships=[{"id":"SHIP_1","name":"我的船","capacity":oldcap}]
    d["ships"]=ships
    valid={str(x.get("id")) for x in ships}
    if str(d.get("active_ship_id") or "") not in valid:d["active_ship_id"]=str(ships[0]["id"])
    return d

def save_player_settings(d):
    os.makedirs(os.path.dirname(PLAYER_SETTINGS_PATH),exist_ok=True)
    with open(PLAYER_SETTINGS_PATH,"w",encoding="utf-8") as f:
        json.dump(d,f,ensure_ascii=False,indent=2)

PLAYER_SETTINGS=load_player_settings()

def should_auto_open_onboarding(settings,state,current_batch_path=None):
    """只招呼真正空白的新安裝；舊玩家即使沒有按過指南也不突然被打擾。"""
    if bool(settings.get("onboarding_seen")):return False
    if str((state or {}).get("batch_id") or "").strip():return False
    if (state or {}).get("routes") or (state or {}).get("unplanned_stations"):return False
    path=current_batch_path or CURRENT_BATCH_PATH
    if path and os.path.isfile(path):return False
    return True

def active_ship_profile(settings=None):
    settings=settings or PLAYER_SETTINGS
    ships=settings.get("ships") if isinstance(settings.get("ships"),list) else []
    active=str(settings.get("active_ship_id") or "")
    ship=next((x for x in ships if isinstance(x,dict) and str(x.get("id") or "")==active),None)
    if ship is None:ship=next((x for x in ships if isinstance(x,dict)),None)
    if ship is None:return {"id":"","name":"未設定船隻","capacity":0.0}
    try:cap=float(ship.get("capacity",0) or 0)
    except Exception:cap=0.0
    return {"id":str(ship.get("id") or ""),"name":str(ship.get("name") or "未命名船"),"capacity":cap}

def available_ship_profiles(settings=None):
    settings=settings or PLAYER_SETTINGS
    return [x for x in (settings.get("ships") or []) if isinstance(x,dict)]

def cycle_active_ship():
    """切到下一艘玩家船隻；只改目前出航船，不修改路線或負重資料。"""
    ships=available_ship_profiles()
    if len(ships)<=1:return active_ship_profile(),False
    active=str(PLAYER_SETTINGS.get("active_ship_id") or "")
    idx=next((i for i,x in enumerate(ships) if str(x.get("id") or "")==active),0)
    nxt=ships[(idx+1)%len(ships)]
    PLAYER_SETTINGS["active_ship_id"]=str(nxt.get("id") or "")
    try:PLAYER_SETTINGS["ship_weight_capacity"]=float(nxt.get("capacity",0) or 0)
    except Exception:PLAYER_SETTINGS["ship_weight_capacity"]=0.0
    save_player_settings(PLAYER_SETTINGS)
    return active_ship_profile(),True

PLAYER_ITEM_ALIASES_PATH=shared_file("player_item_aliases.json", fallback_local=False)
def load_player_item_aliases():
    try:
        with open(PLAYER_ITEM_ALIASES_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):return x
    except Exception:pass
    seed={}
    try:
        with open(os.path.join(BASE,"player_item_aliases_seed.json"),"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):seed=x
    except Exception:pass
    try:
        os.makedirs(os.path.dirname(PLAYER_ITEM_ALIASES_PATH),exist_ok=True)
        with open(PLAYER_ITEM_ALIASES_PATH,"w",encoding="utf-8") as f:json.dump(seed,f,ensure_ascii=False,indent=2)
    except Exception:pass
    return seed
def save_player_item_aliases(d):
    os.makedirs(os.path.dirname(PLAYER_ITEM_ALIASES_PATH),exist_ok=True)
    with open(PLAYER_ITEM_ALIASES_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)

def player_item_alias(item_id, fallback=""):
    try:return str(load_player_item_aliases().get(str(item_id),fallback) or "")
    except Exception:return str(fallback or "")

PLAYER_ROUTE_UI_PATH=shared_file("player_route_ui.json", fallback_local=False)
ROUTE_TEMPLATES_PATH=shared_file("route_templates.json", fallback_local=False)

def load_player_route_ui():
    d={"show_point_alias":False,"show_stage_colors":True,
       "stage_colors":{"1":"#FFF4CC","2":"#E8F4FF","3":"#E9F8E7","4":"#F2E9FF",
                       "5":"#FFE9E9","6":"#E8F7F4","7":"#FFD7A8"}}
    try:
        with open(PLAYER_ROUTE_UI_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):
            d.update({k:v for k,v in x.items() if k!="stage_colors"})
            if isinstance(x.get("stage_colors"),dict):d["stage_colors"].update(x["stage_colors"])
    except Exception:pass
    return d

def save_player_route_ui(d):
    os.makedirs(os.path.dirname(PLAYER_ROUTE_UI_PATH),exist_ok=True)
    with open(PLAYER_ROUTE_UI_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)

def save_route_templates(d):
    os.makedirs(os.path.dirname(ROUTE_TEMPLATES_PATH),exist_ok=True)
    with open(ROUTE_TEMPLATES_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)

def normalize_route_template_entry(entry):
    """V5.65：路線範本只保留交換點與順序，不綁定某一刷的物品。"""
    raw_routes=entry.get("routes",[]) if isinstance(entry,dict) else entry
    if not isinstance(raw_routes,list):raw_routes=[]
    routes=[]
    for route in raw_routes:
        if not isinstance(route,dict):continue
        stations=[]
        for st in route.get("stations",[]):
            if not isinstance(st,dict):continue
            point_id=str(st.get("point_id") or "").strip()
            if not point_id:continue
            rec={"point_id":point_id}
            point_name=str(st.get("point_name") or st.get("point") or "").strip()
            if point_name:rec["point_name"]=point_name
            stations.append(rec)
        if stations:routes.append({"name":str(route.get("name") or "路線"),"stations":stations})
    return {"version":2,"match_mode":"point_id","routes":routes}

def load_route_templates():
    try:
        with open(ROUTE_TEMPLATES_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if not isinstance(x,dict):return {}
        normalized={str(name):normalize_route_template_entry(entry) for name,entry in x.items()}
        if normalized!=x:save_route_templates(normalized)
        return normalized
    except Exception:return {}

def apply_point_route_template(entry,all_stations):
    """按 POINT_ID 套用目前這一刷的站；重複點位不猜，交給玩家確認。"""
    template=normalize_route_template_entry(entry)
    by_point={}
    for st in all_stations:
        pid=str(st.get("point_id") or "").strip()
        if pid:by_point.setdefault(pid,[]).append(st)
    used=set();routes=[];missing=[];ambiguous=[]
    for tr in template["routes"]:
        matched=[]
        for slot in tr.get("stations",[]):
            pid=str(slot.get("point_id") or "")
            label=str(slot.get("point_name") or pid)
            candidates=[st for st in by_point.get(pid,[]) if id(st) not in used]
            if len(candidates)==1:
                matched.append(candidates[0]);used.add(id(candidates[0]))
            elif not candidates:missing.append(label)
            else:ambiguous.append(f"{label}（{len(candidates)}筆）")
        if matched:
            routes.append({"name":tr.get("name") or "路線","status":"尚未開始",
                           "current_index":0,"view_index":0,"stations":matched,
                           "source":"V5.65_POINT_TEMPLATE"})
    loose=[st for st in all_stations if id(st) not in used]
    return routes,loose,missing,ambiguous

def barter_template_identity(row):
    return "|".join(str(row.get(k) or "") for k in
                    ("point_id","input_item_id","output_item_id","input_stage","output_stage"))

def barter_stage_pair(row):
    try:a=int(row.get("input_stage") or 0);b=int(row.get("output_stage") or 0)
    except Exception:return ""
    return f"{a}->{b}" if a in range(1,8) and b in range(1,8) else ""

def build_barter_rule_template(usable,is_selected):
    picked=[r for r in usable if is_selected(r)]
    groups={}
    for r in usable:
        pair=barter_stage_pair(r)
        if pair:groups.setdefault(pair,[]).append(r)
    full_pairs=sorted(pair for pair,group in groups.items() if group and all(is_selected(r) for r in group))
    # 單獨勾選保存「交換點」，不保存當天的物品組合。刷新後同一 POINT_ID
    # 會自動套用今天的新交換品。
    entries=[];seen=set()
    for r in picked:
        if barter_stage_pair(r) in full_pairs:continue
        pid=str(r.get("point_id") or "").strip()
        if not pid or pid in seen:continue
        seen.add(pid);entries.append({"point_id":pid,"point_name":str(r.get("point_name") or "")})
    return {"version":3,"match_mode":"stage_or_point_id","stage_pairs":full_pairs,"entries":entries}

def barter_rule_matches(template,row):
    pairs={str(x) for x in template.get("stage_pairs",[]) if str(x)}
    # 舊 V2 entries 本身已有 point_id，讀取時直接升級語意，不必玩家重存。
    points={str(x.get("point_id") or "").strip() for x in template.get("entries",[]) if isinstance(x,dict)}
    return barter_stage_pair(row) in pairs or str(row.get("point_id") or "").strip() in points

def projected_selected_input_available(target,rows,selected,selected_times,inventory,key_fn):
    """今日選擇用：目前庫存加上已勾選前段交換將產出的同一物品。"""
    iid=str(target.get("input_item_id") or "");stock=inventory.get(iid,None)
    if stock is None:return None,0
    try:stock=max(0,int(stock))
    except Exception:return None,0
    try:target_stage=int(target.get("input_stage") or 0)
    except Exception:target_stage=0
    planned=0;reserved=0
    for producer in rows:
        pk=key_fn(producer)
        if not selected.get(pk,False) or producer is target:continue
        if str(producer.get("output_item_id") or "")!=iid:continue
        try:
            producer_stage=int(producer.get("output_stage") or 0)
            if target_stage and producer_stage and producer_stage>target_stage:continue
            remain=max(1,int(producer.get("remaining_times") or 1))
            ptimes=max(0,min(remain,int(selected_times.get(pk,remain))))
            outqty=max(1,int(producer.get("output_quantity") or 1))
        except Exception:continue
        planned+=ptimes*outqty
    for consumer in rows:
        ck=key_fn(consumer)
        if consumer is target or not selected.get(ck,False):continue
        if str(consumer.get("input_item_id") or "")!=iid:continue
        try:
            cs=int(consumer.get("input_stage") or 0)
            if target_stage and cs and cs>target_stage:continue
            remain=max(1,int(consumer.get("remaining_times") or 1))
            ctimes=max(0,min(remain,int(selected_times.get(ck,remain))))
            inqty=max(1,int(consumer.get("input_quantity") or 1))
        except Exception:continue
        reserved+=ctimes*inqty
    return max(0,stock+planned-reserved),planned

def remaining_route_requirements(route):
    """按剩餘路線順序算真正要帶的貨；前站產物可直接供後站使用。"""
    balance={};required={};records={};order=[]
    def info(st,side):
        iid=str(st.get(f"{side}_id") or "").strip();name=str(st.get(side) or "？")
        key=("ID:"+iid) if iid else ("NAME:"+_norm_identity_name(name))
        try:qty=int(st.get(f"{side}_qty") or 1)
        except Exception:qty=1
        try:times=int(st.get("times") or 1)
        except Exception:times=1
        records.setdefault(key,{"item_id":iid,"name":name,"quantity":0})
        return key,max(0,qty)*max(0,times)
    for st in route.get("stations",[]) or []:
        if st.get("status") in ("完成","跳過"):continue
        ink,inq=info(st,"input");outk,outq=info(st,"output")
        balance[ink]=balance.get(ink,0)-inq
        need=max(0,-balance[ink])
        if need>required.get(ink,0):
            required[ink]=need
            if ink not in order:order.append(ink)
        balance[outk]=balance.get(outk,0)+outq
    result=[]
    for key in order:
        rec=dict(records[key]);rec["quantity"]=required[key];result.append(rec)
    return result

def item_unit_weight(item_id,name=""):
    rec=MASTER.item_by_id.get(str(item_id or ""))
    if rec is None and name:
        hit=MASTER.resolve_item(name)
        if hit.get("matched"):rec=hit.get("record")
    if rec is None:
        observed=_find_registry_item(name)
        rec=observed if isinstance(observed,dict) else None
    try:
        weight=float(rec.get("weight"))
        return weight if weight>=0 else None
    except Exception:return None

def route_peak_weight(route):
    """逐站模擬貨艙；前站產物可供後站使用，不重複算成出發貨物。"""
    stations=[st for st in (route.get("stations",[]) or []) if st.get("status")!="跳過"]
    unknown=set();moves=[];names={};weights={}
    def side_info(st,side):
        iid=str(st.get(f"{side}_id") or "").strip();name=str(st.get(side) or "？")
        key=("ID:"+iid) if iid else ("NAME:"+_norm_identity_name(name))
        try:qty=int(st.get(f"{side}_qty") or 1)
        except Exception:qty=1
        try:times=int(st.get("times") or 1)
        except Exception:times=1
        amount=max(0,qty)*max(0,times);w=item_unit_weight(iid,name)
        names[key]=name;weights[key]=w
        if w is None:unknown.add(name)
        return key,amount
    for st in stations:
        moves.append((side_info(st,"input"),side_info(st,"output")))

    # 先算每種貨真正需要從港口帶多少。若前站已產出，後站直接沿用。
    balance={};required={}
    for (ink,inq),(outk,outq) in moves:
        balance[ink]=balance.get(ink,0)-inq
        required[ink]=max(required.get(ink,0),-balance[ink])
        balance[outk]=balance.get(outk,0)+outq

    cargo={k:q for k,q in required.items() if q>0}
    def cargo_weight():
        return sum(q*weights[k] for k,q in cargo.items() if weights.get(k) is not None)
    start=cargo_weight();peak=start;peak_location="出發時";timeline=[]
    start_items=[{"key":k,"name":names.get(k,k),"quantity":q,"unit_weight":weights.get(k),
                  "known_weight":(q*weights[k] if weights.get(k) is not None else None)}
                 for k,q in cargo.items() if q>0]
    for idx,((ink,inq),(outk,outq)) in enumerate(moves):
        before=cargo_weight()
        cargo[ink]=max(0,cargo.get(ink,0)-inq)
        after_give=cargo_weight()
        cargo[outk]=cargo.get(outk,0)+outq
        after=cargo_weight()
        station=stations[idx]
        timeline.append({"station_index":idx,"point":str(station.get("point") or f"第{idx+1}站"),
                         "input":names.get(ink,ink),"input_quantity":inq,"input_unit_weight":weights.get(ink),
                         "output":names.get(outk,outk),"output_quantity":outq,"output_unit_weight":weights.get(outk),
                         "before_weight":before,"after_give_weight":after_give,"after_weight":after})
        if after>peak:peak=after;peak_location=str(station.get("point") or f"第{idx+1}站")+"交換後"
    return {"start_weight":max(0.0,start),"peak_weight":max(0.0,peak),"peak_location":peak_location,
            "unknown_items":sorted(unknown),"start_items":start_items,"timeline":timeline}

PLAYER_POINT_GROUPS_PATH=shared_file("player_point_groups.json", fallback_local=False)
SPECIAL_TABS_PATH=shared_file("player_special_tabs.json", fallback_local=False)
CUSTOM_SETTINGS_BACKUP_PATH=shared_file("player_custom_settings_backup.json", fallback_local=False)
DEFAULT_SPECIAL_TABS=[
    {"name":"遠線","groups":["遠線"]},
    {"name":"G","groups":["G","高G"]},
    {"name":"零散","groups":["零散"]},
]

def load_special_tabs():
    try:
        with open(SPECIAL_TABS_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,list):
            out=[];seen=set()
            for rec in x:
                if not isinstance(rec,dict):continue
                name=str(rec.get("name") or "").strip()
                if not name or name in seen or name in ("全部特殊","其他"):continue
                groups=[str(v).strip() for v in (rec.get("groups") or []) if str(v).strip()]
                out.append({"name":name,"groups":list(dict.fromkeys(groups))});seen.add(name)
            return out
    except Exception:pass
    return copy.deepcopy(DEFAULT_SPECIAL_TABS)

def save_special_tabs(rows):
    os.makedirs(os.path.dirname(SPECIAL_TABS_PATH),exist_ok=True)
    with open(SPECIAL_TABS_PATH,"w",encoding="utf-8") as f:json.dump(rows,f,ensure_ascii=False,indent=2)

def _load_json_dict(path,default=None):
    try:
        with open(path,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,dict) else ({} if default is None else copy.deepcopy(default))
    except Exception:return {} if default is None else copy.deepcopy(default)

def builtin_player_aliases():
    return _load_json_dict(os.path.join(BASE,"player_item_aliases_seed.json"),{})

def builtin_point_groups():
    seed={}
    prefs=_load_json_dict(os.path.join(BASE,"player_point_preferences_seed.json"),{})
    for pid,rec in prefs.items():
        if isinstance(rec,dict) and str(rec.get("zone") or "").strip():seed[str(pid)]=str(rec["zone"]).strip()
    return seed

def backup_player_customizations():
    snap={
        "created_at":datetime.datetime.now().isoformat(timespec="seconds"),
        "item_aliases":load_player_item_aliases(),
        "point_groups":load_player_point_groups(),
        "special_tabs":load_special_tabs(),
        "route_ui":load_player_route_ui(),
    }
    os.makedirs(os.path.dirname(CUSTOM_SETTINGS_BACKUP_PATH),exist_ok=True)
    with open(CUSTOM_SETTINGS_BACKUP_PATH,"w",encoding="utf-8") as f:json.dump(snap,f,ensure_ascii=False,indent=2)
    return snap

def restore_player_customizations(snapshot):
    if not isinstance(snapshot,dict):return False
    save_player_item_aliases(snapshot.get("item_aliases") if isinstance(snapshot.get("item_aliases"),dict) else {})
    save_player_point_groups(snapshot.get("point_groups") if isinstance(snapshot.get("point_groups"),dict) else {})
    save_special_tabs(snapshot.get("special_tabs") if isinstance(snapshot.get("special_tabs"),list) else [])
    save_player_route_ui(snapshot.get("route_ui") if isinstance(snapshot.get("route_ui"),dict) else load_player_route_ui())
    return True
def load_player_point_groups():
    try:
        with open(PLAYER_POINT_GROUPS_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):return x
    except Exception:pass
    seed={}
    try:
        with open(os.path.join(BASE,"player_point_preferences_seed.json"),"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):
            for pid,rec in x.items():
                if isinstance(rec,dict) and rec.get("zone"):seed[str(pid)]=str(rec["zone"])
    except Exception:pass
    try:
        os.makedirs(os.path.dirname(PLAYER_POINT_GROUPS_PATH),exist_ok=True)
        with open(PLAYER_POINT_GROUPS_PATH,"w",encoding="utf-8") as f:json.dump(seed,f,ensure_ascii=False,indent=2)
    except Exception:pass
    return seed

def save_player_point_groups(d):
    os.makedirs(os.path.dirname(PLAYER_POINT_GROUPS_PATH),exist_ok=True)
    with open(PLAYER_POINT_GROUPS_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)

def load_route_learning_history():
    try:
        with open(ROUTE_LEARNING_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict) and isinstance(x.get("records"),list):return x
    except Exception:pass
    return {"version":1,"records":[]}

def save_route_learning_history(data):
    os.makedirs(os.path.dirname(ROUTE_LEARNING_PATH),exist_ok=True)
    with open(ROUTE_LEARNING_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)

def load_seal_evaluations():
    try:
        with open(SEAL_EVALUATION_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict) and isinstance(x.get("records"),list):return x
    except Exception:pass
    return {"version":1,"records":[]}

def save_seal_evaluations(data):
    os.makedirs(os.path.dirname(SEAL_EVALUATION_PATH),exist_ok=True)
    with open(SEAL_EVALUATION_PATH,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)

def seal_signature_quality(proposed_signature,final_signature):
    """只評估海豹有排入路線的點；名稱與交換內容不參與品質分數。"""
    proposed_signature=[list(map(str,row or [])) for row in proposed_signature or []]
    final_signature=[list(map(str,row or [])) for row in final_signature or []]
    def positions(sig):
        out={}
        for ri,row in enumerate(sig):
            for pi,pid in enumerate(row):
                if pid and pid not in out:out[pid]=(ri,pi)
        return out
    pp=positions(proposed_signature);fp=positions(final_signature);total=len(pp)
    kept=sum(1 for pid in pp if pid in fp)
    same_route=sum(1 for pid,pos in pp.items() if fp.get(pid,(None,None))[0]==pos[0])
    exact=sum(1 for pid,pos in pp.items() if fp.get(pid)==pos)
    return {"total":total,"kept":kept,"same_route":same_route,"exact":exact,
            "same_route_percent":round(same_route/max(1,total)*100),
            "exact_percent":round(exact/max(1,total)*100),
            "missing":max(0,total-kept)}

def create_seal_evaluation(batch_id,current_routes,current_loose,proposal,decision):
    data=load_seal_evaluations();now=datetime.datetime.now().isoformat(timespec="seconds")
    eid=f"SEAL_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    rec={"evaluation_id":eid,"batch_id":str(batch_id or ""),"created_at":now,"status":"previewed",
         "source_record_id":str((proposal.get("record") or {}).get("record_id") or (proposal.get("record") or {}).get("batch_id") or ""),
         "confidence":str(proposal.get("confidence") or "低"),"supporting_records":int(proposal.get("supporting_records") or 0),
         "matched":int(proposal.get("matched") or 0),"eligible":int(proposal.get("eligible") or 0),
         "before_signature":route_plan_signature(current_routes),"proposed_signature":route_plan_signature(proposal.get("routes",[])),
         "before_loose":len(current_loose or []),"proposed_loose":len(proposal.get("loose",[]) or []),
         "decision_summary":{"same_as_current":bool(decision.get("same_as_current")),
             "moved_route":int(decision.get("moved_route") or 0),"moved_position":int(decision.get("moved_position") or 0),
             "from_loose":int(decision.get("from_loose") or 0)}}
    data["version"]=1;data.setdefault("records",[]).append(rec);save_seal_evaluations(data)
    return eid

def update_seal_evaluation(evaluation_id,**changes):
    data=load_seal_evaluations();found=None
    for rec in data.get("records",[]):
        if str(rec.get("evaluation_id") or "")==str(evaluation_id or ""):
            rec.update(changes);found=rec;break
    if found:save_seal_evaluations(data)
    return found

def finalize_seal_evaluation(evaluation_id,final_routes,result):
    final_signature=route_plan_signature(final_routes)
    data=load_seal_evaluations();found=None
    for rec in data.get("records",[]):
        if str(rec.get("evaluation_id") or "")==str(evaluation_id or ""):
            quality=seal_signature_quality(rec.get("proposed_signature",[]),final_signature)
            rec.update({"status":"saved","result":str(result or "edited"),"saved_at":datetime.datetime.now().isoformat(timespec="seconds"),
                        "final_signature":final_signature,"quality":quality});found=rec;break
    if found:save_seal_evaluations(data)
    return found

def seal_evaluation_summary(records=None):
    records=records if records is not None else load_seal_evaluations().get("records",[])
    viewed=len(records or []);applied=sum(1 for r in records or [] if r.get("status") in ("applied","saved"))
    saved=[r for r in records or [] if r.get("status")=="saved" and isinstance(r.get("quality"),dict)]
    accepted=sum(1 for r in saved if r.get("result")=="accepted")
    avg_exact=round(sum(int(r["quality"].get("exact_percent") or 0) for r in saved)/max(1,len(saved)))
    avg_route=round(sum(int(r["quality"].get("same_route_percent") or 0) for r in saved)/max(1,len(saved)))
    return {"viewed":viewed,"applied":applied,"saved":len(saved),"accepted":accepted,
            "edited":max(0,len(saved)-accepted),"apply_percent":round(applied/max(1,viewed)*100),
            "avg_exact_percent":avg_exact,"avg_same_route_percent":avg_route}

def seal_learning_maturity(records=None):
    """評估海豹對『這位玩家排法』的貼合度；不是速度排名，也不是全域最佳證明。"""
    records=records if records is not None else load_seal_evaluations().get("records",[])
    saved=sorted([r for r in records or [] if r.get("status")=="saved" and isinstance(r.get("quality"),dict)],
                 key=lambda r:str(r.get("saved_at") or r.get("created_at") or ""))
    n=len(saved)
    def avg(rows,key):return round(sum(int(r["quality"].get(key) or 0) for r in rows)/max(1,len(rows)))
    recent=saved[-5:];previous=saved[-10:-5]
    recent_route=avg(recent,"same_route_percent") if recent else 0
    recent_exact=avg(recent,"exact_percent") if recent else 0
    accepted=sum(1 for r in recent if r.get("result")=="accepted")
    if n<3:
        level="資料蒐集中";message=f"正式答案只有 {n} 份，現在還不能判斷海豹是否真的貼近你的習慣。"
    elif n<6:
        level="初步貼合";message=f"已有 {n} 份正式答案，可看出初步方向；換一批交換點時仍要人工確認。"
    elif recent_route>=80 and recent_exact>=60:
        level="穩定貼合";message=f"最近 {len(recent)} 份答案平均同線 {recent_route}%、原位 {recent_exact}%，已能穩定貼近你的排法。"
    elif recent_route>=60:
        level="逐漸成形";message=f"最近 {len(recent)} 份答案平均同線 {recent_route}%、原位 {recent_exact}%，分線方向已有共識，站序仍在學。"
    else:
        level="仍在摸索";message=f"最近 {len(recent)} 份答案平均同線 {recent_route}%、原位 {recent_exact}%，海豹的草稿仍常需要你調整。"
    trend="樣本不足，暫不比較趨勢"
    trend_delta=None
    if len(recent)>=3 and len(previous)>=3:
        old=avg(previous,"exact_percent");trend_delta=recent_exact-old
        trend=(f"最近原位貼合比前一段提高 {trend_delta} 個百分點" if trend_delta>=5 else
               (f"最近原位貼合比前一段下降 {abs(trend_delta)} 個百分點" if trend_delta<=-5 else "最近與前一段大致持平"))
    return {"saved":n,"level":level,"message":message,"recent_count":len(recent),
            "recent_same_route_percent":recent_route,"recent_exact_percent":recent_exact,
            "recent_accepted":accepted,"trend":trend,"trend_delta":trend_delta,
            "warning":"這只表示海豹貼近你的常用排法，不代表路線一定最快。"}

def seal_correction_patterns(evaluations=None,learning_records=None,min_samples=2):
    """找出草稿中最常被玩家換線或換位的點，只描述現象，不猜玩家修改原因。"""
    evaluations=evaluations if evaluations is not None else load_seal_evaluations().get("records",[])
    learning_records=learning_records if learning_records is not None else load_route_learning_history().get("records",[])
    names={}
    for rec in learning_records or []:
        for route in rec.get("routes",[]) or []:
            for point in route.get("points",[]) or []:
                pid=str(point.get("point_id") or "")
                if pid and point.get("point_name"):names[pid]=str(point["point_name"])
    stats={}
    def positions(sig):
        out={}
        for ri,row in enumerate(sig or []):
            for pi,pid in enumerate(row or []):
                pid=str(pid or "")
                if pid and pid not in out:out[pid]=(ri,pi)
        return out
    for rec in evaluations or []:
        if rec.get("status")!="saved":continue
        before=positions(rec.get("proposed_signature",[]));after=positions(rec.get("final_signature",[]))
        for pid,pos in before.items():
            s=stats.setdefault(pid,{"point_id":pid,"samples":0,"same_route":0,"exact":0,"missing":0})
            s["samples"]+=1
            if pid not in after:s["missing"]+=1;continue
            if after[pid][0]==pos[0]:s["same_route"]+=1
            if after[pid]==pos:s["exact"]+=1
    rows=[]
    for pid,s in stats.items():
        if s["samples"]<int(min_samples):continue
        n=s["samples"];row=dict(s);row["point_name"]=names.get(pid,pid)
        row["route_change_percent"]=round((n-s["same_route"])/n*100)
        row["position_change_percent"]=round((n-s["exact"])/n*100)
        rows.append(row)
    return sorted(rows,key=lambda x:(-x["position_change_percent"],-x["route_change_percent"],-x["samples"],x["point_name"]))

def seal_suggestion_readiness(proposal,maturity,risk_count=0):
    """把結構信心、個人貼合與本次覆蓋率合併成人話；永遠不授權自動套用。"""
    matched=int((proposal or {}).get("matched") or 0);eligible=int((proposal or {}).get("eligible") or 0)
    coverage=matched/max(1,eligible);confidence=str((proposal or {}).get("confidence") or "低")
    samples=int((maturity or {}).get("saved") or 0);level=str((maturity or {}).get("level") or "資料蒐集中")
    reasons=[]
    if coverage<0.75:reasons.append(f"本次只安全對上 {matched}/{eligible} 個可用站點")
    if confidence=="低":reasons.append("共同排法證據仍少")
    if samples<3:reasons.append(f"只有 {samples} 份正式答案可驗證貼合度")
    if risk_count:reasons.append(f"含 {risk_count} 個過去常被調整的站點")
    if coverage>=0.9 and confidence in ("中","高") and level=="穩定貼合" and not risk_count:
        grade="貼合度較穩定";action="可以優先拿來當草稿，但仍由你確認後手動套用。"
    elif coverage>=0.8 and confidence in ("中","高") and samples>=3:
        grade="可用草稿";action="大方向可參考，請特別檢查分線與標出的易調整點。"
    else:
        grade="先當參考";action="目前不要把它當答案，逐線確認後再決定是否套用。"
    return {"grade":grade,"action":action,"coverage_percent":round(coverage*100),"reasons":reasons,
            "warning":"成熟度與採用率都不會讓海豹自動改路線。"}

def route_learning_station(st,groups):
    """只有身份完整的站才進學習；問號與未確認ID不拿來猜玩家習慣。"""
    pid=str(st.get("point_id") or "").strip();iid=str(st.get("input_id") or "").strip();oid=str(st.get("output_id") or "").strip()
    point=str(st.get("point") or "").strip();inp=str(st.get("input") or "").strip();out=str(st.get("output") or "").strip()
    if not pid or not iid or not oid:return None
    if any(x in ("","?","？") or "?" in x or "？" in x for x in (point,inp,out)):return None
    return {"point_id":pid,"point_name":point,"group":str(groups.get(pid) or ""),
            "input_id":iid,"output_id":oid,"input_stage":str(st.get("input_stage") or ""),
            "output_stage":str(st.get("output_stage") or "")}

def route_plan_signature(routes):
    """只比較分線與點位順序；改路線名稱不算把海豹答案改壞。"""
    return [[str(st.get("point_id") or "") for st in route.get("stations",[]) or []]
            for route in routes or [] if route.get("stations")]

def remember_saved_route_plan(batch_id,routes,skip=False,suggestion_feedback=None):
    batch_id=str(batch_id or "").strip()
    if not batch_id:return {"saved":False,"reason":"沒有批次ID"}
    data=load_route_learning_history();records=[x for x in data.get("records",[]) if isinstance(x,dict)]
    old=next((x for x in records if str(x.get("batch_id") or "")==batch_id),None)
    if skip:
        data["records"]=[x for x in records if str(x.get("batch_id") or "")!=batch_id]
        save_route_learning_history(data)
        return {"saved":False,"removed":bool(old),"reason":"這一批不要學"}
    groups=load_player_point_groups();clean=[];excluded=0
    for route in routes or []:
        points=[]
        for st in route.get("stations",[]) or []:
            rec=route_learning_station(st,groups)
            if rec:points.append(rec)
            else:excluded+=1
        if points:clean.append({"name":str(route.get("name") or "路線"),"points":points})
    if not clean:return {"saved":False,"reason":"沒有身份完整的站點","excluded":excluded}
    now=datetime.datetime.now().isoformat(timespec="seconds")
    rec={"record_id":str(old.get("record_id")) if old else f"LEARN_{batch_id}","batch_id":batch_id,
         "created_at":str(old.get("created_at")) if old else now,"updated_at":now,
         "routes":clean,"excluded_stations":excluded}
    if isinstance(suggestion_feedback,dict):rec["suggestion_feedback"]=suggestion_feedback
    elif old and isinstance(old.get("suggestion_feedback"),dict):rec["suggestion_feedback"]=old["suggestion_feedback"]
    data["version"]=1;data["records"]=[x for x in records if str(x.get("batch_id") or "")!=batch_id]+[rec]
    save_route_learning_history(data)
    return {"saved":True,"updated":bool(old),"routes":len(clean),
            "stations":sum(len(x["points"]) for x in clean),"excluded":excluded}

def route_learning_summary(records=None):
    records=records if records is not None else load_route_learning_history().get("records",[])
    pair_counts={};point_counts={};route_count=0
    for rec in records or []:
        for route in rec.get("routes",[]) or []:
            pts=route.get("points",[]) or [];route_count+=1
            for p in pts:
                key=(str(p.get("point_id") or ""),str(p.get("point_name") or ""));point_counts[key]=point_counts.get(key,0)+1
            for a,b in zip(pts,pts[1:]):
                key=(str(a.get("point_name") or a.get("point_id") or ""),str(b.get("point_name") or b.get("point_id") or ""))
                pair_counts[key]=pair_counts.get(key,0)+1
    pairs=sorted(pair_counts.items(),key=lambda x:(-x[1],x[0]))
    points=sorted(point_counts.items(),key=lambda x:(-x[1],x[0]))
    feedback=[r.get("suggestion_feedback") for r in records or [] if isinstance(r.get("suggestion_feedback"),dict)]
    accepted=sum(1 for x in feedback if x.get("result")=="accepted")
    edited=sum(1 for x in feedback if x.get("result")=="edited")
    return {"batches":len(records or []),"routes":route_count,"pairs":pairs,"points":points,
            "suggestions":len(feedback),"accepted":accepted,"edited":edited}

def suggest_routes_from_learning(all_stations,records=None):
    """以相似舊排法為骨架，再用全部真實經驗的相鄰順序投票挑選草稿。"""
    records=records if records is not None else load_route_learning_history().get("records",[])
    stations=list(all_stations or [])
    by_pid={};invalid=[]
    for st in stations:
        pid=str(st.get("point_id") or "").strip();point=str(st.get("point") or "").strip()
        if not pid or not point or "?" in point or "？" in point:invalid.append(st);continue
        by_pid.setdefault(pid,[]).append(st)
    unique={pid:rows[0] for pid,rows in by_pid.items() if len(rows)==1}
    ambiguous=[st for rows in by_pid.values() if len(rows)>1 for st in rows]
    eligible=set(unique)
    pair_votes={};pair_records={}
    for rec in records or []:
        weight=3 if (rec.get("suggestion_feedback") or {}).get("result")=="edited" else 2
        seen_pairs=set()
        for route in rec.get("routes",[]) or []:
            ids=[str(p.get("point_id") or "") for p in route.get("points",[]) or []]
            for a,b in zip(ids,ids[1:]):
                if a and b:
                    pair_votes[(a,b)]=pair_votes.get((a,b),0)+weight;seen_pairs.add((a,b))
        for pair in seen_pairs:pair_records[pair]=pair_records.get(pair,0)+1
    best=None;best_key=None;relevant=0
    for rec in records or []:
        learned=[]
        for route in rec.get("routes",[]) or []:
            learned.extend(str(p.get("point_id") or "") for p in route.get("points",[]) or [])
        learned=set(x for x in learned if x)
        matched=len(eligible & learned)
        if not matched:continue
        relevant+=1
        consensus=0
        for route in rec.get("routes",[]) or []:
            ids=[str(p.get("point_id") or "") for p in route.get("points",[]) or [] if str(p.get("point_id") or "") in eligible]
            consensus+=sum(pair_votes.get((a,b),0) for a,b in zip(ids,ids[1:]))
        # 命中範圍優先；同樣完整時，選擇最符合多批共同順序的真實排法。
        key=(matched,consensus,matched/max(1,len(eligible)),str(rec.get("updated_at") or ""))
        if best_key is None or key>best_key:best_key=key;best=rec
    if not best:
        return {"routes":[],"loose":stations,"matched":0,"eligible":len(eligible),
                "ambiguous":len(ambiguous),"invalid":len(invalid),"record":None,
                "supporting_records":0,"confidence":"低","pair_support":[]}
    used=set();suggested=[]
    for learned_route in best.get("routes",[]) or []:
        picked=[]
        for p in learned_route.get("points",[]) or []:
            pid=str(p.get("point_id") or "")
            if pid in unique and pid not in used:
                picked.append(unique[pid]);used.add(pid)
        if picked:
            suggested.append({"name":str(learned_route.get("name") or f"建議路線 {len(suggested)+1}"),
                              "status":"尚未開始","current_index":0,"view_index":0,
                              "stations":picked,"source":"V5.73.0_SEAL_CONSENSUS"})
    loose_out=[st for st in stations if id(st) not in {id(unique[pid]) for pid in used}]
    support=[];edge_total=0;repeated=0
    for route in suggested:
        sts=route.get("stations",[]) or []
        for a,b in zip(sts,sts[1:]):
            edge_total+=1;pair=(str(a.get("point_id") or ""),str(b.get("point_id") or ""));n=pair_records.get(pair,0)
            if n>=2:repeated+=1
            support.append({"from":str(a.get("point") or ""),"to":str(b.get("point") or ""),"records":n})
    agreement=repeated/max(1,edge_total)
    confidence="高" if relevant>=6 and agreement>=0.6 else ("中" if relevant>=3 and agreement>=0.35 else "低")
    return {"routes":suggested,"loose":loose_out,"matched":len(used),"eligible":len(eligible),
            "ambiguous":len(ambiguous),"invalid":len(invalid),"record":best,
            "supporting_records":relevant,"confidence":confidence,"pair_support":support}

def station_transfer_fingerprint(st):
    """套用草稿只用來確認沒有掉站、重複或偷換交換內容。"""
    return tuple(str(st.get(k) or "") for k in
                 ("batch_id","point_id","input_id","output_id","point","input","output","times"))

def apply_route_suggestion_safely(current_routes,current_loose,proposal):
    """逐線照預覽順序寫回；任何順序或站點集合異常時拒絕套用。"""
    proposed_routes=proposal.get("routes",[]) if isinstance(proposal,dict) else []
    proposed_loose=proposal.get("loose",[]) if isinstance(proposal,dict) else []
    before=list(current_loose or [])+[st for route in current_routes or [] for st in route.get("stations",[]) or []]
    after=list(proposed_loose or [])+[st for route in proposed_routes for st in route.get("stations",[]) or []]
    if sorted(map(repr,map(station_transfer_fingerprint,before)))!=sorted(map(repr,map(station_transfer_fingerprint,after))):
        return {"ok":False,"reason":"草稿與目前站點不一致，已取消套用"}
    expected=route_plan_signature(proposed_routes)
    # 每條路線獨立複製，絕不把不同分線的局部位置拿去做全域排序。
    new_routes=[]
    for route in proposed_routes:
        new_route=copy.deepcopy(route);new_route["stations"]=[copy.deepcopy(st) for st in route.get("stations",[]) or []]
        new_routes.append(new_route)
    new_loose=[copy.deepcopy(st) for st in proposed_loose]
    if route_plan_signature(new_routes)!=expected:
        return {"ok":False,"reason":"套用後順序與預覽不同，已保留原排法"}
    return {"ok":True,"routes":new_routes,"loose":new_loose,"signature":expected}

def build_seal_decision_report(current_routes,current_loose,proposal,timing_rows=None):
    """把習慣、目前排法、時間、交換鏈與負重攤開；只解釋，不改路線。"""
    timing_rows=timing_rows if timing_rows is not None else load_route_timing_history()
    current_pos={}
    for ri,route in enumerate(current_routes or []):
        for si,st in enumerate(route.get("stations",[]) or []):
            pid=str(st.get("point_id") or "")
            if pid:current_pos[pid]=(ri,si)
    loose_ids={str(st.get("point_id") or "") for st in current_loose or []}
    support={(str(x.get("from") or ""),str(x.get("to") or "")):int(x.get("records") or 0)
             for x in proposal.get("pair_support",[]) or []}
    moved_route=moved_position=from_loose=0;route_rows=[]
    for ri,route in enumerate(proposal.get("routes",[]) or []):
        sts=route.get("stations",[]) or [];dependencies=[]
        for si,st in enumerate(sts):
            pid=str(st.get("point_id") or "")
            if pid in loose_ids:from_loose+=1
            elif pid in current_pos:
                oldri,oldsi=current_pos[pid]
                if oldri!=ri:moved_route+=1
                elif oldsi!=si:moved_position+=1
        for i,a in enumerate(sts):
            out_id=str(a.get("output_id") or "")
            if not out_id:continue
            for j,b in enumerate(sts):
                if i<j and out_id==str(b.get("input_id") or ""):
                    dependencies.append(f"{a.get('point') or '前站'} → {b.get('point') or '後站'}")
        strong=[]
        for a,b in zip(sts,sts[1:]):
            n=support.get((str(a.get("point") or ""),str(b.get("point") or "")),0)
            if n>=2:strong.append(f"{a.get('point')}→{b.get('point')}（{n}批）")
        estimate=estimate_route_duration(route,timing_rows)
        time_review=suggest_time_optimized_order(route,timing_rows)
        weight=route_peak_weight(route)
        reasons=[]
        reasons.append("共同習慣："+("、".join(strong) if strong else "這條線目前沒有達到2批的相鄰共識"))
        if estimate.get("seconds") is None:reasons.append("時間：尚無足夠有效樣本，不判斷快慢")
        elif estimate.get("exact"):reasons.append(f"時間：相同整線 {estimate.get('samples',0)} 趟，中位預估 {format_route_duration(estimate['seconds'])}")
        else:reasons.append(f"時間：只知道站內航段至少 {format_route_duration(estimate['seconds'])}，不含出發到第一站")
        if time_review.get("status")=="suggestion":
            reasons.append(f"省時體檢：同線另有約省 {format_route_duration(time_review['saving_seconds'])} 的有證據順序；草稿仍優先照你的習慣")
        elif time_review.get("status")=="no_improvement":reasons.append("省時體檢：目前順序是已知資料中的最佳或同分")
        else:reasons.append("省時體檢："+str(time_review.get("reason") or "資料不足"))
        reasons.append("交換鏈："+("、".join(dependencies) if dependencies else "沒有偵測到必須前後相接的產出→投入"))
        unknown=weight.get("unknown_items") or []
        reasons.append(f"負重：已知最高 {float(weight.get('peak_weight') or 0):,.0f} LT，出現在 {weight.get('peak_location') or '未知'}"+
                       (("；缺重量 "+"、".join(unknown)) if unknown else ""))
        route_rows.append({"name":route.get("name") or f"建議路線 {ri+1}","reasons":reasons,
                           "strong_pairs":strong,"dependencies":dependencies,"estimate":estimate,
                           "time_review":time_review,"weight":weight})
    same=route_plan_signature(current_routes or [])==route_plan_signature(proposal.get("routes",[]) or []) and not (current_loose or proposal.get("loose"))
    return {"same_as_current":same,"moved_route":moved_route,"moved_position":moved_position,
            "from_loose":from_loose,"routes":route_rows,
            "warning":"海豹草稿學的是你的常用排法，不等於全世界最快；時間與負重只在資料足夠時提供佐證。"}

PLAYER_POINT_PREFS_PATH=shared_file("player_point_preferences.json", fallback_local=False)
def load_player_point_preferences():
    try:
        with open(PLAYER_POINT_PREFS_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):return x
    except Exception:pass
    seed={}
    try:
        with open(os.path.join(BASE,"player_point_preferences_seed.json"),"r",encoding="utf-8") as f:x=json.load(f)
        if isinstance(x,dict):seed=x
    except Exception:pass
    try:
        os.makedirs(os.path.dirname(PLAYER_POINT_PREFS_PATH),exist_ok=True)
        with open(PLAYER_POINT_PREFS_PATH,"w",encoding="utf-8") as f:json.dump(seed,f,ensure_ascii=False,indent=2)
    except Exception:pass
    return seed

INVENTORY_PATH=shared_file("inventory.json", fallback_local=False)
INVENTORY_CATALOG_PATH=shared_file("inventory_catalog.json", fallback_local=True)
def load_inventory():
    try:
        with open(INVENTORY_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,dict) else {}
    except Exception:
        try:
            seed=os.path.join(BASE,"inventory.json")
            with open(seed,"r",encoding="utf-8") as f:x=json.load(f)
            if isinstance(x,dict):
                save_inventory(x); return x
        except Exception:pass
        return {}
def load_inventory_catalog():
    for fp in (INVENTORY_CATALOG_PATH,os.path.join(BASE,"inventory_catalog.json")):
        try:
            with open(fp,"r",encoding="utf-8") as f:x=json.load(f)
            if isinstance(x,list):return x
        except Exception:pass
    return []
def save_inventory(d):
    os.makedirs(os.path.dirname(INVENTORY_PATH),exist_ok=True)
    with open(INVENTORY_PATH,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)


INVENTORY_LEDGER_PATH=shared_file("inventory_ledger.json", fallback_local=False)
INVENTORY_BASELINE_PATH=shared_file("inventory_baseline.json", fallback_local=False)
TEST_INVENTORY_PATH=shared_file("inventory_test.json", fallback_local=False)
TEST_LEDGER_PATH=shared_file("inventory_test_ledger.json", fallback_local=False)
INVENTORY_MODE_PATH=shared_file("inventory_mode.json", fallback_local=False)


def load_inventory_mode():
    try:
        with open(INVENTORY_MODE_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        return x.get("mode","test") if isinstance(x,dict) else "test"
    except Exception:return "test"

def save_inventory_mode(mode):
    os.makedirs(os.path.dirname(INVENTORY_MODE_PATH),exist_ok=True)
    with open(INVENTORY_MODE_PATH,"w",encoding="utf-8") as f:json.dump({"mode":mode},f,ensure_ascii=False,indent=2)

def _load_json_dict(path):
    try:
        with open(path,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,dict) else {}
    except Exception:return {}

def _save_json_dict(path,x):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:json.dump(x,f,ensure_ascii=False,indent=2)

def ensure_test_inventory(reset=False):
    if reset or not os.path.exists(TEST_INVENTORY_PATH):
        _save_json_dict(TEST_INVENTORY_PATH,load_inventory())
        with open(TEST_LEDGER_PATH,"w",encoding="utf-8") as f:json.dump([],f,ensure_ascii=False,indent=2)
    return _load_json_dict(TEST_INVENTORY_PATH)

def active_inventory():
    return ensure_test_inventory() if load_inventory_mode()=="test" else load_inventory()

def save_active_inventory(inv):
    if load_inventory_mode()=="test":_save_json_dict(TEST_INVENTORY_PATH,inv)
    else:save_inventory(inv)

def active_ledger_path():
    return TEST_LEDGER_PATH if load_inventory_mode()=="test" else INVENTORY_LEDGER_PATH

def load_inventory_ledger():
    path=active_ledger_path()
    try:
        with open(path,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,list) else []
    except Exception:return []

def save_inventory_ledger(rows):
    path=active_ledger_path()
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:json.dump(rows,f,ensure_ascii=False,indent=2)

def _inv_int(v):
    try:return int(v)
    except Exception:return None

def normalize_decimal_text(value):
    """接受主鍵盤／數字鍵盤與中英文輸入法常見的小數符號。"""
    return (str(value or "").strip().replace("%","").replace("％","").replace("，",",")
            .replace("。",".").replace("．",".").replace(",","."))

def parse_percent(value):
    return float(normalize_decimal_text(value))

def is_decimal_input_event(keysym="", char=""):
    """辨識 Windows 主鍵盤、數字鍵盤及中英文輸入法送出的小數點事件。"""
    key=str(keysym or "").strip().lower()
    return key in ("decimal","kp_decimal","period") or str(char or "") in (".",",","，","。","．")

def active_inventory_path():
    return TEST_INVENTORY_PATH if load_inventory_mode()=="test" else INVENTORY_PATH

def inventory_mode_label():
    return "🧪 測試庫存" if load_inventory_mode()=="test" else "🟢 正式庫存"

def apply_station_inventory(st, route_name="", route_index=None):
    """完成一站：扣投入、加產出；同站只允許正式入帳一次。未知庫存不擅自猜。"""
    if st.get("inventory_tx_id"):
        return True, "already"
    _pid=str(st.get("point_id") or "")
    _iid=str(st.get("input_id") or "")
    _oid=str(st.get("output_id") or "")
    _batch_id=str(st.get("batch_id") or "")
    for _row in reversed(load_inventory_ledger()):
        if _row.get("reversed"):continue
        # 跨刷新可能出現完全相同的點位與品項；舊批次交易不能擋住新批次入帳。
        # 舊站點沒有 batch_id 時仍沿用舊防重邏輯，避免升級當下重扣。
        _row_batch=str(_row.get("batch_id") or "")
        if _batch_id and _row_batch != _batch_id:
            continue
        if (str(_row.get("point_id") or "")==_pid and str(_row.get("input_id") or "")==_iid
            and str(_row.get("output_id") or "")==_oid and str(_row.get("route") or "")==str(route_name or "")):
            st["inventory_tx_id"]=_row.get("tx_id")
            return True,"already"
    inv=active_inventory()
    times=max(0,_inv_int(st.get("times")) or 0)
    in_id=str(st.get("input_id") or ""); out_id=str(st.get("output_id") or "")
    in_qty=max(0,_inv_int(st.get("input_qty")) or 0)*times
    out_qty=max(0,_inv_int(st.get("output_qty")) or 0)*times
    before_in=inv.get(in_id,None) if in_id else None
    before_out=inv.get(out_id,None) if out_id else None
    # 有正式管理投入庫存時，完成前再做一次最後防線。
    if in_id and before_in is not None:
        bi=_inv_int(before_in)
        if bi is not None and bi<in_qty:
            return False, f"庫存不足：{st.get('input','交換材料')} 目前 {bi:,}，本站需要 {in_qty:,}。"
    # 未知庫存維持未知，不把「—」憑空變成數字。
    if in_id and before_in is not None:
        inv[in_id]=(_inv_int(before_in) or 0)-in_qty
    if out_id and before_out is not None:
        inv[out_id]=(_inv_int(before_out) or 0)+out_qty
    tx=f"TX-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    row={"tx_id":tx,"time":datetime.datetime.now().isoformat(timespec="seconds"),
         "batch_id":_batch_id,
         "action":"完成交換","route":route_name,"station_index":route_index,
         "point":st.get("point",""),"point_id":st.get("point_id",""),
         "input_id":in_id,"input":st.get("input",""),
         "input_delta":-in_qty,"input_before":before_in,"input_after":inv.get(in_id,None) if in_id else None,
         "output_id":out_id,"output":st.get("output",""),"output_delta":out_qty,
         "output_before":before_out,"output_after":inv.get(out_id,None) if out_id else None,
         "times":times}
    save_active_inventory(inv)
    led=load_inventory_ledger();led.append(row);save_inventory_ledger(led)
    st["inventory_tx_id"]=tx
    return True,row

def reverse_station_inventory(st, reason="撤銷交換"):
    """撤銷已完成站：只反轉該站自己曾經做過的正式庫存交易。"""
    tx=str(st.get("inventory_tx_id") or "")
    if not tx:return True,"no_tx"
    led=load_inventory_ledger()
    row=next((x for x in reversed(led) if str(x.get("tx_id"))==tx),None)
    if not row:return False,"找不到這站的庫存異動紀錄，為避免算錯，沒有自動改庫存。"
    if row.get("reversed"):return True,"already_reversed"
    inv=active_inventory()
    iid=str(row.get("input_id") or "");oid=str(row.get("output_id") or "")
    # V5.64：撤銷只反轉「本交易 delta」，不可用舊 before snapshot 覆蓋現況。
    if iid and row.get("input_before") is not None:
        cur=_inv_int(inv.get(iid))
        if cur is None:return False,f"{row.get('input') or '投入材料'}目前不是有效庫存數字，無法安全撤銷。"
        inv[iid]=cur-int(row.get("input_delta") or 0)
    if oid and row.get("output_before") is not None:
        cur=_inv_int(inv.get(oid))
        if cur is None:return False,f"{row.get('output') or '目標物品'}目前不是有效庫存數字，無法安全撤銷。"
        inv[oid]=cur-int(row.get("output_delta") or 0)
    save_active_inventory(inv)
    row["reversed"]=True
    row["reversed_time"]=datetime.datetime.now().isoformat(timespec="seconds")
    row["reverse_reason"]=reason
    row["reverse_input_after"]=inv.get(iid,None) if iid else None
    row["reverse_output_after"]=inv.get(oid,None) if oid else None
    save_inventory_ledger(led)
    st.pop("inventory_tx_id",None)
    return True,row

def get_item_catalog_row(item_id):
    iid=str(item_id or "")
    for x in load_inventory_catalog():
        if str(x.get("item_id") or x.get("id") or "")==iid:return x
    return {}

def normalize_stage_value(value):
    """把 5、5.0 等階段資料統一成字串；所有頁面都能使用。"""
    try:return str(int(float(value)))
    except:return str(value or "")

def normalize_fixed_stage_output_quantity(row,route_station=False):
    """一般相鄰段位固定規則：3→4 是 1:2；4→5、5→6、6→7 是 1:1。"""
    try:
        a=int(row.get("input_stage") or 0);b=int(row.get("output_stage") or 0)
    except Exception:return False
    if not (b==a+1 and 3<=a<=6):return False
    key="output_qty" if route_station else "output_quantity"
    fixed=2 if (a,b)==(3,4) else 1
    old=row.get(key)
    if old==fixed:return False
    row[key]=fixed
    if not route_station:
        row["exchange_ratio_input"]=1;row["exchange_ratio_output"]=fixed
        row["exchange_ratio"]=f"1 : {fixed}（固定規則）"
    return True

def normalize_unstarted_route_quantities(state_data):
    """舊批次可直接沿用；只修尚未完成的站，避免碰已入帳歷史。"""
    changed=0
    for route in (state_data or {}).get("routes",[]) or []:
        for st in route.get("stations",[]) or []:
            if st.get("status","未開始")!="未開始":continue
            if normalize_fixed_stage_output_quantity(st,route_station=True):changed+=1
    return changed

def toggle_route_daze(route,view_index=0,recorded_at=None):
    """無確認視窗的雙向發呆切換；只改本路線計時有效性。"""
    was_dazed=(route.get("timing_valid") is False and route.get("timing_invalid_reason")=="發呆")
    if was_dazed:
        route["timing_valid"]=True;route.pop("timing_invalid_reason",None);action="撤回發呆"
    else:
        route["timing_valid"]=False;route["timing_invalid_reason"]="發呆";action="標記發呆"
    stamp=recorded_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    route.setdefault("daze_events",[]).append({"action":action,"view_index":view_index,"recorded_at":stamp})
    return not was_dazed

def route_timer_elapsed(route,now=None):
    now=float(now if now is not None else datetime.datetime.now().timestamp())
    total=float(route.get("timing_elapsed_seconds") or 0)
    running=route.get("timing_running_since")
    if running is not None:
        try:total+=max(0.0,now-float(running))
        except Exception:pass
    return max(0.0,total)

def route_timer_start(route,now=None):
    now=float(now if now is not None else datetime.datetime.now().timestamp())
    if route.get("timing_running_since") is not None:return False
    if not route.get("timing_started_at"):route["timing_started_at"]=now;action="start"
    else:action="resume"
    route["timing_running_since"]=now
    route.setdefault("timing_events",[]).append({"action":action,"at":now})
    return True

def route_timer_pause(route,now=None):
    now=float(now if now is not None else datetime.datetime.now().timestamp())
    running=route.pop("timing_running_since",None)
    if running is None:return False
    try:route["timing_elapsed_seconds"]=float(route.get("timing_elapsed_seconds") or 0)+max(0.0,now-float(running))
    except Exception:pass
    route.setdefault("timing_events",[]).append({"action":"pause","at":now})
    return True

def route_timer_finish(route,now=None):
    if not route.get("timing_started_at"):return False
    now=float(now if now is not None else datetime.datetime.now().timestamp())
    route_timer_pause(route,now);route["timing_finished_at"]=now
    route.setdefault("timing_events",[]).append({"action":"finish","at":now})
    return True

def format_route_duration(seconds):
    total=max(0,int(round(float(seconds or 0))));h,rem=divmod(total,3600);m,s=divmod(rem,60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def load_route_timing_history():
    try:
        with open(ROUTE_TIMING_HISTORY_PATH,"r",encoding="utf-8") as f:x=json.load(f)
        return x if isinstance(x,list) else []
    except Exception:return []

def save_route_timing_history(rows):
    os.makedirs(os.path.dirname(ROUTE_TIMING_HISTORY_PATH),exist_ok=True)
    with open(ROUTE_TIMING_HISTORY_PATH,"w",encoding="utf-8") as f:json.dump(rows,f,ensure_ascii=False,indent=2)

def median_value(values):
    vals=sorted(float(x) for x in values)
    if not vals:return None
    n=len(vals);mid=n//2
    return vals[mid] if n%2 else (vals[mid-1]+vals[mid])/2.0

def estimate_route_duration(route,rows=None):
    """先找整線相同樣本；否則僅在每個相鄰航段都有有效證據時才組合估算。"""
    rows=rows if rows is not None else load_route_timing_history()
    signature=[str(x.get("point_id") or x.get("point") or "") for x in route.get("stations",[]) or []]
    samples=[]
    for row in rows or []:
        if row.get("valid") is False:continue
        if [str(x) for x in row.get("point_ids",[]) or []]!=signature:continue
        try:
            sec=float(row.get("elapsed_seconds") or 0)
            if sec>0:samples.append(sec)
        except Exception:pass
    if samples:return {"seconds":median_value(samples),"samples":len(samples),"exact":True,"method":"整線"}
    segment_samples={}
    for row in rows or []:
        if row.get("valid") is False:continue
        prev=None;prev_elapsed=None
        for cp in row.get("station_checkpoints",[]) or []:
            pid=str(cp.get("point_id") or "")
            try:elapsed=float(cp.get("elapsed_seconds") or 0)
            except Exception:continue
            if not pid:continue
            if prev is not None and prev_elapsed is not None:
                if elapsed<prev_elapsed:continue
                sec=elapsed-prev_elapsed
                if sec>0:segment_samples.setdefault((prev,pid),[]).append(sec)
            prev=pid;prev_elapsed=elapsed
    if len(signature)<2:return {"seconds":None,"samples":0,"exact":False,"method":"不足","partial":False}
    total=0.0;counts=[];prev=signature[0]
    for pid in signature[1:]:
        vals=segment_samples.get((prev,pid),[])
        if not vals:return {"seconds":None,"samples":0,"exact":False,"method":"不足","partial":False}
        total+=float(median_value(vals) or 0);counts.append(len(vals));prev=pid
    return {"seconds":total,"samples":min(counts) if counts else 0,"exact":False,"method":"站內分段","partial":True}

def route_timing_summary(rows=None):
    rows=rows if rows is not None else load_route_timing_history()
    valid=[];invalid=0
    for row in rows or []:
        try:sec=float(row.get("elapsed_seconds") or 0)
        except Exception:sec=0
        if row.get("valid") is False:invalid+=1
        elif sec>0:valid.append(sec)
    return {"total":len(rows or []),"valid":len(valid),"invalid":invalid,
            "median":median_value(valid),"average":(sum(valid)/len(valid) if valid else None)}

def route_segment_statistics(rows=None):
    """只彙整有效趟次中實際連續完成的站內航段。"""
    rows=rows if rows is not None else load_route_timing_history();groups={}
    for row in rows or []:
        if row.get("valid") is False:continue
        prev=None;prev_elapsed=None
        for cp in row.get("station_checkpoints",[]) or []:
            pid=str(cp.get("point_id") or "");name=str(cp.get("point_name") or pid)
            try:elapsed=float(cp.get("elapsed_seconds") or 0)
            except Exception:continue
            if not pid:continue
            if prev and prev_elapsed is not None and elapsed>prev_elapsed:
                key=(prev[0],pid);g=groups.setdefault(key,{"from_id":prev[0],"from_name":prev[1],"to_id":pid,"to_name":name,"seconds":[]})
                g["seconds"].append(elapsed-prev_elapsed)
            prev=(pid,name);prev_elapsed=elapsed
    out=[]
    for g in groups.values():
        vals=g.pop("seconds");n=len(vals);med=float(median_value(vals) or 0)
        confidence="新樣本" if n==1 else ("低" if n==2 else ("中" if n<=5 else "高"))
        volatile=bool(n>=2 and med>0 and (max(vals)-min(vals))/med>0.5)
        out.append({**g,"count":n,"median":med,"fastest":min(vals),"slowest":max(vals),
                    "confidence":confidence,"volatile":volatile})
    return sorted(out,key=lambda x:(-x["count"],x["from_name"],x["to_name"]))

def suggest_time_optimized_order(route,rows=None,max_stations=11):
    """固定第一站並遵守產出→投入依賴；只有可靠航段足夠時才比較站內順序。"""
    sts=list(route.get("stations",[]) or []);n=len(sts)
    if n<3:return {"status":"insufficient","reason":"少於3站，沒有必要調整"}
    if n>max_stations:return {"status":"insufficient","reason":f"超過{max_stations}站，保守模式不做排列搜尋"}
    pids=[str(x.get("point_id") or "") for x in sts]
    if any(not x for x in pids) or len(set(pids))!=len(pids):
        return {"status":"insufficient","reason":"點位ID不完整或同點重複"}
    edge={}
    for x in route_segment_statistics(rows):
        if x["count"]>=2 and not x["volatile"]:
            edge[(x["from_id"],x["to_id"])]=float(x["median"])
    current_edges=[edge.get((pids[i],pids[i+1])) for i in range(n-1)]
    if any(x is None for x in current_edges):
        return {"status":"insufficient","reason":"目前順序仍缺少至少2份穩定航段樣本"}
    predecessors=[0]*n
    for i,a in enumerate(sts):
        out_id=str(a.get("output_id") or "")
        if not out_id:continue
        for j,b in enumerate(sts):
            if i!=j and out_id==str(b.get("input_id") or ""):predecessors[j]|=1<<i
    if predecessors[0]:return {"status":"insufficient","reason":"第一站前仍有交換鏈依賴，不能固定第一站重排"}
    start=(1,0);dp={start:(0.0,[0])}
    full=(1<<n)-1
    for mask in range(1,full+1):
        states=[(last,cost,path) for (m,last),(cost,path) in list(dp.items()) if m==mask]
        for last,cost,path in states:
            for j in range(1,n):
                if mask&(1<<j) or predecessors[j]&~mask:continue
                sec=edge.get((pids[last],pids[j]))
                if sec is None:continue
                key=(mask|(1<<j),j);candidate=(cost+sec,path+[j])
                if key not in dp or candidate[0]<dp[key][0]:dp[key]=candidate
    finals=[v for (mask,_last),v in dp.items() if mask==full]
    if not finals:return {"status":"insufficient","reason":"可靠航段不足，找不到完整且不破壞交換鏈的替代順序"}
    best_cost,best_path=min(finals,key=lambda x:x[0]);current=sum(float(x) for x in current_edges)
    if best_path==list(range(n)) or best_cost>=current-1:
        return {"status":"no_improvement","current_seconds":current,"best_seconds":best_cost,"reason":"目前順序已是已知資料中的最佳或同分解"}
    return {"status":"suggestion","current_seconds":current,"best_seconds":best_cost,
            "saving_seconds":current-best_cost,"saving_percent":(current-best_cost)/current*100 if current else 0,
            "stations":[sts[i] for i in best_path],"path":best_path}

def apply_time_optimized_orders_safely(routes,reports):
    """只重排完全未執行的原路線；任何掉站、換貨或狀態差異都整批取消。"""
    proposed=[];changed=0
    report_by_route={id(route):report for route,report in reports}
    def identity(st):
        return (str(st.get("point_id") or ""),str(st.get("input_id") or ""),
                str(st.get("output_id") or ""),int(st.get("times") or 1))
    for route in routes:
        nr=dict(route);old=list(route.get("stations",[]) or []);report=report_by_route.get(id(route),{})
        if report.get("status")!="suggestion":
            nr["stations"]=old;proposed.append(nr);continue
        if any(str(st.get("status") or "未開始")!="未開始" for st in old):
            return {"ok":False,"reason":f"「{route.get('name') or '路線'}」已經執行過，不能自動重排。"}
        new=list(report.get("stations",[]) or [])
        if len(new)!=len(old) or sorted(map(identity,new))!=sorted(map(identity,old)):
            return {"ok":False,"reason":f"「{route.get('name') or '路線'}」的站點安全檢查不一致。"}
        if [identity(x) for x in new]!=[identity(x) for x in old]:changed+=1
        nr["stations"]=new;proposed.append(nr)
    return {"ok":True,"routes":proposed,"changed":changed}

def sync_route_timing_sample(route,batch_id=""):
    """完成後保存／更新一筆可分析樣本；發呆趟保留但標為無效。"""
    if not route.get("timing_finished_at"):return None
    rows=load_route_timing_history();sid=str(route.get("timing_sample_id") or "")
    if not sid:
        sid=f"TIMER-{int(float(route['timing_finished_at'])*1000)}-{len(rows)+1}";route["timing_sample_id"]=sid
    sample={"sample_id":sid,"batch_id":batch_id,"route_name":str(route.get("name") or "路線"),
            "station_count":len(route.get("stations",[]) or []),
            "point_ids":[str(x.get("point_id") or x.get("point") or "") for x in route.get("stations",[]) or []],
            "started_at":route.get("timing_started_at"),"finished_at":route.get("timing_finished_at"),
            "elapsed_seconds":route_timer_elapsed(route),
            "station_checkpoints":copy.deepcopy(route.get("timing_station_checkpoints",[]) or []),
            "valid":not (route.get("timing_valid") is False),
            "invalid_reason":str(route.get("timing_invalid_reason") or "")}
    hit=next((i for i,x in enumerate(rows) if str(x.get("sample_id") or "")==sid),None)
    if hit is None:rows.append(sample)
    else:rows[hit]=sample
    save_route_timing_history(rows);return sample

def reset_route_timer(route,remove_sample=True):
    sid=str(route.get("timing_sample_id") or "")
    if remove_sample and sid:
        rows=[x for x in load_route_timing_history() if str(x.get("sample_id") or "")!=sid]
        save_route_timing_history(rows)
    for key in ("timing_started_at","timing_running_since","timing_finished_at","timing_elapsed_seconds","timing_sample_id"):
        route.pop(key,None)
    route["timing_events"]=[]
    route["timing_station_checkpoints"]=[]

def record_route_station_checkpoint(route,station,index):
    """玩家完成站點時記錄有效累積時間；跳過不代表真的到站，所以不記。"""
    try:index=int(index)
    except Exception:return None
    rows=[x for x in route.get("timing_station_checkpoints",[]) or [] if int(x.get("station_index",-1))<index]
    cp={"station_index":index,"point_id":str(station.get("point_id") or station.get("point") or ""),
        "point_name":str(station.get("point") or ""),"elapsed_seconds":route_timer_elapsed(route)}
    rows.append(cp);route["timing_station_checkpoints"]=rows;return cp

def strikethrough_text(value):
    """用組合刪除線顯示已完成站名，不改原始點位名稱。"""
    return "".join(ch+"\u0336" for ch in str(value or ""))

def t5_excess_for_item(item_id):
    """回傳 (是否5階且啟用上限, 目前庫存, 上限, 溢出量, 名稱)。"""
    row=get_item_catalog_row(item_id)
    if normalize_stage_value(row.get("stage"))!="5":return False,None,None,0,row.get("name") or row.get("item_name") or ""
    if not bool(PLAYER_SETTINGS.get("t5_keep_cap_enabled",True)):return False,None,None,0,row.get("name") or row.get("item_name") or ""
    try:cap=max(0,int(PLAYER_SETTINGS.get("t5_keep_cap",8)))
    except:cap=8
    inv=active_inventory();cur=_inv_int(inv.get(str(item_id)))
    excess=max(0,cur-cap) if cur is not None else 0
    return True,cur,cap,excess,row.get("name") or row.get("item_name") or ""

def t5_sale_info_for_station(st):
    """只依傳入的這一站判斷5階出售提示，避免畫面迴圈殘留的其他站變數混入。"""
    oid=str((st or {}).get("output_id") or "")
    cat=get_item_catalog_row(oid) if oid else {}
    is5,cur,cap,excess,name=t5_excess_for_item(oid)
    return {"output_id":oid,"catalog":cat,"is5":is5,"current":cur,"cap":cap,
            "excess":excess,"name":name}

def t5_sale_infos_for_route(route):
    """整趟結束後一次整理本路線產出的5階品；同品項只列一次。"""
    rows=[];seen=set()
    for st in (route or {}).get("stations",[]) or []:
        if st.get("status")!="完成":continue
        info=t5_sale_info_for_station(st);iid=str(info.get("output_id") or "")
        if not iid or iid in seen or not info.get("is5"):continue
        seen.add(iid);info["display_name"]=str(st.get("output") or info.get("name") or iid);rows.append(info)
    return rows

def sell_t5_excess(item_id,item_name="",route=None,source_station=None):
    """玩家確認已在遊戲賣出後才扣庫存並記帳。"""
    ok,cur,cap,excess,name=t5_excess_for_item(item_id)
    if not ok:return False,"這不是啟用5階上限的品項。"
    if cur is None:return False,"這個品項目前沒有可管理的庫存數字。"
    if excess<=0:return False,"目前沒有超過保留上限。"
    inv=active_inventory();iid=str(item_id);before=cur;after=cur-excess
    inv[iid]=after;save_active_inventory(inv)
    source_station=source_station or {}
    append_manual_inventory_ledger(
        iid,item_name or name or iid,before,after,"出售多餘5階品",
        route_name=(route or {}).get("name","") if isinstance(route,dict) else "",
        batch_id=source_station.get("batch_id","") or ((route or {}).get("batch_id","") if isinstance(route,dict) else ""),
        source_tx_id=source_station.get("inventory_tx_id","")
    )
    return True,{"sold":excess,"before":before,"after":after,"cap":cap}

def inventory_audit_snapshot(state_data=None):
    """只讀稽核：不修改任何庫存/路線/帳本。"""
    inv=active_inventory()
    led=load_inventory_ledger()
    txmap={str(x.get("tx_id") or ""):x for x in led if isinstance(x,dict)}
    completed=[]
    orphan=[]
    duplicate=[]
    seen={}
    for rr in ((state_data or {}).get("routes") or []):
        for st in (rr.get("stations") or []):
            if st.get("status")!="完成":continue
            tx=str(st.get("inventory_tx_id") or "")
            rec={"route":rr.get("name",""),"point":st.get("point",""),"tx_id":tx,
                 "input":st.get("input",""),"times":st.get("times",1)}
            completed.append(rec)
            if not tx or tx not in txmap:
                orphan.append(rec)
            elif txmap[tx].get("reversed"):
                orphan.append(dict(rec,reason="站點完成，但帳本交易已標撤銷"))
            if tx:
                seen[tx]=seen.get(tx,0)+1
    duplicate=[tx for tx,n in seen.items() if n>1]
    return {"inventory_path":active_inventory_path(),"ledger_path":active_ledger_path(),
            "mode":load_inventory_mode(),"mode_label":inventory_mode_label(),
            "inventory_items":len(inv),"ledger_rows":len(led),
            "completed_stations":completed,"orphan_completed":orphan,
            "duplicate_tx_ids":duplicate}

def append_manual_inventory_ledger(iid,name,before,after,reason="手動調整",
                                   route_name="",batch_id="",source_tx_id=""):
    """手動庫存修改也留下帳；只記真正有變化的數字。"""
    if before==after:return
    try:delta=int(after)-int(before)
    except:return
    led=load_inventory_ledger()
    led.append({"tx_id":f"MAN-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "time":datetime.datetime.now().isoformat(timespec="seconds"),
                "action":reason,"route":route_name,"batch_id":batch_id,"point":"","input_id":iid,"input":name,
                "input_delta":delta,"input_before":before,"input_after":after,
                "output_id":"","output":"","output_delta":0,"reversed":False,
                "source_tx_id":source_tx_id})
    save_inventory_ledger(led)

def reverse_linked_t5_sales(st,reason="撤銷交換"):
    """撤銷交換時，一併撤銷由該次完成所觸發的5階出售。"""
    source_tx=str((st or {}).get("inventory_tx_id") or "")
    if not source_tx:return 0
    led=load_inventory_ledger();inv=active_inventory();changed=0
    for row in led:
        if (row.get("action")!="出售多餘5階品" or row.get("reversed")
                or str(row.get("source_tx_id") or "")!=source_tx):
            continue
        iid=str(row.get("input_id") or "");cur=_inv_int(inv.get(iid))
        if not iid or cur is None:continue
        inv[iid]=cur-int(row.get("input_delta") or 0)
        row["reversed"]=True
        row["reversed_time"]=datetime.datetime.now().isoformat(timespec="seconds")
        row["reverse_reason"]=reason+"（連動還原5階出售）"
        row["reverse_input_after"]=inv.get(iid)
        changed+=1
    if changed:
        save_active_inventory(inv);save_inventory_ledger(led)
    return changed

def repair_legacy_reversed_t5_sales():
    """一次性修復舊版：交換已撤銷，但該次觸發的5階出售仍留在庫存。"""
    led=load_inventory_ledger();inv=active_inventory();changed=False
    for i,row in enumerate(led):
        if row.get("action")!="出售多餘5階品" or row.get("source_tx_id"):
            continue
        iid=str(row.get("input_id") or "")
        producer=None
        for old in reversed(led[:i]):
            if (old.get("action")=="完成交換" and str(old.get("output_id") or "")==iid
                    and old.get("output_after") is not None
                    and _inv_int(old.get("output_after"))==_inv_int(row.get("input_before"))):
                producer=old;break
        if not producer:continue
        row["source_tx_id"]=producer.get("tx_id","")
        row["route"]=producer.get("route","");row["batch_id"]=producer.get("batch_id","")
        changed=True
        if producer.get("reversed") and not row.get("reversed"):
            cur=_inv_int(inv.get(iid))
            if cur is None:continue
            inv[iid]=cur-int(row.get("input_delta") or 0)
            row["reversed"]=True
            row["reversed_time"]=datetime.datetime.now().isoformat(timespec="seconds")
            row["reverse_reason"]="V5.69.2 修復：來源交換早已撤銷"
            row["reverse_input_after"]=inv.get(iid)
    if changed:
        save_active_inventory(inv);save_inventory_ledger(led)
    return changed

def load_inventory_baseline():
    try:
        with open(INVENTORY_BASELINE_PATH,"r",encoding="utf-8") as f:
            x=json.load(f)
        return x if isinstance(x,dict) else {}
    except Exception:return {}

def save_inventory_baseline(data):
    os.makedirs(os.path.dirname(INVENTORY_BASELINE_PATH),exist_ok=True)
    with open(INVENTORY_BASELINE_PATH,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

def create_inventory_baseline():
    inv=load_inventory();led=load_inventory_ledger()
    now=datetime.datetime.now().isoformat(timespec="seconds")
    base={"baseline_id":f"BASE-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}",
          "created_at":now,"ledger_index":len(led),"inventory":dict(inv),
          "item_count":len(inv),"note":"玩家人工確認的正式庫存基準；此前帳本只作歷史參考。"}
    save_inventory_baseline(base)
    led.append({"tx_id":base["baseline_id"],"time":now,"action":"建立庫存基準線",
                "route":"","point":"","input":"","input_delta":0,"output":"","output_delta":0,
                "reversed":False,"baseline":True,"baseline_item_count":len(inv)})
    save_inventory_ledger(led)
    return base

def build_inventory_reconciliation():
    inv=active_inventory();led=load_inventory_ledger();base=load_inventory_baseline();items={}
    # 正式基準線只屬於正式庫存；測試沙盒以自己帳本第一筆 before 作起點。
    baseline_mode=bool(load_inventory_mode()=="formal" and base and isinstance(base.get("inventory"),dict))
    start_idx=int(base.get("ledger_index") or 0) if baseline_mode else 0
    if baseline_mode:
        for iid,val in base["inventory"].items():
            try:exp=int(val) if val is not None else None
            except:exp=None
            items[str(iid)]={"item_id":str(iid),"name":str(iid),"rows":[],"expected":exp,"anchor":"baseline"}
    def touch(iid,name):
        if not iid:return None
        x=items.setdefault(str(iid),{"item_id":str(iid),"name":name or str(iid),"rows":[],"expected":None,"anchor":None})
        if name and (not x.get("name") or x["name"]==x["item_id"]):x["name"]=name
        return x
    for idx,row in enumerate(led):
        if idx<start_idx or not isinstance(row,dict) or row.get("baseline"):continue
        for side in ("input","output"):
            iid=row.get(side+"_id")
            if not iid:continue
            x=touch(iid,row.get(side))
            before=row.get(side+"_before")
            try:delta=int(row.get(side+"_delta") or 0)
            except:delta=0
            if x["expected"] is None and not baseline_mode and before is not None:
                try:x["expected"]=int(before);x["anchor"]=idx
                except:pass
            if x["expected"] is None:continue
            x["expected"]+=delta;x["rows"].append((idx,row,side,delta))
            if row.get("reversed"):x["expected"]-=delta
    out=[]
    for iid,x in items.items():
        cur=_inv_int(inv.get(iid));exp=x.get("expected")
        diff=None if cur is None or exp is None else cur-exp
        out.append(dict(x,current=cur,diff=diff,verifiable=(exp is not None),baseline_mode=baseline_mode))
    out.sort(key=lambda x:(0 if x.get("diff") not in (None,0) else 1,-abs(x.get("diff") or 0),x.get("name","")))
    return out


def migrate_v546_formal_inventory_once():
    """把使用者整理表匯出的正式庫存種子接到共用庫存；只做一次。"""
    if PLAYER_SETTINGS.get("formal_inventory_v546_migrated"):
        return
    try:
        seed_path=os.path.join(BASE,"inventory.json")
        with open(seed_path,"r",encoding="utf-8") as f:
            seed=json.load(f)
        if not isinstance(seed,dict):
            return
        current={}
        try:
            with open(INVENTORY_PATH,"r",encoding="utf-8") as f:
                x=json.load(f)
            if isinstance(x,dict): current=x
        except Exception:
            pass
        # 正式整理表有填數字者，以整理表為準；空白/未知則保留既有值（若有）。
        for iid,val in seed.items():
            if val is not None:
                current[iid]=val
            elif iid not in current:
                current[iid]=None
        save_inventory(current)
        PLAYER_SETTINGS["formal_inventory_v546_migrated"]=True
        save_player_settings(PLAYER_SETTINGS)
    except Exception:
        pass

migrate_v546_formal_inventory_once()

OCR=OCRPipeline(MASTER)
PARSER=ExchangeCardParser(MASTER)

FONT="Microsoft JhengHei UI"

# V5.70｜可愛綠色航線介面第一階段。集中色票，之後換頁時維持同一套氣氛。
CUTE_BG="#fbfaf4"
CUTE_GREEN="#146b3a"
CUTE_GREEN_SOFT="#eaf6e8"
CUTE_GREEN_LINE="#2e7d4f"
CUTE_BLUE_SOFT="#f1f7ff"
CUTE_YELLOW_SOFT="#fff7dc"
CUTE_BORDER="#c8dcc4"
CUTE_TEXT="#26352d"

def draw_round_rect(canvas,x1,y1,x2,y2,r=18,**kwargs):
    """Tk 原生控制項沒有圓角；用平滑多邊形畫可靠的圓角卡片。"""
    r=max(2,min(r,(x2-x1)/2,(y2-y1)/2))
    pts=[x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,x2,y2-r,x2,y2,
         x2-r,y2,x1+r,y2,x1,y2,x1,y2-r,x1,y1+r,x1,y1]
    return canvas.create_polygon(pts,smooth=True,splinesteps=24,**kwargs)

def make_round_panel(parent,bg="#fffef9",outline=CUTE_BORDER,radius=22,padding=14,min_height=80):
    """可放一般Tk元件的自適應圓角卡片；內容展開時高度會一起長大。"""
    shell=tk.Canvas(parent,bg=parent.cget("bg"),highlightthickness=0,bd=0,height=min_height)
    draw_round_rect(shell,2,2,518,min_height-2,radius,fill=bg,outline=outline,width=2,tags="roundbg")
    inner=tk.Frame(shell,bg=bg)
    win=shell.create_window((padding,padding),window=inner,anchor="nw")
    def layout(_=None):
        w=max(80,shell.winfo_width());h=max(min_height,inner.winfo_reqheight()+padding*2)
        shell.configure(height=h)
        shell.delete("roundbg")
        draw_round_rect(shell,2,2,w-2,h-2,radius,fill=bg,outline=outline,width=2,tags="roundbg")
        shell.tag_lower("roundbg")
        shell.coords(win,padding,padding)
        shell.itemconfigure(win,width=max(20,w-padding*2))
    inner.bind("<Configure>",layout,add="+")
    shell.bind("<Configure>",layout,add="+")
    return shell,inner


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("黑沙航海助手 V6.1")
        try:
            if os.path.exists(APP_ICON_PATH):self.iconbitmap(APP_ICON_PATH)
        except Exception:pass
        self.minsize(930,680)
        smart_window(self,"main","1080x780",980,700)
        self.state_data=STORE.load()
        try:ensure_version_backup("V6.1")
        except Exception:pass
        migrate_confirmed_duplicate_ids_v584()
        archive_legacy_context_rules_v585()
        archive_bad_output_quantity_memory_v5855()
        archive_icon_bound_quantity_memory_v5856()
        bootstrap_visual_memory_from_evidence_corrections()
        repair_legacy_reversed_t5_sales()
        if normalize_unstarted_route_quantities(self.state_data):
            STORE.save(self.state_data)
        # V5.64.6 相容修復：玩家若已在舊版按過刷新，第一次開新版時完成真正的批次重置。
        if self.state_data.get("status") == "🔄已刷新失效":
            invalidate_batch(self.state_data)
            clear_current_ratio_corrections()
            self.state_data["route_learning_skip_batch"]=False
            self.state_data.pop("route_learning_pending_suggestion",None)
            STORE.save(self.state_data)
        self.current_route=None
        self._route_window=None
        self._route_hover_tip=None
        self.bind("<FocusOut>",lambda e:self._close_route_hover(),add="+")
        self.bind("<Unmap>",lambda e:self._close_route_hover(),add="+")
        self.protocol("WM_DELETE_WINDOW",self.close_app)
        self.show_home()
        self.after(650,self._maybe_open_onboarding)

    def _maybe_open_onboarding(self):
        if should_auto_open_onboarding(PLAYER_SETTINGS,self.state_data):self.open_onboarding(auto=True)

    def close_app(self):
        now=datetime.datetime.now().timestamp();changed=False
        for route in self.state_data.get("routes",[]) or []:
            changed=route_timer_pause(route,now) or changed
        if changed:STORE.save(self.state_data)
        self.destroy()

    def _close_route_hover(self):
        tip=getattr(self,"_route_hover_tip",None)
        if tip is not None:
            try:tip.destroy()
            except Exception:pass
        self._route_hover_tip=None

    def clear(self):
        self._close_route_hover()
        try:self.unbind_all("<MouseWheel>")
        except Exception:pass
        _after=getattr(self,"_route_timer_after",None)
        if _after is not None:
            try:self.after_cancel(_after)
            except Exception:pass
        self._route_timer_after=None
        for w in self.winfo_children():
            w.destroy()

    def _route_page_parent(self,title="🗺 路線卡"):
        """路線總覽、排路線、單線執行共用一個副視窗；主視窗永遠保留首頁。"""
        win=getattr(self,"_route_window",None)
        if win is None or not win.winfo_exists():
            win=tk.Toplevel(self)
            self._route_window=win
            # 明確建立首頁→路線工作台的 Windows 視窗層級。
            win.transient(self)
            smart_window(win,"route_workspace_window","1380x840",1080,700)
            win.minsize(980,680)
            win.protocol("WM_DELETE_WINDOW",self.close_route_window)
        win.title(title)
        self._pulse_window_front(win)
        return win

    def _pulse_window_front(self,win,delay=260):
        """短暫提高層級讓 Windows 排好順序，隨即取消，不做永久置頂。"""
        try:
            win.deiconify();win.lift();win.attributes("-topmost",True)
            win.update_idletasks();win.focus_force()
            def release():
                try:
                    if win.winfo_exists():
                        # 只撤銷置頂；不要在此搶焦點，否則可能蓋住剛打開的子對話框。
                        win.attributes("-topmost",False)
                except Exception:pass
            win.after(delay,release)
        except Exception:pass

    def _handoff_modal_grab(self,child):
        """把Tk操作權交給新子視窗；關閉後再還給原本的設定／管理視窗。"""
        previous=None
        try:
            previous=child.grab_current()
            if previous is not None and previous is not child:previous.grab_release()
        except Exception:previous=None
        try:child.grab_set();child.lift();child.focus_force()
        except Exception:pass
        closed={"done":False}
        def close_child():
            if closed["done"]:return
            closed["done"]=True
            try:child.grab_release()
            except Exception:pass
            try:child.destroy()
            except Exception:pass
            try:
                if previous is not None and previous.winfo_exists():
                    previous.grab_set();previous.lift();previous.focus_force()
            except Exception:pass
        child.protocol("WM_DELETE_WINDOW",close_child)
        return close_child

    def _route_dialog_parent(self):
        """路線相關確認框永遠跟著目前可見的路線副視窗，不要錯掛在首頁後方。"""
        win=getattr(self,"_route_window",None)
        try:
            if win is not None and win.winfo_exists():
                self._pulse_window_front(win)
                return win
        except Exception:pass
        return self

    def _clear_route_page(self,title):
        self._close_route_hover()
        try:self.unbind_all("<MouseWheel>")
        except Exception:pass
        _after=getattr(self,"_route_timer_after",None)
        if _after is not None:
            try:self.after_cancel(_after)
            except Exception:pass
        self._route_timer_after=None
        win=self._route_page_parent(title)
        for child in win.winfo_children():child.destroy()
        return win

    def close_route_window(self):
        """右上角 X 只關閉路線工作視窗，不退出整個助手。"""
        try:self.unbind_all("<MouseWheel>")
        except Exception:pass
        _after=getattr(self,"_route_timer_after",None)
        if _after is not None:
            try:self.after_cancel(_after)
            except Exception:pass
        self._route_timer_after=None
        win=getattr(self,"_route_window",None)
        self._route_window=None
        if win is not None:
            try:win.destroy()
            except Exception:pass
        self.deiconify();self.lift()
        try:self.focus_force()
        except Exception:pass

    def open_routes_window(self):
        self.show_routes()

    def bring_main_front(self):
        """頁面從 Toplevel 回主視窗時，避免 Windows 把主程式留在其他視窗下層。"""
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.update_idletasks()
            self.after(180, lambda:self.attributes("-topmost", False))
            self.after(200, self.focus_force)
        except Exception:
            pass


    def btn(self,parent,text,cmd,size=19,width=20):
        return tk.Button(parent,text=text,command=cmd,font=(FONT,size,"bold"),
                         padx=16,pady=16,width=width,wraplength=380)

    def show_home(self):
        self.clear()
        root=tk.Frame(self,bg=CUTE_BG);root.pack(fill="both",expand=True)
        cv=tk.Canvas(root,bg=CUTE_BG,highlightthickness=0)
        sb=tk.Scrollbar(root,orient="vertical",command=cv.yview);cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y");cv.pack(side="left",fill="both",expand=True)
        page=tk.Frame(cv,bg=CUTE_BG);wid=cv.create_window((0,0),window=page,anchor="nw")
        page.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        cv.bind_all("<MouseWheel>",lambda e:cv.yview_scroll(int(-1*(e.delta/120)*3),"units"))

        hero=tk.Frame(page,bg="#fffef9",padx=24,pady=16);hero.pack(fill="x",padx=24,pady=(18,8))
        add_mascot(hero,bg="#fffef9",size=150,opacity=0.92,padx=4,quip_key="home")
        tk.Label(hero,text="⚓ 黑沙航海助手",font=(FONT,31,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="今天也輕輕鬆鬆出航吧～  ⛵",font=(FONT,14,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,8))
        status=tk.Frame(hero,bg=CUTE_GREEN_SOFT,padx=12,pady=7);status.pack(fill="x")
        tk.Label(status,text=f"目前批次：{self.state_data.get('batch_id') or '尚未建立'}",
                 font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).pack(side="left")
        tk.Label(status,text=f"狀態：{self.state_data.get('status','尚未開始')}",
                 font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).pack(side="right")

        tk.Label(page,text="今天要做的事",font=(FONT,20,"bold"),bg=CUTE_BG,fg=CUTE_GREEN).pack(anchor="w",padx=30,pady=(10,3))
        daily=tk.Frame(page,bg=CUTE_BG);daily.pack(fill="x",padx=22,pady=3)
        daily.columnconfigure(0,weight=1);daily.columnconfigure(1,weight=1)
        def daily_card(row,col,title,desc,cmd,color):
            shell,inner=make_round_panel(daily,bg=color,outline=CUTE_BORDER,radius=25,padding=16,min_height=125)
            shell.grid(row=row,column=col,sticky="nsew",padx=8,pady=8)
            title_label=tk.Label(inner,text=title,font=(FONT,21,"bold"),bg=color,fg=CUTE_GREEN)
            title_label.pack(anchor="w",pady=(5,0))
            desc_label=tk.Label(inner,text=desc,font=(FONT,12),bg=color,fg="#607168",wraplength=430,justify="left")
            desc_label.pack(anchor="w",pady=(7,5))
            tk.Label(inner,text="點一下進入  →",font=(FONT,11,"bold"),bg=color,fg=CUTE_GREEN).pack(anchor="e",pady=(3,0))

            # 整張卡片（包括卡內文字）都能進入功能，不再需要另外按「開啟」。
            def bind_click(widget):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>",lambda _event:cmd())
                for child in widget.winfo_children():
                    bind_click(child)
            bind_click(shell)
            shell.configure(takefocus=True)
            shell.bind("<Return>",lambda _event:cmd())
            shell.bind("<space>",lambda _event:cmd())
        daily_card(0,0,"🧭 路線工作台","選今日交換、排路線並開始航行。",self.show_route_picker,CUTE_GREEN_SOFT)
        daily_card(0,1,"📷 匯入截圖","自動處理刷新、指定A/B版面並執行OCR。",self.import_images,CUTE_BLUE_SOFT)
        daily_card(1,0,"🗺 路線卡","直接回到已經排好的路線與航行進度。",self.open_routes_window,CUTE_YELLOW_SOFT)
        daily_card(1,1,"⚙ 玩家設定中心","管理船隻、負重、顯示方式與玩家自訂資料。",self.open_player_settings,"#f2effa")

        tools=tk.Frame(page,bg="#fffef9",padx=14,pady=12);tools.pack(fill="x",padx=24,pady=(14,20))
        tk.Label(tools,text="偶爾才會用到",font=(FONT,13,"bold"),bg="#fffef9",fg="#607168").pack(side="left")
        tk.Button(tools,text="🧰 進階／救援工具  →",font=(FONT,13,"bold"),bg="#edf2ec",fg=CUTE_TEXT,
                  relief="flat",command=self.open_advanced_tools,padx=16,pady=8).pack(side="right")
        tk.Button(tools,text="🌱 新手起航指南",font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN,
                  relief="flat",command=self.open_onboarding,padx=14,pady=8).pack(side="right",padx=6)
        tk.Label(page,text=f"資料庫：V5.85.7完整成熟後台｜118段位品｜91點位｜{len(MASTER.aliases)}筆名稱別名　　版本 V6.1",
                 font=(FONT,10),bg=CUTE_BG,fg="#8a948e").pack(pady=(0,18))

    def open_onboarding(self,auto=False):
        old=getattr(self,"_onboarding_window",None)
        try:
            if old is not None and old.winfo_exists():old.lift();old.focus_force();return
        except Exception:pass
        win=tk.Toplevel(self);self._onboarding_window=win;win.title("🌱 新手起航指南");win.transient(self)
        smart_window(win,"onboarding_guide","880x760",720,580);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=20,pady=14);hero.pack(fill="x",padx=18,pady=(16,7))
        add_mascot(hero,size=86,opacity=0.20,quip_key="home")
        tk.Label(hero,text="🌱 第一次出航，照這五步就好",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="不用一次把所有設定填完；縮寫、分類和第二艘船都可以以後再慢慢加。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(3,0))
        shell=tk.Frame(win,bg=CUTE_BG);shell.pack(fill="both",expand=True,padx=18,pady=7)
        cv=tk.Canvas(shell,bg=CUTE_BG,highlightthickness=0);sb=tk.Scrollbar(shell,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=sb.set);sb.pack(side="right",fill="y");cv.pack(side="left",fill="both",expand=True)
        page=tk.Frame(cv,bg=CUTE_BG);pageid=cv.create_window((0,0),window=page,anchor="nw")
        page.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(pageid,width=e.width))
        ship=active_ship_profile();has_ship=float(ship.get("capacity") or 0)>0
        has_batch=bool(str(self.state_data.get("batch_id") or "").strip() or os.path.isfile(CURRENT_BATCH_PATH))
        has_routes=bool(self.state_data.get("routes"))
        steps=[
            ("1","設定你的船",("已設定" if has_ship else "建議先做"),
             "輸入實際可用負重；船員與裝備會影響數字。超重只提醒，不會鎖路線。"),
            ("2","決定要不要用縮寫與分類","可略過",
             "不習慣縮寫就維持全名。X／Z／G／遠線等分類都是玩家自訂，不是官方規則。"),
            ("3","匯入這一刷的截圖",("已有本輪資料" if has_batch else "尚未匯入"),
             "遊戲這一刷有幾頁就匯入幾張；A、B版可在匯入頁指定。匯入新資料才代表開始新一刷。"),
            ("4","看完OCR人工確認","每一刷新資料時",
             "低信心、問號或新物品要看原圖確認。確認只建立正確身份，不會修改正式庫存。"),
            ("5","把今天要跑的交換排成路線",("已有路線" if has_routes else "等待前一步"),
             "先勾選今天想跑的交換，再把它們排成一條或多條路線。實際到站交換後按「完成」，這時助手才會更新庫存。"),
        ]
        for i,(num,title,state,desc) in enumerate(steps):
            bg=(CUTE_GREEN_SOFT if i%2==0 else CUTE_BLUE_SOFT)
            row=tk.Frame(page,bg=bg,padx=15,pady=11);row.pack(fill="x",pady=5)
            tk.Label(row,text=num,font=(FONT,20,"bold"),bg=bg,fg="white",width=2,
                     relief="flat").pack(side="left",padx=(0,12))
            text=tk.Frame(row,bg=bg);text.pack(side="left",fill="x",expand=True)
            tk.Label(text,text=title,font=(FONT,15,"bold"),bg=bg,fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(text,text=desc,font=(FONT,11),bg=bg,fg=CUTE_TEXT,wraplength=610,justify="left").pack(anchor="w",pady=(3,0))
            tk.Label(row,text=state,font=(FONT,10,"bold"),bg="#fffef9",fg="#607168",padx=8,pady=4).pack(side="right",padx=4)
        def close_guide(mark_seen=False,completed=False):
            if mark_seen:
                PLAYER_SETTINGS["onboarding_seen"]=True
                if completed:PLAYER_SETTINGS["onboarding_completed"]=True
                save_player_settings(PLAYER_SETTINGS)
            self._onboarding_window=None;win.destroy();self.bring_main_front()
        def go_settings():close_guide(True);self.open_player_settings()
        def go_import():close_guide(True);self.import_images()
        foot=tk.Frame(win,bg=CUTE_BG);foot.pack(fill="x",padx=18,pady=(4,14))
        tk.Button(foot,text="⚙ 先設定船隻",font=(FONT,11,"bold"),command=go_settings,padx=12,pady=7).pack(side="left")
        tk.Button(foot,text="📷 直接匯入截圖",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",command=go_import,padx=12,pady=7).pack(side="left",padx=5)
        tk.Button(foot,text="✓ 我知道怎麼用了",font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  command=lambda:close_guide(True,True),padx=16,pady=7).pack(side="right")
        tk.Button(foot,text=("下次再提醒" if auto else "關閉"),font=(FONT,10),
                  command=lambda:close_guide(False),padx=10,pady=7).pack(side="right",padx=5)
        win.protocol("WM_DELETE_WINDOW",lambda:close_guide(False))

    def open_advanced_tools(self):
        """把開發／救援期入口集中起來，首頁只留下玩家日常流程。"""
        win=tk.Toplevel(self);win.title("🧰 進階／救援工具");smart_window(win,"advanced_tools","760x620",700,560)
        win.transient(self);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=11);hero.pack(fill="x",padx=24,pady=(18,10))
        add_mascot(hero,size=76,opacity=0.15,quip_key="advanced")
        tk.Label(hero,text="🧰 進階／救援工具",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="正常每天操作不需要進來；OCR資料異常或舊資料補登時才使用。",
                 font=(FONT,12),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
        shell=tk.Frame(win,bg="#fffef9");shell.pack(fill="both",expand=True,padx=24,pady=(0,20))
        cv=tk.Canvas(shell,bg="#fffef9",highlightthickness=0);sb=tk.Scrollbar(shell,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=sb.set);sb.pack(side="right",fill="y");cv.pack(side="left",fill="both",expand=True)
        box=tk.Frame(cv,bg="#fffef9",padx=14,pady=14);boxid=cv.create_window((0,0),window=box,anchor="nw")
        box.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(boxid,width=e.width))
        specs=[
            ("🛟 資料安全與AI自助求救","檢查、備份／還原，或產生可交給自己ChatGPT的求救包。",self.open_data_safety_center),
            ("🧠 OCR辨識知識包","匯出／匯入已確認的通用OCR別名；不包含個人操作資料。",self.open_ocr_knowledge_center),
            ("📖 我改過什麼","用白話查看每個品項曾被 OCR 讀成什麼，以及是不是同一錯誤重複出現。",self.open_ocr_learning_history),
            ("🔎 本輪OCR人工確認","重新打開這一刷的確認頁；不重跑OCR、不刷新路線。",self.open_current_ocr_review),
            ("📦 本輪交換池","查看這一刷OCR整理出的完整交換資料。",self.show_exchange_pool),
            ("🪪 戶籍檢查","檢查本輪物品與點位是否都有穩定ID。",self.show_identity_census),
            ("🧩 OCR身份疑點","找出欄位文字與目前ID互相矛盾的資料；只報告，不自動改。",self.open_identity_suspects),
            ("🩺 版面自救健檢","用一張圖真正跑OCR；檢查切片雖清楚、文字卻大量失敗的UI差異。",self.open_layout_health_check),
            ("✂ 裁切／版型校正","同一個校正器處理A、B、C版文字欄與數字欄位置。",self.open_calibrator),
            ("🧾 舊OCR補登","掃描舊版本OCR資料，補入待確認物品身份。",self.backfill_legacy_ocr),
            ("🛠 舊OCR待修","逐筆處理舊OCR補登後留下的待修資料。",self.open_legacy_review_workbench),
        ]
        for i,(title,desc,cmd) in enumerate(specs):
            row=tk.Frame(box,bg=(CUTE_GREEN_SOFT if i%2==0 else CUTE_BLUE_SOFT),padx=12,pady=9)
            row.pack(fill="x",pady=4)
            text=tk.Frame(row,bg=row.cget("bg"));text.pack(side="left",fill="x",expand=True)
            tk.Label(text,text=title,font=(FONT,14,"bold"),bg=row.cget("bg"),fg=CUTE_TEXT).pack(anchor="w")
            tk.Label(text,text=desc,font=(FONT,10),bg=row.cget("bg"),fg="#607168").pack(anchor="w")
            def _open(command=cmd):
                win.destroy();command()
            tk.Button(row,text="開啟",font=(FONT,11,"bold"),bg="#fffef9",relief="flat",command=_open,padx=14,pady=6).pack(side="right")

    def _show_saved_file(self,path,title="已完成"):
        messagebox.showinfo(title,f"✓ 已建立：\n{path}",parent=self)
        try:
            if os.name=="nt":os.startfile(os.path.dirname(path))
        except Exception:pass

    def open_identity_suspects(self):
        report=build_identity_suspect_report()
        win=tk.Toplevel(self);win.title("🧩 OCR身份疑點");smart_window(win,"identity_suspects","900x680",760,560)
        win.transient(self);win.configure(bg=CUTE_BG)
        tk.Label(win,text="🧩 OCR身份疑點",font=(FONT,24,"bold"),bg=CUTE_BG,fg=CUTE_GREEN).pack(pady=(18,4))
        tk.Label(win,text=f"找到 {report['count']} 筆｜只列出來，不會自動合併、拆分或修改庫存。",
                 font=(FONT,12,"bold"),bg=CUTE_BG,fg="#607168").pack(pady=(0,10))
        box=tk.Text(win,font=(FONT,12),wrap="word",bg="#fffef9",relief="flat",padx=14,pady=12)
        box.pack(fill="both",expand=True,padx=20,pady=8)
        if not report["suspects"]:box.insert("end","✓ 本輪沒有發現『文字明確指向另一個ID』的矛盾。\n")
        for x in report["suspects"]:
            box.insert("end",f"第{x['row']}筆 {x['side']}｜{x['raw']}\n目前 {x['current_id']}，文字則指向 {x['text_resolves_to']}\n來源：{x.get('source_file','')} 第{x.get('row_index','')}列\n\n")
        box.configure(state="disabled")
        tk.Button(win,text="關閉",font=(FONT,12,"bold"),command=win.destroy,padx=18,pady=7).pack(pady=12)

    def open_ocr_learning_history(self):
        history=build_ocr_learning_history()
        win=tk.Toplevel(self);win.title("📖 我改過什麼");win.transient(self)
        smart_window(win,"ocr_learning_history","1040x760",820,600);win.configure(bg=CUTE_BG)
        tk.Label(win,text="📖 我改過什麼",font=(FONT,24,"bold"),bg=CUTE_BG,fg=CUTE_GREEN).pack(pady=(16,3))
        tk.Label(win,text=(f"已留下 {history['event_count']} 次人工確認｜{history['item_count']} 個品項｜"
                           f"{history['variant_count']} 種 OCR 讀法"),font=(FONT,11,"bold"),bg=CUTE_BG,fg="#607168").pack()
        tk.Label(win,text="上半部選品項，下半部會說明每一次讀錯；不同錯字會明確標成『不是重複修改』。",
                 font=(FONT,10),bg=CUTE_BG,fg="#607168").pack(pady=(2,8))
        body=tk.Frame(win,bg=CUTE_BG);body.pack(fill="both",expand=True,padx=18,pady=(0,14))
        body.grid_columnconfigure(0,weight=1);body.grid_rowconfigure(0,weight=3);body.grid_rowconfigure(1,weight=2)
        shell,tree=make_reliable_tree(body,("name","variants","count","assessment"),
            {"name":"正確品名","variants":"曾經讀錯的文字","count":"確認次數","assessment":"判讀"},
            {"name":220,"variants":430,"count":90,"assessment":210},height=13)
        shell.grid(row=0,column=0,sticky="nsew")
        details=tk.Text(body,font=(FONT,11),wrap="word",bg="#fffef9",fg=CUTE_TEXT,relief="flat",padx=14,pady=12)
        detail_sb=tk.Scrollbar(body,orient="vertical",command=details.yview);details.configure(yscrollcommand=detail_sb.set)
        details.grid(row=1,column=0,sticky="nsew",pady=(9,0));detail_sb.grid(row=1,column=1,sticky="ns",pady=(9,0))
        indexed={}
        for no,item in enumerate(history["items"]):
            assessment=("不同讀法，不是重複修改" if item["variant_count"]>1 else
                        ("同一讀法曾重複" if item["repeat_count"] else "確認過一次"))
            iid=tree.insert("","end",values=(item["name"],item["variant_text"],item["total"],assessment));indexed[iid]=item
        def show_detail(_event=None):
            selected=tree.selection();details.configure(state="normal");details.delete("1.0","end")
            if selected:details.insert("end",indexed[selected[0]]["explanation"])
            elif not history["items"]:details.insert("end","目前還沒有欄位級人工確認紀錄。")
            details.configure(state="disabled")
        tree.bind("<<TreeviewSelect>>",show_detail)
        if tree.get_children():tree.selection_set(tree.get_children()[0]);show_detail()
        else:show_detail()

    def open_ocr_knowledge_center(self):
        win=tk.Toplevel(self);win.title("🧠 OCR辨識知識包");win.transient(self)
        smart_window(win,"ocr_knowledge_center","820x650",700,520);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=18,pady=(16,7))
        add_mascot(hero,size=74,opacity=0.16,quip_key="ocr")
        tk.Label(hero,text="🧠 OCR辨識知識包",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="分享辨識經驗，不分享你的航海生活。匯入只增量合併，不覆蓋衝突答案。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
        info=tk.Text(win,font=(FONT,11),wrap="word",bg="#fffef9",fg=CUTE_TEXT,relief="flat",padx=15,pady=12,height=18)
        info.pack(fill="both",expand=True,padx=18,pady=7)
        def show_home_text(extra=""):
            pack=build_ocr_knowledge_pack();info.configure(state="normal");info.delete("1.0","end")
            info.insert("end",f"目前可安全分享：{len(pack['aliases'])} 筆通用OCR別名\n","head")
            info.insert("end",f"因問號、非正式ID或格式不合而排除：{pack['excluded']} 筆\n\n")
            info.insert("end","會包含：\n• OCR曾經看錯的文字\n• 對應的正式ITEM_ID／POINT_ID\n• 正式名稱（方便核對）\n\n")
            info.insert("end","不會包含：\n• 庫存、帳本、路線與海豹紀錄\n• 船隻、縮寫、分類與玩家設定\n• 原始截圖、裁切圖、檔名或電腦路徑\n• 問號、待確認身份與個人新建的臨時ID\n")
            if extra:info.insert("end","\n"+extra,"result")
            info.tag_configure("head",font=(FONT,15,"bold"),foreground=CUTE_GREEN)
            info.tag_configure("result",font=(FONT,11,"bold"),foreground="#7a4e00");info.configure(state="disabled")
        def do_export():
            pack=build_ocr_knowledge_pack();os.makedirs(KNOWLEDGE_PACK_DIR,exist_ok=True)
            default=f"航海助手_OCR辨識知識包_{datetime.date.today().isoformat()}.json"
            path=filedialog.asksaveasfilename(title="匯出OCR辨識知識包",parent=win,initialdir=KNOWLEDGE_PACK_DIR,
                initialfile=default,defaultextension=".json",filetypes=[("辨識知識包","*.json")])
            if not path:return
            with open(path,"w",encoding="utf-8") as f:json.dump(pack,f,ensure_ascii=False,indent=2)
            show_home_text(f"✓ 已匯出 {len(pack['aliases'])} 筆：\n{path}")
        def do_import():
            path=filedialog.askopenfilename(title="選擇OCR辨識知識包",parent=win,initialdir=KNOWLEDGE_PACK_DIR,
                                            filetypes=[("辨識知識包","*.json"),("所有檔案","*.*")])
            if not path:return
            try:
                with open(path,"r",encoding="utf-8") as f:data=json.load(f)
            except Exception as ex:messagebox.showerror("無法讀取",str(ex),parent=win);return
            check=inspect_ocr_knowledge_pack(data)
            if not check.get("ok"):messagebox.showerror("不能匯入",check.get("error") or "格式錯誤",parent=win);return
            msg=(f"知識包共 {check['total']} 筆：\n"
                 f"可新增 {len(check['valid'])} 筆｜已存在 {len(check['existing'])} 筆｜衝突 {len(check['conflicts'])} 筆｜不合規 {len(check['invalid'])} 筆\n\n"
                 "衝突與不合規項目會略過，絕不覆蓋目前答案。要匯入可新增項目嗎？")
            if not messagebox.askyesno("確認匯入",msg,parent=win):return
            result=import_ocr_knowledge_pack(data)
            if not result.get("ok"):
                messagebox.showerror("匯入未完成",result.get("error") or "\n".join(result.get("errors") or []),parent=win);return
            show_home_text(f"✓ 新增 {result['added']} 筆；已存在 {len(result['existing'])} 筆；衝突略過 {len(result['conflicts'])} 筆。\n匯入前安全備份：{result['backup_path']}")
        foot=tk.Frame(win,bg=CUTE_BG);foot.pack(fill="x",padx=18,pady=(3,14))
        tk.Button(foot,text="匯入知識包",font=(FONT,11,"bold"),command=do_import,padx=15,pady=7).pack(side="left")
        tk.Button(foot,text="匯出可分享知識包",font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  command=do_export,padx=17,pady=7).pack(side="right")
        show_home_text()

    def open_data_safety_center(self):
        win=tk.Toplevel(self);win.title("🛟 資料安全與問題回報");win.transient(self)
        smart_window(win,"data_safety_center","850x700",720,540);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=18,pady=(16,7))
        add_mascot(hero,size=72,opacity=0.15,quip_key="advanced")
        tk.Label(hero,text="🛟 資料安全與 AI 自助求救",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="備份拿來救資料；AI求救包交給玩家自己的ChatGPT。兩者都不會修改庫存。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
        status=tk.StringVar();body=tk.Frame(win,bg="#fffef9",padx=15,pady=12);body.pack(fill="both",expand=True,padx=18,pady=7)
        report=tk.Text(body,font=(FONT,11),wrap="word",bg="#fffef9",fg=CUTE_TEXT,relief="flat",padx=8,pady=8)
        sb=tk.Scrollbar(body,orient="vertical",command=report.yview);report.configure(yscrollcommand=sb.set)
        report.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        def refresh_health():
            h=shared_data_health();report.configure(state="normal");report.delete("1.0","end")
            report.insert("end",("✓ 共用資料格式正常\n" if h["ok"] else "⚠ 共用資料需要注意\n"),"head")
            report.insert("end",f"檢查 {h.get('json_count',0)} 個 JSON 檔案。\n\n")
            if h.get("issues"):
                report.insert("end","發現：\n"+"\n".join(f"• {x}" for x in h["issues"])+"\n\n","warn")
            report.insert("end","檔案摘要：\n")
            for x in h.get("files",[]):
                state="正常" if x.get("valid_json") else "損壞"
                count=(f"｜{x['records']}筆" if x.get("records") is not None else "")
                report.insert("end",f"• {x['name']}｜{state}{count}\n")
            report.tag_configure("head",font=(FONT,16,"bold"),foreground=CUTE_GREEN)
            report.tag_configure("warn",foreground="#b42318");report.configure(state="disabled")
            status.set("檢查完成；這個動作只讀，不會修檔。")
        def do_backup():
            try:r=create_shared_backup("玩家手動建立")
            except Exception as ex:messagebox.showerror("備份失敗",str(ex),parent=win);return
            status.set(f"已備份 {r['count']} 個共用資料檔。")
            messagebox.showinfo("安全備份",f"✓ 已備份 {r['count']} 個 JSON。\n\n{r['path']}",parent=win)
        def do_restore():
            path=filedialog.askopenfilename(title="選擇航海助手共用資料備份",parent=win,
                                            filetypes=[("ZIP備份","*.zip")],initialdir=SUPPORT_BACKUP_DIR)
            if not path:return
            check=inspect_shared_backup(path)
            if not check["ok"]:
                messagebox.showerror("不能還原","這不是有效的航海助手備份：\n"+"\n".join(check["issues"]),parent=win);return
            if not messagebox.askyesno("確認還原",f"將用備份中的 {len(check['files'])} 個檔案取代目前同名資料。\n\n還原前會先自動保存目前資料。要繼續嗎？",parent=win):return
            r=restore_shared_backup(path)
            if not r.get("ok"):messagebox.showerror("還原失敗","\n".join(r.get("issues") or ["未知錯誤"]),parent=win);return
            messagebox.showinfo("還原完成",f"✓ 已還原 {r['count']} 個檔案。\n請關閉並重新開啟助手。\n\n還原前的安全備份：\n{r['safety_backup']}",parent=win)
            refresh_health()
        def do_bundle():
            try:r=create_support_bundle("V6.1")
            except Exception as ex:messagebox.showerror("產生失敗",str(ex),parent=win);return
            messagebox.showinfo("AI自助求救包",f"✓ 已整理 {r['included']} 個必要資料檔。\n不含任何圖片。\n\n把ZIP上傳到玩家自己的ChatGPT，再照包內提問文字描述問題：\n{r['path']}",parent=win)
            try:
                if os.name=="nt":os.startfile(SUPPORT_REPORT_DIR)
            except Exception:pass
        foot=tk.Frame(win,bg=CUTE_BG);foot.pack(fill="x",padx=18,pady=(3,14))
        tk.Button(foot,text="重新檢查",font=(FONT,11,"bold"),command=refresh_health,padx=12,pady=7).pack(side="left")
        tk.Button(foot,text="建立安全備份",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",command=do_backup,padx=12,pady=7).pack(side="left",padx=5)
        tk.Button(foot,text="從備份還原",font=(FONT,11,"bold"),command=do_restore,padx=12,pady=7).pack(side="left")
        tk.Button(foot,text="🤖 產生 AI 自助求救包",font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",command=do_bundle,padx=16,pady=7).pack(side="right")
        tk.Label(win,textvariable=status,font=(FONT,10,"bold"),bg=CUTE_BG,fg="#607168").pack(anchor="w",padx=22,pady=(0,10))
        refresh_health()

    def open_current_ocr_review(self):
        """OCR視窗關掉後仍可從進階工具回來修正本輪，不重新辨識。"""
        if not os.path.exists(CURRENT_BATCH_PATH):
            messagebox.showinfo("本輪OCR人工確認","目前沒有本輪OCR資料；請先匯入截圖。",parent=self)
            return
        try:
            with open(CURRENT_BATCH_PATH,"r",encoding="utf-8") as f:rows=json.load(f)
            if not isinstance(rows,list):raise ValueError("本輪資料格式不是清單")
        except Exception as ex:
            messagebox.showerror("本輪OCR人工確認",f"讀取本輪資料失敗：\n{ex}",parent=self)
            return
        self._ocr_review_filter="全部"
        if hasattr(self,"_ocr_review_ui_state"):
            self._ocr_review_ui_state["scroll_fraction"]=0.0
        self.show_ocr_review(rows)

    def backfill_legacy_ocr(self):
        root=filedialog.askdirectory(title="選擇存放舊版航海助手的資料夾")
        if not root:return
        result=scan_legacy_ocr_folder(root)
        if not result["paths"]:
            messagebox.showinfo("舊OCR補登","這個資料夾下面找不到舊 OCR 結構資料。\n沒有做任何修改。",parent=self)
            return
        existing=sum(1 for n in result["named"] if _find_registry_item(n))
        new_names=[n for n in result["named"] if not _find_registry_item(n)]
        msg=(f"找到 {len(result['paths'])} 份舊 OCR 資料，共 {len(result['rows'])} 筆。\n\n"
             f"已經有身份：{existing} 種\n"
             f"可補建『OCR待確認 ID』：{len(new_names)} 種\n"
             f"仍是問號、需要看原圖：{result['unknown']} 欄\n"
             f"疑似段位品但主表無 ID：{len(result['stage_missing'])} 種\n\n"
             "這次只補物品戶籍，不改正式庫存、路線或舊帳本。\n要開始補登嗎？")
        if not messagebox.askyesno("舊OCR人口普查",msg,parent=self):return
        before={str(x.get("item_id") or "") for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
        _register_observed_nonstage_items(result["rows"])
        queue=update_legacy_review_queue(result["rows"])
        after={str(x.get("item_id") or "") for x in _load_item_registry().get("items",[]) if isinstance(x,dict)}
        details=[]
        if result["stage_missing"]:details.append("段位品待人工確認："+"、".join(result["stage_missing"][:12]))
        if result["unknown"]:details.append(f"另有 {result['unknown']} 個問號欄位，必須找到原圖後手動填名稱。")
        messagebox.showinfo("舊OCR補登完成",
            f"✓ 新增 {len(after-before)} 個待確認物品 ID。\n"
            f"待修工作台目前共有 {sum(1 for x in queue if x.get('status')=='待處理')} 筆待處理。\n"
            "現有正式資料、庫存與帳本都沒有被改動。\n\n"+"\n".join(details),parent=self)

    def open_legacy_review_workbench(self):
        queue=load_legacy_review_queue()
        if not queue:
            messagebox.showinfo("舊OCR待修","目前沒有待修清單。\n請先執行一次「舊OCR補登」。",parent=self);return
        w=tk.Toplevel(self);w.title("🛠 舊OCR待修工作台");smart_window(w,"legacy_ocr_review","1180x760",1050,680)
        tk.Label(w,text="🛠 舊OCR待修工作台",font=(FONT,22,"bold")).pack(pady=(12,2))
        tk.Label(w,text="只補物品身份；不會把舊資料放進目前路線，也不會修改庫存與帳本。",
                 font=(FONT,11),fg="#667085").pack(pady=(0,8))
        body=tk.Frame(w);body.pack(fill="both",expand=True,padx=14,pady=8)
        left=tk.Frame(body);left.pack(side="left",fill="both",expand=True)
        right=tk.LabelFrame(body,text="這一筆",font=(FONT,13,"bold"),padx=14,pady=12);right.pack(side="right",fill="both",padx=(12,0))
        lb=tk.Listbox(left,font=(FONT,12),width=68,exportselection=False);sb=tk.Scrollbar(left,command=lb.yview)
        lb.configure(yscrollcommand=sb.set);lb.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        info=tk.StringVar();tk.Label(right,textvariable=info,font=(FONT,13,"bold"),justify="left",anchor="nw",wraplength=400).pack(fill="x",pady=6)
        tk.Label(right,text="確認後的正式名稱",font=(FONT,12,"bold")).pack(anchor="w",pady=(12,2))
        name_var=tk.StringVar();entry=tk.Entry(right,textvariable=name_var,font=(FONT,17),width=30);entry.pack(fill="x",pady=(0,10))
        visible=[]
        def reload_list(select=0):
            nonlocal queue,visible
            queue=load_legacy_review_queue();visible=[x for x in queue if x.get("status")=="待處理"]
            lb.delete(0,"end")
            for x in visible:
                side="投入" if x.get("side")=="input" else "產出"
                lb.insert("end",f"{x.get('reason')}｜{x.get('point_name') or '未知點'}｜{side}：{x.get('observed_name')}")
            if visible:
                i=max(0,min(select,len(visible)-1));lb.selection_set(i);lb.activate(i);show_selected()
            else:
                info.set("✓ 所有舊資料都已處理完。") ;name_var.set("")
        def selected():
            s=lb.curselection();return (int(s[0]),visible[int(s[0])]) if s else (None,None)
        def show_selected(_=None):
            _,x=selected()
            if not x:return
            info.set(f"原因：{x.get('reason')}\n交換點：{x.get('point_name') or '？'}\n"
                     f"投入：{x.get('input_name') or '？'}\n產出：{x.get('output_name') or '？'}\n"
                     f"來源：{os.path.basename(x.get('source_file') or x.get('json_path') or '')}\n"
                     f"第 {x.get('row_index')} 筆｜目前ID：{x.get('item_id') or '尚無'}")
            observed=str(x.get("observed_name") or "");name_var.set("" if observed in ("?","？") else observed)
        def save_queue_row(x):
            allq=load_legacy_review_queue()
            for i,q in enumerate(allq):
                if q.get("key")==x.get("key"):allq[i]=x;break
            save_legacy_review_queue(allq)
        def confirm_name():
            idx,x=selected();name=name_var.get().strip()
            if x is None or not name:return
            repaired=copy.deepcopy(x.get("row") or {});before=copy.deepcopy(repaired)
            hit=MASTER.resolve_item(name)
            if hit.get("matched"):
                iid=hit["record"]["id"];formal=hit["record"]["name"]
            else:
                known=_find_registry_item(name)
                if not known and not messagebox.askyesno("建立新物品ID",
                    f"資料庫找不到「{name}」。\n確定已看過原圖，要建立新的穩定 ID 嗎？",parent=w):return
                rec,_=_confirm_new_item_identity(name,x.get("kind",""),x.get("stage",""),
                    x.get("source_file",""),x.get("side",""),x.get("item_id","") if not known else known.get("item_id",""))
                iid=rec["item_id"];formal=name
            side=str(x.get("side") or "output")
            repaired[f"{side}_item_id"]=iid;repaired[f"{side}_name"]=formal
            remember_manual_repairs(before,repaired)
            x["status"]="已確認";x["confirmed_name"]=formal;x["item_id"]=iid
            x["confirmed_at"]=datetime.datetime.now().isoformat(timespec="seconds");save_queue_row(x);reload_list(idx)
        def cannot_confirm():
            idx,x=selected()
            if x is None:return
            x["status"]="缺少證物";save_queue_row(x);reload_list(idx)
        def evidence():
            _,x=selected()
            if x is None:return
            rr=copy.deepcopy(x.get("row") or {});rr["_legacy_scan_root"]=x.get("scan_root","")
            self._show_original_evidence(rr,x.get("row_index") or "?",w)
        lb.bind("<<ListboxSelect>>",show_selected)
        tk.Button(right,text="🖼 查看原圖／切片",font=(FONT,12,"bold"),command=evidence,pady=7).pack(fill="x",pady=4)
        tk.Button(right,text="✓ 套用／建立ID",font=(FONT,14,"bold"),command=confirm_name,pady=8).pack(fill="x",pady=4)
        tk.Button(right,text="缺少證物，暫時跳過",font=(FONT,11),command=cannot_confirm,pady=6).pack(fill="x",pady=4)
        reload_list()


    def show_identity_census(self):
        """V5.48｜戶籍檢查直接讀本輪交換池真正的來源 CURRENT_BATCH_PATH。"""
        try:
            debug_path=CURRENT_BATCH_PATH
            if not os.path.exists(debug_path):
                local_old=os.path.join(BASE,"data","ocr_structured_debug.json")
                if os.path.exists(local_old):
                    debug_path=local_old
                else:
                    messagebox.showinfo(
                        "還沒有本輪資料",
                        "共用資料裡目前沒有本輪OCR結構資料。\n"
                        "先按「沿用上一版本輪資料」，不用重跑OCR。"
                    )
                    return

            with open(debug_path,"r",encoding="utf-8") as f:
                rows=json.load(f)
            if not isinstance(rows,list):
                messagebox.showerror("戶籍檢查","本輪OCR結構資料格式不是預期的清單。")
                return

            _show_identity_census(self,rows)

            # 補到的身份直接寫回真正來源，之後交換池/搜尋都沿用。
            try:
                with open(debug_path,"w",encoding="utf-8") as f:
                    json.dump(rows,f,ensure_ascii=False,indent=2)
            except Exception:
                pass
        except Exception:
            messagebox.showerror("戶籍檢查失敗",traceback.format_exc())

    def carry_forward_previous_batch(self):
        """V5.48｜測試期快速前進：直接選上一版資料夾，承接已跑好的本輪OCR資料。"""
        from tkinter import filedialog
        import shutil
        prev=filedialog.askdirectory(title="選擇『上一版』黑沙航海助手資料夾")
        if not prev:
            return
        candidates=[
            os.path.join(prev,"data","ocr_structured_debug.json"),
            os.path.join(prev,"data","current_exchange_pool.json"),
        ]
        src_debug=candidates[0]
        if not os.path.exists(src_debug):
            messagebox.showerror("沿用上一版本輪資料",
                "這個資料夾裡找不到已跑好的本輪OCR資料。\n\n請選你『真的跑過這批截圖』的版本資料夾，例如剛才驗收到全0的 V5.28。")
            return
        try:
            os.makedirs(os.path.join(BASE,"data"),exist_ok=True)
            shutil.copy2(src_debug,os.path.join(BASE,"data","ocr_structured_debug.json"))
            shutil.copy2(src_debug,CURRENT_BATCH_PATH)
            # 有交換池就一起搬；沒有也沒關係，V5.47.3.2會重新建立。
            if os.path.exists(candidates[1]):
                shutil.copy2(candidates[1],os.path.join(BASE,"data","current_exchange_pool.json"))
            # 同一版可能還有與這批資料直接相關的確認/修正檔；只複製存在者，不覆蓋V5.47程式碼。
            copied=["ocr_structured_debug.json"]
            for name in ("manual_repairs.json","manual_confirmations.json","ocr_manual_confirmations.json",
                         "learned_aliases.json","ratio_confirmations.json"):
                a=os.path.join(prev,"data",name); b=os.path.join(BASE,"data",name)
                if os.path.exists(a):
                    shutil.copy2(a,b); copied.append(name)
            messagebox.showinfo("沿用完成",
                "✓ 目前批次已存進「黑沙航海助手_共用資料」。\n\n以後刪舊版本也不會丟；新版會自動讀取。\n現在直接點「📦 本輪交換池」即可。")
            self.show_home()
        except Exception as ex:
            messagebox.showerror("沿用失敗",f"搬資料時發生錯誤：\n{ex}")

    def show_exchange_pool(self):
        """V5.48｜本輪交換池查帳＋搜尋。"""
        debug_path=CURRENT_BATCH_PATH
        if not os.path.exists(debug_path):
            local_old=os.path.join(BASE,"data","ocr_structured_debug.json")
            if os.path.exists(local_old):
                debug_path=local_old
            else:
                messagebox.showinfo("本輪交換池","共用資料裡目前還沒有本輪資料。\n第一次請用「沿用上一版本輪資料」匯入；之後新版會自動接手。")
                return
        try:
            with open(debug_path,"r",encoding="utf-8") as f:
                rows=json.load(f)
            if not isinstance(rows,list): rows=[]
        except Exception as ex:
            messagebox.showerror("本輪交換池",f"讀取失敗：\n{ex}")
            return

        recover_batch_remaining_times(rows)
        apply_confirmed_raw_identity_learning(rows)
        apply_confirmed_blank_context_learning(rows)
        apply_conservative_registry_typo_learning(rows)
        apply_visual_item_learning(rows)
        _register_observed_nonstage_items(rows)
        apply_learned_aliases_to_rows(rows)
        _promote_pending_aliases(rows)
        apply_manual_repairs(rows)
        reconcile_row_identities_independently(rows)
        apply_evidence_identity_corrections(rows)
        apply_visual_quantity_learning(rows)
        apply_ratio_confirmations(rows)
        apply_confirmed_material_input_quantity(rows)
        recover_structured_special_quantity_source(rows)
        recover_remaining_times_from_batch_twins(rows)
        for _rr in rows:normalize_dynamic_ratio_status(_rr)
        apply_current_ratio_corrections(rows)
        apply_learned_core_confirmations(rows)
        # 查看本輪交換池時，也把已套用的人工記憶同步寫回本輪資料。
        save_current_batch_rows(rows)
        for _r in rows:
            normalize_point_identity(_r)

        usable=[]; blocked=[]
        for r in rows:
            item={k:r.get(k,"") for k in (
                "point_id","point_name","input_item_id","input_name","input_stage","input_kind","input_quantity",
                "output_item_id","output_name","output_stage","output_quantity","output_kind",
                "exchange_type","remaining_times","exchange_ratio","exchange_ratio_input","exchange_ratio_output",
                "status","issues","source_file","row_index"
            )}
            (usable if r.get("status")=="候選可用" else blocked).append(item)

        pool_path=CURRENT_POOL_PATH
        with open(pool_path,"w",encoding="utf-8") as f:
            json.dump({"usable":usable,"blocked":blocked},f,ensure_ascii=False,indent=2)

        # V5.47：共用縮寫/別名引擎。既有筆記縮寫作為種子，只影響搜尋，不改核心資料。
        def _load_search_aliases():
            seed_path=os.path.join(BASE,"search_alias_seed.json")
            if not os.path.exists(SEARCH_ALIAS_PATH) and os.path.exists(seed_path):
                try: shutil.copy2(seed_path,SEARCH_ALIAS_PATH)
                except Exception: pass
            try:
                with open(SEARCH_ALIAS_PATH,"r",encoding="utf-8") as f:
                    x=json.load(f)
                return x if isinstance(x,dict) else {"items":{},"points":{}}
            except Exception:
                return {"items":{},"points":{}}
        search_aliases=_load_search_aliases()
        try:
            with open(os.path.join(BASE,"search_master_data.json"),"r",encoding="utf-8") as f:
                search_master=json.load(f)
        except Exception:
            search_master={"items":[],"points":[]}

        win=tk.Toplevel(self); win.title("本輪交換池｜V5.47")
        smart_window(win,"exchange_pool","1320x780",1080,680)
        tk.Label(win,text="📦 本輪交換池",font=(FONT,24,"bold")).pack(pady=(14,4))
        tk.Label(win,text=f"可進路線引擎 {len(usable)} 筆 ｜ 暫不進入 {len(blocked)} 筆",
                 font=(FONT,15,"bold")).pack(pady=(0,8))
        tk.Label(win,text="查帳／抽查用：畫面只留人要看的；階級與ID等結構資料仍完整保留在地下室供路線引擎使用。",
                 font=(FONT,12)).pack(pady=(0,8))

        diag=tk.Label(win,text="交換池介面初始化中…",font=(FONT,11,"bold"))
        diag.pack(pady=3)

        top=tk.Frame(win); top.pack(pady=4)

        search_bar=tk.Frame(win)
        search_bar.pack(fill="x",padx=18,pady=(2,6))
        tk.Label(search_bar,text="🔎 搜尋：",font=(FONT,12,"bold")).pack(side="left")
        search_var=tk.StringVar()
        search_entry=tk.Entry(search_bar,textvariable=search_var,font=(FONT,13),width=32)
        search_entry.pack(side="left",padx=(4,8))
        tk.Label(search_bar,text="正式名 / 名稱片段 / 個人縮寫",font=(FONT,10)).pack(side="left",padx=(0,8))

        # V5.47：交換池顯示層改成最單純的 Text + Scrollbar。
        # 不再使用 Canvas / Frame / create_window / 卡片子視窗。
        # 這是刻意的：先把「能穩定看到89筆」當第一優先，避免Tk版面再搞鬼。
        text_area=tk.Frame(win)
        text_area.pack(fill="both",expand=True,padx=14,pady=8)
        txt=tk.Text(text_area,wrap="word",font=(FONT,13),state="disabled",padx=12,pady=10)
        sb=tk.Scrollbar(text_area,orient="vertical",command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left",fill="both",expand=True)
        sb.pack(side="right",fill="y")
        txt.configure(state="normal")
        txt.insert("end","交換池畫面已建立，正在載入資料…\n")
        txt.configure(state="disabled")

        # 大字標題/品名
        txt.tag_configure("point",font=(FONT,14,"bold"),spacing1=8)
        txt.tag_configure("trade",font=(FONT,16,"bold"),spacing1=2,spacing3=2)
        txt.tag_configure("meta",font=(FONT,11),spacing3=7)
        txt.tag_configure("issue",font=(FONT,11,"bold"))

        def stage_of(r):
            # V5.47：分類器必須「永遠立即返回」。
            # 任何怪資料都只回特殊，不允許拖住整個交換池。
            try:
                if not isinstance(r,dict):
                    return "特殊"
                ex=r.get("exchange_type")
                out_kind=r.get("output_kind")
                ex="" if ex is None else str(ex)
                out_kind="" if out_kind is None else str(out_kind)
                if "特殊" in ex or "大洋" in ex or "特殊" in out_kind or "大洋" in out_kind:
                    return "特殊"

                for v in (r.get("input_stage"), r.get("output_stage")):
                    try:
                        st=int(v or 0)
                        if 1<=st<=7:
                            return f"{st}階"
                    except Exception:
                        pass

                # exchange_type只做最簡單掃描，不用regex。
                for d in "1234567":
                    if f"{d}段" in ex or f"{d}階" in ex or f"{d}階段" in ex:
                        return f"{d}階"
                return "特殊"
            except Exception:
                return "特殊"

        # 先建立分頁按鈕。V5.31.7 卡在「算筆數」以前，所以這版不准先算完才畫UI。
        stage_names=["全部","1階","2階","3階","4階","5階","6階","7階","特殊"]
        counts={x:0 for x in stage_names}
        counts["全部"]=len(usable)

        stage_buttons={}
        current_stage={"name":"全部"}

        def update_button_texts():
            for name,b in stage_buttons.items():
                try:
                    b.configure(text=f"{name} {counts.get(name,0)}")
                except Exception:
                    pass

        for name in stage_names:
            b=tk.Button(top,text=f"{name} …",font=(FONT,12),
                        padx=8,pady=5)
            b.pack(side="left",padx=2)
            stage_buttons[name]=b

        status=tk.Label(win,text="分頁已建立，準備顯示全部89筆…",font=(FONT,10))
        status.pack(side="bottom",pady=2)

        def render(data):
            txt.configure(state="normal")
            txt.delete("1.0","end")
            txt.configure(state="disabled")
            status.configure(text=f"開始顯示：{len(data)} 筆")

            if not data:
                txt.configure(state="normal")
                txt.insert("end","這個分頁目前 0 筆。\n","point")
                txt.configure(state="disabled")
                return

            idx_state={"i":0}

            def write_chunk():
                try:
                    st=idx_state["i"]
                    ed=min(st+10,len(data))
                    diag.configure(text=f"正在顯示第 {st+1}～{ed} 筆")
                    txt.configure(state="normal")

                    for idx in range(st,ed):
                        r=data[idx] if isinstance(data[idx],dict) else {}
                        txt.insert("end",f"{idx+1}. {r.get('point_name') or '未知點位'}\n","point")
                        txt.insert("end",f"{r.get('input_name') or '？'}  →  {r.get('output_name') or '？'}\n","trade")

                        meta=f"剩餘：{r.get('remaining_times','')}"
                        try:
                            ins=int(r.get("input_stage") or 0)
                            outs=int(r.get("output_stage") or 0)
                        except Exception:
                            ins=outs=0
                        if (ins,outs) in ((1,2),(2,3)):
                            meta+=f" ｜ 比例：{r.get('exchange_ratio','')}"
                        txt.insert("end",meta+"\n\n","meta")

                    txt.configure(state="disabled")
                    idx_state["i"]=ed
                    status.configure(text=f"已寫入 {ed}/{len(data)} 筆")

                    if ed<len(data):
                        win.after(1,write_chunk)
                    else:
                        txt.yview_moveto(0)
                        diag.configure(text=f"✓ 已顯示 {len(data)} 筆")
                except Exception:
                    try: txt.configure(state="disabled")
                    except Exception: pass
                    diag.configure(text="❌ 顯示資料時失敗")
                    status.configure(text=f"卡在第 {idx_state['i']+1} 筆附近")
                    messagebox.showerror("交換池顯示失敗",traceback.format_exc())

            win.after(1,write_chunk)

        def select_stage(name):
            current_stage["name"]=name
            try:
                data=usable if name=="全部" else [r for r in usable if stage_of(r)==name]

                q_raw=search_var.get().strip()
                q=q_raw.lower()
                if q:
                    # 1) 正式文字片段直接搜
                    # 2) 既有玩家縮寫/別名展開成正式名稱再搜
                    # 3) 同一縮寫可對應多個點，全部列出供玩家選
                    expanded=[q_raw]

                    # 點位分區代號不是永久別名；每次搜尋時臨時從原始點位主表展開。
                    # 例如 E -> 原始分區E的所有正式點位，再與本輪交換池交集。
                    group_targets=[]
                    if len(q_raw)<=3:
                        for pt in search_master.get("points",[]):
                            if str(pt.get("group") or "").strip().lower()==q:
                                nm=str(pt.get("name") or "").strip()
                                if nm: group_targets.append(nm)
                    expanded.extend(group_targets)

                    for bucket_name in ("items","points"):
                        bucket=search_aliases.get(bucket_name,{})
                        if isinstance(bucket,dict):
                            for alias,targets in bucket.items():
                                if str(alias).lower()==q:
                                    if not isinstance(targets,list): targets=[targets]
                                    expanded.extend(str(x) for x in targets if x)
                    expanded_l=[x.lower() for x in expanded if str(x).strip()]

                    def _hit(r):
                        hay=" | ".join(str(r.get(k) or "") for k in (
                            "point_name","input_name","output_name","exchange_type"
                        )).lower()
                        return any(term in hay for term in expanded_l)
                    data=[r for r in data if _hit(r)]

                for n,b in stage_buttons.items():
                    b.configure(relief="sunken" if n==name else "raised",
                                bd=4 if n==name else 2,
                                font=(FONT,12,"bold" if n==name else "normal"))
                render(data)
                if q:
                    shown=[x for x in expanded[1:] if x]
                    if shown:
                        preview="、".join(shown[:4])+("…" if len(shown)>4 else "")
                        status.configure(text=f"搜尋「{q_raw}」→ {preview}：{len(data)} 筆")
                    else:
                        status.configure(text=f"搜尋「{q_raw}」：{len(data)} 筆")
            except Exception:
                messagebox.showerror("交換池分頁失敗",traceback.format_exc())

        # 現在才把按鈕綁上事件。
        for name,b in stage_buttons.items():
            b.configure(command=lambda n=name:select_stage(n))

        def do_search(event=None):
            # 搜某物時跨全部交換池找，玩家不必先知道它是哪一階。
            select_stage("全部")
            return "break"

        def clear_search():
            search_var.set("")
            select_stage("全部")
            search_entry.focus_set()

        tk.Button(search_bar,text="搜尋",font=(FONT,11,"bold"),
                  command=do_search,padx=10,pady=3).pack(side="left",padx=3)
        tk.Button(search_bar,text="清除",font=(FONT,11),
                  command=clear_search,padx=10,pady=3).pack(side="left",padx=3)

        def save_aliases():
            try:
                os.makedirs(os.path.dirname(SEARCH_ALIAS_PATH),exist_ok=True)
                with open(SEARCH_ALIAS_PATH,"w",encoding="utf-8") as f:
                    json.dump(search_aliases,f,ensure_ascii=False,indent=2)
                return True
            except Exception:
                messagebox.showerror("縮寫儲存失敗",traceback.format_exc())
                return False

        def _open_alias_manager_impl():
            aw=tk.Toplevel(win)
            aw.title("新增搜尋縮寫｜V5.47")
            smart_window(aw,"alias_manager","900x650",780,560)
            aw.minsize(760,540)

            tk.Label(aw,text="新增搜尋縮寫",font=(FONT,20,"bold")).pack(pady=(12,3))
            tk.Label(
                aw,
                text="只看這一輪畫面真的出現過的東西。你選它、取縮寫；身份與名稱歷史交給地下室。",
                font=(FONT,11)
            ).pack(pady=(0,8))

            topf=tk.Frame(aw); topf.pack(fill="x",padx=16,pady=4)
            kind_var=tk.StringVar(value="物品")
            tk.Label(topf,text="看：",font=(FONT,11,"bold")).pack(side="left")
            kind_box=tk.OptionMenu(topf,kind_var,"物品","交換點")
            kind_box.configure(font=(FONT,11),width=8)
            kind_box.pack(side="left",padx=(0,14))

            filter_var=tk.StringVar()
            tk.Label(topf,text="找一下：",font=(FONT,11,"bold")).pack(side="left")
            tk.Entry(topf,textvariable=filter_var,font=(FONT,11),width=30).pack(side="left",padx=(0,8))
            count_lab=tk.Label(topf,text="",font=(FONT,10))
            count_lab.pack(side="left")

            middle=tk.Frame(aw); middle.pack(fill="both",expand=True,padx=16,pady=6)

            left=tk.LabelFrame(middle,text="本輪看到的",font=(FONT,11,"bold"),padx=6,pady=6)
            left.pack(side="left",fill="both",expand=True,padx=(0,8))
            pick=tk.Listbox(left,font=(FONT,11),exportselection=False)
            psb=tk.Scrollbar(left,orient="vertical",command=pick.yview)
            pick.configure(yscrollcommand=psb.set)
            pick.pack(side="left",fill="both",expand=True)
            psb.pack(side="right",fill="y")

            right=tk.LabelFrame(middle,text="已保存縮寫",font=(FONT,11,"bold"),padx=6,pady=6)
            right.pack(side="left",fill="both",expand=True,padx=(8,0))
            saved=tk.Listbox(right,font=(FONT,11),exportselection=False)
            ssb=tk.Scrollbar(right,orient="vertical",command=saved.yview)
            saved.configure(yscrollcommand=ssb.set)
            saved.pack(side="left",fill="both",expand=True)
            ssb.pack(side="right",fill="y")

            bottom=tk.Frame(aw); bottom.pack(fill="x",padx=16,pady=(4,12))
            target_var=tk.StringVar()
            alias_var=tk.StringVar()
            identity_var=tk.StringVar(value="")

            tk.Label(bottom,text="你選的是：",font=(FONT,11,"bold")).grid(row=0,column=0,sticky="w",pady=4)
            tk.Entry(bottom,textvariable=target_var,font=(FONT,11),state="readonly").grid(
                row=0,column=1,columnspan=3,sticky="ew",pady=4
            )
            tk.Label(bottom,textvariable=identity_var,font=(FONT,10,"bold")).grid(
                row=1,column=1,columnspan=3,sticky="w",pady=(0,5)
            )
            tk.Label(bottom,text="我平常叫它：",font=(FONT,11,"bold")).grid(row=2,column=0,sticky="w",pady=4)
            alias_entry=tk.Entry(bottom,textvariable=alias_var,font=(FONT,12),width=28)
            alias_entry.grid(row=2,column=1,sticky="w",pady=4)
            bottom.columnconfigure(1,weight=1)

            shown=[]
            saved_rows=[]
            current_choice={"name":"","id":"","raw":""}

            def current_batch_records():
                # 前台只列「本輪真的看到的」。同名合併，但保留是否已有身份ID。
                found={}
                if kind_var.get()=="物品":
                    for r in usable:
                        if not isinstance(r,dict): continue
                        for namekey,idkey,rawkey in (
                            ("input_name","input_item_id","input_raw"),
                            ("output_name","output_item_id","output_raw")
                        ):
                            nm=str(r.get(namekey) or r.get(rawkey) or "").strip()
                            if not nm or nm in ("?","？"): continue
                            iid=str(r.get(idkey) or "").strip()
                            raw=str(r.get(rawkey) or nm).strip()
                            old=found.get(nm)
                            if old is None or (iid and not old.get("id")):
                                found[nm]={"name":nm,"id":iid,"raw":raw}
                else:
                    for r in usable:
                        if not isinstance(r,dict): continue
                        nm=str(r.get("point_name") or "").strip()
                        if not nm or nm in ("?","？"): continue
                        iid=str(r.get("point_id") or "").strip()
                        found[nm]={"name":nm,"id":iid,"raw":nm}
                return sorted(found.values(),key=lambda x:x["name"])

            def refresh_pick(*_):
                shown.clear(); pick.delete(0,"end")
                q=filter_var.get().strip().lower()
                vals=[x for x in current_batch_records() if not q or q in x["name"].lower()]
                shown.extend(vals)
                for x in vals:
                    mark="✓" if x.get("id") else "⚠"
                    pick.insert("end",f"{mark} {x['name']}")
                count_lab.configure(text=f"{len(vals)} 筆｜✓正式歸戶　⚠可先記縮寫")

            def refresh_saved():
                saved_rows.clear(); saved.delete(0,"end")
                for bucket_name,label in (("items","物品"),("points","交換點")):
                    bucket=search_aliases.get(bucket_name,{})
                    if not isinstance(bucket,dict): continue
                    for aa,targets in sorted(bucket.items(),key=lambda x:str(x[0]).lower()):
                        if not isinstance(targets,list): targets=[targets]
                        for tt in targets:
                            saved_rows.append((bucket_name,str(aa),str(tt)))
                            saved.insert("end",f"[{label}] {aa} → {tt}")

                # 待確認縮寫也顯示，但不假裝它已經是正式歸戶資料。
                try:
                    with open(PENDING_ALIAS_PATH,"r",encoding="utf-8") as f:
                        pending=json.load(f)
                    if isinstance(pending,list):
                        for x in pending:
                            if not isinstance(x,dict): continue
                            label="物品" if x.get("kind")=="item" else "交換點"
                            saved.insert("end",f"[待確認·{label}] {x.get('alias','')} → {x.get('seen_name','')}")
                except Exception:
                    pass

            def choose_pick(event=None):
                sel=pick.curselection()
                if not sel: return
                x=shown[sel[0]]
                current_choice.clear(); current_choice.update(x)
                target_var.set(x["name"])
                if x.get("id"):
                    identity_var.set("✓ 這個地下室已經認得，可以直接幫它取縮寫。")
                    alias_entry.configure(state="normal")
                    alias_entry.focus_set()
                else:
                    identity_var.set("⚠ 身份還沒確認，但可以先記你怎麼叫它；地下室暫存，之後再掛到正確ID。")
                    alias_entry.configure(state="normal")
                    alias_entry.focus_set()

            def load_saved(event=None):
                sel=saved.curselection()
                if not sel:return
                bucket,aa,tt=saved_rows[sel[0]]
                kind_var.set("物品" if bucket=="items" else "交換點")
                filter_var.set(tt)
                refresh_pick()
                target_var.set(tt)
                alias_var.set(aa)

            def add_alias():
                tt=target_var.get().strip()
                aa=alias_var.get().strip()
                if not tt:
                    messagebox.showwarning("還沒選","先在左邊點你肉眼看到的那個東西。"); return
                if not aa:
                    messagebox.showwarning("還沒輸入縮寫","輸入你平常腦子裡會叫它的名字就好。"); return

                if not current_choice.get("id"):
                    # V5.47：未確認身份也允許先記縮寫，但只進待確認區，不碰正式ID資料。
                    try:
                        try:
                            with open(PENDING_ALIAS_PATH,"r",encoding="utf-8") as f:
                                pending=json.load(f)
                            if not isinstance(pending,list): pending=[]
                        except Exception:
                            pending=[]

                        rec={
                            "kind":"item" if kind_var.get()=="物品" else "point",
                            "seen_name":tt,
                            "raw_text":str(current_choice.get("raw") or tt),
                            "alias":aa
                        }
                        if not any(
                            isinstance(x,dict)
                            and x.get("kind")==rec["kind"]
                            and x.get("seen_name")==rec["seen_name"]
                            and x.get("alias")==rec["alias"]
                            for x in pending
                        ):
                            pending.append(rec)

                        os.makedirs(os.path.dirname(PENDING_ALIAS_PATH),exist_ok=True)
                        with open(PENDING_ALIAS_PATH,"w",encoding="utf-8") as f:
                            json.dump(pending,f,ensure_ascii=False,indent=2)

                        alias_var.set("")
                        identity_var.set("⏳ 縮寫已先記住；等身份確認後再自動歸戶。")
                        messagebox.showinfo("先記住了",f"「{tt}」你叫它「{aa}」。\n目前先放待確認區，不會亂建ID。")
                    except Exception:
                        messagebox.showerror("待確認縮寫儲存失敗",traceback.format_exc())
                    return

                bucket_name="items" if kind_var.get()=="物品" else "points"
                bucket=search_aliases.setdefault(bucket_name,{})
                targets=bucket.get(aa,[])
                if not isinstance(targets,list): targets=[targets]
                if tt not in targets: targets.append(tt)
                bucket[aa]=targets
                if save_aliases():
                    refresh_saved()
                    alias_var.set("")
                    messagebox.showinfo("好了",f"以後搜尋「{aa}」就會找到「{tt}」。")

            def delete_saved():
                sel=saved.curselection()
                if not sel:
                    messagebox.showwarning("未選取","先在右邊點一筆要刪的縮寫。"); return
                bucket_name,aa,tt=saved_rows[sel[0]]
                bucket=search_aliases.get(bucket_name,{})
                targets=bucket.get(aa,[])
                if not isinstance(targets,list): targets=[targets]
                targets=[x for x in targets if str(x)!=tt]
                if targets: bucket[aa]=targets
                else: bucket.pop(aa,None)
                if save_aliases(): refresh_saved()

            btnrow=tk.Frame(bottom); btnrow.grid(row=3,column=0,columnspan=4,sticky="ew",pady=(8,0))
            tk.Button(btnrow,text="儲存縮寫",font=(FONT,11,"bold"),command=add_alias,padx=14,pady=5).pack(side="left")
            tk.Button(btnrow,text="刪除右側選取",font=(FONT,11),command=delete_saved,padx=12,pady=5).pack(side="left",padx=6)
            tk.Button(btnrow,text="關閉",font=(FONT,11),command=aw.destroy,padx=14,pady=5).pack(side="right")

            pick.bind("<<ListboxSelect>>",choose_pick)
            saved.bind("<Double-Button-1>",load_saved)

            def on_kind_change(*_):
                filter_var.set("")
                target_var.set("")
                alias_var.set("")
                identity_var.set("")
                current_choice.clear()
                try: alias_entry.configure(state="normal")
                except: pass
                refresh_pick()

            kind_var.trace_add("write",on_kind_change)
            filter_var.trace_add("write",refresh_pick)

            refresh_pick()
            refresh_saved()

        def open_alias_manager():
            try:
                _open_alias_manager_impl()
            except Exception:
                messagebox.showerror("縮寫管理開啟失敗",traceback.format_exc())


        tk.Button(search_bar,text="＋縮寫",font=(FONT,11,"bold"),
                  command=open_alias_manager,padx=10,pady=3).pack(side="left",padx=3)
        search_entry.bind("<Return>",do_search)

        # 第一件事：完全不分類，直接顯示全部。
        diag.configure(text="分頁已建立，先直接顯示全部資料…")
        select_stage("全部")

        # 第二件事：背景分批算各階筆數，不得阻塞畫面。
        count_state={"i":0}
        def count_chunk():
            try:
                st=count_state["i"]
                ed=min(st+10,len(usable))
                for i in range(st,ed):
                    k=stage_of(usable[i])
                    if k not in counts:
                        k="特殊"
                    counts[k]+=1
                count_state["i"]=ed
                update_button_texts()
                if ed<len(usable):
                    win.after(1,count_chunk)
                else:
                    diag.configure(text=f"✓ 已顯示全部資料；階級分頁統計完成")
            except Exception:
                diag.configure(text="⚠ 階級統計失敗，但全部資料仍可看")
                update_button_texts()
        win.after(1,count_chunk)

    def refresh_confirm(self):
        if messagebox.askyesno(
            "確認刷新",
            "舊交換會失效，上一刷的路線、未規劃站點與今日勾選會封存後清空。\n\n"
            "已完成交換的庫存與帳本不會撤銷。\n"
            "確定已在遊戲中刷新？"
        ):
            invalidate_batch(self.state_data)
            clear_current_ratio_corrections()
            self.state_data["route_learning_skip_batch"]=False
            self.state_data.pop("route_learning_pending_suggestion",None)
            STORE.save(self.state_data)
            self.current_route=None
            self.show_home()

    def import_images(self):
        paths=filedialog.askopenfilenames(
            title="匯入截圖｜選擇這一刷的交換圖片",
            filetypes=[("圖片","*.png *.jpg *.jpeg *.webp"),("所有檔案","*.*")]
        )
        if not paths:return
        paths=list(paths)
        OCR.validate_images(paths)
        # 首頁不再要求先單獨按「我已刷新」。匯入新截圖時一起完成批次封存。
        should_refresh=False
        if self.state_data.get("status")!="等待匯入新一刷":
            _new_batch=messagebox.askyesnocancel(
                "匯入截圖",
                "這是遊戲刷新後的『新一刷』嗎？\n\n"
                "按『是』：OCR成功後自動封存上一批路線並建立新批次；已完成的庫存與帳本會保留。\n"
                "按『否』：同一批重新跑OCR，不重置目前路線。\n"
                "按『取消』：先不匯入。",
                parent=self
            )
            if _new_batch is None:return
            should_refresh=bool(_new_batch)
        # 遊戲改版可能增減頁數；本輪選到幾張就處理幾張，不把15張當永久規則。

        # V5.22：匯入後就在同一條流程指定A/B，圖片只選一次。
        assign_path=shared_file("layout_assignments.json", fallback_local=False)
        try:
            with open(assign_path,encoding="utf-8") as f:existing=json.load(f)
            if not isinstance(existing,dict):existing={}
        except Exception:existing={}
        custom_names={"CA":"自訂版面 1","CB":"自訂版面 2"}
        try:
            with open(shared_file("crop_layouts.json"),encoding="utf-8") as f:_layout_cfg=json.load(f)
            _names=_layout_cfg.get("_profile_names",{}) if isinstance(_layout_cfg,dict) else {}
            for _key in ("CA","CB"):
                if str(_names.get(_key,"")).strip():custom_names[_key]=str(_names[_key]).strip()
        except Exception:pass

        win=tk.Toplevel(self);win.title("📷 這一刷的版面");smart_window(win,"layout_choice","940x760",820,650)
        win.transient(self);win.grab_set();win.configure(bg=CUTE_BG);win.minsize(760,620)
        hero=tk.Frame(win,bg="#fffef9",padx=22,pady=14);hero.pack(fill="x",padx=20,pady=(16,8))
        add_mascot(hero,size=78,opacity=0.16,quip_key="import")
        tk.Label(hero,text="📷 這一刷的版面",font=(FONT,27,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="通常全部選 A；只有特殊排版的圖片改成 B。最後一張已放在最上面。",
                 font=(FONT,12,"bold"),bg="#fffef9",fg="#607168",justify="left").pack(anchor="w",pady=(3,0))
        top=tk.Frame(win,bg=CUTE_GREEN_SOFT,padx=12,pady=9);top.pack(fill="x",padx=20,pady=4)
        vars_=[]
        def set_all(val):
            for _,vv in vars_:vv.set(val)
        tk.Label(top,text=f"共 {len(paths)} 張",font=(FONT,13,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).pack(side="left",padx=(2,14))
        tk.Button(top,text="全部設為 A",font=(FONT,13,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  command=lambda:set_all("A"),padx=18,pady=7).pack(side="left")
        tk.Button(top,text="全部設為 B",font=(FONT,12,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",
                  command=lambda:set_all("B"),padx=14,pady=7).pack(side="left",padx=8)
        all_custom=tk.Menubutton(top,text="全部設為自訂 ▾",font=(FONT,12,"bold"),bg=CUTE_YELLOW_SOFT,
                                 relief="flat",padx=14,pady=7)
        all_menu=tk.Menu(all_custom,tearoff=False,font=(FONT,11))
        for _key in ("CA","CB"):
            all_menu.add_command(label=custom_names[_key],command=lambda k=_key:set_all(k))
        all_custom.config(menu=all_menu);all_custom.pack(side="left",padx=4)
        tk.Label(top,text="A＝一般版面　B＝特殊版面　其他版型按『自訂』選擇",font=(FONT,10,"bold"),bg=CUTE_GREEN_SOFT,fg="#607168").pack(side="right",padx=5)

        holder=tk.Frame(win,bg=CUTE_BG);holder.pack(fill="both",expand=True,padx=20,pady=8)
        canvas=tk.Canvas(holder,bg=CUTE_BG,highlightthickness=0)
        sb=tk.Scrollbar(holder,orient="vertical",command=canvas.yview)
        inner=tk.Frame(canvas,bg=CUTE_BG)
        inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0),window=inner,anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        def _layout_wheel(e):
            if getattr(e,"delta",0):
                canvas.yview_scroll(int(-1*(e.delta/120))*3,"units")
            elif getattr(e,"num",None)==4:
                canvas.yview_scroll(-3,"units")
            elif getattr(e,"num",None)==5:
                canvas.yview_scroll(3,"units")
            return "break"
        canvas.bind("<Enter>",lambda e:canvas.bind_all("<MouseWheel>",_layout_wheel))
        canvas.bind("<Leave>",lambda e:canvas.unbind_all("<MouseWheel>"))
        canvas.bind("<Button-4>",_layout_wheel)
        canvas.bind("<Button-5>",_layout_wheel)
        # V5.47：A/B指定頁只「倒序顯示」，不改 paths 本身，因此OCR/資料順序完全不受影響。
        # 最後一張通常最可能是B版，直接放最上面，省掉每天2~3刷還要滾到底。
        for display_i,(orig_i,path) in enumerate(reversed(list(enumerate(paths,1))),1):
            i=orig_i
            name=os.path.basename(path)
            old=str(existing.get(name,"A")).upper()
            if old=="C":old="CA"
            vv=tk.StringVar(value=old if old in ("A","B","CA","CB") else "A")
            row_bg="#fffef9" if display_i%2 else CUTE_BLUE_SOFT
            row=tk.Frame(inner,bg=row_bg,padx=10,pady=7);row.pack(fill="x",pady=3)
            tk.Label(row,text=f"{i:02d}",width=4,font=(FONT,14,"bold"),bg=row_bg,fg=CUTE_GREEN).pack(side="left")
            tk.Label(row,text=name,anchor="w",font=(FONT,13,"bold"),bg=row_bg,fg=CUTE_TEXT).pack(side="left",fill="x",expand=True,padx=6)
            tk.Radiobutton(row,text="A 一般",variable=vv,value="A",font=(FONT,12,"bold"),bg=row_bg,
                           activebackground=row_bg,selectcolor=CUTE_GREEN_SOFT).pack(side="left",padx=8)
            tk.Radiobutton(row,text="B 特殊",variable=vv,value="B",font=(FONT,12,"bold"),bg=row_bg,
                           activebackground=row_bg,selectcolor=CUTE_YELLOW_SOFT).pack(side="left",padx=8)
            custom_pick=tk.Menubutton(row,text="自訂 ▾",font=(FONT,11,"bold"),bg="#f2e8ff",relief="raised",width=15)
            custom_menu=tk.Menu(custom_pick,tearoff=False,font=(FONT,11))
            for _key in ("CA","CB"):
                custom_menu.add_command(label=custom_names[_key],command=lambda k=_key,v=vv:v.set(k))
            custom_pick.config(menu=custom_menu);custom_pick.pack(side="left",padx=8)
            def _refresh_custom_button(*_args,v=vv,b=custom_pick):
                key=v.get();b.config(text=(custom_names.get(key,key)+" ▾" if key in ("CA","CB") else "自訂 ▾"))
            vv.trace_add("write",_refresh_custom_button);_refresh_custom_button()
            vars_.append((name,vv))
        proceed={"ok":False}
        def confirm_layout():
            d=dict(existing)
            for name,vv in vars_:d[name]=vv.get()
            os.makedirs(os.path.dirname(assign_path),exist_ok=True)
            with open(assign_path,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)
            proceed["ok"]=True;win.destroy()
        buttons=tk.Frame(win,bg="#fffef9",padx=16,pady=10);buttons.pack(fill="x",side="bottom")
        tk.Button(buttons,text="✓ 確認版面並開始 OCR",font=(FONT,16,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  activebackground="#0f5b31",activeforeground="white",command=confirm_layout,padx=24,pady=10).pack(side="right",padx=8)
        tk.Button(buttons,text="取消",font=(FONT,13),bg="#edf2ec",relief="flat",command=win.destroy,padx=18,pady=10).pack(side="right",padx=8)
        win.protocol("WM_DELETE_WINDOW",win.destroy)
        self.wait_window(win)
        if not proceed["ok"]:return

        save_path=os.path.join(BASE,"data","ocr_debug.json")
        progress=tk.Toplevel(self)
        progress.title("🔎 OCR 執行進度")
        remember_window(progress,"ocr_progress","720x320")
        progress.transient(self);progress.configure(bg=CUTE_BG)
        ph=tk.Frame(progress,bg="#fffef9",padx=20,pady=13);ph.pack(fill="x",padx=20,pady=(18,8))
        tk.Label(ph,text="🔎 正在讀取交換截圖",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(ph,text="一張一張整理中，完成後會自動打開確認頁。",font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
        progress_var=tk.StringVar(value=f"準備處理 {len(paths)} 張截圖")
        tk.Label(progress,textvariable=progress_var,font=(FONT,18,"bold"),bg=CUTE_BG,fg=CUTE_GREEN).pack(pady=(10,5))
        ocr_bar=ttk.Progressbar(progress,orient="horizontal",mode="determinate",maximum=max(1,len(paths)),value=0)
        ocr_bar.pack(fill="x",padx=56,pady=7)
        file_var=tk.StringVar(value="")
        tk.Label(progress,textvariable=file_var,font=(FONT,12,"bold"),bg=CUTE_BG,fg="#475467",wraplength=620).pack(pady=5)
        tk.Label(progress,text="單張圖片辨識時畫面可能短暫不動，完成這張後進度就會繼續前進。",
                 font=(FONT,11),bg=CUTE_YELLOW_SOFT,fg="#78622f",justify="center",padx=12,pady=7).pack(fill="x",padx=40,pady=8)

        def on_progress(stage,index,total,name):
            if stage=="start_image":
                progress_var.set(f"正在處理第 {index} / {total} 張")
                file_var.set(name)
                ocr_bar.configure(value=max(0,index-1))
            elif stage=="finish_image":
                progress_var.set(f"第 {index} / {total} 張 OCR完成，準備下一張")
                ocr_bar.configure(value=index)
            try:
                progress.update_idletasks()
                self.update_idletasks()
            except Exception:
                pass

        self.config(cursor="watch")
        try:
            result=OCR.run(paths,save_path=save_path,progress_cb=on_progress)
        except Exception as e:
            traceback.print_exc()
            try: progress.destroy()
            except: pass
            self.config(cursor="")
            messagebox.showerror("OCR啟動失敗",f"{type(e).__name__}: {e}\n\n完整錯誤已印在除錯CMD。")
            return
        finally:
            self.config(cursor="")
        try: progress.destroy()
        except: pass

        total=sum(x["line_count"] for x in result["images"])
        point_hits=[]; item_hits=[]
        for im in result["images"]:
            for line in im["lines"]:
                for h in line["hits"]:
                    if h["type"] in ("POINT","點位"): point_hits.append(h["name"])
                    if h["type"] in ("ITEM","品項"): item_hits.append(h["name"])
        point_hits=list(dict.fromkeys(point_hits))
        item_hits=list(dict.fromkeys(item_hits))

        structured=PARSER.parse_ocr_result(result)
        # V5.93.2：健檢必須看「成熟知識套用後」的真實結果。
        # 舊順序先把原始OCR的20筆當成失敗，之後確認頁才降到9筆，會讓玩家誤以為整庫失憶。
        recover_batch_remaining_times(structured)
        apply_confirmed_raw_identity_learning(structured)
        apply_confirmed_blank_context_learning(structured)
        apply_conservative_registry_typo_learning(structured)
        apply_visual_item_learning(structured)
        _register_observed_nonstage_items(structured)
        apply_learned_aliases_to_rows(structured)
        _promote_pending_aliases(structured)
        apply_manual_repairs(structured)
        reconcile_row_identities_independently(structured)
        apply_evidence_identity_corrections(structured)
        apply_learned_core_confirmations(structured)
        apply_visual_quantity_learning(structured)
        apply_ratio_confirmations(structured)
        apply_confirmed_material_input_quantity(structured)
        recover_structured_special_quantity_source(structured)
        recover_remaining_times_from_batch_twins(structured)
        for _rr in structured:normalize_dynamic_ratio_status(_rr)
        apply_current_ratio_corrections(structured)
        health=OCR.layout_health(result,structured)
        try:
            with open(shared_file("layout_health_last.json", fallback_local=False),"w",encoding="utf-8") as f:
                json.dump(health,f,ensure_ascii=False,indent=2)
        except Exception:pass
        if not health.get("ok"):
            bad=[x for x in health.get("images",[]) if x.get("suspect")]
            lines=[f"• {x}" for x in (health.get("batch_reasons") or [])]
            for x in bad[:8]:
                reason="、".join(x.get("reasons") or ["辨識成功率異常"])
                lines.append(f"• {x.get('file')}（版型{x.get('layout')}）：{reason}")
            more=f"\n…另有 {len(bad)-8} 張" if len(bad)>8 else ""
            use_anyway=messagebox.askyesno(
                "⚠ 版面健檢未通過",
                "切片可能看起來清楚，但實際OCR大量失敗。\n\n"+"\n".join(lines)+more+
                "\n\n按『是』：仍使用這批結果。\n按『否』：保留目前正常批次，開啟版型校正器。",
                parent=self)
            if not use_anyway:
                first_layout=str((bad[0] if bad else {}).get("layout") or "CA")
                CropCalibrator(self,shared_file("crop_layouts.json", fallback_local=False),initial_layout=first_layout)
                return
        # OCR成功才真正刷新，避免辨識失敗時先把玩家目前路線清空。
        if should_refresh:
            invalidate_batch(self.state_data)
            clear_current_ratio_corrections()
            self.state_data["route_learning_skip_batch"]=False
            self.state_data.pop("route_learning_pending_suggestion",None)
            STORE.save(self.state_data)
            self.current_route=None
        structured_path=os.path.join(BASE,"data","ocr_structured_debug.json")
        with open(structured_path,"w",encoding="utf-8") as f:
            json.dump(structured,f,ensure_ascii=False,indent=2)
        # V5.47.3.2：目前批次正式住進共用資料。版本資料夾可安心刪除。
        try:
            with open(CURRENT_BATCH_PATH,"w",encoding="utf-8") as f:
                json.dump(structured,f,ensure_ascii=False,indent=2)
        except Exception:
            pass

        msg=(f"OCR完成：{len(paths)}張 / {total}行文字\n"
             f"已命中點位：{len(point_hits)}個\n"
             f"已命中品項：{len(item_hits)}個\n"
             f"結構化候選：{len(structured)}列\n\n"
             "面板定位＋交換列＋精準欄位裁切圖：data\\ocr_roi_debug\\*.ROW*.png\n"
             "完整原始結果：data\\ocr_debug.json\n"
             "結構化結果：data\\ocr_structured_debug.json\n\n"
             "按確定後會打開『結構化候選』大字檢視。")
        messagebox.showinfo("🔎 OCR實機測試結果",msg)
        self.show_ocr_review(structured)
    def open_calibrator(self):
        CropCalibrator(self,shared_file("crop_layouts.json", fallback_local=False))

    def open_layout_health_check(self):
        """用單張圖片實跑，不改本輪資料；專抓肉眼切片正常但OCR不適合的UI差異。"""
        path=filedialog.askopenfilename(title="版面自救健檢｜選一張原始遊戲截圖",
            filetypes=[("圖片","*.png *.jpg *.jpeg *.webp"),("所有檔案","*.*")])
        if not path:return
        layout=simpledialog.askstring("選擇版型","請輸入 A、B、CA 或 CB。\nCA＝舊電腦一般，CB＝舊電腦特殊。",
                                      parent=self,initialvalue="CA")
        layout=str(layout or "").strip().upper()
        if layout=="C":layout="CA"
        if layout not in ("A","B","CA","CB"):
            messagebox.showwarning("版型不正確","請輸入 A、B、CA 或 CB。",parent=self);return
        assign_path=shared_file("layout_assignments.json", fallback_local=False)
        try:
            with open(assign_path,encoding="utf-8") as f:assign=json.load(f)
            if not isinstance(assign,dict):assign={}
        except Exception:assign={}
        assign[os.path.basename(path)]=layout
        os.makedirs(os.path.dirname(assign_path),exist_ok=True)
        with open(assign_path,"w",encoding="utf-8") as f:json.dump(assign,f,ensure_ascii=False,indent=2)
        self.config(cursor="watch");self.update_idletasks()
        try:
            debug_path=os.path.join(BASE,"data","layout_health_debug.json")
            result=OCR.run([path],save_path=debug_path)
            structured=PARSER.parse_ocr_result(result)
            health=OCR.layout_health(result,structured)
            with open(shared_file("layout_health_last.json", fallback_local=False),"w",encoding="utf-8") as f:
                json.dump(health,f,ensure_ascii=False,indent=2)
        except Exception as ex:
            traceback.print_exc();messagebox.showerror("健檢失敗",f"{type(ex).__name__}: {ex}",parent=self);return
        finally:self.config(cursor="")
        rec=(health.get("images") or [{}])[0]
        reasons="、".join(rec.get("reasons") or [])
        summary=(f"版型：{layout}\n切出列數：{rec.get('rows',0)}\n"
                 f"文字欄完整率：{float(rec.get('field_rate',0))*100:.0f}%\n"
                 f"平均OCR信心：{float(rec.get('average_confidence',0)):.2f}\n"
                 f"待確認：{rec.get('pending',0)}\n")
        if health.get("ok"):
            messagebox.showinfo("✓ 版面健檢通過",summary+"\n這個版型可以用來跑整批。",parent=self)
        else:
            go=messagebox.askyesno("⚠ 版面健檢未通過",summary+f"\n原因：{reasons or '辨識成功率異常'}\n\n要現在開啟校正器重框嗎？",parent=self)
            if go:CropCalibrator(self,shared_file("crop_layouts.json", fallback_local=False),initial_layout=layout)

    def open_b_calibrator(self):
        """V5.64.3｜讓 B 版重切入口保持在主畫面前段，並直接預選 Layout B。"""
        CropCalibrator(
            self,
            shared_file("crop_layouts.json", fallback_local=False),
            initial_layout="B"
        )

    def open_route_timing_manager(self,parent=None):
        win=tk.Toplevel(self);win.title("⏱ 海豹航行時間簿")
        smart_window(win,"route_timing_manager","980x700",760,540)
        win.transient(parent or self);win.configure(bg=CUTE_BG)
        close_timing=self._handoff_modal_grab(win)
        head=tk.Frame(win,bg="#fffef9",padx=20,pady=13);head.pack(fill="x",padx=18,pady=(16,8))
        add_mascot(head,size=70,opacity=0.16,quip_key="routes")
        tk.Label(head,text="⏱ 海豹航行時間簿",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(head,text="暫停時間不計；標記發呆的趟次會保留證據，但不拿去估算。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
        summary_var=tk.StringVar()
        tk.Label(win,textvariable=summary_var,font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,
                 anchor="w",padx=16,pady=10).pack(fill="x",padx=18,pady=5)
        box=tk.Frame(win,bg=CUTE_BG);box.pack(fill="both",expand=True,padx=18,pady=7)
        cols=("finished","route","stations","duration","valid","points")
        tree=ttk.Treeview(box,columns=cols,show="headings",selectmode="browse")
        labels={"finished":"完成時間","route":"路線","stations":"站數","duration":"有效用時","valid":"是否採用","points":"點位順序"}
        widths={"finished":150,"route":135,"stations":55,"duration":95,"valid":100,"points":350}
        for c in cols:tree.heading(c,text=labels[c]);tree.column(c,width=widths[c],anchor="center" if c!="points" else "w")
        sb=tk.Scrollbar(box,orient="vertical",command=tree.yview);tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        row_ids={}
        def stamp(v):
            try:return datetime.datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M")
            except Exception:return "—"
        def refresh():
            rows=load_route_timing_history();info=route_timing_summary(rows)
            med=format_route_duration(info["median"]) if info["median"] is not None else "尚無"
            avg=format_route_duration(info["average"]) if info["average"] is not None else "尚無"
            summary_var.set(f"全部 {info['total']} 趟｜有效 {info['valid']} 趟｜發呆／無效 {info['invalid']} 趟｜有效中位數 {med}｜平均 {avg}")
            for iid in tree.get_children():tree.delete(iid)
            row_ids.clear()
            def point_name(pid):
                rec=MASTER.point_by_id.get(str(pid),{})
                return str(rec.get("name") or rec.get("point_name") or pid)
            for row in sorted(rows,key=lambda x:float(x.get("finished_at") or 0),reverse=True):
                valid=row.get("valid") is not False
                iid=tree.insert("","end",values=(stamp(row.get("finished_at")),str(row.get("route_name") or "路線"),
                    int(row.get("station_count") or 0),format_route_duration(row.get("elapsed_seconds") or 0),
                    "採用" if valid else f"不採用｜{row.get('invalid_reason') or '無效'}"," → ".join(point_name(x) for x in row.get("point_ids",[]) or [])))
                row_ids[iid]=str(row.get("sample_id") or "")
        def delete_selected():
            sel=tree.selection()
            if not sel:return
            sid=row_ids.get(sel[0],"")
            if not messagebox.askyesno("刪除時間樣本","只刪除選取的計時樣本？不會影響路線或庫存。",parent=win):return
            save_route_timing_history([x for x in load_route_timing_history() if str(x.get("sample_id") or "")!=sid]);refresh()
        def open_segments():
            segwin=tk.Toplevel(win);segwin.title("🧭 海豹航段統計");segwin.transient(win)
            smart_window(segwin,"route_segment_stats","900x640",700,500);segwin.configure(bg=CUTE_BG)
            close_segments=self._handoff_modal_grab(segwin)
            hero=tk.Frame(segwin,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=16,pady=(14,7))
            tk.Label(hero,text="🧭 海豹學到的站內航段",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(hero,text="只計算實際連續完成的站點；不含出發到第一站，發呆趟也不採用。",
                     font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
            stats=route_segment_statistics();count_label=tk.StringVar(value=f"目前學到 {len(stats)} 組航段")
            tk.Label(segwin,textvariable=count_label,font=(FONT,13,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,
                     anchor="w",padx=14,pady=8).pack(fill="x",padx=16,pady=5)
            holder=tk.Frame(segwin,bg=CUTE_BG);holder.pack(fill="both",expand=True,padx=16,pady=7)
            cols2=("from","to","count","median","fast","slow","confidence")
            t2=ttk.Treeview(holder,columns=cols2,show="headings")
            labels2={"from":"從","to":"到","count":"樣本","median":"中位","fast":"最快","slow":"最慢","confidence":"信心／狀況"}
            widths2={"from":150,"to":150,"count":65,"median":90,"fast":90,"slow":90,"confidence":130}
            for c in cols2:t2.heading(c,text=labels2[c]);t2.column(c,width=widths2[c],anchor="center")
            sb2=tk.Scrollbar(holder,orient="vertical",command=t2.yview);t2.configure(yscrollcommand=sb2.set)
            t2.pack(side="left",fill="both",expand=True);sb2.pack(side="right",fill="y")
            for x in stats:
                state=x["confidence"]+("｜波動大 ⚠" if x["volatile"] else "")
                t2.insert("","end",values=(x["from_name"],x["to_name"],x["count"],format_route_duration(x["median"]),
                    format_route_duration(x["fastest"]),format_route_duration(x["slowest"]),state))
            if not stats:
                t2.insert("","end",values=("尚無資料","完成至少一條兩站以上的路線後，海豹才有航段可學。","—","—","—","—","—"))
            tk.Button(segwin,text="關閉",font=(FONT,11,"bold"),command=close_segments,padx=18,pady=7).pack(pady=(2,14))
        foot=tk.Frame(win,bg=CUTE_BG);foot.pack(fill="x",padx=18,pady=(2,14))
        tk.Button(foot,text="↻ 重新整理",font=(FONT,11,"bold"),command=refresh,padx=12,pady=6).pack(side="left")
        tk.Button(foot,text="刪除選取樣本",font=(FONT,11),command=delete_selected,padx=12,pady=6).pack(side="left",padx=5)
        tk.Button(foot,text="🧭 查看航段統計",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",
                  command=open_segments,padx=12,pady=6).pack(side="left",padx=5)
        tk.Button(foot,text="關閉",font=(FONT,11,"bold"),command=close_timing,padx=16,pady=6).pack(side="right")
        refresh()

    def open_route_learning_manager(self,parent=None):
        win=tk.Toplevel(self);win.title("🦭 海豹學到的路線習慣")
        smart_window(win,"route_learning_manager","980x720",760,560)
        win.transient(parent or self);win.configure(bg=CUTE_BG)
        close_learning=self._handoff_modal_grab(win)
        head=tk.Frame(win,bg="#fffef9",padx=20,pady=13);head.pack(fill="x",padx=18,pady=(16,8))
        add_mascot(head,size=72,opacity=0.16,quip_key="routes")
        tk.Label(head,text="🦭 海豹的路線筆記",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(head,text="每次按「儲存今日排法」才記一筆；同一刷重存只會更新，不會重複灌水。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))

        summary_var=tk.StringVar();habit_var=tk.StringVar();quality_var=tk.StringVar()
        summary=tk.Label(win,textvariable=summary_var,font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,
                         anchor="w",justify="left",padx=16,pady=10)
        summary.pack(fill="x",padx=18,pady=5)
        habit=tk.Label(win,textvariable=habit_var,font=(FONT,11),bg=CUTE_BLUE_SOFT,fg=CUTE_TEXT,
                       anchor="w",justify="left",padx=16,pady=9,wraplength=900)
        habit.pack(fill="x",padx=18,pady=5)
        quality=tk.Label(win,textvariable=quality_var,font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,fg=CUTE_TEXT,
                         anchor="w",justify="left",padx=16,pady=9,wraplength=900)
        quality.pack(fill="x",padx=18,pady=5)

        box=tk.Frame(win,bg=CUTE_BG);box.pack(fill="both",expand=True,padx=18,pady=7)
        cols=("updated","batch","routes","stations","excluded","feedback")
        tree=ttk.Treeview(box,columns=cols,show="headings",selectmode="browse")
        labels={"updated":"最後儲存","batch":"批次","routes":"路線","stations":"有效站點","excluded":"略過問號站","feedback":"建議結果"}
        widths={"updated":155,"batch":210,"routes":70,"stations":90,"excluded":100,"feedback":125}
        for c in cols:
            tree.heading(c,text=labels[c]);tree.column(c,width=widths[c],anchor="center" if c not in ("batch",) else "w")
        sb=tk.Scrollbar(box,orient="vertical",command=tree.yview);tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        rows={}

        def refresh():
            data=load_route_learning_history();records=[x for x in data.get("records",[]) if isinstance(x,dict)]
            info=route_learning_summary(records);n=info["batches"]
            phase="尚未開始" if n==0 else ("低信心｜先累積真實排法" if n<=2 else ("中信心｜已看得出一些習慣" if n<=5 else "高信心基礎｜相似資料越多越可靠"))
            summary_var.set(f"有效批次 {n}　｜　路線 {info['routes']}　｜　目前：{phase}\n"
                            f"海豹草稿套用 {info['suggestions']} 次：原樣採用 {info['accepted']}｜調整後採用 {info['edited']}")
            pairs="、".join(f"{a} → {b}（{count}次）" for (a,b),count in info["pairs"][:5]) or "還沒有足夠資料"
            points="、".join(f"{name or pid}（{count}次）" for (pid,name),count in info["points"][:5]) or "還沒有足夠資料"
            habit_var.set(f"常見相鄰順序：{pairs}\n常排交換點：{points}")
            q=seal_evaluation_summary()
            maturity=seal_learning_maturity()
            quality_var.set(f"草稿品質驗收：預覽 {q['viewed']} 次｜套用 {q['applied']} 次（{q['apply_percent']}%）｜正式儲存 {q['saved']} 次\n"
                            f"最後原樣採用 {q['accepted']} 次、調整後採用 {q['edited']} 次｜平均同線保留 {q['avg_same_route_percent']}%｜平均原位保留 {q['avg_exact_percent']}%\n"
                            f"成熟度：{maturity['level']}｜{maturity['trend']}。{maturity['warning']}")
            for iid in tree.get_children():tree.delete(iid)
            rows.clear()
            for rec in sorted(records,key=lambda x:str(x.get("updated_at") or ""),reverse=True):
                rid=str(rec.get("record_id") or rec.get("batch_id") or "")
                rs=rec.get("routes",[]) or [];stations=sum(len(r.get("points",[]) or []) for r in rs)
                fb=rec.get("suggestion_feedback") if isinstance(rec.get("suggestion_feedback"),dict) else {}
                fbtext="原樣採用 🌟" if fb.get("result")=="accepted" else ("調整後採用 ✏" if fb.get("result")=="edited" else "—")
                iid=tree.insert("","end",values=(str(rec.get("updated_at") or "").replace("T"," "),
                    str(rec.get("batch_id") or ""),len(rs),stations,int(rec.get("excluded_stations") or 0),fbtext))
                rows[iid]=rid

        def delete_selected():
            sel=tree.selection()
            if not sel:return
            rid=rows.get(sel[0],"")
            if not messagebox.askyesno("刪除學習紀錄","只刪除這一刷的路線學習紀錄？\n不會動到路線、庫存或OCR資料。",parent=win):return
            data=load_route_learning_history();data["records"]=[r for r in data.get("records",[]) if str(r.get("record_id") or r.get("batch_id") or "")!=rid]
            save_route_learning_history(data);refresh()

        def clear_all():
            if not messagebox.askyesno("清空海豹筆記","確定清空全部路線學習紀錄？\n不會動到路線、範本、庫存或OCR資料。",parent=win):return
            save_route_learning_history({"version":1,"records":[]});refresh()

        def open_quality_report():
            qwin=tk.Toplevel(win);qwin.title("🌟 海豹草稿品質驗收");qwin.transient(win)
            smart_window(qwin,"seal_quality_report","980x650",760,500);qwin.configure(bg=CUTE_BG)
            close_quality=self._handoff_modal_grab(qwin)
            hero=tk.Frame(qwin,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=16,pady=(14,7))
            tk.Label(hero,text="🌟 海豹草稿品質驗收",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(hero,text="只比較草稿和玩家最後儲存的分線與站序；不碰交換內容、庫存或帳本。預覽後未套用也會保留，避免只計算成功案例。",
                     font=(FONT,11),bg="#fffef9",fg="#607168",wraplength=900,justify="left").pack(anchor="w",pady=(2,0))
            data=load_seal_evaluations();records=[r for r in data.get("records",[]) if isinstance(r,dict)]
            info=seal_evaluation_summary(records)
            maturity=seal_learning_maturity(records)
            tk.Label(qwin,text=f"預覽 {info['viewed']}｜套用 {info['applied']}（{info['apply_percent']}%）｜正式儲存 {info['saved']}｜原樣採用 {info['accepted']}｜調整後採用 {info['edited']}\n"
                              f"正式儲存樣本平均：同線保留 {info['avg_same_route_percent']}%｜原位保留 {info['avg_exact_percent']}%\n"
                              f"成熟度：{maturity['level']}｜{maturity['message']}｜{maturity['trend']}\n{maturity['warning']}",
                     font=(FONT,13,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,justify="left",anchor="w",padx=14,pady=9).pack(fill="x",padx=16,pady=5)
            corrections=seal_correction_patterns(records)
            correction_text=("、".join(f"{x['point_name']}（{x['samples']}次樣本，換線或換位{x['position_change_percent']}%）" for x in corrections[:5])
                             if corrections else "至少要有同一站2份正式草稿樣本，才會開始列出常被調整的點。")
            tk.Label(qwin,text=f"海豹最常被你改的地方：{correction_text}\n只顯示發生比例，不會擅自猜測你為什麼修改。",
                     font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,fg=CUTE_TEXT,justify="left",anchor="w",wraplength=920,padx=14,pady=8).pack(fill="x",padx=16,pady=5)
            holder=tk.Frame(qwin,bg=CUTE_BG);holder.pack(fill="both",expand=True,padx=16,pady=7)
            cols2=("time","batch","status","confidence","coverage","result","same_route","exact")
            t2=ttk.Treeview(holder,columns=cols2,show="headings")
            labels2={"time":"預覽時間","batch":"批次","status":"進度","confidence":"信心","coverage":"安全對上","result":"最後結果","same_route":"同線保留","exact":"原位保留"}
            widths2={"time":145,"batch":170,"status":75,"confidence":60,"coverage":85,"result":100,"same_route":90,"exact":90}
            for c in cols2:t2.heading(c,text=labels2[c]);t2.column(c,width=widths2[c],anchor="center" if c!="batch" else "w")
            sb2=tk.Scrollbar(holder,orient="vertical",command=t2.yview);t2.configure(yscrollcommand=sb2.set)
            t2.pack(side="left",fill="both",expand=True);sb2.pack(side="right",fill="y")
            status_name={"previewed":"只預覽","applied":"已套用未儲存","saved":"已儲存"}
            for rec in sorted(records,key=lambda x:str(x.get("created_at") or ""),reverse=True):
                qu=rec.get("quality") if isinstance(rec.get("quality"),dict) else {}
                result="原樣採用 🌟" if rec.get("result")=="accepted" else ("調整後採用 ✏" if rec.get("result")=="edited" else "—")
                t2.insert("","end",values=(str(rec.get("created_at") or "").replace("T"," "),str(rec.get("batch_id") or ""),
                    status_name.get(rec.get("status"),str(rec.get("status") or "—")),str(rec.get("confidence") or "—"),
                    f"{int(rec.get('matched') or 0)}/{int(rec.get('eligible') or 0)}",result,
                    (f"{int(qu.get('same_route_percent') or 0)}%" if qu else "—"),(f"{int(qu.get('exact_percent') or 0)}%" if qu else "—")))
            if not records:t2.insert("","end",values=("尚無資料","開啟一次海豹建議預覽後，就會從這裡開始記錄。","—","—","—","—","—","—"))
            def clear_quality_records():
                if not messagebox.askyesno("清空草稿品質紀錄","只清空海豹草稿的預覽、套用與貼合成績？\n不會刪路線學習、計時、路線、庫存或OCR資料。",parent=qwin):return
                save_seal_evaluations({"version":1,"records":[]});refresh();close_quality()
            qfoot=tk.Frame(qwin,bg=CUTE_BG);qfoot.pack(fill="x",padx=16,pady=(2,14))
            tk.Button(qfoot,text="🧹 清空品質紀錄",font=(FONT,11),command=clear_quality_records,padx=12,pady=7).pack(side="left")
            tk.Button(qfoot,text="關閉",font=(FONT,11,"bold"),command=close_quality,padx=18,pady=7).pack(side="right")

        foot=tk.Frame(win,bg=CUTE_BG);foot.pack(fill="x",padx=18,pady=(2,15))
        tk.Button(foot,text="↻ 重新整理",font=(FONT,11,"bold"),command=refresh,padx=12,pady=6).pack(side="left")
        tk.Button(foot,text="刪除選取紀錄",font=(FONT,11),command=delete_selected,padx=12,pady=6).pack(side="left",padx=5)
        tk.Button(foot,text="🧹 清空全部學習",font=(FONT,11),command=clear_all,padx=12,pady=6).pack(side="left")
        tk.Button(foot,text="🌟 草稿品質驗收",font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,relief="flat",command=open_quality_report,padx=12,pady=6).pack(side="left",padx=5)
        tk.Button(foot,text="關閉",font=(FONT,11,"bold"),command=close_learning,padx=16,pady=6).pack(side="right")
        refresh()

    def open_player_settings(self):
        win=tk.Toplevel(self);win.title("⚙ 玩家設定中心");smart_window(win,"player_settings","900x820",760,600);win.transient(self);win.grab_set()
        win.configure(bg=CUTE_BG)
        shell=tk.Frame(win,bg=CUTE_BG);shell.pack(fill="both",expand=True)
        cv=tk.Canvas(shell,bg=CUTE_BG,highlightthickness=0);sb=tk.Scrollbar(shell,orient="vertical",command=cv.yview)
        body=tk.Frame(cv,bg=CUTE_BG);body_id=cv.create_window((0,0),window=body,anchor="nw")
        cv.configure(yscrollcommand=sb.set);cv.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
        body.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(body_id,width=e.width))
        def _settings_wheel(e):
            cv.yview_scroll(-3 if e.delta>0 else 3,"units");return "break"
        win.bind("<MouseWheel>",_settings_wheel,add="+")
        hero=tk.Frame(body,bg="#fffef9",padx=22,pady=14);hero.pack(fill="x",padx=24,pady=(18,8))
        add_mascot(hero,size=82,opacity=0.18,quip_key="settings")
        tk.Label(hero,text="⚙ 玩家設定中心",font=(FONT,27,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="船隻、負重與顯示習慣，都集中放在這裡。",font=(FONT,12,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(3,0))

        def settings_section(title,subtitle,color):
            section_shell,section=make_round_panel(body,bg=color,outline=CUTE_BORDER,radius=24,padding=16,min_height=150)
            section_shell.pack(fill="x",padx=24,pady=8)
            tk.Label(section,text=title,font=(FONT,18,"bold"),bg=color,fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(section,text=subtitle,font=(FONT,10),bg=color,fg="#607168").pack(anchor="w",pady=(2,9))
            content=tk.Frame(section,bg=color);content.pack(fill="x")
            return content

        frm=settings_section("⚡ 交涉力與文字大小","平常只需要填遊戲面板上的減免；其餘數字可沿用預設。",CUTE_GREEN_SOFT)
        discount=tk.StringVar(value=str(PLAYER_SETTINGS.get("negotiation_discount_percent",0)))
        normal=tk.StringVar(value=str(PLAYER_SETTINGS.get("normal_barter_base_power",14286)))
        high=tk.StringVar(value=str(PLAYER_SETTINGS.get("high_barter_base_power",21650)))
        display=tk.StringVar(value=PLAYER_SETTINGS.get("display_size","大字"))
        show_stage_input=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("show_stage_input_qty",False)))
        show_nonstage_input=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("show_nonstage_input_qty",True)))
        show_low_output=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("show_lowstage_output_qty",True)))
        show_high_output=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("show_highstage_output_qty",False)))
        show_nonstage_output=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("show_nonstage_output_qty",True)))
        route_learning_enabled=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("route_learning_enabled",True)))
        ui_show_timing=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_timing",True)))
        ui_show_weight=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_weight",True)))
        ui_show_ship_switch=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_ship_switch",True)))
        ui_show_route_requirements=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_route_requirements",True)))
        ui_show_daze_card=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_daze_card",True)))
        ui_show_seal_features=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_seal_features",True)))
        ui_show_negotiation_power=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("ui_show_negotiation_power",True)))
        ships=copy.deepcopy(PLAYER_SETTINGS.get("ships") or [])
        if not ships:ships=[{"id":"SHIP_1","name":"我的船","capacity":0.0}]
        active_id=str(PLAYER_SETTINGS.get("active_ship_id") or ships[0]["id"])
        active_index=next((i for i,x in enumerate(ships) if str(x.get("id"))==active_id),0)
        ship_name=tk.StringVar(value=str(ships[active_index].get("name") or "未命名船"))
        ship_capacity=tk.StringVar(value=("" if not ships[active_index].get("capacity") else str(ships[active_index].get("capacity"))))
        setting_entries={}
        for i,(lab,var,hint) in enumerate((
            ("遊戲顯示的交涉力減免 (%)",discount,"例：40.27"),
            ("普貨｜單次必要交涉力",normal,"目前預設 14,286"),
            ("高貨／鴉幣｜單次必要交涉力",high,"目前預設 21,650"))):
            tk.Label(frm,text=lab,font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).grid(row=i,column=0,sticky="w",pady=9)
            ent=tk.Entry(frm,textvariable=var,font=(FONT,16),width=14)
            ent.grid(row=i,column=1,padx=12);setting_entries[i]=ent
            tk.Label(frm,text=hint,font=(FONT,11),bg=CUTE_GREEN_SOFT,fg="#667085").grid(row=i,column=2,sticky="w")
        def _decimal_key(event):
            # Windows 會依鍵盤／Tk版本送出 decimal、KP_Decimal 或 period；全部統一成半形小數點。
            if is_decimal_input_event(event.keysym,event.char):
                widget=event.widget
                try:
                    if widget.selection_present():widget.delete("sel.first","sel.last")
                except Exception:pass
                # 一個百分比只能有一個小數點，避免誤按後到儲存時才看到格式錯誤。
                if "." in normalize_decimal_text(widget.get()):return "break"
                widget.insert("insert",".");return "break"
        setting_entries[0].bind("<KeyPress>",_decimal_key,add="+")
        remain=tk.StringVar()
        def upd(*_):
            try:
                v=parse_percent(discount.get())
                remain.set(f"減免 {v:.2f}% → 實際消耗 {100-v:.2f}%" if 0<=v<=100 else "請輸入 0～100")
            except:remain.set("可輸入小數，例如 40.27")
        discount.trace_add("write",upd);upd()
        tk.Label(frm,textvariable=remain,font=(FONT,13,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).grid(row=3,column=0,columnspan=3,sticky="w",pady=8)
        tk.Label(frm,text="顯示大小",font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).grid(row=4,column=0,sticky="w",pady=8)
        tk.OptionMenu(frm,display,"標準","大字","超大字").grid(row=4,column=1,sticky="w",padx=12)

        display_box=settings_section("👀 今日交換顯示方式","只影響畫面怎麼寫，不會改變交換數量、庫存或物品 ID。",CUTE_BLUE_SOFT)
        for _text,_var in (
            ("階段品作為投入物時，也顯示每次要拿幾個（例：某4階品 ×1）",show_stage_input),
            ("一般材料作為投入物時，顯示需求數量（例：烤鳥肉 ×300）",show_nonstage_input),
            ("1→2、2→3交換：目標物顯示一次會換到幾個（例：五色珠 ×2）",show_low_output),
            ("3→4、4→5、5→6、6→7交換：目標物也顯示數量",show_high_output),
            ("特殊交換的目標物顯示數量（例：烏鴉硬幣 ×20）",show_nonstage_output)):
            tk.Checkbutton(display_box,text=_text,variable=_var,font=(FONT,11),bg=CUTE_BLUE_SOFT,
                           activebackground=CUTE_BLUE_SOFT,selectcolor="#fffef9").pack(anchor="w",pady=2)

        feature_box=settings_section("🧩 功能顯示清單","進階玩家可把不需要的資訊收起來；只影響畫面，不刪資料，也不停止背景紀錄。","#f2effa")
        for _text,_var in (
            ("顯示航行計時與歷史時間預估",ui_show_timing),
            ("顯示負重估算與逐站負重明細",ui_show_weight),
            ("有多艘船時顯示快速切換船隻",ui_show_ship_switch),
            ("顯示本趟剩餘需帶交換品",ui_show_route_requirements),
            ("顯示路線內的發呆卡",ui_show_daze_card),
            ("顯示海豹建議排法與航段省時體檢",ui_show_seal_features),
            ("顯示交涉力摘要與路線預估交涉力",ui_show_negotiation_power)):
            tk.Checkbutton(feature_box,text=_text,variable=_var,font=(FONT,11),bg="#f2effa",
                           activebackground="#f2effa",selectcolor="#fffef9").pack(anchor="w",pady=3)
        tk.Label(feature_box,text="提示：關掉計時顯示後，已累積的時間樣本仍保留；重新勾回就看得到。",
                 font=(FONT,10,"bold"),bg="#f2effa",fg="#78622f").pack(anchor="w",pady=(6,1))

        weight_box=settings_section("⛵ 船隻與負重提示","可以存多艘船；左邊選目前出航船，超重只提醒、不會鎖住路線。",CUTE_YELLOW_SOFT)
        ship_combo=ttk.Combobox(weight_box,state="readonly",font=(FONT,12),width=18)
        ship_combo.grid(row=0,column=0,padx=(0,8),pady=4)
        tk.Entry(weight_box,textvariable=ship_name,font=(FONT,13),width=18).grid(row=0,column=1,padx=4)
        tk.Entry(weight_box,textvariable=ship_capacity,font=(FONT,13),width=12).grid(row=0,column=2,padx=4)
        tk.Label(weight_box,text="LT",font=(FONT,12,"bold"),bg=CUTE_YELLOW_SOFT).grid(row=0,column=3,padx=(0,8))
        current_ship={"index":active_index}
        def ship_values():ship_combo.configure(values=[str(x.get("name") or "未命名船") for x in ships])
        def commit_ship():
            i=current_ship["index"]
            if not (0<=i<len(ships)):return True
            name=ship_name.get().strip() or "未命名船"
            captext=ship_capacity.get().strip().replace(",","")
            try:cap=float(captext) if captext else 0.0
            except Exception:return False
            if cap<0:return False
            ships[i]["name"]=name;ships[i]["capacity"]=cap
            return True
        def persist_ships(commit=True):
            if commit and not commit_ship():return False
            i=current_ship["index"]
            PLAYER_SETTINGS["ships"]=copy.deepcopy(ships)
            PLAYER_SETTINGS["active_ship_id"]=str(ships[i]["id"])
            PLAYER_SETTINGS["ship_weight_capacity"]=float(ships[i].get("capacity",0) or 0)
            save_player_settings(PLAYER_SETTINGS);ship_values()
            return True
        def choose_ship(_=None):
            old=current_ship["index"]
            if not commit_ship():
                messagebox.showerror("負重格式錯誤","船隻負重請留空或輸入大於0的數字。",parent=win);ship_combo.current(old);return
            i=ship_combo.current();current_ship["index"]=i
            ship_name.set(str(ships[i].get("name") or "未命名船"));ship_capacity.set("" if not ships[i].get("capacity") else str(ships[i].get("capacity")))
            persist_ships(False)
        def add_ship():
            if not commit_ship():messagebox.showerror("負重格式錯誤","先修正目前船隻的負重數字。",parent=win);return
            name=simpledialog.askstring("新增船隻","這艘船叫什麼名字？",parent=win)
            if not name or not name.strip():return
            cap=simpledialog.askfloat("新增船隻",f"{name.strip()} 的可用負重（LT）：",parent=win,minvalue=0)
            if cap is None:return
            used={str(x.get("id") or "") for x in ships};n=1
            while f"SHIP_{n}" in used:n+=1
            ships.append({"id":f"SHIP_{n}","name":name.strip(),"capacity":float(cap)})
            ship_values();current_ship["index"]=len(ships)-1;ship_combo.current(len(ships)-1)
            ship_name.set(name.strip());ship_capacity.set(str(float(cap)))
            persist_ships(False)
        def delete_ship():
            if len(ships)<=1:
                messagebox.showinfo("至少保留一艘船","唯一的船不能刪除；可以直接改船名和負重。",parent=win);return
            i=current_ship["index"]
            if not messagebox.askyesno("刪除船隻",f"刪除「{ships[i].get('name')}」？",parent=win):return
            ships.pop(i);i=max(0,min(i,len(ships)-1));current_ship["index"]=i;ship_values();ship_combo.current(i)
            ship_name.set(str(ships[i].get("name") or "未命名船"));ship_capacity.set("" if not ships[i].get("capacity") else str(ships[i].get("capacity")))
            persist_ships(False)
        ship_values();ship_combo.current(active_index);ship_combo.bind("<<ComboboxSelected>>",choose_ship)
        tk.Button(weight_box,text="＋ 新增船隻",font=(FONT,10,"bold"),command=add_ship).grid(row=1,column=0,pady=4,sticky="w")
        tk.Button(weight_box,text="刪除目前船隻",font=(FONT,10),command=delete_ship).grid(row=1,column=1,pady=4,sticky="w")
        tk.Label(weight_box,text="船名和負重會直接保存；切換船隻後，路線卡會用目前這艘估算。",
                 font=(FONT,10),bg=CUTE_YELLOW_SOFT,fg="#667085").grid(row=1,column=2,columnspan=2,sticky="w")

        learn_box=settings_section("🦭 海豹路線學習","儲存今日排法時自動記住最終點位分組與順序；不完整或有問號的站會略過。",CUTE_GREEN_SOFT)
        tk.Checkbutton(learn_box,text="自動記住我儲存的排法",variable=route_learning_enabled,
                       font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,selectcolor="#fffef9").pack(side="left",padx=(0,12),pady=5)
        tk.Button(learn_box,text="📖 查看海豹學到的習慣",font=(FONT,11,"bold"),bg="#fffef9",relief="flat",
                  command=lambda:self.open_route_learning_manager(win),padx=12,pady=6).pack(side="left",padx=5)
        tk.Button(learn_box,text="⏱ 查看航行時間簿",font=(FONT,11,"bold"),bg="#fffef9",relief="flat",
                  command=lambda:self.open_route_timing_manager(win),padx=12,pady=6).pack(side="left",padx=5)

        def defaults():
            discount.set("0");normal.set("14286");high.set("21650")
        tk.Button(frm,text="↺ 恢復目前預設值",font=(FONT,11,"bold"),command=defaults,padx=10,pady=5).grid(row=5,column=0,columnspan=3,sticky="w",pady=8)
        info=settings_section("🧰 其他玩家自訂資料","名稱縮寫和交換點分組都只是你的顯示習慣，不會改動官方身份資料。","#f2effa")
        tk.Label(info,text="📦 庫存：在今日交換選擇直接手動維護。",font=(FONT,13),bg="#f2effa",anchor="w").pack(fill="x",pady=4)
        tk.Label(info,text="🏷 物品縮寫：玩家自定義；不影響 ITEM_ID。",font=(FONT,13),bg="#f2effa",anchor="w").pack(fill="x",pady=4)
        tk.Button(info,text="🏷 編輯玩家物品縮寫",font=(FONT,12,"bold"),
                  command=lambda:self.open_item_alias_manager(win),padx=12,pady=6).pack(anchor="w",pady=4)
        tk.Button(info,text="🗺 編輯交換點自訂分類／群組",font=(FONT,12,"bold"),
                  command=lambda:self.open_point_group_manager(win),padx=12,pady=6).pack(anchor="w",pady=6)
        tk.Button(info,text="🧭 編輯特殊交換自訂分頁",font=(FONT,12,"bold"),
                  command=lambda:self.open_special_tab_manager(win),padx=12,pady=6).pack(anchor="w",pady=4)
        resetrow=tk.Frame(info,bg="#f2effa");resetrow.pack(fill="x",pady=(9,4))
        tk.Button(resetrow,text="↺ 恢復我的航海預設",font=(FONT,11,"bold"),bg=CUTE_GREEN_SOFT,relief="flat",
                  command=lambda:self.reset_player_customizations("preset",win),padx=11,pady=6).pack(side="left",padx=(0,5))
        tk.Button(resetrow,text="🧹 清空所有自訂",font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,relief="flat",
                  command=lambda:self.reset_player_customizations("clear",win),padx=11,pady=6).pack(side="left",padx=5)
        tk.Button(resetrow,text="🛟 還原上次備份",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",
                  command=lambda:self.reset_player_customizations("restore",win),padx=11,pady=6).pack(side="left",padx=5)
        tk.Label(info,text="執行恢復或清空前會自動備份；不碰庫存、OCR、路線、範本或帳本。",
                 font=(FONT,10,"bold"),bg="#f2effa",fg="#78622f",anchor="w").pack(fill="x",pady=(1,4))
        tk.Label(info,text="🔒 ID、OCR證物、去重、資料關聯由系統維護，不開放手改。",font=(FONT,12),bg="#f2effa",fg="#667085",anchor="w").pack(fill="x",pady=4)
        def close():
            if not persist_ships():
                messagebox.showerror("負重格式錯誤","船隻負重請留空或輸入大於 0 的數字。",parent=win);return
            try:win.grab_release()
            except:pass
            win.destroy()
        def done():
            try:
                d=parse_percent(discount.get())
                n=int(normal.get().strip().replace(",",""));h=int(high.get().strip().replace(",",""))
                if not commit_ship():raise ValueError
                shipcap=float(ships[current_ship["index"]].get("capacity",0) or 0)
                if not 0<=d<=100 or n<=0 or h<=0 or shipcap<0:raise ValueError
            except:
                messagebox.showerror("數值不正確","減免請填 0～100；必要交涉力請填大於 0 的整數；船隻負重請留空或填大於 0 的數字。",parent=win);return
            PLAYER_SETTINGS.update({"negotiation_discount_percent":d,"normal_barter_base_power":n,
                                    "high_barter_base_power":h,"display_size":display.get(),
                                    "show_stage_input_qty":bool(show_stage_input.get()),
                                    "show_nonstage_input_qty":bool(show_nonstage_input.get()),
                                    "show_lowstage_output_qty":bool(show_low_output.get()),
                                    "show_highstage_output_qty":bool(show_high_output.get()),
                                    "show_nonstage_output_qty":bool(show_nonstage_output.get()),
                                    "route_learning_enabled":bool(route_learning_enabled.get()),
                                    "ui_show_timing":bool(ui_show_timing.get()),
                                    "ui_show_weight":bool(ui_show_weight.get()),
                                    "ui_show_ship_switch":bool(ui_show_ship_switch.get()),
                                    "ui_show_route_requirements":bool(ui_show_route_requirements.get()),
                                    "ui_show_daze_card":bool(ui_show_daze_card.get()),
                                    "ui_show_seal_features":bool(ui_show_seal_features.get()),
                                    "ui_show_negotiation_power":bool(ui_show_negotiation_power.get()),
                                    "ship_weight_capacity":shipcap,
                                    "ships":ships,"active_ship_id":str(ships[current_ship["index"]]["id"])})
            save_player_settings(PLAYER_SETTINGS);messagebox.showinfo("玩家設定","✓ 已儲存。重新進入交換選擇即套用。",parent=win);close()
        foot=tk.Frame(win,bg="#fffef9",padx=18,pady=10);foot.pack(side="bottom",fill="x")
        tk.Label(foot,text="設定會保存在共用資料中，換新版也會保留。",font=(FONT,10,"bold"),bg="#fffef9",fg="#607168").pack(side="left")
        tk.Button(foot,text="取消",font=(FONT,13),bg="#edf2ec",relief="flat",command=close,padx=18,pady=8).pack(side="right",padx=6)
        tk.Button(foot,text="✓ 儲存設定",font=(FONT,15,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  activebackground="#0f5b31",activeforeground="white",command=done,padx=24,pady=9).pack(side="right",padx=6)
        win.protocol("WM_DELETE_WINDOW",close)

    def open_item_alias_manager(self,parent=None):
        aliases=load_player_item_aliases()
        win=tk.Toplevel(parent or self);win.title("🏷 玩家物品縮寫")
        smart_window(win,"item_alias_manager","920x760",820,650);win.transient(parent or self);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=11);hero.pack(fill="x",padx=16,pady=(14,7))
        add_mascot(hero,size=72,opacity=0.14,quip_key="custom")
        tk.Label(hero,text="🏷 玩家物品縮寫",font=(FONT,22,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="縮寫只屬於玩家設定。你可以改、刪、留空；官方 ITEM_ID 與名稱不會被改。",
                 font=(FONT,12),bg="#fffef9",fg="#536176").pack(anchor="w",pady=(2,0))
        ps=get_page_state("item_alias_manager")
        search=tk.StringVar(value=str(ps.get("search") or ""))
        top=tk.Frame(win);top.pack(fill="x",padx=16,pady=6)
        tk.Label(top,text="搜尋",font=(FONT,12,"bold")).pack(side="left")
        tk.Entry(top,textvariable=search,font=(FONT,13),width=28).pack(side="left",padx=7)
        cv=tk.Canvas(win,highlightthickness=0);sb=tk.Scrollbar(win,orient="vertical",command=cv.yview)
        body=tk.Frame(cv);wid=cv.create_window((0,0),window=body,anchor="nw")
        cv.configure(yscrollcommand=sb.set);cv.pack(side="left",fill="both",expand=True,padx=(16,0),pady=6)
        sb.pack(side="right",fill="y",padx=(0,10),pady=6)
        body.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        win.bind("<MouseWheel>",lambda e:(cv.yview_scroll(-3 if e.delta>0 else 3,"units"),"break")[1],add="+")
        items=[]
        try:
            with open(os.path.join(BASE,"data","master_data.json"),"r",encoding="utf-8") as f:md=json.load(f)
            items=[x for x in md.get("items",[]) if isinstance(x,dict) and x.get("id")]
        except Exception:pass
        vars_={}
        def render(*_):
            for w in body.winfo_children():w.destroy()
            for j,h in enumerate(("階段","官方名稱","你的縮寫")):
                tk.Label(body,text=h,font=(FONT,12,"bold"),bg="#dce6f1",anchor="w",
                         width=(8,34,18)[j]).grid(row=0,column=j,sticky="ew")
            q=search.get().strip().lower()
            shown=[x for x in items if not q or q in str(x.get("name","")).lower() or q in str(aliases.get(str(x["id"]),"")).lower()]
            for i,x in enumerate(shown,1):
                iid=str(x["id"])
                tk.Label(body,text=str(x.get("stage") or ""),font=(FONT,12)).grid(row=i,column=0,sticky="ew",pady=3)
                tk.Label(body,text=str(x.get("name") or ""),font=(FONT,13,"bold"),anchor="w").grid(row=i,column=1,sticky="ew",padx=5,pady=3)
                v=vars_.setdefault(iid,tk.StringVar(value=str(aliases.get(iid,""))))
                tk.Entry(body,textvariable=v,font=(FONT,13),width=18).grid(row=i,column=2,sticky="w",padx=5,pady=3)
            body.columnconfigure(1,weight=1)
        search.trace_add("write",render);render()
        try:win.after(100,lambda:cv.yview_moveto(float(ps.get("scroll",0) or 0)))
        except:pass
        def save_all():
            for iid,v in vars_.items():
                val=v.get().strip()
                if val:aliases[iid]=val
                else:aliases.pop(iid,None)
            save_player_item_aliases(aliases)
            save_page_state("item_alias_manager",search=search.get(),scroll=cv.yview()[0] if cv.yview() else 0)
            messagebox.showinfo("玩家物品縮寫","✓ 已儲存。官方資料庫沒有被修改。",parent=win)
        def close():
            save_page_state("item_alias_manager",search=search.get(),scroll=cv.yview()[0] if cv.yview() else 0);win.destroy()
        foot=tk.Frame(win);foot.pack(fill="x",padx=16,pady=10)
        tk.Button(foot,text="✓ 儲存",font=(FONT,14,"bold"),command=save_all,padx=18,pady=7).pack(side="right")
        win.protocol("WM_DELETE_WINDOW",close)

    def reset_player_customizations(self,mode,parent=None):
        host=parent or self
        if mode=="restore":
            snap=_load_json_dict(CUSTOM_SETTINGS_BACKUP_PATH,{})
            if not snap:
                messagebox.showinfo("還原上次備份","目前還沒有可還原的自訂設定備份。",parent=host);return
            when=str(snap.get("created_at") or "未知時間")
            if not messagebox.askyesno("還原上次備份",f"要還原 {when} 保存的自訂設定嗎？\n\n目前設定會被取代，但庫存、OCR、路線、範本與帳本不受影響。",parent=host):return
            restore_player_customizations(snap)
            messagebox.showinfo("自訂設定",f"✓ 已還原 {when} 的備份。\n重新進入今日交換選擇就會套用。",parent=host);return

        label="恢復我的航海預設" if mode=="preset" else "清空所有自訂"
        detail=("物品縮寫、交換點分類、特殊交換分頁與路線顯示偏好會恢復成內建航海預設。"
                if mode=="preset" else
                "物品縮寫、交換點分類與特殊交換分頁會全部清空；其他玩家可以從空白重新建立自己的體系。")
        if not messagebox.askyesno(label,f"{detail}\n\n執行前會自動備份目前設定。\n庫存、OCR、路線、範本與帳本都不會被修改。\n\n確定繼續？",parent=host):return
        backup_player_customizations()
        if mode=="preset":
            save_player_item_aliases(builtin_player_aliases())
            save_player_point_groups(builtin_point_groups())
            save_special_tabs(copy.deepcopy(DEFAULT_SPECIAL_TABS))
            save_player_route_ui({"show_point_alias":False,"show_stage_colors":True,
                "stage_colors":{"1":"#FFF4CC","2":"#E8F4FF","3":"#E9F8E7","4":"#F2E9FF","5":"#FFE9E9","6":"#E8F7F4","7":"#FFD7A8"}})
        else:
            save_player_item_aliases({});save_player_point_groups({});save_special_tabs([])
            save_player_route_ui({"show_point_alias":False,"show_stage_colors":True,
                "stage_colors":{"1":"#FFF4CC","2":"#E8F4FF","3":"#E9F8E7","4":"#F2E9FF","5":"#FFE9E9","6":"#E8F7F4","7":"#FFD7A8"}})
        messagebox.showinfo("自訂設定",f"✓ 已{label}。\n若結果不是你要的，可按「還原上次備份」。",parent=host)

    def open_special_tab_manager(self,parent=None):
        tabs=load_special_tabs()
        win=tk.Toplevel(parent or self);win.title("🧭 特殊交換自訂分頁")
        smart_window(win,"special_tab_manager","760x650",700,560);win.transient(parent or self);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=18,pady=(14,7))
        add_mascot(hero,size=76,opacity=0.16,quip_key="custom")
        tk.Label(hero,text="🧭 特殊交換自訂分頁",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="分頁名稱可以自己取；右欄填這一頁要包含哪些交換點分類。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(3,0))
        note=tk.Label(win,text="例：分頁『G』包含 G、高G；多個分類請用逗號隔開。『全部特殊』與未命中任何自訂頁的『其他』由系統固定保留。",
                      font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f",padx=12,pady=8)
        note.pack(fill="x",padx=18,pady=(0,7))
        box=tk.Frame(win,bg="#fffef9",padx=10,pady=8);box.pack(fill="both",expand=True,padx=18,pady=4)
        lb=tk.Listbox(box,font=(FONT,15,"bold"),width=22,exportselection=False,selectbackground="#bfe2b9",activestyle="none")
        lb.pack(side="left",fill="both",expand=True,padx=(0,9))
        edit=tk.Frame(box,bg=CUTE_GREEN_SOFT,padx=12,pady=12);edit.pack(side="left",fill="both",expand=True)
        namev=tk.StringVar();groupsv=tk.StringVar();current={"index":None}
        tk.Label(edit,text="分頁名稱",font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT).pack(anchor="w")
        tk.Entry(edit,textvariable=namev,font=(FONT,14),relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).pack(fill="x",pady=(3,12))
        tk.Label(edit,text="包含的交換點分類",font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT).pack(anchor="w")
        tk.Entry(edit,textvariable=groupsv,font=(FONT,14),relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).pack(fill="x",pady=(3,5))
        tk.Label(edit,text="例如：G, 高G",font=(FONT,10),bg=CUTE_GREEN_SOFT,fg="#607168").pack(anchor="w")
        def label(rec):return f"{rec['name']}　←　{('、'.join(rec.get('groups') or [])) or '尚未指定分類'}"
        def refresh(select=None):
            lb.delete(0,"end")
            for rec in tabs:lb.insert("end",label(rec))
            if tabs:
                i=0 if select is None else max(0,min(int(select),len(tabs)-1));lb.selection_set(i);lb.activate(i);load_selected(i)
            else:current["index"]=None;namev.set("");groupsv.set("")
        def load_selected(i=None):
            if i is None:
                sel=lb.curselection();i=int(sel[0]) if sel else None
            if i is None or not (0<=i<len(tabs)):return
            current["index"]=i;namev.set(tabs[i]["name"]);groupsv.set(", ".join(tabs[i].get("groups") or []))
        def commit():
            i=current.get("index")
            if i is None:return False
            name=namev.get().strip()
            if not name or name in ("全部特殊","其他"):messagebox.showerror("分頁名稱","請輸入分頁名稱；『全部特殊』與『其他』是系統保留名稱。",parent=win);return False
            if any(j!=i and x.get("name")==name for j,x in enumerate(tabs)):
                messagebox.showerror("分頁名稱",f"已經有「{name}」分頁。",parent=win);return False
            groups=[x.strip() for x in re.split(r"[,，]",groupsv.get()) if x.strip()]
            tabs[i]={"name":name,"groups":list(dict.fromkeys(groups))};refresh(i);return True
        def add():
            tabs.append({"name":f"新分頁{len(tabs)+1}","groups":[]});refresh(len(tabs)-1);namev.focus_set()
        def delete():
            i=current.get("index")
            if i is None:return
            tabs.pop(i);refresh(max(0,i-1))
        def move(delta):
            i=current.get("index")
            if i is None:return
            if namev.get().strip()!=tabs[i].get("name") or groupsv.get().replace("，",",").replace(" ","")!=",".join(tabs[i].get("groups") or []):
                if not commit():return
                i=current.get("index")
            j=i+delta
            if not (0<=j<len(tabs)):return
            tabs[i],tabs[j]=tabs[j],tabs[i];refresh(j)
        lb.bind("<<ListboxSelect>>",lambda _e:load_selected())
        buttons=tk.Frame(edit,bg=CUTE_GREEN_SOFT);buttons.pack(fill="x",pady=16)
        tk.Button(buttons,text="＋ 新增",font=(FONT,11,"bold"),command=add).pack(side="left",padx=2)
        tk.Button(buttons,text="🗑 刪除",font=(FONT,11),command=delete).pack(side="left",padx=2)
        tk.Button(buttons,text="↑",font=(FONT,12,"bold"),command=lambda:move(-1)).pack(side="left",padx=2)
        tk.Button(buttons,text="↓",font=(FONT,12,"bold"),command=lambda:move(1)).pack(side="left",padx=2)
        tk.Button(edit,text="✓ 套用這列修改",font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",command=commit,padx=12,pady=7).pack(anchor="w")
        foot=tk.Frame(win,bg="#fffef9",padx=12,pady=9);foot.pack(fill="x",padx=18,pady=(5,12))
        def save_all():
            i=current.get("index")
            if i is not None and not commit():return
            save_special_tabs(tabs);messagebox.showinfo("特殊交換分頁","✓ 已儲存。重新進入今日交換選擇就會套用。",parent=win)
        tk.Button(foot,text="✓ 儲存全部分頁",font=(FONT,13,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",command=save_all,padx=18,pady=7).pack(side="right")
        refresh()

    def open_point_group_manager(self,parent=None):
        groups=load_player_point_groups()
        win=tk.Toplevel(parent or self);win.title("🗺 玩家交換點分類")
        smart_window(win,"point_group_manager","900x760",820,650);win.transient(parent or self);win.configure(bg=CUTE_BG)
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=11);hero.pack(fill="x",padx=16,pady=(14,7))
        add_mascot(hero,size=72,opacity=0.14,quip_key="custom")
        tk.Label(hero,text="🗺 玩家交換點分類／群組",font=(FONT,22,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="X、Z、Q、G…是玩家自己的使用語言，不是官方身份。",font=(FONT,12),bg="#fffef9",fg="#536176").pack(anchor="w",pady=(2,0))
        ps=get_page_state("point_group_manager")
        search=tk.StringVar(value=str(ps.get("search") or ""))
        top=tk.Frame(win);top.pack(fill="x",padx=16,pady=6)
        tk.Label(top,text="搜尋",font=(FONT,12,"bold")).pack(side="left")
        tk.Entry(top,textvariable=search,font=(FONT,13),width=24).pack(side="left",padx=7)
        tk.Label(top,text="群組可填 X / Z / Q / G / 遠線…或自己的名字",font=(FONT,11),fg="#667085").pack(side="left",padx=10)
        cv=tk.Canvas(win,highlightthickness=0);sb=tk.Scrollbar(win,orient="vertical",command=cv.yview)
        body=tk.Frame(cv);wid=cv.create_window((0,0),window=body,anchor="nw")
        cv.configure(yscrollcommand=sb.set);cv.pack(side="left",fill="both",expand=True,padx=(16,0),pady=6)
        sb.pack(side="right",fill="y",padx=(0,10),pady=6)
        body.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        win.bind("<MouseWheel>",lambda e:(cv.yview_scroll(-3 if e.delta>0 else 3,"units"),"break")[1],add="+")
        points=[];seen=set()
        for rec in MASTER.points:
            iid=str(rec.get("id") or ""); nm=str(rec.get("name") or "")
            if iid and iid not in seen:seen.add(iid);points.append((iid,nm))
        vars_={}
        def render(*_):
            for w in body.winfo_children():w.destroy()
            tk.Label(body,text="交換點",font=(FONT,12,"bold"),bg="#dce6f1",width=30,anchor="w").grid(row=0,column=0,sticky="ew")
            tk.Label(body,text="你的群組",font=(FONT,12,"bold"),bg="#dce6f1",width=18,anchor="w").grid(row=0,column=1,sticky="ew")
            q=search.get().strip().lower()
            shown=[x for x in points if not q or q in x[1].lower() or q in x[0].lower() or q in str(groups.get(x[0],"")).lower()]
            for i,(iid,nm) in enumerate(shown,1):
                tk.Label(body,text=nm,font=(FONT,13,"bold"),anchor="w").grid(row=i,column=0,sticky="ew",padx=5,pady=3)
                v=vars_.setdefault(iid,tk.StringVar(value=str(groups.get(iid,""))))
                tk.Entry(body,textvariable=v,font=(FONT,13),width=18).grid(row=i,column=1,sticky="w",padx=5,pady=3)
            body.columnconfigure(0,weight=1)
        search.trace_add("write",render);render()
        try:win.after(100,lambda:cv.yview_moveto(float(ps.get("scroll",0) or 0)))
        except:pass
        def save_all():
            for iid,v in vars_.items():
                val=v.get().strip()
                if val:groups[iid]=val
                else:groups.pop(iid,None)
            save_player_point_groups(groups)
            save_page_state("point_group_manager",search=search.get(),scroll=cv.yview()[0] if cv.yview() else 0)
            messagebox.showinfo("交換點分類","✓ 已儲存。",parent=win)
        def close():
            save_page_state("point_group_manager",search=search.get(),scroll=cv.yview()[0] if cv.yview() else 0);win.destroy()
        foot=tk.Frame(win);foot.pack(fill="x",padx=16,pady=10)
        tk.Button(foot,text="✓ 儲存",font=(FONT,14,"bold"),command=save_all,padx=18,pady=7).pack(side="right")
        win.protocol("WM_DELETE_WINDOW",close)

    def open_inventory_manager(self,parent=None,rows=None,on_saved=None):
        inv=active_inventory()
        _opened_inv=dict(inv)
        catalog=load_inventory_catalog()
        _mode_label=inventory_mode_label()
        win=tk.Toplevel(parent or self);win.title(f"📦 {_mode_label}管理");smart_window(win,"inventory_manager","1220x800",1120,700);win.transient(parent or self)
        win.configure(bg=CUTE_BG)
        try: win.minsize(900,600)
        except Exception: pass
        hero=tk.Frame(win,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=18,pady=(12,6))
        add_mascot(hero,size=76,opacity=0.16,quip_key="inventory")
        tk.Label(hero,text=f"📦 {_mode_label}管理",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left")
        tk.Label(hero,text="　直接改總數，或用 ＋N／－N 快速調整。",
                 font=(FONT,12),bg="#fffef9",fg="#607168").pack(side="left",pady=(8,0))

        rulebar=tk.Frame(win,bg=CUTE_YELLOW_SOFT,padx=13,pady=8);rulebar.pack(fill="x",padx=18,pady=(0,6))
        _t5_enabled=tk.BooleanVar(value=bool(PLAYER_SETTINGS.get("t5_keep_cap_enabled",True)))
        _t5_cap=tk.StringVar(value=str(PLAYER_SETTINGS.get("t5_keep_cap",8)))
        tk.Label(rulebar,text="🧺 5階保留上限",font=(FONT,12,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f").pack(side="left")
        tk.Checkbutton(rulebar,text="啟用",variable=_t5_enabled,font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,selectcolor=CUTE_YELLOW_SOFT).pack(side="left",padx=(10,3))
        tk.Entry(rulebar,textvariable=_t5_cap,font=(FONT,13,"bold"),width=5,justify="right",relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).pack(side="left",padx=4)
        tk.Label(rulebar,text="個　｜　超過只標示待出售，不會自動扣庫存。",font=(FONT,11),bg=CUTE_YELLOW_SOFT,fg="#78622f").pack(side="left",padx=4)

        top=tk.Frame(win,bg=CUTE_GREEN_SOFT,padx=12,pady=7);top.pack(fill="x",padx=18,pady=5)
        _invps=get_page_state("inventory_manager")
        search=tk.StringVar(value=str(_invps.get("search") or ""))
        tk.Label(top,text="🔎 搜尋",font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).pack(side="left")
        tk.Entry(top,textvariable=search,font=(FONT,14),width=22,relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).pack(side="left",padx=(7,14))
        stage=tk.StringVar(value=str(_invps.get("stage") or "全部"))
        for st in ("全部","1","2","3","4","5","6","7"):
            tk.Radiobutton(top,text=("全部" if st=="全部" else f"{st}階"),variable=stage,value=st,
                           indicatoron=False,font=(FONT,11,"bold"),bg="#fffef9",selectcolor="#bfe2b9",
                           activebackground="#dcefd8",relief="flat",padx=9,pady=5).pack(side="left",padx=2)

        cv=tk.Canvas(win,bg="#fffef9",highlightthickness=0);sb=tk.Scrollbar(win,orient="vertical",command=cv.yview)
        body=tk.Frame(cv,bg="#fffef9");wid=cv.create_window((0,0),window=body,anchor="nw")
        cv.configure(yscrollcommand=sb.set);cv.pack(side="left",fill="both",expand=True,padx=(16,0),pady=6)
        sb.pack(side="right",fill="y",padx=(0,10),pady=6)
        body.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        win.bind("<MouseWheel>",lambda e:(cv.yview_scroll(-3 if e.delta>0 else 3,"units"),"break")[1],add="+")

        qtyvars={}
        delta_vars={}
        def norm_stage(x):
            try:return str(int(float(x)))
            except:return str(x or "")
        def parse_qty(v):
            t=str(v).strip().replace(",","")
            if t in ("","—","-"):return None
            n=int(t)
            if n<0:raise ValueError
            return n
        def save_silent():
            _before=dict(active_inventory())
            for iid,v in qtyvars.items():
                try:inv[iid]=parse_qty(v.get())
                except:pass
            save_active_inventory(inv)
            _names={str(x.get("item_id") or x.get("id") or ""):str(x.get("name") or x.get("item_name") or "") for x in catalog if isinstance(x,dict)}
            for iid,after in inv.items():
                before=_before.get(iid)
                if before is not None and after is not None and before!=after:
                    append_manual_inventory_ledger(iid,_names.get(iid,iid),before,after,"手動庫存調整")
            if callable(on_saved):
                try:on_saved()
                except:pass
        def adjust(iid,sign):
            dv=delta_vars[iid]
            try:n=int(dv.get().strip().replace(",","") or 0)
            except:
                messagebox.showerror("數字不正確","增減數量請輸入 0 以上整數。",parent=win);return
            if n<0:
                messagebox.showerror("數字不正確","增減數量請輸入 0 以上整數。",parent=win);return
            try:cur=parse_qty(qtyvars[iid].get())
            except:cur=None
            if cur is None:cur=0
            new=max(0,cur+sign*n)
            qtyvars[iid].set(str(new));inv[iid]=new;save_active_inventory(inv)
            if callable(on_saved):
                try:on_saved()
                except:pass

        def render(*_):
            for w in body.winfo_children():w.destroy()
            hdrs=("階段","官方名稱","玩家縮寫","目前庫存","待出售","快速數量","調整")
            widths=(6,24,13,10,10,9,16)
            for j,h in enumerate(hdrs):
                tk.Label(body,text=h,font=(FONT,12,"bold"),bg=CUTE_BLUE_SOFT,fg="#35566e",padx=5,pady=9,width=widths[j]).grid(row=0,column=j,sticky="ew",padx=1,pady=1)
            q=search.get().strip().lower(); st=stage.get()
            shown=[]
            for x in catalog:
                xs=norm_stage(x.get("stage"))
                if st!="全部" and xs!=st:continue
                hay=" ".join((str(x.get("name") or ""),str(x.get("alias") or ""),str(x.get("id") or ""))).lower()
                if q and q not in hay:continue
                shown.append(x)
            for i,x in enumerate(shown,1):
                iid=str(x.get("id")); name=str(x.get("name") or ""); al=player_item_alias(iid,"")
                rowbg="#fffef9" if i%2 else "#f4f9f2"
                qv=qtyvars.setdefault(iid,tk.StringVar(value=("—" if inv.get(iid) is None else str(inv.get(iid)))))
                dv=delta_vars.setdefault(iid,tk.StringVar(value="1"))
                tk.Label(body,text=f"{norm_stage(x.get('stage'))}階",font=(FONT,13,"bold"),bg=rowbg,fg=CUTE_GREEN,anchor="center",pady=7).grid(row=i,column=0,sticky="nsew",pady=1)
                tk.Label(body,text=name,font=(FONT,14,"bold"),bg=rowbg,anchor="w",pady=7).grid(row=i,column=1,sticky="nsew",padx=1,pady=1)
                tk.Label(body,text=al or "—",font=(FONT,13),bg=rowbg,fg="#607168",anchor="w",pady=7).grid(row=i,column=2,sticky="nsew",padx=1,pady=1)
                tk.Entry(body,textvariable=qv,font=(FONT,14,"bold"),width=10,justify="right",relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).grid(row=i,column=3,padx=5,pady=5)
                excess="—"
                if norm_stage(x.get("stage"))=="5" and _t5_enabled.get():
                    try:
                        cap=max(0,int(_t5_cap.get().strip() or 0))
                        cur=parse_qty(qv.get())
                        excess=str(max(0,(cur or 0)-cap)) if cur is not None else "—"
                    except: excess="?"
                tk.Label(body,text=excess,font=(FONT,13,"bold"),bg=rowbg,fg=("#b42318" if excess not in ("—","0") else "#607168"),anchor="center").grid(row=i,column=4,sticky="nsew",padx=1,pady=1)
                tk.Entry(body,textvariable=dv,font=(FONT,13),width=8,justify="right",relief="flat",highlightthickness=1,highlightbackground=CUTE_BORDER).grid(row=i,column=5,padx=5,pady=5)
                f=tk.Frame(body,bg=rowbg)
                f.grid(row=i,column=6,sticky="w",padx=3)
                tk.Button(f,text="－ N",font=(FONT,11,"bold"),bg="#edf2ec",relief="flat",command=lambda k=iid:adjust(k,-1),width=5).pack(side="left",padx=2,pady=4)
                tk.Button(f,text="＋ N",font=(FONT,11,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN,relief="flat",command=lambda k=iid:adjust(k,1),width=5).pack(side="left",padx=2,pady=4)
            body.columnconfigure(1,weight=1)
        search.trace_add("write",render);stage.trace_add("write",render)
        _t5_enabled.trace_add("write",render);_t5_cap.trace_add("write",render);render()
        try:win.after(120,lambda:cv.yview_moveto(float(_invps.get("scroll",0) or 0)))
        except:pass

        # V5.64：診斷入口獨立一列，不再跟「儲存」及說明文字搶 footer 寬度。
        diagbar=tk.Frame(win,bg=CUTE_BLUE_SOFT,padx=10,pady=7);diagbar.pack(fill="x",padx=18,pady=(6,0))
        tk.Label(diagbar,text="🧰 庫存工具",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,fg="#35566e").pack(side="left")
        tk.Button(diagbar,text="📒 異動帳本",font=(FONT,11,"bold"),
                  bg="#fffef9",relief="flat",command=lambda:self.open_inventory_ledger(win),padx=12,pady=5).pack(side="left",padx=3)
        tk.Button(diagbar,text="🔎 庫存稽核",font=(FONT,11,"bold"),
                  bg="#fffef9",relief="flat",command=lambda:self.open_inventory_audit(win),padx=12,pady=5).pack(side="left",padx=3)
        tk.Button(diagbar,text="🧮 庫存對帳",font=(FONT,11,"bold"),
                  bg="#fffef9",relief="flat",command=lambda:self.open_inventory_reconciliation(win),padx=12,pady=5).pack(side="left",padx=3)
        if load_inventory_mode()=="formal":
            tk.Button(diagbar,text="📍 建立庫存基準線",font=(FONT,11,"bold"),
                      bg="#fffef9",relief="flat",command=lambda:self.confirm_inventory_baseline(win),padx=12,pady=5).pack(side="left",padx=3)
        def _reset_test():
            if not messagebox.askyesno("重建測試庫存","把目前正式庫存複製成一份全新的測試庫存？\n\n只會清空測試帳，不會修改正式庫存。",parent=win):return
            ensure_test_inventory(reset=True)
            save_inventory_mode("test")
            messagebox.showinfo("測試庫存","已重新建立測試沙盒並切換為 🧪 測試模式。",parent=win)
        tk.Button(diagbar,text="🧪 重建測試沙盒",font=(FONT,11,"bold"),
                  bg="#fffef9",relief="flat",command=_reset_test,padx=12,pady=5).pack(side="left",padx=3)

        foot=tk.Frame(win,bg="#fffef9",padx=10,pady=8);foot.pack(fill="x",padx=18,pady=10)
        tk.Label(foot,text="💡「—」是未知／未管理，不等於 0",font=(FONT,11,"bold"),bg="#fffef9",fg="#667085").pack(side="left")
        def _save_t5_rule():
            try: cap=max(0,int(_t5_cap.get().strip() or 0))
            except:
                messagebox.showerror("5階保留上限","請填 0 以上整數。",parent=win);return False
            PLAYER_SETTINGS["t5_keep_cap_enabled"]=bool(_t5_enabled.get())
            PLAYER_SETTINGS["t5_keep_cap"]=cap
            save_player_settings(PLAYER_SETTINGS)
            return True
        def save_all():
            if not _save_t5_rule():return
            bad=[]
            for iid,v in qtyvars.items():
                try:inv[iid]=parse_qty(v.get())
                except:bad.append(iid)
            if bad:
                messagebox.showerror("庫存數字不正確","庫存請填 0 以上整數，或留空／— 表示未知。",parent=win);return
            save_active_inventory(inv)
            if callable(on_saved):
                try:on_saved()
                except:pass
            messagebox.showinfo("庫存管理","✓ 庫存已儲存。",parent=win)
        tk.Button(foot,text="✓ 儲存全部庫存",font=(FONT,14,"bold"),bg=CUTE_GREEN,fg="white",activebackground="#0f5b31",activeforeground="white",relief="flat",command=save_all,padx=20,pady=8).pack(side="right")
        def _close_inventory():
            _save_t5_rule()
            save_page_state("inventory_manager",search=search.get(),stage=stage.get(),
                            scroll=cv.yview()[0] if cv.yview() else 0);win.destroy()
        win.protocol("WM_DELETE_WINDOW",_close_inventory)


    def confirm_inventory_baseline(self,parent=None):
        inv=load_inventory()
        ok=messagebox.askyesno("建立庫存基準線",
            f"請先逐項確認目前庫存與遊戲內一致。\n\n目前共有 {len(inv)} 個庫存品項。\n\n"
            "建立後，現在庫存會成為正式可信起點；舊帳完整保留，但不再參與新帳對帳。\n"
            "之後完成、撤銷、手動修改都從這條基準線往後驗算。\n\n"
            "確定現在庫存已全部人工確認正確？",parent=parent)
        if not ok:return
        base=create_inventory_baseline()
        messagebox.showinfo("基準線已建立",
            f"正式庫存新紀元已建立。\n\n時間：{base['created_at']}\n品項：{base['item_count']}\n"
            f"舊帳截止索引：{base['ledger_index']}\n\n舊歷史沒有刪除。",parent=parent)

    def open_inventory_audit(self,parent=None):
        snap=inventory_audit_snapshot(self.state_data)
        _base=load_inventory_baseline()
        win=tk.Toplevel(parent or self);win.title("🔎 庫存稽核｜只讀")
        smart_window(win,"inventory_audit","1000x700",920,620);win.transient(parent or self)
        tk.Label(win,text="🔎 庫存犯罪現場稽核",font=(FONT,22,"bold")).pack(pady=(14,4))
        tk.Label(win,text="這頁只讀，不會修庫存、不會撤銷、不會補帳。",font=(FONT,11),fg="#8a4b08").pack()
        box=tk.Text(win,font=(FONT,11),wrap="word")
        box.pack(fill="both",expand=True,padx=16,pady=12)
        lines=[
            f"目前模式：{snap['mode_label']}",
            f"庫存檔：{snap['inventory_path']}",
            f"帳本檔：{snap['ledger_path']}",
            f"庫存品項數：{snap['inventory_items']}",
            f"帳本筆數：{snap['ledger_rows']}",
            f"目前完成站數：{len(snap['completed_stations'])}",
            f"完成但找不到有效帳本交易：{len(snap['orphan_completed'])}",
            f"重複交易 ID：{len(snap['duplicate_tx_ids'])}",
            f"正式基準線：{(_base.get('created_at') if _base else '尚未建立') if snap['mode']=='formal' else '測試模式不使用正式基準線'}",
            "",
            "=== 完成但帳本對不上 ==="
        ]
        if snap["orphan_completed"]:
            for x in snap["orphan_completed"]:
                lines.append(f"- {x.get('route')}｜{x.get('point')}｜{x.get('input')} x{x.get('times')}｜tx={x.get('tx_id') or '(空白)'}｜{x.get('reason','帳本無此交易')}")
        else:
            lines.append("（沒有）")
        lines += ["","=== 目前完成站 ==="]
        for x in snap["completed_stations"]:
            lines.append(f"- {x.get('route')}｜{x.get('point')}｜{x.get('input')} x{x.get('times')}｜tx={x.get('tx_id') or '(空白)'}")
        box.insert("1.0","\n".join(lines));box.configure(state="disabled")

    def open_inventory_ledger(self,parent=None):
        """V5.64｜玩家可見庫存帳本。"""
        rows=list(reversed(load_inventory_ledger()))
        win=tk.Toplevel(parent or self);win.title("📒 庫存異動帳本")
        smart_window(win,"inventory_ledger","1220x760",1100,680);win.transient(parent or self)
        win.grid_rowconfigure(3,weight=1);win.grid_columnconfigure(0,weight=1)
        tk.Label(win,text="📒 庫存異動帳本",font=(FONT,24,"bold")).grid(row=0,column=0,pady=(14,2))
        tk.Label(win,text=f"{inventory_mode_label()}｜帳本：{active_ledger_path()}　｜　目前 {len(rows)} 筆",
                 font=(FONT,9),fg="#6b7280").grid(row=1,column=0,pady=(0,5))
        top=tk.Frame(win);top.grid(row=2,column=0,sticky="ew",padx=16,pady=5)
        qv=tk.StringVar();only_active=tk.BooleanVar(value=False)
        tk.Label(top,text="搜尋",font=(FONT,11,"bold")).pack(side="left")
        tk.Entry(top,textvariable=qv,font=(FONT,12),width=28).pack(side="left",padx=6)
        tk.Checkbutton(top,text="只看未撤銷交易",variable=only_active,font=(FONT,11)).pack(side="left",padx=10)

        cols=("time","action","route","point","input","input_delta","output","output_delta","state")
        body,tree=make_reliable_tree(
            win,cols,
            {"time":"時間","action":"類型","route":"路線","point":"交換點","input":"投入/品項",
             "input_delta":"異動","output":"取得物品","output_delta":"異動","state":"狀態"},
            {"time":145,"action":105,"route":85,"point":115,"input":170,"input_delta":75,
             "output":160,"output_delta":75,"state":75})
        body.grid(row=3,column=0,sticky="nsew",padx=16,pady=(4,14))
        def fd(v):
            try:return f"{int(v or 0):+,}"
            except:return str(v or "")
        def render(*_):
            tree.delete(*tree.get_children())
            q=qv.get().strip().lower()
            for r in rows:
                if only_active.get() and r.get("reversed"):continue
                hay=" ".join(str(r.get(k) or "") for k in ("action","route","point","input","output","tx_id")).lower()
                if q and q not in hay:continue
                tree.insert("","end",values=(r.get("time",""),r.get("action",""),r.get("route",""),r.get("point",""),
                    r.get("input",""),fd(r.get("input_delta")),r.get("output",""),fd(r.get("output_delta")),
                    "已撤銷" if r.get("reversed") else "有效"))
        qv.trace_add("write",render);only_active.trace_add("write",render);render()

    def open_inventory_reconciliation(self,parent=None):
        rows=build_inventory_reconciliation()
        win=tk.Toplevel(parent or self);win.title("🧮 庫存對帳")
        smart_window(win,"inventory_reconciliation","1100x760",1000,680);win.transient(parent or self)
        tk.Label(win,text="🧮 庫存對帳",font=(FONT,24,"bold")).pack(pady=(14,2))
        bad=[x for x in rows if x.get("diff") not in (None,0)]
        unver=[x for x in rows if not x.get("verifiable")]
        _base=load_inventory_baseline()
        if load_inventory_mode()=="formal" and _base:
            tk.Label(win,text=f"📍 正式基準：{_base.get('created_at')}｜只驗算基準後異動",
                     font=(FONT,10,"bold"),fg="#536176").pack(pady=(0,3))
        tk.Label(win,text=f"可追溯品項 {len(rows)-len(unver)}｜有差異 {len(bad)}｜無可靠起始錨點 {len(unver)}",
                 font=(FONT,11,"bold")).pack(pady=(0,3))
        tk.Label(win,text="只讀：帳本推算 ≠ 實際庫存時只標示，不會自動改數字。雙擊任一品項可看完整異動時間線。",
                 font=(FONT,10),fg="#8a4b08").pack(pady=(0,8))
        cols=("name","expected","current","diff","status")
        shell,tree=make_reliable_tree(
            win,cols,
            {"name":"品項","expected":"帳本推算","current":"實際庫存","diff":"差異","status":"狀態"},
            {"name":300,"expected":130,"current":130,"diff":110,"status":240},
            height=27)
        shell.pack(fill="both",expand=True,padx=16,pady=(4,14))
        if not rows:
            tree.insert("","end",values=("（沒有可對帳資料）","—","—","—","帳本尚無可用錨點"))
        for x in rows:
            if not x.get("verifiable"):
                status="⚪ 無起始錨點，無法驗證";exp="—";diff="—"
            elif x.get("diff")==0:
                status="✓ 對得上";exp=f"{x['expected']:,}";diff="0"
            else:
                status="⚠ 有差異";exp=f"{x['expected']:,}";diff=f"{x['diff']:+,}"
            cur="—" if x.get("current") is None else f"{x['current']:,}"
            iid=tree.insert("","end",values=(x.get("name"),exp,cur,diff,status))
            tree.set(iid,"name",x.get("name"))
            tree.item(iid,tags=(str(x.get("item_id") or ""),))
        def open_selected_timeline(_event=None):
            sel=tree.selection()
            if not sel:return
            vals=tree.item(sel[0],"values")
            tags=tree.item(sel[0],"tags")
            if not tags:return
            self.open_inventory_item_timeline(tags[0],vals[0],win)
        tree.bind("<Double-1>",open_selected_timeline)


    def open_inventory_item_timeline(self,item_id,item_name,parent=None):
        led=load_inventory_ledger()
        inv=active_inventory()
        rows=[]
        for idx,r in enumerate(led):
            if not isinstance(r,dict): continue
            for side in ("input","output"):
                if str(r.get(side+"_id") or "")!=str(item_id): continue
                try: delta=int(r.get(side+"_delta") or 0)
                except: delta=0
                rows.append((idx,r,side,delta))
        win=tk.Toplevel(parent or self);win.title(f"🧾 品項異動時間線｜{item_name}")
        smart_window(win,"inventory_item_timeline","1180x760",1050,680);win.transient(parent or self)
        tk.Label(win,text=f"🧾 {item_name}",font=(FONT,22,"bold")).pack(pady=(12,2))
        cur=_inv_int(inv.get(str(item_id)))
        tk.Label(win,text=f"{inventory_mode_label()}｜目前實際庫存：{'—' if cur is None else format(cur,',')}　｜　帳本相關紀錄：{len(rows)}",
                 font=(FONT,11,"bold")).pack(pady=(0,4))
        tk.Label(win,text="只讀。時間線保留帳本原貌；⚠ 表示這筆紀錄可能跨越舊版快照撤銷時期，供人工判讀。",
                 font=(FONT,10),fg="#8a4b08").pack(pady=(0,8))
        cols=("time","action","route","point","before","delta","after","undo","tx")
        shell,tree=make_reliable_tree(win,cols,
            {"time":"時間","action":"類型","route":"路線","point":"交換點","before":"交易前",
             "delta":"異動","after":"交易後","undo":"撤銷/狀態","tx":"TX"},
            {"time":150,"action":105,"route":80,"point":120,"before":85,"delta":75,"after":85,"undo":155,"tx":235},height=25)
        shell.pack(fill="both",expand=True,padx=14,pady=(4,14))
        def fmt(v):
            if v is None:return "—"
            try:return f"{int(v):,}"
            except:return str(v)
        for _,r,side,delta in rows:
            rev=bool(r.get("reversed"))
            revtxt=""
            if rev:
                revtxt="已撤銷"
                if r.get("reversed_time"): revtxt+=f"｜{r.get('reversed_time')}"
            elif str(r.get("action") or "").startswith("手動"):
                revtxt="手動調整"
            else:
                revtxt="有效"
            tree.insert("","end",values=(r.get("time",""),r.get("action",""),r.get("route",""),r.get("point",""),
                fmt(r.get(side+"_before")),f"{delta:+,}",fmt(r.get(side+"_after")),revtxt,r.get("tx_id","")))


    def open_layout_assigner(self):
        LayoutAssigner(self, shared_file("layout_assignments.json", fallback_local=False))

    def identity_test(self):
        q=simpledialog.askstring("名稱／別名測試","輸入品項或點位名稱／縮寫：")
        if not q: return
        i=MASTER.resolve_item(q)
        p=MASTER.resolve_point(q)
        lines=[]
        if i.get("matched"):
            r=i["record"]
            lines.append(f"品項：{i['id']}｜{r['name']}｜縮寫 {r.get('abbr','')}｜{i['via']}")
        if p.get("matched"):
            r=p["record"]
            lines.append(f"點位：{p['id']}｜{r['name']}｜分區 {r.get('zone','')}｜{p['via']}")
        if not lines:
            lines=["找不到已知身份。\n正式OCR版會把它送去人工確認，而不是亂猜。"]
        messagebox.showinfo("解析結果","\n".join(lines))

    def _learn_alias(self, object_type, raw_alias, stage=None, parent=None):
        raw_alias=str(raw_alias or "").strip()
        if not raw_alias:
            messagebox.showwarning("沒有OCR文字","這格沒有可學習的OCR名稱。",parent=parent or self); return
        label="點位" if object_type=="點位" else "品項"

        candidates=[]
        records=MASTER.points if label=="點位" else MASTER.items
        for rec in records:
            if label=="品項" and stage not in (None,"") and rec.get("stage") not in (stage,None,""):
                continue
            name=rec.get("name","")
            ratio=difflib.SequenceMatcher(None,raw_alias,name).ratio()
            candidates.append((ratio,rec))
        candidates.sort(key=lambda x:x[0],reverse=True)
        candidates=candidates[:5]

        win=tk.Toplevel(parent or self); win.title(f"🧠 學習{label}｜大字差異確認")
        smart_window(win,"alias_learning","900x720",820,620); win.transient(parent or self); win.grab_set()

        tk.Label(win,text="OCR 讀到",font=(FONT,17,"bold")).pack(pady=(18,2))
        tk.Label(win,text=raw_alias,font=(FONT,30,"bold"),wraplength=820).pack(pady=(0,14))

        best=candidates[0][1] if candidates else None
        if best:
            name=best.get("name","")
            tk.Label(win,text="最可能正式名稱",font=(FONT,17,"bold")).pack()
            tk.Label(win,text=name,font=(FONT,30,"bold"),wraplength=820).pack(pady=(2,8))

            sm=difflib.SequenceMatcher(None,raw_alias,name)
            left=[]; right=[]
            for tag,i1,i2,j1,j2 in sm.get_opcodes():
                if tag!="equal":
                    left.append(raw_alias[i1:i2] or "∅"); right.append(name[j1:j2] or "∅")
            tk.Label(win,text="差異字（強制超大）",font=(FONT,17,"bold")).pack(pady=(8,0))
            tk.Label(win,text=f"{' / '.join(left) or '—'}   ⇄   {' / '.join(right) or '—'}",
                     font=(FONT,68,"bold"),wraplength=840).pack(pady=(0,14))

        btns=tk.Frame(win); btns.pack(fill="x",padx=25,pady=5)
        def choose(rec):
            # 留下「推薦第一名 vs 玩家最後選擇」犯罪紀錄，之後可直接分析排序命中率。
            first=candidates[0][1] if candidates else None
            first_ratio=candidates[0][0] if candidates else None
            append_candidate_log({
                "object_type":label,
                "ocr_raw":raw_alias,
                "stage":stage,
                "first_candidate_id": first.get("id") if first else None,
                "first_candidate_name": first.get("name") if first else None,
                "first_candidate_similarity": round(first_ratio,4) if first_ratio is not None else None,
                "chosen_id":rec.get("id"),
                "chosen_name":rec.get("name"),
                "first_candidate_was_chosen": bool(first and first.get("id")==rec.get("id"))
            })
            ok,msg=MASTER.add_user_alias(label,raw_alias,rec["id"])
            if not ok:
                messagebox.showerror("無法學習",msg,parent=win); return
            win.grab_release(); win.destroy()
            # 學習成功後立即用同一份OCR資料重新解析，
            # 讓原本的學習按鈕直接變成「✓ 已學習」，並更新候選狀態。
            if parent and parent.winfo_exists():
                try:
                    self._reparse_last_ocr(parent)
                except Exception:
                    parent.after(50,lambda:(parent.lift(),parent.focus_force()))
        if best:
            tk.Button(btns,text=f"✓ 就是它：{best.get('name')}",font=(FONT,18,"bold"),
                      command=lambda r=best:choose(r),pady=10).pack(fill="x",pady=5)

        tk.Label(win,text="其他同段位候選",font=(FONT,15,"bold")).pack(pady=(10,4))
        for ratio,rec in candidates[1:]:
            tk.Button(win,text=f"{rec.get('name')}   ({ratio:.0%})",font=(FONT,15),
                      command=lambda r=rec:choose(r),pady=6).pack(fill="x",padx=70,pady=3)

        def manual():
            target=simpledialog.askstring("自己選","輸入正式名稱或縮寫：",parent=win)
            if not target:return
            resolved=MASTER.resolve_point(target) if label=="點位" else MASTER.resolve_item(target)
            if not resolved.get("matched"):
                messagebox.showerror("找不到正式身份",f"資料庫找不到：{target}",parent=win); return
            choose(resolved["record"])
        tk.Button(win,text="✎ 都不是，我自己輸入",font=(FONT,14),command=manual).pack(pady=14)
        def close():
            win.grab_release(); win.destroy()
            if parent and parent.winfo_exists():
                parent.after(50,lambda:(parent.lift(),parent.focus_force()))
        win.protocol("WM_DELETE_WINDOW",close)


    def _reparse_last_ocr(self, oldwin=None):
        path=os.path.join(BASE,"data","ocr_debug.json")
        if not os.path.exists(path):
            messagebox.showwarning("找不到OCR結果","請先匯入截圖跑一次OCR。")
            return
        try:
            with open(path,"r",encoding="utf-8") as f: result=json.load(f)
            rows=PARSER.parse_ocr_result(result)
            out=os.path.join(BASE,"data","ocr_structured_debug.json")
            with open(out,"w",encoding="utf-8") as f:
                json.dump(rows,f,ensure_ascii=False,indent=2)
            if oldwin and oldwin.winfo_exists():
                try:
                    # 找出舊視窗中的 Canvas，保存最大化/尺寸與捲動位置。
                    st=oldwin.state()
                    if not hasattr(self,"_ocr_review_ui_state"):
                        self._ocr_review_ui_state={}
                    self._ocr_review_ui_state["state"]=st
                    if st=="normal":
                        self._ocr_review_ui_state["geometry"]=oldwin.geometry()
                    def _find_canvas(w):
                        for child in w.winfo_children():
                            if isinstance(child,tk.Canvas):
                                return child
                            found=_find_canvas(child)
                            if found:return found
                        return None
                    c=_find_canvas(oldwin)
                    if c:
                        self._ocr_review_ui_state["scroll_fraction"]=c.yview()[0]
                except Exception:
                    pass
                oldwin.destroy()
            self.show_ocr_review(rows)
        except Exception as e:
            messagebox.showerror("重新解析失敗",str(e))

    def _ratio_needs_human(self, r):
        if not r.get("exchange_ratio_needs_ocr"):
            return False
        return not bool(r.get("exchange_ratio_auto_accepted") or r.get("_ratio_confirmed_current_batch"))

    def _after_ratio_fix(self, rows, parent=None, canvas=None, ratio_text=""):
        """按人工比例後立即給可見回音，再依剩餘筆數導航。"""
        try:
            if canvas is not None and hasattr(self,"_ocr_review_ui_state"):
                self._ocr_review_ui_state["scroll_fraction"]=canvas.yview()[0]
        except Exception: pass
        remaining=sum(1 for x in (rows or []) if self._ratio_needs_human(x))
        msg=(f"✓ 交換比例 {ratio_text} 已確認" if ratio_text else "✓ 交換比例已確認")
        msg += ("\n比例待處理：0｜全部完成！" if remaining<=0 else f"\n比例待處理：{remaining}")

        # 直接蓋在當前頁面，不依賴獨立toast視窗
        banner=None
        try:
            if parent is not None and parent.winfo_exists():
                banner=tk.Frame(parent,bd=5,relief="ridge")
                banner.place(relx=.5,rely=.18,anchor="center")
                tk.Label(banner,text=msg,font=(FONT,24,"bold"),padx=36,pady=22).pack()
                parent.update_idletasks()
        except Exception: banner=None

        def go():
            try:
                if banner is not None and banner.winfo_exists(): banner.destroy()
            except Exception: pass
            try:
                if parent is not None and parent.winfo_exists(): parent.destroy()
            except Exception: pass
            self._ocr_review_filter="全部" if remaining<=0 else "比例待處理"
            if remaining<=0 and hasattr(self,"_ocr_review_ui_state"):
                self._ocr_review_ui_state["scroll_fraction"]=0.0
            self.show_ocr_review(rows or [])
        self.after(900,go)

    def _show_original_evidence(self, rr, row_index, parent=None, rows=None):
        """V5.22：精準證物鏈。只開這筆OCR實際使用的 ROW / POINT / INPUT / OUTPUT 切片。"""
        parent=parent or self
        win=tk.Toplevel(parent)
        win.title(f"🖼 原始切片證物｜第 {row_index} 筆")
        smart_window(win,"ocr_evidence_window","1180x860",980,650)
        win.transient(parent);win.configure(bg=CUTE_BG)
        win.grab_set()
        def _close_evidence():
            try:
                self._evidence_window_state={"geometry":win.geometry(),"state":win.state()}
            except Exception:
                self._evidence_window_state={}
            save_evidence_window_state(win)
            try: win.grab_release()
            except Exception: pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW",_close_evidence)

        hero=tk.Frame(win,bg="#fffef9",padx=20,pady=12);hero.pack(fill="x",padx=18,pady=(14,6))
        add_mascot(hero,size=74,opacity=0.14,quip_key="evidence")
        tk.Label(hero,text=f"🖼 第 {row_index} 筆｜原始切片證物",
                 font=(FONT,26,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="上方是程式目前採用的文字；下方黑底區是這筆真正的原始圖片。",
                 font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))

        confs=rr.get("ocr_field_confidence") or {}
        lowfield=""
        if confs:
            lowfield=min(confs,key=lambda k:confs.get(k,1))
        conf_text=" ｜ ".join(f"{k} {confs.get(k):.3f}" for k in ("POINT","INPUT","OUTPUT") if k in confs)
        if conf_text:
            tk.Label(win,text=f"OCR欄位信心：{conf_text}   ｜ 最低：{lowfield}",
                     font=(FONT,13,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f",padx=12,pady=6).pack(fill="x",padx=22,pady=(0,6))

        # 大字顯示OCR文字與正式匹配。
        top=tk.Frame(win,bg=CUTE_GREEN_SOFT,padx=14,pady=10); top.pack(fill="x",padx=22,pady=4)
        summary_rows=[
            ("POINT", rr.get("point_name") or "？"),
            ("INPUT", rr.get("input_raw") or rr.get("input_name") or "？"),
            ("OUTPUT", rr.get("output_raw") or rr.get("output_name") or "？"),
            ("交換比例", rr.get("exchange_ratio") or "⚠ OUTPUT數量未讀到"),
            ("INPUT數量OCR", (
                f"{(rr.get('exchange_ratio_source') or {}).get('input_token')} ｜ 信心 {(rr.get('exchange_ratio_source') or {}).get('input_confidence',0)}"
                if (rr.get('exchange_ratio_source') or {}).get('input_token') else "固定1或未讀到"
            )),
            ("OUTPUT數量OCR", (
                f"{(rr.get('exchange_ratio_source') or {}).get('output_token')} ｜ 信心 {(rr.get('exchange_ratio_source') or {}).get('output_confidence',0)}"
                if (rr.get('exchange_ratio_source') or {}).get('output_token') else "未讀到"
            ))
        ]
        for i,(lab,val) in enumerate(summary_rows):
            tk.Label(top,text=lab,font=(FONT,14,"bold"),width=12,anchor="w",bg=CUTE_GREEN_SOFT,fg="#607168").grid(row=i,column=0,sticky="w",pady=3)
            tk.Label(top,text=val,font=(FONT,20,"bold"),anchor="w",bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).grid(row=i,column=1,sticky="w",pady=3)

        if rr.get("exchange_ratio_needs_ocr"):
            qbar=tk.Frame(win,bg=CUTE_BLUE_SOFT,padx=12,pady=8); qbar.pack(fill="x",padx=22,pady=(4,8))
            tk.Label(qbar,text=("照原圖確認投入與產出數量：" if rr.get("exchange_ratio_manual_both") else "原圖看到 OUTPUT 數量後直接點："),
                     font=(FONT,14,"bold"),bg=CUTE_BLUE_SOFT).pack(side="left",padx=(0,8))
            def apply_qty(n):
                rr["exchange_ratio_input"]=1
                rr["exchange_ratio_output"]=int(n)
                rr["output_quantity"]=int(n)
                rr["exchange_ratio"]=f"1 : {int(n)}"
                rr["_ratio_confirmed_current_batch"]=True;rr["exchange_ratio_auto_accepted"]=True
                save_ratio_override(rr,int(n))
                try: win.grab_release()
                except: pass
                try:self._evidence_window_state={"geometry":win.geometry(),"state":win.state()}
                except Exception:pass
                save_evidence_window_state(win)
                win.destroy()
                if rows is not None:
                    self._after_ratio_fix(rows,parent=parent,ratio_text=f"1:{int(n)}")
            if rr.get("exchange_ratio_manual_both"):
                def apply_both():
                    iq=simpledialog.askinteger("投入數量","每次交換要交出幾個？",parent=win,minvalue=1,maxvalue=99999)
                    if iq is None:return
                    oq=simpledialog.askinteger("產出數量","每次交換會得到幾個？",parent=win,minvalue=1,maxvalue=99999)
                    if oq is None:return
                    rr["exchange_ratio_input"]=iq;rr["input_quantity"]=iq
                    rr["exchange_ratio_output"]=oq;rr["output_quantity"]=oq
                    rr["exchange_ratio"]=f"{iq} : {oq}（本輪人工確認）";rr["_ratio_confirmed_current_batch"]=True;rr["exchange_ratio_auto_accepted"]=True
                    save_ratio_override(rr,oq,iq)
                    _close_evidence()
                    if rows is not None:self._after_ratio_fix(rows,parent=parent,ratio_text=f"{iq}:{oq}")
                tk.Button(qbar,text="✎ 輸入完整比例",font=(FONT,13,"bold"),command=apply_both,padx=10,pady=4).pack(side="left")
            for n in ([] if rr.get("exchange_ratio_manual_both") else range(1,6)):
                tk.Button(qbar,text=str(n),font=(FONT,20,"bold"),
                          command=lambda v=n:apply_qty(v),width=3,pady=4).pack(side="left",padx=4)
            def custom_qty():
                v=simpledialog.askinteger("OUTPUT數量","輸入原圖看到的OUTPUT數量：",
                                          parent=win,minvalue=1,maxvalue=999)
                if v is not None: apply_qty(v)
            if not rr.get("exchange_ratio_manual_both"):
                tk.Button(qbar,text="其他…",font=(FONT,14,"bold"),
                          command=custom_qty,padx=10,pady=6).pack(side="left",padx=6)

        holder=tk.Frame(win,bg=CUTE_BG); holder.pack(fill="both",expand=True,padx=18,pady=6)
        canvas=tk.Canvas(holder,bg="#202020",highlightthickness=0)
        vbar=tk.Scrollbar(holder,orient="vertical",command=canvas.yview)
        hbar=tk.Scrollbar(holder,orient="horizontal",command=canvas.xview)
        inner=tk.Frame(canvas,bg="#202020")
        inner.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0),window=inner,anchor="nw")
        canvas.configure(yscrollcommand=vbar.set,xscrollcommand=hbar.set)
        canvas.grid(row=0,column=0,sticky="nsew")
        vbar.grid(row=0,column=1,sticky="ns")
        hbar.grid(row=1,column=0,sticky="ew")
        holder.rowconfigure(0,weight=1); holder.columnconfigure(0,weight=1)
        win._evidence_imgs=[]

        evidence=rr.get("evidence") or {}
        if not isinstance(evidence,dict):
            evidence={}

        # V5.47：不要求重跑OCR。舊批次若證物欄位在列本身，或完整debug仍保留，就直接沿用。
        for _ek in ("row_image","point_image","input_image","input_qty_image","output_image","output_qty_image"):
            if not evidence.get(_ek) and rr.get(_ek):
                evidence[_ek]=rr.get(_ek)

        def _same_evidence_identity(a,b):
            # source_file + row_index 最精準；不足時退回交換身份。
            if str(a.get("source_file") or "") and str(a.get("row_index") or ""):
                if (str(a.get("source_file") or "")==str(b.get("source_file") or "") and
                    str(a.get("row_index") or "")==str(b.get("row_index") or "")):
                    return True
            return (
                str(a.get("point_id") or "")==str(b.get("point_id") or "") and
                str(a.get("input_item_id") or "")==str(b.get("input_item_id") or "") and
                str(a.get("output_item_id") or "")==str(b.get("output_item_id") or "")
            )

        if not any(evidence.get(k) for k in ("row_image","point_image","input_image","output_image")):
            for _fp in (CURRENT_BATCH_PATH, os.path.join(BASE,"data","ocr_structured_debug.json")):
                try:
                    if not os.path.exists(_fp): continue
                    with open(_fp,"r",encoding="utf-8") as _f:
                        _all=json.load(_f)
                    if isinstance(_all,dict):
                        _all=_all.get("rows") or _all.get("usable") or _all.get("data") or []
                    for _cand in (_all if isinstance(_all,list) else []):
                        if not isinstance(_cand,dict) or not _same_evidence_identity(rr,_cand): continue
                        _ev=_cand.get("evidence") if isinstance(_cand.get("evidence"),dict) else {}
                        for _ek in ("row_image","point_image","input_image","input_qty_image","output_image","output_qty_image"):
                            if not evidence.get(_ek):
                                evidence[_ek]=_ev.get(_ek) or _cand.get(_ek)
                        if any(evidence.get(k) for k in ("row_image","point_image","input_image","output_image")):
                            break
                except Exception:
                    pass

        # V5.47：切片路徑失聯時，至少先把「這筆來自哪張原始截圖」找回來。
        # source_file 可能是絕對路徑、舊版本路徑或只剩檔名。
        source_candidates=[]
        _src=str(rr.get("source_file") or "")
        if _src:
            source_candidates.append(_src)

        def resolve_path(fp,preferred_dir=""):
            if not fp:return ""
            fp=os.path.normpath(str(fp))
            bn=os.path.basename(fp)
            # 一旦 ROW 已定位，所有欄位證物都必須來自同一個 debug 資料夾。
            # 不可讓同名 POINT／INPUT／OUTPUT 各自在不同版本中被搜尋到。
            if preferred_dir and bn:
                same_batch=os.path.join(preferred_dir,bn)
                return same_batch if os.path.isfile(same_batch) else ""
            if os.path.isfile(fp): return fp
            # 若是上一版OCR結果的絕對路徑，把 \\data\\... 後半段接到目前版本。
            low=fp.lower()
            token=os.sep+"data"+os.sep
            pos=low.rfind(token)
            if pos>=0:
                alt=os.path.join(BASE,fp[pos+1:])
                if os.path.isfile(alt): return alt
            if bn:
                roots=[str(rr.get("_legacy_scan_root") or ""),
                       os.path.dirname(str(rr.get("_legacy_json_path") or "")),
                       SHARED,BASE,os.path.dirname(CURRENT_BATCH_PATH),os.path.dirname(BASE),os.path.dirname(SHARED)]
                seen_roots=set()
                for base_dir in roots:
                    base_dir=os.path.abspath(base_dir)
                    if base_dir in seen_roots or not os.path.isdir(base_dir):continue
                    seen_roots.add(base_dir)
                    try:
                        for dp,_,fs in os.walk(base_dir):
                            if bn in fs:return os.path.join(dp,bn)
                    except Exception:pass
            return ""

        specs=[
            ("整列 ROW","row_image"),
            ("POINT","point_image"),
            ("INPUT","input_image"),
            ("INPUT_QTY 定位證物","input_qty_image"),
            ("OUTPUT","output_image"),
            ("OUTPUT_QTY 定位證物","output_qty_image"),
            ("原始截圖","__source_file__"),
        ]
        resolved_row=resolve_path(evidence.get("row_image"))
        evidence_dir=os.path.dirname(resolved_row) if resolved_row else ""
        opened=0
        for title,key in specs:
            if key=="__source_file__":
                fp=resolve_path(_src)
            elif key=="row_image":
                fp=resolved_row
            else:
                fp=resolve_path(evidence.get(key),evidence_dir)
            if not fp: continue
            try:
                im=Image.open(fp).convert("RGB")
                # 證物只做等比例顯示放大/縮小，不銳化、不改內容。
                # 小切片最多放大2.5倍；超寬ROW則先適合約1050px寬。
                if im.width>1050:
                    scale=1050/im.width
                else:
                    scale=min(2.5,1050/max(1,im.width))
                scale=max(0.3,scale)
                disp=im.resize((max(1,int(im.width*scale)),max(1,int(im.height*scale))),Image.Resampling.LANCZOS)
                ph=ImageTk.PhotoImage(disp)
                win._evidence_imgs.append(ph)
                lab=f"{title}"
                if title in confs:
                    lab+=f"   OCR信心 {confs[title]:.3f}"
                tk.Label(inner,text=lab,font=(FONT,17,"bold"),
                         bg="#202020",fg="white",anchor="w").pack(fill="x",padx=10,pady=(12,4))
                tk.Label(inner,image=ph,bg="#202020").pack(anchor="w",padx=10)
                opened+=1
            except Exception as e:
                tk.Label(inner,text=f"{title} 無法開啟：{e}",
                         font=(FONT,13),bg="#202020",fg="white").pack(anchor="w",padx=10,pady=8)

        if not opened:
            tk.Label(inner,
                text="⚠ 目前找不到這筆的既有原始切片或原始截圖。\n\n"
                     f"資料記錄來源：{os.path.basename(_src) if _src else '未記錄 source_file'}\n"
                     "不需要重跑OCR；目前問題是舊批次只留下資料記錄、影像檔沒有在可搜尋位置。",
                font=(FONT,18,"bold"),justify="left",wraplength=1000,
                bg="#202020",fg="white").pack(padx=30,pady=70)

        def wheel(e):
            if getattr(e,"delta",0): canvas.yview_scroll(-3 if e.delta>0 else 3,"units")
            return "break"
        win.bind("<MouseWheel>",wheel,add="+")
        def close():
            try:win.grab_release()
            except:pass
            win.destroy()
            try:parent.after(40,lambda:(parent.lift(),parent.focus_force()))
            except:pass
        tk.Button(win,text="關閉證物視窗",font=(FONT,14,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  activebackground="#0f5b31",activeforeground="white",command=close,padx=24,pady=8).pack(pady=9)
        win.protocol("WM_DELETE_WINDOW",close)

    def generate_ocr_report(self, rows, parent_win):
        """V6.1：生成 OCR 識識失敗報告，用於數據徵集"""
        import datetime
        
        # 找出有問題的樣本（低信心度或有問題標記）
        failed_rows = []
        for r in rows:
            issues = r.get("issues", [])
            low_confidence = r.get("ocr_min_confidence", 1.0) < 0.85
            has_issues = bool(issues)
            if low_confidence or has_issues:
                failed_rows.append(r)
        
        if not failed_rows:
            messagebox.showinfo(
                "沒有失敗樣本",
                "本次 OCR 識識都很好，沒有需要報告的樣本。\n\n信心度都很高，且沒有識識問題。",
                parent=parent_win
            )
            return
        
        # 生成報告文字
        report_lines = [
            "## OCR 識識報告",
            f"生成時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"總樣本數：{len(rows)}",
            f"失敗樣本數：{len(failed_rows)}",
            "",
            "### 失敗樣本詳情",
            ""
        ]
        
        for i, r in enumerate(failed_rows, 1):
            report_lines.append(f"#### 樣本 {i}")
            report_lines.append(f"- **點位**：{r.get('point_name', '未知')}")
            report_lines.append(f"- **交換**：{r.get('input_name', '？')} → {r.get('output_name', '？')}")
            report_lines.append(f"- **OCR 最低信心度**：{r.get('ocr_min_confidence', 0):.3f}")
            
            # 欄位信心度
            fc = r.get("ocr_field_confidence") or {}
            if fc:
                field_conf = " ｜ ".join(f"{k} {fc.get(k, 0):.3f}" for k in ("POINT", "INPUT", "OUTPUT") if k in fc)
                report_lines.append(f"- **欄位信心度**：{field_conf}")
            
            # 問題
            issues = r.get("issues", [])
            if issues:
                report_lines.append(f"- **問題**：{', '.join(issues)}")
            
            # 交換比例
            if r.get("exchange_ratio_needs_ocr"):
                ratio = r.get("exchange_ratio", "未讀到")
                report_lines.append(f"- **交換比例**：{ratio}")
            
            # 截圖檔名
            source_file = r.get("source_file", "")
            if source_file:
                report_lines.append(f"- **截圖**：{os.path.basename(source_file)}")
            
            report_lines.append("")
        
        report_lines.append("---")
        report_lines.append("")
        report_lines.append("### 備註")
        report_lines.append("- 此報告由航海助手自動生成")
        report_lines.append("- 所有數據已匿名化處理")
        report_lines.append("- 僅包含 OCR 識識相關資訊，不包含個人資料")
        
        report_text = "\n".join(report_lines)
        
        # 複製到剪貼簿
        parent_win.clipboard_clear()
        parent_win.clipboard_append(report_text)
        parent_win.update()
        
        # 顯示提示
        messagebox.showinfo(
            "報告已生成",
            f"✓ 報告已複製到剪貼簿！\n\n"
            f"共 {len(failed_rows)} 個失敗樣本\n\n"
            f"下一步：\n"
            f"1. 前往 GitHub：https://github.com/你的用戶名/BDO_Sailing_Assistant_V6.1/issues\n"
            f"2. 點擊「New Issue」\n"
            f"3. 選擇「OCR 識識報告」模板\n"
            f"4. 貼上報告內容\n"
            f"5. 提交 Issue",
            parent=parent_win
        )


    def show_ocr_review(self, rows, reuse_win=None):
        recover_batch_remaining_times(rows)
        # V5.47：先替已結構化辨識出的非段位物品補永久身份。
        apply_confirmed_raw_identity_learning(rows)
        apply_confirmed_blank_context_learning(rows)
        apply_conservative_registry_typo_learning(rows)
        apply_visual_item_learning(rows)
        _register_observed_nonstage_items(rows)
        apply_learned_aliases_to_rows(rows)
        _promote_pending_aliases(rows)
        # V5.22：先補回人工修正，再套用人工信任。
        apply_manual_repairs(rows)
        reconcile_row_identities_independently(rows)
        apply_evidence_identity_corrections(rows)
        apply_learned_core_confirmations(rows)
        apply_visual_quantity_learning(rows)
        apply_ratio_confirmations(rows)
        apply_confirmed_material_input_quantity(rows)
        recover_structured_special_quantity_source(rows)
        recover_remaining_times_from_batch_twins(rows)
        for _rr in rows or []:
            normalize_dynamic_ratio_status(_rr)
        apply_current_ratio_corrections(rows)
        # 確認頁每次重建都落盤；上方數字與下一頁讀到的狀態保持一致。
        save_current_batch_rows(rows)
        # V5.85：舊永久比例只作封存證物，永遠不能蓋過這次圖片。
        # V5.22：結構化視窗可以重建，但不准失憶。
        if not hasattr(self,"_ocr_review_ui_state"):
            self._ocr_review_ui_state={
                "scroll_fraction":0.0
            }
        saved=dict(self._ocr_review_ui_state)

        if reuse_win is not None and reuse_win.winfo_exists():
            win=reuse_win
            # V5.47：真正原地換頁。Toplevel本身完全不關，只換裡面的內容。
            for child in win.winfo_children():
                child.destroy()
        else:
            win=tk.Toplevel(self)
            # V5.90.3：確認分頁也寫入共用 window_states.json，重開程式仍保留大小、位置與最大化。
            smart_window(win,"ocr_review","1180x720",980,650)
        win.title("🔎 OCR 人工確認")
        win.configure(bg=CUTE_BG)

        hero=tk.Frame(win,bg="#fffef9",padx=20,pady=12);hero.pack(fill="x",padx=18,pady=(14,6))
        add_mascot(hero,size=74,opacity=0.14,quip_key="ocr")
        tk.Label(hero,text="🔎 OCR 人工確認",font=(FONT,27,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(hero,text="先處理真的有問題的資料；已正確的內容可以直接放行。",
                 font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))

        ok=sum(1 for r in rows if r["status"]=="候選可用")
        bad=len(rows)-ok
        layout_counts={"A":0,"B":0}
        for _r in rows:
            _layout=str((_r.get("evidence") or {}).get("layout") or _r.get("layout") or "?").upper()
            if _layout in layout_counts:layout_counts[_layout]+=1
        learned_count=len(getattr(MASTER,"user_aliases",[]))
        summary=tk.Frame(win,bg=CUTE_BG);summary.pack(fill="x",padx=18,pady=4)
        for _i,(_title,_value,_color) in enumerate((
            ("📄 本輪資料",f"{len(rows)} 筆｜A {layout_counts['A']}｜B {layout_counts['B']}",CUTE_BLUE_SOFT),
            ("✓ 可以使用",f"{ok} 筆",CUTE_GREEN_SOFT),
            ("⚠ 需要確認",f"{bad} 筆",CUTE_YELLOW_SOFT),
            ("🧠 已學別名",f"{learned_count} 個","#f2effa"))):
            _box=tk.Frame(summary,bg=_color,padx=13,pady=8);_box.grid(row=0,column=_i,sticky="nsew",padx=4)
            tk.Label(_box,text=_title,font=(FONT,10,"bold"),bg=_color,fg="#607168").pack(anchor="w")
            tk.Label(_box,text=_value,font=(FONT,17,"bold"),bg=_color,fg=CUTE_GREEN).pack(anchor="w")
            summary.columnconfigure(_i,weight=1)

        # V5.22：殘兵不用在89筆裡躲。可直接只看待確認，或按問題類型獵殺。
        filterbar=tk.Frame(win,bg="#fffef9",padx=10,pady=8); filterbar.pack(fill="x",padx=18,pady=5)
        filter_var=tk.StringVar(value=getattr(self,"_ocr_review_filter","全部"))
        def _core_missing(r):
            missing=[]
            if not r.get("point_id") or str(r.get("point_name") or "").strip() in ("","?","？"): missing.append("POINT")
            if not r.get("input_item_id") or str(r.get("input_name") or r.get("input_raw") or "").strip() in ("","?","？"): missing.append("INPUT")
            out_text=str(r.get("output_name") or r.get("output_raw") or "").strip()
            out_id=r.get("output_item_id")
            # V5.47：特殊交換品也應有身份；不再用「特殊所以可無ID」例外。
            if not out_id or out_text in ("","?","？"): missing.append("OUTPUT")
            if any(("無法判定" in str(q) or "未識別" in str(q)) for q in r.get("issues",[])) and not missing:
                missing.append("核心")
            return missing
        def _is_core_confirmable(r):
            if r.get("status")=="候選可用": return False
            return not _core_missing(r)

        filter_counts={
            "全部":len(rows),
            "候選可用":sum(1 for x in rows if x.get("status")=="候選可用"),
            "可用－一般":sum(1 for x in rows if x.get("status")=="候選可用" and is_normal_stage_exchange(x)),
            "可用－特殊":sum(1 for x in rows if x.get("status")=="候選可用" and not is_normal_stage_exchange(x)),
            "待確認":sum(1 for x in rows if x.get("status")!="候選可用"),
            "POINT":sum(1 for x in rows if any("POINT" in q for q in x.get("issues",[]))),
            "INPUT":sum(1 for x in rows if any("INPUT" in q for q in x.get("issues",[]))),
            "OUTPUT":sum(1 for x in rows if any("OUTPUT" in q for q in x.get("issues",[]))),
            "低信心":sum(1 for x in rows if any("低信心" in q for q in x.get("issues",[]))),
            "比例待處理":sum(1 for x in rows if self._ratio_needs_human(x)),
            "可以直接確認":sum(1 for x in rows if _is_core_confirmable(x)),
            "真的有問題":sum(1 for x in rows if x.get("status")!="候選可用" and _core_missing(x))
        }
        def set_filter(name):
            if name==filter_var.get():
                return
            # V5.47：真正單視窗切頁。不要製造第二個 Toplevel，
            # 直接在原視窗內重建內容，避免「開了一堆分頁」的錯覺。
            self._ocr_review_filter=name
            if not hasattr(self,"_ocr_review_pages"):self._ocr_review_pages={}
            self._ocr_review_pages[name]=0
            filter_var.set(name)
            try:
                if not hasattr(self,"_ocr_review_ui_state"): self._ocr_review_ui_state={}
                st=win.state()
                self._ocr_review_ui_state["state"]=st
                if st=="normal":
                    self._ocr_review_ui_state["geometry"]=win.geometry()
                self._ocr_review_ui_state["scroll_fraction"]=0.0
            except Exception:
                pass
            # V5.47：不 destroy、不另開 Toplevel。
            # 下一個 idle 只把同一個視窗內的 widgets 換掉。
            self.after_idle(lambda:self.show_ocr_review(rows, reuse_win=win))
        for _idx,name in enumerate(("全部","候選可用","可用－一般","可用－特殊","待確認","可以直接確認","真的有問題","比例待處理","POINT","INPUT","OUTPUT","低信心")):
            active=(name==filter_var.get())
            prefix="▶ " if active else ("⚠ " if name=="待確認" else "")
            btn=tk.Button(filterbar,text=f"{prefix}{name} {filter_counts[name]}",
                      font=(FONT,12,"bold" if active else "normal"),
                      bg=(CUTE_GREEN if active else (CUTE_YELLOW_SOFT if name in ("待確認","真的有問題","比例待處理") else "#edf2ec")),
                      fg=("white" if active else CUTE_TEXT),relief="flat",bd=0,
                      activebackground=("#0f5b31" if active else CUTE_GREEN_SOFT),
                      command=lambda n=name:set_filter(n),padx=10,pady=5)
            btn.grid(row=_idx//5,column=_idx%5,sticky="ew",padx=3,pady=3)
            filterbar.columnconfigure(_idx%5,weight=1)

        # 批量確認：玩家仍可逐筆看完卡片，但最後只按一次。
        bulkbar=tk.Frame(win,bg=CUTE_BG); bulkbar.pack(pady=3)
        def _bulk_confirm_core():
            targets=[r for r in rows if _is_core_confirmable(r)]
            if not targets:
                self._toast("✓ 沒有需要批量確認的核心資料",1000); return
            data=load_manual_confirmations()
            def _confirm_mem_key(sig):
                if not isinstance(sig,dict): return None
                p=str(sig.get("point_id") or ""); i=str(sig.get("input_item_id") or "")
                o=str(sig.get("output_item_id") or ""); n=str(sig.get("output_name") or "").strip()
                e=str(sig.get("exchange_type") or "")
                if p and i and o: return ("id",p,i,o)
                if p and i and n: return ("name",p,i,n)
                if p and i and e: return ("type",p,i,e)
                return None
            existing={_confirm_mem_key(x.get("signature"))
                      for x in data if isinstance(x,dict) and _confirm_mem_key(x.get("signature"))}
            added=0
            for rr in targets:
                sig=core_confirm_signature(rr)
                k=_confirm_mem_key(sig)
                if k not in existing:
                    data.append({"signature":sig,"confirmed_as":"核心資料正確"})
                    existing.add(k); added+=1
                rr["issues"]=_issues_after_core_confirmation(rr)
                rr["status"]="候選可用" if not rr["issues"] else "待確認"; rr["_manual_confirmed"]=True; rr["_core_trusted"]=True
            save_manual_confirmations(data)
            save_current_batch_rows(rows)
            # V5.47：不關視窗、不等切頁，原地重建；上方數字與卡片立即同步。
            self._toast(f"✓ 已確認 {len(targets)} 筆；本頁已立即更新",1000)
            self.show_ocr_review(rows,reuse_win=win)
        core_n=sum(1 for r in rows if _is_core_confirmable(r))
        excluded_n=sum(1 for r in rows if r.get("status")!="候選可用" and _core_missing(r))
        if core_n:
            tk.Button(bulkbar,text=f"✓ 按這裡，只確認這 {core_n} 筆正常資料",
                      font=(FONT,13,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",command=_bulk_confirm_core,
                      padx=12,pady=6).pack(side="left",padx=5)
        if excluded_n:
            tk.Label(bulkbar,text=f"🛑 另外 {excluded_n} 筆有問題，不會動它們",
                     font=(FONT,13,"bold"),bg=CUTE_BG,fg="#b42318").pack(side="left",padx=8)

        if filter_var.get()=="可以直接確認":
            tk.Label(win,text="這一頁都是核心資料完整的。你可以逐筆看原圖；看完後按上面的按鈕一次確認全部。",
                     font=(FONT,13,"bold")).pack(pady=3)
        elif filter_var.get()=="真的有問題":
            tk.Label(win,text="這一頁只留核心身份真的缺失的資料。單純OCR信心低、但核心已完整，不再混進來。",
                     font=(FONT,13,"bold")).pack(pady=3)
        elif filter_var.get()=="可用－特殊":
            tk.Label(win,text="這裡只列已放行的特殊交換，方便集中檢查烏鴉硬幣、波浪黑石、奧基魯阿之花與其他非一般段位交換。",
                     font=(FONT,13,"bold")).pack(pady=3)

        nav=tk.Frame(win,bg=CUTE_BLUE_SOFT,padx=12,pady=7); nav.pack(fill="x",padx=18,pady=4)
        page_label_var=tk.StringVar(value="準備分頁…")
        def move_review_page(delta):
            if not hasattr(self,"_ocr_review_pages"):self._ocr_review_pages={}
            current=int(self._ocr_review_pages.get(filter_var.get(),0) or 0)
            self._ocr_review_pages[filter_var.get()]=max(0,min(total_pages-1,current+delta))
            self._ocr_review_ui_state["scroll_fraction"]=0.0
            self.show_ocr_review(rows,reuse_win=win)
        prev_page_btn=tk.Button(nav,text="← 上一頁",font=(FONT,13,"bold"),command=lambda:move_review_page(-1))
        prev_page_btn.pack(side="left",padx=4)
        tk.Label(nav,textvariable=page_label_var,font=(FONT,14,"bold"),bg=CUTE_BLUE_SOFT).pack(side="left",expand=True)
        next_page_btn=tk.Button(nav,text="下一頁 →",font=(FONT,13,"bold"),command=lambda:move_review_page(1))
        next_page_btn.pack(side="right",padx=4)

        tk.Button(win,text="🔁 用已學別名重新解析（不用重跑OCR）",
                  command=lambda:self._reparse_last_ocr(win),
                  font=(FONT,12,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",padx=12,pady=7).pack(pady=6)
        
        # V6.1：添加 OCR 報告生成按鈕
        def _generate_report():
            self.generate_ocr_report(rows, win)
        tk.Button(win,text="📊 生成 OCR 報告",
                  command=_generate_report,
                  font=(FONT,12,"bold"),bg=CUTE_YELLOW_SOFT,relief="flat",padx=12,pady=7).pack(pady=6)

        outer=tk.Frame(win,bg=CUTE_BG)
        outer.pack(fill="both",expand=True,padx=18,pady=12)
        canvas=tk.Canvas(outer,bg=CUTE_BG,highlightthickness=0)
        sb=tk.Scrollbar(outer,orient="vertical",command=canvas.yview)
        frame=tk.Frame(canvas,bg=CUTE_BG)
        def _refresh_review_scrollregion(_=None):
            try:
                canvas.update_idletasks();box=canvas.bbox("all")
                if box:canvas.configure(scrollregion=(box[0],box[1],box[2],box[3]+100))
            except Exception:pass
        frame.bind("<Configure>",_refresh_review_scrollregion)
        _frame_window=canvas.create_window((0,0),window=frame,anchor="nw")
        canvas.bind("<Configure>",lambda e:(canvas.itemconfigure(_frame_window,width=e.width),_refresh_review_scrollregion()),add="+")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left",fill="both",expand=True)
        sb.pack(side="right",fill="y")

        # V5.22：中鍵平權。
        # 以前根本沒有把 MouseWheel 綁進這個結構化候選視窗，
        # 所以只能抓右側捲軸。現在整個視窗（包含卡片/文字/按鈕）都能滾。
        def _review_wheel(event):
            delta=getattr(event,"delta",0)
            if delta:
                canvas.yview_scroll((-3 if delta>0 else 3),"units")
            elif getattr(event,"num",None)==4:
                canvas.yview_scroll(-3,"units")
            elif getattr(event,"num",None)==5:
                canvas.yview_scroll(3,"units")
            return "break"

        win.bind("<MouseWheel>",_review_wheel)
        win.bind("<Button-4>",_review_wheel)
        win.bind("<Button-5>",_review_wheel)

        active_filter=getattr(self,"_ocr_review_filter","全部")
        def _visible(r):
            issues=r.get("issues",[])
            if active_filter=="全部": return True
            if active_filter=="候選可用": return r.get("status")=="候選可用"
            if active_filter=="可用－一般": return r.get("status")=="候選可用" and is_normal_stage_exchange(r)
            if active_filter=="可用－特殊": return r.get("status")=="候選可用" and not is_normal_stage_exchange(r)
            if active_filter=="待確認": return r.get("status")!="候選可用"
            if active_filter=="POINT": return any("POINT" in q for q in issues)
            if active_filter=="INPUT": return any("INPUT" in q for q in issues)
            if active_filter=="OUTPUT": return any("OUTPUT" in q for q in issues)
            if active_filter=="低信心": return any("低信心" in q for q in issues)
            if active_filter=="比例待處理": return self._ratio_needs_human(r)
            if active_filter=="可以直接確認": return _is_core_confirmable(r)
            if active_filter=="真的有問題": return r.get("status")!="候選可用" and bool(_core_missing(r))
            return True

        all_visible_rows=[(i,r) for i,r in enumerate(rows,1) if _visible(r)]
        # Windows/Tk 的超高內嵌 Frame 會在約八十張大卡附近碰到高度上限。
        # 每頁只建立24張卡，89筆以上也不會被畫布偷偷截掉。
        page_size=24
        total_pages=max(1,(len(all_visible_rows)+page_size-1)//page_size)
        if not hasattr(self,"_ocr_review_pages"):self._ocr_review_pages={}
        page=max(0,min(total_pages-1,int(self._ocr_review_pages.get(active_filter,0) or 0)))
        self._ocr_review_pages[active_filter]=page
        visible_rows=all_visible_rows[page*page_size:(page+1)*page_size]
        page_label_var.set(f"第 {page+1} / {total_pages} 頁　｜　本頁第 {visible_rows[0][0] if visible_rows else 0}～{visible_rows[-1][0] if visible_rows else 0} 筆")
        prev_page_btn.configure(state=("normal" if page>0 else "disabled"))
        next_page_btn.configure(state=("normal" if page<total_pages-1 else "disabled"))
        if active_filter=="待確認":
            tk.Label(frame,text="⚠ 待確認工作台：只校正會進表/影響路線的核心資料；其他OCR差異可確認後忽略。",
                     font=(FONT,15,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f",pady=8).pack(fill="x",padx=10)
        if active_filter=="比例待處理":
            tk.Label(frame,text="⚠ 數量待確認：只有沒讀到、信心不足或多次辨識不一致才會出現在這裡。",
                     font=(FONT,15,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f",pady=8).pack(fill="x",padx=10)

        def _soften_review_card(widget,bg):
            """卡片內容沿用原功能，只統一柔和底色。"""
            for child in widget.winfo_children():
                if isinstance(child,(tk.Frame,tk.Label)):
                    try:child.configure(bg=bg)
                    except Exception:pass
                _soften_review_card(child,bg)

        for i,r in visible_rows:
            card_bg=CUTE_GREEN_SOFT if r.get("status")=="候選可用" else CUTE_YELLOW_SOFT
            _layout=str((r.get("evidence") or {}).get("layout") or r.get("layout") or "?").upper()
            card=tk.LabelFrame(frame,text=f"{i}. {r.get('point_name') or '⚠ 未知點位'}　｜版型 {_layout}",
                               font=(FONT,15,"bold"),bg=card_bg,fg=CUTE_GREEN,
                               bd=1,relief="solid",padx=14,pady=11)
            card.pack(fill="x",padx=10,pady=8)

            trade=f"{r.get('input_name') or '？'}  →  {r.get('output_name') or '？'}"
            tk.Label(card,text=trade,font=(FONT,17,"bold"),anchor="w").pack(fill="x")
            if r.get("_manual_repair_memory"):
                tk.Label(card,text="✓ 這筆有套用你以前的人工修正",font=(FONT,12,"bold"),anchor="w").pack(fill="x",pady=(2,0))
            if r.get("_core_trusted_from_memory"):
                tk.Label(card,text="✓ 你以前確認過這筆核心資料，這次直接記得",font=(FONT,12,"bold"),anchor="w").pack(fill="x",pady=(2,0))
            tk.Label(card,text=f"來源：{r.get('source_file','')} ｜ OCR最低信心：{r.get('ocr_min_confidence')}",
                     font=(FONT,11),anchor="w").pack(fill="x",pady=(4,0))
            fc=r.get("ocr_field_confidence") or {}
            if fc:
                tk.Label(card,text="欄位信心："+" ｜ ".join(f"{k} {fc.get(k):.3f}" for k in ("POINT","INPUT","OUTPUT") if k in fc),
                         font=(FONT,11,"bold"),anchor="w").pack(fill="x")
            def _stage_item_text(side):
                name=r.get(f"{side}_name") or r.get(f"{side}_raw") or "？"
                kind=str(r.get(f"{side}_kind") or "")
                stage=r.get(f"{side}_stage")
                # 只有「段位品」才需要階段；一般材料／特殊品本來就沒有段位。
                if kind=="段位品":return f"[{stage or '?'}段] {name}"
                return str(name)
            stages=f"{_stage_item_text('input')}  →  {_stage_item_text('output')}"
            tk.Label(card,text=f"交換：{stages}",
                     font=(FONT,14,"bold"),anchor="w").pack(fill="x")
            tk.Label(card,text=f"剩餘交換次數：{r.get('remaining_times') or '？'}",
                     font=(FONT,11),anchor="w").pack(fill="x")
            issues="、".join(r.get("issues",[])) or "無"
            tk.Label(card,text=f"判定：{r['status']} ｜ 疑點：{issues}",
                     font=(FONT,12,"bold"),anchor="w").pack(fill="x",pady=(4,0))
            # 一般低階確認產出；特殊交換則完整確認投入與產出。
            ratio_box=tk.Frame(card); ratio_box.pack(fill="x",pady=(6,3))
            if not r.get("exchange_ratio_needs_ocr"):
                tk.Label(ratio_box,text="交換比例：預設規則（不需OCR）",
                         font=(FONT,15,"bold")).pack(side="left")
            else:
                ratio=r.get("exchange_ratio") or "⚠ OUTPUT數量未讀到"
                srcq=r.get("exchange_ratio_source") or {}
                intok=srcq.get("input_token");iconf=srcq.get("input_confidence",0)
                ocrtok=srcq.get("output_token")
                conf=srcq.get("output_confidence",0)
                tk.Label(ratio_box,text=f"交換比例：{ratio}",
                         font=(FONT,18,"bold")).pack(side="left",padx=(0,10))
                _ocr_qty_text=(f"OCR數字：INPUT={intok or r.get('exchange_ratio_input') or '未讀到'}（{iconf}）｜"
                               f"OUTPUT={ocrtok or '未讀到'}（{conf}）")
                tk.Label(ratio_box,text=_ocr_qty_text,
                         font=(FONT,12,"bold")).pack(side="left",padx=(0,10))
                def _set_qty(v,rr=r,idx=i,parent=win):
                    rr["exchange_ratio_input"]=1
                    rr["exchange_ratio_output"]=int(v)
                    rr["output_quantity"]=int(v)
                    rr["exchange_ratio"]=f"1 : {int(v)}"
                    rr["_ratio_confirmed_current_batch"]=True;rr["exchange_ratio_auto_accepted"]=True
                    save_ratio_override(rr,int(v))
                    self._after_ratio_fix(rows,parent=parent,canvas=canvas,ratio_text=f"1:{int(v)}")
                if not self._ratio_needs_human(r):
                    if r.get("_ratio_confirmed_current_batch"):
                        tk.Label(ratio_box,text="✓ 本輪人工確認",font=(FONT,13,"bold")).pack(side="left",padx=5)
                    else:
                        tk.Label(ratio_box,text="✓ OCR數字清楚，已自動採用",font=(FONT,13,"bold")).pack(side="left",padx=5)
                else:
                    if r.get("exchange_ratio_manual_both"):
                        def _set_both(rr=r,parent=win):
                            iq=simpledialog.askinteger("投入數量","每次交換要交出幾個？",parent=parent,minvalue=1,maxvalue=99999)
                            if iq is None:return
                            oq=simpledialog.askinteger("產出數量","每次交換會得到幾個？",parent=parent,minvalue=1,maxvalue=99999)
                            if oq is None:return
                            rr["exchange_ratio_input"]=iq;rr["input_quantity"]=iq
                            rr["exchange_ratio_output"]=oq;rr["output_quantity"]=oq
                            rr["exchange_ratio"]=f"{iq} : {oq}（本輪人工確認）";rr["_ratio_confirmed_current_batch"]=True;rr["exchange_ratio_auto_accepted"]=True
                            save_ratio_override(rr,oq,iq)
                            self._after_ratio_fix(rows,parent=parent,canvas=canvas,ratio_text=f"{iq}:{oq}")
                        tk.Button(ratio_box,text="✎ 輸入完整比例",font=(FONT,12,"bold"),command=_set_both,padx=7,pady=3).pack(side="left",padx=4)
                    for n in ([] if r.get("exchange_ratio_manual_both") else range(1,6)):
                        tk.Button(ratio_box,text=str(n),font=(FONT,13,"bold"),
                                  command=lambda v=n:_set_qty(v),width=2).pack(side="left",padx=2)
                    tk.Button(ratio_box,text="🖼 看原圖",
                              font=(FONT,12,"bold"),
                              command=lambda rr=r,idx=i:self._show_original_evidence(rr,idx,win,rows=rows),
                              padx=7,pady=3).pack(side="left",padx=5)


            learn=tk.Frame(card); learn.pack(fill="x",pady=(6,2))

            # V5.22：學習進度。已由別名成功匹配的欄位保留明顯「✓ 已學習」標記，
            # 不讓玩家校到後面忘記自己做到哪。
            def _norm_ui(x):
                return MASTER._n(str(x or ""))
            rawp=""
            for t in r.get("raw_texts",[]):
                if t.startswith("POINT:"):
                    rawp=t[6:].split("|")[0].strip()

            if r.get("point_id") and rawp and _norm_ui(rawp)!=_norm_ui(r.get("point_name")):
                tk.Label(learn,text="✓ POINT 已學習",font=(FONT,12,"bold"),padx=8,pady=4).pack(side="left",padx=(0,6))
            if r.get("input_item_id") and r.get("input_raw") and _norm_ui(r.get("input_raw"))!=_norm_ui(r.get("input_name")):
                tk.Label(learn,text="✓ INPUT 已學習",font=(FONT,12,"bold"),padx=8,pady=4).pack(side="left",padx=(0,6))
            if r.get("output_item_id") and r.get("output_raw") and _norm_ui(r.get("output_raw"))!=_norm_ui(r.get("output_name")):
                tk.Label(learn,text="✓ OUTPUT 已學習",font=(FONT,12,"bold"),padx=8,pady=4).pack(side="left",padx=(0,6))

            if not r.get("point_id"):
                tk.Button(learn,text=f"🧠 學習點位：{rawp or '未知'}",
                          command=lambda x=rawp:self._learn_alias("點位",x,parent=win),
                          font=(FONT,10,"bold")).pack(side="left",padx=(0,6))
            if r.get("input_kind")=="段位品" and not r.get("input_item_id") and r.get("input_raw"):
                tk.Button(learn,text=f"🧠 學習INPUT：{r.get('input_raw')}",
                          command=lambda x=r.get("input_raw"),st=r.get("input_stage"):self._learn_alias("品項",x,st,parent=win),
                          font=(FONT,10,"bold")).pack(side="left",padx=(0,6))
            if r.get("output_kind")=="段位品" and not r.get("output_item_id") and r.get("output_raw"):
                tk.Button(learn,text=f"🧠 學習OUTPUT：{r.get('output_raw')}",
                          command=lambda x=r.get("output_raw"),st=r.get("output_stage"):self._learn_alias("品項",x,st,parent=win),
                          font=(FONT,10,"bold")).pack(side="left",padx=(0,6))
            # 每一筆都能看原圖與手動修改；已放行資料也可能發生「合法ID但配錯人」。
            if True:
                actionbox=tk.Frame(card); actionbox.pack(fill="x",pady=(8,4))
                pending_identity_sides=[side for side in ("input","output")
                    if any(f"{side.upper()}新身份待確認" in str(q) for q in r.get("issues",[]))]
                for side in pending_identity_sides:
                    side_label="INPUT" if side=="input" else "OUTPUT"
                    def _confirm_identity(s=side,rr=r,parent=win):
                        result=confirm_row_item_identity(rr,s)
                        if not result.get("ok"):
                            messagebox.showerror("無法確認",result.get("error") or "確認失敗",parent=parent);return
                        apply_confirmed_raw_identity_learning(rows);apply_confirmed_blank_context_learning(rows);reconcile_row_identities_independently(rows)
                        save_current_batch_rows(rows)
                        self._toast(f"✓ {result['name']} 已記住；下次自動辨認",1600)
                        self.show_ocr_review(rows,reuse_win=parent)
                    tk.Button(actionbox,text=f"✓ {side_label}名稱正確，確認身份",
                              font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                              command=_confirm_identity,padx=10,pady=5).pack(side="left",padx=4)
                has_learn=(
                    not r.get("point_id") or
                    (r.get("input_kind")=="段位品" and not r.get("input_item_id") and r.get("input_raw")) or
                    (r.get("output_kind")=="段位品" and not r.get("output_item_id") and r.get("output_raw"))
                )
                if r.get("status")!="候選可用" and not has_learn and not pending_identity_sides:
                    tk.Label(actionbox,text="👉 核心身份已匹配；請看原始切片確認核心資料即可。",
                             font=(FONT,13,"bold")).pack(side="left",padx=(0,8))
                    def _confirm_row(rr=r, idx=i, parent=win):
                        data=load_manual_confirmations()
                        sig=core_confirm_signature(rr)
                        def _cmk(s):
                            if not isinstance(s,dict): return None
                            p=str(s.get("point_id") or ""); ii=str(s.get("input_item_id") or "")
                            o=str(s.get("output_item_id") or ""); n=str(s.get("output_name") or "").strip()
                            ee=str(s.get("exchange_type") or "")
                            if p and ii and o:return ("id",p,ii,o)
                            if p and ii and n:return ("name",p,ii,n)
                            if p and ii and ee:return ("type",p,ii,ee)
                            return None
                        sigkey=_cmk(sig)
                        existing={_cmk(x.get("signature")) for x in data if isinstance(x,dict) and _cmk(x.get("signature"))}
                        if sigkey not in existing:
                            data.append({"signature":sig,"confirmed_as":"核心資料正確"})
                            save_manual_confirmations(data)
                        rr["issues"]=_issues_after_core_confirmation(rr)
                        rr["status"]="候選可用" if not rr["issues"] else "待確認"
                        rr["_manual_confirmed"]=True
                        save_current_batch_rows(rows)
                        try:self._ocr_review_ui_state["scroll_fraction"]=canvas.yview()[0]
                        except:pass
                        self.show_ocr_review(rows,reuse_win=parent)
                    tk.Button(actionbox,text="✓ 核心資料正確，忽略非必要OCR差異",
                              font=(FONT,13,"bold"),command=_confirm_row,
                              padx=10,pady=5).pack(side="left",padx=4)

                def _manual_edit(rr=r, idx=i, parent=win):
                    ew=tk.Toplevel(parent); ew.title(f"✏ 手動修改第 {idx} 筆")
                    smart_window(ew,"manual_edit","840x590",760,540); ew.transient(parent); ew.grab_set();ew.configure(bg=CUTE_BG)
                    edit_hero=tk.Frame(ew,bg="#fffef9",padx=20,pady=12);edit_hero.pack(fill="x",padx=22,pady=(18,8))
                    add_mascot(edit_hero,size=72,opacity=0.14,quip_key="edit")
                    tk.Label(edit_hero,text=f"✏ 第 {idx} 筆｜手動修改",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
                    tk.Label(edit_hero,text="請照原始切片輸入正式名稱；沒有修改的欄位保持原樣即可。",
                             font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
                    body=tk.Frame(ew,bg=CUTE_GREEN_SOFT,padx=18,pady=14); body.pack(fill="x",padx=22,pady=6)
                    specs=[("POINT","point_name"),("INPUT","input_name"),("OUTPUT","output_name")]
                    vars_={}
                    for rown,(lab,keyname) in enumerate(specs):
                        tk.Label(body,text=lab,font=(FONT,16,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).grid(row=rown,column=0,sticky="w",pady=10)
                        v=tk.StringVar(value=str(rr.get(keyname) or rr.get(keyname.replace("_name","_raw")) or ""))
                        vars_[keyname]=v
                        tk.Entry(body,textvariable=v,font=(FONT,19),width=36,relief="flat",highlightthickness=1,
                                 highlightbackground=CUTE_BORDER,highlightcolor=CUTE_GREEN).grid(row=rown,column=1,padx=12,pady=10,sticky="ew")
                    body.columnconfigure(1,weight=1)
                    tk.Label(ew,text="輸入正式名稱；已有資料會沿用原ID，真正的新物品會先問你再建立ID。",
                             font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f",padx=12,pady=7).pack(fill="x",padx=22,pady=8)
                    def save_edit():
                        before=dict(rr)
                        mapping=[("point_name",MASTER.resolve_point,"point_id","point"),
                                 ("input_name",MASTER.resolve_item,"input_item_id","input"),
                                 ("output_name",MASTER.resolve_item,"output_item_id","output")]
                        unresolved_points=[]
                        for keyname,resolver,idkey,side in mapping:
                            val=vars_[keyname].get().strip()
                            if not val: continue
                            x=resolver(val)
                            if x.get("matched"):
                                rr[idkey]=x["record"]["id"]; rr[keyname]=x["record"]["name"]
                                if side!="point":
                                    st=x["record"].get("stage")
                                    rr[f"{side}_stage"]=str(st) if st not in (None,"") else ""
                                    rr[f"{side}_kind"]="段位品" if st not in (None,"") else (rr.get(f"{side}_kind") or "一般材料")
                            elif side!="point":
                                old=_find_registry_item(val)
                                if old:
                                    rec,_=_confirm_new_item_identity(val,rr.get(f"{side}_kind",""),
                                        rr.get(f"{side}_stage",""),rr.get("source_file",""),side)
                                    rr[idkey]=rec["item_id"];rr[keyname]=val
                                    rr[f"{side}_kind"]=str(rec.get("kind") or rr.get(f"{side}_kind") or "未分類")
                                    if rr[f"{side}_kind"]!="段位品":rr[f"{side}_stage"]=""
                                elif messagebox.askyesno("建立新物品身份",
                                        f"主資料庫找不到「{val}」。\n\n"
                                        "如果你已經看原圖確認這是正式名稱，按『是』建立新的穩定 ID。\n"
                                        "如果還不確定，按『否』回去繼續確認。",parent=ew):
                                    rec,_=_confirm_new_item_identity(val,rr.get(f"{side}_kind",""),
                                        rr.get(f"{side}_stage",""),rr.get("source_file",""),side)
                                    rr[idkey]=rec["item_id"];rr[keyname]=val
                                    rr[f"{side}_kind"]=str(rec.get("kind") or rr.get(f"{side}_kind") or "未分類")
                                    if rr[f"{side}_kind"]!="段位品":rr[f"{side}_stage"]=""
                                else:return
                            else:
                                unresolved_points.append(val)
                        if unresolved_points:
                            messagebox.showerror("找不到交換點身份",
                                "交換點目前仍必須對上正式點位資料庫：\n"+"\n".join(unresolved_points),parent=ew)
                            return
                        corrected_sides=[]
                        for side,idkey,namekey in (("point","point_id","point_name"),("input","input_item_id","input_name"),("output","output_item_id","output_name")):
                            if (str(before.get(idkey) or "")!=str(rr.get(idkey) or "") or
                                    str(before.get(namekey) or "")!=str(rr.get(namekey) or "")):
                                if save_evidence_identity_correction(rr,side):corrected_sides.append(side)
                        visual_learned=[]
                        for side in ("input","output"):
                            if side in corrected_sides and learn_visual_item(rr,side):visual_learned.append(side)
                        learned_repairs=remember_manual_repairs(before,rr)
                        if rr.get("point_id") and rr.get("input_item_id") and rr.get("output_item_id"):
                            rr["issues"]=_issues_after_core_confirmation(rr)
                            if not rr["issues"]:rr["status"]="候選可用"
                        save_current_batch_rows(rows)
                        route_sync=sync_planned_route_identity_after_edit(self.state_data,before,rr)
                        if route_sync.get("updated") or route_sync.get("completed_skipped"):STORE.save(self.state_data)
                        if visual_learned:self._toast("✓ 本張已修正，物品圖片也學起來了\n下次文字讀不到會先看圖辨認",1800)
                        elif corrected_sides:self._toast("✓ 本張圖片已修正\n不會再拿去覆蓋別張圖片",1500)
                        if route_sync.get("completed_skipped"):
                            self._toast("⚠ 這筆已有完成入帳紀錄；為保護帳本，已完成站未自動換 ID\n請先撤銷該站再重新套用修正",2600)
                        try:ew.grab_release()
                        except:pass
                        ew.destroy()
                        try:self._ocr_review_ui_state["scroll_fraction"]=canvas.yview()[0]
                        except:pass
                        parent.destroy(); self.show_ocr_review(rows)
                    def cancel():
                        try:ew.grab_release()
                        except:pass
                        ew.destroy(); parent.after(30,lambda:(parent.lift(),parent.focus_force()))
                    edit_foot=tk.Frame(ew,bg="#fffef9",padx=16,pady=10);edit_foot.pack(side="bottom",fill="x")
                    tk.Button(edit_foot,text="取消",font=(FONT,13),bg="#edf2ec",relief="flat",command=cancel,padx=18,pady=8).pack(side="right",padx=6)
                    tk.Button(edit_foot,text="✓ 套用本筆修改",font=(FONT,16,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                              activebackground="#0f5b31",activeforeground="white",command=save_edit,padx=24,pady=9).pack(side="right",padx=6)
                    ew.protocol("WM_DELETE_WINDOW",cancel)

                tk.Button(actionbox,text="🖼 查看原始切片",
                          font=(FONT,12,"bold"),
                          command=lambda rr=r,idx=i:self._show_original_evidence(rr,idx,win,rows=rows),
                          padx=8,pady=4).pack(side="right",padx=4)
                tk.Button(actionbox,text="✏ 手動修改這筆",
                          font=(FONT,12),command=_manual_edit,padx=8,pady=4).pack(side="right",padx=4)

            tk.Label(card,text="OCR原文："+" ｜ ".join(r.get("raw_texts",[])),
                     font=(FONT,10),anchor="w",wraplength=1080,justify="left").pack(fill="x",pady=(4,0))
            _soften_review_card(card,card_bg)

        # 大批資料的卡片高度會分數次完成計算；保留底部空間並分段重算，
        # 避免第82筆以後已建立卻落在舊 scrollregion 外面。
        tk.Frame(frame,bg=CUTE_BG,height=100).pack(fill="x")
        for _delay in (0,80,250,700):win.after(_delay,_refresh_review_scrollregion)

        tk.Label(win,
                 text="小提醒：確認過的 OCR 錯名會被記住，下次遇到相同內容可以自動套用。",
                 font=(FONT,11,"bold"),bg=CUTE_BG,fg="#607168").pack(pady=10)
        # 恢復上次捲動位置（卡片建完後才有 scrollregion）。
        target_scroll=max(0.0,min(1.0,float(saved.get("scroll_fraction",0.0))))
        win.after(120,lambda: canvas.yview_moveto(target_scroll) if win.winfo_exists() else None)

        def _remember_review_state():
            try:
                st=win.state()
                self._ocr_review_ui_state["state"]=st
                if st=="normal":
                    self._ocr_review_ui_state["geometry"]=win.geometry()
                self._ocr_review_ui_state["scroll_fraction"]=canvas.yview()[0]
            except Exception:
                pass
        win.bind("<Destroy>",lambda e:_remember_review_state() if e.widget is win else None,add="+")
        return win

    def show_route_picker(self):
        """V5.48｜正式選路頁：把工程清單換成今日交換選擇儀表板。"""
        try:
            path=CURRENT_BATCH_PATH
            if not os.path.exists(path):
                messagebox.showinfo("今日交換選擇","目前沒有本輪資料。先沿用上一版資料或完成一次OCR。")
                return
            with open(path,"r",encoding="utf-8") as f:
                rows=json.load(f)
            if not isinstance(rows,list): rows=[]
            recover_batch_remaining_times(rows)
            apply_confirmed_raw_identity_learning(rows)
            apply_confirmed_blank_context_learning(rows)
            apply_conservative_registry_typo_learning(rows)
            apply_visual_item_learning(rows)
            _register_observed_nonstage_items(rows)
            apply_learned_aliases_to_rows(rows)
            _promote_pending_aliases(rows)
            apply_manual_repairs(rows)
            reconcile_row_identities_independently(rows)
            apply_evidence_identity_corrections(rows)
            apply_visual_quantity_learning(rows)
            apply_ratio_confirmations(rows)
            apply_confirmed_material_input_quantity(rows)
            recover_structured_special_quantity_source(rows)
            recover_remaining_times_from_batch_twins(rows)
            for _rr in rows:normalize_dynamic_ratio_status(_rr)
            apply_current_ratio_corrections(rows)
            apply_learned_core_confirmations(rows)
            _stage_input_repaired=[r for r in rows if normalize_stage_input_quantity(r)]
            save_current_batch_rows(rows)
            _qty_repaired=sum(1 for _r in rows if isinstance(_r,dict) and normalize_fixed_stage_output_quantity(_r))
            if _qty_repaired:
                with open(CURRENT_BATCH_PATH,"w",encoding="utf-8") as _f:
                    json.dump(rows,_f,ensure_ascii=False,indent=2)

            # V5.48｜高G ID碰撞修正：
            # POINT_G_002 原本被「卡修麻」與「哈科班」共用。
            # 卡修麻保留 POINT_G_002；只有名稱明確為哈科班的舊資料改成 POINT_HIGHG_002。
            for _r in rows:
                normalize_point_identity(_r)

            usable=[r for r in rows if isinstance(r,dict) and r.get("status")=="候選可用"]

            _dedup=[];_seen=set()
            for _r in usable:
                _k=(str(_r.get("point_id") or ""),str(_r.get("input_item_id") or ""),str(_r.get("output_item_id") or ""),
                    str(_r.get("input_stage") or ""),str(_r.get("output_stage") or ""))
                if all(_k[:3]) and _k in _seen:continue
                _seen.add(_k);_dedup.append(_r)
            usable=_dedup

            # V5.47：交涉力資料補源。
            # 某些沿用版本的 current_batch_ocr.json 保留了交換身份，但沒把 negotiation_power 帶過來；
            # 同一批完整 ocr_structured_debug.json 通常仍在 data 內。用穩定身份補回，不再默默當 0。
            full_candidates=[
                os.path.join(BASE,"data","ocr_structured_debug.json"),
                os.path.join(BASE,"data","current_exchange_pool.json"),
            ]
            full_rows=[]
            for fp in full_candidates:
                try:
                    if not os.path.exists(fp): continue
                    with open(fp,"r",encoding="utf-8") as ff:
                        xx=json.load(ff)
                    if isinstance(xx,dict):
                        xx=xx.get("rows") or xx.get("usable") or xx.get("data") or []
                    if isinstance(xx,list):
                        full_rows.extend(x for x in xx if isinstance(x,dict))
                except Exception:
                    pass

            def _barter_identity(rr):
                # source_file + row_index 最精準；沒有時退回核心交換身份。
                sf=str(rr.get("source_file") or "")
                ri=str(rr.get("row_index") or "")
                if sf and ri:
                    return ("src",sf,ri)
                return ("core",
                        str(rr.get("point_id") or rr.get("point_name") or ""),
                        str(rr.get("input_item_id") or rr.get("input_name") or ""),
                        str(rr.get("output_item_id") or rr.get("output_name") or ""))

            enrich={}
            for rr in full_rows:
                enrich[_barter_identity(rr)]=rr
            for rr in usable:
                if rr.get("negotiation_power"):
                    continue
                hit=enrich.get(_barter_identity(rr))
                if hit:
                    for kk in ("negotiation_power","raw_texts","input_raw","output_raw","point_raw"):
                        if not rr.get(kk) and hit.get(kk):
                            rr[kk]=hit.get(kk)
        except Exception:
            messagebox.showerror("今日交換選擇",traceback.format_exc()); return

        try:
            with open(SEARCH_ALIAS_PATH,"r",encoding="utf-8") as f:
                aliases=json.load(f)
            if not isinstance(aliases,dict): aliases={}
        except Exception: aliases={}
        item_aliases=aliases.get("items",{}) if isinstance(aliases.get("items",{}),dict) else {}
        point_aliases=aliases.get("points",{}) if isinstance(aliases.get("points",{}),dict) else {}

        canonical_item_alias={}; canonical_point_alias={}
        _player_item_aliases=load_player_item_aliases()
        _official_item_names={}
        try:
            with open(os.path.join(BASE,"data","master_data.json"),"r",encoding="utf-8") as _f:_mj=json.load(_f)
            for _x in _mj.get("items",[]):
                if isinstance(_x,dict) and _x.get("id"):
                    _official_item_names[str(_x["id"])]=str(_x.get("name") or "")
        except Exception:pass
        for _iid,_ab in _player_item_aliases.items():
            _ab=str(_ab or "").strip()
            if _ab:
                canonical_item_alias.setdefault(_ab,set()).update(v for v in (str(_iid),_official_item_names.get(str(_iid),"")) if v)
        player_point_groups=load_player_point_groups()
        for _pid,_grp in player_point_groups.items():
            _grp=str(_grp or "").strip()
            if _grp:canonical_point_alias.setdefault(_grp,set()).add(str(_pid))

        # V5.47：路線屬性回到既有 search_master_data，不再猜 MASTER.points 的 zone 欄位。
        point_meta={}
        try:
            with open(os.path.join(BASE,"search_master_data.json"),"r",encoding="utf-8") as _f:
                _sm=json.load(_f)
            for _i,_x in enumerate(_sm.get("points",[]) if isinstance(_sm,dict) else []):
                if not isinstance(_x,dict): continue
                _id=str(_x.get("id") or "")
                _nm=str(_x.get("name") or "")
                _grp=str(_x.get("group") or "")
                # 同ID可能在舊表出現兩次；具策略意義的高G/高Z/遠線/大洋優先。
                _priority=2 if _grp in ("高G","高Z","遠線","大洋") else 1
                for _key in (("id",_id),("name",_nm)):
                    if not _key[1]: continue
                    old=point_meta.get(_key)
                    if old is None or _priority>=old.get("_priority",0):
                        point_meta[_key]=dict(_x,_order=_i,_priority=_priority)
        except Exception:
            pass

        # 疊加正式 master_data 的 zone / strategy / scattered；分類與公式分離。
        try:
            with open(os.path.join(BASE,"data","master_data.json"),"r",encoding="utf-8") as _f:
                _mfull=json.load(_f)
            for _i,_x in enumerate(_mfull.get("points",[]) if isinstance(_mfull,dict) else []):
                if not isinstance(_x,dict):continue
                for _key in (("id",str(_x.get("id") or "")),("name",str(_x.get("name") or ""))):
                    if not _key[1]:continue
                    if _key in point_meta:
                        point_meta[_key].update({k:v for k,v in _x.items() if v not in ("",None)})
                    else:
                        point_meta[_key]=dict(_x,_order=10000+_i,_priority=1)
        except Exception:pass

        _player_point_prefs=load_player_point_preferences()
        def point_info(r):
            pid=str(r.get("point_id") or "")
            base=dict(point_meta.get(("id",pid)) or
                      point_meta.get(("name",str(r.get("point_name") or ""))) or {})
            pref=_player_point_prefs.get(pid,{})
            if isinstance(pref,dict):base.update(pref)
            pg=player_point_groups.get(pid)
            if pg is not None:base["player_group"]=str(pg)
            return base

        def is_remote(r):
            return str(point_info(r).get("group") or "")=="遠線"

        try:
            disc=float(PLAYER_SETTINGS.get("negotiation_discount_percent",0) or 0)
        except Exception: disc=0.0
        _saved_picker=self.state_data.get("route_picker") or {}
        try:
            _saved_cap=int(_saved_picker.get("power_cap") or PLAYER_SETTINGS.get("negotiation_power_limit",1000000) or 1000000)
        except Exception:_saved_cap=1000000
        if _saved_cap not in (1000000,1250000):_saved_cap=1000000
        cap_var=tk.IntVar(value=_saved_cap)
        def current_cap():
            return int(cap_var.get() or 1000000)
        mult=max(0.0,1.0-disc/100.0)

        # V5.47：交涉力完全改回硬規則計算，不讀OCR畫面值。
        # 來源：黑沙航海筆記已驗證規則
        # 普貨原始單次 14,286；高貨原始單次 21,650；
        # 高貨判定＝目標物為烏鴉硬幣。
        try:NORMAL_BARTER_BASE=int(PLAYER_SETTINGS.get("normal_barter_base_power",14286) or 14286)
        except:NORMAL_BARTER_BASE=14286
        try:HIGH_BARTER_BASE=int(PLAYER_SETTINGS.get("high_barter_base_power",21650) or 21650)
        except:HIGH_BARTER_BASE=21650

        def barter_base_power(r):
            out=str(r.get("output_name") or "").strip()
            return HIGH_BARTER_BASE if out=="烏鴉硬幣" else NORMAL_BARTER_BASE

        def discounted_single_power(r):
            return barter_base_power(r)*mult

        win=tk.Toplevel(self)
        win.title("今日交換選擇｜V5.70.3")
        smart_window(win,"route_picker","1380x840",1100,680)

        outer=tk.Frame(win,bg=CUTE_BG); outer.pack(fill="both",expand=True)
        head=tk.Frame(outer,bg="#fffef9"); head.pack(fill="x",padx=18,pady=(12,6))
        add_mascot(head,size=72,opacity=0.15,quip_key="picker")
        tk.Label(head,text="⚓ 今日交換選擇",font=(FONT,27,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left")
        tk.Label(head,text="　選好今天想跑的交換，再交給路線工作台。",font=(FONT,12),bg="#fffef9",fg="#607168").pack(side="left",pady=(9,0))

        summary=tk.Frame(outer,bg=CUTE_BG);summary.pack(fill="x",padx=18,pady=7)
        rem_var=tk.StringVar(); sel_var=tk.StringVar(); use_var=tk.StringVar(); power_diag_var=tk.StringVar()
        _picker_summary_colors=(CUTE_GREEN_SOFT,CUTE_BLUE_SOFT,CUTE_YELLOW_SOFT)
        for i,(title,var) in enumerate((("⚡ 剩餘交涉力",rem_var),("☑ 已選交換",sel_var),("📊 已選預估消耗",use_var))):
            _sc=_picker_summary_colors[i]
            f=tk.Frame(summary,bg=_sc,bd=0,padx=16,pady=10)
            f.grid(row=0,column=i,sticky="nsew",padx=5)
            tk.Label(f,text=title,font=(FONT,11,"bold"),bg=_sc,fg="#607168").pack(anchor="w")
            tk.Label(f,textvariable=var,font=(FONT,20,"bold"),bg=_sc,fg=CUTE_GREEN).pack(anchor="w",pady=(2,0))
            if i==0:
                caprow=tk.Frame(f,bg=_sc);caprow.pack(anchor="w",pady=(5,0))
                tk.Label(caprow,text="上限：",font=(FONT,10,"bold"),bg=_sc).pack(side="left")
                for _cap,_txt in ((1000000,"1,000,000"),(1250000,"1,250,000")):
                    tk.Radiobutton(caprow,text=_txt,variable=cap_var,value=_cap,
                                   font=(FONT,10,"bold"),bg=_sc,selectcolor=_sc,
                                   command=lambda: (save_picker_state(),update_summary())).pack(side="left",padx=3)
            summary.columnconfigure(i,weight=1)
        sf=tk.Frame(summary,bg="#f2effa",bd=0,padx=16,pady=10)
        sf.grid(row=0,column=3,sticky="nsew",padx=5); summary.columnconfigure(3,weight=1)
        tk.Label(sf,text="⚙ 目前設定",font=(FONT,11,"bold"),bg="#f2effa",fg="#685b78").pack(anchor="w")
        tk.Label(sf,text=f"交涉力減免 {disc:.2f}%\n普貨 14,286 × {mult:.4f}\n高貨 21,650 × {mult:.4f}",
                 font=(FONT,11,"bold"),bg="#f2effa",fg=CUTE_TEXT).pack(anchor="w")
        tk.Label(sf,textvariable=power_diag_var,font=(FONT,9,"bold"),bg="#f2effa",fg="#b42318").pack(anchor="w")

        # V5.47：第一層只放「交換階段」，不再把遠線這種路線分類混進來。
        tabs=tk.Frame(outer,bg=CUTE_BG);tabs.pack(fill="x",padx=18,pady=(7,3))
        stage_var=tk.StringVar(value=str(_saved_picker.get("stage") or "4→5階"))
        special_definitions=load_special_tabs()
        custom_special_names=[str(x.get("name") or "") for x in special_definitions if str(x.get("name") or "")]
        special_names=custom_special_names+["其他"]
        _saved_special=str(_saved_picker.get("special") or "全部特殊")
        special_var=tk.StringVar(value=_saved_special if _saved_special in (["全部特殊"]+special_names) else "全部特殊")
        modes=["全部","1→2階","2→3階","3→4階","4→5階","5→6階","6→7階","特殊"]
        tab_buttons={}

        def is_normal_stage(r):
            return is_normal_stage_exchange(r)

        def special_group_keys(r):
            """一筆特殊交換可能同時命中多個玩家分類；只負責顯示篩選，不參與公式。"""
            meta=point_info(r)
            grp=str(meta.get("player_group") or meta.get("group") or "").strip()
            zone=str(meta.get("player_group") or meta.get("zone") or "").strip()
            strategy=str(meta.get("strategy") or "").strip()
            keys={x for x in (grp,zone) if x}
            if bool(meta.get("scattered")) or strategy=="零散" or grp=="零散":
                keys.add("零散")
            if grp in ("G","高G") or zone in ("G","高G") or str(r.get("point_id") or "").startswith(("POINT_G_","POINT_HIGHG_")):
                keys.update(("G","高G" if "高G" in (grp,zone) else "G"))
            if grp=="遠線" or zone=="遠線" or str(r.get("point_id") or "").startswith("POINT_REMOTE_"):
                keys.add("遠線")
            return keys

        def special_tab_matches(r,mode):
            if mode=="其他":
                # 動態兜底：沒有命中任何玩家自訂特殊分頁的項目都集中在這裡。
                return not any(special_tab_matches(r,name) for name in custom_special_names)
            rec=next((x for x in special_definitions if str(x.get("name") or "")==mode),None)
            if rec is None:return False
            wanted={str(x).strip() for x in (rec.get("groups") or []) if str(x).strip()}
            return bool(wanted.intersection(special_group_keys(r)))

        def mode_count(mode):
            c=0
            for r in usable:
                try:a=int(r.get("input_stage") or 0);b=int(r.get("output_stage") or 0)
                except:a=b=0
                if mode=="全部":ok=True
                elif mode=="特殊":ok=not is_normal_stage(r)
                else:
                    nums=re.findall(r'\d+',mode)
                    ok=len(nums)>=2 and a==int(nums[0]) and b==int(nums[1])
                if ok:c+=1
            return c

        for mode in modes:
            text=f"{mode}\n{mode_count(mode)} 筆"
            rb=tk.Radiobutton(tabs,text=text,variable=stage_var,value=mode,indicatoron=False,
                              font=(FONT,11,"bold"),padx=10,pady=7,bg="#fffef9",selectcolor=CUTE_GREEN_SOFT,
                              activebackground=CUTE_GREEN_SOFT,relief="flat",bd=0)
            rb.pack(side="left",fill="x",expand=True,padx=2)
            tab_buttons[mode]=rb

        special_tabs=tk.Frame(outer,bg=CUTE_BLUE_SOFT)
        special_buttons={}
        def special_count(mode):
            special_rows=[r for r in usable if not is_normal_stage(r)]
            if mode=="全部特殊":return len(special_rows)
            return sum(1 for r in special_rows if special_tab_matches(r,mode))
        for mode in ["全部特殊"]+special_names:
            rb=tk.Radiobutton(special_tabs,text=f"{mode}  {special_count(mode)}",
                              variable=special_var,value=mode,indicatoron=False,
                              font=(FONT,11,"bold"),padx=14,pady=6,bg="#fffef9",selectcolor="#dcecff",relief="flat",bd=0)
            rb.pack(side="left",padx=3,pady=4)
            special_buttons[mode]=rb

        def sync_special_tabs(*_):
            if stage_var.get()=="特殊":
                if not special_tabs.winfo_ismapped():
                    special_tabs.pack(fill="x",padx=18,pady=(0,4),after=tabs)
            else:
                if special_tabs.winfo_ismapped():
                    special_tabs.pack_forget()


        table=tk.Frame(outer,bg="#fffef9",bd=0,relief="flat",padx=4,pady=4);table.pack(fill="both",expand=True,padx=18,pady=5)
        cols=("選","交換地點","交換材料","庫存","→","目標物","次數","折後單次","本列消耗","原圖")
        widths=(5,16,19,8,3,19,8,10,10,7)
        # V5.47：標題不再放在另一個獨立 Frame。
        # 標題和資料列如果各自 grid，Tk 會依各自內容重新分配欄寬，造成「庫存」標題與數字錯位。
        cv=tk.Canvas(table,bg="white",highlightthickness=0)
        sb=tk.Scrollbar(table,orient="vertical",command=cv.yview);cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y");cv.pack(side="left",fill="both",expand=True)
        rows_frame=tk.Frame(cv,bg="white");wid=cv.create_window((0,0),window=rows_frame,anchor="nw")
        rows_frame.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(wid,width=e.width))
        def _picker_wheel(e):
            try:
                cv.yview_scroll(-3 if e.delta>0 else 3,"units");return "break"
            except Exception:pass
        win.bind("<MouseWheel>",_picker_wheel,add="+")
        win.bind("<Destroy>",lambda e:save_page_state("route_picker",search=search_var.get(),
                 scroll=cv.yview()[0] if cv.yview() else 0) if e.widget is win else None,add="+")

        # V5.47：今日交換選擇是工作狀態，不因進路線卡/回來就清空。
        _picker_state=self.state_data.setdefault("route_picker",{})
        selected=dict(_picker_state.get("selected") or {})
        selected_times=dict(_picker_state.get("selected_times") or {})
        visible=[]
        def rkey(r):
            return "|".join(str(r.get(k) or "") for k in ("point_id","input_item_id","output_item_id","source_file","row_index"))
        def save_picker_state():
            self.state_data["route_picker"]={
                "selected":{str(k):bool(v) for k,v in selected.items() if v},
                "selected_times":{str(k):int(v) for k,v in selected_times.items()},
                "stage":stage_var.get(),
                "special":special_var.get(),
                "power_cap":current_cap(),
            }
            STORE.save(self.state_data)

        def template_identity(r):
            """跨每日OCR批次的穩定範本身份：不記 source_file/row_index，只記遊戲身份。"""
            return barter_template_identity(r)

        def template_stage_pair(r):
            return barter_stage_pair(r)

        def save_default_template():
            picked=[r for r in usable if selected.get(rkey(r),False)]
            if not picked:
                messagebox.showinfo("預設範本","目前沒有勾選任何交換，沒有可儲存的範本。",parent=win); _keep_picker_front()
                return
            tpl=build_barter_rule_template(usable,lambda r:selected.get(rkey(r),False))
            full_pairs=tpl["stage_pairs"];exact_entries=tpl["entries"]
            self.state_data["default_barter_template"]=tpl
            STORE.save(self.state_data)
            ruletext="、".join(x.replace("->","→")+"階全選" for x in sorted(full_pairs)) or "無整階全選"
            messagebox.showinfo("預設範本",f"✓ 已儲存選擇規則：{ruletext}\n另保留 {len(exact_entries)} 個固定點位。\n\n新一刷會在這些點位套用當天的新物品。",parent=win); _keep_picker_front()

        def apply_default_template():
            tpl=self.state_data.get("default_barter_template") or {}
            entries=tpl.get("entries") or []
            stage_pairs={str(x) for x in tpl.get("stage_pairs",[]) if str(x)}
            if not entries and not stage_pairs:
                messagebox.showinfo("預設範本","目前還沒有預設範本。\n先勾好你平常固定要跑的項目，再按「💾 儲存為預設範本」。",parent=win); _keep_picker_front()
                return
            matched=0;matched_points=set()
            for r in usable:
                if barter_rule_matches(tpl,r):
                    k=rkey(r)
                    selected[k]=True
                    if k not in selected_times:selected_times[k]=max_times(r)
                    matched+=1
                    matched_points.add(str(r.get("point_id") or ""))
            save_picker_state()
            refresh()
            extra=""
            if int(tpl.get("version") or 1)<2 and matched==0:
                extra="\n\n這是舊式完整品項範本；請手動勾選一次後重新儲存，之後就會按階段套用。"
            wanted={str(x.get("point_id") or "") for x in entries if isinstance(x,dict) and x.get("point_id")}
            missing=sorted(wanted-matched_points)
            miss_text=("\n本輪沒出現的固定點位："+"、".join(missing)) if missing else ""
            messagebox.showinfo("套用預設範本",f"✓ 今天這批資料找到並勾選 {matched} 筆。\n階段規則與固定點位都會使用今天最新品項。"+miss_text+extra,parent=win); _keep_picker_front()

        def alias_match(q,r):
            if not q:return True
            q=q.strip().lower()

            # V5.49：玩家交換點群組直接用這筆的 POINT_ID 查玩家設定。
            # 不再繞 canonical alias 集合，避免拆戶籍後 X/Z/Q/G 搜尋失聯。
            _pid=str(r.get("point_id") or "")
            _pgroup=str(player_point_groups.get(_pid) or "").strip().lower()
            if _pgroup and q in _pgroup:
                return True
            _ppref=_player_point_prefs.get(_pid,{}) if isinstance(_player_point_prefs,dict) else {}
            if isinstance(_ppref,dict):
                _legacy_group=str(_ppref.get("zone") or "").strip().lower()
                if _legacy_group and q in _legacy_group:
                    return True

            if any(q in str(r.get(k) or "").lower() for k in ("point_name","input_name","output_name","point_id","input_item_id","output_item_id")):return True
            def hit(targets,*vals):
                if not isinstance(targets,(list,tuple,set)):targets=[targets]
                ts={str(x).lower() for x in targets}
                return any(str(v or "").lower() in ts for v in vals)
            for amap,vals in ((item_aliases,(r.get("input_name"),r.get("input_item_id"))),
                              (item_aliases,(r.get("output_name"),r.get("output_item_id"))),
                              (point_aliases,(r.get("point_name"),r.get("point_id")))):
                for aa,targets in amap.items():
                    if q in str(aa).lower() and hit(targets,*vals):return True
            for aa,targets in canonical_item_alias.items():
                if q in aa.lower() and (hit(targets,r.get("input_name"),r.get("input_item_id")) or hit(targets,r.get("output_name"),r.get("output_item_id"))):return True
            for aa,targets in canonical_point_alias.items():
                if q in aa.lower() and hit(targets,r.get("point_name"),r.get("point_id")):return True
            return False

        def stage_ok(r):
            mode=stage_var.get()
            try:a=int(r.get("input_stage") or 0);b=int(r.get("output_stage") or 0)
            except:a=b=0
            if mode=="全部":return True
            if mode=="特殊":
                if is_normal_stage(r):return False
                sub=special_var.get()
                return sub=="全部特殊" or special_tab_matches(r,sub)
            nums=re.findall(r'\d+',mode)
            return is_normal_stage(r) and len(nums)>=2 and a==int(nums[0]) and b==int(nums[1])

        def exchange_remaining_times(r):
            try:return max(1,int(r.get("remaining_times") or 1))
            except:return 1

        def planned_input_available(r):
            """目前庫存＋已勾選前段交換預計產出；用本輪OCR實際數量計算。"""
            return projected_selected_input_available(r,usable,selected,selected_times,load_inventory(),rkey)

        def stock_exchange_capacity(r):
            """依目前庫存與已勾選交換鏈產出，算這筆最多能做幾次。未知庫存不限制。"""
            available,_planned=planned_input_available(r)
            if available is None:return None
            try:need=max(1,int(r.get("input_quantity") or 1))
            except Exception:need=1
            return max(0,available//need)

        def max_times(r):
            remain=exchange_remaining_times(r)
            cap=stock_exchange_capacity(r)
            return remain if cap is None else max(0,min(remain,cap))

        # 舊資料曾把持有量當成單次需求，並把錯誤的1/6存進 selected_times。
        # 本次修復時同步恢復成這筆目前真正可選的上限；之後玩家仍可自由調低。
        if _stage_input_repaired:
            for _r in _stage_input_repaired:
                selected_times[rkey(_r)]=max_times(_r)
            save_picker_state()

        def chosen_times(r):
            k=rkey(r)
            mx=max_times(r)
            return max(0,min(mx,int(selected_times.get(k,mx))))

        def cost(r):
            return int(round(discounted_single_power(r)*chosen_times(r)))

        def single_cost(r):
            return int(round(discounted_single_power(r)))

            return int(round(raw_negotiation_power(r)))
        def update_summary():
            picked=[r for r in usable if selected.get(rkey(r),False)]
            total=sum(cost(r) for r in picked)
            cap=current_cap()

            # V5.64：剩餘交涉力是「實際帳」，只扣真正完成的站。
            # 勾選/排路線只是規劃，不得改變剩餘交涉力；
            # 撤銷完成則自然把該站交涉力還回來。
            actual_used=0
            for _rr in (self.state_data.get("routes") or []):
                for _st in (_rr.get("stations") or []):
                    if _st.get("status")!="完成":
                        continue
                    _out=str(_st.get("output") or "")
                    _base=HIGH_BARTER_BASE if _out=="烏鴉硬幣" else NORMAL_BARTER_BASE
                    try:_n=int(_st.get("times") or 1)
                    except:_n=1
                    actual_used += int(round(_base*mult*_n))

            rem=cap-actual_used
            rem_var.set(f"{rem:,} / {cap:,}")
            total_times=sum(chosen_times(r) for r in picked)
            sel_var.set(f"{len(picked)} 項 / {total_times} 次")
            use_var.set(f"{total:,}")
            power_diag_var.set(f"今日已實際消耗 {actual_used:,}")
            try:
                summary.winfo_children()[0].configure(fg="#b42318" if rem<0 else "black")
            except:pass
        def toggle(k):
            selected[k]=not selected.get(k,False)
            if selected[k] and k not in selected_times:
                rr=next((x for x in usable if rkey(x)==k),None)
                if rr is not None:selected_times[k]=exchange_remaining_times(rr)
            save_picker_state()
            refresh()

        def adjust_times(r,delta):
            k=rkey(r)
            remain=exchange_remaining_times(r)
            desired=int(selected_times.get(k,remain))
            selected_times[k]=max(0,min(remain,desired+delta))
            save_picker_state()
            refresh()

        def display_input_text(r):
            name=str(r.get("input_name") or "？");qty=r.get("input_quantity") or 1
            if str(r.get("input_kind") or "")=="段位品":return f"{name} ×{qty}" if PLAYER_SETTINGS.get("show_stage_input_qty",False) else name
            return f"{name} ×{qty}" if PLAYER_SETTINGS.get("show_nonstage_input_qty",True) else name

        def display_output_text(r):
            name=str(r.get("output_name") or "？");qty=r.get("output_quantity") or 1
            try:a=int(r.get("input_stage") or 0);b=int(r.get("output_stage") or 0)
            except:a=b=0
            normal=is_normal_stage(r)
            if normal and (a,b) in ((1,2),(2,3)):show=bool(PLAYER_SETTINGS.get("show_lowstage_output_qty",True))
            elif normal:show=bool(PLAYER_SETTINGS.get("show_highstage_output_qty",False))
            else:show=bool(PLAYER_SETTINGS.get("show_nonstage_output_qty",True))
            txt=f"{name} ×{qty}" if show else name
            if name=="烏鴉硬幣" and str(r.get("output_quantity") or "").isdigit() and int(r.get("output_quantity") or 0)<20:txt+="  ⚠"
            return txt

        def edit_output_quantity(r):
            cur=str(r.get("output_quantity") or "")
            val=simpledialog.askinteger(
                "修正本輪目標物數量",
                f"{r.get('point_name','')}\n{r.get('output_name','目標物')}目前讀到：{cur or '空白'}\n\n請照原圖輸入正確數量：",
                parent=win,minvalue=1,maxvalue=99999
            )
            if val is None:return
            r["output_quantity"]=val
            r["_manual_output_qty"]=True
            try:
                with open(CURRENT_BATCH_PATH,"w",encoding="utf-8") as _f:
                    json.dump(rows,_f,ensure_ascii=False,indent=2)
            except Exception:
                messagebox.showerror("儲存失敗","本輪數量已改在畫面，但寫回交換池失敗。",parent=win)
            refresh()

        def refresh(*_):
            sync_special_tabs()
            for w in rows_frame.winfo_children():w.destroy()

            # 同一個 grid 內畫標題，保證欄位永遠與資料對齊。
            for j,(c,widc) in enumerate(zip(cols,widths)):
                tk.Label(rows_frame,text=c,font=(FONT,11,"bold"),bg="#e5f1e2",fg=CUTE_GREEN,
                         width=widc,anchor="w",padx=7,pady=10).grid(row=0,column=j,sticky="ew")
                rows_frame.columnconfigure(j,weight=1 if j in (1,2,5) else 0)

            visible[:]=[r for r in usable if stage_ok(r) and alias_match(search_var.get().strip(),r)]
            for i,r in enumerate(visible, start=1):
                k=rkey(r);on=selected.get(k,False)
                bg=CUTE_GREEN_SOFT if on else ("#fffef9" if i%2==0 else "#f7faf6")
                n=exchange_remaining_times(r)
                _stock=load_inventory().get(str(r.get("input_item_id") or ""),None)
                _available,_chain_bonus=planned_input_available(r)
                vals=[
                    "☑" if on else "☐",
                    str(r.get("point_name") or "未知"),
                    display_input_text(r),
                    ("—" if _stock is None else
                     (f"{int(_stock):,} +{_chain_bonus:,}" if _chain_bonus>0 else f"{int(_stock):,}")),
                    "→",
                    display_output_text(r),
                    None,
                    f"{single_cost(r):,}",
                    f"{cost(r):,}",
                    "🖼 查看",
                ]
                for j,(v,widc) in enumerate(zip(vals,widths)):
                    if j==6:
                        cell=tk.Frame(rows_frame,bg=bg)
                        cell.grid(row=i,column=j,sticky="ew",padx=3,pady=4)
                        tk.Button(cell,text="－",font=(FONT,15,"bold"),width=2,bg="#edf2ec",relief="flat",
                                  command=lambda rr=r:adjust_times(rr,-1)).pack(side="left")
                        tk.Label(cell,text=f"{chosen_times(r)} / {n}",font=(FONT,15,"bold"),
                                 bg=bg,width=7,
                                 fg=("#b42318" if max_times(r)==0 else
                                     ("#356fb3" if planned_input_available(r)[1]>0 else CUTE_GREEN))).pack(side="left",padx=3)
                        tk.Button(cell,text="＋",font=(FONT,15,"bold"),width=2,bg="#dcefd8",fg=CUTE_GREEN,relief="flat",
                                  command=lambda rr=r:adjust_times(rr,1)).pack(side="left")
                        continue
                    lab=tk.Label(rows_frame,text=v,font=(FONT,15,"bold" if j in (0,1,2,5) else "normal"),
                                 bg=bg,width=widc,anchor="w",padx=7,pady=12)
                    lab.grid(row=i,column=j,sticky="ew")
                    if j==0:
                        lab.configure(fg=CUTE_GREEN if on else "#87918b",cursor="hand2")
                        lab.bind("<Button-1>",lambda e,kk=k:toggle(kk))
                    elif j==5:
                        lab.configure(cursor="hand2")
                        lab.bind("<Double-Button-1>",lambda e,rr=r:edit_output_quantity(rr))
                    elif j==9:
                        lab.configure(fg="#1f6feb",cursor="hand2")
                        lab.bind("<Button-1>",lambda e,rr=r,idx=i:self._show_original_evidence(rr,idx,win,rows=rows))
                for j in (1,2,5):rows_frame.columnconfigure(j,weight=1)
            update_summary()
        def select_page(value):
            for r in visible:
                k=rkey(r)
                selected[k]=value
                if value and k not in selected_times:
                    selected_times[k]=max_times(r)
            save_picker_state()
            refresh()

        def _keep_picker_front():
            # Modal 關閉後 Windows 有時會把 Toplevel 丟到主視窗後面。
            # 不設永久 topmost，只重新抬回原本層級與焦點。
            try:
                win.after_idle(win.lift)
                win.after_idle(win.focus_force)
            except Exception:pass

        def reset_all_selections():
            if not any(selected.values()):
                messagebox.showinfo("全部重置","目前沒有已勾選的交換。",parent=win)
                _keep_picker_front()
                return
            ok=messagebox.askyesno("全部重置","要把所有階段／特殊分類目前已勾選的點位全部取消嗎？\n\n預設範本不會被刪除。",parent=win)
            _keep_picker_front()
            if not ok:return
            selected.clear()
            selected_times.clear()
            save_picker_state()
            refresh()
            _keep_picker_front()

        def go_route_cards_direct():
            _routes=[r for r in (self.state_data.get("routes") or []) if r.get("stations")]
            if not _routes:
                messagebox.showinfo("路線卡","目前還沒有已儲存的今日路線。\n先按「排今天的路線」完成一次規劃。",parent=win)
                _keep_picker_front()
                return
            win.destroy()
            self.after(60,self.show_routes)

        toolbar=tk.Frame(outer,bg="#fffef9",padx=8,pady=5);toolbar.pack(fill="x",padx=18,pady=6)
        _rpps=get_page_state("route_picker")
        search_var=tk.StringVar(value=str(_rpps.get("search") or ""))
        tk.Label(toolbar,text="🔎 搜尋",font=(FONT,12,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left")
        tk.Entry(toolbar,textvariable=search_var,font=(FONT,14),width=25,relief="flat",highlightthickness=1,
                 highlightbackground=CUTE_BORDER).pack(side="left",padx=7,pady=3)
        tk.Button(toolbar,text="💾 儲存為預設範本",font=(FONT,11,"bold"),
                  command=save_default_template,padx=10,pady=4).pack(side="left",padx=(10,3))
        tk.Button(toolbar,text="⭐ 套用預設範本",font=(FONT,11,"bold"),
                  command=apply_default_template,padx=10,pady=4).pack(side="left",padx=3)
        def _set_mode_here(mode):
            if mode=="formal":
                if not messagebox.askyesno("切換正式模式","正式模式下，完成／撤銷路線會直接修改正式庫存。\n\n確定切換？",parent=win):return
            save_inventory_mode(mode)
            if mode=="test":ensure_test_inventory(reset=False)
            messagebox.showinfo("庫存模式","已切換為 "+("🧪 測試模式" if mode=="test" else "🟢 正式模式")+"。\n重新進入頁面後標示會更新。",parent=win)
        tk.Button(toolbar,text="🧪 測試庫存",font=(FONT,10,"bold"),
                  command=lambda:_set_mode_here("test"),padx=8,pady=4).pack(side="left",padx=2)
        tk.Button(toolbar,text="🟢 正式庫存",font=(FONT,10,"bold"),
                  command=lambda:_set_mode_here("formal"),padx=8,pady=4).pack(side="left",padx=2)
        tk.Button(toolbar,text="📦 庫存管理",font=(FONT,11,"bold"),
                  command=lambda:self.open_inventory_manager(win,usable,refresh),padx=10,pady=4).pack(side="left",padx=3)
        tk.Button(toolbar,text="⟲ 全部重置",font=(FONT,11,"bold"),command=reset_all_selections,padx=12,pady=4).pack(side="right",padx=(8,0))
        tk.Button(toolbar,text="✓ 全選本頁",font=(FONT,11,"bold"),command=lambda:select_page(True),padx=12,pady=4).pack(side="right",padx=4)
        tk.Button(toolbar,text="× 全不選本頁",font=(FONT,11),command=lambda:select_page(False),padx=12,pady=4).pack(side="right")


        foot=tk.Frame(outer,bg="#fffef9",padx=8,pady=7);foot.pack(fill="x",padx=18,pady=(6,12))
        tk.Label(foot,text="💡 先選你今天想跑的交換；路線順序、拆趟與負重下一步再處理。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(side="left")
        def make_draft():
            _existing=self.state_data.get("routes") or []
            if any(any(st.get("status","未開始")!="未開始" for st in r.get("stations",[])) for r in _existing):
                win.destroy()
                self.after(60,self.show_routes)
                return
            picked=[r for r in usable if selected.get(rkey(r),False)]
            if not picked:
                messagebox.showinfo("還沒選","先勾這趟想跑的交換。");return
            stations=[]
            skipped_no_stock=[]
            for r in picked:
                times=chosen_times(r)
                if times<=0:
                    skipped_no_stock.append(r.get("point_name") or "未知點位")
                    continue
                stations.append({"point":r.get("point_name") or "未知點位","point_id":r.get("point_id") or "",
                    "input":r.get("input_name") or "？","input_id":r.get("input_item_id") or "",
                    "input_qty":r.get("input_quantity") or 1,"input_stage":r.get("input_stage"),
                    "output":r.get("output_name") or "？","output_id":r.get("output_item_id") or "",
                    "output_qty":r.get("output_quantity") or 1,"output_stage":r.get("output_stage"),
                    "times":times,"status":"未開始","batch_id":self.state_data.get("batch_id") or ""})
            # V5.47 第一顆正式分類腦：依點位主表的 zone/strategy 拆卡。
            # 這裡只做「分類」，尚不宣稱站序是最佳航線。
            if not stations:
                messagebox.showinfo("目前沒有可執行交換",
                                    "已勾選項目的庫存可交換次數都是 0。\n先補庫存或調整選擇後再建立路線。",
                                    parent=win)
                return
            executable_picked=[r for r in picked if chosen_times(r)>0]
            buckets={}
            for st,r in zip(stations,executable_picked):
                meta=point_info(r)
                zone=str(meta.get("group") or "")
                scattered=bool(meta.get("scattered")) or str(meta.get("strategy") or "")=="零散"
                if scattered: group="零散"
                elif zone in ("G","高G") or str(r.get("point_id") or "").startswith(("POINT_G_","POINT_HIGHG_")): group="G"
                elif zone=="高Z": group="高Z"
                elif zone=="遠線": group="遠線"
                elif zone=="大洋": group="大洋"
                else: group="內海"
                st["route_group"]=group
                st["source_file"]=r.get("source_file") or ""
                st["row_index"]=r.get("row_index")
                buckets.setdefault(group,[]).append((meta.get("_order",99999),st))
            order=["G","高Z","遠線","大洋","內海","零散"]
            routes=[]
            for group in order:
                if group not in buckets: continue
                sts=[x[1] for x in sorted(buckets[group],key=lambda x:x[0])]
                routes.append({"name":group,"status":"尚未開始","current_index":0,"stations":sts,"source":"V5.47_CLASSIFIER"})
            # V5.53.1：今日選擇變更採「增量更新」；已排好的路線/站序不洗掉。
            # identity 只用今日交換的穩定身份，不把 times 算進去，次數變更直接更新原站。
            def _sid(st):
                # 人工改正名稱／ID後仍是同一張圖同一列；優先用證物身份保留原路線位置。
                source=os.path.basename(str(st.get("source_file") or ""));row=str(st.get("row_index") or "")
                if source and row:return ("evidence",source,row)
                return ("identity",str(st.get("point_id") or ""),str(st.get("input_id") or ""),str(st.get("output_id") or ""))
            new_by_id={_sid(st):st for st in stations}
            old_routes=self.state_data.get("routes",[]) or []
            old_loose=self.state_data.get("unplanned_stations",[]) or []
            kept_ids=set()
            merged_routes=[]
            for _or in old_routes:
                nr=copy.deepcopy(_or); nr["stations"]=[]
                for _ost in _or.get("stations",[]):
                    sid=_sid(_ost)
                    if sid in new_by_id:
                        fresh=copy.deepcopy(new_by_id[sid])
                        # 已完成站的交易已按舊 ID 入帳；不可在背後換 ID 造成帳本斷鏈。
                        if _ost.get("status")=="完成" and (str(_ost.get("input_id") or ""),str(_ost.get("output_id") or ""))!=(str(fresh.get("input_id") or ""),str(fresh.get("output_id") or "")):
                            fresh=copy.deepcopy(_ost)
                        # V5.55.2：保留執行生命週期，不得因回今日交換/重進排路線而重生。
                        for _k in ("status","inventory_tx_id","completed_at","skipped_at"):
                            if _k in _ost:
                                fresh[_k]=_ost[_k]
                        nr["stations"].append(fresh); kept_ids.add(sid)
                if nr["stations"]:merged_routes.append(nr)
            merged_loose=[]
            for _ost in old_loose:
                sid=_sid(_ost)
                if sid in new_by_id and sid not in kept_ids:
                    merged_loose.append(copy.deepcopy(new_by_id[sid]));kept_ids.add(sid)
            for sid,st in new_by_id.items():
                if sid not in kept_ids:merged_loose.append(copy.deepcopy(st))

            # 交換鏈導航：由本筆 output_item 沿今天交換池往後追，找到 output_stage=7 的點位。
            # 只記「今天」的鏈，不寫進官方資料庫。
            rows_by_input={}
            for rr in usable:
                rows_by_input.setdefault(str(rr.get("input_item_id") or ""),[]).append(rr)
            def _stage_num(v):
                m=re.search(r"([1-7])",str(v or ""))
                return int(m.group(1)) if m else None
            def _chain7(st):
                start=str(st.get("output_id") or "")
                seen=set();front=[start];ends=[]
                for _ in range(8):
                    nxt=[]
                    for iid in front:
                        if not iid or iid in seen:continue
                        seen.add(iid)
                        for rr in rows_by_input.get(iid,[]):
                            if _stage_num(rr.get("output_stage"))==7:
                                ends.append({"point":rr.get("point_name") or "未知",
                                             "point_id":str(rr.get("point_id") or "")})
                            else:
                                oo=str(rr.get("output_item_id") or "")
                                if oo and oo not in seen:nxt.append(oo)
                    front=nxt
                    if not front:break
                # 去重
                out=[];seenp=set()
                for x in ends:
                    k=(x["point_id"],x["point"])
                    if k not in seenp:seenp.add(k);out.append(x)
                return out
            for st in list(merged_loose)+[x for rr in merged_routes for x in rr.get("stations",[])]:
                st["chain7"]=_chain7(st)

            self.state_data["routes"]=merged_routes
            self.state_data["unplanned_stations"]=merged_loose
            if not old_routes and not old_loose:
                self.state_data["route_planner_baseline"]={"routes":[],"unplanned_stations":copy.deepcopy(merged_loose)}
            self.state_data["status"]="路線待編排";STORE.save(self.state_data)
            win.destroy()
            self.after(60,self.show_route_planner)
        tk.Button(foot,text="排今天的路線  →",font=(FONT,16,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  activebackground="#0f5b31",activeforeground="white",command=make_draft,padx=27,pady=10).pack(side="right")
        tk.Button(foot,text="🧭 路線卡",font=(FONT,14,"bold"),
                  command=go_route_cards_direct,padx=18,pady=9).pack(side="right",padx=(0,8))

        def _tab_changed(*_):
            save_picker_state();refresh()
        stage_var.trace_add("write",_tab_changed)
        special_var.trace_add("write",_tab_changed)
        search_var.trace_add("write",refresh)
        sync_special_tabs()
        refresh()
        try:win.after(120,lambda:cv.yview_moveto(float(_rpps.get("scroll",0) or 0)))
        except:pass

    def show_route_planner(self):
        """V5.55.2｜自由排路線：增量保存、執行狀態保護與玩家範本。"""
        routes=self.state_data.get("routes",[])
        loose=self.state_data.get("unplanned_stations",[])
        if not routes and not loose:
            messagebox.showinfo("排路線","目前沒有可規劃站點。先到「今日交換選擇」勾今天要跑的交換。",parent=self)
            self.show_route_picker();return
        # V6.1修復：只當有「進行中」的站點時才不允許調整，允許在部分站點「完成」後調整後面的「未開始」站點
        if any(any(st.get("status","未開始")=="進行中" for st in r.get("stations",[])) for r in routes):
            messagebox.showinfo("路線進行中","目前有路線正在執行中，請先完成或暫停當前路線後再調整。",parent=self)
            self.show_routes();return

        original_routes=copy.deepcopy(routes);original_loose=copy.deepcopy(loose)
        ui=load_player_route_ui()
        show_alias=tk.BooleanVar(value=bool(ui.get("show_point_alias",False)))
        show_colors=tk.BooleanVar(value=bool(ui.get("show_stage_colors",True)))
        skip_learning=tk.BooleanVar(value=bool(self.state_data.get("route_learning_skip_batch",False)))
        stage_colors=ui.get("stage_colors",{})

        # 排路線的「自訂縮寫」優先讀既有玩家點位分組/偏好：
        # 也就是玩家已經用了很久的 X / Z / Q / G ... 那套，不要求重建第二份。
        _route_groups=load_player_point_groups()
        _route_prefs=load_player_point_preferences()
        try:
            with open(SEARCH_ALIAS_PATH,"r",encoding="utf-8") as f:_sa=json.load(f)
            _pa=_sa.get("points",{}) if isinstance(_sa,dict) else {}
        except Exception:_pa={}
        def point_alias(st):
            pid=str(st.get("point_id") or "");name=str(st.get("point") or "")
            # 1. 玩家點位分組：X/Z/Q/G/古...（目前排路線最主要的縮寫來源）
            g=str(_route_groups.get(pid) or "").strip()
            if g:return g
            # 2. 舊玩家點位偏好 zone，兼容既有資料。
            pref=_route_prefs.get(pid,{})
            if isinstance(pref,dict):
                g=str(pref.get("zone") or "").strip()
                if g:return g
            # 3. 最後才退回搜尋別名。
            hits=[]
            for a,targets in _pa.items():
                vals=targets if isinstance(targets,list) else [targets]
                if any(str(v)==pid or str(v)==name for v in vals):
                    aa=str(a).strip()
                    if aa and aa!=name:hits.append(aa)
            return min(hits,key=lambda x:(len(x),x)) if hits else ""

        def stage_of(st):
            for v in (st.get("output_stage"),st.get("input_stage")):
                m=re.search(r"([1-7])",str(v or ""))
                if m:return m.group(1)
            return ""

        def st_label(st,i=None):
            p=f"{i+1:02d}. " if i is not None else ""
            name=str(st.get("point") or "未知")
            al=point_alias(st) if show_alias.get() else ""
            if al:name=al
            chain=st.get("chain7") or []
            ctext=""
            if chain:
                names="／".join(str(x.get("point") or "未知") for x in chain)
                ctext=f"  🔗7階:{names}"
            return f"{p}{name} ｜ {st.get('input','？')} → {st.get('output','？')} ×{int(st.get('times') or 1)}{ctext}"

        route_parent=self._clear_route_page("✏ 路線工作台")
        # 路線工作台本身是副視窗；本頁小視窗若掛在首頁下，Windows 會把它壓到工作台後面。
        def keep_planner_front():
            self._pulse_window_front(route_parent)
        outer=tk.Frame(route_parent,bg=CUTE_BG);outer.pack(fill="both",expand=True)
        head=tk.Frame(outer,bg="#fffef9");head.pack(fill="x",padx=18,pady=(10,5))
        add_mascot(head,size=68,opacity=0.14,quip_key="planner")
        tk.Label(head,text="✏ 路線工作台",font=(FONT,27,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left")
        tk.Label(head,text="　排法自動保存；回今日交換補/刪點，只增量更新，不再拆掉已排路線。",
                 font=(FONT,11),bg="#fffef9",fg="#607168").pack(side="left",pady=(9,0))

        toolbar=tk.Frame(outer,bg=CUTE_GREEN_SOFT,padx=8,pady=5);toolbar.pack(fill="x",padx=18,pady=(3,6))
        tk.Checkbutton(toolbar,text="顯示自訂縮寫",variable=show_alias,bg=CUTE_GREEN_SOFT,selectcolor=CUTE_GREEN_SOFT,font=(FONT,12,"bold")).pack(side="left")
        tk.Checkbutton(toolbar,text="顯示階段顏色",variable=show_colors,bg=CUTE_GREEN_SOFT,selectcolor=CUTE_GREEN_SOFT,font=(FONT,12,"bold")).pack(side="left",padx=8)
        learn_status=tk.StringVar()
        def persist_skip_learning(*_):
            self.state_data["route_learning_skip_batch"]=bool(skip_learning.get());STORE.save(self.state_data)
            enabled=bool(PLAYER_SETTINGS.get("route_learning_enabled",True))
            learn_status.set("🦭 這批不學" if skip_learning.get() else ("🦭 儲存時會學" if enabled else "🦭 自動學習已關閉"))
        tk.Checkbutton(toolbar,text="這一批不要讓海豹學",variable=skip_learning,bg=CUTE_GREEN_SOFT,
                       selectcolor=CUTE_GREEN_SOFT,font=(FONT,11,"bold")).pack(side="left",padx=8)
        tk.Label(toolbar,textvariable=learn_status,font=(FONT,10,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).pack(side="right",padx=6)
        skip_learning.trace_add("write",persist_skip_learning);persist_skip_learning()

        def open_time_optimization_review():
            rows=load_route_timing_history();reports=[(r,suggest_time_optimized_order(r,rows)) for r in routes if r.get("stations")]
            w=tk.Toplevel(route_parent);w.title("🧭 航段省時體檢");w.transient(route_parent)
            smart_window(w,"route_time_optimization_review","820x700",680,520);w.configure(bg=CUTE_BG)
            close_review=self._handoff_modal_grab(w)
            hero=tk.Frame(w,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=16,pady=(14,7))
            tk.Label(hero,text="🧭 航段省時體檢",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(hero,text="固定第一站、不跨線搬站、不破壞交換鏈；只有按下安全套用才會改排法。",
                     font=(FONT,11),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(2,0))
            foot_review=tk.Frame(w,bg=CUTE_BG);foot_review.pack(side="bottom",fill="x",padx=16,pady=(2,14))
            tk.Button(foot_review,text="關閉",font=(FONT,11,"bold"),command=close_review,padx=18,pady=7).pack(side="right")
            suggestions=sum(1 for _route,report in reports if report.get("status")=="suggestion")
            def apply_time_suggestions():
                nonlocal routes
                if not suggestions:
                    messagebox.showinfo("目前不用調整","這次沒有可安全套用的省時建議。",parent=w);return
                if not messagebox.askyesno("安全套用省時建議",
                    f"要重排 {suggestions} 條完全未執行的路線嗎？\n\n只改各線內站序；不跨線、不掉站、不改交換品，也不碰庫存。",parent=w):return
                result=apply_time_optimized_orders_safely(routes,reports)
                if not result.get("ok"):
                    messagebox.showerror("已取消套用",result.get("reason") or "安全檢查未通過。",parent=w);return
                routes=result["routes"];autosave();refresh_routes();close_review()
                route_parent.after(40,lambda:messagebox.showinfo("海豹排好了",
                    f"✓ 已安全重排 {result['changed']} 條路線。\n請照平常方式檢查後，再按「儲存今日排法」。",parent=route_parent))
            tk.Button(foot_review,text=f"✓ 安全套用建議（{suggestions}）",font=(FONT,12,"bold"),
                      bg=CUTE_GREEN,fg="white",relief="flat",command=apply_time_suggestions,
                      padx=18,pady=7,state=("normal" if suggestions else "disabled")).pack(side="right",padx=6)
            shell=tk.Frame(w,bg="#fffef9");shell.pack(fill="both",expand=True,padx=16,pady=7)
            txt=tk.Text(shell,font=(FONT,12),wrap="word",bg="#fffef9",fg=CUTE_TEXT,relief="flat",padx=14,pady=12)
            sb=tk.Scrollbar(shell,orient="vertical",command=txt.yview);txt.configure(yscrollcommand=sb.set)
            txt.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
            ship=active_ship_profile();cap=float(ship.get("capacity") or 0)
            for i,(route,report) in enumerate(reports,1):
                txt.insert("end",f"{i}. {route.get('name') or '路線'}\n","title")
                if report["status"]=="suggestion":
                    names=" → ".join(str(x.get("point") or "未知") for x in report["stations"])
                    txt.insert("end",f"   💡 已知站內航段可少約 {format_route_duration(report['saving_seconds'])}（{report['saving_percent']:.0f}%）\n")
                    txt.insert("end",f"   建議順序：{names}\n")
                elif report["status"]=="no_improvement":txt.insert("end","   ✓ 目前順序已是已知資料中的最佳或同分解。\n")
                else:txt.insert("end",f"   ○ 暫不建議：{report.get('reason','資料不足')}。\n")
                wp=route_peak_weight(route);peak=float(wp.get("peak_weight") or 0);unknown=wp.get("unknown_items") or []
                if cap>0 and peak>cap:txt.insert("end",f"   ⚠ {ship['name']} 已知最高 {peak:,.0f}/{cap:,.0f} LT；超重只提醒。\n")
                elif unknown:txt.insert("end",f"   ⚖ 另有 {len(unknown)} 種物品缺重量，不能宣稱安全。\n")
                txt.insert("end","\n")
            if not reports:txt.insert("end","目前沒有可體檢的路線。")
            txt.tag_configure("title",font=(FONT,15,"bold"),foreground=CUTE_GREEN);txt.configure(state="disabled")
        if bool(PLAYER_SETTINGS.get("ui_show_seal_features",True)):
            tk.Button(toolbar,text="🧭 航段省時體檢",font=(FONT,10,"bold"),bg="#fffef9",relief="flat",
                      command=open_time_optimization_review,padx=10,pady=5).pack(side="right",padx=5)

        body=tk.Frame(outer,bg=CUTE_BG);body.pack(fill="both",expand=True,padx=18,pady=6)
        poolf=tk.Frame(body,bg="#fffef9",bd=0,padx=12,pady=11);poolf.pack(side="left",fill="both",expand=True,padx=(0,7))
        mid=tk.Frame(body,bg=CUTE_YELLOW_SOFT,bd=0,padx=12,pady=11);mid.pack(side="left",fill="y",padx=7)
        right=tk.Frame(body,bg="#fffef9",bd=0,padx=12,pady=11);right.pack(side="left",fill="both",expand=True,padx=(7,0))
        pool_count_var=tk.StringVar();route_count_var=tk.StringVar();station_count_var=tk.StringVar()
        tk.Label(poolf,text="① 未規劃交換",font=(FONT,17,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(poolf,textvariable=pool_count_var,font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(0,7))
        tk.Label(mid,text="② 我的路線",font=(FONT,17,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f").pack(anchor="w")
        tk.Label(mid,textvariable=route_count_var,font=(FONT,11,"bold"),bg=CUTE_YELLOW_SOFT,fg="#78622f").pack(anchor="w",pady=(0,7))
        tk.Label(right,text="③ 站點順序",font=(FONT,17,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
        tk.Label(right,textvariable=station_count_var,font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(0,7))
        pool=tk.Listbox(poolf,font=(FONT,16),height=18,exportselection=False,selectmode="extended",
                        bd=0,highlightthickness=0,selectbackground="#bfe2b9",selectforeground=CUTE_TEXT,activestyle="none");pool.pack(fill="both",expand=True)
        rlist=tk.Listbox(mid,font=(FONT,15,"bold"),width=21,height=18,exportselection=False,
                         bd=0,highlightthickness=0,selectbackground="#ead99a",selectforeground=CUTE_TEXT,activestyle="none");rlist.pack(fill="y",expand=True)
        slist=tk.Listbox(right,font=(FONT,16),height=18,exportselection=False,selectmode="extended",cursor="fleur",
                         bd=0,highlightthickness=0,selectbackground="#bfe2b9",selectforeground=CUTE_TEXT,activestyle="none");slist.pack(fill="both",expand=True)

        def paint(lb,seq):
            if not show_colors.get():return
            for i,st in enumerate(seq):
                c=stage_colors.get(stage_of(st))
                if c:
                    try:lb.itemconfig(i,background=c)
                    except Exception:pass

        def autosave():
            self.state_data["routes"]=routes;self.state_data["unplanned_stations"]=loose
            self.state_data["status"]="路線待編排"
            STORE.save(self.state_data)

        def refresh_pool():
            pool.delete(0,"end")
            for st in loose:pool.insert("end",st_label(st))
            pool_count_var.set(f"{len(loose)} 個站點｜可複選後加入路線")
            paint(pool,loose)

        def ri():
            x=rlist.curselection();return int(x[0]) if x else None

        def refresh_routes(select=None):
            rlist.delete(0,"end")
            for i,r in enumerate(routes):rlist.insert("end",f"{i+1:02d}. {r.get('name') or '未命名'} ({len(r.get('stations',[]))}站)")
            route_count_var.set(f"{len(routes)} 條路線")
            if routes:
                j=0 if select is None else max(0,min(int(select),len(routes)-1))
                rlist.selection_set(j);rlist.activate(j)
            refresh_stations()

        def refresh_stations(select=None):
            slist.delete(0,"end");j=ri()
            if j is None:station_count_var.set("先選擇一條路線");return
            sts=routes[j].get("stations",[])
            station_count_var.set(f"{len(sts)} 個站點｜按住拖曳即可換位")
            for i,st in enumerate(sts):slist.insert("end",st_label(st,i))
            paint(slist,sts)
            if sts:
                k=0 if select is None else max(0,min(int(select),len(sts)-1))
                slist.selection_set(k);slist.activate(k)

        rlist.bind("<<ListboxSelect>>",lambda e:refresh_stations())

        def persist_ui(*_):
            ui["show_point_alias"]=bool(show_alias.get());ui["show_stage_colors"]=bool(show_colors.get());ui["stage_colors"]=stage_colors
            save_player_route_ui(ui);refresh_pool();refresh_stations()
        show_alias.trace_add("write",persist_ui);show_colors.trace_add("write",persist_ui)

        def edit_colors():
            keep_planner_front()
            w=tk.Toplevel(route_parent);w.title("🎨 階段顏色");w.transient(route_parent);w.grab_set()
            self._pulse_window_front(w)
            remember_window(w,"stage_color_settings","420x430")
            tk.Label(w,text="🎨 玩家階段顏色",font=(FONT,18,"bold")).pack(pady=(14,8))
            box=tk.Frame(w);box.pack(fill="both",expand=True,padx=18)
            def choose(stg,btn):
                c=colorchooser.askcolor(stage_colors.get(stg,"#FFFFFF"),title=f"{stg}階顏色",parent=w)[1]
                if c:stage_colors[stg]=c;btn.config(bg=c);persist_ui()
            for stg in map(str,range(1,8)):
                row=tk.Frame(box);row.pack(fill="x",pady=4)
                tk.Label(row,text=f"{stg} 階",font=(FONT,13,"bold"),width=8,anchor="w").pack(side="left")
                b=tk.Button(row,text=stage_colors.get(stg,""),bg=stage_colors.get(stg,"white"),font=(FONT,11),width=18)
                b.config(command=lambda x=stg,bb=b:choose(x,bb));b.pack(side="left")
            def close_colors():
                w.destroy();route_parent.after(30,keep_planner_front)
            w.protocol("WM_DELETE_WINDOW",close_colors)
            tk.Button(w,text="完成",font=(FONT,12,"bold"),command=close_colors,padx=18,pady=6).pack(pady=10)
        tk.Button(toolbar,text="🎨 設定階段顏色",font=(FONT,11,"bold"),bg="#fffef9",relief="flat",command=edit_colors).pack(side="left",padx=4)

        def new_route():
            keep_planner_front()
            name=simpledialog.askstring("新增路線","這條路線叫什麼？",parent=route_parent)
            keep_planner_front()
            if name is None:return
            routes.append({"name":name.strip() or f"路線 {len(routes)+1}","status":"尚未開始","current_index":0,"view_index":0,"stations":[],"source":"V5.53.1_MANUAL"})
            autosave();refresh_routes(len(routes)-1)

        def rename_route():
            j=ri()
            if j is None:return
            keep_planner_front()
            name=simpledialog.askstring("路線改名","新的名稱：",initialvalue=routes[j].get("name",""),parent=route_parent)
            keep_planner_front()
            if name and name.strip():routes[j]["name"]=name.strip();autosave();refresh_routes(j)

        def add_to_route():
            j=ri()
            if j is None:
                messagebox.showinfo("先建立路線","先按「＋ 新增路線」，再把站點放進去。",parent=route_parent);keep_planner_front();return
            idx=list(pool.curselection())
            if not idx:return
            chosen=[loose[i] for i in idx]
            for i in reversed(idx):loose.pop(i)
            routes[j].setdefault("stations",[]).extend(chosen)
            autosave();refresh_pool();refresh_routes(j)

        def single_routes():
            idx=list(pool.curselection())
            if not idx:return
            chosen=[loose[i] for i in idx]
            for i in reversed(idx):loose.pop(i)
            for st in chosen:
                name=point_alias(st) or str(st.get("point") or f"單點{len(routes)+1}")
                routes.append({"name":name,"status":"尚未開始","current_index":0,"view_index":0,
                               "stations":[st],"source":"V5.53.1_SINGLE"})
            autosave();refresh_pool();refresh_routes(max(0,len(routes)-1))

        def return_to_pool():
            j=ri()
            if j is None:return
            idx=list(slist.curselection())
            if not idx:return
            sts=routes[j].get("stations",[]);chosen=[sts[i] for i in idx]
            for i in reversed(idx):sts.pop(i)
            loose.extend(chosen);autosave();refresh_pool();refresh_routes(j)

        def move_station(delta):
            j=ri();sel=slist.curselection()
            if j is None or len(sel)!=1:return
            k=int(sel[0]);n=k+delta;sts=routes[j].get("stations",[])
            if not (0<=n<len(sts)):return
            sts[k],sts[n]=sts[n],sts[k];autosave();refresh_stations(n)

        drag={"current":None,"moved":False}
        def drag_start(e):
            if not slist.size():return "break"
            if getattr(e,"state",0)&0x0004:
                drag["current"]=None;drag["moved"]=False
                return None
            k=slist.nearest(e.y);drag["current"]=k;drag["moved"]=False
            slist.selection_clear(0,"end");slist.selection_set(k);slist.activate(k)
            return "break"
        def drag_motion(e):
            j=ri();cur=drag.get("current")
            if j is None or cur is None or not slist.size():return "break"
            end=slist.nearest(e.y);sts=routes[j].get("stations",[])
            if end!=cur and 0<=cur<len(sts) and 0<=end<len(sts):
                st=sts.pop(cur);sts.insert(end,st);drag["current"]=end;drag["moved"]=True
                refresh_stations(end);slist.see(end)
            return "break"
        def drag_end(e):
            end=drag.get("current");moved=drag.get("moved");drag["current"]=None;drag["moved"]=False
            if moved:autosave();refresh_stations(end)
            return "break"
        slist.bind("<ButtonPress-1>",drag_start)
        slist.bind("<B1-Motion>",drag_motion)
        slist.bind("<ButtonRelease-1>",drag_end)

        def move_route(delta):
            j=ri()
            if j is None:return
            n=j+delta
            if not (0<=n<len(routes)):return
            routes[j],routes[n]=routes[n],routes[j];autosave();refresh_routes(n)

        def delete_route():
            j=ri()
            if j is None:return
            loose.extend(routes[j].get("stations",[]));routes.pop(j);autosave();refresh_pool();refresh_routes(max(0,j-1))

        def reset_all():
            nonlocal routes,loose
            ok=messagebox.askyesno("全部釋放","把目前排法全部拆掉，所有站回未規劃池？",parent=route_parent)
            keep_planner_front()
            if not ok:return
            allst=list(loose)
            for r in routes:allst.extend(r.get("stations",[]))
            routes=[];loose=allst;autosave();refresh_pool();refresh_routes()

        def save_template():
            if not routes:return
            keep_planner_front()
            name=simpledialog.askstring("儲存路線範本","範本名稱：",parent=route_parent)
            keep_planner_front()
            if not name or not name.strip():return
            def sig(st):return {"point_id":st.get("point_id",""),"point_name":st.get("point","")}
            t=load_route_templates()
            t[name.strip()]={"version":2,"match_mode":"point_id","routes":[
                {"name":r.get("name",""),"stations":[sig(st) for st in r.get("stations",[])]}
                for r in routes if r.get("stations")]}
            save_route_templates(t)
            messagebox.showinfo("路線範本",f"✓ 已儲存「{name.strip()}」。\n只記交換點和順序；下次套用會使用當輪最新物品。",parent=route_parent);keep_planner_front()

        def _apply_template_name(name):
            nonlocal routes,loose
            t=load_route_templates()
            if name not in t:return
            allst=list(loose)+[st for r in routes for st in r.get("stations",[])]
            routes,loose,missing,ambiguous=apply_point_route_template(t[name],allst)
            autosave();refresh_pool();refresh_routes()
            notes=[f"已套用「{name}」，共排入 {sum(len(r.get('stations',[])) for r in routes)} 個交換點。",
                   "物品內容使用目前這一刷的資料。"]
            if missing:notes.append("本輪找不到，已留在範本外："+"、".join(missing))
            if ambiguous:notes.append("同點有多筆，怕排錯所以先不放："+"、".join(ambiguous))
            messagebox.showinfo("路線範本","\n".join(notes),parent=route_parent);keep_planner_front()

        def apply_template():
            t=load_route_templates()
            if not t:
                messagebox.showinfo("路線範本","目前還沒有玩家路線範本。",parent=route_parent);keep_planner_front();return

            keep_planner_front()
            w=tk.Toplevel(route_parent);w.title("⭐ 套用路線範本");w.transient(route_parent);w.grab_set()
            self._pulse_window_front(w)
            remember_window(w,"route_template_picker","520x520")
            tk.Label(w,text="⭐ 選擇路線範本",font=(FONT,18,"bold")).pack(pady=(14,4))
            tk.Label(w,text="點一下選擇；雙擊可直接套用。",font=(FONT,10),fg="#667085").pack(pady=(0,8))
            lb=tk.Listbox(w,font=(FONT,14,"bold"),exportselection=False)
            lb.pack(fill="both",expand=True,padx=18,pady=6)

            def reload_names(select_name=None):
                names=list(load_route_templates().keys())
                lb.delete(0,"end")
                for n in names:lb.insert("end",n)
                if names:
                    idx=names.index(select_name) if select_name in names else 0
                    lb.selection_set(idx);lb.activate(idx)
                return names
            reload_names()

            def selected():
                sel=lb.curselection()
                return lb.get(sel[0]) if sel else None

            def do_apply(_=None):
                name=selected()
                if not name:return
                w.destroy();route_parent.after(30,keep_planner_front);_apply_template_name(name)

            def rename_selected():
                name=selected()
                if not name:return
                nn=simpledialog.askstring("範本改名","新的範本名稱：",initialvalue=name,parent=w)
                if not nn or not nn.strip() or nn.strip()==name:return
                nn=nn.strip();tt=load_route_templates()
                if nn in tt and not messagebox.askyesno("覆蓋範本",f"「{nn}」已存在，要覆蓋嗎？",parent=w):return
                tt[nn]=tt.pop(name);save_route_templates(tt);reload_names(nn)

            def delete_selected():
                name=selected()
                if not name:return
                if not messagebox.askyesno("刪除範本",f"確定刪除「{name}」？",parent=w):return
                tt=load_route_templates();tt.pop(name,None);save_route_templates(tt)
                if not reload_names():w.destroy();route_parent.after(30,keep_planner_front)

            def close_template_picker():
                w.destroy();route_parent.after(30,keep_planner_front)

            lb.bind("<Double-Button-1>",do_apply)
            btns=tk.Frame(w);btns.pack(fill="x",padx=18,pady=(6,14))
            tk.Button(btns,text="✎ 改名",font=(FONT,11),command=rename_selected,padx=10,pady=6).pack(side="left")
            tk.Button(btns,text="🗑 刪除",font=(FONT,11),command=delete_selected,padx=10,pady=6).pack(side="left",padx=4)
            w.protocol("WM_DELETE_WINDOW",close_template_picker)
            tk.Button(btns,text="取消",font=(FONT,11),command=close_template_picker,padx=12,pady=6).pack(side="right")
            tk.Button(btns,text="✓ 套用",font=(FONT,12,"bold"),command=do_apply,padx=18,pady=6).pack(side="right",padx=4)

        def seal_suggestion():
            nonlocal routes,loose
            allst=list(loose)+[st for r in routes for st in r.get("stations",[])]
            proposal=suggest_routes_from_learning(allst)
            if not load_route_learning_history().get("records"):
                messagebox.showinfo("海豹還在做筆記","目前還沒有路線學習紀錄。\n先照平常方式排好並儲存幾批，海豹才有真實經驗可參考。",parent=route_parent)
                keep_planner_front();return
            if not proposal.get("routes"):
                messagebox.showinfo("這一刷還配不起來","海豹有筆記，但這一刷沒有找到可安全對上的交換點。\n目前排法完全不會變動。",parent=route_parent)
                keep_planner_front();return
            rec=proposal.get("record") or {};matched=int(proposal.get("matched") or 0);eligible=int(proposal.get("eligible") or 0)
            similarity=round(matched/max(1,eligible)*100)
            w=tk.Toplevel(route_parent);w.title("🦭 海豹建議排法預覽");w.transient(route_parent);w.grab_set()
            smart_window(w,"seal_route_suggestion","760x680",700,600);w.configure(bg=CUTE_BG);self._pulse_window_front(w)
            w.minsize(680,520)
            hero=tk.Frame(w,bg="#fffef9",padx=18,pady=13);hero.pack(fill="x",padx=16,pady=(15,7))
            add_mascot(hero,size=68,opacity=0.16,quip_key="planner")
            tk.Label(hero,text="🦭 海豹先排了一張草稿",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
            tk.Label(hero,text=f"信心：{proposal.get('confidence','低')}　｜　{proposal.get('supporting_records',0)} 批相似經驗支持　｜　安全對上 {matched}/{eligible} 點（{similarity}%）",
                     font=(FONT,11,"bold"),bg="#fffef9",fg="#607168").pack(anchor="w",pady=(3,0))
            tk.Label(hero,text=f"骨架參考批次：{rec.get('batch_id','')}；相同覆蓋範圍時，優先採用多批共同出現的順序。",
                     font=(FONT,10),bg="#fffef9",fg="#758078").pack(anchor="w",pady=(2,0))
            strong=[x for x in proposal.get("pair_support",[]) if int(x.get("records") or 0)>=2]
            shared=("、".join(f"{x['from']}→{x['to']}（{x['records']}批）" for x in strong[:5]) or "目前還沒有重複出現的相鄰順序")
            timing_rows=load_route_timing_history()
            decision=build_seal_decision_report(routes,loose,proposal,timing_rows)
            maturity=seal_learning_maturity()
            correction_rows=seal_correction_patterns();correction_by_pid={x["point_id"]:x for x in correction_rows}
            proposal_pids={str(st.get("point_id") or "") for r in proposal.get("routes",[]) for st in r.get("stations",[])}
            current_risks=[x for x in correction_rows if x["point_id"] in proposal_pids and x["position_change_percent"]>=50]
            risk_note=("、".join(f"{x['point_name']}（{x['samples']}次中調整{x['position_change_percent']}%）" for x in current_risks[:4])
                       if current_risks else "目前沒有累積到足夠次數的常改站點")
            readiness=seal_suggestion_readiness(proposal,maturity,len(current_risks))
            evaluation_id=create_seal_evaluation(self.state_data.get("batch_id") or "",routes,loose,proposal,decision)
            compare=("和目前排法相同" if decision["same_as_current"] else
                     f"相對目前排法：{decision['moved_route']}站換線、{decision['moved_position']}站同線換位、{decision['from_loose']}站由未規劃排入")
            note=("這只是預覽。按下套用前，現在的排法完全不會改。\n"
                  f"共同習慣：{shared}\n"
                  f"{compare}\n"
                  f"個人貼合成熟度：{maturity['level']}｜{maturity['message']}\n"
                  f"學習趨勢：{maturity['trend']}\n"
                  f"這份草稿的歷史易調整點：{risk_note}\n"
                  f"本次建議等級：{readiness['grade']}｜{readiness['action']}\n"
                  f"提醒：{decision['warning']}\n"
                  f"未規劃保留 {len(proposal.get('loose',[]))} 站；同點重複 {proposal.get('ambiguous',0)} 站；問號或身份不完整 {proposal.get('invalid',0)} 站。")
            tk.Label(w,text=note,font=(FONT,11),bg=CUTE_YELLOW_SOFT,fg=CUTE_TEXT,justify="left",anchor="w",padx=14,pady=9).pack(fill="x",padx=16,pady=5)
            # 先固定底部操作列，再讓草稿區吃剩下的空間；小視窗也不會把套用鍵擠掉。
            foot2=tk.Frame(w,bg=CUTE_BG);foot2.pack(side="bottom",fill="x",padx=16,pady=(2,14))
            preview_shell=tk.Frame(w,bg="#fffef9");preview_shell.pack(fill="both",expand=True,padx=16,pady=7)
            preview=tk.Text(preview_shell,font=(FONT,13),wrap="word",bg="#fffef9",fg=CUTE_TEXT,relief="flat",padx=14,pady=12)
            preview_scroll=tk.Scrollbar(preview_shell,orient="vertical",command=preview.yview)
            preview.configure(yscrollcommand=preview_scroll.set)
            preview.pack(side="left",fill="both",expand=True);preview_scroll.pack(side="right",fill="y")
            for i,r in enumerate(proposal["routes"],1):
                estimate=estimate_route_duration(r,timing_rows)
                _basis=(f"整線 {estimate['samples']} 趟" if estimate.get("exact") else f"站內分段最低 {estimate['samples']} 份支持")
                _prefix="預估" if estimate.get("exact") else "已知站內航段至少"
                _suffix="" if estimate.get("exact") else "，不含出發→第一站"
                estimate_text=(f"｜{_prefix} {format_route_duration(estimate['seconds'])}（{_basis}{_suffix}）"
                               if estimate.get("seconds") is not None else "｜尚無相同路線的有效計時")
                preview.insert("end",f"{i}. {r.get('name') or '建議路線'} {estimate_text}\n","title")
                for j,st in enumerate(r.get("stations",[]),1):
                    preview.insert("end",f"   {j:02d}. {st.get('point','未知')}｜{st.get('input','？')} → {st.get('output','？')} ×{int(st.get('times') or 1)}\n")
                preview.insert("end","   為什麼這樣排：\n","why")
                for reason in decision["routes"][i-1]["reasons"]:
                    preview.insert("end",f"   • {reason}\n","reason")
                route_risks=[correction_by_pid.get(str(st.get("point_id") or "")) for st in r.get("stations",[])]
                route_risks=[x for x in route_risks if x and x["position_change_percent"]>=50]
                for risk in route_risks[:3]:
                    preview.insert("end",f"   • 注意：{risk['point_name']} 過去 {risk['samples']} 次草稿中有 {risk['position_change_percent']}% 被你換線或換位；海豹只標記現象，不猜原因。\n","risk")
                preview.insert("end","\n")
            preview.tag_configure("title",font=(FONT,15,"bold"),foreground=CUTE_GREEN)
            preview.tag_configure("why",font=(FONT,12,"bold"),foreground="#7a4e00")
            preview.tag_configure("reason",font=(FONT,11),foreground="#475467")
            preview.tag_configure("risk",font=(FONT,11,"bold"),foreground="#9a5a00")
            preview.configure(state="disabled")
            def apply_suggestion():
                nonlocal routes,loose
                safe=apply_route_suggestion_safely(routes,loose,proposal)
                if not safe.get("ok"):
                    messagebox.showerror("海豹取消套用",str(safe.get("reason") or "套用安全檢查未通過"),parent=w)
                    return
                routes=safe["routes"];loose=safe["loose"]
                self.state_data["route_learning_pending_suggestion"]={
                    "batch_id":str(self.state_data.get("batch_id") or ""),
                    "source_record_id":str(rec.get("record_id") or rec.get("batch_id") or ""),
                    "evaluation_id":evaluation_id,
                    "signature":safe["signature"],
                    "applied_at":datetime.datetime.now().isoformat(timespec="seconds")}
                update_seal_evaluation(evaluation_id,status="applied",applied_at=datetime.datetime.now().isoformat(timespec="seconds"))
                autosave();refresh_pool();refresh_routes();w.destroy();route_parent.after(30,keep_planner_front)
                learn_status.set("🦭 收到小紅花！儲存後才算正式答案 🌟")
            tk.Button(foot2,text="先不要",font=(FONT,11),command=w.destroy,padx=16,pady=7).pack(side="right")
            tk.Button(foot2,text="✓ 套用這份建議",font=(FONT,13,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                      command=apply_suggestion,padx=20,pady=8).pack(side="right",padx=6)


        pf=tk.Frame(poolf,bg="#fffef9");pf.pack(fill="x",pady=(9,0))
        tk.Button(pf,text="⚡ 單點成線",font=(FONT,12,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",command=single_routes,padx=10,pady=7).pack(side="left")
        tk.Button(pf,text="加入選中路線 →",font=(FONT,12,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  command=add_to_route,padx=12,pady=7).pack(side="right")

        mf=tk.Frame(mid,bg=CUTE_YELLOW_SOFT);mf.pack(fill="x",pady=(9,0))
        tk.Button(mf,text="＋ 新增路線",font=(FONT,12,"bold"),bg="#fffef9",relief="flat",command=new_route).grid(row=0,column=0,columnspan=2,sticky="ew",pady=2)
        tk.Button(mf,text="✎ 改名",font=(FONT,10),command=rename_route).grid(row=1,column=0,sticky="ew",padx=1,pady=2)
        tk.Button(mf,text="🗑 拆掉",font=(FONT,10),command=delete_route).grid(row=1,column=1,sticky="ew",padx=1,pady=2)
        tk.Button(mf,text="↑ 路線",font=(FONT,10),command=lambda:move_route(-1)).grid(row=2,column=0,sticky="ew",padx=1,pady=2)
        tk.Button(mf,text="↓ 路線",font=(FONT,10),command=lambda:move_route(1)).grid(row=2,column=1,sticky="ew",padx=1,pady=2)
        mf.columnconfigure(0,weight=1);mf.columnconfigure(1,weight=1)

        sf=tk.Frame(right,bg="#fffef9");sf.pack(fill="x",pady=(9,0))
        tk.Button(sf,text="← 放回未規劃",font=(FONT,12,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",command=return_to_pool,padx=10,pady=7).pack(side="left",padx=2)
        tk.Button(sf,text="↑ 站",font=(FONT,12,"bold"),bg="#edf2ec",relief="flat",command=lambda:move_station(-1),padx=10,pady=7).pack(side="left",padx=2)
        tk.Button(sf,text="↓ 站",font=(FONT,12,"bold"),bg="#edf2ec",relief="flat",command=lambda:move_station(1),padx=10,pady=7).pack(side="left",padx=2)

        foot=tk.Frame(outer,bg="#fffef9",padx=8,pady=6);foot.pack(fill="x",padx=18,pady=(5,10))
        tk.Button(foot,text="↺ 全部釋放重排",font=(FONT,10,"bold"),command=reset_all,padx=9,pady=6).pack(side="left")
        tk.Button(foot,text="💾 存成路線範本",font=(FONT,10,"bold"),command=save_template,padx=9,pady=6).pack(side="left",padx=4)
        tk.Button(foot,text="⭐ 套用路線範本",font=(FONT,10,"bold"),command=apply_template,padx=9,pady=6).pack(side="left",padx=4)
        if bool(PLAYER_SETTINGS.get("ui_show_seal_features",True)):
            tk.Button(foot,text="🦭 海豹建議排法",font=(FONT,10,"bold"),bg=CUTE_BLUE_SOFT,relief="flat",
                      command=seal_suggestion,padx=9,pady=6).pack(side="left",padx=4)

        def go_picker():
            autosave();self.show_route_picker()
        def save_plan():
            autosave()
            cleaned=[r for r in routes if r.get("stations")]
            if not cleaned:
                messagebox.showinfo("還沒有路線","先建立至少一條有站點的路線。",parent=route_parent);keep_planner_front();return
            self.state_data["routes"]=cleaned;self.state_data["unplanned_stations"]=loose;self.state_data["status"]="路線已編排"
            explicit_skip=bool(skip_learning.get())
            pending=self.state_data.get("route_learning_pending_suggestion")
            feedback=None
            if isinstance(pending,dict) and str(pending.get("batch_id") or "")==str(self.state_data.get("batch_id") or ""):
                unchanged=route_plan_signature(cleaned)==pending.get("signature")
                feedback={"result":"accepted" if unchanged else "edited",
                          "source_record_id":str(pending.get("source_record_id") or ""),
                          "applied_at":str(pending.get("applied_at") or ""),
                          "saved_at":datetime.datetime.now().isoformat(timespec="seconds")}
                finalize_seal_evaluation(pending.get("evaluation_id"),cleaned,feedback["result"])
            if explicit_skip:
                learn_result=remember_saved_route_plan(self.state_data.get("batch_id") or "",cleaned,skip=True)
            elif bool(PLAYER_SETTINGS.get("route_learning_enabled",True)):
                learn_result=remember_saved_route_plan(self.state_data.get("batch_id") or "",cleaned,suggestion_feedback=feedback)
            else:
                learn_result={"saved":False,"reason":"自動學習已關閉"}
            self.state_data["last_route_learning_result"]=learn_result
            STORE.save(self.state_data);self.show_routes()

        tk.Button(foot,text="⚓ 回今日交換補點",font=(FONT,10,"bold"),command=go_picker,padx=10,pady=6).pack(side="right",padx=4)
        tk.Button(foot,text="✓ 儲存今日排法",font=(FONT,15,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                  activebackground="#0f5b31",activeforeground="white",command=save_plan,padx=22,pady=9).pack(side="right",padx=4)

        refresh_pool();refresh_routes()

    def show_routes(self):
        """V5.48｜正式路線卡選擇頁。"""
        route_parent=self._clear_route_page("🗺 路線卡")

        outer=tk.Frame(route_parent,bg=CUTE_BG)
        outer.pack(fill="both",expand=True)

        head=tk.Frame(outer,bg="#fffef9")
        head.pack(fill="x",padx=24,pady=(18,8))
        add_mascot(head,size=76,opacity=0.16,quip_key="routes")

        tk.Label(head,text="🧭 今日航線",font=(FONT,28,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left")
        tk.Label(head,text="　今天想先跑哪一趟，就直接點哪一張。",font=(FONT,12),
                 bg="#fffef9",fg="#607168").pack(side="left",pady=(10,0))
        tk.Button(head,text="📦 庫存管理",font=(FONT,11,"bold"),command=self.open_inventory_manager,padx=12,pady=5).pack(side="right",padx=4)
        tk.Button(head,text="⚓ 今日交換選擇",font=(FONT,11,"bold"),command=self.show_route_picker,padx=12,pady=5).pack(side="right",padx=4)
        tk.Button(head,text="✏ 排路線",font=(FONT,11,"bold"),command=self.show_route_planner,padx=12,pady=5).pack(side="right",padx=4)

        routes=self.state_data.get("routes",[])
        completed=sum(1 for r in routes if r.get("status")=="已完成")
        total=len(routes)
        timing_rows=load_route_timing_history()
        route_time_estimates={id(r):estimate_route_duration(r,timing_rows) for r in routes}
        known_remaining=0.0;unknown_remaining=0
        for r in routes:
            if r.get("status")=="已完成":continue
            est=route_time_estimates[id(r)]
            if est.get("seconds") is None or not est.get("exact"):unknown_remaining+=1;continue
            spent=route_timer_elapsed(r) if r.get("timing_started_at") else 0.0
            known_remaining+=max(0.0,float(est["seconds"])-spent)

        # Top summary
        summary=tk.Frame(outer,bg=CUTE_BG)
        summary.pack(fill="x",padx=24,pady=6)

        try:
            disc=float(PLAYER_SETTINGS.get("negotiation_discount_percent",0) or 0)
        except Exception:
            disc=0.0
        try:
            cap=int((self.state_data.get("route_picker") or {}).get("power_cap")
                    or PLAYER_SETTINGS.get("negotiation_power_limit",1000000) or 1000000)
        except Exception:
            cap=1000000
        if cap not in (1000000,1250000):cap=1000000
        active_ship=active_ship_profile();shipcap=active_ship["capacity"]
        if bool(PLAYER_SETTINGS.get("ui_show_ship_switch",True)) and len(available_ship_profiles())>1:
            def switch_ship_overview():
                cycle_active_ship();self.show_routes()
            tk.Button(head,text=f"⛵ 切換船隻｜{active_ship['name']}",font=(FONT,11,"bold"),
                      bg=CUTE_YELLOW_SOFT,fg="#69552a",relief="flat",command=switch_ship_overview,
                      padx=12,pady=5).pack(side="right",padx=4)

        def route_power(rr):
            # 路線草稿已不依賴OCR交涉力；直接沿用正式算法。
            mult=max(0.0,1.0-disc/100.0)
            total_power=0
            for st in rr.get("stations",[]):
                out=str(st.get("output") or "")
                base=21650 if out=="烏鴉硬幣" else 14286
                try:n=int(st.get("times") or 1)
                except:n=1
                total_power += int(round(base*mult*n))
            return total_power

        # V5.64：交涉力採逐站實際帳。
        # 路線狀態（進行中/暫停/已完成）不能決定消耗；
        # 只有真正按過「完成」的站才算，跳過不算，撤銷後即歸還。
        active_used=0
        _mult=max(0.0,1.0-disc/100.0)
        try:_normal_base=int(PLAYER_SETTINGS.get("normal_barter_base_power",14286) or 14286)
        except:_normal_base=14286
        try:_high_base=int(PLAYER_SETTINGS.get("high_barter_base_power",21650) or 21650)
        except:_high_base=21650
        for rr in routes:
            for st in (rr.get("stations") or []):
                if st.get("status")!="完成":
                    continue
                _base=_high_base if str(st.get("output") or "")=="烏鴉硬幣" else _normal_base
                try:_n=int(st.get("times") or 1)
                except:_n=1
                active_used += int(round(_base*_mult*_n))
        remain=max(0,cap-active_used)

        cards=[]
        if bool(PLAYER_SETTINGS.get("ui_show_negotiation_power",True)):cards.append(("⚡ 剩餘交涉力",f"{remain:,} / {cap:,}",CUTE_GREEN_SOFT))
        cards.extend([("☑ 已完成 / 總數",f"{completed} / {total} 條",CUTE_BLUE_SOFT),
                      ("📦 今日路線",f"{total} 條",CUTE_YELLOW_SOFT)])
        if bool(PLAYER_SETTINGS.get("ui_show_timing",True)):
            cards.append(("⏱ 未完成路線預估",((format_route_duration(known_remaining) if known_remaining>0 else "尚無樣本")+
                          (f"｜{unknown_remaining}條未知" if unknown_remaining else "")),"#f2effa"))
        for i,(lab,val,color) in enumerate(cards):
            f=tk.Frame(summary,bg=color,bd=0,padx=16,pady=10)
            f.grid(row=0,column=i,sticky="nsew",padx=5)
            tk.Label(f,text=lab,font=(FONT,11,"bold"),bg=color,fg="#607168").pack(anchor="w")
            tk.Label(f,text=val,font=(FONT,14 if lab.startswith("⏱") else 20,"bold"),bg=color,fg=CUTE_GREEN,
                     wraplength=220,justify="left").pack(anchor="w",pady=(2,0))
            summary.columnconfigure(i,weight=1)


        # Scrollable route-card grid
        shell=tk.Frame(outer,bg=CUTE_BG)
        shell.pack(fill="both",expand=True,padx=24,pady=(6,14))
        cv=tk.Canvas(shell,bg=CUTE_BG,highlightthickness=0)
        sb=tk.Scrollbar(shell,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y")
        cv.pack(side="left",fill="both",expand=True)

        grid=tk.Frame(cv,bg=CUTE_BG)
        winid=cv.create_window((0,0),window=grid,anchor="nw")
        grid.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",lambda e:cv.itemconfigure(winid,width=e.width))

        if not routes:
            tk.Label(grid,text="目前還沒有路線。\n先回「今日交換選擇」勾這趟要跑的交換。",
                     font=(FONT,18,"bold"),bg=CUTE_BG,fg="#667085",justify="center").pack(expand=True,pady=80)
            return

        def route_title(rr,idx):
            src=str(rr.get("source") or "")
            if rr.get("name") and rr.get("name") not in ("今日手選","本趟手選"):
                return rr.get("name")
            # 現階段只有手選草稿，先用明確的人話命名；
            # 下一版再接真正的自動拆趟/高G/高Z等分類。
            return f"今日路線 {idx+1}"

        def route_range(rr):
            sts=rr.get("stations",[])
            if not sts:return ("—","—")
            return (sts[0].get("point") or "—",sts[-1].get("point") or "—")

        def attach_route_hover(card,tiptext):
            """在卡片內直接展開提示；不依賴浮動視窗、焦點或置頂層。"""
            state={"shown":False,"job":None}
            panel=tk.Frame(card,bg="#fff8d8",bd=2,relief="solid")
            tk.Label(panel,text="📦 這趟出發要帶",font=(FONT,13,"bold"),
                     bg="#fff8d8",fg="#7a4e00").pack(anchor="w",padx=12,pady=(8,2))
            tk.Label(panel,text=tiptext,font=(FONT,12,"bold"),bg="#fff8d8",fg="#344054",
                     justify="left",anchor="w",wraplength=520).pack(fill="x",padx=12,pady=(0,9))
            def close_tip():
                if state["shown"]:
                    panel.pack_forget();state["shown"]=False
            def show_tip(_=None):
                if state["job"] is not None:
                    try:self.after_cancel(state["job"])
                    except Exception:pass
                    state["job"]=None
                if not state["shown"]:
                    panel.pack(fill="x",padx=16,pady=(0,12));state["shown"]=True
            def maybe_hide(_=None):
                def check():
                    state["job"]=None
                    try:
                        x=self.winfo_pointerx();y=self.winfo_pointery()
                        inside=(card.winfo_rootx()<=x<=card.winfo_rootx()+card.winfo_width() and
                                card.winfo_rooty()<=y<=card.winfo_rooty()+card.winfo_height())
                    except Exception:inside=False
                    if not inside:close_tip()
                if state["job"] is not None:
                    try:self.after_cancel(state["job"])
                    except Exception:pass
                state["job"]=self.after(90,check)
            def bind_tree(w):
                w.bind("<Enter>",show_tip,add="+");w.bind("<Leave>",maybe_hide,add="+")
                for child in w.winfo_children():bind_tree(child)
            bind_tree(card)

        for i,rr in enumerate(routes):
            done,total_st=route_progress(rr)
            status=rr.get("status","尚未開始")
            start,end=route_range(rr)
            power=route_power(rr)

            _card_bg="#f2f8ef" if status=="已完成" else "#fffef9"
            card_shell,card=make_round_panel(grid,bg=_card_bg,
                                             outline=("#b6d4ae" if status=="已完成" else CUTE_BORDER),
                                             radius=24,padding=14,min_height=300)
            card_shell.grid(row=i//2,column=i%2,sticky="nsew",padx=10,pady=10)
            grid.columnconfigure(i%2,weight=1)

            top=tk.Frame(card,bg=_card_bg)
            top.pack(fill="x",padx=16,pady=(14,6))

            num_bg=CUTE_GREEN if status!="已完成" else "#82a57d"
            badge=tk.Label(top,text=str(i+1),font=(FONT,18,"bold"),bg=num_bg,fg="white",
                           width=3,pady=4)
            badge.pack(side="left")

            tk.Label(top,text=route_title(rr,i),font=(FONT,21,"bold"),bg=_card_bg,
                     fg=CUTE_GREEN).pack(side="left",padx=10)

            tk.Label(top,text=f"{total_st} 站",font=(FONT,13,"bold"),bg=_card_bg,
                     fg="#475467").pack(side="right")

            route_line=tk.Frame(card,bg=_card_bg)
            route_line.pack(fill="x",padx=16,pady=6)
            tk.Label(route_line,text=start,font=(FONT,16,"bold"),bg=_card_bg,fg=CUTE_TEXT).pack(side="left")
            tk.Label(route_line,text="  ➜  ",font=(FONT,16,"bold"),bg=_card_bg,fg="#83a58b").pack(side="left")
            tk.Label(route_line,text=end,font=(FONT,16,"bold"),bg=_card_bg,fg=CUTE_TEXT).pack(side="left")

            # Preview first two exchanges
            preview=tk.Frame(card,bg=CUTE_BLUE_SOFT,bd=0,padx=4,pady=3)
            preview.pack(fill="x",padx=16,pady=6)
            shown=rr.get("stations",[])[:2]
            if shown:
                for st in shown:
                    tk.Label(preview,
                             text=f"{st.get('point','')}｜{st.get('input','？')} → {st.get('output','？')}",
                             font=(FONT,11),bg=CUTE_BLUE_SOFT,fg="#425a70",anchor="w").pack(fill="x",padx=8,pady=4)
            else:
                tk.Label(preview,text="沒有站點資料",font=(FONT,11),bg=CUTE_BLUE_SOFT).pack(pady=6)

            meta=tk.Frame(card,bg=_card_bg)
            meta.pack(fill="x",padx=16,pady=(6,10))
            tk.Label(meta,text=f"進度：{done}/{total_st}",font=(FONT,11,"bold"),bg=_card_bg,fg=CUTE_GREEN).pack(side="left")
            if bool(PLAYER_SETTINGS.get("ui_show_negotiation_power",True)):
                tk.Label(meta,text=f"預估交涉力：{power:,}",font=(FONT,11,"bold"),bg=_card_bg,fg="#607168").pack(side="right")
            _time_est=route_time_estimates[id(rr)]
            if status=="已完成" and rr.get("timing_finished_at"):
                _time_text=("😴 本趟標記發呆，不列入時間統計" if rr.get("timing_valid") is False
                            else f"⏱ 本趟實際用時：{format_route_duration(route_timer_elapsed(rr))}")
            elif _time_est.get("seconds") is not None:
                _spent=route_timer_elapsed(rr) if rr.get("timing_started_at") else 0.0
                _remaining=max(0.0,float(_time_est["seconds"])-_spent) if _time_est.get("exact") else float(_time_est["seconds"])
                if _time_est.get("exact"):
                    _time_text=f"⏱ 歷史預估剩餘：{format_route_duration(_remaining)}｜{_time_est['samples']}趟相同路線"
                else:
                    _time_text=f"⏱ 已知站內航段至少：{format_route_duration(_remaining)}｜不含出發→第一站｜最低{_time_est['samples']}份支持"
            else:_time_text="⏱ 尚無相同路線的有效計時"
            if bool(PLAYER_SETTINGS.get("ui_show_timing",True)):
                tk.Label(card,text=_time_text,font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,fg="#425a70",
                         anchor="w",padx=10,pady=6).pack(fill="x",padx=16,pady=(0,7))
            progress=tk.Canvas(card,height=14,bg=_card_bg,highlightthickness=0)
            progress.pack(fill="x",padx=16,pady=(0,7))
            def _draw_progress(e=None,pc=progress,d=done,t=total_st,bg0=_card_bg):
                w=max(30,pc.winfo_width());pc.delete("all")
                draw_round_rect(pc,1,2,w-1,12,6,fill="#dfe9dc",outline="#dfe9dc")
                if t>0 and d>0:draw_round_rect(pc,1,2,max(12,(w-1)*d/t),12,6,fill=CUTE_GREEN_LINE,outline=CUTE_GREEN_LINE)
            progress.bind("<Configure>",_draw_progress)
            if bool(PLAYER_SETTINGS.get("ui_show_weight",True)):
                wp=route_peak_weight(rr);peak=wp["peak_weight"];unknown=wp["unknown_items"]
                over=bool(shipcap>0 and peak>shipcap)
                if shipcap>0:
                    wtext=f"⚠ {active_ship['name']} 可能超重：已知至少 {peak:,.0f} / {shipcap:,.0f} LT" if over else f"⚖ {active_ship['name']} 估算最高：{peak:,.0f} / {shipcap:,.0f} LT"
                else:wtext=f"⚖ {active_ship['name']} 估算最高已知重量：{peak:,.0f} LT（尚未設定上限）"
                if unknown:wtext+=f"｜另有 {len(unknown)} 種缺重量"
                tk.Label(card,text=wtext,font=(FONT,11,"bold"),bg=("#ffe8e8" if over else _card_bg),
                         fg=("#b42318" if over else "#475467"),anchor="w").pack(fill="x",padx=16,pady=(0,8))

            if status=="已完成":
                actions=tk.Frame(card,bg=_card_bg)
                actions.pack(fill="x",padx=16,pady=(0,14))
                tk.Label(actions,text="✓ 已完成",font=(FONT,14,"bold"),bg=_card_bg,fg=CUTE_GREEN).pack(side="left",padx=8)
                tk.Button(actions,text="↶ 撤銷完成",font=(FONT,12,"bold"),
                          command=lambda r=rr:self.undo_route_complete(r),padx=14,pady=7).pack(side="right")
                tk.Button(actions,text="查看／整理",font=(FONT,12,"bold"),
                          command=lambda r=rr:self.open_route(r),padx=14,pady=7).pack(side="right",padx=6)
                btn=None
            else:
                label="繼續這趟 →" if status in ("進行中","暫停") else "開始這趟 →"
                btn=tk.Button(card,text=label,font=(FONT,15,"bold"),bg=CUTE_GREEN,fg="white",relief="flat",
                              activebackground="#0f5b31",activeforeground="white",
                              command=lambda r=rr:self.open_route(r),padx=18,pady=9)
            if btn is not None:
                btn.pack(fill="x",padx=16,pady=(0,14))
            _needs=remaining_route_requirements(rr)
            _tiptext="\n".join(f"{x.get('name') or '？'} × {x.get('quantity',0)}" for x in _needs)
            if not _tiptext:_tiptext="這趟已完成，沒有剩餘交換品。"
            attach_route_hover(card,_tiptext)

        # Mousewheel support
        def _wheel(e):
            try:cv.yview_scroll(int(-1*(e.delta/120)*3),"units")
            except Exception:pass
        cv.bind_all("<MouseWheel>",_wheel)

    def undo_route_complete(self,route):
        """手滑可回頭：整條完成狀態撤銷，站點恢復成可操作。"""
        if not messagebox.askyesno("撤銷完成","要把這趟恢復成可操作狀態嗎？\n已完成／跳過的站點會恢復為未開始。",parent=self._route_dialog_parent()):
            return
        for st in route.get("stations",[]):
            if st.get("status")=="完成":
                _source_tx_before_undo=st.get("inventory_tx_id")
                ok,msg=reverse_station_inventory(st,"撤銷整趟完成")
                if not ok:
                    messagebox.showwarning("庫存未還原",str(msg),parent=self._route_dialog_parent())
                    return
                reverse_linked_t5_sales({"inventory_tx_id":_source_tx_before_undo},"撤銷整趟完成")
            if st.get("status") in ("完成","跳過"):
                st["status"]="未開始"
        reset_route_timer(route)
        self.state_data.pop("journey_ending_shown_for",None)
        route["status"]="尚未開始"
        route["current_index"]=0
        route["view_index"]=0
        STORE.save(self.state_data)
        self.show_routes()

    def maybe_show_journey_ending(self):
        """所有路線都處理完才出現一次；撤銷後可重新結算。"""
        routes=self.state_data.get("routes") or []
        if not routes:return
        stations=[st for r in routes for st in (r.get("stations") or [])]
        if not stations or any(st.get("status","未開始") not in ("完成","跳過") for st in stations):return
        signature="|".join(str(st.get("inventory_tx_id") or st.get("status")) for st in stations)
        if self.state_data.get("journey_ending_shown_for")==signature:return
        self.state_data["journey_ending_shown_for"]=signature;STORE.save(self.state_data)

        completed=sum(1 for st in stations if st.get("status")=="完成")
        skipped=sum(1 for st in stations if st.get("status")=="跳過")
        valid=[r for r in routes if r.get("timing_finished_at") and r.get("timing_valid") is not False]
        dazed=[r for r in routes if r.get("timing_finished_at") and r.get("timing_valid") is False]
        elapsed=sum(route_timer_elapsed(r) for r in valid)
        sales=[];seen=set()
        for r in routes:
            for info in t5_sale_infos_for_route(r):
                iid=str(info.get("output_id") or "")
                if iid and iid not in seen and info.get("excess",0)>0:
                    seen.add(iid);info["route"]=r
                    info["source"]=next((s for s in r.get("stations",[]) if str(s.get("output_id") or "")==iid),{})
                    sales.append(info)

        parent=self._route_dialog_parent();win=tk.Toplevel(parent)
        win.title("🎉 今日航程完成");win.geometry("720x650");win.configure(bg=CUTE_BG);win.transient(parent)
        hero=tk.Frame(win,bg="#fffef9",padx=24,pady=18);hero.pack(fill="x",padx=18,pady=(18,8))
        add_mascot(hero,size=110,opacity=.96,side="left",padx=10,quip_key="routes")
        tk.Label(hero,text="🎉 全程跑完啦！\n海豹已經把今天的航海日記寫好了。",font=(FONT,22,"bold"),
                 bg="#fffef9",fg=CUTE_GREEN,justify="left").pack(side="left",padx=12)
        body=tk.Frame(win,bg=CUTE_BLUE_SOFT,padx=22,pady=18);body.pack(fill="x",padx=18,pady=8)
        summary=(f"路線：{len(routes)} 條　｜　完成：{completed} 站　｜　跳過：{skipped} 站\n"
                 f"有效計時：{len(valid)} 趟　｜　發呆不採計：{len(dazed)} 趟\n"
                 f"有效總時間：{format_route_duration(elapsed) if valid else '沒有有效樣本'}")
        tk.Label(body,text=summary,font=(FONT,16,"bold"),bg=CUTE_BLUE_SOFT,fg="#315f91",justify="left").pack(anchor="w")
        if sales:
            sellbox=tk.LabelFrame(win,text="🏝 回伊利亞時記得整理｜5階溢出",font=(FONT,14,"bold"),
                                  bg=CUTE_YELLOW_SOFT,fg="#8a4b08",padx=12,pady=8)
            sellbox.pack(fill="both",expand=True,padx=18,pady=8)
            for info in sales:
                row=tk.Frame(sellbox,bg=CUTE_YELLOW_SOFT);row.pack(fill="x",pady=4)
                nm=info.get("display_name") or info.get("name") or info["output_id"]
                tk.Label(row,text=f"{nm}：目前 {info['current']}｜保留 {info['cap']}｜待出售 {info['excess']}",
                         font=(FONT,12,"bold"),bg=CUTE_YELLOW_SOFT,fg="#8a4b08").pack(side="left")
                def sell_one(x=info,name=nm):
                    if not messagebox.askyesno("確認已賣出",f"你已在遊戲內賣出「{name}」多餘的 {x['excess']} 個？",parent=win):return
                    ok,msg=sell_t5_excess(x["output_id"],name,x["route"],x["source"])
                    if not ok:messagebox.showwarning("無法出售",str(msg),parent=win);return
                    STORE.save(self.state_data);win.destroy();self.state_data.pop("journey_ending_shown_for",None);self.maybe_show_journey_ending()
                tk.Button(row,text=f"✓ 已賣出 {info['excess']}",font=(FONT,11,"bold"),command=sell_one).pack(side="right")
            # V6.1：添加批量確認賣出按鈕
            def sell_all():
                total_excess=sum(info.get("excess",0) for info in sales)
                if not messagebox.askyesno("批量確認賣出",f"你已在遊戲內賣出所有溢出的 5 階品（共 {total_excess} 個）？",parent=win):return
                for info in sales:
                    ok,msg=sell_t5_excess(info["output_id"],info.get("display_name") or info.get("name") or info["output_id"],info["route"],info["source"])
                    if not ok:messagebox.showwarning("出售失敗",f"{info.get('display_name') or info.get('name') or info['output_id']}：{msg}",parent=win);return
                STORE.save(self.state_data);win.destroy();self.state_data.pop("journey_ending_shown_for",None);self.maybe_show_journey_ending()
            batch_row=tk.Frame(sellbox,bg=CUTE_YELLOW_SOFT);batch_row.pack(fill="x",pady=(12,4))
            tk.Button(batch_row,text="✓ 批量確認賣出全部",font=(FONT,13,"bold"),bg=CUTE_GREEN,fg="white",
                      relief="flat",command=sell_all,padx=18,pady=7).pack(side="right")
        else:
            tk.Label(win,text="📦 5階庫存都在保留上限內，不用另外出售。",font=(FONT,14,"bold"),
                     bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN,pady=12).pack(fill="x",padx=18,pady=8)
        tk.Button(win,text="收下今天的航海日記 🌟",font=(FONT,14,"bold"),bg=CUTE_GREEN,fg="white",
                  relief="flat",command=win.destroy,padx=18,pady=9).pack(pady=14)

    def open_route(self,route):
        now=datetime.datetime.now().timestamp()
        for other in self.state_data.get("routes",[]) or []:
            if other is not route:route_timer_pause(other,now)
        self.current_route=route
        if route.get("status") in ("尚未開始","暫停"):
            route["status"]="進行中"
        if route.get("status")!="已完成":route_timer_start(route,now)
        try:self._route_view_index=int(route.get("view_index"))
        except Exception:self._route_view_index=None
        STORE.save(self.state_data)
        self.show_route()

    def show_route(self):
        """V5.53.1｜船長捷運正式執行層：整線、當前站、前後站、完成/撤銷與跨頁返回。"""
        route_parent=self._clear_route_page("⛵ 路線執行")
        r=self.current_route
        sts=r.get("stations",[])
        route_ui=load_player_route_ui()
        show_item_alias=bool(route_ui.get("route_card_show_item_alias",False))
        def shown_item(item_id,name):
            return player_item_alias(item_id,name) if show_item_alias else str(name or "？")
        idx=first_unhandled_index(r)
        done,total=route_progress(r)

        # 整條線已處理完，仍可回看並撤銷最後一站。
        if idx is None and sts:
            idx=len(sts)-1

        outer=tk.Frame(route_parent,bg=CUTE_BG);outer.pack(fill="both",expand=True)
        top=tk.Frame(outer,bg="#fffef9",bd=0);top.pack(fill="x",padx=20,pady=(12,5))
        add_mascot(top,size=62,opacity=0.14,padx=4,quip_key="runner")
        def leave_route(where):
            # V5.64：所有離開執行頁入口統一收尾。
            try:r["view_index"]=int(view_idx)
            except Exception:pass
            if sts and first_unhandled_index(r) is None:
                r["status"]="已完成"
            STORE.save(self.state_data)
            if where=="routes":self.show_routes()
            elif where=="picker":self.show_route_picker()
            else:self.close_route_window()

        tk.Button(top,text="← 路線總覽",font=(FONT,11,"bold"),command=lambda:leave_route("routes"),padx=12,pady=5).pack(side="left")
        tk.Button(top,text="⚓ 今日交換",font=(FONT,11,"bold"),command=lambda:leave_route("picker"),padx=10,pady=5).pack(side="left",padx=4)
        tk.Button(top,text="⌂ 首頁",font=(FONT,11,"bold"),command=lambda:leave_route("home"),padx=10,pady=5).pack(side="left",padx=4)
        tk.Label(top,text=f"⛵ {r.get('name','路線')}",font=(FONT,25,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="left",padx=16)
        tk.Label(top,text=f"{done} / {total} 站",font=(FONT,19,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(side="right",padx=8)
        timer_var=tk.StringVar()
        timer_label=tk.Label(top,textvariable=timer_var,font=(FONT,14,"bold"),bg="#fffef9",
                             fg=("#8b5fbf" if r.get("timing_valid") is False else "#157347"))
        if bool(PLAYER_SETTINGS.get("ui_show_timing",True)):timer_label.pack(side="right",padx=10)
        def tick_timer():
            if not timer_label.winfo_exists():return
            elapsed=route_timer_elapsed(r)
            if r.get("timing_valid") is False:timer_var.set(f"😴 本趟不計時｜{format_route_duration(elapsed)}")
            elif r.get("timing_finished_at"):timer_var.set(f"⏱ 本趟用時 {format_route_duration(elapsed)}")
            elif r.get("timing_running_since") is not None:timer_var.set(f"⏱ 本趟航行中 {format_route_duration(elapsed)}")
            else:timer_var.set(f"⏸ 已暫停 {format_route_duration(elapsed)}")
            self._route_timer_after=self.after(1000,tick_timer)
        if bool(PLAYER_SETTINGS.get("ui_show_timing",True)):tick_timer()
        def toggle_item_alias():
            route_ui["route_card_show_item_alias"]=not show_item_alias
            save_player_route_ui(route_ui);self.show_route()
        tk.Button(top,text=("顯示全名" if show_item_alias else "顯示物品縮寫"),font=(FONT,11,"bold"),
                  command=toggle_item_alias,padx=10,pady=5).pack(side="right",padx=8)

        # 標題與主要返回操作固定在上方；下面內容可隨視窗大小捲動，避免功能被埋住。
        body_shell=tk.Frame(outer,bg=CUTE_BG);body_shell.pack(fill="both",expand=True)
        body_cv=tk.Canvas(body_shell,bg=CUTE_BG,highlightthickness=0)
        body_sb=tk.Scrollbar(body_shell,orient="vertical",command=body_cv.yview)
        body_cv.configure(yscrollcommand=body_sb.set)
        body_sb.pack(side="right",fill="y");body_cv.pack(side="left",fill="both",expand=True)
        body=tk.Frame(body_cv,bg=CUTE_BG)
        body_win=body_cv.create_window((0,0),window=body,anchor="nw")
        body.bind("<Configure>",lambda e:body_cv.configure(scrollregion=body_cv.bbox("all")))
        body_cv.bind("<Configure>",lambda e:body_cv.itemconfigure(body_win,width=e.width))
        def _body_wheel(e):
            try:body_cv.yview_scroll(int(-1*(e.delta/120)*3),"units")
            except Exception:pass
        body_cv.bind("<Enter>",lambda e:body_cv.bind_all("<MouseWheel>",_body_wheel))
        body_cv.bind("<Leave>",lambda e:body_cv.unbind_all("<MouseWheel>"))

        needs=remaining_route_requirements(r)
        needbar=tk.Frame(body,bg=CUTE_GREEN_SOFT,bd=0,relief="flat",padx=5,pady=3)
        if bool(PLAYER_SETTINGS.get("ui_show_route_requirements",True)):needbar.pack(fill="x",padx=26,pady=(9,4))
        needtext="、".join(f"{shown_item(x.get('item_id'),x.get('name'))} ×{x.get('quantity')}" for x in needs) if needs else "已不需要攜帶交換品"
        tk.Label(needbar,text="📦 本趟剩餘需帶：",font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).pack(side="left",padx=(14,4),pady=9)
        tk.Label(needbar,text=needtext,font=(FONT,14,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,anchor="w",justify="left",wraplength=900).pack(side="left",fill="x",expand=True,padx=(0,12),pady=9)

        weight=route_peak_weight(r)
        active_ship=active_ship_profile();shipcap=active_ship["capacity"]
        if bool(PLAYER_SETTINGS.get("ui_show_ship_switch",True)) and len(available_ship_profiles())>1:
            def switch_ship_runner():
                try:r["view_index"]=int(view_idx)
                except Exception:pass
                STORE.save(self.state_data);cycle_active_ship();self.show_route()
            tk.Button(top,text=f"⛵ 切換｜{active_ship['name']}",font=(FONT,11,"bold"),
                      bg=CUTE_YELLOW_SOFT,fg="#69552a",relief="flat",command=switch_ship_runner,
                      padx=10,pady=5).pack(side="right",padx=6)
        peak=weight["peak_weight"];unknown=weight["unknown_items"];over=bool(shipcap>0 and peak>shipcap)
        weightbar=tk.Frame(body,bg=("#ffe8e8" if over else CUTE_BLUE_SOFT),bd=0,relief="flat",padx=5,pady=3)
        if bool(PLAYER_SETTINGS.get("ui_show_weight",True)):weightbar.pack(fill="x",padx=26,pady=(4,7))
        if shipcap>0:
            weighttext=(f"⚠ {active_ship['name']} 可能超重｜已知最高至少 {peak:,.0f} / {shipcap:,.0f} LT" if over
                        else f"⚖ {active_ship['name']} 估算最高負重 {peak:,.0f} / {shipcap:,.0f} LT")
        else:weighttext=f"⚖ {active_ship['name']} 已知最高重量 {peak:,.0f} LT｜請到玩家設定輸入可用負重"
        if unknown:weighttext+=f"｜缺重量：{'、'.join(unknown)}"
        def open_weight_timeline():
            w=tk.Toplevel(route_parent);w.title("⚖ 逐站負重明細");w.transient(route_parent)
            smart_window(w,"route_weight_timeline","980x720",760,540);w.configure(bg=CUTE_BG)
            close_weight=self._handoff_modal_grab(w)
            hero=tk.Frame(w,bg="#fffef9",padx=18,pady=12);hero.pack(fill="x",padx=16,pady=(14,7))
            tk.Label(hero,text=f"⚖ {r.get('name') or '本趟路線'}｜逐站負重明細",font=(FONT,23,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(anchor="w")
            captext=f"{active_ship['name']} {shipcap:,.0f} LT" if shipcap>0 else f"{active_ship['name']} 尚未設定上限"
            tk.Label(hero,text=f"已知出發 {weight['start_weight']:,.0f} LT｜已知最高 {peak:,.0f} LT｜最高點：{weight.get('peak_location','—')}｜{captext}",
                     font=(FONT,11,"bold"),bg="#fffef9",fg=("#b42318" if over else "#607168")).pack(anchor="w",pady=(3,0))
            start_box=tk.Frame(w,bg=CUTE_GREEN_SOFT,padx=14,pady=9);start_box.pack(fill="x",padx=16,pady=5)
            starts=[]
            for x in weight.get("start_items",[]):
                wt=(f"＝{x['known_weight']:,.0f} LT" if x.get("known_weight") is not None else "＝缺重量")
                starts.append(f"{x.get('name','？')} ×{x.get('quantity',0)} {wt}")
            tk.Label(start_box,text="📦 出發貨物："+("、".join(starts) if starts else "不需要攜帶交換品"),
                     font=(FONT,12,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT,anchor="w",justify="left",wraplength=900).pack(fill="x")
            if unknown:
                tk.Label(w,text="⚠ 缺重量："+"、".join(unknown)+"。下表與最高值只代表已知至少重量，不能宣稱安全。",
                         font=(FONT,11,"bold"),bg="#fff0f0",fg="#b42318",anchor="w",padx=14,pady=8,wraplength=900).pack(fill="x",padx=16,pady=5)
            bottom=tk.Frame(w,bg=CUTE_BG);bottom.pack(side="bottom",fill="x",padx=16,pady=(2,14))
            tk.Button(bottom,text="關閉",font=(FONT,11,"bold"),command=close_weight,padx=18,pady=7).pack(side="right")
            holder=tk.Frame(w,bg=CUTE_BG);holder.pack(fill="both",expand=True,padx=16,pady=7)
            cols=("no","point","before","give","after","exchange")
            tree=ttk.Treeview(holder,columns=cols,show="headings")
            labels={"no":"站","point":"交換點","before":"交換前","give":"交出後","after":"換回後","exchange":"交換內容"}
            widths={"no":48,"point":145,"before":95,"give":95,"after":95,"exchange":390}
            for c in cols:tree.heading(c,text=labels[c]);tree.column(c,width=widths[c],anchor="center" if c!="exchange" else "w")
            sb=tk.Scrollbar(holder,orient="vertical",command=tree.yview);tree.configure(yscrollcommand=sb.set)
            tree.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y")
            tree.tag_configure("peak",background="#fff2c7",foreground="#7a4e00")
            for x in weight.get("timeline",[]):
                inp=f"{x['input']} ×{x['input_quantity']}"+("（缺重量）" if x.get("input_unit_weight") is None else "")
                out=f"{x['output']} ×{x['output_quantity']}"+("（缺重量）" if x.get("output_unit_weight") is None else "")
                is_peak=(str(x.get("point") or "")+"交換後"==weight.get("peak_location"))
                tree.insert("","end",values=(x["station_index"]+1,x["point"],f"{x['before_weight']:,.0f} LT",
                    f"{x['after_give_weight']:,.0f} LT",f"{x['after_weight']:,.0f} LT",f"{inp} → {out}"),tags=(("peak",) if is_peak else ()))
        weight_label=tk.Label(weightbar,text=weighttext,font=(FONT,12,"bold"),bg=("#ffe8e8" if over else CUTE_BLUE_SOFT),
                 fg=("#b42318" if over else "#344054"),anchor="w",wraplength=930,justify="left")
        weight_label.pack(side="left",fill="x",expand=True,padx=12,pady=7)
        tk.Button(weightbar,text="查看逐站明細",font=(FONT,11,"bold"),bg="#fffef9",relief="flat",
                  command=open_weight_timeline,padx=12,pady=6).pack(side="right",padx=(4,10),pady=5)

        # 捷運線：全部站都看得到；目前站醒目、已完成打勾、跳過顯示問號。
        metro_shell=tk.Frame(body,bg="#fffef9",bd=0,relief="flat",padx=6,pady=4)
        metro_shell.pack(fill="x",padx=26,pady=9)
        daze_slot=tk.Frame(metro_shell,bg=CUTE_YELLOW_SOFT,bd=0)
        if bool(PLAYER_SETTINGS.get("ui_show_daze_card",True)):daze_slot.pack(side="right",fill="y")
        metro_track=tk.Frame(metro_shell,bg="#fffef9");metro_track.pack(side="left",fill="both",expand=True)
        cv=tk.Canvas(metro_track,height=172,bg="#fffef9",highlightthickness=0)
        hb=tk.Scrollbar(metro_track,orient="horizontal",command=cv.xview);cv.configure(xscrollcommand=hb.set)
        cv.pack(fill="x",expand=True);hb.pack(fill="x")
        line=tk.Frame(cv,bg="#fffef9");winid=cv.create_window((8,10),window=line,anchor="nw")
        line.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))

        def goto_station(j):
            # 允許點捷運線任一站回看；只改觀看站，不竄改完成狀態。
            self._route_view_index=j
            r["view_index"]=j
            STORE.save(self.state_data)
            self.show_route()

        view_idx=getattr(self,"_route_view_index",None)
        if view_idx is None:
            try:view_idx=int(r.get("view_index"))
            except Exception:view_idx=None
        auto_idx=first_unhandled_index(r)
        if view_idx is None or not (0<=view_idx<len(sts)):
            view_idx=auto_idx if auto_idx is not None else max(0,len(sts)-1)
        # 若目前view站已完成且剛剛是由 mark 觸發，mark會清掉view index，所以下一站自動接手。
        for j,st in enumerate(sts):
            status=st.get("status","未開始")
            current=(j==view_idx)
            sym="?" if status=="跳過" else str(j+1)
            point_text=str(st.get("point","") or "")
            bg="#dff2dc" if current else ("#eef7eb" if status=="完成" else ("#fff4e5" if status=="跳過" else "#fffef9"))
            fg=CUTE_GREEN if current or status=="完成" else "#65716a"
            sw,sh=(178,132) if current else (148,108)
            station=tk.Canvas(line,width=sw,height=sh,bg="#fffef9",highlightthickness=0,cursor="hand2")
            station.grid(row=0,column=j*2,padx=(5,1),pady=(3,3))
            draw_round_rect(station,4,4,sw-4,sh-4,24 if current else 20,
                            fill=bg,outline=(CUTE_GREEN if current else CUTE_BORDER),width=(4 if current else 2),tags="station")
            station.create_text(sw/2,29 if current else 25,text=("●  現在" if current else sym),
                                font=(FONT,15 if current else 13,"bold"),fill=fg,tags="station")
            station.create_text(sw/2,78 if current else 65,text=point_text,
                                font=(FONT,20 if current else 16,"bold"),fill=fg,tags="station")
            if status=="完成":
                # 真正畫一條粗刪除線，避免組合字元在不同Windows字型上太細。
                half=min((sw-24)/2,max(28,len(point_text)*9.5))
                station.create_line(sw/2-half,78 if current else 65,sw/2+half,78 if current else 65,
                                    fill=CUTE_GREEN,width=5,capstyle="round",tags="station")
            station.tag_bind("station","<Button-1>",lambda e,jj=j:goto_station(jj))
            if j<len(sts)-1:
                tk.Label(line,text="━━",font=(FONT,16,"bold"),bg="#fffef9",
                         fg=(CUTE_GREEN_LINE if status=="完成" else "#d0d5d2")).grid(row=0,column=j*2+1)

        # 發呆卡不是站點：只讓這趟的時間統計失效，不改路線、庫存或完成狀態。
        def mark_dazed():
            toggle_route_daze(r,view_idx)
            sync_route_timing_sample(r,self.state_data.get("batch_id") or "")
            STORE.save(self.state_data);self.show_route()
        dazed=(r.get("timing_valid") is False and r.get("timing_invalid_reason")=="發呆")
        daze_bg="#8b6caf" if dazed else CUTE_YELLOW_SOFT
        daze_fg="white" if dazed else "#69557b"
        daze=tk.Canvas(daze_slot,width=190,height=140,bg="#fffef9",highlightthickness=0,cursor="hand2")
        daze.pack(fill="both",expand=True,padx=8,pady=12)
        draw_round_rect(daze,5,5,185,135,28,fill=daze_bg,outline=("#725599" if dazed else "#ead99a"),width=2,tags="daze")
        daze.create_text(95,42,text=("Σ(っ °Д °;)っ" if dazed else "（ ˘ω˘ ）💤"),
                         font=(FONT,16,"bold"),fill=daze_fg,tags="daze")
        daze.create_text(95,85,text=("發呆了" if dazed else "發呆卡"),font=(FONT,20,"bold"),fill=daze_fg,tags="daze")
        daze.create_text(95,112,text=("再按一次撤回" if dazed else "本趟不列入計時"),font=(FONT,10,"bold"),fill=daze_fg,tags="daze")
        daze.tag_bind("daze","<Button-1>",lambda e:mark_dazed())

        if not sts:
            tk.Label(body,text="這條路線沒有站點。",font=(FONT,20,"bold"),bg=CUTE_BG).pack(expand=True)
            return

        s=sts[view_idx]
        status=s.get("status","未開始")
        card=tk.Frame(body,bg="#fffef9",bd=0,relief="flat",padx=8,pady=5);card.pack(fill="both",expand=True,padx=26,pady=10)
        tk.Label(card,text=f"📍 當前站點｜第 {view_idx+1} 站",font=(FONT,14,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(pady=(18,2))
        tk.Label(card,text=s.get("point",""),font=(FONT,38,"bold"),bg="#fffef9",fg=CUTE_GREEN).pack(pady=5)

        ex=tk.Frame(card,bg=CUTE_GREEN_SOFT,bd=0);ex.pack(fill="x",padx=36,pady=14)
        tk.Label(ex,text=f"拿　{shown_item(s.get('input_id'),s.get('input'))} × {s.get('input_qty',1)}",font=(FONT,21,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).grid(row=0,column=0,padx=24,pady=16)
        tk.Label(ex,text="→",font=(FONT,25,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_GREEN).grid(row=0,column=1)
        tk.Label(ex,text=f"換　{shown_item(s.get('output_id'),s.get('output'))} × {s.get('output_qty',1)}",font=(FONT,21,"bold"),bg=CUTE_GREEN_SOFT,fg=CUTE_TEXT).grid(row=0,column=2,padx=24,pady=16)
        ex.columnconfigure(0,weight=1);ex.columnconfigure(2,weight=1)
        tk.Label(card,text=f"實際交換次數：{s.get('times',1)}　｜　目前狀態：{status}",
                 font=(FONT,14,"bold"),bg="#fffef9",fg=CUTE_TEXT).pack(pady=4)

        nav=tk.Frame(card,bg="#fffef9");nav.pack(pady=(4,6))
        tk.Button(nav,text="← 上一站",font=(FONT,12,"bold"),
                  state=("normal" if view_idx>0 else "disabled"),
                  command=lambda:goto_station(view_idx-1)).grid(row=0,column=0,padx=6)
        _auto_now=first_unhandled_index(r)
        if _auto_now is not None and _auto_now!=view_idx:
            tk.Button(nav,text="◎ 回目前站",font=(FONT,12,"bold"),
                      command=lambda:goto_station(_auto_now)).grid(row=0,column=1,padx=6)
        tk.Button(nav,text="下一站 →",font=(FONT,12,"bold"),
                  state=("normal" if view_idx<len(sts)-1 else "disabled"),
                  command=lambda:goto_station(view_idx+1)).grid(row=0,column=2,padx=6)

        _mode_now=load_inventory_mode()
        tk.Label(card,text=("🧪 測試庫存" if _mode_now=="test" else "🟢 正式庫存"),
                 font=(FONT,10,"bold"),bg="#fffef9",fg=("#8a4b08" if _mode_now=="test" else "#157347")).pack(pady=(4,0))
        actions=tk.Frame(card,bg="#fffef9");actions.pack(pady=14)
        _actual_current=first_unhandled_index(r)
        if status=="未開始" and view_idx==_actual_current:
            complete_btn=tk.Canvas(actions,width=230,height=68,bg="#fffef9",highlightthickness=0,cursor="hand2")
            complete_btn.grid(row=0,column=0,padx=10)
            draw_round_rect(complete_btn,3,3,227,65,22,fill=CUTE_GREEN,outline=CUTE_GREEN,width=1,tags="complete")
            complete_btn.create_text(115,34,text="✓ 完成這站",font=(FONT,18,"bold"),fill="white",tags="complete")
            complete_btn.tag_bind("complete","<Button-1>",lambda e:self.mark("完成"))
            self.btn(actions,"？ 跳過",lambda:self.mark("跳過"),18,10).grid(row=0,column=1,padx=10)
        elif status=="未開始":
            tk.Label(actions,text="👀 正在回看路線｜回目前站才能完成交換",
                     font=(FONT,12,"bold"),bg="#fffef9",fg="#667085").grid(row=0,column=0,padx=10)
        else:
            tk.Button(actions,text="↶ 撤銷這站",font=(FONT,15,"bold"),
                      command=lambda j=view_idx:self.undo_station(j),padx=18,pady=9).grid(row=0,column=0,padx=6)
            tk.Button(actions,text="← 回總覽",font=(FONT,14,"bold"),
                      command=lambda:leave_route("routes"),padx=16,pady=9).grid(row=0,column=1,padx=6)

        # 5階品等整趟最後一站完成後，再集中提醒回港整理；中途永遠不擋下一站。
        if False and sts and first_unhandled_index(r) is None:
            _route_sales=[x for x in t5_sale_infos_for_route(r) if x.get("excess",0)>0]
            if _route_sales:
                sellbox=tk.Frame(card,bg="#fff8e7",bd=1,relief="solid")
                sellbox.pack(fill="x",padx=28,pady=(0,10))
                tk.Label(sellbox,text="🏝 回伊利亞整理｜本趟5階溢出",
                         font=(FONT,13,"bold"),bg="#fff8e7",fg="#8a4b08").pack(anchor="w",padx=12,pady=(9,3))
                for _sale5 in _route_sales:
                    _oid5=_sale5["output_id"];_cur5=_sale5["current"];_cap5=_sale5["cap"]
                    _ex5=_sale5["excess"];_name5=_sale5["display_name"]
                    row5=tk.Frame(sellbox,bg="#fff8e7");row5.pack(fill="x",padx=8,pady=3)
                    tk.Label(row5,text=f"{_name5}：目前 {_cur5}｜保留 {_cap5}｜待出售 {_ex5}",
                             font=(FONT,11,"bold"),bg="#fff8e7",fg="#8a4b08").pack(side="left",padx=4)
                    _source5=next((st for st in sts if str(st.get("output_id") or "")==str(_oid5)),{})
                    def _sell_here(iid=_oid5,nm=_name5,n=_ex5,source=_source5):
                        if not messagebox.askyesno("確認已賣出",f"你已經在遊戲內賣出「{nm}」多餘的 {n} 個？\n\n確認後助手庫存才會扣除並寫入帳本。",parent=self._route_dialog_parent()):return
                        ok,msg=sell_t5_excess(iid,nm,r,source)
                        if not ok:messagebox.showwarning("無法出售",str(msg),parent=self._route_dialog_parent());return
                        STORE.save(self.state_data);self.open_route(r)
                    tk.Button(row5,text=f"✓ 已賣出 {_ex5}",font=(FONT,11,"bold"),
                              command=_sell_here,padx=10,pady=4).pack(side="right",padx=4)

        # 舊原型明確顯示下一站；完成/跳過後自動指向第一個未處理站。
        nxt=None
        _basis=first_unhandled_index(r)
        if _basis is not None:
            for j in range(_basis+1,len(sts)):
                if sts[j].get("status","未開始")=="未開始":
                    nxt=j;break
        nextbox=tk.Frame(card,bg=CUTE_BLUE_SOFT,bd=0,relief="flat",padx=5,pady=3)
        nextbox.pack(fill="x",padx=36,pady=(8,12))
        if nxt is None:
            finish=tk.Frame(nextbox,bg=CUTE_BLUE_SOFT);finish.pack(pady=7)
            add_mascot(finish,bg=CUTE_BLUE_SOFT,size=145,opacity=0.96,side="left",padx=8)
            tk.Label(finish,text="🎉 航線完成！小海豹已在碼頭等你回家～\n今天也是可靠的海豹船長！",
                     font=(FONT,18,"bold"),bg=CUTE_BLUE_SOFT,fg="#315f91",justify="left").pack(side="left",padx=10)
        else:
            tk.Label(nextbox,text="🚩 下一站",font=(FONT,11,"bold"),bg=CUTE_BLUE_SOFT,fg="#5479a7").pack(pady=(8,0))
            tk.Label(nextbox,text=sts[nxt].get("point",""),font=(FONT,18,"bold"),
                     bg=CUTE_BLUE_SOFT,fg="#315f91").pack(pady=(0,9))

        sec=tk.Frame(card,bg="#fffef9");sec.pack(pady=(0,16))
        tk.Button(sec,text="⏸ 暫停",font=(FONT,12,"bold"),command=self.pause_route).grid(row=0,column=0,padx=6)
        tk.Button(sec,text="🧭 換路線",font=(FONT,12,"bold"),
                  command=lambda:leave_route("routes")).grid(row=0,column=1,padx=6)

        # 讓目前站盡量出現在橫向捷運可視區附近。
        try:
            self.after(80,lambda:cv.xview_moveto(max(0,min(1,(view_idx/max(1,len(sts)-1))-0.18))))
        except Exception:pass

    def undo_station(self,index):
        r=self.current_route
        sts=r.get("stations",[])
        if not (0<=index<len(sts)):return
        st=sts[index]
        if st.get("status","未開始")=="未開始":return
        if not messagebox.askyesno("撤銷這站",f"把「{st.get('point','這一站')}」恢復成未開始？",parent=self._route_dialog_parent()):
            return
        if st.get("status")=="完成":
            _source_tx_before_undo=st.get("inventory_tx_id")
            ok,msg=reverse_station_inventory(st,"撤銷這站")
            if not ok:
                messagebox.showwarning("庫存未還原",str(msg),parent=self._route_dialog_parent())
                return
            reverse_linked_t5_sales({"inventory_tx_id":_source_tx_before_undo},"撤銷這站")
        st["status"]="未開始"
        self.state_data.pop("journey_ending_shown_for",None)
        if r.get("timing_finished_at"):
            reset_route_timer(r);route_timer_start(r)
        else:
            r["timing_station_checkpoints"]=[x for x in r.get("timing_station_checkpoints",[]) or []
                                             if int(x.get("station_index",-1))<index]
        r["status"]="進行中"
        r["current_index"]=index
        r["view_index"]=index
        self._route_view_index=index
        STORE.save(self.state_data)
        self.show_route()

    def mark(self,result):
        # 舊設計：完成/跳過後自動跳到第一個尚未處理站。
        auto=first_unhandled_index(self.current_route)
        view=getattr(self,"_route_view_index",auto)
        if auto is None:return
        if view!=auto:
            self._route_view_index=auto
        st=self.current_route.get("stations",[])[auto]
        if result=="完成":
            ok,msg=apply_station_inventory(st,self.current_route.get("name",""),auto)
            if not ok:
                messagebox.showwarning("不能完成這站",str(msg),parent=self._route_dialog_parent())
                return
        process_station(self.current_route,result)
        if result=="完成":record_route_station_checkpoint(self.current_route,st,auto)
        nxt=first_unhandled_index(self.current_route)
        if nxt is None and route_timer_finish(self.current_route):
            sync_route_timing_sample(self.current_route,self.state_data.get("batch_id") or "")
        self.current_route["view_index"]=(nxt if nxt is not None else max(0,len(self.current_route.get("stations",[]))-1))
        self._route_view_index=None
        STORE.save(self.state_data)
        self.show_route()
        if nxt is None:self.after(120,self.maybe_show_journey_ending)

    def pause_route(self):
        route_timer_pause(self.current_route)
        self.current_route["status"]="暫停"
        STORE.save(self.state_data)
        self.show_routes()

    def switch_route(self):
        route_timer_pause(self.current_route)
        self.current_route["status"]="暫停"
        STORE.save(self.state_data)
        self.show_routes()

if __name__=="__main__":
    App().mainloop()
