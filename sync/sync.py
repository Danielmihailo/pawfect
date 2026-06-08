#!/usr/bin/env python3
"""
lovepawfect <- Zoodrop Sync
  --mode full        : zoodrop_utf8.csv -> Produkte anlegen/aktualisieren (voll, inkl. Bilder)
  --mode update      : update_utf8.csv  -> nur Preis/Bestand/Status (schnell, taeglich)
  --mode retag       : zoodrop_utf8.csv -> nur normalisierte Tags + Produkttyp auf vorhandene Produkte
  --mode collections : Smart Collections (Welten + Kategorien) idempotent anlegen/aktualisieren
  --dry-run / --limit N / --local PATH

Preisregel:  Brutto = EK_netto * FACTOR, gedeckelt auf UVP-1, Marge(netto) >= MIN_MARGIN, Charm x,99.
Verfuegbar:  "Im Verkauf" UND (Bestand>0 ODER Lagerstatus "Fullfillment"). Bildlose -> uebersprungen.
Tags:        Tier (Hund/Katze/...) + Bedarf (Futter/Spielzeug/Schlafen/...) + Marke (Hersteller).
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

# ---------- Taxonomie / Tagging ----------
TIER_MAP = {
    "Hundewelt": "Hund", "Katzenwelt": "Katze", "Kleintierwelt": "Kleintier",
    "Vogelwelt": "Vogel", "Aquarienwelt": "Aquaristik", "Terrarienwelt": "Reptil",
    "Gartenwelt": "Garten",
}
# (keyword in lowercased Kategorie-Pfad) -> Bedarfs-Tags
CAT_RULES = [
    ("trockenfutter", ["Trockenfutter", "Futter"]),
    ("nassfutter", ["Nassfutter", "Futter"]),
    ("ergänz", ["Futterergänzung", "Futter"]),
    ("erganz", ["Futterergänzung", "Futter"]),
    ("leckerl", ["Snacks"]), ("kausnack", ["Snacks"]), ("snack", ["Snacks"]),
    ("futter", ["Futter"]),
    ("spielzeug", ["Spielzeug"]),
    ("kratz", ["Kratzbäume"]),
    ("bett", ["Schlafen"]), ("körb", ["Schlafen"]), ("korb", ["Schlafen"]),
    ("kissen", ["Schlafen"]), ("höhle", ["Schlafen"]), ("hoehle", ["Schlafen"]),
    ("decke", ["Schlafen"]), ("schlaf", ["Schlafen"]),
    ("leine", ["Leinen"]),
    ("halsband", ["Halsbänder"]), ("halsbänder", ["Halsbänder"]),
    ("geschirr", ["Geschirre"]),
    ("transport", ["Transport"]), ("reise", ["Transport"]), ("auto", ["Transport"]),
    ("näpfe", ["Näpfe"]), ("napf", ["Näpfe"]), ("tränke", ["Näpfe"]),
    ("traenke", ["Näpfe"]), ("brunnen", ["Näpfe"]),
    ("fellpflege", ["Pflege"]), ("pflege", ["Pflege"]), ("hygiene", ["Pflege"]),
    ("zahn", ["Pflege"]), ("kralle", ["Pflege"]), ("bürste", ["Pflege"]), ("shampoo", ["Pflege"]),
    ("gesundheit", ["Gesundheit"]), ("apotheke", ["Gesundheit"]), ("wurm", ["Gesundheit"]),
    ("floh", ["Gesundheit"]), ("zecke", ["Gesundheit"]), ("vitamin", ["Gesundheit"]),
    ("toilette", ["Katzenklo"]), ("katzenklo", ["Katzenklo"]),
    ("erziehung", ["Erziehung"]), ("training", ["Erziehung"]), ("clicker", ["Erziehung"]),
    ("bekleidung", ["Bekleidung"]), ("mantel", ["Bekleidung"]), ("pullover", ["Bekleidung"]),
    ("käfig", ["Käfige"]), ("kaefig", ["Käfige"]), ("gehege", ["Käfige"]),
    ("voliere", ["Käfige"]), ("stall", ["Käfige"]), ("nest", ["Käfige"]),
    ("filter", ["Technik"]), ("pumpe", ["Technik"]), ("heizer", ["Technik"]),
    ("beleuchtung", ["Technik"]), ("co2", ["Technik"]), ("technik", ["Technik"]),
    ("wasserpflege", ["Wasserpflege"]), ("wasseraufbereit", ["Wasserpflege"]), ("wassertest", ["Wasserpflege"]),
    ("heu", ["Einstreu"]), ("einstreu", ["Einstreu"]),
    ("ausstattung", ["Zubehör"]), ("zubehör", ["Zubehör"]), ("zubehoer", ["Zubehör"]),
]

# Smart-Collections: Welten + Kategorien (Tag-Regeln, AND)
WORLD_HANDLE = {"Hund": "hundewelt", "Katze": "katzenwelt", "Kleintier": "kleintierwelt",
                "Vogel": "vogelwelt", "Aquaristik": "aquarienwelt", "Reptil": "terrarienwelt",
                "Garten": "gartenwelt"}
WORLD_SLUG = {"Hund": "hund", "Katze": "katze", "Kleintier": "kleintier", "Vogel": "vogel",
              "Aquaristik": "aquaristik", "Reptil": "reptil", "Garten": "garten"}
WORLD_TITLE = {"Hund": "Hundewelt", "Katze": "Katzenwelt", "Kleintier": "Kleintierwelt",
               "Vogel": "Vogelwelt", "Aquaristik": "Aquaristik", "Reptil": "Terraristik",
               "Garten": "Garten & Outdoor"}
CAT_HANDLE = {"Futter": "futter", "Trockenfutter": "trockenfutter", "Nassfutter": "nassfutter",
              "Snacks": "snacks", "Spielzeug": "spielzeug", "Leinen": "leinen",
              "Halsbänder": "halsbaender", "Geschirre": "geschirre", "Schlafen": "schlafen",
              "Näpfe": "naepfe", "Pflege": "pflege", "Transport": "transport", "Erziehung": "erziehung",
              "Bekleidung": "bekleidung", "Gesundheit": "gesundheit", "Kratzbäume": "kratzbaeume",
              "Katzenklo": "katzenklo", "Einstreu": "einstreu", "Käfige": "kaefige",
              "Zubehör": "zubehoer", "Technik": "technik", "Wasserpflege": "wasserpflege"}
WORLD_CATS = {
    "Hund": ["Futter", "Trockenfutter", "Nassfutter", "Snacks", "Spielzeug", "Leinen", "Halsbänder",
             "Geschirre", "Schlafen", "Näpfe", "Pflege", "Transport", "Erziehung", "Bekleidung", "Gesundheit"],
    "Katze": ["Futter", "Trockenfutter", "Nassfutter", "Snacks", "Spielzeug", "Kratzbäume", "Schlafen",
              "Katzenklo", "Näpfe", "Pflege", "Transport", "Geschirre", "Gesundheit"],
    "Kleintier": ["Futter", "Einstreu", "Käfige", "Zubehör", "Transport", "Pflege"],
    "Vogel": ["Futter", "Käfige", "Zubehör"],
    "Aquaristik": ["Futter", "Wasserpflege", "Technik", "Zubehör"],
    "Reptil": ["Zubehör", "Technik"],
    "Garten": [],
}

# ---------- CSV / Mapping ----------
def fnum(s):
    s = (s or "").strip().replace(",", ".")
    try: return float(s)
    except ValueError: return 0.0

def load_rows(mode, local):
    if local:
        return list(csv.DictReader(open(local, encoding="utf-8", errors="replace"), delimiter=";", quotechar='"'))
    url = FULL_URL if mode == "update" and False else (UPDATE_URL if mode == "update" else FULL_URL)
    log("Lade Feed:", url)
    req = urllib.request.Request(url, headers={"User-Agent": "lovepawfect-sync/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read().decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(data), delimiter=";", quotechar='"'))

def is_available(row):
    if row.get("VERKAUFSSTATUS", "").strip() != "Im Verkauf":
        return False
    return fnum(row.get("BESTAND")) > 0 or row.get("LAGERSTATUS", "").strip().lower() == "fullfillment"

def compute_gross(ek, uvp, vat):
    if ek <= 0: return None
    gross = ek * FACTOR
    cap = uvp - 1.0 if uvp and uvp > 0 else None
    if cap is not None: gross = min(gross, cap)
    if gross <= 0: return None
    if (gross / (1.0 + vat / 100.0)) - ek < MIN_MARGIN: return None
    charm = float(int(gross)) + 0.99
    if charm > ek and (cap is None or charm <= cap) and abs(charm - gross) <= 1.0:
        gross = charm
    return round(gross, 2)

def categories(row):
    """-> (product_type=Welt, tags=[Tier..., Bedarf..., Marke])"""
    raw = row.get("KATEGORIE", "") or ""
    low = raw.lower()
    paths = [p for p in raw.split("|") if p.strip()]
    tiers, ptype = [], ""
    for p in paths:
        levels = [l.strip() for l in p.split(">") if l.strip()]
        if not levels: continue
        if not ptype: ptype = levels[0]
        t = TIER_MAP.get(levels[0])
        if t and t not in tiers: tiers.append(t)
    cats = []
    for kw, ts in CAT_RULES:
        if kw in low:
            for t in ts:
                if t not in cats: cats.append(t)
    if "streu" in low:
        if "Katze" in tiers: cats.append("Katzenklo")
        else: cats.append("Einstreu")
    tags = []
    for t in tiers + cats:
        if t not in tags: tags.append(t)
    vendor = row.get("HERSTELLER", "").strip()
    if vendor and vendor not in tags: tags.append(vendor)
    return ptype, tags

def images(row):
    return [row[f"BILD{i}"].strip() for i in range(1, 9) if row.get(f"BILD{i}", "").strip()]

def body_html(row):
    html = row.get("BESCHREIBUNG", "") or ""
    gp, gpe = fnum(row.get("GRUNDPREIS")), (row.get("GRUNDPREIS_EINHEIT") or "").strip()
    if gp > 0 and gpe:
        html += f'<p class="grundpreis"><small>Grundpreis: {gp:.2f} € / {gpe}</small></p>'
    return html

def fetch_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (lovepawfect-sync)"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return base64.b64encode(r.read()).decode()
    except Exception:
        return None

# ---------- Shopify REST ----------
def api(method, path, body=None):
    url = f"https://{SHOP}/admin/api/{APIV}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                used, cap = (int(x) for x in r.headers.get("X-Shopify-Shop-Api-Call-Limit", "0/40").split("/"))
                if used > cap * 0.7: time.sleep(0.6)
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2.0 * (attempt + 1)); continue
            if e.code >= 500: time.sleep(1.5 * (attempt + 1)); continue
            log("HTTP", e.code, method, path, e.read().decode()[:300]); raise
    raise RuntimeError(f"API failed: {method} {path}")

def get_location_id():
    locs = api("GET", "locations.json").get("locations", [])
    for l in locs:
        if l.get("active"): return l["id"]
    return locs[0]["id"] if locs else None

def load_state():
    try: return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception: return {}

def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)

def build_state_from_store(state):
    since, total = 0, 0
    while True:
        data = api("GET", f"products.json?limit=250&since_id={since}&fields=id,variants")
        prods = data.get("products", [])
        if not prods: break
        for p in prods:
            since = max(since, p["id"])
            for v in p.get("variants", []):
                bc = (v.get("barcode") or "").strip()
                if bc:
                    state.setdefault(bc, {"p": p["id"], "v": v["id"], "i": v.get("inventory_item_id")})
            total += 1
        log(f"  ...{total} vorhandene Produkte gelesen")
    log(f"State aus Store: {total} Produkte, {len(state)} Barcodes")

# ---------- Upsert ----------
def create_product(row, gross, loc_id):
    ptype, tags = categories(row)
    bestand = int(fnum(row.get("BESTAND")))
    fulfill = row.get("LAGERSTATUS", "").strip().lower() == "fullfillment"
    art = ((row.get("ARTIKELNUMMER", "") or "img").strip().replace(" ", "-")) or "img"
    imgs = []
    for idx, u in enumerate(images(row)[:6]):
        b64 = fetch_image(u)
        if b64: imgs.append({"attachment": b64, "filename": f"{art}-{idx+1}.jpg"})
    if SKIP_NO_IMAGE and not imgs: return None
    payload = {"product": {
        "title": row.get("TITEL", "").strip(), "body_html": body_html(row),
        "vendor": row.get("HERSTELLER", "").strip(), "product_type": ptype,
        "tags": ", ".join(tags), "status": "active", "images": imgs,
        "variants": [{"price": f"{gross:.2f}", "sku": row.get("ARTIKELNUMMER", "").strip(),
                      "barcode": row.get("EAN", "").strip(), "inventory_management": "shopify",
                      "inventory_policy": "continue" if fulfill else "deny", "taxable": True}],
    }}
    p = api("POST", "products.json", payload)["product"]
    v = p["variants"][0]
    if loc_id:
        api("POST", "inventory_levels/set.json",
            {"location_id": loc_id, "inventory_item_id": v["inventory_item_id"], "available": max(bestand, 0)})
    return {"p": p["id"], "v": v["id"], "i": v["inventory_item_id"]}

def update_product(ids, row, gross, loc_id, full):
    bestand = int(fnum(row.get("BESTAND")))
    fulfill = row.get("LAGERSTATUS", "").strip().lower() == "fullfillment"
    api("PUT", f"variants/{ids['v']}.json", {"variant": {
        "id": ids["v"], "price": f"{gross:.2f}", "inventory_policy": "continue" if fulfill else "deny"}})
    if loc_id and ids.get("i"):
        api("POST", "inventory_levels/set.json",
            {"location_id": loc_id, "inventory_item_id": ids["i"], "available": max(bestand, 0)})
    if full:
        ptype, tags = categories(row)
        api("PUT", f"products/{ids['p']}.json", {"product": {
            "id": ids["p"], "status": "active", "product_type": ptype,
            "tags": ", ".join(tags), "title": row.get("TITEL", "").strip()}})

def deactivate(ids):
    api("PUT", f"products/{ids['p']}.json", {"product": {"id": ids["p"], "status": "draft"}})

# ---------- Collections ----------
def find_collection(handle):
    sc = api("GET", f"smart_collections.json?handle={handle}").get("smart_collections", [])
    return sc[0] if sc else None

def upsert_collection(handle, title, tag_conditions):
    rules = [{"column": "tag", "relation": "equals", "condition": c} for c in tag_conditions]
    body = {"smart_collection": {"title": title, "handle": handle, "disjunctive": False,
                                 "rules": rules, "published": True}}
    existing = find_collection(handle)
    if existing:
        body["smart_collection"]["id"] = existing["id"]
        api("PUT", f"smart_collections/{existing['id']}.json", body)
        return "updated"
    api("POST", "smart_collections.json", body)
    return "created"

def make_collections():
    n = {"created": 0, "updated": 0}
    for world, slug in WORLD_SLUG.items():
        r = upsert_collection(WORLD_HANDLE[world], WORLD_TITLE[world], [world])
        n[r] += 1; log(f"Welt {WORLD_HANDLE[world]} [{world}] -> {r}")
        for cat in WORLD_CATS.get(world, []):
            handle = f"{slug}-{CAT_HANDLE[cat]}"
            r = upsert_collection(handle, f"{world} · {cat}", [world, cat])
            n[r] += 1; log(f"  {handle} [{world}+{cat}] -> {r}")
    log(f"\nCollections: {n['created']} neu, {n['updated']} aktualisiert.")

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "update", "retag", "collections"], default="full")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--local", default="")
    args = ap.parse_args()

    if args.mode == "collections":
        if args.dry_run: log("[DRY-RUN] Collections-Plan:");
        if args.dry_run:
            for world, slug in WORLD_SLUG.items():
                log(WORLD_HANDLE[world], "->", world)
                for cat in WORLD_CATS.get(world, []): log(f"  {slug}-{CAT_HANDLE[cat]} -> {world}+{cat}")
            return
        make_collections(); return

    rows = load_rows(args.mode, args.local)
    log(f"{len(rows)} Zeilen geladen.")
    state = load_state()
    loc_id = None if args.dry_run else get_location_id()
    if not args.dry_run:
        log("Location:", loc_id)
        build_state_from_store(state)

    # ---- retag: nur Tags/Produkttyp auf vorhandene Produkte ----
    if args.mode == "retag":
        st = {"retagged": 0, "skip_notinstore": 0, "errors": 0}
        for row in rows:
            ean = row.get("EAN", "").strip()
            if ean not in state:
                st["skip_notinstore"] += 1; continue
            ptype, tags = categories(row)
            if args.dry_run: st["retagged"] += 1; continue
            try:
                api("PUT", f"products/{state[ean]['p']}.json",
                    {"product": {"id": state[ean]["p"], "product_type": ptype, "tags": ", ".join(tags)}})
                st["retagged"] += 1
            except Exception as e:
                st["errors"] += 1; log("ERR", ean, str(e)[:140])
            if st["retagged"] % 100 == 0: log(f"... {st['retagged']} neu getaggt")
            if args.limit and st["retagged"] >= args.limit: break
        log("\n=== Retag ==="); [log(f"{k:16}: {v}") for k, v in st.items()]
        return

    # ---- full / update ----
    st = {"avail": 0, "qualified": 0, "noimg": 0, "created": 0, "updated": 0,
          "deactivated": 0, "drop_unavail": 0, "drop_margin": 0, "errors": 0}
    processed = 0
    for row in rows:
        ean = row.get("EAN", "").strip()
        if not is_available(row):
            st["drop_unavail"] += 1
            if args.mode == "full" and ean in state and not args.dry_run:
                try: deactivate(state[ean]); st["deactivated"] += 1
                except Exception: st["errors"] += 1
            continue
        st["avail"] += 1
        ek, uvp, vat = fnum(row.get("VERKAUFSPREIS")), fnum(row.get("UVP")), fnum(row.get("MWST") or "19")
        gross = compute_gross(ek, uvp, vat)
        if gross is None:
            st["drop_margin"] += 1
            if args.mode == "full" and ean in state and not args.dry_run:
                try: deactivate(state[ean]); st["deactivated"] += 1
                except Exception: st["errors"] += 1
            continue
        if args.mode == "full" and SKIP_NO_IMAGE and not images(row) and ean not in state:
            st["noimg"] += 1; continue
        st["qualified"] += 1
        if args.dry_run: continue
        try:
            if ean in state:
                update_product(state[ean], row, gross, loc_id, args.mode == "full")
                st["updated"] += 1
            elif args.mode == "full":
                ids = create_product(row, gross, loc_id)
                if ids is None: st["noimg"] += 1
                else: state[ean] = ids; st["created"] += 1
        except Exception as e:
            st["errors"] += 1; log("ERR", ean, str(e)[:140])
        processed += 1
        if processed % 50 == 0:
            save_state(state)
            log(f"... {processed} verarbeitet (created {st['created']}, updated {st['updated']}, err {st['errors']})")
        if args.limit and st["qualified"] >= args.limit: break

    save_state(state)
    log("\n=== Ergebnis ==="); [log(f"{k:13}: {v}") for k, v in st.items()]

if __name__ == "__main__":
    main()
