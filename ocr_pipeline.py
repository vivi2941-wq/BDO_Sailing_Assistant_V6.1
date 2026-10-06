import os, json, re
from shared_paths import shared_file

class OCRPipeline:
    def __init__(self, master=None):
        self.master=master

    def _engine(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
            return RapidOCR()
        except Exception as e:
            raise RuntimeError(
                "目前啟動助手的 Python 找不到 RapidOCR。\n"
                "這通常不是 OCR 被刪掉，而是啟動器用了錯的 Python 環境。\n\n"
                f"目前 Python：{__import__('sys').executable}\n"
                f"原始錯誤：{e}"
            )

    def validate_images(self,image_paths):
        paths=list(image_paths)
        return {
            "count":len(paths),
            "ready":len(paths)==15,
            "message":"15 / 15，可以開始OCR" if len(paths)==15 else f"{len(paths)} / 15"
        }

    def _resolve_line(self,text):
        hits=[]
        if not self.master: return hits
        norm="".join(str(text).split())
        for item in self.master.items:
            for candidate in (item.get("name",""),item.get("abbr","")):
                c="".join(str(candidate).split())
                if c and len(c)>=2 and c in norm:
                    hits.append({"type":"ITEM","id":item["id"],"name":item["name"],"matched_text":candidate})
                    break
        for point in self.master.points:
            c="".join(str(point.get("name","")).split())
            if c and len(c)>=2 and c in norm:
                hits.append({"type":"POINT","id":point["id"],"name":point["name"],"matched_text":point["name"]})
        for a in self.master.aliases:
            c="".join(str(a.get("alias","")).split())
            if c and len(c)>=2 and c in norm:
                hits.append({"type":a["object_type"],"id":a["id"],"name":a.get("current_name",""),"matched_text":a["alias"],"via":"別名"})
        out=[]; seen=set()
        for h in hits:
            k=(h["type"],h["id"])
            if k not in seen:
                seen.add(k); out.append(h)
        return out

    def _load_user_layout(self,layout,w,h):
        cfg=shared_file("crop_layouts.json")
        try:
            with open(cfg,encoding="utf-8") as f:all_layouts=json.load(f)
            # V5.93.1：舊C其實是「另一台電腦的一般A」，不是第三種排列。
            # 保留向後相容，讓玩家不用重框既有C。
            d=all_layouts.get(layout,{})
            if not d and layout=="CA":d=all_layouts.get("C",{})
            def den(key):
                box=d.get(key)
                return [int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h)] if box else None
            return {
                "list":den("list_box_norm"),"row1":den("row1_box_norm"),"row2":den("row2_box_norm"),
                "point":den("point_box_norm"),"input":den("input_box_norm"),"output":den("output_box_norm"),
                "input_qty":den("input_qty_box_norm"),"output_qty":den("output_qty_box_norm"),
                "sample_filename":d.get("sample_filename","")
            }
        except Exception:return {}

    def _crop_roi(self,path,layout="A"):
        from PIL import Image
        im=Image.open(path).convert("RGB"); w,h=im.size
        ud=self._load_user_layout(layout,w,h)
        list_box=ud.get("list")
        if list_box and ud.get("row1"):
            roi=im.crop(tuple(list_box))
            lx1,ly1,lx2,ly2=list_box
            def rel(box):
                if not box:return None
                return [box[0]-lx1,box[1]-ly1,box[2]-lx1,box[3]-ly1]
            return roi,{
                "layout":layout,"source_width":w,"source_height":h,"user_calibrated":True,
                "list_box":list_box,"row1_box_in_roi":rel(ud.get("row1")),
                "row2_box_in_roi":rel(ud.get("row2")),
                "point_box_in_roi":rel(ud.get("point")),
                "input_box_in_roi":rel(ud.get("input")),
                "output_box_in_roi":rel(ud.get("output")),
                "input_qty_box_in_roi":rel(ud.get("input_qty")),
                "output_qty_box_in_roi":rel(ud.get("output_qty"))
            }

        # 尚未校正時保留V4.3 fallback。
        base_layout="B" if layout in ("B","CB") else "A"
        if base_layout=="A":
            panel_left=int(w*0.064); panel_top=int(h*0.130); panel_right=int(w*0.747); panel_bottom=int(h*0.875)
            panel=im.crop((panel_left,panel_top,panel_right,panel_bottom)); pw,ph=panel.size
            list_left=int(pw*0.285); list_top=int(ph*0.300); list_right=int(pw*0.985); list_bottom=int(ph*0.985)
        else:
            panel_left=int(w*0.060); panel_top=int(h*0.120); panel_right=int(w*0.755); panel_bottom=int(h*0.900)
            panel=im.crop((panel_left,panel_top,panel_right,panel_bottom)); pw,ph=panel.size
            list_left=int(pw*0.235); list_top=int(ph*0.285); list_right=int(pw*0.985); list_bottom=int(ph*0.985)
        roi=panel.crop((list_left,list_top,list_right,list_bottom))
        return roi,{"layout":layout,"source_width":w,"source_height":h,"user_calibrated":False}

    def _segment_rows(self, roi, debug_dir, base_name, layout="A", roi_meta=None):
        """
        V4.3：A/B 不再把ROI平均6等分。

        A版實機debug顯示：
        - ROW1~3 尚可
        - ROW4~6開始上下吃字
        原因是實際列高約只有舊版的 75~80%，誤差逐列累積。

        B版實機debug顯示：
        - ROI頂端先有一個殘列
        - 真正完整列從下一列開始
        因此B有自己的起始Y與列高。
        """
        from PIL import Image, ImageDraw
        w,h=roi.size

        meta=roi_meta or {}
        user_row=meta.get("row1_box_in_roi") if meta.get("user_calibrated") else None
        user_row2=meta.get("row2_box_in_roi") if meta.get("user_calibrated") else None

        if user_row and user_row2:
            ux1,uy1,ux2,uy2=user_row
            pitch=user_row2[1]-uy1
            start_y=uy1; row_h=max(1,uy2-uy1); visible_rows=6

            # V5.64.2｜B版防回歸；V5.64.5 修正誤判：
            # ROW6 底部只超出少量像素時，後面的裁邊邏輯本來就能安全保留該列，
            # 不能因此把玩家整套 ROW/POINT/INPUT/OUTPUT 校正改成 fallback。
            # 只有 ROW6 起點已在 ROI 外，或 ROI 內剩餘高度不足一列的 55%，才 fallback。
            if layout in ("B","CB"):
                test_y1=start_y+5*pitch
                remaining=h-test_y1
                if pitch<=0 or test_y1>=h or remaining<max(20,int(row_h*0.55)):
                    scale=h/937.0
                    start_y=int(105*scale)
                    row_h=max(1,int(100*scale))
                    pitch=max(1,int(105*scale))
        elif layout in ("A","CA","C"):
            scale=h/1102.0; start_y=int(35*scale); row_h=int(130*scale); pitch=int(140*scale); visible_rows=6
        else:
            scale=h/937.0; start_y=int(105*scale); row_h=int(100*scale); pitch=int(105*scale); visible_rows=6

        rows=[]
        for i in range(visible_rows):
            y1=start_y+i*pitch; y2=y1+row_h
            if y1>=h:
                break
            # 最後一列只要起點仍在 ROI 內，就裁到邊界，不因幾個像素超界直接整列消失。
            y2=min(h,y2)
            if y2-y1 < max(20,int(row_h*0.55)):
                break

            # User ROW1 also teaches horizontal row extent.
            if user_row:
                row_x1=max(0,user_row[0]); row_x2=min(w,user_row[2])
            else:
                row_x1=0; row_x2=w
            row=roi.crop((row_x1,y1,row_x2,y2))

            # User-calibrated field rectangles are copied by the true ROW pitch.
            # Convert each field from ROI coords to current ROW-local coords.
            def field_crop(key, fallback):
                fb=meta.get(key+"_box_in_roi") if meta.get("user_calibrated") else None
                if fb:
                    dy=i*pitch
                    x1=max(row_x1,fb[0]); x2=min(row_x2,fb[2])
                    yy1=max(y1,fb[1]+dy); yy2=min(y2,fb[3]+dy)
                    return roi.crop((x1,yy1,x2,yy2))
                f1,f2=fallback
                return row.crop((int(row.width*f1),0,int(row.width*f2),row.height))

            point=field_crop("point",(0.0,0.285))
            inp=field_crop("input",(0.260,0.735))
            out=field_crop("output",(0.710,1.0))

            # 一般材料 INPUT 的交換需求量疊在物品圖示右下角；整欄中文 OCR
            # 常只回傳品名。獨立保留這塊小數字，交給數字專線多門檻投票。
            def exact_field_crop(key):
                fb=meta.get(key+"_box_in_roi") if meta.get("user_calibrated") else None
                if not fb:return None
                dy=i*pitch
                x1=max(row_x1,fb[0]);x2=min(row_x2,fb[2])
                yy1=max(y1,fb[1]+dy);yy2=min(y2,fb[3]+dy)
                if x2<=x1 or yy2<=yy1:return None
                return roi.crop((x1,yy1,x2,yy2))

            input_qty=exact_field_crop("input_qty")
            if input_qty is None:
                iw,ih=inp.size
                # V5.93.7：一般材料可能是三位數（如 300），擴寬裁切區域以完整讀取
                input_qty=inp.crop((int(iw*.055),int(ih*.42),int(iw*.225),int(ih*.99)))

            # 段位品 INPUT 圖示數字是玩家持有量，解析器會忽略；一般材料 INPUT
            # 則是每次需求量，V5.92.2 恢復獨立小字專線，避免整欄只讀到品名。
            # OUTPUT_QTY 位於 OUTPUT 物品圖示右下的小數字。
            rw,rh=row.size
            # V5.22：右邊界大幅收窄，避免OUTPUT名稱前的 [2階段]/[3階段] 數字混入。
            # 依V5.10.2實機證物：真正QTY位於圖示右下，階段文字在更右側。
            # V5.46.2：這區不只會出現 2/3階比例的單位數，也會出現烏鴉硬幣等 3 位數。
            # 舊裁切太窄，127 可能只剩最後的 7。只向左加寬，右界仍避開階段文字。
            # V5.85.5 實機回歸：V5.85.3誤將框移到0.735之後，整批切到圖示右邊，
            # 連原本可讀的2/3階數量也變問號。恢復圖示右下的實機定位。
            output_qty=exact_field_crop("output_qty")
            if output_qty is None:
                output_qty=row.crop((int(rw*0.625),int(rh*0.34),int(rw*0.712),int(rh*0.99)))

            prefix=os.path.join(debug_dir,f"{base_name}.ROW{i+1}")
            row_path=prefix+".png"
            point_path=prefix+".POINT.png"
            input_path=prefix+".INPUT.png"
            input_qty_path=prefix+".INPUT_QTY.png"
            output_path=prefix+".OUTPUT.png"
            output_qty_path=prefix+".OUTPUT_QTY.png"

            row.save(row_path); point.save(point_path); inp.save(input_path); out.save(output_path)
            iq_big=input_qty.resize((max(1,input_qty.width*5),max(1,input_qty.height*5)))
            iq_big.save(input_qty_path)
            oq_big=output_qty.resize((max(1,output_qty.width*5),max(1,output_qty.height*5)))
            oq_big.save(output_qty_path)

            # OUTPUT_QTY只進證物圖，本版不增加EasyOCR次數。
            parts=[("POINT",point),("INPUT",inp),("INPUT_QTY",iq_big),("OUTPUT",out),("OUTPUT_QTY",oq_big)]
            label_h=28
            widths=[im.width for _,im in parts]
            max_h=max(im.height for _,im in parts)
            contact=Image.new("RGB",(sum(widths),max_h+label_h),"white")
            draw=ImageDraw.Draw(contact)
            xx=0
            for label,im_part in parts:
                contact.paste(im_part,(xx,label_h))
                draw.text((xx+8,6),label,fill="black")
                xx+=im_part.width
            contact_path=prefix+".FIELDS.png"
            contact.save(contact_path)

            rows.append({
                "index":i+1,
                "row_image":row_path,
                "point_image":point_path,
                "input_image":input_path,
                "input_qty_image":input_qty_path,
                "output_image":output_path,
                "output_qty_image":output_qty_path,
                "fields_contact_image":contact_path,
                "box":[0,y1,w,y2],
                "layout":layout
            })

        # V5.64.2：每張圖留下切列摘要。B版不是6列時直接寫醒目的警告檔，
        # 以後不必等某個點位消失才知道 segmentation 回歸。
        try:
            manifest=os.path.join(debug_dir,f"{base_name}.SEGMENT.txt")
            with open(manifest,"w",encoding="utf-8") as f:
                f.write(f"layout={layout}\nroi={w}x{h}\nstart_y={start_y}\nrow_h={row_h}\npitch={pitch}\nrows={len(rows)}\n")
                for rr in rows:
                    f.write(f"ROW{rr['index']} box={rr['box']}\n")
                if layout in ("B","CB") and len(rows)!=6:
                    f.write(f"WARNING=B版切列異常：預期6列，實際{len(rows)}列\n")
        except Exception:
            pass
        return rows

    def _ocr_output_qty(self, engine, image_path, debug_prefix, digit_only_rescue=False):
        """
        V5.22 地下室數字專線：
        OUTPUT_QTY 已定位完成後，不拿模糊小字去跑一般中文流程。
        產生多種高對比版本，RapidOCR各跑一次，接受 1~3 位數（比例與特殊貨幣數量），
        以「辨識信心 + 多版本共識」選結果。失敗就留給地面人工大按鈕。
        """
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter
        rgb=Image.open(image_path).convert("RGB")
        im=rgb.convert("L")

        # 放大採 LANCZOS；先做 autocontrast，再建立不同強度版本。
        scale=8
        big=im.resize((max(1,im.width*scale),max(1,im.height*scale)), Image.Resampling.LANCZOS)
        base=ImageOps.autocontrast(big)

        variants=[]
        variants.append(("gray", ImageEnhance.Contrast(base).enhance(2.2).filter(ImageFilter.SHARPEN)))
        variants.append(("sharp", ImageEnhance.Contrast(base).enhance(3.0).filter(ImageFilter.UnsharpMask(radius=2,percent=220,threshold=2))))
        # 白字黑底環境：閾值版保留亮字，壓掉背景。
        for th in (150,175,200):
            bw=base.point(lambda x,t=th: 255 if x>=t else 0)
            variants.append((f"bw{th}",bw))

        votes=[]
        debug_paths=[]
        for name,v in variants:
            vp=f"{debug_prefix}.QTY_{name}.png"
            v.save(vp); debug_paths.append(vp)
            try:
                rr,_=engine(vp)
            except Exception:
                rr=[]
            for r in rr or []:
                txt=str(r[1]).strip()
                # V5.46.2：允許完整 1~3 位數。舊版只承認單一 1~9，
                # 因此像 127 會被裁成/讀成 7；592 則有時剛好被一般OUTPUT OCR救回，造成同類資料一半對一半錯。
                cleaned=re.sub(r"[^0-9]","",txt)
                if re.fullmatch(r"[0-9]{1,3}",cleaned):
                    value=int(cleaned)
                    if 1 <= value <= 999:
                        try: score=float(r[2])
                        except Exception: score=0.0
                        votes.append({"value":value,"score":score,"variant":name,"raw":txt})

        # 多版本共識加分；同值出現越多次越可靠。
        counts={}; best_score={}
        for v in votes:
            n=v["value"]
            counts[n]=counts.get(n,0)+1
            best_score[n]=max(best_score.get(n,0.0),v["score"])
        ranked=sorted(counts,key=lambda n:(counts[n],best_score[n]),reverse=True)
        if ranked:
            winner=ranked[0]
            # 至少兩個預處理版本同意，或單版本信心非常高，才自動端菜。
            if counts[winner]>=2 or best_score[winner]>=0.92:
                return winner,round(best_score[winner],4),f"{counts[winner]}/{len(variants)}版本共識",debug_paths

        # V5.91.5：奧花／波浪黑石的數量是圖示右下角單一小字。
        # 整張圖示的高亮紋理會蓋過數字；只在已知單位數動態特殊品啟用窄框救援，
        # 避免把烏鴉硬幣 127、592 等三位數裁成最後一碼。
        if digit_only_rescue:
            w,h=im.size
            # 只留圖示右下疊字；右界停在外框前，避免金色直線被誤認成 1。
            digit=im.crop((int(w*0.82),int(h*0.25),int(w*0.915),int(h*0.88)))
            digit_big=digit.resize((max(1,digit.width*8),max(1,digit.height*8)),Image.Resampling.LANCZOS)
            digit_base=ImageOps.autocontrast(digit_big)
            digit_variants=[
                ("DIGIT_gray",ImageEnhance.Contrast(digit_base).enhance(2.2).filter(ImageFilter.SHARPEN)),
                ("DIGIT_sharp",ImageEnhance.Contrast(digit_base).enhance(3.0).filter(ImageFilter.UnsharpMask(radius=2,percent=220,threshold=2))),
            ]
            for th in (150,175,200):
                digit_variants.append((f"DIGIT_bw{th}",digit_base.point(lambda x,t=th:255 if x>=t else 0)))
            digit_votes=[]
            for name,v in digit_variants:
                vp=f"{debug_prefix}.QTY_{name}.png"
                v.save(vp); debug_paths.append(vp)
                try: rr,_=engine(vp)
                except Exception: rr=[]
                for r in rr or []:
                    cleaned=re.sub(r"[^0-9]","",str(r[1]).strip())
                    if re.fullmatch(r"[1-9]",cleaned):
                        try:score=float(r[2])
                        except Exception:score=0.0
                        digit_votes.append((int(cleaned),score))
            dcounts={}; dbest={}
            for value,score in digit_votes:
                dcounts[value]=dcounts.get(value,0)+1
                dbest[value]=max(dbest.get(value,0.0),score)
            if dcounts:
                dwinner=max(dcounts,key=lambda n:(dcounts[n],dbest[n]))
                if dcounts[dwinner]>=2 or dbest[dwinner]>=0.92:
                    return dwinner,round(dbest[dwinner],4),f"右下角{dcounts[dwinner]}/{len(digit_variants)}版本共識",debug_paths

            # RapidOCR 對奧花的高亮圖示仍可能完全不出字。數字 1 在實機圖上是
            # 與圖示分離的低彩度白色直條；確認輪廓後直接回傳字形結果。
            if self._is_right_bottom_one_glyph(rgb):
                return 1,0.97,"右下角白色數字1輪廓",debug_paths

        # V5.93.3：數字1輪廓不是奧花／波浪黑石專屬。
        # 一般材料→1階品（例如絲綢→肥沃的土）的OUTPUT圖示同樣會顯示細長白色1；
        # RapidOCR可能完全不回傳文字，但輪廓證據仍然清楚。輪廓判定本身已嚴格限制
        # 在圖示右下數量區、長寬比與低彩度白色連通元件，因此可安全作全品項最後救援。
        if self._is_right_bottom_one_glyph(rgb):
            return 1,0.97,"右下角白色數字1輪廓",debug_paths

        if ranked:
            winner=ranked[0]
            return None,round(best_score[winner],4),f"候選{winner}但共識不足",debug_paths
        return None,0.0,"",debug_paths

    @staticmethod
    def _is_right_bottom_one_glyph(image):
        """Recognize the isolated white '1' overlay, without assuming a ratio."""
        try:
            import numpy as np

            a=np.asarray(image.convert("RGB"),dtype=np.int16)
            h,w=a.shape[:2]
            if w<80 or h<50:return False
            mx=a.max(axis=2);mn=a.min(axis=2)
            saturation=(mx-mn)*255/np.maximum(mx,1)
            mask=(mx>=150)&(saturation<=90)
            # Only the quantity overlay zone; excludes most icon highlights and its border.
            x0=int(w*.84);x1=int(w*.915);y0=int(h*.28);y1=int(h*.82)
            sub=mask[y0:y1,x0:x1]
            seen=np.zeros_like(sub,dtype=bool)
            sh,sw=sub.shape
            for sy,sx in zip(*np.where(sub)):
                if seen[sy,sx]:continue
                stack=[(int(sy),int(sx))];seen[sy,sx]=True;pts=[]
                while stack:
                    yy,xx=stack.pop();pts.append((yy,xx))
                    for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
                        ny,nx=yy+dy,xx+dx
                        if 0<=ny<sh and 0<=nx<sw and sub[ny,nx] and not seen[ny,nx]:
                            seen[ny,nx]=True;stack.append((ny,nx))
                if len(pts)<20:continue
                xs=[p[1] for p in pts];ys=[p[0] for p in pts]
                cw=max(xs)-min(xs)+1;ch=max(ys)-min(ys)+1
                fill=len(pts)/(cw*ch)
                global_left=x0+min(xs);global_right=x0+max(xs)+1
                # Actual 2048/3840 evidence scales to about 12x49 / 22x91.
                if (w*.86<=global_left and global_right<=w*.91 and
                    h*.14<=ch<=h*.32 and w*.012<=cw<=w*.045 and
                    ch/max(cw,1)>=3.0 and .20<=fill<=1.0):
                    return True
            return False
        except Exception:
            return False

    def _ocr_output_rescue(self, engine, image_path, debug_prefix):
        """
        V5.22 高階OUTPUT救援：
        只在INPUT是5/6階、一般OUTPUT第一次讀成空白/極短/沒有階級標記時啟動。
        不增加所有89筆的OCR負擔，只救可疑列。
        """
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter
        im=Image.open(image_path).convert("L")

        # V5.22：真正病灶不是「字不夠銳」，而是OUTPUT切片同時塞了
        # 左邊大圖示、右邊船錨/數量，OCR很容易把注意力丟去那些高對比物件。
        # 高階救援先只留下中間文字帶，再放大辨識。
        w,h=im.size
        text_only=im.crop((int(w*0.16), int(h*0.03), int(w*0.84), int(h*0.97)))
        big=text_only.resize((max(1,text_only.width*4),max(1,text_only.height*4)),Image.Resampling.LANCZOS)
        base=ImageOps.autocontrast(big)
        variants=[
            # V5.22：證物顯示硬二值化會把灰白中文直接滅口，因此姓名救援不再用bw。
            ("soft",ImageEnhance.Contrast(base).enhance(1.35)),
            ("contrast",ImageEnhance.Contrast(base).enhance(1.8)),
            ("sharp",ImageEnhance.Contrast(base).enhance(2.05).filter(
                ImageFilter.UnsharpMask(radius=1.4,percent=180,threshold=2))),
            ("bright",ImageEnhance.Brightness(ImageEnhance.Contrast(base).enhance(1.55)).enhance(1.18)),
        ]

        vals=[]
        seen=set()
        paths=[]
        text_path=f"{debug_prefix}.RESCUE_textonly.png"
        text_only.save(text_path)
        paths.append(text_path)
        for name,v in variants:
            vp=f"{debug_prefix}.RESCUE_{name}.png"
            v.save(vp); paths.append(vp)
            try: rr,_=engine(vp)
            except Exception: rr=[]
            for r in rr or []:
                txt=str(r[1]).strip()
                if not txt: continue
                try: score=round(float(r[2]),4)
                except Exception: score=0.0
                key=txt
                if key in seen: continue
                seen.add(key)
                vals.append({"text":txt,"score":score,"box":r[0],
                             "hits":self._resolve_line(txt),
                             "rescue_variant":name})
        return vals,paths

    def _assignment_path(self):
        # V5.64.4：匯入流程與版型指派器都把玩家的 A/B 指派存到共用資料。
        # OCR 必須讀同一份檔案；舊寫法讀版本內 data，會看不到玩家剛指定的 B 版。
        return shared_file("layout_assignments.json", fallback_local=False)

    def _load_assignments(self):
        try:
            with open(self._assignment_path(),encoding="utf-8") as f:
                d=json.load(f)
            return d if isinstance(d,dict) else {}
        except Exception:
            return {}

    def _layout_for_path(self,path,total,index):
        """
        V4.6優先順序：
        1. 玩家明確指定的檔名 -> A/B
        2. 若未指定且正好15張，暫時保留舊fallback：前14=A、最後=B
        3. 其他情況=A
        """
        assignments=self._load_assignments()
        name=os.path.basename(path)
        # exact filename first, then normalized lowercase
        if name in assignments:
            v=str(assignments[name]).upper()
            if v=="C":v="CA"
            if v in ("A","B","CA","CB"): return v,"玩家指定"
        low=name.lower()
        for k,v in assignments.items():
            vv=str(v).upper()
            if vv=="C":vv="CA"
            if str(k).lower()==low and vv in ("A","B","CA","CB"):
                return vv,"玩家指定"
        if total==15:
            return ("B" if index==15 else "A"),"15張fallback"
        return "A","預設A"

    def run(self,image_paths,save_path=None,progress_cb=None):
        engine=self._engine()
        result={"images":[],"status":"OCR完成","mode":"EXPLICIT_LAYOUT_ASSIGNMENTS"}
        debug_dir=os.path.join(os.path.dirname(save_path or "."),"ocr_roi_debug")
        os.makedirs(debug_dir,exist_ok=True)

        image_paths=list(image_paths)
        total=len(image_paths)
        for image_index,path in enumerate(image_paths, start=1):
            if progress_cb:
                try: progress_cb("start_image",image_index,total,os.path.basename(path))
                except Exception: pass
            layout,layout_reason=self._layout_for_path(path,total,image_index)
            roi,roi_box=self._crop_roi(path,layout=layout)
            debug_img=os.path.join(debug_dir,os.path.basename(path)+".ROI.png")
            roi.save(debug_img)

            # Full ROI OCR remains as fallback/debug.
            raw,_=engine(debug_img)
            lines=[]; raw_rows=[]
            for row in raw or []:
                box=row[0]
                text=str(row[1]).strip()
                score=float(row[2])
                lines.append({"text":text,"score":round(score,4),"hits":self._resolve_line(text)})
                raw_rows.append({"box":box,"text":text,"score":round(score,4)})

            segmented=self._segment_rows(roi,debug_dir,os.path.basename(path),layout=layout,roi_meta=roi_box)
            field_rows=[]
            for seg in segmented:
                fields={}
                for key,img_path in (
                    ("point",seg["point_image"]),
                    ("input",seg["input_image"]),
                    ("output",seg["output_image"])
                ):
                    rr,_=engine(img_path)
                    vals=[]
                    for r in rr or []:
                        vals.append({
                            "text":str(r[1]).strip(),
                            "score":round(float(r[2]),4),
                            "box":r[0],
                            "hits":self._resolve_line(str(r[1]).strip())
                        })
                    fields[key]=vals

                # V5.22：三隻鬼集中在高階OUTPUT。先看INPUT是否為5/6階；
                # 若OUTPUT第一次OCR空白、只剩1~2個字，或完全沒讀到[6/7段]，才開地下室救援。
                input_text=" ".join(str(x.get("text","")) for x in fields.get("input",[]))
                output_text=" ".join(str(x.get("text","")) for x in fields.get("output",[])).strip()
                high_in=bool(re.search(r"[\[【]\s*[56]\s*[^\]】]{0,2}?段",input_text))
                has_high_out=bool(re.search(r"[\[【]\s*[67]\s*[^\]】]{0,2}?段",output_text))
                meaningful=re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]","",output_text)
                if high_in and (not has_high_out or len(meaningful)<=2):
                    rescued,rescue_paths=self._ocr_output_rescue(
                        engine,seg["output_image"],os.path.splitext(seg["output_image"])[0])
                    if rescued:
                        # 原始結果也保留；解析器可從所有候選挑最可靠匹配。
                        fields["output"].extend(rescued)
                    fields["output_rescue_debug"]=[{"text":x,"score":1.0} for x in rescue_paths]

                output_text=" | ".join(str(v.get("text","")) for v in fields.get("output",[]))
                digit_only_rescue=bool(re.search(r"波浪黑石|[奧奥]基[魯鲁]阿之花",output_text))
                input_qty_value,input_qty_conf,input_qty_reason,input_qty_debug=self._ocr_output_qty(
                    engine,seg["input_qty_image"],os.path.splitext(seg["input_qty_image"])[0])
                fields["input_qty"]=[{
                    "text":str(input_qty_value) if input_qty_value is not None else "",
                    "score":input_qty_conf,
                    "reason":input_qty_reason,
                    "debug_images":input_qty_debug
                }]
                qty_value,qty_conf,qty_reason,qty_debug=self._ocr_output_qty(
                    engine,seg["output_qty_image"],
                    os.path.splitext(seg["output_qty_image"])[0],
                    digit_only_rescue=digit_only_rescue
                )
                fields["output_qty"]=[{
                    "text":str(qty_value) if qty_value is not None else "",
                    "score":qty_conf,
                    "reason":qty_reason,
                    "debug_images":qty_debug
                }]
                field_rows.append({
                    "index":seg["index"],
                    "images":seg,
                    "fields":fields
                })

            if progress_cb:
                try: progress_cb("finish_image",image_index,total,os.path.basename(path))
                except Exception: pass

            result["images"].append({
                "path":path,
                "file":os.path.basename(path),
                "layout":layout,
                "layout_reason":layout_reason,
                "roi_box":roi_box,
                "roi_debug_image":debug_img,
                "line_count":len(lines),
                "lines":lines,
                "raw_rows":raw_rows,
                "segmented_rows":field_rows
            })

        if save_path:
            os.makedirs(os.path.dirname(save_path),exist_ok=True)
            with open(save_path,"w",encoding="utf-8") as f:
                json.dump(result,f,ensure_ascii=False,indent=2)
        return result

    @staticmethod
    def layout_health(result,structured_rows=None):
        """用真正OCR結果健檢版面；切片看起來清楚但文字大量失敗也會被抓出來。"""
        structured_rows=structured_rows or []
        by_file={}
        for row in structured_rows:
            if not isinstance(row,dict):continue
            by_file.setdefault(os.path.basename(str(row.get("source_file") or "")),[]).append(row)
        images=[];suspect_files=[]
        for im in (result or {}).get("images",[]) or []:
            name=str(im.get("file") or os.path.basename(str(im.get("path") or "")))
            seg=im.get("segmented_rows") or [];row_count=len(seg)
            present={"point":0,"input":0,"output":0};scores=[]
            for row in seg:
                fields=row.get("fields") or {}
                for key in present:
                    vals=fields.get(key) or []
                    meaningful=[x for x in vals if re.search(r"[0-9A-Za-z\u3400-\u9fff]",str(x.get("text") or ""))]
                    if meaningful:present[key]+=1
                    scores.extend(float(x.get("score",0) or 0) for x in meaningful)
            parsed=by_file.get(name,[])
            pending=sum(1 for x in parsed if str(x.get("status") or "")!="候選可用")
            core_missing=sum(1 for x in parsed if not x.get("point_id") or not x.get("input_item_id") or not x.get("output_item_id"))
            point_missing=sum(1 for x in parsed if not x.get("point_id"))
            denom=max(1,row_count)
            field_rate=min(present.values())/denom if present else 0.0
            avg_conf=sum(scores)/len(scores) if scores else 0.0
            pending_rate=pending/max(1,len(parsed)) if parsed else 1.0
            # 新品項本來就可能待確認；只有待確認同時伴隨偏低信心，才判作版面異常。
            suspect=bool(row_count<4 or field_rate<.67 or avg_conf<.67 or point_missing>=3 or
                         (len(parsed)>=4 and pending>=3 and avg_conf<.78))
            reasons=[]
            if row_count<4:reasons.append(f"只切出{row_count}列")
            if field_rate<.67:reasons.append("POINT/INPUT/OUTPUT有大量空白")
            if avg_conf<.67:reasons.append(f"平均信心偏低 {avg_conf:.2f}")
            if len(parsed)>=4 and pending>=3 and avg_conf<.78:reasons.append(f"{pending}列未通過且信心偏低")
            if point_missing>=3:reasons.append(f"{point_missing}列點位無法辨識")
            rec={"file":name,"layout":im.get("layout"),"rows":row_count,"field_rate":round(field_rate,3),
                 "average_confidence":round(avg_conf,3),"pending":pending,"core_missing":core_missing,"point_missing":point_missing,
                 "suspect":suspect,"reasons":reasons}
            images.append(rec)
            if suspect:suspect_files.append(name)
        total_parsed=sum(len(v) for v in by_file.values())
        total_pending=sum(1 for rows in by_file.values() for x in rows if str(x.get("status") or "")!="候選可用")
        new_identity=sum(1 for rows in by_file.values() for x in rows
                         if any("新身份待確認" in str(issue) for issue in (x.get("issues") or [])))
        batch_reasons=[]
        if total_parsed>=20 and total_pending>=8 and total_pending/max(1,total_parsed)>=.12:
            batch_reasons.append(f"整批{total_pending}/{total_parsed}筆待確認，疑似UI/OCR規格不合")
        if new_identity>=5:
            batch_reasons.append(f"整批出現{new_identity}筆新身份；成熟資料庫不應突然大量失憶")
        return {"ok":not suspect_files and not batch_reasons,"suspect_count":len(suspect_files),
                "image_count":len(images),"suspect_files":suspect_files,"images":images,
                "batch_reasons":batch_reasons,"total_parsed":total_parsed,
                "total_pending":total_pending,"new_identity_pending":new_identity}
