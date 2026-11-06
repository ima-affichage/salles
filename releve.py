#!/usr/bin/env python3
"""Relevé CELCAT : emploi du temps et salles libres en autonomie -> data.json.

Usage :
    python releve.py              # planning du jour, heure de Paris
    python releve.py 2026-12-01   # date de test

Entrée  : Noms_CELCAT.csv, trois colonnes sans en-tête
          identifiant CELCAT, nom affiché, autonomie (0 ou 1).
Sortie  : data.json, avec les champs historiques date/genere/evenements et
          le nouveau champ disponibilites.

Les horaires de deux demi-journées se modifient avec PLAGES_AUTONOMIE.
"""
import csv
import html
import json
import re
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://celcat.u-bordeaux.fr/calendar"
CSV_SALLES = Path(__file__).with_name("Noms_CELCAT.csv")
SORTIE = Path(__file__).with_name("data.json")
FUSEAU = ZoneInfo("Europe/Paris")
TENTATIVES = 3
PAUSE = 0.1

# Les limites sont incluses au début et exclues à la fin :
# un cours finissant à 08:00 ne bloque pas le matin ; un cours débutant
# à 12:00 ne bloque pas le matin non plus.
PLAGES_AUTONOMIE = {
    "matin": ("08:00", "12:00"),
    "apres_midi": ("13:00", "18:00"),
}

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://celcat.u-bordeaux.fr",
    "Referer": BASE + "/",
    "User-Agent": "affichage-salles (IMA, Universite de Bordeaux)",
})


def lire_salles():
    """Retourne (code Celcat -> nom affiché, ensemble des codes autorisés)."""
    salles = {}
    autonomie = set()
    with CSV_SALLES.open(encoding="utf-8-sig", newline="") as fichier:
        for numero, ligne in enumerate(csv.reader(fichier), start=1):
            if not ligne or all(not champ.strip() for champ in ligne):
                continue
            if len(ligne) != 3:
                raise ValueError(f"CSV ligne {numero} : 3 colonnes attendues, {len(ligne)} reçues")
            identifiant, nom, indicateur = (v.strip() for v in ligne)
            if not identifiant or not nom or indicateur not in {"0", "1"}:
                raise ValueError(f"CSV ligne {numero} : identifiant, nom ou indicateur 0/1 invalide")
            if identifiant in salles:
                raise ValueError(f"CSV ligne {numero} : salle dupliquée {identifiant}")
            salles[identifiant] = nom
            if indicateur == "1":
                autonomie.add(identifiant)
    if not salles:
        raise ValueError("Le CSV ne contient aucune salle")
    # Les libellés utilisés dans le JSON doivent être sans ambiguïté.
    noms_autonomie = [salles[identifiant] for identifiant in autonomie]
    if len(noms_autonomie) != len(set(noms_autonomie)):
        raise ValueError("Des salles autorisées en autonomie ont le même nom d'affichage")
    return salles, autonomie


def post_json(url, donnees):
    """POST avec trois essais, suivi d'une erreur explicite si nécessaire."""
    derniere = None
    for essai in range(1, TENTATIVES + 1):
        try:
            reponse = session.post(url, data=donnees, timeout=30)
            reponse.raise_for_status()
            return reponse.json()
        except (requests.RequestException, ValueError) as erreur:
            derniere = erreur
            if essai < TENTATIVES:
                time.sleep(2 * essai)
    raise RuntimeError(f"{url} : {derniere}")


def evenements_du_jour(salles, jour):
    # CELCAT utilise une borne de fin exclusive.
    lendemain = (date.fromisoformat(jour) + timedelta(days=1)).isoformat()
    donnees = {
        "start": jour,
        "end": lendemain,
        "resType": "102",
        "calView": "agendaDay",
        "colourScheme": "3",
        "federationIds[]": list(salles),
    }
    return post_json(BASE + "/Home/GetCalendarData", donnees)


def nettoyer(texte):
    return html.unescape(str(texte or "")).strip()


def details(event_id, salles, codes_par_nom):
    """Retourne les libellés classiques + les identifiants CELCAT des salles.

    Les identifiants bruts servent au calcul des disponibilités ; on n'utilise
    jamais les noms d'affichage pour deviner une salle occupée.
    """
    reponse = post_json(BASE + "/Home/GetSideBarEvent", {"eventId": event_id})
    if not isinstance(reponse, dict) or not isinstance(reponse.get("elements"), list):
        raise ValueError(f"Réponse de détails CELCAT inattendue pour {event_id}")
    groupes, modules, profs, lieux = [], [], [], []
    codes_salles = set()
    for element in reponse["elements"]:
        if not isinstance(element, dict):
            raise ValueError(f"Élément de détail mal formé pour {event_id}")
        contenu = nettoyer(element.get("content"))
        if not contenu:
            continue
        entite = element.get("entityType")
        if entite == 103:
            groupes.append(contenu)
        elif entite == 100:
            modules.append(contenu)
        elif entite == 101:
            profs.append(contenu)
        elif entite == 102:
            lieux.append(salles.get(contenu, contenu))
            if contenu in salles:
                codes_salles.add(contenu)
            elif contenu in codes_par_nom:
                # Repli uniquement sur une correspondance de nom exacte et unique.
                codes_salles.add(codes_par_nom[contenu])
    return groupes, modules, profs, lieux, codes_salles


def instant(chaine):
    """Interprète les dates CELCAT dans le fuseau de Paris."""
    if not chaine:
        return None
    valeur = datetime.fromisoformat(str(chaine).replace("Z", "+00:00"))
    if valeur.tzinfo is None:
        valeur = valeur.replace(tzinfo=FUSEAU)
    return valeur.astimezone(FUSEAU)


def plages_evenement(evt, jour):
    """Retourne les bornes d'occupation dans la journée, ou None si hors jour.

    Pour un événement de journée entière sans fin (par exemple « Vacances »),
    on marque la journée demandée comme occupée si CELCAT l'a retourné et si
    sa date de début n'est pas postérieure à la journée demandée.
    """
    le_jour = date.fromisoformat(jour)
    debut_jour = datetime.combine(le_jour, dt_time.min, tzinfo=FUSEAU)
    fin_jour = datetime.combine(le_jour + timedelta(days=1), dt_time.min, tzinfo=FUSEAU)
    debut = instant(evt.get("start"))
    if debut is None:
        raise ValueError(f"Événement sans date de début : {evt.get('id')}")
    fin = instant(evt.get("end"))
    jour_entier = bool(evt.get("allDay")) or fin is None
    if jour_entier and fin is None:
        # Le serveur a inclus cet événement dans sa réponse pour le jour demandé.
        if debut >= fin_jour:
            return None
        return debut_jour, fin_jour, True
    if fin is None or fin <= debut:
        raise ValueError(f"Intervalle CELCAT incohérent : {evt.get('id')}")
    if debut >= fin_jour or fin <= debut_jour:
        return None
    return max(debut, debut_jour), min(fin, fin_jour), jour_entier


def une_liste(valeur):
    if valeur is None:
        return []
    return valeur if isinstance(valeur, list) else [valeur]


def cle_tri_naturel(texte):
    """Ordre numérique naturel des salles : 11, 12, 107 (et non 107, 11, 12)."""
    return tuple((1, int(m)) if m.isdecimal() else (0, m)
                 for m in re.split(r"(\d+)", texte.casefold()))


def calculer_disponibilites(salles, autonomie, occupations, fiable, jour):
    resultat = {
        "verifiees": fiable,
        "horaires": {cle: list(plage) for cle, plage in PLAGES_AUTONOMIE.items()},
        "matin": [],
        "apres_midi": [],
    }
    if not fiable:
        # Jamais de liste faussement rassurante si une occupation n'a pas de salle connue.
        return resultat
    pour_date = date.fromisoformat(jour)
    for tranche, (heure_debut, heure_fin) in PLAGES_AUTONOMIE.items():
        debut = datetime.combine(pour_date, dt_time.fromisoformat(heure_debut), tzinfo=FUSEAU)
        fin = datetime.combine(pour_date, dt_time.fromisoformat(heure_fin), tzinfo=FUSEAU)
        disponibles = []
        for identifiant in autonomie:
            # Intersection de deux intervalles demi-ouverts : début1 < fin2 et fin1 > début2.
            occupe = any(a < fin and b > debut for a, b in occupations.get(identifiant, []))
            if not occupe:
                disponibles.append(salles[identifiant])
        resultat[tranche] = sorted(disponibles, key=cle_tri_naturel)
    return resultat


def main():
    jour = sys.argv[1] if len(sys.argv) > 1 else datetime.now(FUSEAU).date().isoformat()
    date.fromisoformat(jour)  # Validation explicite de l'argument de date.
    salles, autonomie = lire_salles()
    print(f"{len(salles)} salles, dont {len(autonomie)} autorisées en autonomie, date {jour}")

    bruts = evenements_du_jour(salles, jour)
    if not isinstance(bruts, list) or any(not isinstance(evt, dict) for evt in bruts):
        raise RuntimeError("Réponse CELCAT inattendue : une liste d'événements était attendue")

    # Les événements du lendemain sont filtrés, mais une réservation commençant
    # la veille et se poursuivant aujourd'hui doit bloquer sa salle.
    evenements_jour = []
    for evt in bruts:
        intervalle = plages_evenement(evt, jour)
        if intervalle is not None:
            evenements_jour.append((evt, intervalle))
    print(f"{len(bruts)} événements reçus, dont {len(evenements_jour)} concernant le {jour}")

    codes_par_nom = {nom: code for code, nom in salles.items()}
    occupations = {code: [] for code in autonomie}
    vus, lignes, erreurs = set(), [], 0
    localisation_inconnue = 0
    for evt, (debut, fin, jour_entier) in evenements_jour:
        identifiant = evt.get("id")
        if identifiant is None:
            raise RuntimeError("Réponse CELCAT : événement sans identifiant")
        if identifiant in vus:
            continue
        vus.add(identifiant)
        try:
            groupes, modules_detail, profs, lieux, codes = details(identifiant, salles, codes_par_nom)
        except Exception as erreur:
            if jour_entier:
                # L'occupation reste potentiellement générale : ne publier aucune
                # disponibilité, mais ne pas interrompre le relevé des cours.
                localisation_inconnue += 1
                print("ATTENTION : détails indisponibles pour l'événement en journée entière",
                      identifiant, ":", erreur)
            else:
                erreurs += 1
                print("ERREUR sur l'événement", identifiant, ":", erreur)
            continue
        if not codes:
            localisation_inconnue += 1
            print("ATTENTION : salle non identifiable pour l'événement", identifiant)
        else:
            for code in (codes & autonomie):
                occupations[code].append((debut, fin))

        # Format historique du planning inchangé. Une journée entière n'est pas
        # affichée dans l'emploi du temps, mais occupe bien la salle.
        if not jour_entier and instant(evt["start"]).date().isoformat() == jour:
            modules = [nettoyer(m) for m in une_liste(evt.get("modules")) if nettoyer(m)]
            ue = modules or modules_detail or [nettoyer(evt.get("eventCategory"))]
            ue = [u for u in ue if u]
            lignes.append([
                debut.strftime("%H:%M"), fin.strftime("%H:%M"), groupes, ue, profs, lieux
            ])
        time.sleep(PAUSE)

    if erreurs:
        print(f"{erreurs} événement(s) en erreur : data.json n'a PAS été modifié.")
        sys.exit(1)

    fiables = localisation_inconnue == 0
    disponibilites = calculer_disponibilites(salles, autonomie, occupations, fiables, jour)
    if not fiables:
        print(f"ATTENTION : {localisation_inconnue} événement(s) sans salle reconnue.")
        print("Les disponibilités ne seront pas affichées, mais le planning reste publié.")

    lignes.sort(key=lambda ligne: (ligne[0], ligne[1], ligne[5][0] if ligne[5] else ""))
    resultat = {
        "date": jour,
        "genere": datetime.now(FUSEAU).isoformat(timespec="seconds"),
        "evenements": lignes,
        "disponibilites": disponibilites,
    }
    temporaire = SORTIE.with_suffix(".tmp")
    temporaire.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    temporaire.replace(SORTIE)
    print(f"OK : {len(lignes)} lignes écrites dans {SORTIE.name}")
    if fiables:
        print(f"Autonomie : {len(disponibilites['matin'])} salle(s) le matin, "
              f"{len(disponibilites['apres_midi'])} l'après-midi")


if __name__ == "__main__":
    main()
