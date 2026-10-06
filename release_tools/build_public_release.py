import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from audit_public_release import audit

RUNTIME_PY=("app.py","batch_service.py","crop_calibrator.py","exchange_parser.py","inventory.py",
            "layout_assigner.py","master_data.py","ocr_pipeline.py","route_engine.py","shared_paths.py","state_store.py")
PUBLIC_SEEDS=("inventory.json","inventory_catalog.json","player_item_aliases_seed.json",
              "player_point_preferences_seed.json","search_alias_seed.json","search_master_data.json","ocr_public_alias_seed.json",
              "ratio_confirmations_seed.json")
PUBLIC_DOCS=("README_PUBLIC.txt","V5.82.0_海豹學習成熟度整合.txt","V5.84.0_實測修正整合版.txt","V5.85.0_OCR底層規則整理版.txt","V5.85.7_OCR階段收尾.txt","V5.89.0_玩家版母版乾淨交付.txt","黑沙航海助手_核心設計與版本交接.md",
             "V5.90.3_成熟ID撞號與視窗記憶修正.txt","V5.90.4_玩家介面與測試庫存對帳修正.txt",
             "V5.90.5_核心確認與路線身份同步修正.txt",
             "V5.90.6_成熟知識信任修正與學習履歷.txt",
             "V5.90.7_同物異ID與確認流程收斂.txt",
             "V5.90.8_一般材料路線與剩餘次數容錯.txt",
             "V5.90.9_路線特殊分頁分類修正.txt",
             "V5.91.0_OCR學習命中補強.txt",
             "V5.91.1_特殊交換檢查與數字交接修正.txt",
             "V5.91.2_證物切片同資料夾鎖定.txt",
             "V5.91.3_一般材料符號解析修正.txt",
             "V5.91.5_動態特殊品OUTPUT數字救援.txt",
             "V5.91.6_OUTPUT數字1輪廓救援.txt",
             "V5.91.8_玩家資料完全隔離.txt",
             "V5.91.9_INPUT比例與證物視窗記憶.txt",
             "V5.92.0_交涉力小數與交付收尾.txt",
             "V5.92.1_空白欄上下文與其他分頁.txt",
             "V5.92.2_INPUT數字專線與空白鑄塊.txt",
             "V5.92.3_本輪數字優先與數字型空白身份.txt",
             "V5.92.4_本批覆蓋阻斷與鉛鑄塊情境.txt",
             "V5.92.5_救援包實播與缺字身份學習.txt",
             "V5.93.0_版面自救健檢與放大吸附校正.txt",
             "V5.93.1_舊電腦AB版與OCR變形相容.txt",
             "V5.93.2_舊UI陪段與健檢順序修正.txt",
             "V5.93.3_全品項數字1輪廓救援.txt",
             "V5.93.4_自訂版面選擇與隋段相容.txt",
             "V5.93.6_裁切像素與數字框模板.txt",
             "BDO_Sailing_Assistant.spec","BUILD_EXE.cmd","BUILD_EXE_CORE.cmd","建立Windows_EXE.bat","requirements-public.txt")


def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()


def build(source,target):
    source=Path(source).resolve();target=Path(target).resolve()
    if source==target or source in target.parents:raise ValueError("公開輸出資料夾不可等於或包住原始專案")
    if target.exists():shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in RUNTIME_PY+PUBLIC_SEEDS+PUBLIC_DOCS:
        shutil.copy2(source/name,target/name)
    shutil.copytree(source/"assets",target/"assets")
    shutil.copytree(source/"release_tools",target/"release_tools",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    (target/"data").mkdir();shutil.copy2(source/"data"/"master_data.json",target/"data"/"master_data.json")
    for name in ("crop_layouts_seed.json","observed_item_registry_seed.json","ocr_visual_item_memory_seed.json"):
        shutil.copy2(source/"data"/name,target/"data"/name)
    shutil.copytree(source/"data"/"mature_knowledge",target/"data"/"mature_knowledge")
    # 公開版只保留118個正式ID的空殼；None表示玩家尚未盤點，絕對不是0。
    inventory=json.load(open(target/"inventory.json",encoding="utf-8-sig"))
    inventory={str(k):None for k in inventory}
    with open(target/"inventory.json","w",encoding="utf-8") as f:json.dump(inventory,f,ensure_ascii=False,indent=2)
    with open(target/"PUBLIC_PLAYER_RELEASE.json","w",encoding="utf-8") as f:
        json.dump({"format":"BDO_PUBLIC_PLAYER_RELEASE_V1","data_scope":"BDO_Sailing_Assistant_Player"},f,ensure_ascii=False,indent=2)
    public_registry=json.load(open(target/"data"/"observed_item_registry_seed.json",encoding="utf-8"))
    public_aliases=json.load(open(target/"ocr_public_alias_seed.json",encoding="utf-8"))
    public_visuals=json.load(open(target/"data"/"ocr_visual_item_memory_seed.json",encoding="utf-8"))
    manifest={"format":"BDO_SAILING_MATURE_KNOWLEDGE_V2","version":"V6.1","privacy":
              "不含玩家庫存數值、路線、OCR證物、人工確認、船隻、設定、計時或海豹歷史。",
              "public_defaults":["V5.85.7完整玩家版功能","V5.85.7實跑待確認1的完整成熟後台",
                                 "57筆OCR別名","72筆人工確認","83筆人工修復","44筆欄位身份修正",
                                 "44筆品項視覺記憶","6筆數字視覺記憶","401筆比例規則","玩家本機永久自學"],"files":{}}
    manifest["verified_public_knowledge"]={
        "confirmed_item_ids":len(public_registry.get("items",[])),
        "ocr_aliases":len(public_aliases),
        "item_visual_signatures":len(public_visuals),
        "source_batch_pending":0,
        "note":"由2026-09-17待確認0的實跑資料去個資後升級；歧義或過短文字不作全域強制別名。",
    }
    with open(target/"PUBLIC_RELEASE_MANIFEST.json","w",encoding="utf-8") as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.name!="PUBLIC_RELEASE_MANIFEST.json":manifest["files"][p.relative_to(target).as_posix()]=sha256(p)
    with open(target/"PUBLIC_RELEASE_MANIFEST.json","w",encoding="utf-8") as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
    result=audit(target)
    if not result["ok"]:raise RuntimeError("公開版稽核失敗：\n"+"\n".join(result["issues"]))
    return result


if __name__=="__main__":
    source=sys.argv[1] if len(sys.argv)>1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target=sys.argv[2] if len(sys.argv)>2 else os.path.join(os.path.dirname(os.path.abspath(source)),"MATURE_KNOWLEDGE_V6.1")
    print(json.dumps(build(source,target),ensure_ascii=False,indent=2))
