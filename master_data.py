import json
import os
import unicodedata

class MasterData:
    def __init__(self,path,user_alias_path=None,public_alias_path=None):
        with open(path,"r",encoding="utf-8") as f:
            data=json.load(f)
        self.items=data["items"]
        self.points=data["points"]
        self.aliases=list(data["aliases"])
        self.user_alias_path=user_alias_path
        self.public_aliases=[]
        if public_alias_path and os.path.exists(public_alias_path):
            try:
                with open(public_alias_path,"r",encoding="utf-8") as f:pa=json.load(f)
                if isinstance(pa,list):self.public_aliases=[x for x in pa if isinstance(x,dict)];self.aliases.extend(self.public_aliases)
            except Exception:self.public_aliases=[]
        self.user_aliases=[]
        if user_alias_path and os.path.exists(user_alias_path):
            try:
                with open(user_alias_path,"r",encoding="utf-8") as f:
                    ua=json.load(f)
                if isinstance(ua,list):
                    self.user_aliases=ua
                    self.aliases.extend(ua)
            except Exception:
                self.user_aliases=[]

        self.item_by_id={x["id"]:x for x in self.items}
        self.point_by_id={x["id"]:x for x in self.points}

        self.item_name={}
        for x in self.items:
            self.item_name[self._n(x["name"])]=x["id"]
            if x.get("abbr"):
                self.item_name[self._n(x["abbr"])]=x["id"]

        self.point_name={self._n(x["name"]):x["id"] for x in self.points}
        self.alias_map={}
        for a in self.aliases:
            self.alias_map[(a["object_type"],self._n(a["alias"]))]=a["id"]

    def _n(self,s):
        # OCR 中文模型常把繁體字輸出成簡體。只在「比對鍵」統一字形；
        # 原始OCR證物與遊戲正式繁體名稱完全不改寫。
        text="".join(unicodedata.normalize("NFKC",str(s or "")).strip().split())
        table=str.maketrans({
            "島":"岛","黃":"黄","賊":"贼","餘":"余","換":"换","數":"数",
            "罈":"罐","瑪":"玛","麗":"丽","燈":"灯","鹽":"盐","幣":"币",
            "華":"华","團":"团","騎":"骑","頭":"头","龍":"龙","樹":"树",
            "塊":"块","觀":"观","測":"测","報":"报","書":"书","鴉":"鸦",
            "烏":"乌","遺":"遗","運":"运","結":"结","質":"质","貿":"贸",
            "維":"维","藍":"蓝","鑄":"铸","鏡":"镜","偵":"侦","戰":"战",
            "鬥":"斗","糧":"粮","統":"统","傳":"传","遠":"远","裝":"装",
            "飾":"饰","漿":"浆","櫻":"樱","參":"参","鱗":"鳞","斷":"断",
            "萬":"万","綠":"绿","門":"门","槍":"枪","飲":"饮","錫":"锡",
            "魚":"鱼","鷹":"鹰","頸":"颈","髏":"髅","製":"制","絲":"丝",
            "綢":"绸","銅":"铜","鑰":"钥","蘿":"萝","蔔":"卜","練":"练",
            "堅":"坚","縫":"缝","織":"织","獲":"获","協":"协","級":"级",
            "燭":"烛","臺":"台","楓":"枫"
        })
        return text.translate(table)

    def add_user_alias(self, object_type, alias, target_id):
        object_type="點位" if object_type in ("POINT","點位") else "品項"
        alias=str(alias or "").strip()
        if not alias:
            return False,"別名不可空白"
        valid=self.point_by_id if object_type=="點位" else self.item_by_id
        if target_id not in valid:
            return False,"目標ID不存在"
        key=(object_type,self._n(alias))
        existing=self.alias_map.get(key)
        if existing and existing!=target_id:
            return False,f"這個OCR別名已指向 {existing}，為避免亂認不自動覆蓋"
        if existing==target_id:
            return True,"這個別名已經學過了"
        rec={"object_type":object_type,"alias":alias,"id":target_id,"source":"玩家確認OCR"}
        self.user_aliases.append(rec)
        self.aliases.append(rec)
        self.alias_map[key]=target_id
        if self.user_alias_path:
            os.makedirs(os.path.dirname(self.user_alias_path),exist_ok=True)
            with open(self.user_alias_path,"w",encoding="utf-8") as f:
                json.dump(self.user_aliases,f,ensure_ascii=False,indent=2)
        return True,"已永久記住"

    def resolve_item(self,text):
        key=self._n(text)
        if key in self.item_name:
            iid=self.item_name[key]
            return {"matched":True,"id":iid,"record":self.item_by_id[iid],"via":"正式名稱/縮寫"}
        aid=self.alias_map.get(("品項",key))
        if aid:
            return {"matched":True,"id":aid,"record":self.item_by_id.get(aid),"via":"別名"}
        return {"matched":False,"query":text}

    def resolve_point(self,text):
        key=self._n(text)
        if key in self.point_name:
            pid=self.point_name[key]
            return {"matched":True,"id":pid,"record":self.point_by_id[pid],"via":"正式名稱"}
        aid=self.alias_map.get(("點位",key))
        if aid:
            return {"matched":True,"id":aid,"record":self.point_by_id.get(aid),"via":"別名"}
        # Parser convenience only: common visible suffix.
        if key.endswith("島") and key[:-1] in self.point_name:
            pid=self.point_name[key[:-1]]
            return {"matched":True,"id":pid,"record":self.point_by_id[pid],"via":"島字尾正規化"}
        return {"matched":False,"query":text}
