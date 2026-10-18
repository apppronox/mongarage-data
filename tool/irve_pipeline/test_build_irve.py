"""Tests du pipeline IRVE sur les lignes réelles de test/fixtures/irve/.

Lancer : python -m unittest discover -s tool/irve_pipeline
"""

import csv
import gzip
import json
import pathlib
import tempfile
import unittest

import build_irve as b

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "test" / "fixtures" / "irve"


def rows():
    with (FIXTURES / "sample.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def by_case(name):
    return next(r for r in rows() if r["_cas"] == name)


class Normalisation(unittest.TestCase):
    def test_booleens(self):
        for v in ("True", "true", "TRUE", "1"):
            self.assertIs(b.tri_bool(v), True)
        for v in ("False", "false", "FALSE", "0"):
            self.assertIs(b.tri_bool(v), False)
        self.assertIsNone(b.tri_bool(""))
        self.assertIsNone(b.tri_bool(None))

    def test_puissance(self):
        self.assertEqual(b.power_kw("22"), 22)
        self.assertEqual(b.power_kw("22000"), 22)  # en W
        self.assertEqual(b.power_kw("7,4"), 7.4)
        self.assertIsNone(b.power_kw("0"))
        self.assertIsNone(b.power_kw("abc"))
        self.assertIsNone(b.power_kw("5000000"))
        self.assertEqual(b.power_kw(by_case("puissance_en_watts")["puissance_nominale"]) <= 1000, True)

    def test_acces_encodage_casse(self):
        self.assertEqual(b.access_code("Accès libre"), 1)
        self.assertEqual(b.access_code("Accès réservé"), 2)
        self.assertEqual(b.access_code(by_case("encodage_casse")["condition_acces"]), 1)
        self.assertEqual(b.access_code(""), 0)

    def test_coordonnees(self):
        self.assertTrue(b.valid_coords(48.85, 2.35))
        self.assertTrue(b.valid_coords(-20.88, 55.45))
        self.assertFalse(b.valid_coords(0, 0))
        self.assertFalse(b.valid_coords(48, 48))
        self.assertFalse(b.valid_coords(40.4, -3.7))


class Construction(unittest.TestCase):
    def setUp(self):
        self.stations, self.stats = b.build_stations(rows(), today="2026-10-18")
        self.ids = {s["i"]: s for s in self.stations}

    def test_coordonnees_invalides_rejetees(self):
        self.assertGreaterEqual(self.stats["invalid_coords"], 2)
        for s in self.stations:
            self.assertTrue(b.valid_coords(s["la"], s["lo"]))

    def test_outre_mer_conserve(self):
        self.assertTrue(any(not (41 <= s["la"] <= 51.5) for s in self.stations))

    def test_regroupement_multi_pdc(self):
        multi = [r for r in rows() if r["_cas"] == "station_multi_pdc"]
        s = self.ids[multi[0]["id_station_itinerance"]]
        self.assertGreaterEqual(len(s["p"]), len(multi))

    def test_station_non_concernee_a_un_id(self):
        self.assertTrue(any(i.startswith("LOC") for i in self.ids) or self.stats["merged"] > 0)

    def test_champs_compacts(self):
        for s in self.stations:
            self.assertIn("i", s)
            self.assertIn("n", s)
            self.assertIsInstance(s["p"], list)
            for power, mask in s["p"]:
                self.assertTrue(power is None or 0 < power <= 1000)
                self.assertIsInstance(mask, int)
            self.assertNotIn("_op", s)

    def test_dates_futures_ignorees(self):
        for s in self.stations:
            if "u" in s:
                self.assertLessEqual(s["u"], "2026-10-18")

    def test_doublons_de_pdc(self):
        dup = [r for r in rows() if r["_cas"] == "pdc_publie_deux_fois"][0]
        twice = [dup, dict(dup, last_modified="2000-01-01", puissance_nominale="3")]
        stations, stats = b.build_stations(twice, today="2026-10-18")
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(len(stations[0]["p"]), 1)
        self.assertNotEqual(stations[0]["p"][0][0], 3)


class Fusion(unittest.TestCase):
    def test_meme_operateur_a_moins_de_30m(self):
        base = by_case("ccs_rapide")
        a = dict(base, id_station_itinerance="FRAAA1", id_pdc_itinerance="FRAAA1P1")
        lat = float(base["consolidated_latitude"])
        c = dict(base, id_station_itinerance="FRAAA2", id_pdc_itinerance="FRAAA2P1", consolidated_latitude=str(lat + 0.0001))
        far = dict(base, id_station_itinerance="FRAAA3", id_pdc_itinerance="FRAAA3P1", consolidated_latitude=str(lat + 0.01))
        stations, stats = b.build_stations([a, c, far], today="2026-10-18")
        self.assertEqual(len(stations), 2)
        self.assertEqual(stats["merged"], 1)


class Sortie(unittest.TestCase):
    def test_manifest_et_fichier(self):
        stations, _ = b.build_stations(rows(), today="2026-10-18")
        with tempfile.TemporaryDirectory() as d:
            m = b.write_output(stations, d, "2026-10-18", "test")
            raw = (pathlib.Path(d) / "irve" / m["file"]).read_bytes()
            payload = json.loads(gzip.decompress(raw))
            self.assertEqual(payload["v"], b.FORMAT_VERSION)
            self.assertEqual(len(payload["stations"]), m["count"])
            import hashlib

            self.assertEqual(hashlib.sha256(raw).hexdigest(), m["sha256"])
            # Même contenu → même version.
            m2 = b.write_output(stations, d, "2026-10-18", "test")
            self.assertEqual(m["version"], m2["version"])

    def test_choix_de_la_ressource(self):
        meta = json.loads((FIXTURES / "dataset.json").read_text(encoding="utf-8"))
        res = b.pick_resource(meta)
        self.assertEqual(res["format"], "csv")
        self.assertTrue(res["title"].lower().startswith("consolidation de la dernière version"))


if __name__ == "__main__":
    unittest.main()
