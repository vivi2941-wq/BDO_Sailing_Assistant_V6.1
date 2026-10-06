import re
import difflib
import collections
from statistics import median

def _cx(box):
    xs=[p[0] for p in box]
    return sum(xs)/len(xs)

def _cy(box):
    ys=[p[1] for p in box]
    return sum(ys)/len(ys)

def _h(box):
    ys=[p[1] for p in box]
    return max(ys)-min(ys)

def _w(box):
    xs=[p[0] for p in box]
    return max(xs)-min(xs)

def _norm(s):
    s="".join(str(s or "").strip().split())
    # Common BDO OCR Traditional/Simplified variants.  This is only for matching;
    # displayed/master names remain untouched.
    table=str.maketrans({
        "島":"岛","黃":"黄","賊":"贼","餘":"余","換":"换","數":"数",
        "罈":"罐","瑪":"玛","麗":"丽","燈":"灯","鹽":"盐","幣":"币",
        "華":"华","團":"团","騎":"骑","頭":"头","龍":"龙","樹":"树",
        "塊":"块","輕":"轻","盈":"盈","觀":"观","測":"测","報":"报",
        "書":"书","鴉":"鸦","烏":"乌","齊":"齐","遺":"遗","運":"运","裴":"裴",
        "結":"结","質":"质","貿":"贸","純":"纯",
        "維":"维","藍":"蓝","鑄":"铸","鏡":"镜","偵":"侦",
        "戰":"战","鬥":"斗","糧":"粮","統":"统","傳":"传","遠":"远","楓":"枫"
    })
    return s.translate(table)

def _v525_identity_trace(kind, raw_text, expected_stage, result, extra=None):
    """Append compact identity-resolution evidence for unresolved/new-batch cases."""
    try:
        import json, os, datetime
        base=os.path.dirname(os.path.abspath(__file__))
        d=os.path.join(base,"data")
        os.makedirs(d,exist_ok=True)
        rec={
            "time":datetime.datetime.now().isoformat(timespec="seconds"),
            "kind":kind,
            "raw":raw_text,
            "expected_stage":expected_stage,
            "matched":bool(result.get("matched")) if isinstance(result,dict) else False,
            "id":result.get("id") if isinstance(result,dict) else None,
            "via":result.get("via") if isinstance(result,dict) else None,
            "matched_text":result.get("matched_text") if isinstance(result,dict) else None,
        }
        if extra: rec.update(extra)
        with open(os.path.join(d,"v525_identity_trace.jsonl"),"a",encoding="utf-8") as f:
            f.write(json.dumps(rec,ensure_ascii=False)+"\n")
    except Exception:
        pass


class ExchangeCardParser:
    """
    V3.5：不再把整張圖用Y座標粗暴切列。
    改成：
    1) 先找POINT名稱作為交換卡錨點
    2) 找錨點附近、下一個POINT之前的文字當作同一卡
    3) 從卡內抓 [N段]品項
    4) 依畫面X位置/閱讀順序判定 輸入 -> 輸出
    5) 抓「剩餘交換次數」
    """
    def __init__(self, master):
        self.master=master

    def _resolve_point_loose(self,text):
        raw=str(text or "").strip()
        direct=self.master.resolve_point(raw)
        if direct.get("matched"):
            return direct
        n=_norm(raw)
        # OCR may append 島 / 村莊 / 海岸 or slightly vary spacing.
        for p in self.master.points:
            pn=_norm(p.get("name",""))
            if not pn:
                continue
            if pn in n or n in pn:
                if min(len(pn),len(n))>=3:
                    return {"matched":True,"id":p["id"],"record":p,"via":"包含比對"}
        for a in self.master.aliases:
            if a.get("object_type")!="點位":
                continue
            an=_norm(a.get("alias",""))
            if an and (an in n or n in an) and min(len(an),len(n))>=3:
                rec=self.master.point_by_id.get(a["id"])
                if rec:
                    return {"matched":True,"id":rec["id"],"record":rec,"via":"別名包含"}

        # OCR point-name typo tolerance.  Require a strong score to avoid silently
        # mapping one real island to another.
        fuzzy=[]
        labels=[]
        for p in self.master.points:
            labels.append((p,p.get("name",""),"點位近似"))
        for a in self.master.aliases:
            if a.get("object_type")=="點位":
                rec=self.master.point_by_id.get(a.get("id"))
                if rec: labels.append((rec,a.get("alias",""),"別名近似"))
        for rec,label,via in labels:
            ln=_norm(label)
            if len(ln)>=3 and len(n)>=3:
                ratio=difflib.SequenceMatcher(None,n,ln).ratio()
                if ratio>=0.78:
                    fuzzy.append((ratio,len(ln),rec,label,via))
        if fuzzy:
            fuzzy.sort(key=lambda x:(x[0],x[1]),reverse=True)
            ratio,_,rec,label,via=fuzzy[0]
            return {"matched":True,"id":rec["id"],"record":rec,
                    "via":f"{via}({ratio:.2f})","matched_text":label}
        return {"matched":False,"query":raw}

    def _resolve_item_loose(self,text,expected_stage=None):
        _trace_raw=text
        raw=str(text or "").strip()
        cleaned=re.sub(r"^[\[\【（(]?\s*[0-7]\s*(?:階|楷|陪|隋)?\s*段\s*[\]\】）)]?\s*","",raw).strip()
        direct=self.master.resolve_item(cleaned)
        if direct.get("matched"):
            return direct
        n=_norm(cleaned)

        # V5.31.3.3：新題庫已兩次證實「海上戰鬥糧食」會被OCR吃成這類形狀。
        # 只在expected_stage=1時啟用，避免跨階誤認。
        if expected_stage==1:
            compact=n
            known_stage1_aliases=("海上开食","海上门粒食","海上门糧食","海上門糧食")
            if any(_norm(a)==compact for a in known_stage1_aliases):
                rec=next((x for x in self.master.items
                          if x.get("stage")==1 and x.get("name")=="海上戰鬥糧食"),None)
                if rec:
                    return {"matched":True,"id":rec["id"],"record":rec,
                            "via":"V5.31.3.3實測OCR別名","matched_text":"海上戰鬥糧食"}

        candidates=[]
        for item in self.master.items:
            for label in (item.get("name",""),item.get("abbr","")):
                ln=_norm(label)
                if ln and len(ln)>=2 and (ln in n or n in ln):
                    candidates.append((len(ln),item,label,"包含比對"))
        for a in self.master.aliases:
            if a.get("object_type")!="品項":
                continue
            an=_norm(a.get("alias",""))
            if an and len(an)>=2 and (an in n or n in an):
                rec=self.master.item_by_id.get(a["id"])
                if rec:
                    candidates.append((len(an),rec,a["alias"],"別名包含"))
        if candidates:
            candidates.sort(key=lambda x:x[0],reverse=True)
            _,rec,label,via=candidates[0]
            return {"matched":True,"id":rec["id"],"record":rec,"via":via,"matched_text":label}

        # V5.31.3.3：階段本身就是很強的身份證據。
        # 舊版把所有階級混在一起比，門檻只能設到0.82，像「海上門糧食」永遠進不來。
        # 現在畫面已明確讀到 [N階段] 時，只在該階正式名冊內競賽。
        fuzzy=[]
        for item in self.master.items:
            if expected_stage not in (None,""):
                try:
                    if int(item.get("stage",0) or 0)!=int(expected_stage): continue
                except Exception:
                    continue
            for label in (item.get("name",""),item.get("abbr","")):
                ln=_norm(label)
                if len(ln)>=3 and len(n)>=3:
                    ratio=difflib.SequenceMatcher(None,n,ln).ratio()
                    common=len(set(n) & set(ln))
                    fuzzy.append((ratio,common,len(ln),item,label))
        if fuzzy:
            fuzzy.sort(key=lambda x:(x[0],x[1],x[2]),reverse=True)
            best=fuzzy[0]
            second=next((x for x in fuzzy[1:] if x[3].get("id")!=best[3].get("id")),None)
            gap=best[0]-(second[0] if second else 0.0)
            # 有階級時可較寬鬆，但第一名必須明顯勝出；無階級仍維持舊安全門。
            safe=(best[0]>=0.82) if expected_stage in (None,"") else (
                best[0]>=0.60 and best[1]>=2 and gap>=0.10
            )
            if safe:
                ratio,_,_,rec,label=best
                return {"matched":True,"id":rec["id"],"record":rec,
                        "via":f"V5.31.3.3階級名冊近似({ratio:.2f},差{gap:.2f})","matched_text":label}
        return {"matched":False,"query":raw}

    def _prepare(self,raw_rows):
        out=[]
        for row in raw_rows:
            box,text,score=row
            out.append({
                "box":box,"text":str(text).strip(),"score":float(score),
                "cx":_cx(box),"cy":_cy(box),"h":max(1,_h(box)),"w":max(1,_w(box))
            })
        out.sort(key=lambda x:(x["cy"],x["cx"]))
        return out

    def _find_point_anchors(self,lines):
        anchors=[]
        for i,line in enumerate(lines):
            p=self._resolve_point_loose(line["text"])
            if p.get("matched"):
                anchors.append({
                    "idx":i,"line":line,"point_id":p["id"],
                    "point_name":p["record"]["name"],"via":p.get("via","")
                })
        # Deduplicate same point recognized multiple times nearby.
        cleaned=[]
        for a in anchors:
            if cleaned and a["point_id"]==cleaned[-1]["point_id"] and abs(a["line"]["cy"]-cleaned[-1]["line"]["cy"])<35:
                continue
            cleaned.append(a)
        return cleaned

    def _card_ranges(self,lines,anchors):
        if not anchors:
            return []
        heights=[x["h"] for x in lines] or [24]
        medh=median(heights)
        cards=[]
        for i,a in enumerate(anchors):
            start_y=a["line"]["cy"]-medh*1.4
            if i+1<len(anchors):
                end_y=(a["line"]["cy"]+anchors[i+1]["line"]["cy"])/2
            else:
                end_y=a["line"]["cy"]+medh*6.5
            card_lines=[x for x in lines if start_y<=x["cy"]<end_y]
            cards.append((a,card_lines))
        return cards

    def _parse_card(self,anchor,lines,filename):
        texts=[x["text"] for x in lines if x["text"]]
        staged=[]
        for x in lines:
            m=re.search(r"(?:^|\s)[\[\【（(]?\s*([0-7])\s*(?:階|楷|陪|隋)?\s*段\s*[\]\】）)]?\s*(.+)",x["text"])
            if not m:
                continue
            stage=int(m.group(1))
            item_text=m.group(2).strip()
            # Strip trailing UI fragments if OCR glued them on.
            item_text=re.split(r"\s+\|\s+|交涉力|剩餘交換次數",item_text)[0].strip()
            r=self._resolve_item_loose(item_text,stage)
            _v525_identity_trace("ITEM",item_text,stage,r,{"side":side if "side" in locals() else None})
            staged.append({
                "stage":stage,"raw":item_text,"cx":x["cx"],"cy":x["cy"],
                "score":x["score"],
                "id":r.get("id",""),
                "name":r.get("record",{}).get("name","") if r.get("matched") else "",
                "matched":bool(r.get("matched"))
            })

        # Order by X first when two items are on same horizontal band; otherwise reading order.
        staged.sort(key=lambda x:(round(x["cy"]/35),x["cx"]))

        remaining=None
        negotiation=None
        joined=" | ".join(texts)

        # OCR可能把「剩餘交換次數」「：5次」拆成不同token，所以整張卡一起找。
        # Normalize separators because OCR often inserts pipes/spaces between label and value.
        flat=re.sub(r"[|｜]", " ", joined)
        m=re.search(r"剩[餘余]\s*交[換换]\s*次[數数]+\s*[:：]?\s*(\d+)\s*次?", flat)
        if m:
            remaining=int(m.group(1))
        else:
            m=re.search(r"交換\s*次數.{0,24}?(\d+)\s*次", flat)
            if m:
                remaining=int(m.group(1))

        m=re.search(r"交涉力\s*[:：]?\s*(?:需要)?\s*([\d,]+)", joined)
        if m:
            try: negotiation=int(m.group(1).replace(",",""))
            except: pass

        # Quantities: black desert card may show a standalone number between staged goods.
        # V3.5 keeps candidates only; next version will map them with fixed card geometry.
        numeric_tokens=[]
        for x in lines:
            tt=x["text"].strip()
            if re.fullmatch(r"\d{1,4}",tt):
                numeric_tokens.append({"value":int(tt),"cx":x["cx"],"cy":x["cy"]})

        inp=staged[0] if len(staged)>=1 else None
        outp=staged[1] if len(staged)>=2 else None

        issues=[]
        if not inp: issues.append("找不到輸入段位品項")
        elif not inp["matched"]: issues.append("輸入品項未匹配ITEM_ID")
        if not outp: issues.append("找不到輸出段位品項")
        elif not outp["matched"]: issues.append("輸出品項未匹配ITEM_ID")
        if remaining is None: issues.append("找不到剩餘交換次數")

        min_conf=min([x["score"] for x in lines] or [0])
        if min_conf<0.55: issues.append("OCR低信心")

        return {
            "source_file":filename,
            "point_id":anchor["point_id"],
            "point_name":anchor["point_name"],
            "point_match_via":anchor["via"],
            "input_stage":inp["stage"] if inp else "",
            "input_item_id":inp["id"] if inp else "",
            "input_name":inp["name"] if inp else "",
            "input_raw":inp["raw"] if inp else "",
            "output_stage":outp["stage"] if outp else "",
            "output_item_id":outp["id"] if outp else "",
            "output_name":outp["name"] if outp else "",
            "output_raw":outp["raw"] if outp else "",
            "remaining_times":remaining if remaining is not None else "",
            "numeric_tokens":numeric_tokens,
            "ocr_min_confidence":round(min_conf,4),
            "issues":issues,
            "status":"候選可用" if not issues else "待確認",
            "raw_texts":texts
        }

    def parse_image(self,raw_rows,filename=""):
        lines=self._prepare(raw_rows)
        anchors=self._find_point_anchors(lines)
        cards=self._card_ranges(lines,anchors)
        parsed=[]
        for a,ls in cards:
            card=self._parse_card(a,ls,filename)
            # 防止聊天/系統通知中剛好出現島名就被當交換卡。
            # 真交換卡至少要看見一個 [N段] 品項。
            has_stage=bool(card.get("input_stage") or card.get("output_stage"))
            if not has_stage:
                continue
            parsed.append(card)
        return parsed

    def _text_join(self, field):
        return " | ".join(x.get("text","") for x in field if x.get("text"))

    def _resolve_field_item(self, field):
        candidates=[]
        for i,x in enumerate(field):
            t=x.get("text","")
            # 不同電腦/UI縮放常漏掉左括號，或把「階段」讀成「楷段」。
            # 段位標籤也可能和品名拆成相鄰兩段，仍應把正式品名送進成熟ID庫。
            m=re.search(r"(?:^|\s)[\[\【（(]?\s*([0-7])\s*(?:階|楷|陪|隋)?\s*段\s*[\]\】）)]?\s*(.*)",t)
            if m:
                stage=int(m.group(1))
                raw=m.group(2).strip()
                if not raw:
                    for nxt in field[i+1:i+4]:
                        nt=str(nxt.get("text","") or "").strip()
                        if not nt or re.fullmatch(r"[0-9Xx|｜子]+",nt):
                            continue
                        if re.search(r"剩餘交換|交涉力",nt):
                            continue
                        raw=nt;break
                if not raw:
                    continue
                # V5.31.3.3：這裡才是V5.24~26身份橋接沒吃到新題庫的真兇。
                # segmented row明明已解析出stage，卻呼叫 _resolve_item_loose(raw) 把stage丟掉。
                # 現在正式把stage傳進去，才會真的啟用「只在同階名冊競賽」。
                r=self._resolve_item_loose(raw,stage)
                candidates.append((stage,raw,r,float(x.get("score",0))))
        if not candidates:
            return None
        # Prefer successfully resolved candidate, then OCR confidence.
        candidates.sort(key=lambda z:(bool(z[2].get("matched")),z[3]),reverse=True)
        stage,raw,r,score=candidates[0]
        return {
            "stage":stage,"raw":raw,
            "id":r.get("id",""),
            "name":r.get("record",{}).get("name","") if r.get("matched") else "",
            "matched":bool(r.get("matched")),
            "score":score
        }

    def _resolve_expected_stage_output(self, field, expected_stage):
        """
        V5.22｜7階名冊圍毆
        高階OUTPUT不再要求某一次OCR讀出接近完整品名。
        把 normal/contrast/sharp/bright 等版本留下的殘字一起拿去和「下一階正式名冊」比對。

        安全原則：
        - 只搜尋 expected_stage（6階INPUT=>只搜7階）
        - 1~2字碎片只能加弱分，不能單獨定案
        - 至少要有一個 >=3字的有效碎片，或多版本共同支持同一候選
        - 第一名必須明顯勝過第二名
        - 不夠確定就繼續留鬼，不硬猜
        """
        def clean_text(t):
            t=str(t or "").strip()
            t=re.sub(r"^[\[\【（(]?\s*[0-7]\s*(?:階|楷|陪|隋)?\s*段\s*[\]\】）)]?\s*","",t)
            t=re.sub(r"^[0-9]+\s*[|｜]?\s*","",t)
            # UI常見殘留
            t=re.sub(r"[⚓①②③④⑤⑥⑦⑧⑨⑩]","",t)
            return _norm(t)

        frags=[]
        for x in field or []:
            raw=str(x.get("text","")).strip()
            n=clean_text(raw)
            if not n: continue
            try: conf=float(x.get("score",0) or 0)
            except Exception: conf=0.0
            variant=str(x.get("rescue_variant") or "normal")
            frags.append((n,conf,variant,raw))

        if not frags:
            return None

        # 去掉完全重複，但保留「同樣殘字由不同版本讀到」這件事作為投票證據。
        candidates=[]
        for item in self.master.items:
            try: st=int(item.get("stage",0) or 0)
            except Exception: st=0
            if st!=expected_stage: continue
            name=str(item.get("name","") or "")
            abbr=str(item.get("abbr","") or "")
            labels=[x for x in (name,abbr) if len(_norm(x))>=2]
            if not labels: continue

            best_per_variant={}
            strong_frag=False
            support_variants=set()
            exact_chars=0
            best_single=0.0

            for frag,conf,variant,raw in frags:
                flen=len(frag)
                local=0.0
                for label in labels:
                    ln=_norm(label)
                    ratio=difflib.SequenceMatcher(None,frag,ln).ratio()
                    if frag in ln:
                        # 完整殘字確實出現在正式名中，比一般SequenceMatcher更有價值。
                        ratio=max(ratio, 0.58 if flen==1 else 0.72 if flen==2 else 0.88)
                    elif ln in frag:
                        ratio=max(ratio,0.93)

                    # 字元交集：OCR順序壞掉時仍能提供一點證據。
                    common=sum((collections.Counter(frag) & collections.Counter(ln)).values())
                    coverage=common/max(1,min(len(frag),len(ln)))
                    ratio=max(ratio, coverage*(0.58 if flen<=2 else 0.78))
                    local=max(local,ratio)

                # 1字「子」這類只能是弱證據，絕不讓它自己殺鬼。
                if flen==1: local*=0.34
                elif flen==2: local*=0.62
                else: strong_frag=True

                weighted=local*(0.65+0.35*max(0.0,min(1.0,conf)))
                best_single=max(best_single,weighted)
                if weighted>=0.50:
                    support_variants.add(variant)
                exact_chars=max(exact_chars,flen if local>=0.82 else 0)
                best_per_variant[variant]=max(best_per_variant.get(variant,0.0),weighted)

            # 每個影像版本最多投一票，避免某版OCR吐很多碎片灌票。
            vals=sorted(best_per_variant.values(),reverse=True)
            top3=vals[:3]
            vote=(sum(top3)/len(top3)) if top3 else 0.0
            multi_bonus=min(0.12,0.035*max(0,len(support_variants)-1))
            score=min(1.0,0.62*best_single+0.38*vote+multi_bonus)

            candidates.append((score,best_single,len(support_variants),exact_chars,item))

        if not candidates:
            return None
        candidates.sort(key=lambda z:(z[0],z[1],z[2],z[3]),reverse=True)
        best=candidates[0]
        second=next((z for z in candidates[1:] if z[4].get("id")!=best[4].get("id")),None)
        gap=best[0]-(second[0] if second else 0.0)

        # 安全門：
        # A. 至少一個>=3字碎片且分數夠高；或
        # B. 至少兩種OCR版本共同支持同一候選，且總分更高。
        enough_evidence=(best[3]>=3 and best[0]>=0.62) or (best[2]>=2 and best[0]>=0.68)
        if not enough_evidence or gap<0.055:
            return None

        item=best[4]
        return {
            "stage":expected_stage,
            "raw":" / ".join(dict.fromkeys(x[3] for x in frags))[:240],
            "id":item.get("id",""),
            "name":item.get("name",""),
            "matched":True,
            "score":round(best[0],4),
            "via":f"V5.22名冊圍毆({best[0]:.2f},差{gap:.2f},版本{best[2]})"
        }

    def _field_quantity(self, field, kind=""):
        """V5.8：從欄位OCR token抽取交換數量；排除段位、交涉力等非比例數字。"""
        nums=[]
        for x in field or []:
            t=str(x.get("text","")).strip()
            # standalone quantity token is the strongest signal
            if re.fullmatch(r"[0-9]{1,5}",t):
                v=int(t)
                if 1 <= v <= 99999:
                    nums.append((v,float(x.get("score",0)),t))
                    continue
            # tolerate x2 / ×2 / 2個 / 2個交換品 style tokens
            m=re.fullmatch(r"(?:[xX×]\s*)?([0-9]{1,5})(?:\s*[個个])?",t)
            if m:
                v=int(m.group(1))
                if 1 <= v <= 99999:
                    nums.append((v,float(x.get("score",0)),t))
        if not nums:
            return "",0.0,""
        # Prefer highest OCR confidence; for ties preserve first encountered.
        nums.sort(key=lambda z:z[1],reverse=True)
        return nums[0]

    def _clean_nonstage_field(self, text, kind=""):
        """Extract material/special-item name and likely quantity from a non-[N段] field."""
        raw=str(text or "")
        parts=[x.strip() for x in re.split(r"\s*\|\s*",raw) if x.strip()]
        ignore=("交涉力","需要")
        name_parts=[]
        nums=[]
        for x in parts:
            if any(k in x for k in ignore):
                continue
            if re.fullmatch(r"[0-9]{1,5}",x):
                value=int(x)
                if value>0:nums.append(value)
                continue
            # 交換箭頭、加號、錨點等畫面裝飾不是物品名稱；繼續找後面的真正文字。
            if not re.search(r"[\u3400-\u9fffA-Za-z]",x):
                continue
            # Drop tiny OCR UI garbage, but retain useful CJK item names.
            cleaned=re.sub(r"^[0-9?。,.iIW团]+$","",x).strip()
            if cleaned:
                name_parts.append(cleaned)
        name=name_parts[0] if name_parts else ""
        # 一般材料 INPUT 常同時讀到「交換需求量｜物品圖示堆疊數」，例如
        # 羊毛｜200｜1。前者才是交換比例；舊規則取最後一個數字，會固定
        # 把 100/1、200/1 誤判為 1。OUTPUT 保留原本取尾端數量的行為。
        qty=(nums[0] if kind=="input" else nums[-1]) if nums else ""
        return name,qty

    def _classify_special_output(self, text):
        n=_norm(text)
        # Match distinctive cores, not the whole OCR line; UI junk often appears before the item.
        rules=[
            ("烏鴉硬幣", ("乌鸦硬","乌乌硬","乌硬")),
            ("奧基魯阿之花", ("奥基鲁阿之花","奥基鲁阿")),
            ("遺失的貿易品箱子", ("遗失的贸易品箱子","遗失的质易品箱子","易品箱子","品箱子")),
            ("波浪黑石", ("波浪黑石",)),
            ("大洋觀測報告書", ("大洋观测报告书","观测报告书")),
            ("大洋五彩珊瑚飾品", ("大洋五彩珊瑚饰品","五彩珊瑚饰品")),
            ("華麗的岩鹽鑄塊", ("华丽的岩盐铸块","华丽的岩盐块","华丽的岩块")),
            ("純粹的珍珠結晶", ("纯粹的珍珠结晶",)),
            ("華麗的珍珠結晶", ("华丽的珍珠结晶",)),
            ("[大洋]鐵修理工具", ("铁修理工具","修理工具")),
            ("深藍色鑄塊", ("深蓝色铸块","深蓝色块","蓝色铸块","深藍色塊")),
            ("[大洋]酷斯海賊團的日記", ("酷斯海贼團的日記","酷斯海賊團的日記","酷斯海贼团的日记")),
            ("酷斯海賊團的遺物(協商下級)", ("酷斯海贼团的遗物","酷斯海賊團的遺物")),
        ]
        for canonical,keys in rules:
            for k in keys:
                kn=_norm(k)
                if len(kn)>=2 and kn in n:
                    return canonical
        if (("乌" in n or "鸦" in n) and "硬" in n):
            return "烏鴉硬幣"
        # 只讀到泛稱「珍珠結晶」時無法區分純粹/華麗，必須留給身份或圖片證據，不猜。
        if "箱子" in n and ("遗失" in n or "易品" in n):
            return "遺失的貿易品箱子"
        return ""

    def parse_segmented_row(self,row,filename=""):
        f=row.get("fields",{})
        pf=f.get("point",[]); inf=f.get("input",[]); outf=f.get("output",[])
        point=None
        for x in pf:
            r=self._resolve_point_loose(x.get("text",""))
            if r.get("matched"):
                point=r; break

        joined_point=self._text_join(pf)
        # Parse on normalized OCR text so 交換/交换, 次數/次数, punctuation variants
        # all behave the same.  V4.6 also had escaped regex tokens here (\\s/\\d),
        # which made every segmented row fail remaining-times extraction.
        parse_point=_norm(joined_point)
        remaining=None
        m=re.search(r"(?:剩余)?(?:交换|交換)(?:次[数數]+)?[:：]?([0-9]+)次?",parse_point)
        if not m:
            m=re.search(r"次[数數]+[:：]?([0-9]+)次?",parse_point)
        if m:
            try: remaining=int(m.group(1))
            except: pass

        inp=self._resolve_field_item(inf)
        out=self._resolve_field_item(outf)

        # V5.22：若高階OUTPUT的階級標籤被吃掉，利用已知INPUT階級做受限救援。
        if inp and not out and inp.get("stage") in (5,6):
            out=self._resolve_expected_stage_output(outf,int(inp.get("stage"))+1)

        joined_input=self._text_join(inf)
        joined_output=self._text_join(outf)

        # V5.8：交換比例是核心資料。底層數字TOKEN可隱藏，但推導結果必須進結構化。
        iq,iq_conf,iq_token=self._field_quantity(inf,"input")
        input_qtyf=f.get("input_qty",[])
        input_qrec=input_qtyf[0] if input_qtyf else {}
        dedicated_iq_token=str(input_qrec.get("text","")).strip()
        dedicated_iq_conf=float(input_qrec.get("score",0) or 0)
        try:dedicated_iq=int(dedicated_iq_token) if dedicated_iq_token else ""
        except Exception:dedicated_iq=""
        # V5.22：比例數量改走獨立 OUTPUT_QTY 小字專線，不再從OUTPUT品名OCR碰運氣。
        qtyf=f.get("output_qty",[])
        qrec=qtyf[0] if qtyf else {}
        oq_token=str(qrec.get("text","")).strip()
        oq_conf=float(qrec.get("score",0) or 0)
        try: oq=int(oq_token) if oq_token else ""
        except Exception: oq=""

        # V4.8: understand legal non-stage sides instead of flagging all of them as OCR failures.
        input_kind="段位品" if inp else ""
        output_kind="段位品" if out else ""
        input_nonstage_name=""; input_quantity=""
        output_nonstage_name=""; output_quantity=""

        if not inp:
            input_nonstage_name,input_quantity=self._clean_nonstage_field(joined_input,"input")
            if input_nonstage_name:
                input_kind="一般材料"

        if not out:
            output_nonstage_name,output_quantity=self._clean_nonstage_field(joined_output,"output")
            special=self._classify_special_output(joined_output)
            if special:
                output_kind="特殊交換品"
                output_nonstage_name=special
            elif output_nonstage_name:
                # 不用白名單決定「它是不是物品」。只要OUTPUT欄確實有結構化名稱，
                # 就建立待確認身份；白名單僅用來校正已知名稱。
                output_kind="特殊交換品"

        # INPUT欄位中，段位品旁邊的數字是玩家「目前持有量」，不是每次交換需求。
        # 只有一般材料才從欄位取需求量；1～7階品每次固定投入1個。
        if input_kind=="段位品":
            input_quantity=1
        elif isinstance(dedicated_iq,int) and dedicated_iq>0:
            # 獨立小字專線可補救整欄只剩品名的情形，但圖示紋理偶爾會把 10
            # 看成 11。若整欄已有更高信心、且數值不同的獨立數字，以整欄為準。
            broad_trusted=bool(isinstance(iq,int) and iq>0 and iq_conf>=.85)
            if broad_trusted and iq!=dedicated_iq and iq_conf>dedicated_iq_conf:
                input_quantity=iq
            else:
                input_quantity=dedicated_iq;iq=dedicated_iq;iq_conf=dedicated_iq_conf;iq_token=dedicated_iq_token
        elif input_quantity=="" and iq!="":
            input_quantity=iq
        # 一般材料可能是「醋 1000」等四、五位數。重新以最後採用的數量
        # 回找OCR token信心，避免拿交涉力或別的UI數字當比例。
        if input_kind!="段位品" and str(input_quantity).isdigit():
            _wanted=int(input_quantity);_qty_hits=[]
            for _x in inf:
                _digits=re.sub(r"[^0-9]","",str(_x.get("text") or ""))
                if _digits and int(_digits)==_wanted:
                    _qty_hits.append((float(_x.get("score",0) or 0),str(_x.get("text") or "")))
            if _qty_hits:
                iq_conf,iq_token=max(_qty_hits,key=lambda z:z[0]);iq=_wanted
        if output_quantity=="" and oq!="": output_quantity=oq
        # 寬版 OUTPUT_QTY 偶爾沒出結果，但一般 OUTPUT OCR 已讀到獨立數字。
        # 找回同一個數字 token 的實際信心，避免數量已寫入卻因 source 空白又被打回待確認。
        if str(output_quantity).isdigit() and (not oq_token or oq_conf<=0):
            _wanted=int(output_quantity);_qty_hits=[]
            for _x in outf:
                _text=str(_x.get("text") or "").strip()
                _digits=re.sub(r"[^0-9]","",_text)
                if _digits and int(_digits)==_wanted and re.fullmatch(r"[^0-9]*[0-9]+[^0-9]*",_text):
                    _qty_hits.append((float(_x.get("score",0) or 0),_digits))
            if _qty_hits:
                oq_conf,oq_token=max(_qty_hits,key=lambda z:z[0]);oq=_wanted
        # 特殊交換的數量常是 3 位數；寬版 OUTPUT_QTY 若讀到多位數，優先於一般OUTPUT欄位裡可能殘缺的個位數。
        if output_kind=="特殊交換品" and isinstance(oq,int) and oq>=10:
            output_quantity=oq
        # 一般相鄰段位固定規則：3→4 為 1:2；4→5 以上為 1:1。
        # 這些階段本來就標示「預設規則（不需OCR）」，不得讓雜訊污染負重與庫存入帳。
        if inp and out and int(out.get("stage") or 0) in (4,5,6,7):
            output_quantity=2 if (int(inp.get("stage") or 0),int(out.get("stage") or 0))==(3,4) else 1
        # 一般段位交換若畫面沒有獨立讀到數量，不能偷偷猜；留空讓人工複核。
        # V5.10：表格真正需要辨識的比例只有 OUTPUT 2/3階。
        # 前項固定為1；其他階走既有固定規則，不需要截圖OCR。
        ratio_manual_both=bool(not (inp and out))
        ratio_needed=bool((out and out.get("stage") in (2,3)) or ratio_manual_both)
        ratio_input=(input_quantity if ratio_manual_both else 1) if ratio_needed else ""
        # 2/3階比例仍只接受單位數；特殊交換（例如烏鴉硬幣）可保留 3 位數。
        ratio_output=(output_quantity if ratio_needed and str(output_quantity).isdigit()
                      and (ratio_manual_both or 1 <= int(output_quantity) <= 9) else "")
        crow_coin_incomplete=bool(output_nonstage_name=="烏鴉硬幣" and str(ratio_output).isdigit() and int(ratio_output)<10)
        source_reason=str(qrec.get("reason","") or "")
        input_auto=bool(input_kind=="段位品" or (str(ratio_input).isdigit() and int(ratio_input)>0 and iq_conf>=0.80))
        output_auto=bool(str(ratio_output).isdigit() and int(ratio_output)>0 and
                         not crow_coin_incomplete and
                         (oq_conf>=0.92 or re.search(r"[2-9]/\d+版本共識",source_reason)))
        ratio_auto_accepted=bool(ratio_needed and input_auto and output_auto)
        if ratio_manual_both:
            if not str(ratio_input).isdigit():ratio_reason="INPUT數量未讀到"
            elif not input_auto:ratio_reason=f"INPUT數量信心不足（{iq_conf:.3f}）"
            elif not str(ratio_output).isdigit():ratio_reason="OUTPUT數量未讀到"
            elif crow_coin_incomplete:ratio_reason="烏鴉硬幣只讀到個位數，可能被截掉前段"
            elif not output_auto:ratio_reason=f"OUTPUT數量信心或多次辨識共識不足（{oq_conf:.3f}）"
            else:ratio_reason="OCR數字清楚，已自動採用"
        else:ratio_reason=""
        ratio_text=(f"{ratio_input} : {ratio_output}（OCR自動採用）" if ratio_manual_both and ratio_auto_accepted else
                    (f"⚠ {ratio_input or '?'} : {ratio_output or '?'}（{ratio_reason}）" if ratio_manual_both else
                    (f"{ratio_input} : {ratio_output}" if ratio_needed and ratio_input not in ("",None) and ratio_output!="" else
                    ("⚠ 交換比例待確認" if ratio_needed else "預設規則（不需OCR）"))
                   ))

        if input_kind=="一般材料" and out and out.get("stage")==1:
            exchange_type="一般材料→1段"
        elif inp and out:
            exchange_type=f"{inp.get('stage')}段→{out.get('stage')}段"
        elif inp and output_kind=="特殊交換品":
            exchange_type=f"{inp.get('stage')}段→特殊交換"
        else:
            exchange_type="待判定"

        issues=[]
        if not point: issues.append("POINT區未匹配")
        if inp:
            if not inp["matched"]: issues.append("INPUT品項未匹配ITEM_ID")
        elif input_kind!="一般材料":
            issues.append("INPUT區無法判定")
        if out:
            if not out["matched"]: issues.append("OUTPUT品項未匹配ITEM_ID")
        elif output_kind!="特殊交換品":
            issues.append("OUTPUT區無法判定")
        if remaining is None: issues.append("POINT區找不到剩餘交換次數")

        scores=[x.get("score",1) for x in pf+inf+outf]
        conf=round(min(scores or [0]),4)
        point_conf=round(min([x.get("score",1) for x in pf] or [1]),4)
        input_conf=round(min([x.get("score",1) for x in inf] or [1]),4)
        output_conf=round(min([x.get("score",1) for x in outf] or [1]),4)
        if conf<0.55: issues.append("OCR低信心")

        # V5.6：把OCR pipeline已經知道的精準切片路徑一路帶進結構化資料。
        # 不再由UI拿檔名去資料夾猜「哪一張可能是它」。
        imgs=row.get("images") or {}
        evidence={
            "layout":imgs.get("layout",""),
            "row_image":imgs.get("row_image",""),
            "point_image":imgs.get("point_image",""),
            "input_image":imgs.get("input_image",""),
            "input_qty_image":imgs.get("input_qty_image",""),
            "output_image":imgs.get("output_image",""),
            "output_qty_image":imgs.get("output_qty_image",""),
            "fields_contact_image":imgs.get("fields_contact_image","")
        }

        return {
            "source_file":filename,
            "row_index":row.get("index"),
            "point_id":point.get("id","") if point else "",
            "point_name":point.get("record",{}).get("name","") if point else "",
            "exchange_type":exchange_type,
            "input_kind":input_kind,
            "input_stage":inp.get("stage","") if inp else "",
            "input_item_id":inp.get("id","") if inp else "",
            "input_name":inp.get("name","") if inp else input_nonstage_name,
            "input_raw":inp.get("raw","") if inp else input_nonstage_name,
            "input_quantity":input_quantity,
            "exchange_ratio_input":ratio_input,
            "exchange_ratio_output":ratio_output,
            "exchange_ratio":ratio_text,
            "exchange_ratio_needs_ocr":ratio_needed,
            "exchange_ratio_manual_both":ratio_manual_both,
            "exchange_ratio_auto_accepted":ratio_auto_accepted,
            "exchange_ratio_review_reason":ratio_reason,
            "exchange_ratio_source":{
                "input_token":iq_token,
                "input_confidence":round(iq_conf,4),
                "input_reason":input_qrec.get("reason",""),
                "output_token":oq_token,
                "output_confidence":round(oq_conf,4),
                "output_reason":qrec.get("reason","")
            },
            "output_kind":output_kind,
            "output_stage":out.get("stage","") if out else "",
            "output_item_id":out.get("id","") if out else "",
            "output_name":out.get("name","") if out else output_nonstage_name,
            "output_raw":out.get("raw","") if out else output_nonstage_name,
            "output_quantity":output_quantity,
            "remaining_times":remaining if remaining is not None else "",
            "numeric_tokens":[],
            "ocr_min_confidence":conf,
            "ocr_field_confidence":{
                "POINT":point_conf,
                "INPUT":input_conf,
                "OUTPUT":output_conf
            },
            "evidence":evidence,
            "issues":issues,
            "status":"候選可用" if not issues else "待確認",
            "raw_texts":[
                "POINT: "+joined_point,
                "INPUT: "+re.sub(r"\s*\|?\s*交涉力\s*[:：]?\s*(?:需要)?\s*[0-9，,.]+","",self._text_join(inf)).strip(" |"),
                "OUTPUT: "+self._text_join(outf)
            ]
        }

    def parse_ocr_result(self,ocr_result):
        all_cards=[]
        for im in ocr_result.get("images",[]):
            segmented=im.get("segmented_rows") or []
            if segmented:
                for row in segmented:
                    card=self.parse_segmented_row(row,im.get("file",""))
                    # Ignore fully blank/incomplete rows.
                    if card["point_name"] or card["input_raw"] or card["output_raw"]:
                        all_cards.append(card)
                        # V5.31.3.3：監視器直接裝在「最終卡片落地點」。
                        # 不管前面走哪條解析支線，只要UI最後看得到這筆，這裡一定會記。
                        try:
                            import json, os, datetime
                            trace_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"V5.31.3.3_最終身份追蹤.jsonl")
                            trace={
                                "time":datetime.datetime.now().isoformat(timespec="seconds"),
                                "source_file":card.get("source_file"),
                                "row_index":card.get("row_index"),
                                "point":{"id":card.get("point_id"),"name":card.get("point_name")},
                                "input":{"stage":card.get("input_stage"),"id":card.get("input_item_id"),
                                         "name":card.get("input_name"),"raw":card.get("input_raw")},
                                "output":{"stage":card.get("output_stage"),"id":card.get("output_item_id"),
                                          "name":card.get("output_name"),"raw":card.get("output_raw"),
                                          "kind":card.get("output_kind")},
                                "exchange_type":card.get("exchange_type"),
                                "issues":card.get("issues",[]),
                                "status":card.get("status"),
                                "raw_texts":card.get("raw_texts",[])
                            }
                            with open(trace_path,"a",encoding="utf-8") as f:
                                f.write(json.dumps(trace,ensure_ascii=False)+"\\n")
                        except Exception:
                            pass
            else:
                raw=[(x["box"],x["text"],x["score"]) for x in im.get("raw_rows",[])]
                all_cards.extend(self.parse_image(raw,im.get("file","")))
        return all_cards
