import os, json, tkinter as tk
from tkinter import filedialog, messagebox

FONT="Microsoft JhengHei UI"

class LayoutAssigner(tk.Toplevel):
    def __init__(self,parent,config_path):
        super().__init__(parent)
        self.title("🧩 版型指派器｜V5.22")
        self.geometry("980x720")
        self.config_path=config_path
        self.parent=parent
        self.rows=[]
        self.build()
        self.load_existing()
        self.protocol('WM_DELETE_WINDOW',self._close)

    def _close(self):
        self.destroy()
        try:self.parent.after(50,lambda:(self.parent.lift(),self.parent.focus_force()))
        except:pass

    def build(self):
        top=tk.Frame(self); top.pack(fill="x",padx=10,pady=8)
        tk.Button(top,text="① 選擇本輪全部截圖",font=(FONT,13,"bold"),command=self.pick).pack(side="left")
        tk.Button(top,text="全部設為 A",font=(FONT,11),command=lambda:self.set_all("A")).pack(side="left",padx=6)
        tk.Button(top,text="全部設為舊A",font=(FONT,11),command=lambda:self.set_all("CA")).pack(side="left",padx=6)
        tk.Button(top,text="儲存指派",font=(FONT,13,"bold"),command=self.save).pack(side="right")

        tk.Label(self,text="做法：標準電腦用A/B；不同電腦或UI縮放用舊A/舊B。檔名不必改成1～15。",
                 font=(FONT,12,"bold"),anchor="w").pack(fill="x",padx=12,pady=(0,6))

        frame=tk.Frame(self); frame.pack(fill="both",expand=True,padx=10,pady=5)
        self.canvas=tk.Canvas(frame,highlightthickness=0)
        sb=tk.Scrollbar(frame,orient="vertical",command=self.canvas.yview)
        self.inner=tk.Frame(self.canvas)
        self.inner.bind("<Configure>",lambda e:self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0,0),window=self.inner,anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        def _wheel(e):
            self.canvas.yview_scroll(int(-1*(e.delta/120)),"units"); return "break"
        self.canvas.bind("<Enter>",lambda e:self.canvas.bind_all("<MouseWheel>",_wheel))
        self.canvas.bind("<Leave>",lambda e:self.canvas.unbind_all("<MouseWheel>"))
        self.status=tk.Label(self,text="",font=(FONT,11),anchor="w"); self.status.pack(fill="x",padx=12,pady=5)

    def load_existing(self):
        try:
            with open(self.config_path,encoding="utf-8") as f:self.existing=json.load(f)
        except Exception:self.existing={}

    def pick(self):
        paths=filedialog.askopenfilenames(title="選擇這一刷要OCR的全部原始截圖",
            filetypes=[("圖片","*.png *.jpg *.jpeg *.webp"),("所有檔案","*.*")])
        if not paths:return
        for w in self.inner.winfo_children():w.destroy()
        self.rows=[]
        for i,p in enumerate(paths,1):
            name=os.path.basename(p)
            old=str(self.existing.get(name,"A")).upper()
            if old=="C":old="CA"
            v=tk.StringVar(value=old if old in ("A","B","CA","CB") else "A")
            row=tk.Frame(self.inner); row.pack(fill="x",pady=2)
            tk.Label(row,text=f"{i:02d}",width=4,font=(FONT,11,"bold")).pack(side="left")
            tk.Label(row,text=name,anchor="w",width=72,font=(FONT,11)).pack(side="left",fill="x",expand=True)
            tk.Radiobutton(row,text="A",variable=v,value="A",font=(FONT,11,"bold")).pack(side="left")
            tk.Radiobutton(row,text="B",variable=v,value="B",font=(FONT,11,"bold")).pack(side="left")
            tk.Radiobutton(row,text="舊A",variable=v,value="CA",font=(FONT,11,"bold")).pack(side="left")
            tk.Radiobutton(row,text="舊B",variable=v,value="CB",font=(FONT,11,"bold")).pack(side="left")
            self.rows.append((name,v))
        self.status.config(text=f"已載入 {len(self.rows)} 張。請把特殊版面的那一張點成 B。")

    def set_all(self,val):
        for _,v in self.rows:v.set(val)

    def save(self):
        if not self.rows:
            messagebox.showwarning("尚未選圖","請先選擇原始截圖。");return
        d=dict(self.existing)
        for name,v in self.rows:d[name]=v.get()
        os.makedirs(os.path.dirname(self.config_path),exist_ok=True)
        with open(self.config_path,"w",encoding="utf-8") as f:json.dump(d,f,ensure_ascii=False,indent=2)
        self.existing=d
        b=[n for n,v in self.rows if v.get()=="B"]
        messagebox.showinfo("已儲存",f"版型指派已儲存。\nB版：{', '.join(b) if b else '目前沒有'}")
