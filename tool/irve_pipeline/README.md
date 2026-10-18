# Pipeline des données publiques

Construit chaque semaine le fichier des bornes de recharge et publie, sur GitHub Pages :

```
https://<compte>.github.io/<dépôt>/
  irve/manifest.json              version, date source, sha256, taille, nombre de stations
  irve/stations-<version>.json.gz ~1,9 Mo, ~35 000 stations
  other_apps.json                 section « Nos autres apps » de l'app
  privacy.html                    politique de confidentialité (phase 13)
  index.html
```

L'app lit `irve/manifest.json` au plus une fois par jour. Elle ne télécharge le fichier que si la version a changé : tout de suite s'il n'y a encore aucune donnée, sinon en Wi-Fi.

## Mise en place (à faire une fois)

GitHub Pages est gratuit pour les dépôts **publics**. Deux options :

**A. Ce dépôt est public**
1. Pousser le dépôt sur GitHub.
2. Settings → Pages → Source : **GitHub Actions**.
3. Onglet Actions → « Données publiques (bornes IRVE) » → **Run workflow**.

**B. Dépôt séparé, public (recommandé si le code de l'app reste privé)**
1. Créer un dépôt public, par exemple `mongarage-data`.
2. Y copier `tool/irve_pipeline/`, `tool/site/`, `test/fixtures/irve/` (pour les tests) et `.github/workflows/data_pipeline.yml`, en gardant les mêmes chemins.
3. Settings → Pages → Source : **GitHub Actions**, puis lancer le workflow.

Ensuite, construire l'app avec l'URL publiée :

```
flutter run --dart-define=DATA_BASE_URL=https://<compte>.github.io/<dépôt>
```

## En local

```
python -m unittest discover -s tool/irve_pipeline
python tool/irve_pipeline/build_irve.py --out public          # télécharge ~110 Mo
python tool/irve_pipeline/build_irve.py --csv fichier.csv --out public
```

Garde-fou : si le résultat contient moins de 1 000 stations, la publication est annulée (source cassée), et l'ancienne version reste en ligne.

## Plan B prix carburants

Si l'API Opendatasoft disparaît, un job peut être ajouté à ce workflow pour publier un instantané des prix par département (docs/fuel_api.md, « Plan B »). Le client de l'app est isolé dans `lib/data/fuel/` et peut changer de source.
