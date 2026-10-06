import json, os

class StateStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {"batch_id":None,"status":"尚未開始","routes":[],"inventory":{},"runtime_aliases":[]}
        with open(self.path,"r",encoding="utf-8") as f:
            return json.load(f)

    def save(self,state):
        os.makedirs(os.path.dirname(self.path),exist_ok=True)
        with open(self.path,"w",encoding="utf-8") as f:
            json.dump(state,f,ensure_ascii=False,indent=2)
