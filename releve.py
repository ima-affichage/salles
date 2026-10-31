#!/usr/bin/env python3
"""Relevé du planning des salles sur Celcat -> data.json

Usage :
    python releve.py              # planning du jour (heure de Paris)
    python releve.py 2026-11-02   # planning d'une date donnée (pour les essais)

Fichiers :
    Noms_CELCAT.csv  (dans le même dossier) : identifiant Celcat de la salle, nom affiché
    data.json        (créé dans le même dossier) : lu par la page d'affichage
"""
import csv
import html
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://celcat.u-bordeaux.fr/calendar"
CSV_SALLES = Path(__file__).with_name("Noms_CELCAT.csv")
SORTIE = Path(__file__).with_name("data.json")
TENTATIVES = 3          # nombre d'essais par requête avant d'abandonner
PAUSE = 0.1             # pause (secondes) entre deux requêtes de détail

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://celcat.u-bordeaux.fr",
    "Referer": BASE + "/",
    "User-Agent": "affichage-salles (IMA, Universite de Bordeaux)",
})


def lire_salles():
    """Retourne {identifiant Celcat: nom affiché}."""
    salles = {}
    with open(CSV_SALLES, encoding="utf-8-sig", newline="") as f:
        for ligne in csv.reader(f):
            if len(ligne) >= 2 and ligne[0].strip():
                salles[ligne[0].strip()] = ligne[1].strip()
    return salles


def post_json(url, data):
    """POST avec nouvelles tentatives ; lève une erreur si tout échoue."""
    derniere = None
    for essai in range(1, TENTATIVES + 1):
        try:
            r = session.post(url, data=data, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            derniere = e
            time.sleep(2 * essai)
    raise RuntimeError(f"{url} : {derniere}")


def evenements_du_jour(salles, jour):
    data = {
        "start": jour,
        "end": jour,
        "resType": "102",
        "calView": "agendaDay",
        "colourScheme": "3",
        "federationIds[]": list(salles),
    }
    return post_json(BASE + "/Home/GetCalendarData", data)


def nettoyer(texte):
    return html.unescape(texte or "").strip()


def details(event_id, salles):
    """Lit groupes, modules, enseignants et salles d'un événement.
    Seuls ces quatre types d'éléments sont gardés : rien d'autre n'est conservé."""
    r = post_json(BASE + "/Home/GetSideBarEvent", {"eventId": event_id})
    groupes, modules, profs, lieux = [], [], [], []
    for el in r.get("elements", []):
        contenu = nettoyer(el.get("content"))
        if not contenu:
            continue
        t = el.get("entityType")
        if t == 103:
            groupes.append(contenu)
        elif t == 100:
            modules.append(contenu)
        elif t == 101:
            profs.append(contenu)
        elif t == 102:
            lieux.append(salles.get(contenu, contenu))
    return groupes, modules, profs, lieux


def main():
    if len(sys.argv) > 1:
        jour = sys.argv[1]
    else:
        jour = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")

    salles = lire_salles()
    print(f"{len(salles)} salles, date {jour}")

    bruts = evenements_du_jour(salles, jour)
    print(f"{len(bruts)} événements reçus")

    vus, lignes, erreurs = set(), [], 0
    for evt in bruts:
        if evt["id"] in vus:
            continue
        vus.add(evt["id"])
        if evt.get("allDay"):
            print("Ignoré (événement sur la journée entière) :", evt["id"])
            continue
        try:
            groupes, modules_detail, profs, lieux = details(evt["id"], salles)
        except Exception as e:
            erreurs += 1
            print("ERREUR sur l'événement", evt["id"], ":", e)
            continue

        # UE : le champ « modules » de Celcat s'il est renseigné,
        # sinon les modules du détail, sinon le type d'événement (ex. « Conférence »)
        modules = [nettoyer(m) for m in (evt.get("modules") or []) if nettoyer(m)]
        ue = modules or modules_detail or [nettoyer(evt.get("eventCategory"))]
        ue = [u for u in ue if u]

        lignes.append([evt["start"][11:16], evt["end"][11:16], groupes, ue, profs, lieux])
        time.sleep(PAUSE)

    if erreurs:
        # On n'écrit rien : la page affichera « Planning indisponible »
        # plutôt qu'un planning incomplet.
        print(f"{erreurs} événement(s) en erreur : data.json n'a PAS été modifié.")
        sys.exit(1)

    lignes.sort(key=lambda e: (e[0], e[1], e[5][0] if e[5] else ""))
    resultat = {
        "date": jour,
        "genere": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="seconds"),
        "evenements": lignes,
    }
    tmp = SORTIE.with_suffix(".tmp")
    tmp.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SORTIE)
    print(f"OK : {len(lignes)} lignes écrites dans {SORTIE.name}")


main()
