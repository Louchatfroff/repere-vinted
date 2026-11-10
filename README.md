# Repère — local Vinted workspace

General-purpose searches, SO-DIMM presets, editable filters, price history, favorites, estimated margins, JSON/CSV import/export, light/dark themes, density and list/grid preferences. Python 3.9+, no runtime package installation.

## Start

- macOS: double-click `Lancer Repere.command`.
- Windows: double-click `Lancer Repere.bat` (Python must be installed).
- Linux: `sh start.sh`.
- Terminal: `python3 radar.py --open`.

Open http://127.0.0.1:8765. Under **Données & connexion**, enter the hosted service URL and the shared activation key from the owner. Verify access, review profiles and start monitoring. Browser notifications require an open tab. The client must keep running to initiate searches.

## Hosted collection

The owner-only collector is a separate project (`../repere-service`). Deploy it first using its README. This client contains no direct Vinted collector and no fallback when the service denies access. Replacing the shared activation key stops new collections using the old key for every user. Existing local data remains available; this is not file destruction or a guarantee against independently rewritten software.

The client stores the shared activation key in `data/connection.json` (owner-only file permissions on Unix). It never returns the key through dashboard state or config exports. Do not share `data/`. Everyone uses the same key; rotating it disables access for all users until they enter the replacement key. This is an access gate, not copy protection: someone with the source code and key can inspect or modify their local copy.

## Distribution

Publish only this directory to a private repository. Do not publish the owner collector to the friends' repository. A publication helper is provided:

```
gh auth login
python3 publish_private.py antoinebrnl/repere-vinted
```

The helper requires reviewed source committed in Git and creates a private repository. Invite friends through GitHub Settings > Collaborators, then they can clone it and use the launchers above. No repository has been published by this environment.

## Settings and data

SO-DIMM DDR5 is the initial preset; new free searches can target any product. RAM filters apply only to RAM profiles. Each line is a separate search; extra keyword exclusions and URL-imported Vinted filter IDs are available. Initial scans are silent by default. Live items are associated with their originating searches; imported general items are matched against query words.

The €250 default resale assumption applies to 16 GB RAM totals only and is a user hypothesis, not market pricing. Shipping, fees and preparation costs must be reviewed. Margins exclude tax. No purchases or seller messages are sent.

## Validation

```
python3 -m unittest discover -s tests -v
node --check static/app.js
python3 radar.py --diagnose
```

Optional browser checks: install `requirements-dev.txt`, then `python3 -m playwright install chromium`. GitHub CI configuration covers Windows/macOS/Linux and browser checks. Local socket/browser execution is blocked in the current editing environment; real collection and visual browser validation remain outstanding. Passing offline tests does not establish that Vinted accepts the deployed collector.
