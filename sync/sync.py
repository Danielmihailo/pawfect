#!/usr/bin/env python3
"""
lovepawfect <- Zoodrop Sync  (Varianten gruppiert nach SUCHNUMMER)
  --mode full        : zoodrop_utf8.csv -> Produkte (mit Varianten) anlegen/aktualisieren, inkl. Bilder
  --mode update      : update_utf8.csv  -> nur Preis/Bestand/Policy je Variante (schnell, taeglich)
  --mode retag       : zoodrop_utf8.csv -> nur Tags/Produkttyp je Produkt
  --mode collections : Smart Collections (Welten + Kategorien) idempotent
  --mode wipe        : ALLE Produkte loeschen (Neuaufbau)
  --dry-run / --limit N / --local PATH

Gruppierung: alle Zeilen mit gleicher SUCHNUMMER = EIN Produkt; jede Zeile (EAN) = eine Variante.
Variantenname = Teil des TITEL nach dem gemeinsamen Praefix. Einzel-Artikel = 1 Variante ohne Optionen.
Preisregel: Brutto = EK*FACTOR, gedeckelt UVP-1, Marge>=MIN_MARGIN, Charm x,99.
Tags: Tier + Bedarf + Marke. Bildlose Gruppen uebersprungen.
"""
import os, sys, csv, io, json, time, base64, argparse, urllib.request, urllib.error

SHOP   = os.environ.get("SHOPIFY_SHOP", "lovepawfect.myshopify.com")
TOKEN  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
APIV   = os.environ.get("SHOPIFY_API_VERSION", "2025-01")
FACTOR = float(os.environ.get("PRICE_FACTOR", "1.7"))
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "2.0"))
STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")
SKIP_NO_IMAGE = os.environ.get("SKIP_NO_IMAGE", "1") == "1"
FULL_URL   = "https://www.zoodrop.de/csv/download/zoodrop_utf8.csv"
UPDATE_URL = "https://www.zoodrop.de/csv/download/update_utf8.csv"

def log(*a): print(*a, flush=True)

# ---------- Taxonomie ----------
TIER_MAP = {"Hundewelt":"Hund","Katzenwelt":"Katze","Kleintierwelt":"Kleintier","Vogelwelt":"Vogel",
            "Aquarienwelt":"Aquaristik","Terrarienwelt":"Reptil","Gartenwelt":"Garten"}
CAT_RULES = [
    ("trockenfutter",["Trockenfutter","Futter"]),("nassfutter",["Nassfutter","Futter"]),
    ("ergänz",["Futterergänzung","Futter"]),("erganz",["Futterergänzung","Futter"]),
    ("leckerl",["Snacks"]),("kausnack",["Snacks"]),("snack",["Snacks"]),("futter",["Futter"]),
    ("spielzeug",["Spielzeug"]),("kratz",["Kratzbäume"]),
    ("bett",["Schlafen"]),("körb",["Schlafen"]),("korb",["Schlafen"]),("kissen",["Schlafen"]),
    ("höhle",["Schlafen"]),("hoehle",["Schlafen"]),("decke",["Schlafen"]),("schlaf",["Schlafen"]),
    ("leine",["Leinen"]),("halsband",["Halsbänder"]),("halsbänder",["Halsbänder"]),("geschirr",["Geschirre"]),
    ("transport",["Transport"]),("reise",["Transport"]),("auto",["Transport"]),
    ("näpfe",["Näpfe"]),("napf",["Näpfe"]),("tränke",["Näpfe"]),("traenke",["Näpfe"]),("brunnen",["Näpfe"]),
    ("fellpflege",["Pflege"]),("pflege",["Pflege"]),("hygiene",["Pflege"]),("zahn",["Pflege"]),
    ("kralle",["Pflege"]),("bürste",["Pflege"]),("shampoo",["Pflege"]),
    ("gesundheit",["Gesundheit"]),("apotheke",["Gesundheit"]),("wurm",["Gesundheit"]),
    ("floh",["Gesundheit"]),("zecke",["Gesundheit"]),("vitamin",["Gesundheit"]),
    ("toilette",["Katzenklo"]),("katzenklo",["Katzenklo"]),
    ("erziehung",["Erziehung"]),("training",["Erziehung"]),("clicker",["Erziehung"]),
    ("bekleidung",["Bekleidung"]),("mantel",["Bekleidung"]),("pullover",["Bekleidung"]),
    ("käfig",["Käfige"]),("kaefig",["Käfige"]),("gehege",["Käfige"]),("voliere",["Käfige"]),("stall",["Käfige"]),("nest",["Käfige"]),
    ("filter",["Technik"]),("pumpe",["Technik"]),("heizer",["Technik"]),("beleuchtung",["Technik"]),("co2",["Technik"]),("technik",["Technik"]),
    ("wasserpflege",["Wasserpflege"]),("wasseraufbereit",["Wasserpflege"]),("wassertest",["Wasserpflege"]),
    ("heu",["Einstreu"]),("einstreu",["Einstreu"]),
    ("ausstattung",["Zubehör"]),("zubehör",["Zubehör"]),("zubehoer",["Zubehör"]),
]
WORLD_HANDLE={"Hund":"hundewelt","Katze":"katzenwelt","Kleintier":"kleintierwelt","Vogel":"vogelwelt","Aquaristik":"aquarienwelt","Reptil":"terrarienwelt","Garten":"gartenwelt"}
WORLD_SLUG={"Hund":"hund","Katze":"katze","Kleintier":"kleintier","Vogel":"vogel","Aquaristik":"aquaristik","Reptil":"reptil","Garten":"garten"}
WORLD_TITLE={"Hund":"Hundewelt","Katze":"Katzenwelt","Kleintier":"Kleintierwelt","Vogel":"Vogelwelt","Aquaristik":"Aquaristik","Reptil":"Terraristik","Garten":"Garten & Outdoor"}
CAT_HANDLE={"Futter":"futter","Trockenfutter":"trockenfutter","Nassfutter":"nassfutter","Snacks":"snacks","Spielzeug":"spielzeug","Leinen":"leinen","Halsbänder":"halsbaender","Geschirre":"geschirre","Schlafen":"schlafen","Näpfe":"naepfe","Pflege":"pflege","Transport":"transport","Erziehung":"erziehung","Bekleidung":"bekleidung","Gesundheit":"gesundheit","Kratzbäume":"kratzbaeume","Katzenklo":"katzenklo","Einstreu":"einstreu","Käfige":"kaefige","Zubehör":"zubehoer","Technik":"technik","Wasserpflege":"wasserpflege"}
WORLD_CATS={
    "Hund":["Futter","Trockenfutter","Nassfutter","Snacks","Spielzeug","Leinen","Halsbänder","Geschirre","Schlafen","Näpfe","Pflege","Transport","Erziehung","Bekleidung","Gesundheit"],
    "Katze":["Futter","Trockenfutter","Nassfutter","Snacks","Spielzeug","Kratzbäume","Schlafen","Katzenklo","Näpfe","Pflege","Transport","Geschirre","Gesundheit"],
    "Kleintier":["Futter","Einstreu","Käfige","Zubehör","Transport","Pflege"],
    "Vogel":["Futter","Käfige","Zubehör"],"Aquaristik":["Futter","Wasserpflege","Technik","Zubehör"],
    "Reptil":["Zubehör","Technik"],"Garten":[],
}

# ---------- CSV / Mapping ----------
def fnum(s):
    s=(s or "").strip().replace(",",".")
    try: return float(s)
    except ValueError: return 0.0

def load_rows(mode, local):
    if local:
        return list(csv.DictReader(open(local, encoding="utf-8", errors="replace"), delimiter=";", quotechar='"'))
    url = UPDATE_URL if mode=="update" else FULL_URL
    log("Lade Feed:", url)
    req=urllib.request.Request(url, headers={"User-Agent":"lovepawfect-sync/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data=r.read().decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(data), delimiter=";", quotechar='"'))

def group_rows(rows):
    groups={}; order=[]
    for r in rows:
        key=(r.get("SUCHNUMMER") or "").strip() or (r.get("ARTIKELNUMMER") or "").strip() or (r.get("EAN") or "").strip()
        if not key: continue
        if key not in groups: groups[key]=[]; order.append(key)
        groups[key].append(r)
    return [(k, groups[k]) for k in order]

def is_available(row):
    if row.get("VERKAUFSSTATUS","").strip()!="Im Verkauf": return False
    return fnum(row.get("BESTAND"))>0 or row.get("LAGERSTATUS","").strip().lower()=="fullfillment"

def compute_gross(ek, uvp, vat):
    if ek<=0: return None
    g=ek*FACTOR; cap=uvp-1.0 if uvp and uvp>0 else None
    if cap is not None: g=min(g,cap)
    if g<=0: return None
    if (g/(1.0+vat/100.0))-ek < MIN_MARGIN: return None
    charm=float(int(g))+0.99
    if charm>ek and (cap is None or charm<=cap) and abs(charm-g)<=1.0: g=charm
    return round(g,2)

def categories(row):
    raw=row.get("KATEGORIE","") or ""; low=raw.lower()
    tiers=[]; ptype=""
    for p in [x for x in raw.split("|") if x.strip()]:
        levels=[l.strip() for l in p.split(">") if l.strip()]
        if not levels: continue
        if not ptype: ptype=levels[0]
        t=TIER_MAP.get(levels[0])
        if t and t not in tiers: tiers.append(t)
    cats=[]
    for kw,ts in CAT_RULES:
        if kw in low:
            for t in ts:
                if t not in cats: cats.append(t)
    if "streu" in low: cats.append("Katzenklo" if "Katze" in tiers else "Einstreu")
    tags=[]
    for t in tiers+cats:
        if t not in tags: tags.append(t)
    vendor=row.get("HERSTELLER","").strip()
    if vendor and vendor not in tags: tags.append(vendor)
    return ptype, tags

def images(row):
    return [row[f"BILD{i}"].strip() for i in range(1,9) if row.get(f"BILD{i}","").strip()]

def group_images(rows):
    seen=set(); urls=[]
    for r in rows:
        for u in images(r):
            if u not in seen: seen.add(u); urls.append(u)
    return urls[:6]

def body_html(row):
    html=row.get("BESCHREIBUNG","") or ""
    gp,gpe=fnum(row.get("GRUNDPREIS")),(row.get("GRUNDPREIS_EINHEIT") or "").strip()
    if gp>0 and gpe: html+=f'<p class="grundpreis"><small>Grundpreis: {gp:.2f} € / {gpe}</small></p>'
    return html

def fetch_image(url):
    try:
        req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (lovepawfect-sync)"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return base64.b64encode(r.read()).decode()
    except Exception: return None

def variant_labels(titles):
    """gemeinsamer Praefix entfernen -> Varianten-Labels + Produkttitel"""
    if len(titles)==1: return [""], titles[0]
    s1=min(titles); s2=max(titles); i=0
    while i<len(s1) and i<len(s2) and s1[i]==s2[i]: i+=1
    seps=" -–—/|,·"
    ptitle=titles[0][:i].strip(seps) or titles[0]
    raw=[t[i:].strip(seps) if t[:i]==titles[0][:i] else t for t in titles]
    out=[]; seen={}
    for idx,lab in enumerate(raw):
        if not lab: lab="Variante "+str(idx+1)
        if lab in seen: seen[lab]+=1; lab=f"{lab} ({seen[lab]})"
        else: seen[lab]=1
        out.append(lab[:80])
    return out, ptitle

# ---------- Shopify REST ----------
def api(method, path, body=None):
    url=f"https://{SHOP}/admin/api/{APIV}/{path}"
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url, data=data, method=method, headers={"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                used,cap=(int(x) for x in r.headers.get("X-Shopify-Shop-Api-Call-Limit","0/40").split("/"))
                if used>cap*0.7: time.sleep(0.6)
                txt=r.read().decode(); return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.0*(attempt+1)); continue
            if e.code>=500: time.sleep(1.5*(attempt+1)); continue
            log("HTTP",e.code,method,path,e.read().decode()[:300]); raise
    raise RuntimeError(f"API failed: {method} {path}")

def get_location_id():
    locs=api("GET","locations.json").get("locations",[])
    for l in locs:
        if l.get("active"): return l["id"]
    return locs[0]["id"] if locs else None

def load_state():
    try: return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception: return {}
def save_state(st): json.dump(st, open(STATE_FILE,"w",encoding="utf-8"), ensure_ascii=False)

def build_state_from_store(state):
    """barcode(EAN) -> {p:product_id, v:variant_id, i:inventory_item_id}"""
    since,total=0,0
    while True:
        data=api("GET", f"products.json?limit=250&since_id={since}&fields=id,variants")
        prods=data.get("products",[])
        if not prods: break
        for p in prods:
            since=max(since,p["id"])
            for v in p.get("variants",[]):
                bc=(v.get("barcode") or "").strip()
                if bc: state.setdefault(bc,{"p":p["id"],"v":v["id"],"i":v.get("inventory_item_id")})
            total+=1
        log(f"  ...{total} vorhandene Produkte gelesen")
    log(f"State aus Store: {total} Produkte, {len(state)} Barcodes")

# ---------- Create (gruppiert) ----------
def qualifying(group):
    q=[]
    for r in group:
        if not is_available(r): continue
        g=compute_gross(fnum(r.get("VERKAUFSPREIS")), fnum(r.get("UVP")), fnum(r.get("MWST") or "19"))
        if g is None: continue
        q.append((r,g))
    return q

def create_grouped(group, loc_id):
    q=qualifying(group)
    if not q: return "drop", None
    rows=[r for r,_ in q]
    imgs_urls=group_images(rows)
    if SKIP_NO_IMAGE and not imgs_urls: return "noimg", None
    art0=((rows[0].get("ARTIKELNUMMER","") or "img").strip().replace(" ","-")) or "img"
    imgs=[]
    for idx,u in enumerate(imgs_urls):
        b=fetch_image(u)
        if b: imgs.append({"attachment":b,"filename":f"{art0}-{idx+1}.jpg"})
    if SKIP_NO_IMAGE and not imgs: return "noimg", None
    rep=rows[0]; ptype,tags=categories(rep); multi=len(q)>1
    labels,ptitle=variant_labels([(r.get("TITEL") or "").strip() for r in rows])
    variants=[]
    for idx,(r,g) in enumerate(q):
        fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
        var={"price":f"{g:.2f}","sku":r.get("ARTIKELNUMMER","").strip(),"barcode":r.get("EAN","").strip(),
             "inventory_management":"shopify","inventory_policy":"continue" if fulfill else "deny","taxable":True}
        if multi: var["option1"]=labels[idx]
        variants.append(var)
    payload={"product":{"title":ptitle,"body_html":body_html(rep),"vendor":rep.get("HERSTELLER","").strip(),
        "product_type":ptype,"tags":", ".join(tags),"status":"active","images":imgs,"variants":variants}}
    if multi: payload["product"]["options"]=["Variante"]
    p=api("POST","products.json",payload)["product"]
    bestand={ (r.get("EAN","").strip()): int(fnum(r.get("BESTAND"))) for r,_ in q }
    vmap={}
    for var in p["variants"]:
        bc=(var.get("barcode") or "").strip()
        if loc_id and var.get("inventory_item_id"):
            api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":var["inventory_item_id"],"available":max(bestand.get(bc,0),0)})
        vmap[bc]={"v":var["id"],"i":var.get("inventory_item_id")}
    return "created", {"p":p["id"],"variants":vmap}

def update_grouped(group, state, loc_id, full):
    pid=None
    for r in group:
        bc=r.get("EAN","").strip()
        if bc in state: pid=state[bc]["p"]; break
    if not pid: return "miss"
    for r,g in qualifying(group):
        bc=r.get("EAN","").strip(); fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
        if bc in state:
            api("PUT",f"variants/{state[bc]['v']}.json",{"variant":{"id":state[bc]['v'],"price":f"{g:.2f}","inventory_policy":"continue" if fulfill else "deny"}})
            if loc_id and state[bc].get("i"):
                api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":state[bc]["i"],"available":max(int(fnum(r.get('BESTAND'))),0)})
    if full:
        ptype,tags=categories(group[0])
        api("PUT",f"products/{pid}.json",{"product":{"id":pid,"status":"active","product_type":ptype,"tags":", ".join(tags)}})
    return "updated"

# ---------- Collections ----------
def find_collection(handle):
    sc=api("GET",f"smart_collections.json?handle={handle}").get("smart_collections",[])
    return sc[0] if sc else None
def upsert_collection(handle,title,conds):
    rules=[{"column":"tag","relation":"equals","condition":c} for c in conds]
    body={"smart_collection":{"title":title,"handle":handle,"disjunctive":False,"rules":rules,"published":True}}
    ex=find_collection(handle)
    if ex:
        body["smart_collection"]["id"]=ex["id"]; api("PUT",f"smart_collections/{ex['id']}.json",body); return "updated"
    api("POST","smart_collections.json",body); return "created"
def make_collections():
    n={"created":0,"updated":0}
    for world,slug in WORLD_SLUG.items():
        n[upsert_collection(WORLD_HANDLE[world],WORLD_TITLE[world],[world])]+=1
        for cat in WORLD_CATS.get(world,[]):
            n[upsert_collection(f"{slug}-{CAT_HANDLE[cat]}",f"{world} · {cat}",[world,cat])]+=1
    log(f"Collections: {n['created']} neu, {n['updated']} aktualisiert.")

def wipe_all():
    total=0
    while True:
        prods=api("GET","products.json?limit=250&fields=id").get("products",[])
        if not prods: break
        for p in prods: api("DELETE",f"products/{p['id']}.json"); total+=1
        log(f"  ...{total} geloescht")
    try: os.remove(STATE_FILE)
    except OSError: pass
    log(f"Wipe fertig: {total} Produkte geloescht, State geleert.")

# ---------- Main ----------
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["full","update","retag","collections","wipe"],default="full")
    ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--local",default="")
    args=ap.parse_args()

    if args.mode=="collections":
        if args.dry_run:
            for w,s in WORLD_SLUG.items():
                log(WORLD_HANDLE[w],"->",w)
                for c in WORLD_CATS.get(w,[]): log(f"  {s}-{CAT_HANDLE[c]} -> {w}+{c}")
            return
        make_collections(); return
    if args.mode=="wipe":
        if args.dry_run: log("[DRY-RUN] wuerde alle Produkte loeschen."); return
        wipe_all(); return

    rows=load_rows(args.mode,args.local); log(f"{len(rows)} Zeilen geladen.")
    state={}   # immer frisch aus dem Store (verhindert veraltete Zuordnungen nach Wipe)
    loc_id=None if args.dry_run else get_location_id()
    if not args.dry_run: log("Location:",loc_id); build_state_from_store(state)

    if args.mode=="update":
        st={"updated":0,"miss":0,"errors":0}
        for r in rows:
            bc=r.get("EAN","").strip()
            if bc not in state: st["miss"]+=1; continue
            if not is_available(r): continue
            g=compute_gross(fnum(r.get("VERKAUFSPREIS")),fnum(r.get("UVP")),fnum(r.get("MWST") or "19"))
            if g is None: continue
            if args.dry_run: st["updated"]+=1; continue
            try:
                fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
                api("PUT",f"variants/{state[bc]['v']}.json",{"variant":{"id":state[bc]['v'],"price":f"{g:.2f}","inventory_policy":"continue" if fulfill else "deny"}})
                if loc_id and state[bc].get("i"):
                    api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":state[bc]["i"],"available":max(int(fnum(r.get('BESTAND'))),0)})
                st["updated"]+=1
            except Exception as e: st["errors"]+=1; log("ERR",bc,str(e)[:120])
        log("\n=== Update ==="); [log(f"{k:9}: {v}") for k,v in st.items()]; return

    groups=group_rows(rows); log(f"{len(groups)} Artikel-Gruppen (SUCHNUMMER).")

    if args.mode=="retag":
        st={"retagged":0,"miss":0,"errors":0}
        for key,group in groups:
            pid=None
            for r in group:
                if r.get("EAN","").strip() in state: pid=state[r.get("EAN","").strip()]["p"]; break
            if not pid: st["miss"]+=1; continue
            ptype,tags=categories(group[0])
            if args.dry_run: st["retagged"]+=1; continue
            try: api("PUT",f"products/{pid}.json",{"product":{"id":pid,"product_type":ptype,"tags":", ".join(tags)}}); st["retagged"]+=1
            except Exception as e: st["errors"]+=1; log("ERR",key,str(e)[:120])
            if st["retagged"]%100==0: log(f"... {st['retagged']} getaggt")
            if args.limit and st["retagged"]>=args.limit: break
        log("\n=== Retag ==="); [log(f"{k:9}: {v}") for k,v in st.items()]; return

    # full
    st={"created":0,"updated":0,"variants":0,"noimg":0,"drop":0,"errors":0}
    done=0
    for key,group in groups:
        exists=any(r.get("EAN","").strip() in state for r in group)
        q=qualifying(group)
        if not q:
            st["drop"]+=1; continue
        if args.dry_run:
            st["created" if not exists else "updated"]+=1; st["variants"]+=len(q); continue
        try:
            if exists:
                update_grouped(group,state,loc_id,True); st["updated"]+=1
            else:
                res,ids=create_grouped(group,loc_id)
                if res=="created":
                    for bc,vm in ids["variants"].items(): state[bc]={"p":ids["p"],"v":vm["v"],"i":vm["i"]}
                    st["created"]+=1; st["variants"]+=len(ids["variants"])
                else: st[res]+=1
        except Exception as e: st["errors"]+=1; log("ERR",key,str(e)[:140])
        done+=1
        if done%50==0: save_state(state); log(f"... {done}/{len(groups)} Gruppen (created {st['created']}, updated {st['updated']}, err {st['errors']})")
        if args.limit and (st["created"]+st["updated"])>=args.limit: break
    save_state(state)
    log("\n=== Full ==="); [log(f"{k:9}: {v}") for k,v in st.items()]

if __name__=="__main__":
    main()
