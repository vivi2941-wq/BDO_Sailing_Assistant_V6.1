import os, json, tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk

FONT='Microsoft JhengHei UI'

def snap_box_to_edges(box,candidate_boxes,tolerance,image_size=None):
    """將新框四邊吸附到附近既有邊線；回傳吸附後座標與吸附邊數。"""
    xs=[];ys=[]
    if image_size:
        xs.extend((0,int(image_size[0])));ys.extend((0,int(image_size[1])))
    for other in candidate_boxes or []:
        if not other or len(other)!=4:continue
        xs.extend((int(other[0]),int(other[2])));ys.extend((int(other[1]),int(other[3])))
    out=list(map(int,box));count=0
    for idx,candidates in ((0,xs),(2,xs),(1,ys),(3,ys)):
        if not candidates:continue
        nearest=min(candidates,key=lambda value:abs(value-out[idx]))
        if abs(nearest-out[idx])<=tolerance:
            if nearest!=out[idx]:count+=1
            out[idx]=nearest
    return out,count

class CropCalibrator(tk.Toplevel):
    def __init__(self,parent,config_path,initial_layout='A'):
        super().__init__(parent)
        self.title('✂ 完整可視化版型校正器｜V4.6')
        self.geometry('1420x900'); self.minsize(1100,740)
        self.config_path=config_path
        self.parent=parent
        initial_layout='CA' if initial_layout=='C' else initial_layout
        self.layout=tk.StringVar(value=initial_layout if initial_layout in ('A','B','CA','CB') else 'A')
        self.mode=tk.StringVar(value='LIST')
        self.snap_enabled=tk.BooleanVar(value=True)
        self.img=None; self.tkimg=None; self.path=None; self.scale=1.0; self.zoom=1.0
        self.drag_start=None; self.temp=None
        self.profile_names={'CA':'自訂版面 1','CB':'自訂版面 2'}
        self.boxes={'A':{},'B':{},'CA':{},'CB':{}}
        self.load_config(); self.build()
        if self.layout.get() == 'B':
            self.status.config(text='Layout B：請按「① 載入原始截圖」，選擇本輪 B 版截圖後重新手動框選。')

    def load_config(self):
        try:
            with open(self.config_path,encoding='utf-8') as f: d=json.load(f)
            names=d.get('_profile_names',{}) if isinstance(d,dict) else {}
            for k in ('CA','CB'):
                if str(names.get(k,'')).strip():self.profile_names[k]=str(names[k]).strip()
            for k in ('A','B','CA','CB'):
                old=d.get(k,{}) or (d.get('C',{}) if k=='CA' else {})
                # Existing V4.4 normalized settings are loaded later when an image is opened.
                self.boxes[k]={'_saved':old}
            # 成熟 A/B 過去由 OCR 底層暗中推算數字框，舊設定檔沒有存欄位。
            # 在校正器補成可見模板，但不改變既有 OCR 實際裁切公式。
            for k in ('A','B'):
                self.boxes[k]['_saved']=self._saved_with_qty_fallback(k)
        except Exception: pass

    def build(self):
        top=tk.Frame(self); top.pack(fill='x',padx=10,pady=6)
        tk.Button(top,text='① 載入原始截圖',font=(FONT,13,'bold'),command=self.open_image).pack(side='left',padx=3)
        tk.Button(top,text='－ 縮小',font=(FONT,11,'bold'),command=lambda:self.zoom_by(1/1.25)).pack(side='left',padx=(12,2))
        tk.Button(top,text='＋ 放大',font=(FONT,11,'bold'),command=lambda:self.zoom_by(1.25)).pack(side='left',padx=2)
        tk.Button(top,text='適合視窗',font=(FONT,11),command=self.fit_view).pack(side='left',padx=2)
        tk.Checkbutton(top,text='🧲 邊線吸附',variable=self.snap_enabled,font=(FONT,11,'bold')).pack(side='left',padx=8)
        tk.Label(top,text='版型',font=(FONT,12,'bold')).pack(side='left',padx=(12,2))
        for v,t in [('A','A 一般'),('B','B 特殊')]:
            tk.Radiobutton(top,text=t,variable=self.layout,value=v,font=(FONT,12,'bold'),command=self.layout_changed).pack(side='left')
        self.custom_button=tk.Button(top,text='▾ 選擇自訂版面',font=(FONT,11,'bold'),command=self.choose_custom_layout)
        self.custom_button.pack(side='left',padx=(8,2))
        self.custom_active=tk.Label(top,text='',font=(FONT,10,'bold'),fg='#6b4b9a')
        self.custom_active.pack(side='left',padx=3)
        tk.Button(top,text='儲存版型',font=(FONT,13,'bold'),command=self.save_layout).pack(side='right',padx=3)

        tools=tk.Frame(self); tools.pack(fill='x',padx=10,pady=4)
        defs=[('LIST','② 交換列表'),('ROW1','③ ROW1'),('ROW2','④ ROW2'),
              ('POINT','⑤ POINT欄'),('INPUT','⑥ INPUT欄'),('OUTPUT','⑦ OUTPUT欄'),
              ('INPUT_QTY','⑧ INPUT數字'),('OUTPUT_QTY','⑨ OUTPUT數字')]
        for key,label in defs:
            tk.Radiobutton(tools,text=label,variable=self.mode,value=key,indicatoron=False,
                           font=(FONT,11,'bold'),padx=8,pady=5,
                           command=self._update_box_info).pack(side='left',padx=2)
        tk.Button(tools,text='⑩ 預覽6列＋欄位',font=(FONT,11,'bold'),command=self.preview).pack(side='left',padx=8)
        tk.Button(tools,text='清除此版型重畫',font=(FONT,11),command=self.clear_layout).pack(side='right',padx=3)

        template=tk.Frame(self);template.pack(fill='x',padx=10,pady=(0,3))
        tk.Label(template,text='自訂版快速起點：',font=(FONT,11,'bold')).pack(side='left')
        tk.Button(template,text='複製 A 一般模板',font=(FONT,10,'bold'),command=lambda:self.copy_standard_layout('A')).pack(side='left',padx=3)
        tk.Button(template,text='複製 B 特殊模板',font=(FONT,10,'bold'),command=lambda:self.copy_standard_layout('B')).pack(side='left',padx=3)
        tk.Label(template,text='（先選自訂版，再複製最接近的標準版，只微調有偏移的框）',font=(FONT,10),fg='#555').pack(side='left',padx=8)

        self.help=tk.Label(self,text='常用A／B直接放在頁面；其他解析度或UI可在「選擇自訂版面」中命名、切換。舊C會自動沿用到自訂版面1。',
                           font=(FONT,12),anchor='w')
        self.help.pack(fill='x',padx=14,pady=(2,5))
        frame=tk.Frame(self,bg='#222'); frame.pack(fill='both',expand=True,padx=10,pady=5)
        self.canvas=tk.Canvas(frame,bg='#111',highlightthickness=0,cursor='crosshair')
        xbar=tk.Scrollbar(frame,orient='horizontal',command=self.canvas.xview)
        ybar=tk.Scrollbar(frame,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xbar.set,yscrollcommand=ybar.set)
        self.canvas.grid(row=0,column=0,sticky='nsew');ybar.grid(row=0,column=1,sticky='ns');xbar.grid(row=1,column=0,sticky='ew')
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.canvas.bind('<ButtonPress-1>',self.down); self.canvas.bind('<B1-Motion>',self.move); self.canvas.bind('<ButtonRelease-1>',self.up)
        self.canvas.bind('<Motion>',self.pointer_move)
        self.canvas.bind('<Configure>',lambda e:self.redraw())
        self.canvas.bind('<Control-MouseWheel>',self._ctrl_wheel)
        self.protocol('WM_DELETE_WINDOW',self._close)
        self.pixel_info=tk.Label(self,text='像素座標：請先載入圖片',font=(FONT,11,'bold'),anchor='w',fg='#155e75')
        self.pixel_info.pack(fill='x',padx=14,pady=(4,0))
        self.status=tk.Label(self,text='尚未載入圖片',font=(FONT,11),anchor='w'); self.status.pack(fill='x',padx=14,pady=(1,5))
        self._refresh_layout_badge()

    def _layout_title(self,key=None):
        key=key or self.layout.get()
        return {'A':'A 一般','B':'B 特殊'}.get(key,self.profile_names.get(key,key))

    def _refresh_layout_badge(self):
        if not hasattr(self,'custom_active'):return
        key=self.layout.get()
        self.custom_active.config(text=(f'目前：{self.profile_names.get(key,key)}' if key in ('CA','CB') else ''))

    def _save_profile_names(self):
        try:
            with open(self.config_path,encoding='utf-8') as f:d=json.load(f)
            if not isinstance(d,dict):d={}
        except Exception:d={}
        d['_profile_names']=dict(self.profile_names)
        os.makedirs(os.path.dirname(self.config_path),exist_ok=True)
        with open(self.config_path,'w',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False,indent=2)

    def choose_custom_layout(self):
        """自訂版面平時收起；在小視窗中改名並選擇，避免四個版型擠在工具列。"""
        win=tk.Toplevel(self);win.title('選擇自訂版面');win.geometry('520x250');win.resizable(False,False)
        win.transient(self);win.grab_set()
        tk.Label(win,text='選擇要校正的自訂版面，也可以改成容易記得的名稱。',font=(FONT,12,'bold')).pack(anchor='w',padx=18,pady=(16,10))
        vars_={k:tk.StringVar(value=self.profile_names[k]) for k in ('CA','CB')}
        def choose(key):
            name=vars_[key].get().strip() or ('自訂版面 1' if key=='CA' else '自訂版面 2')
            self.profile_names[key]=name;self._save_profile_names()
            self.layout.set(key);win.destroy();self.layout_changed()
        for key,label in [('CA','自訂 1'),('CB','自訂 2')]:
            row=tk.Frame(win);row.pack(fill='x',padx=18,pady=5)
            tk.Label(row,text=label,width=8,font=(FONT,11,'bold'),anchor='w').pack(side='left')
            tk.Entry(row,textvariable=vars_[key],font=(FONT,11),width=28).pack(side='left',fill='x',expand=True,padx=5)
            tk.Button(row,text='選擇',font=(FONT,11,'bold'),command=lambda k=key:choose(k),width=8).pack(side='left',padx=4)
        def save_only():
            for key in ('CA','CB'):
                name=vars_[key].get().strip()
                if name:self.profile_names[key]=name
            self._save_profile_names();self._refresh_layout_badge();win.destroy()
        tk.Button(win,text='只儲存名稱',font=(FONT,11),command=save_only).pack(side='right',padx=20,pady=14)

    def _restore_saved_for_current_image(self):
        k=self.layout.get(); b=self.boxes.setdefault(k,{})
        saved=b.get('_saved') or {}
        if not self.img or not saved:return
        w,h=self.img.size
        for name,normkey in [('LIST','list_box_norm'),('ROW1','row1_box_norm'),('ROW2','row2_box_norm'),
                             ('POINT','point_box_norm'),('INPUT','input_box_norm'),('OUTPUT','output_box_norm'),
                             ('INPUT_QTY','input_qty_box_norm'),('OUTPUT_QTY','output_qty_box_norm')]:
            nb=saved.get(normkey)
            if nb and name not in b:
                b[name]=[round(nb[0]*w),round(nb[1]*h),round(nb[2]*w),round(nb[3]*h)]

    def _saved_with_qty_fallback(self,key):
        saved=dict((self.boxes.get(key,{}) or {}).get('_saved') or {})
        if not saved:return saved
        # 舊 A/B 沒有獨立數字框時，精確重現成熟 OCR 原本的隱藏裁切公式。
        row=saved.get('row1_box_norm');inp=saved.get('input_box_norm')
        if inp and not saved.get('input_qty_box_norm'):
            x1,y1,x2,y2=inp;w=x2-x1;h=y2-y1
            saved['input_qty_box_norm']=[x1+.075*w,y1+.42*h,x1+.185*w,y1+.99*h]
        if row and not saved.get('output_qty_box_norm'):
            x1,y1,x2,y2=row;w=x2-x1;h=y2-y1
            saved['output_qty_box_norm']=[x1+.625*w,y1+.34*h,x1+.712*w,y1+.99*h]
        return saved

    def copy_standard_layout(self,source):
        target=self.layout.get()
        if target not in ('CA','CB'):
            messagebox.showinfo('請先選自訂版面','請先按「選擇自訂版面」，選定要編輯的自訂版，再複製 A 或 B 模板。');return
        saved=self._saved_with_qty_fallback(source)
        if not saved:
            messagebox.showwarning('沒有可複製模板',f'目前找不到 {source} 版設定。');return
        if not messagebox.askyesno('複製標準模板',f'要用 {source} 版覆蓋「{self._layout_title(target)}」目前的框線嗎？'):
            return
        self.boxes[target]={'_saved':dict(saved)}
        if self.img:self._restore_saved_for_current_image()
        self.redraw();self._update_box_info()

    def open_image(self):
        p=filedialog.askopenfilename(title=f'選擇「{self._layout_title()}」的原始遊戲截圖',
            filetypes=[('圖片','*.png *.jpg *.jpeg *.webp'),('所有檔案','*.*')])
        if not p:return
        self.path=p; self.img=Image.open(p).convert('RGB');self.zoom=1.0
        self._restore_saved_for_current_image(); self.redraw()

    def layout_changed(self):
        # Important: do not reuse the other layout's currently opened sample.
        self.path=None; self.img=None; self.tkimg=None
        self.canvas.delete('all')
        self._refresh_layout_badge()
        self.status.config(text=f'{self._layout_title()}：請載入這個版型自己的原始截圖。')

    def clear_layout(self):
        self.boxes[self.layout.get()]={}
        self.redraw()

    def zoom_by(self,factor):
        if not self.img:return
        self.zoom=max(.5,min(4.0,self.zoom*float(factor)))
        self.redraw()

    def fit_view(self):
        self.zoom=1.0;self.redraw()

    def _ctrl_wheel(self,e):
        self.zoom_by(1.25 if getattr(e,'delta',0)>0 else 1/1.25)
        return 'break'

    def redraw(self):
        if not self.img:return
        cw=max(200,self.canvas.winfo_width()); ch=max(200,self.canvas.winfo_height())
        oldx=self.canvas.xview()[0] if self.canvas.xview() else 0
        oldy=self.canvas.yview()[0] if self.canvas.yview() else 0
        fit=min(cw/self.img.width,ch/self.img.height,1.0)
        self.scale=fit*self.zoom
        sz=(max(1,int(self.img.width*self.scale)),max(1,int(self.img.height*self.scale)))
        disp=self.img.resize(sz,Image.LANCZOS)
        self.tkimg=ImageTk.PhotoImage(disp); self.canvas.delete('all')
        self.canvas.create_image(0,0,image=self.tkimg,anchor='nw')
        self.canvas.configure(scrollregion=(0,0,sz[0],sz[1]))
        colors={'LIST':'#00ff66','ROW1':'#ffcc00','ROW2':'#ff66ff','POINT':'#00ffff','INPUT':'#ff8800','OUTPUT':'#66ff66',
                'INPUT_QTY':'#ff4444','OUTPUT_QTY':'#aa66ff'}
        for key,color in colors.items():
            box=self.boxes.get(self.layout.get(),{}).get(key)
            if box:
                x1,y1,x2,y2=box
                self.canvas.create_rectangle(x1*self.scale,y1*self.scale,x2*self.scale,y2*self.scale,outline=color,width=3)
                self.canvas.create_text((x1+5)*self.scale,(y1+5)*self.scale,text=key,fill='white',anchor='nw',font=(FONT,10,'bold'))
        self.canvas.xview_moveto(oldx);self.canvas.yview_moveto(oldy)
        self.status.config(text=f'{os.path.basename(self.path)}｜{self.img.width}×{self.img.height}｜{self._layout_title()}｜現在框：{self.mode.get()}｜顯示倍率 {self.zoom:.0%}')
        self._update_box_info()

    def _original_xy(self,e):
        if not self.img or not self.scale:return None
        x=round(self.canvas.canvasx(e.x)/self.scale);y=round(self.canvas.canvasy(e.y)/self.scale)
        return max(0,min(self.img.width,x)),max(0,min(self.img.height,y))

    def _update_box_info(self,pointer=None,box=None):
        if not hasattr(self,'pixel_info'):return
        box=box or self.boxes.get(self.layout.get(),{}).get(self.mode.get())
        parts=[]
        if pointer:parts.append(f'游標 X={pointer[0]}  Y={pointer[1]}')
        if box and len(box)==4:
            x1,y1,x2,y2=map(int,box)
            parts.append(f'{self.mode.get()}：({x1}, {y1}) → ({x2}, {y2})｜寬 {x2-x1}px × 高 {y2-y1}px')
        self.pixel_info.config(text='　｜　'.join(parts) if parts else '像素座標：移動滑鼠可查看；拖曳時會顯示框線寬高')

    def pointer_move(self,e):
        p=self._original_xy(e)
        if not self.drag_start:self._update_box_info(pointer=p)

    def down(self,e): self.drag_start=(self.canvas.canvasx(e.x),self.canvas.canvasy(e.y))
    def move(self,e):
        if not self.drag_start:return
        if self.temp:self.canvas.delete(self.temp)
        x,y=self.drag_start;cx=self.canvas.canvasx(e.x);cy=self.canvas.canvasy(e.y)
        self.temp=self.canvas.create_rectangle(x,y,cx,cy,outline='#ffffff',width=3)
        if self.img:
            box=[round(min(x,cx)/self.scale),round(min(y,cy)/self.scale),round(max(x,cx)/self.scale),round(max(y,cy)/self.scale)]
            self._update_box_info(pointer=self._original_xy(e),box=box)
    def up(self,e):
        if not self.img or not self.drag_start:return
        x1,y1=self.drag_start;x2,y2=self.canvas.canvasx(e.x),self.canvas.canvasy(e.y);self.drag_start=None
        if self.temp:self.canvas.delete(self.temp); self.temp=None
        x1,x2=sorted((x1,x2)); y1,y2=sorted((y1,y2))
        if x2-x1<12 or y2-y1<12:return
        ox1=max(0,min(self.img.width,round(x1/self.scale)));ox2=max(0,min(self.img.width,round(x2/self.scale)))
        oy1=max(0,min(self.img.height,round(y1/self.scale)));oy2=max(0,min(self.img.height,round(y2/self.scale)))
        snapped=0
        if self.snap_enabled.get():
            current=self.mode.get();others=[box for key,box in self.boxes.get(self.layout.get(),{}).items()
                                           if key not in ('_saved',current) and isinstance(box,list)]
            tolerance=max(2,round(12/max(self.scale,.01)))
            (ox1,oy1,ox2,oy2),snapped=snap_box_to_edges(
                [ox1,oy1,ox2,oy2],others,tolerance,self.img.size)
        if ox2-ox1<4 or oy2-oy1<4:return
        self.boxes.setdefault(self.layout.get(),{})[self.mode.get()]=[ox1,oy1,ox2,oy2]
        self.redraw()
        if snapped:
            self.status.config(text=self.status.cget('text')+f'｜🧲 已吸附 {snapped} 條邊線')

    def _geometry_warnings(self,b):
        warnings=[]
        r1=b['ROW1'];r2=b['ROW2'];row_h=r1[3]-r1[1];pitch=r2[1]-r1[1]
        if pitch<=0:warnings.append('ROW2 必須在 ROW1 下方。')
        elif not (.80*row_h<=pitch<=1.30*row_h):warnings.append('ROW1／ROW2 的列距和列高差太多。')
        for key in ('INPUT_QTY','OUTPUT_QTY'):
            x1,y1,x2,y2=b[key];bw=x2-x1;bh=y2-y1
            if bw<.35*row_h or bh<.45*row_h:
                warnings.append(f'{key} 太小：請框「完整物品縮圖＋右下角數字」，不要只框數字。')
        if b['LIST'][3] < r1[1]+max(1,pitch)*5+row_h*.75:
            warnings.append('交換列表高度可能沒有涵蓋完整 6 列。')
        return warnings

    def _rows(self):
        b=self.boxes.get(self.layout.get(),{})
        if 'ROW1' not in b or 'ROW2' not in b:return []
        r1=b['ROW1']; r2=b['ROW2']
        pitch=r2[1]-r1[1]
        if pitch<=0:return []
        return [[r1[0],r1[1]+i*pitch,r1[2],r1[3]+i*pitch] for i in range(6)]

    def preview(self):
        if not self.img:return
        rows=self._rows()
        if not rows:
            messagebox.showwarning('還差一步','請先框 ROW1 和 ROW2；兩列會直接決定真正列距。'); return
        self.redraw()
        b=self.boxes.get(self.layout.get(),{})
        for i,r in enumerate(rows,1):
            x1,y1,x2,y2=r
            self.canvas.create_rectangle(x1*self.scale,y1*self.scale,x2*self.scale,y2*self.scale,outline='#ff33cc',width=2)
            self.canvas.create_text((x1+6)*self.scale,(y1+6)*self.scale,text=f'ROW{i}',fill='white',anchor='nw',font=(FONT,10,'bold'))
            # Copy field X positions and field vertical offsets from ROW1 to every row.
            for key,color in [('POINT','#00ffff'),('INPUT','#ff8800'),('OUTPUT','#66ff66'),
                              ('INPUT_QTY','#ff4444'),('OUTPUT_QTY','#aa66ff')]:
                fb=b.get(key)
                if not fb: continue
                dy=y1-b['ROW1'][1]
                self.canvas.create_rectangle(fb[0]*self.scale,(fb[1]+dy)*self.scale,fb[2]*self.scale,(fb[3]+dy)*self.scale,
                                             outline=color,width=1)

    def _close(self):
        self.destroy()
        try:self.parent.after(50,lambda:(self.parent.lift(),self.parent.focus_force()))
        except:pass

    def save_layout(self):
        if not self.img:return
        k=self.layout.get(); b=self.boxes.get(k,{})
        needed=['LIST','ROW1','ROW2','POINT','INPUT','OUTPUT','INPUT_QTY','OUTPUT_QTY']
        miss=[x for x in needed if x not in b]
        if miss:
            messagebox.showwarning('不能儲存','還沒框：'+'、'.join(miss)); return
        warnings=self._geometry_warnings(b)
        if warnings and not messagebox.askyesno('裁切框可能有問題','\n'.join('• '+x for x in warnings)+'\n\n仍要儲存嗎？'):
            return
        w,h=self.img.size
        def norm(box):return [round(box[0]/w,8),round(box[1]/h,8),round(box[2]/w,8),round(box[3]/h,8)]
        out={}
        try:
            with open(self.config_path,encoding='utf-8') as f:out=json.load(f)
        except Exception:pass
        out[k]={
            'source_size':[w,h],
            'sample_filename':os.path.basename(self.path),
            'list_box_norm':norm(b['LIST']),
            'row1_box_norm':norm(b['ROW1']),
            'row2_box_norm':norm(b['ROW2']),
            'point_box_norm':norm(b['POINT']),
            'input_box_norm':norm(b['INPUT']),
            'output_box_norm':norm(b['OUTPUT']),
            'input_qty_box_norm':norm(b['INPUT_QTY']),
            'output_qty_box_norm':norm(b['OUTPUT_QTY'])
        }
        out['_profile_names']=dict(self.profile_names)
        os.makedirs(os.path.dirname(self.config_path),exist_ok=True)
        with open(self.config_path,'w',encoding='utf-8') as f:json.dump(out,f,ensure_ascii=False,indent=2)
        self.boxes[k]['_saved']=out[k]
        messagebox.showinfo('已儲存',f'「{self._layout_title(k)}」已儲存。\n列距、文字欄與 INPUT／OUTPUT 數字欄都會照你的框。\n請再用「版面自救健檢」實際試跑一張。')
