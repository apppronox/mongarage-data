"""Pipeline IRVE : fichier consolidé data.gouv.fr → fichier compact pour l'app.

Usage :
    python build_irve.py --out public            # télécharge la dernière version
    python build_irve.py --csv local.csv --out public

Produit :
    public/irve/stations-<version>.json.gz   (format : lib/data/charging/irve_format.dart)
    public/irve/manifest.json                 (version, date, sha256, taille, nombre)

Règles de nettoyage : docs/irve_data.md. Bibliothèque standard uniquement.
"""

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import pathlib
import sys
import unicodedata
import urllib.request

DATASET_ID = "5448d3e0c751df01f85d0572"
DATASET_API = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_ID}/"
SCHEMA = "etalab/schema-irve-statique"
FORMAT_VERSION = 1

# Métropole + Corse, puis DROM (même définition que lib/domain/models/geo.dart).
ZONES = [
    (41.0, 51.5, -5.5, 10.0),
    (15.8, 16.6, -61.9, -60.9),
    (14.3, 14.95, -61.3, -60.7),
    (2.0, 6.0, -54.7, -51.5),
    (-21.5, -20.8, 55.1, 55.9),
    (-13.1, -12.6, 44.9, 45.4),
]

MASK_TYPE2, MASK_CCS, MASK_CHADEMO, MASK_EF, MASK_OTHER = 1, 2, 4, 8, 16
MERGE_DISTANCE_M = 30


# --- Téléchargement ----------------------------------------------------------

def http_get(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "MonGarage-irve-pipeline"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def pick_resource(meta):
    """Ressource CSV au schéma IRVE, « dernière version » en priorité, sinon la plus récente."""
    csvs = [
        r for r in meta.get("resources", [])
        if (r.get("format") or "").lower() == "csv" and ((r.get("schema") or {}).get("name") == SCHEMA)
    ]
    if not csvs:
        raise RuntimeError("aucune ressource CSV au schéma IRVE")
    latest = [r for r in csvs if (r.get("title") or "").lower().startswith("consolidation de la dernière version")]
    pool = latest or csvs
    return max(pool, key=lambda r: r.get("last_modified") or "")


# --- Normalisation -------------------------------------------------------------

def norm_text(s):
    """Minuscules sans accents ni caractères non ASCII (encodages cassés compris)."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if ord(c) < 128).lower().strip()


def tri_bool(v):
    s = (v or "").strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def power_kw(v):
    try:
        p = float((v or "").strip().replace(",", "."))
    except ValueError:
        return None
    if p > 1000:
        p /= 1000
    if p <= 0 or p > 1000 or not math.isfinite(p):
        return None
    return round(p, 1)


def access_code(v):
    n = norm_text(v)
    if "reserv" in n:
        return 2
    if "libre" in n:
        return 1
    return 0


def valid_coords(lat, lon):
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if (lat == 0 and lon == 0) or abs(lat - lon) < 1e-6:
        return False
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in ZONES)


def to_float(v):
    try:
        return float((v or "").strip().replace(",", "."))
    except ValueError:
        return None


def clean(v):
    v = (v or "").strip()
    return v or None


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


# --- Transformation ------------------------------------------------------------

def connector_mask(row):
    m = 0
    for col, bit in (
        ("prise_type_2", MASK_TYPE2),
        ("prise_type_combo_ccs", MASK_CCS),
        ("prise_type_chademo", MASK_CHADEMO),
        ("prise_type_ef", MASK_EF),
        ("prise_type_autre", MASK_OTHER),
    ):
        if tri_bool(row.get(col)):
            m |= bit
    return m


def dedupe_points(rows):
    """Un point de charge publié plusieurs fois : la ligne la plus récente gagne."""
    best = {}
    anonymous = []
    for row in rows:
        pdc = (row.get("id_pdc_itinerance") or "").strip()
        if not pdc or norm_text(pdc) in ("non concerne", "inconnu"):
            anonymous.append(row)
            continue
        cur = best.get(pdc)
        if cur is None or (row.get("last_modified") or "") > (cur.get("last_modified") or ""):
            best[pdc] = row
    return list(best.values()) + anonymous


def station_key(row, lat, lon):
    sid = (row.get("id_station_itinerance") or "").strip()
    if sid and norm_text(sid) not in ("non concerne", "inconnu"):
        return sid
    op = norm_text(row.get("nom_operateur") or row.get("nom_enseigne"))
    h = hashlib.sha1(f"{lat:.5f},{lon:.5f},{op}".encode()).hexdigest()[:12]
    return f"LOC{h}"


def update_date(row, today):
    d = (row.get("date_maj") or "").strip()[:10]
    if len(d) == 10 and d <= today:
        return d
    lm = (row.get("last_modified") or "").strip()[:10]
    return lm if len(lm) == 10 else None


def build_stations(rows, today=None):
    today = today or dt.date.today().isoformat()
    stats = {"rows": 0, "invalid_coords": 0, "duplicates": 0, "merged": 0}
    rows = list(rows)
    stats["rows"] = len(rows)
    unique = dedupe_points(rows)
    stats["duplicates"] = len(rows) - len(unique)

    stations = {}
    for row in unique:
        lat, lon = to_float(row.get("consolidated_latitude")), to_float(row.get("consolidated_longitude"))
        if not valid_coords(lat, lon):
            stats["invalid_coords"] += 1
            continue
        key = station_key(row, lat, lon)
        s = stations.get(key)
        if s is None:
            s = stations[key] = {
                "i": key,
                "n": clean(row.get("nom_station")) or clean(row.get("nom_enseigne")) or clean(row.get("nom_operateur")) or "Borne",
                "a": clean(row.get("adresse_station")) or "",
                "la": round(lat, 5),
                "lo": round(lon, 5),
                "op": clean(row.get("nom_operateur")),
                "en": clean(row.get("nom_enseigne")),
                "ct": clean(row.get("contact_operateur")),
                "tel": clean(row.get("telephone_operateur")),
                "p": [],
                "_g": [],
                "_cb": [],
                "ac": access_code(row.get("condition_acces")),
                "h": clean(row.get("horaires")),
                "pmr": None if "inconnu" in norm_text(row.get("accessibilite_pmr")) else clean(row.get("accessibilite_pmr")),
                "t": clean(row.get("tarification")),
                "u": update_date(row, today),
                "_op": norm_text(row.get("nom_operateur") or row.get("nom_enseigne")),
            }
        s["p"].append([power_kw(row.get("puissance_nominale")), connector_mask(row)])
        s["_g"].append(tri_bool(row.get("gratuit")))
        s["_cb"].append(tri_bool(row.get("paiement_cb")))
        u = update_date(row, today)
        if u and (s["u"] is None or u > s["u"]):
            s["u"] = u

    merged = merge_nearby(list(stations.values()))
    stats["merged"] = len(stations) - len(merged)

    out = []
    for s in merged:
        g, cb = s.pop("_g"), s.pop("_cb")
        s.pop("_op")
        # Vrai seulement si tous les points le disent ; faux si l'un le dément.
        s["g"] = _tri_all(g)
        s["cb"] = _tri_any(cb)
        out.append({k: v for k, v in s.items() if v is not None and v != ""})
    out.sort(key=lambda s: s["i"])
    return out, stats


def _tri_all(values):
    known = [v for v in values if v is not None]
    if not known:
        return None
    return 1 if all(known) else 0


def _tri_any(values):
    known = [v for v in values if v is not None]
    if not known:
        return None
    return 1 if any(known) else 0


def merge_nearby(stations):
    """Fusionne les « stations » du même opérateur à moins de 30 m (ids par point de charge)."""
    grid = {}
    kept = []
    for s in sorted(stations, key=lambda x: x["i"]):
        cell = (round(s["la"], 3), round(s["lo"], 3))
        target = None
        for dla in (-0.001, 0, 0.001):
            for dlo in (-0.001, 0, 0.001):
                for other in grid.get((round(cell[0] + dla, 3), round(cell[1] + dlo, 3)), []):
                    if other["_op"] == s["_op"] and haversine_m(other["la"], other["lo"], s["la"], s["lo"]) <= MERGE_DISTANCE_M:
                        target = other
                        break
                if target:
                    break
            if target:
                break
        if target:
            target["p"].extend(s["p"])
            target["_g"].extend(s["_g"])
            target["_cb"].extend(s["_cb"])
            if s["u"] and (target["u"] is None or s["u"] > target["u"]):
                target["u"] = s["u"]
        else:
            grid.setdefault(cell, []).append(s)
            kept.append(s)
    return kept


# --- Sortie --------------------------------------------------------------------

def write_output(stations, out_dir, source_date, source_url):
    payload = {
        "v": FORMAT_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_date": source_date,
        "stations": stations,
    }
    # Contenu déterministe (hors generated_at) : la version ne change que si les données changent.
    body = json.dumps(stations, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    version = hashlib.sha256(body).hexdigest()[:16]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    data = buf.getvalue()

    irve = pathlib.Path(out_dir) / "irve"
    irve.mkdir(parents=True, exist_ok=True)
    for old in irve.glob("stations-*.json.gz"):
        old.unlink()
    name = f"stations-{version}.json.gz"
    (irve / name).write_bytes(data)
    manifest = {
        "version": version,
        "format": FORMAT_VERSION,
        "generated_at": payload["generated_at"],
        "source_date": source_date,
        "source": source_url,
        "license": "Licence Ouverte v2.0 (Etalab) — Base nationale des IRVE, data.gouv.fr",
        "file": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "count": len(stations),
    }
    (irve / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="fichier consolidé local (sinon téléchargé)")
    ap.add_argument("--out", default="public")
    args = ap.parse_args(argv)

    if args.csv:
        text = pathlib.Path(args.csv).read_text(encoding="utf-8")
        source_date, source_url = dt.date.today().isoformat(), args.csv
    else:
        meta = json.loads(http_get(DATASET_API))
        res = pick_resource(meta)
        print("ressource :", res["title"], res["url"], file=sys.stderr)
        text = http_get(res["url"]).decode("utf-8")
        source_date, source_url = (res.get("last_modified") or "")[:10], res["url"]

    stations, stats = build_stations(csv.DictReader(io.StringIO(text, newline="")))
    if len(stations) < 1000 and not args.csv:
        raise SystemExit(f"seulement {len(stations)} stations : publication annulée")
    manifest = write_output(stations, args.out, source_date, source_url)
    print(json.dumps({**stats, **manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
