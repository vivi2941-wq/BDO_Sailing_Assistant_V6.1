# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

rapid_datas, rapid_bins, rapid_hidden = collect_all('rapidocr_onnxruntime')
onnx_datas, onnx_bins, onnx_hidden = collect_all('onnxruntime')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=rapid_bins + onnx_bins,
    datas=rapid_datas + onnx_datas + [
        ('data/master_data.json','data'),
        ('data/crop_layouts_seed.json','data'),('data/observed_item_registry_seed.json','data'),
        ('data/ocr_visual_item_memory_seed.json','data'),('ocr_public_alias_seed.json','.'),('assets','assets'),
        ('data/mature_knowledge','data/mature_knowledge'),
        ('inventory.json','.'),('inventory_catalog.json','.'),
        ('ratio_confirmations_seed.json','.'),
        ('player_item_aliases_seed.json','.'),('player_point_preferences_seed.json','.'),
        ('search_alias_seed.json','.'),('search_master_data.json','.'),
        ('PUBLIC_RELEASE_MANIFEST.json','.'),
        ('PUBLIC_PLAYER_RELEASE.json','.'),
    ],
    hiddenimports=rapid_hidden + onnx_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='黑沙航海助手',debug=False,bootloader_ignore_signals=False,
          strip=False,upx=True,console=False,icon='assets/sailing_assistant.ico')
coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=True,upx_exclude=[],name='BDO_Sailing_Assistant_V6.1')
