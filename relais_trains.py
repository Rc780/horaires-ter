#!/usr/bin/env python3
"""
Relais des horaires TER Gujan-Mestras <-> Bordeaux Saint-Jean, pour le script Cloudflare.

Lancé toutes les 15 minutes par GitHub Actions, à partir des données ouvertes SNCF (sans clé) :
  - horaires prévus (GTFS national) : relus dès que la SNCF les republie, au moins toutes les 12 h ;
  - temps réel (GTFS-RT) : retards, suppressions et trains ajoutés, connus environ 1 h à l'avance ;
  - surveillance du site des clés de l'API SNCF, une fois par heure.
Écrit trains.json (lu par Cloudflare) et horaires.json (cache des horaires prévus).

Usage : python relais_trains.py <dossier>
Le dossier peut contenir les fichiers du passage précédent : ils sont réutilisés.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from zoneinfo import ZoneInfo

GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
RT_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
SITES_CLES = (
    "https://numerique.sncf.com/startup/api/token-developpeur/",
    "https://www.digital.sncf.com/startup/api/token-developpeur",
)
MAISON = "87582619"   # Gujan-Mestras (code UIC)
TRAVAIL = "87581009"  # Bordeaux Saint-Jean (code UIC)
JOURS = 10            # horaires gardés : hier + les 10 prochains jours
RELIRE_HEURES = 12    # horaires prévus relus au moins toutes les 12 h
VERIFIER_SITE_MINUTES = 55
PARIS = ZoneInfo("Europe/Paris")
FORMAT = "%Y%m%dT%H%M%S"
AGENT = "Mozilla/5.0 (compatible; RelaisHorairesTER/1.0)"
NAVIGATEUR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ outils

def ouvrir(url, delai=120, entetes=None, methode="GET", limite=None):
    """Renvoie (statut, en-têtes, adresse finale, contenu). Ne lève pas d'erreur pour un statut HTTP."""
    requete = urllib.request.Request(url, method=methode, headers=entetes or {"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            corps = b"" if methode == "HEAD" else reponse.read(limite) if limite else reponse.read()
            return reponse.status, reponse.headers, reponse.geturl(), corps
    except urllib.error.HTTPError as erreur:
        return erreur.code, erreur.headers, url, b""


def telecharger(url, delai):
    statut, _, _, corps = ouvrir(url, delai)
    if statut != 200:
        raise RuntimeError(f"{url} a répondu {statut}")
    return corps


def maintenant():
    return dt.datetime.now(dt.timezone.utc)


def iso(instant):
    return instant.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lire_iso(texte):
    try:
        return dt.datetime.fromisoformat(str(texte).replace("Z", "+00:00"))
    except ValueError:
        return None


def local(posix):
    """Instant POSIX -> heure de Paris au format AAAAMMJJTHHMMSS."""
    return dt.datetime.fromtimestamp(posix, dt.timezone.utc).astimezone(PARIS).strftime(FORMAT)


def decaler(heure_locale, secondes):
    return (dt.datetime.strptime(heure_locale, FORMAT) + dt.timedelta(seconds=secondes)).strftime(FORMAT)


def ecart_secondes(a, b):
    return abs((dt.datetime.strptime(a, FORMAT) - dt.datetime.strptime(b, FORMAT)).total_seconds())


def secondes(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


def uic(stop_id):
    m = re.search(r"-(\d{8})$", stop_id or "")
    return m.group(1) if m else None


def numero_train(trip_id):
    m = re.match(r"OCESN(\d+)", trip_id or "")
    return m.group(1) if m else None


def lire_csv(archive, nom):
    with archive.open(nom) as brut:
        lecteur = csv.reader(io.TextIOWrapper(brut, encoding="utf-8-sig", newline=""))
        entete = next(lecteur)
        index = {colonne: i for i, colonne in enumerate(entete)}
        for ligne in lecteur:
            yield index, ligne


def lire_json(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ecrire_json(chemin, donnees):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------- horaires prévus

def signature_gtfs():
    """Identifiant de la version publiée du GTFS (sans le télécharger), ou None."""
    try:
        statut, entetes, _, _ = ouvrir(GTFS_URL, 30, methode="HEAD")
        if statut == 200:
            return entetes.get("ETag") or entetes.get("Last-Modified")
    except Exception:
        pass
    return None


def extraire_horaires(signature):
    """Trains directs Gujan <-> Saint-Jean, d'hier aux 10 prochains jours, d'après le GTFS."""
    archive = zipfile.ZipFile(io.BytesIO(telecharger(GTFS_URL, 300)))

    # 1. Passages aux deux gares (stop_times.txt est gros : on ne garde que nos gares).
    passages = {}
    for i, l in lire_csv(archive, "stop_times.txt"):
        code = uic(l[i["stop_id"]])
        if code not in (MAISON, TRAVAIL):
            continue
        passages.setdefault(l[i["trip_id"]], {})[code] = {
            "ordre": int(l[i["stop_sequence"]]),
            "arrivee": secondes(l[i["arrival_time"]]),
            "depart": secondes(l[i["departure_time"]]),
            "montee": l[i["pickup_type"]] != "1" if "pickup_type" in i else True,
            "descente": l[i["drop_off_type"]] != "1" if "drop_off_type" in i else True,
            "car": "Car" in l[i["stop_id"]],
        }
    voyages = {t: p for t, p in passages.items() if MAISON in p and TRAVAIL in p}

    # 2. Numéro de train et jours de circulation.
    infos = {}
    for i, l in lire_csv(archive, "trips.txt"):
        if l[i["trip_id"]] in voyages:
            infos[l[i["trip_id"]]] = (l[i["service_id"]], l[i["trip_headsign"]].strip())
    aujourdhui = maintenant().astimezone(PARIS).date()
    debut, fin = aujourdhui - dt.timedelta(days=1), aujourdhui + dt.timedelta(days=JOURS)
    services = {s for s, _ in infos.values()}
    jours = {}
    for i, l in lire_csv(archive, "calendar_dates.txt"):
        service = l[i["service_id"]]
        if service not in services:
            continue
        jour = dt.datetime.strptime(l[i["date"]], "%Y%m%d").date()
        if not debut <= jour <= fin:
            continue
        if l[i["exception_type"]] == "1":
            jours.setdefault(service, set()).add(jour)
        else:
            jours.setdefault(service, set()).discard(jour)

    # 3. Un train par voyage et par jour de circulation.
    trains = []
    for trip_id, p in voyages.items():
        if trip_id not in infos:
            continue
        aller = p[MAISON]["ordre"] < p[TRAVAIL]["ordre"]
        origine, destination = (p[MAISON], p[TRAVAIL]) if aller else (p[TRAVAIL], p[MAISON])
        if not origine["montee"] or not destination["descente"]:
            continue
        service, numero = infos[trip_id]
        for jour in sorted(jours.get(service, ())):
            minuit = dt.datetime.combine(jour, dt.time())
            trains.append({
                "sens": "aller" if aller else "retour",
                "numero": numero or numero_train(trip_id) or "",
                "mode": "Car TER" if origine["car"] else "TER",
                "dep": (minuit + dt.timedelta(seconds=origine["depart"])).strftime(FORMAT),
                "arr": (minuit + dt.timedelta(seconds=destination["arrivee"])).strftime(FORMAT),
                "date": jour.strftime("%Y%m%d"),
                "trip": trip_id,
            })
    if not trains:
        raise RuntimeError("aucun train Gujan <-> Saint-Jean dans le GTFS")
    trains.sort(key=lambda t: t["dep"])
    return {
        "genere_le": iso(maintenant()),
        "signature": signature,
        "du": debut.strftime("%Y%m%d"),
        "au": fin.strftime("%Y%m%d"),
        "trains": trains,
    }


# -------------------------------------------------------------- temps réel

def appliquer_temps_reel(trains, contenu=None):
    """Ajoute retards, suppressions et trains ajoutés. Renvoie l'heure du flux temps réel."""
    from google.transit import gtfs_realtime_pb2

    flux = gtfs_realtime_pb2.FeedMessage()
    flux.ParseFromString(contenu if contenu is not None else telecharger(RT_URL, 60))
    par_trip, par_numero, ajouts = {}, {}, []
    for entite in flux.entity:
        if not entite.HasField("trip_update"):
            continue
        maj = entite.trip_update
        # Le même trip_id sert plusieurs jours : la date de circulation fait partie de la clé.
        par_trip[(maj.trip.trip_id, maj.trip.start_date)] = maj
        if maj.trip.schedule_relationship == 1:  # ADDED
            ajouts.append(maj)
        numero = numero_train(maj.trip.trip_id)
        if numero:
            cle = (numero, maj.trip.start_date)
            # En cas de doublon, le train prévu (ou supprimé) l'emporte sur un train « ajouté ».
            if cle not in par_numero or par_numero[cle].trip.schedule_relationship == 1:
                par_numero[cle] = maj

    def arret(maj, code):
        return next((s for s in maj.stop_time_update if uic(s.stop_id) == code), None)

    def heure(evenement, prevue):
        """Heure réelle d'un passage : heure donnée, ou heure prévue + retard."""
        if evenement.HasField("time") and evenement.time > 0:
            reelle = local(evenement.time)
        elif evenement.HasField("delay") and prevue:
            reelle = decaler(prevue, evenement.delay)
        else:
            return None
        if prevue and ecart_secondes(reelle, prevue) > 12 * 3600:
            return None  # valeur aberrante
        return reelle

    for train in trains:
        maj = par_trip.get((train.get("trip"), train["date"])) or par_numero.get((train["numero"], train["date"]))
        if not maj:
            continue
        de, vers = (MAISON, TRAVAIL) if train["sens"] == "aller" else (TRAVAIL, MAISON)
        a, b = arret(maj, de), arret(maj, vers)
        supprime = maj.trip.schedule_relationship == 3  # CANCELED
        supprime = supprime or (a is not None and a.schedule_relationship == 1)  # arrêt supprimé
        supprime = supprime or (b is not None and b.schedule_relationship == 1)
        if supprime:
            train["supprime"] = True
            continue
        if a is not None and a.HasField("departure"):
            depart = heure(a.departure, train["dep"])
            if depart:
                train["depReel"] = depart
        if b is not None and b.HasField("arrival"):
            arrivee = heure(b.arrival, train["arr"])
            if arrivee:
                train["arrReel"] = arrivee

    # Trains ajoutés au dernier moment (remplacements, cars…), sauf ceux déjà connus.
    connus = {(t["numero"], t["date"]) for t in trains}
    for maj in ajouts:
        numero = numero_train(maj.trip.trip_id) or ""
        if (numero, maj.trip.start_date) in connus:
            continue
        arrets = list(maj.stop_time_update)
        for de, vers, sens in ((MAISON, TRAVAIL, "aller"), (TRAVAIL, MAISON, "retour")):
            a = next((k for k, s in enumerate(arrets) if uic(s.stop_id) == de), None)
            b = next((k for k, s in enumerate(arrets) if uic(s.stop_id) == vers), None)
            if a is None or b is None or a >= b:
                continue
            if arrets[a].schedule_relationship == 1 or arrets[b].schedule_relationship == 1:
                continue
            depart = heure(arrets[a].departure, None) or heure(arrets[a].arrival, None)
            arrivee = heure(arrets[b].arrival, None) or heure(arrets[b].departure, None)
            if depart and arrivee and arrivee > depart:
                trains.append({
                    "sens": sens, "numero": numero,
                    "mode": "Car TER" if "Car" in arrets[a].stop_id else "TER",
                    "dep": depart, "arr": arrivee, "depReel": depart, "arrReel": arrivee,
                    "date": maj.trip.start_date, "ajoute": True,
                })
                connus.add((numero, maj.trip.start_date))
    horodatage = flux.header.timestamp
    return iso(dt.datetime.fromtimestamp(horodatage, dt.timezone.utc)) if horodatage else iso(maintenant())


# ------------------------------------------------- site des clés SNCF

def tester_page_cles(url):
    """La page de demande de clé est-elle de nouveau en ligne ? -> (oui/non, détail)"""
    try:
        statut, _, finale, corps = ouvrir(url, 30, limite=800_000, entetes={
            "User-Agent": NAVIGATEUR,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "fr-FR,fr;q=0.9",
        })
    except Exception as erreur:
        return False, f"{type(erreur).__name__} ({getattr(erreur, 'reason', erreur)})"[:90]
    if statut != 200:
        return False, f"HTTP {statut}"
    if "/startup/api" not in urllib.parse.urlparse(finale).path:
        return False, "redirigé ailleurs"
    texte = corps.decode("utf-8", "replace")
    titre = re.search(r"<title[^>]*>(.*?)</title>", texte, re.I | re.S)
    titre = re.sub(r"\s+", " ", titre.group(1)).strip() if titre else ""
    if re.search(r"maintenance|indisponible|unavailable|erreur|error|introuvable|not found|forbidden|"
                 r"denied|refus|just a moment|attention required|\b(?:403|404|500|502|503|504)\b", titre, re.I):
        return False, f"page d'erreur ({titre[:60]})"
    if len(texte) < 2000 or not re.search(r"<form|token|cl[ée] d|e-?mail", texte, re.I):
        return False, "formulaire absent"
    return True, "accessible"


def verifier_site(precedent):
    precedent = precedent if isinstance(precedent, dict) else {}
    dernier = lire_iso(precedent.get("verifie_le"))
    if dernier and maintenant() - dernier < dt.timedelta(minutes=VERIFIER_SITE_MINUTES):
        return precedent
    resultat = {"accessible": False, "verifie_le": iso(maintenant()),
                "accessible_le": precedent.get("accessible_le"), "adresse": precedent.get("adresse")}
    details = []
    for url in SITES_CLES:
        ok, detail = tester_page_cles(url)
        details.append(f"{urllib.parse.urlparse(url).netloc} : {detail}")
        if ok:
            resultat.update(accessible=True, accessible_le=iso(maintenant()), adresse=url)
            break
    resultat["detail"] = " ; ".join(details)
    return resultat


# --------------------------------------------------------------- principal

def main():
    dossier = sys.argv[1] if len(sys.argv) > 1 else "sortie"
    os.makedirs(dossier, exist_ok=True)
    chemin_horaires = os.path.join(dossier, "horaires.json")
    chemin_trains = os.path.join(dossier, "trains.json")
    precedent = lire_json(chemin_trains) or {}
    horaires = lire_json(chemin_horaires)

    signature = signature_gtfs()
    relire = not horaires or not horaires.get("trains")
    if not relire:
        genere = lire_iso(horaires.get("genere_le"))
        relire = (not genere or maintenant() - genere >= dt.timedelta(hours=RELIRE_HEURES)
                  or (signature is not None and signature != horaires.get("signature")))
    if relire:
        try:
            horaires = extraire_horaires(signature)
            ecrire_json(chemin_horaires, horaires)
            print(f"Horaires prévus relus : {len(horaires['trains'])} trains du {horaires['du']} au {horaires['au']}")
        except Exception as erreur:  # on garde les anciens horaires si la SNCF ne répond pas
            print(f"Horaires prévus indisponibles ({erreur})")
            if not horaires or not horaires.get("trains"):
                raise

    trains = [dict(t) for t in horaires["trains"]]
    try:
        temps_reel_le = appliquer_temps_reel(trains)
        print(f"Temps réel appliqué (flux de {temps_reel_le})")
    except Exception as erreur:
        print(f"Temps réel indisponible ({erreur})")
        temps_reel_le = None

    site = verifier_site(precedent.get("site_cles_sncf"))
    print(f"Site des clés SNCF : {'ACCESSIBLE' if site.get('accessible') else 'inaccessible'} ({site.get('detail', '')})")

    sortie = []
    for t in sorted(trains, key=lambda t: t["dep"]):
        sortie.append({k: v for k, v in t.items() if k not in ("trip", "date")})
    ecrire_json(chemin_trains, {
        "version": 1,
        "mis_a_jour_le": iso(maintenant()),
        "horaires_le": horaires["genere_le"],
        "temps_reel_le": temps_reel_le,
        "du": horaires["du"],
        "au": horaires["au"],
        "site_cles_sncf": site,
        "trains": sortie,
    })
    resume = (f"{len(sortie)} trains Gujan <-> Saint-Jean du {horaires['du']} au {horaires['au']}, "
              f"temps réel {'OK' if temps_reel_le else 'indisponible'}")
    print(f"trains.json écrit : {resume}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"{resume}.\n\n")


if __name__ == "__main__":
    main()
