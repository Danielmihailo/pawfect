#!/usr/bin/env python3
"""
lovepawfect <- Zoodrop Sync
  --mode full   : zoodrop_utf8.csv  -> Produkte anlegen/aktualisieren (voll)
  --mode update : update_utf8.csv   -> nur Preis/Bestand/Status (schnell, taeglich)
  --dry-run     : nichts schreiben, nur Statistik/Sample
  --limit N     : max. N qualifizierte Artikel (Test)
  --local PATH  : lokale CSV statt Download

Preisregel:  Brutto = EK_netto * FACTOR, gedeckelt auf UVP-1, Mindestmarge (netto) >= MIN_MARGIN, Charm x,99.
Verfuegbar:  VERKAUFSSTATUS == "Im Verkauf" UND (BESTAND > 0 ODER LAGERSTATUS == "Fullfillment").
Bildlos:     standardmaessig uebersprungen (SKIP_NO_IMAGE=1).

Env: SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, PRICE_FACTOR, MIN_MARGIN, STATE_FILE, SKIP_NO_IMAGE
State: STATE_FILE (JSON) = { ean: {p: product_id, v: variant_id, i: inventory_item_id} }
"""
import os, sys, csv, io, json, time, argparse, urllib.request, urllib.error

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

# ---------- CSV / Mapping ----------
def fnum(s):
    s = (s or "").strip().replace(",", ".")
    try: return float(s)
    except ValueError: return 0.0

def load_rows(mode, local):
    if local:
        f = open(local, encoding="utf-8", errors="replace")
        return list(csv.DictReader(f, delimiter=";", quotechar='"'))
    url = FULL_URL if mode == "full" else UPDATE_URL
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
    charm = float(int(gross)) + 0.99           # x,99
    if charm > ek and (cap is None or charm <= cap) and abs(charm - gross) <= 1.0:
        gross = charm
    return round(gross, 2)

def categories(row):
    paths = [p.strip() for p in (row.get("KATEGORIE") or "").split("|") if p.strip()]
    ptype, tags = "", []
    for p in paths:
        levels = [l.strip() for l in p.split(">") if l.strip()]
        if levels and not ptype: ptype = levels[0]
        for l in levels:
            if l not in tags: tags.append(l)
    return ptype, tags

def images(row):
    return [row[f"BILD{i}"].strip() for i in range(1, 9) if row.get(f"BILD{i}", "").strip()]

def body_html(row):
    html = row.get("BESCHREIBUNG", "") or ""
    gp, gpe = fnum(row.get("GRUNDPREIS")), (row.get("GRUNDPREIS_EINHEIT") or "").strip()
    if gp > 0 and gpe:
        html += f'<p class="grundpreis"><small>Grundpreis: {gp:.2f} € / {gpe}</small></p>'
    return html

# ---------- Shopify REST (gedrosselt) ----------
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
            if e.code == 429:
                time.sleep(2.0 * (attempt + 1)); continue
            if e.code >= 500:
                time.sleep(1.5 * (attempt + 1)); continue
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

# ---------- Upsert ----------
def create_product(row, gross, loc_id):
    ptype, tags = categories(row)
    bestand = int(fnum(row.get("BESTAND")))
    fulfill = row.get("LAGERSTATUS", "").strip().lower() == "fullfillment"
    payload = {"product": {
        "title": row.get("TITEL", "").strip(),
        "body_html": body_html(row),
        "vendor": row.get("HERSTELLER", "").strip(),
        "product_type": ptype,
        "tags": ", ".join(tags),
        "status": "active",
        "images": [{"src": u} for u in images(row)],
        "variants": [{
            "price": f"{gross:.2f}",
            "sku": row.get("ARTIKELNUMMER", "").strip(),
            "barcode": row.get("EAN", "").strip(),
            "inventory_management": "shopify",
            "inventory_policy": "continue" if fulfill else "deny",
            "taxable": True,
        }],
    }}
    p = api("POST", "products.json", payload)["product"]
    v = p["variants"][0]
    if loc_id:
        api("POST", "inventory_levels/set.json",
            {"location_id": loc_id, "inventory_item_id": v["inventory_item_id"],
             "available": max(bestand, 0)})
    return {"p": p["id"], "v": v["id"], "i": v["inventory_item_id"]}

def update_product(ids, row, gross, loc_id, full):
    bestand = int(fnum(row.get("BESTAND")))
    fulfill = row.get("LAGERSTATUS", "").strip().lower() == "fullfillment"
    api("PUT", f"variants/{ids['v']}.json", {"variant": {
        "id": ids["v"], "price": f"{gross:.2f}",
        "inventory_policy": "continue" if fulfill else "deny"}})
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

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "update"], default="full")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--local", default="")
    args = ap.parse_args()

    rows = load_rows(args.mode, args.local)
    log(f"{len(rows)} Zeilen geladen.")
    state = load_state()
    loc_id = None if args.dry_run else get_location_id()
    if not args.dry_run: log("Location:", loc_id)

    st = {"avail": 0, "qualified": 0, "noimg": 0, "created": 0, "updated": 0,
          "deactivated": 0, "drop_unavail": 0, "drop_margin": 0, "errors": 0}
    seen = set()
    sample = []
    processed = 0

    for row in rows:
        ean = row.get("EAN", "").strip()
        if not is_available(row):
            st["drop_unavail"] += 1
            # nicht mehr verfuegbar, aber im Store -> deaktivieren (nur full)
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
        # Update-Feed hat keine Bilder -> Bildcheck nur im Full
        if args.mode == "full" and SKIP_NO_IMAGE and not images(row) and ean not in state:
            st["noimg"] += 1; continue
        st["qualified"] += 1
        seen.add(ean)

        if len(sample) < 5:
            ptype, tags = categories(row)
            sample.append({"ean": ean, "titel": row.get("TITEL"), "brutto": gross,
                           "typ": ptype, "bilder": len(images(row))})

        if args.dry_run:
            continue
        try:
            if ean in state:
                update_product(state[ean], row, gross, loc_id, args.mode == "full")
                st["updated"] += 1
            elif args.mode == "full":
                state[ean] = create_product(row, gross, loc_id)
                st["created"] += 1
            # im update-Modus unbekannte EANs ueberspringen (kommen beim naechsten Full rein)
        except Exception as e:
            st["errors"] += 1; log("ERR", ean, str(e)[:160])

        processed += 1
        if processed % 50 == 0:
            if not args.dry_run: save_state(state)
            log(f"... {processed} verarbeitet (created {st['created']}, updated {st['updated']}, err {st['errors']})")
        if args.limit and st["qualified"] >= args.limit:
            log("Limit erreicht."); break

    if not args.dry_run: save_state(state)
    log("\n=== Ergebnis ===")
    for k, v in st.items(): log(f"{k:13}: {v}")
    log("\n=== Sample ===")
    for s in sample: log(json.dumps(s, ensure_ascii=False))
    if args.dry_run: log("\n[DRY-RUN] Keine Schreibzugriffe.")

if __name__ == "__main__":
    main()
