def first_unhandled_index(route):
    for i,s in enumerate(route["stations"]):
        if s.get("status","未開始")=="未開始":
            return i
    return None

def process_station(route,result):
    idx=first_unhandled_index(route)
    if idx is None:
        route["status"]="已完成"
        return None
    route["stations"][idx]["status"]=result
    nxt=first_unhandled_index(route)
    route["current_index"]=0 if nxt is None else nxt
    route["status"]="已完成" if nxt is None else "進行中"
    return nxt

def route_progress(route):
    handled=sum(1 for s in route["stations"] if s.get("status") in ("完成","跳過"))
    return handled,len(route["stations"])
