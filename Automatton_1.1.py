"""
================================================================================
 SCRIPT UNIFIÉ D'AUTOMATISATION — AVIS'AU & OPEN ADS
================================================================================
NOUVEAUTÉS DE CETTE VERSION :
  - Configuration unifiée dans un seul config.json (sections "Avis'Au",
    "OpenADS", "Commun", "Réserves"), déplacé dans C:/Users/matton_a/Documents/
    Automate/Config (avec dossiers_en_attente.json et background_queue.json).
  - Bibliothèque de réserves : popup enrichie avec boutons pré-remplis,
    réserves éditables directement dans le config.json (AEP et EU séparés).
  - OpenADS génère désormais aussi un rapport de session (rapport_session.txt).
  - Sélecteurs OpenADS déplacés dans le config.json (plus de code en dur).
  - Tous les paramètres fixes (communes, mapping, coefficients, timeouts,
    retry, qualité signataire, délais) sont chargés depuis le config.json.
  - Le logger capture aussi la console (print/erreurs) dans le même fichier
    de log structuré, pour remplacer un éventuel "debug_log" externe.
  - Correctifs de téléchargement (downloads_path, CDP override, arguments
    Chrome anti-"Download Bubble") conservés depuis les sessions précédentes.
================================================================================
"""

import json
import csv
import os
import re
import sys
import time
import zipfile
import unicodedata
import shutil
import subprocess
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from fillpdf import fillpdfs
from pypdf import PdfReader, PdfWriter

# ============================================================
# CHEMINS DE CONFIGURATION CENTRALISÉS
# ============================================================

DOSSIER_CONFIG = Path(r"C:\Users\matton_a\Documents\Automate\Config")
CHEMIN_CONFIG_JSON = DOSSIER_CONFIG / "config.json"
CHEMIN_ATTENTE_JSON = DOSSIER_CONFIG / "dossiers_en_attente.json"
CHEMIN_QUEUE_JSON = DOSSIER_CONFIG / "background_queue.json"

FICHIER_EXPORT_SHEETS_AVISAU = "lignes_a_copier.txt"
RECAP_FILE_AVISAU = "rapport_session.txt"

# ============================================================
# GLOBALS APPLIQUÉS DEPUIS LE CONFIG.JSON (valeurs par défaut de secours)
# ============================================================

COMMUNES_SEMM, COMMUNES_SAOM, COMMUNES_SAEM = [], [], []
SERVICES_AVISAU = {}
MAPPING_NATURE_INDEX_AVISAU = {}
AVIS_OPTIONS_OPENADS_LABELS = {}
SELECTEURS_OPENADS = {}
DELAIS_AVISAU = {"SEMM": 30, "SAOM": 21, "SAEM": 21}
QUALITE_SIGNATAIRE = "Chargé d'affaires AMO"
COEFFICIENT_PRESSION = 0.065
TIMEOUT_COURT, TIMEOUT_MOYEN, TIMEOUT_LONG, TIMEOUT_TELECHARGEMENT = 5000, 15000, 30000, 120000
NB_TENTATIVES_RETRY, DELAI_RETRY_SECONDES = 3, 1
SEUIL_ALERTE_JOURS = 2

CLES_REQUISES_AVISAU = [
    "identifiant", "mot_de_passe", "service", "chrome_profile",
    "dossier_telechargement", "dossier_destination", "formulaire_pdf",
    "formulaire_dossier_sortie", "dossier_en_attente", "dossier_a_upload"
]
CLES_REQUISES_OPENADS = [
    "identifiant", "mot_de_passe", "chrome_profile",
    "dossier_telechargement", "dossier_destination", "formulaire_pdf",
    "formulaire_dossier_sortie", "dossier_en_attente", "dossier_a_upload"
]

# ============================================================
# EXCEPTIONS
# ============================================================

class ConfigurationInvalide(Exception):
    pass


class FenetreFermeeException(Exception):
    pass


# ============================================================
# LOGGER (capture aussi la console pour remplacer un debug_log externe)
# ============================================================

class FluxDedouble:
    """Duplique l'écriture vers un flux original (console) et vers un fichier."""
    def __init__(self, flux_original, fichier_log):
        self.flux_original = flux_original
        self.fichier_log = fichier_log

    def write(self, message):
        try:
            self.flux_original.write(message)
        except Exception:
            pass
        if message.strip():
            try:
                with open(self.fichier_log, "a", encoding="utf-8") as f:
                    f.write(message if message.endswith("\n") else message + "\n")
            except Exception:
                pass

    def flush(self):
        try:
            self.flux_original.flush()
        except Exception:
            pass


class Logger:
    def __init__(self, dossier_logs, outil_nom, capturer_console=True):
        self.dossier_logs = Path(dossier_logs)
        self.dossier_logs.mkdir(parents=True, exist_ok=True)

        horodatage = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        nom_fichier = f"{outil_nom}_{horodatage}.log"
        self.chemin_log = self.dossier_logs / nom_fichier

        self._flux_originaux = (sys.stdout, sys.stderr)
        if capturer_console:
            sys.stdout = FluxDedouble(sys.stdout, self.chemin_log)
            sys.stderr = FluxDedouble(sys.stderr, self.chemin_log)

        self._ecrire(f"{'=' * 60}")
        self._ecrire(f"  SESSION {outil_nom.upper()} — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        self._ecrire(f"{'=' * 60}")
        print(f"📝 Log de session (console + structuré) : {self.chemin_log}")

    def _ecrire(self, message):
        horodatage = datetime.now().strftime("%H:%M:%S")
        ligne = f"[{horodatage}] {message}"
        try:
            with open(self.chemin_log, "a", encoding="utf-8") as f:
                f.write(ligne + "\n")
        except Exception:
            pass

    def info(self, message):
        print(message)

    def succes(self, message):
        print(message)

    def warning(self, message):
        print(message)

    def erreur(self, message):
        print(message)

    def separateur(self, titre=""):
        ligne = f"{'=' * 60}"
        if titre:
            print(f"\n{ligne}\n  {titre}\n{ligne}")
        else:
            print(ligne)

    def fermer(self):
        self._ecrire(f"{'=' * 60}")
        self._ecrire(f"  FIN DE SESSION — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        self._ecrire(f"{'=' * 60}")
        sys.stdout, sys.stderr = self._flux_originaux


# ============================================================
# NOTIFICATIONS "TOAST"
# ============================================================

def afficher_notification(titre, message, type_="info", duree_ms=6000, parent=None):
    couleurs = {"info": "#2196F3", "warning": "#FF9800", "erreur": "#f44336", "succes": "#4CAF50"}
    couleur = couleurs.get(type_, "#2196F3")

    root_temporaire = None
    if parent is None:
        parent = tk._default_root
    if parent is None:
        root_temporaire = tk.Tk()
        root_temporaire.withdraw()
        parent = root_temporaire

    try:
        toast = tk.Toplevel(parent)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        largeur, hauteur = 340, 100
        ecran_l, ecran_h = toast.winfo_screenwidth(), toast.winfo_screenheight()
        toast.geometry(f"{largeur}x{hauteur}+{ecran_l - largeur - 20}+{ecran_h - hauteur - 60}")
        cadre = tk.Frame(toast, bg=couleur, bd=1, relief="solid")
        cadre.pack(fill="both", expand=True)
        tk.Label(cadre, text=titre, font=("Arial", 10, "bold"), bg=couleur, fg="white",
                 anchor="w", justify="left", wraplength=310).pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(cadre, text=message, font=("Arial", 9), bg=couleur, fg="white",
                 anchor="w", justify="left", wraplength=310).pack(fill="x", padx=10, pady=(0, 8))

        def _fermer():
            try: toast.destroy()
            except Exception: pass
            if root_temporaire:
                try: root_temporaire.destroy()
                except Exception: pass

        toast.after(duree_ms, _fermer)
        toast.update()
    except Exception:
        if root_temporaire:
            try: root_temporaire.destroy()
            except Exception: pass


# ============================================================
# UTILITAIRES : ROBUSTESSE
# ============================================================

def avec_retry(fonction, tentatives=None, delai=None, logger=None, description=""):
    tentatives = tentatives or NB_TENTATIVES_RETRY
    delai = delai or DELAI_RETRY_SECONDES
    derniere_erreur = None
    for tentative in range(1, tentatives + 1):
        try:
            return fonction()
        except Exception as e:
            derniere_erreur = e
            suffixe = f" ({description})" if description else ""
            if logger:
                logger.warning(f"⚠️ Tentative {tentative}/{tentatives} échouée{suffixe} : {e}")
            if tentative < tentatives:
                time.sleep(delai)
    suffixe = f" ({description})" if description else ""
    if logger:
        logger.erreur(f"❌ Échec définitif après {tentatives} tentatives{suffixe} : {derniere_erreur}")
    raise derniere_erreur


def ecrire_json_atomique(chemin, data):
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fichier_temp = chemin.with_suffix(chemin.suffix + ".tmp")
    with open(fichier_temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(str(fichier_temp), str(chemin))

def remplir_pdf_champs_cibles(chemin_modele, chemin_sortie, valeurs, logger=None):
    """
    Remplit uniquement les champs explicitement fournis dans `valeurs`, via pypdf
    plutôt que fillpdf/pdftk. Contourne un bug de fillpdf qui plante
    ('NoneType has no len()') dès qu'on fournit une valeur pour un champ de
    type menu déroulant (Choice/Dropdown) sur ce PDF modèle.
    """
    lecteur = PdfReader(chemin_modele)
    ecrivain = PdfWriter()
    ecrivain.append(lecteur)

    for page in ecrivain.pages:
        try:
            ecrivain.update_page_form_field_values(page, valeurs)
        except Exception as e:
            if logger:
                logger.warning(f"⚠️ Remplissage partiel sur une page (certains champs ignorés) : {e}")

    try:
        ecrivain.set_need_appearances_writer(True)
    except Exception:
        pass

    with open(chemin_sortie, "wb") as f:
        ecrivain.write(f)

    return chemin_sortie


def nettoyer_historique_chrome(chemin_profil, logger=None):
    """
    Supprime la base de données d'historique/téléchargements de Chrome (History,
    History-journal) sans toucher aux cookies ni aux données de connexion.
    Empêche la corruption/blocage observé lors des téléchargements répétés sur un
    profil persistant réutilisé. Recherche automatiquement le bon sous-dossier de
    profil (Default, Profile 1, etc.) au lieu de supposer "Default" à l'aveugle.
    """
    dossier_racine = Path(chemin_profil)
    if not dossier_racine.exists():
        if logger: logger.info(f"ℹ️ Profil {chemin_profil} inexistant (premier lancement) — rien à nettoyer.")
        return

    candidats = [d for d in dossier_racine.iterdir() if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile"))]
    if not candidats:
        if logger: logger.warning(f"⚠️ Aucun sous-dossier de profil trouvé dans {chemin_profil} (History non nettoyé).")
        return

    for dossier_profil in candidats:
        for nom_fichier in ["History", "History-journal"]:
            chemin_fichier = dossier_profil / nom_fichier
            if chemin_fichier.exists():
                try:
                    chemin_fichier.unlink()
                    if logger: logger.info(f"🧹 {nom_fichier} nettoyé ({dossier_profil.name}).")
                except Exception as e:
                    if logger: logger.warning(f"⚠️ Impossible de nettoyer {nom_fichier} ({dossier_profil.name}) : {e}")



# ============================================================
# CONFIGURATION UNIFIÉE
# ============================================================

def charger_configuration_unifiee():
    if not CHEMIN_CONFIG_JSON.exists():
        raise ConfigurationInvalide(f"Fichier de configuration introuvable : {CHEMIN_CONFIG_JSON}")
    try:
        with open(CHEMIN_CONFIG_JSON, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigurationInvalide(
            f"Erreur de syntaxe JSON dans {CHEMIN_CONFIG_JSON} : {e}\n"
            f"💡 Astuce : dans un chemin Windows, utilisez '/' ou doublez les '\\' (ex: 'C:/dossier' ou 'C:\\\\dossier')."
        )

    for section in ["Avis'Au", "OpenADS", "Commun", "Réserves"]:
        if section not in config:
            raise ConfigurationInvalide(f"Section manquante dans config.json : '{section}'")

    return config


def valider_section(section_config, cles_requises, nom_outil):
    manquantes = [c for c in cles_requises if c not in section_config or section_config[c] in (None, "")]
    if manquantes:
        raise ConfigurationInvalide(
            f"Configuration '{nom_outil}' incomplète. Clé(s) manquante(s) ou vide(s) : {', '.join(manquantes)}"
        )


def appliquer_configuration_globale(config):
    """Injecte les paramètres du config.json dans les variables globales du script."""
    global COMMUNES_SEMM, COMMUNES_SAOM, COMMUNES_SAEM, SERVICES_AVISAU
    global MAPPING_NATURE_INDEX_AVISAU, AVIS_OPTIONS_OPENADS_LABELS, SELECTEURS_OPENADS
    global DELAIS_AVISAU, QUALITE_SIGNATAIRE, COEFFICIENT_PRESSION
    global TIMEOUT_COURT, TIMEOUT_MOYEN, TIMEOUT_LONG, TIMEOUT_TELECHARGEMENT
    global NB_TENTATIVES_RETRY, DELAI_RETRY_SECONDES, SEUIL_ALERTE_JOURS

    commun = config.get("Commun", {})
    avisau = config.get("Avis'Au", {})
    openads = config.get("OpenADS", {})

    communes = commun.get("communes", {})
    COMMUNES_SEMM = communes.get("SEMM", [])
    COMMUNES_SAOM = communes.get("SAOM", [])
    COMMUNES_SAEM = communes.get("SAEM", [])

    SERVICES_AVISAU = avisau.get("services", {})
    MAPPING_NATURE_INDEX_AVISAU = avisau.get("mapping_nature_index", {})
    DELAIS_AVISAU = avisau.get("delais", {"SEMM": 30, "SAOM": 21, "SAEM": 21})

    AVIS_OPTIONS_OPENADS_LABELS = openads.get("mapping_avis_labels", {})
    SELECTEURS_OPENADS = openads.get("selectors", {})

    QUALITE_SIGNATAIRE = commun.get("qualite_signataire", "Chargé d'affaires AMO")
    COEFFICIENT_PRESSION = commun.get("coefficient_pression", 0.065)

    timeouts = commun.get("timeouts", {})
    TIMEOUT_COURT = timeouts.get("court", 5000)
    TIMEOUT_MOYEN = timeouts.get("moyen", 15000)
    TIMEOUT_LONG = timeouts.get("long", 30000)
    TIMEOUT_TELECHARGEMENT = timeouts.get("telechargement", 120000)

    retry = commun.get("retry", {})
    NB_TENTATIVES_RETRY = retry.get("tentatives", 3)
    DELAI_RETRY_SECONDES = retry.get("delai_secondes", 1)

    SEUIL_ALERTE_JOURS = commun.get("seuil_alerte_jours", 2)


def construire_config_outil(config, cle_outil):
    """Fusionne la section d'un outil avec les clés communes nécessaires (compatibilité avec le code existant)."""
    cfg = dict(config.get(cle_outil, {}))
    cfg["dossier_logs"] = config.get("Commun", {}).get("dossier_logs", "logs")
    return cfg


# ============================================================
# FONCTIONS UTILITAIRES PARTAGÉES
# ============================================================

def verifier_dossiers_config(config):
    cles_dossiers = ["dossier_telechargement", "dossier_destination", "formulaire_dossier_sortie",
                      "dossier_en_attente", "dossier_a_upload", "dossier_logs"]
    for cle in cles_dossiers:
        if cle in config and config[cle]:
            Path(config[cle]).mkdir(parents=True, exist_ok=True)


def formater_numero_dossier_avisau(num):
    m = re.match(r'^([A-Z]{2})(\d{3})(\d{3})(\d{2})(.+)$', num.strip().upper())
    return f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)} {m.group(5)}" if m else num.strip().upper()


def formater_numero_dossier_openads(numero_brut):
    numero = numero_brut.strip().upper()
    match = re.match(r'^([A-Z]{2})(\d{6})(\d{2})(\d{5}.*)$', numero)
    return f"{match.group(1)} {match.group(2)} {match.group(3)} {match.group(4)}" if match else numero_brut


def nettoyer_nom_dossier(nom):
    return re.sub(r'[<>:"/\\|?*]', "_", nom).strip()


def normaliser_commune(commune):
    commune = commune.lower().strip()
    commune = ''.join(c for c in unicodedata.normalize('NFD', commune) if unicodedata.category(c) != 'Mn')
    commune = commune.replace('-', ' ')
    return ' '.join(commune.split())


def charger_attente(fichier=None):
    fichier = Path(fichier) if fichier else CHEMIN_ATTENTE_JSON
    if fichier.exists():
        try:
            with open(fichier, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"⚠️ Impossible de lire {fichier} ({e}). Réinitialisation.")
            return {}
    return {}


def sauvegarder_attente(data, fichier=None):
    fichier = Path(fichier) if fichier else CHEMIN_ATTENTE_JSON
    try:
        ecrire_json_atomique(fichier, data)
    except Exception as e:
        print(f"❌ Échec de l'écriture de {fichier} : {e}")


def ouvrir_fichier_os(chemin_fichier):
    chemin_str = str(chemin_fichier)
    try:
        os.startfile(chemin_str)
    except AttributeError:
        if sys.platform == "darwin":
            subprocess.run(["open", chemin_str], check=False)
        else:
            subprocess.run(["xdg-open", chemin_str], check=False)


def ouvrir_fichiers_dossier(chemin_dossier, logger):
    if not chemin_dossier or not Path(chemin_dossier).exists():
        return
    for fichier in Path(chemin_dossier).iterdir():
        if fichier.is_file() and not fichier.name.endswith("Motif en attente.txt"):
            try:
                ouvrir_fichier_os(fichier)
            except Exception as e:
                logger.warning(f"   ⚠️ {fichier.name} : {e}")


def trouver_pdf_avis(chemin_dossier, numero_formate, exclure_suffixe=None):
    for fichier in sorted(Path(chemin_dossier).iterdir()):
        if fichier.suffix.lower() == ".pdf" and fichier.stem.startswith(numero_formate):
            if exclure_suffixe and fichier.stem.endswith(exclure_suffixe):
                continue
            return str(fichier)
    return None


def trouver_pdf_par_suffixe(chemin_dossier, suffixe):
    for fichier in sorted(Path(chemin_dossier).iterdir()):
        if fichier.suffix.lower() == ".pdf" and fichier.stem.endswith(suffixe):
            return str(fichier)
    return None


def get_societe_avisau(commune):
    c = normaliser_commune(commune).lower()
    for soc, lst in [("SEMM", COMMUNES_SEMM), ("SAOM", COMMUNES_SAOM), ("SAEM", COMMUNES_SAEM)]:
        for ville in lst:
            if ville in c or c in ville:
                return soc
    return None


def calculer_date_limite_avisau(date_depart_str, societe):
    try:
        date_depart = datetime.strptime(date_depart_str, "%Y-%m-%d")
        jours = DELAIS_AVISAU.get(societe, 21)
        return (date_depart + timedelta(days=jours)).strftime("%d/%m")
    except Exception:
        return "??/??"


def calculer_urgence_avisau(date_depart_str, societe):
    try:
        jours = DELAIS_AVISAU.get(societe, 21)
        limite = datetime.strptime(date_depart_str, "%Y-%m-%d") + timedelta(days=jours)
        restant = (limite - datetime.now()).days
        if restant < 0: return restant, f"⛔ {abs(restant)}j", "#f44336"
        if restant <= 5: return restant, f"⚠️ {restant}j", "#FF9800"
        return restant, f"✅ {restant}j", "#4CAF50"
    except Exception:
        return None, "?", "#9E9E9E"


def calculer_urgence_openads(date_limite_str):
    try:
        limite = datetime.strptime(date_limite_str, "%d/%m/%Y")
        restant = (limite - datetime.now()).days
        if restant < 0: return restant, f"⛔ {abs(restant)}j", "#f44336"
        if restant <= 5: return restant, f"⚠️ {restant}j", "#FF9800"
        return restant, f"✅ {restant}j", "#4CAF50"
    except Exception:
        return None, "?", "#9E9E9E"


def categorie_urgence(jours_restants):
    if jours_restants is None: return "inconnu"
    if jours_restants < 0: return "retard"
    if jours_restants <= 5: return "urgent"
    return "ok"


# ============================================================
# FILTRES / TRI COMMUNS
# ============================================================

LABELS_URGENCE = {"toutes": "Toutes", "retard": "⛔ En retard", "urgent": "⚠️ Urgent (≤5j)", "ok": "✅ OK", "inconnu": "❓ Inconnu"}
LABELS_STATUT = {"tous": "Tous", "attente": "📂 En attente", "nouveau": "◻️ Nouveau"}


class EtatFiltreTri:
    def __init__(self):
        self.recherche = ""
        self.commune = "Toutes"
        self.societe = "Toutes"
        self.urgence = "toutes"
        self.statut = "tous"
        self.colonne_tri = None
        self.tri_inverse = False

    def basculer_tri(self, colonne):
        if self.colonne_tri == colonne:
            self.tri_inverse = not self.tri_inverse
        else:
            self.colonne_tri, self.tri_inverse = colonne, False

    def fleche(self, colonne):
        if self.colonne_tri != colonne: return ""
        return " ▼" if self.tri_inverse else " ▲"


def creer_barre_filtres(parent, couleur_bg, etat, communes_disponibles, societes_disponibles, avec_societe, callback_appliquer):
    cadre = tk.Frame(parent, bg=couleur_bg)
    cadre.pack(fill="x", padx=20, pady=(10, 0))

    tk.Label(cadre, text="🔎", bg=couleur_bg, font=("Arial", 10)).pack(side="left", padx=(0, 2))
    var_recherche = tk.StringVar(value=etat.recherche)
    entree_recherche = tk.Entry(cadre, textvariable=var_recherche, width=22, font=("Arial", 9))
    entree_recherche.pack(side="left", padx=(0, 10))

    tk.Label(cadre, text="Commune :", bg=couleur_bg, font=("Arial", 9, "bold")).pack(side="left")
    var_commune = tk.StringVar(value=etat.commune)
    combo_commune = ttk.Combobox(cadre, textvariable=var_commune, values=["Toutes"] + communes_disponibles, width=18, state="readonly", font=("Arial", 9))
    combo_commune.pack(side="left", padx=(4, 10))

    var_societe = tk.StringVar(value=etat.societe)
    if avec_societe:
        tk.Label(cadre, text="Société :", bg=couleur_bg, font=("Arial", 9, "bold")).pack(side="left")
        combo_societe = ttk.Combobox(cadre, textvariable=var_societe, values=["Toutes"] + societes_disponibles, width=10, state="readonly", font=("Arial", 9))
        combo_societe.pack(side="left", padx=(4, 10))

    tk.Label(cadre, text="Urgence :", bg=couleur_bg, font=("Arial", 9, "bold")).pack(side="left")
    var_urgence = tk.StringVar(value=LABELS_URGENCE[etat.urgence])
    combo_urgence = ttk.Combobox(cadre, textvariable=var_urgence, values=list(LABELS_URGENCE.values()), width=16, state="readonly", font=("Arial", 9))
    combo_urgence.pack(side="left", padx=(4, 10))

    tk.Label(cadre, text="Statut :", bg=couleur_bg, font=("Arial", 9, "bold")).pack(side="left")
    var_statut = tk.StringVar(value=LABELS_STATUT[etat.statut])
    combo_statut = ttk.Combobox(cadre, textvariable=var_statut, values=list(LABELS_STATUT.values()), width=14, state="readonly", font=("Arial", 9))
    combo_statut.pack(side="left", padx=(4, 10))

    def _appliquer(*_args):
        etat.recherche = var_recherche.get().strip().lower()
        etat.commune = var_commune.get()
        etat.societe = var_societe.get()
        etat.urgence = {v: k for k, v in LABELS_URGENCE.items()}.get(var_urgence.get(), "toutes")
        etat.statut = {v: k for k, v in LABELS_STATUT.items()}.get(var_statut.get(), "tous")
        callback_appliquer()

    entree_recherche.bind("<KeyRelease>", _appliquer)
    combo_commune.bind("<<ComboboxSelected>>", _appliquer)
    if avec_societe:
        combo_societe.bind("<<ComboboxSelected>>", _appliquer)
    combo_urgence.bind("<<ComboboxSelected>>", _appliquer)
    combo_statut.bind("<<ComboboxSelected>>", _appliquer)

    tk.Button(cadre, text="↺ Réinitialiser", bg="#607D8B", fg="white", font=("Arial", 8, "bold"),
              command=lambda: (var_recherche.set(""), var_commune.set("Toutes"), var_societe.set("Toutes"),
                                var_urgence.set(LABELS_URGENCE["toutes"]), var_statut.set(LABELS_STATUT["tous"]), _appliquer())
              ).pack(side="left", padx=(10, 0))
    return cadre


def entete_colonne_triable(parent, texte, largeur, colonne_id, etat, callback_tri):
    texte_affiche = texte + etat.fleche(colonne_id)
    bouton = tk.Button(parent, text=texte_affiche, width=largeur, bg="#37474F", fg="white", font=("Arial", 9, "bold"),
                        relief="flat", activebackground="#455A64", activeforeground="white",
                        command=lambda: callback_tri(colonne_id))
    bouton.pack(side="left", padx=3, pady=5)
    return bouton


# ============================================================
# BIBLIOTHÈQUE DE RÉSERVES (composant commun)
# ============================================================

def construire_popup_reserve(fenetre_parent, couleur_bg, titre_popup, valeur_initiale, dict_reserves, callback_valider, callback_annuler):
    """
    Construit la popup "Bibliothèque de réserves" : titre, grille de boutons
    pré-remplis (issus du config.json), puis zone de texte libre.
    Un clic sur un bouton ajoute le texte à la suite de l'existant (ou le
    remplace si la zone est vide). Aucune déduplication n'est appliquée.
    """
    popup = tk.Toplevel(fenetre_parent)
    popup.title(titre_popup)
    popup.geometry("650x560")
    popup.configure(bg=couleur_bg)
    popup.transient(fenetre_parent)
    popup.grab_set()
    popup.geometry(f"+{fenetre_parent.winfo_x() + 200}+{fenetre_parent.winfo_y() + 120}")

    tk.Label(popup, text="📚 Bibliothèque de réserves", bg=couleur_bg, font=("Arial", 12, "bold"), fg="#1565C0").pack(pady=(12, 6))

    cadre_boutons = tk.Frame(popup, bg=couleur_bg)
    cadre_boutons.pack(fill="x", padx=20, pady=(0, 10))

    zone_texte = tk.Text(popup, font=("Arial", 10), width=70, height=10)

    def inserer_reserve(texte_a_ajouter):
        contenu_actuel = zone_texte.get("1.0", "end-1c").strip()
        if contenu_actuel:
            zone_texte.insert("end", "\n\n" + texte_a_ajouter)
        else:
            zone_texte.insert("1.0", texte_a_ajouter)
        zone_texte.see("end")

    if dict_reserves:
        for i, (label, texte_reserve) in enumerate(dict_reserves.items()):
            tk.Button(cadre_boutons, text=label, font=("Arial", 8, "bold"), bg="#E3F2FD", fg="#1565C0",
                      wraplength=140, justify="left", relief="groove", bd=1,
                      command=lambda t=texte_reserve: inserer_reserve(t)
                      ).grid(row=i // 3, column=i % 3, padx=4, pady=4, sticky="nsew")
        for col in range(3):
            cadre_boutons.grid_columnconfigure(col, weight=1)
    else:
        tk.Label(cadre_boutons, text="Aucune réserve prédéfinie pour ce type d'avis.", bg=couleur_bg, fg="#9E9E9E", font=("Arial", 9, "italic")).pack()

    ttk.Separator(popup, orient="horizontal").pack(fill="x", padx=20, pady=8)
    tk.Label(popup, text="Motif / détail final :", bg=couleur_bg, font=("Arial", 10, "bold")).pack(anchor="w", padx=20)
    zone_texte.pack(padx=20, pady=5)
    if valeur_initiale:
        zone_texte.insert("1.0", valeur_initiale)
    zone_texte.focus_set()

    def valider():
        texte = zone_texte.get("1.0", "end-1c").strip()
        popup.destroy()
        callback_valider(texte)

    def annuler():
        popup.destroy()
        callback_annuler()

    f_btn = tk.Frame(popup, bg=couleur_bg)
    f_btn.pack(pady=10)
    tk.Button(f_btn, text="Valider", command=valider, bg="#FF9800", fg="white", width=12, font=("Arial", 9, "bold")).pack(side="left", padx=10)
    tk.Button(f_btn, text="Annuler", command=annuler, bg="#9E9E9E", fg="white", width=12, font=("Arial", 9, "bold")).pack(side="left", padx=10)
    popup.protocol("WM_DELETE_WINDOW", annuler)


# ============================================================
# ============================================================
# MODULE : AVIS'AU
# ============================================================
# ============================================================

class StatsSessionAvisAU:
    def __init__(self):
        self.debut = datetime.now()
        self.traites = self.incomplets = self.en_attente = 0

    def generer_rapport(self):
        duree = datetime.now() - self.debut
        rapport = (
            f"\n--- SESSION AVIS'AU DU {datetime.now().strftime('%d/%m/%Y')} ---\n"
            f"⏱ Durée : {int(duree.total_seconds() // 60)} min\n"
            f"✅ Finalisés : {self.traites} | 📋 Incomplets : {self.incomplets} | 📦 Attente : {self.en_attente}\n"
        )
        with open(RECAP_FILE_AVISAU, "a", encoding="utf-8") as f:
            f.write(rapport)


class InterfaceAvisAU:
    def __init__(self, dossiers_csv, logger, reserves):
        self.dossiers_csv = dossiers_csv
        self.logger = logger
        self.reserves = reserves  # {"AEP": {...}, "EU": {...}}
        self.resultat = {"dossiers": [], "action": "QUIT"}
        self.stealth_mode = False
        self.societe_actuelle = None
        self.etat_filtre = EtatFiltreTri()
        self.dossiers_affiches = []
        self.selection_persistante = set()

        self.COULEURS = {"SEMM": "#2196F3", "SAOM": "#4CAF50", "SAEM": "#FF9800", None: "#9E9E9E", "bg": "#F5F5F5", "titre": "#1565C0"}

        self.fenetre = tk.Tk()
        self.fenetre.title("AUTOMATISATION AVIS'AU")
        self.fenetre.geometry("1150x820")
        self.fenetre.configure(bg=self.COULEURS["bg"])
        self.fenetre.update_idletasks()
        self.fenetre.geometry(f"+{(self.fenetre.winfo_screenwidth()//2)-(1150//2)}+{(self.fenetre.winfo_screenheight()//2)-(820//2)}")

        self.avis_aep_var = self.avis_eu_var = self.contrat_var = self.surf_anc_var = self.surf_nouv_var = None
        self.press_mini_var, self.press_tn_var, self.press_res_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.nouveau_branchement_var, self.extension_logement_var, self.deja_consulte_var = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        self.resultat_avis = {"dossier_pret": None, "avis_aep": None, "avis_eu": None, "action": None}

        self._canvas_actif = None
        self.fenetre.bind_all("<MouseWheel>", self._on_mousewheel)
        self.fenetre.bind("<Control-Alt-f>", self._toggle_stealth)
        self.fenetre.bind("<Control-Alt-F>", self._toggle_stealth)

        self.cadre_titre = tk.Frame(self.fenetre, bg=self.COULEURS["titre"], pady=10)
        self.cadre_titre.pack(fill="x")
        tk.Label(self.cadre_titre, text="🚰  AUTOMATISATION AVIS'AU", font=("Arial", 14, "bold"), bg=self.COULEURS["titre"], fg="white").pack()
        self.label_stealth = tk.Label(self.cadre_titre, text="", font=("Arial", 9, "bold"), bg=self.COULEURS["titre"], fg="#FFEB3B")
        self.label_stealth.pack()
        self.label_sous_titre = tk.Label(self.cadre_titre, text=f"📋  {len(dossiers_csv)} dossier(s) en attente", font=("Arial", 10), bg=self.COULEURS["titre"], fg="#BBDEFB")
        self.label_sous_titre.pack()

        self.cadre_contenu = tk.Frame(self.fenetre, bg=self.COULEURS["bg"])
        self.cadre_contenu.pack(fill="both", expand=True)

        self.afficher_etape_selection()

    def _on_mousewheel(self, event):
        if self._canvas_actif:
            try: self._canvas_actif.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception: pass

    def _toggle_stealth(self, event=None):
        self.stealth_mode = not self.stealth_mode
        self.label_stealth.config(text="🥷 MODE FURTIF ACTIVÉ (Réseau allégé)" if self.stealth_mode else "")
        self.logger.info("🥷 Mode Furtif " + ("activé." if self.stealth_mode else "désactivé."))

    def vider_contenu(self):
        for widget in self.cadre_contenu.winfo_children():
            widget.destroy()

    def mettre_a_jour_sous_titre(self, texte, couleur="#BBDEFB"):
        self.label_sous_titre.config(text=texte, fg=couleur)

    def _dossiers_enrichis(self, dossiers_attente):
        enrichis = []
        for d in self.dossiers_csv:
            societe = get_societe_avisau(d["commune"])
            restant, texte_restant, couleur_restant = calculer_urgence_avisau(d["date_depart"], societe)
            en_attente = d["numero_formate"] in dossiers_attente
            motif = dossiers_attente.get(d["numero_formate"], {}).get("motif", "") if en_attente else ""
            enrichis.append({"dossier": d, "societe": societe, "restant": restant, "texte_restant": texte_restant,
                              "couleur_restant": couleur_restant, "en_attente": en_attente, "motif": motif,
                              "categorie_urgence": categorie_urgence(restant)})
        return enrichis

    def _filtrer_et_trier(self, enrichis):
        etat = self.etat_filtre
        resultat = []
        for e in enrichis:
            d = e["dossier"]
            if etat.recherche and etat.recherche not in d["numero_formate"].lower() and etat.recherche not in d["commune"].lower():
                continue
            if etat.commune != "Toutes" and d["commune"] != etat.commune:
                continue
            if etat.societe != "Toutes" and (e["societe"] or "?") != etat.societe:
                continue
            if etat.urgence != "toutes" and e["categorie_urgence"] != etat.urgence:
                continue
            if etat.statut == "attente" and not e["en_attente"]:
                continue
            if etat.statut == "nouveau" and e["en_attente"]:
                continue
            resultat.append(e)

        if etat.colonne_tri:
            def cle(e):
                d = e["dossier"]
                mapping = {
                    "numero": d["numero_formate"], "commune": d["commune"], "societe": e["societe"] or "zzz",
                    "urgence": e["restant"] if e["restant"] is not None else 999999, "statut": e["en_attente"],
                }
                if etat.colonne_tri == "date_depart":
                    try: return datetime.strptime(d["date_depart"], "%Y-%m-%d")
                    except Exception: return datetime.min
                return mapping.get(etat.colonne_tri, "")
            resultat.sort(key=cle, reverse=etat.tri_inverse)
        return resultat

    def _rafraichir_filtrage(self):
        self.afficher_etape_selection()

    def afficher_etape_selection(self):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"📋  {len(self.dossiers_csv)} dossier(s) en attente")

        dossiers_attente = charger_attente()
        enrichis = self._dossiers_enrichis(dossiers_attente)
        communes_disponibles = sorted({d["commune"] for d in self.dossiers_csv if d.get("commune")})
        societes_disponibles = sorted({e["societe"] for e in enrichis if e["societe"]})

        creer_barre_filtres(self.cadre_contenu, self.COULEURS["bg"], self.etat_filtre, communes_disponibles,
                             societes_disponibles, True, self._rafraichir_filtrage)

        lignes_affichees = self._filtrer_et_trier(enrichis)
        self.dossiers_affiches = [e["dossier"] for e in lignes_affichees]

        tk.Label(self.cadre_contenu, text=f"{len(lignes_affichees)} résultat(s) après filtrage", bg=self.COULEURS["bg"],
                 fg="#666666", font=("Arial", 8, "italic")).pack(anchor="w", padx=22, pady=(2, 0))

        cadre_entete = tk.Frame(self.cadre_contenu, bg="#37474F")
        cadre_entete.pack(fill="x", padx=20, pady=(6, 0))
        tk.Label(cadre_entete, text="", width=3, bg="#37474F").pack(side="left", padx=3)
        entete_colonne_triable(cadre_entete, "Numéro de permis", 24, "numero", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Commune", 22, "commune", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Société", 10, "societe", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Départ", 10, "date_depart", self.etat_filtre, self._trier)
        tk.Label(cadre_entete, text="Limite", width=10, bg="#37474F", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=3)
        entete_colonne_triable(cadre_entete, "Délai", 10, "urgence", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Statut", 33, "statut", self.etat_filtre, self._trier)

        cadre_scroll = tk.Frame(self.cadre_contenu)
        cadre_scroll.pack(fill="both", expand=True, padx=20)
        scrollbar = tk.Scrollbar(cadre_scroll)
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(cadre_scroll, yscrollcommand=scrollbar.set, bg=self.COULEURS["bg"])
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=canvas.yview)
        cadre_liste = tk.Frame(canvas, bg=self.COULEURS["bg"])
        canvas.create_window((0, 0), window=cadre_liste, anchor="nw")
        self._canvas_actif = canvas

        self.variables = []
        for i, e in enumerate(lignes_affichees):
            dossier = e["dossier"]
            numero = dossier["numero_formate"]
            var = tk.BooleanVar(value=(numero in self.selection_persistante))
            self.variables.append(var)

            def _maj(*_a, num=numero, v=var):
                if v.get(): self.selection_persistante.add(num)
                else: self.selection_persistante.discard(num)
            var.trace_add("write", _maj)

            societe = e["societe"]
            couleur_fond = "#f9f9f9" if i % 2 == 0 else "white"
            date_limite = calculer_date_limite_avisau(dossier["date_depart"], societe)
            texte_restant, coul_rest = e["texte_restant"], e["couleur_restant"]
            try:
                date_dep_fmt = datetime.strptime(dossier["date_depart"], "%Y-%m-%d").strftime("%d/%m") if "-" in dossier["date_depart"] else dossier["date_depart"]
            except Exception:
                date_dep_fmt = dossier["date_depart"]

            statut_txt, statut_fg = "", "#333333"
            if e["en_attente"]:
                m = e["motif"] or "En attente"
                statut_txt, statut_fg = (f"📂 {m[:45]}..." if len(m) > 45 else f"📂 {m}"), "#FF9800"

            tk.Checkbutton(cadre_liste, variable=var, bg=couleur_fond).grid(row=i, column=0, padx=5, pady=1)
            tk.Label(cadre_liste, text=numero, width=24, bg=couleur_fond, anchor="center", font=("Courier", 9)).grid(row=i, column=1, padx=3, pady=1)
            tk.Label(cadre_liste, text=dossier["commune"], width=22, bg=couleur_fond, anchor="center").grid(row=i, column=2, padx=3, pady=1)
            tk.Label(cadre_liste, text=f" {societe or '?'} ", width=10, bg=self.COULEURS.get(societe, "#9E9E9E"), fg="white", font=("Arial", 8, "bold"), anchor="center").grid(row=i, column=3, padx=3, pady=1)
            tk.Label(cadre_liste, text=date_dep_fmt, width=10, bg=couleur_fond, anchor="center").grid(row=i, column=4, padx=3, pady=1)
            tk.Label(cadre_liste, text=date_limite, width=10, bg=couleur_fond, anchor="center", font=("Arial", 9, "bold")).grid(row=i, column=5, padx=3, pady=1)
            tk.Label(cadre_liste, text=texte_restant, width=10, bg=couleur_fond, fg=coul_rest, anchor="center", font=("Arial", 9, "bold")).grid(row=i, column=6, padx=3, pady=1)
            tk.Label(cadre_liste, text=statut_txt, width=33, bg=couleur_fond, fg=statut_fg, anchor="center", font=("Arial", 9, "bold")).grid(row=i, column=7, padx=3, pady=1)

        cadre_liste.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

        self.label_compteur = tk.Label(self.cadre_contenu, text=f"{len(self.selection_persistante)} dossier(s) sélectionné(s)", font=("Arial", 10), fg="#666666", bg=self.COULEURS["bg"])
        self.label_compteur.pack(pady=4)
        for var in self.variables:
            var.trace_add("write", lambda *a: self.label_compteur.config(
                text=f"{len(self.selection_persistante)} dossier(s) sélectionné(s)",
                fg="#2196F3" if self.selection_persistante else "#666666"))

        cadre_boutons = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_boutons.pack(pady=8)
        tk.Button(cadre_boutons, text="✅  Tout sélectionner", command=lambda: [v.set(True) for v in self.variables], bg="#4CAF50", fg="white", font=("Arial", 10), width=18).grid(row=0, column=0, padx=6)
        tk.Button(cadre_boutons, text="☐  Tout désélectionner", command=lambda: [v.set(False) for v in self.variables], bg="#f44336", fg="white", font=("Arial", 10), width=18).grid(row=0, column=1, padx=6)
        tk.Button(cadre_boutons, text="▶  Traiter la sélection", command=self._confirmer_selection, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=18).grid(row=0, column=2, padx=6)
        tk.Button(cadre_boutons, text="🔄  Rafraîchir", command=self._rafraichir, bg="#607D8B", fg="white", font=("Arial", 10), width=18).grid(row=0, column=3, padx=6)
        tk.Button(cadre_boutons, text="✖  Quitter", command=self._quitter, bg="#9E9E9E", fg="white", font=("Arial", 10), width=18).grid(row=0, column=4, padx=6)
        self.fenetre.bind("<Escape>", lambda e: self._quitter())
        self.fenetre.bind("<Return>", lambda e: self._confirmer_selection())

    def _trier(self, colonne):
        self.etat_filtre.basculer_tri(colonne)
        self.afficher_etape_selection()

    def _confirmer_selection(self):
        index_par_numero = {d["numero_formate"]: d for d in self.dossiers_csv}
        dossiers = [index_par_numero[n] for n in self.selection_persistante if n in index_par_numero]
        if not dossiers:
            return self.mettre_a_jour_sous_titre("⚠️ Sélectionnez un dossier !", couleur="#FF9800")
        self.resultat["dossiers"], self.resultat["action"] = dossiers, "TRAITER"
        self.fenetre.quit()

    def _rafraichir(self):
        self.resultat["action"] = "REFRESH"
        self.fenetre.quit()

    def _quitter(self):
        self.resultat["action"] = "QUIT"
        self.fenetre.quit()

    def _creer_carte_info(self, donnees, societe):
        cadre_carte = tk.Frame(self.cadre_contenu, bg="white", pady=10, padx=20)
        cadre_carte.pack(fill="x", padx=30, pady=5)
        jours = DELAIS_AVISAU.get(societe, 21)

        f_l1 = tk.Frame(cadre_carte, bg="white")
        f_l1.pack(fill="x", pady=2)
        tk.Label(f_l1, text=f"  {societe}  ", bg=self.COULEURS.get(societe, "#9E9E9E"), fg="white", font=("Arial", 11, "bold"), padx=10, pady=3).pack(side="left", padx=(0, 15))
        champ_num = tk.Entry(f_l1, font=("Courier", 14, "bold"), fg="#1565C0", bg="white", bd=0, width=23)
        champ_num.insert(0, donnees.get("numero_formate", "")); champ_num.configure(state="readonly", readonlybackground="white"); champ_num.pack(side="left")
        champ_com = tk.Entry(f_l1, font=("Arial", 12, "bold"), fg="#333333", bg="white", bd=0, width=30)
        champ_com.insert(0, donnees.get("commune", "").upper()); champ_com.configure(state="readonly", readonlybackground="white"); champ_com.pack(side="left", padx=(60, 0))

        def ligne(label_g, val_g, label_d, val_d):
            f_ligne = tk.Frame(cadre_carte, bg="white"); f_ligne.pack(fill="x", pady=2)
            tk.Label(f_ligne, text=label_g, font=("Arial", 9, "bold"), bg="white", fg="#666666", width=16, anchor="w").pack(side="left")
            champ_g = tk.Entry(f_ligne, font=("Arial", 9), fg="#333333", bg="white", width=45, bd=0)
            champ_g.insert(0, re.sub(r'\s*\n\s*', ' - ', str(val_g).strip())); champ_g.configure(state="readonly", readonlybackground="white"); champ_g.pack(side="left")
            if label_d:
                f_d = tk.Frame(f_ligne, bg="white"); f_d.pack(side="right")
                tk.Label(f_d, text=label_d, font=("Arial", 9, "bold"), bg="white", fg="#666666").pack(side="left")
                champ_d = tk.Entry(f_d, font=("Arial", 9, "bold"), fg="#333333", bg="white", width=12, bd=0, justify="right")
                champ_d.insert(0, val_d); champ_d.configure(state="readonly", readonlybackground="white"); champ_d.pack(side="left")

        ligne("👤 Pétitionnaire :", donnees.get("nom_petitionnaire", ""), "📅 Date départ :", donnees.get("date_depart", ""))
        ligne("🏠 Adresse :", donnees.get("adresse_travaux", ""), f"⏱️ Délai ({jours}j) :", calculer_date_limite_avisau(donnees.get("date_depart", ""), societe))
        ligne("🗺️ Parcelle :", donnees.get("parcelle", ""), None, None)
        return cadre_carte

    def afficher_etape_preparation(self, donnees, societe):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre("⏳ Téléchargement du dossier en cours...", couleur="#FF9800")
        self._creer_carte_info(donnees, societe)
        tk.Label(self.cadre_contenu, text="\n\n⏳ Préparation du dossier en cours...\n(Téléchargement et Création des formulaires)",
                 font=("Arial", 13, "italic", "bold"), fg="#FF9800", bg=self.COULEURS["bg"]).pack(pady=30)
        self.fenetre.update()

    def _calculer_pression(self):
        try:
            val1 = float(self.press_mini_var.get().replace(',', '.'))
            val2 = float(self.press_tn_var.get().replace(',', '.'))
            self.press_res_var.set(f"{(val1 - val2) * COEFFICIENT_PRESSION:.2f}")
        except Exception:
            self.press_res_var.set("Erreur")

    def _demander_reserve(self, type_reseau, var_radio):
        valeur = var_radio.get()
        if valeur not in ["favorable avec réserves", "défavorable", "incomplet"]:
            return
        titre = "Réserve" if valeur == "favorable avec réserves" else valeur.capitalize()
        cle_reseau = "AEP" if type_reseau == "AEP" else "EU"
        reserves_categorie = self.reserves.get(cle_reseau, {}).get(valeur, {})
        valeur_initiale = self.resultat_avis.get(f"detail_{type_reseau.lower()}", "")

        def _valider(texte):
            self.resultat_avis[f"detail_{type_reseau.lower()}"] = texte

        def _annuler():
            var_radio.set("")

        construire_popup_reserve(self.fenetre, self.COULEURS["bg"], f"{titre} {type_reseau}", valeur_initiale,
                                  reserves_categorie, _valider, _annuler)

    def afficher_etape_avis(self, donnees, societe):
        self.societe_actuelle = societe
        self.vider_contenu()
        self.press_mini_var.set(""); self.press_tn_var.set(""); self.press_res_var.set("")
        self.nouveau_branchement_var.set(False); self.extension_logement_var.set(False); self.deja_consulte_var.set(False)
        self.mettre_a_jour_sous_titre(f"🏢  Instruction — {donnees['numero_formate']}", couleur="#BBDEFB")

        self._creer_carte_info(donnees, societe)
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)

        cadre_avis = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_avis.pack(fill="x", padx=30, pady=2)
        tk.Label(cadre_avis, text="💧  AVIS AEP", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#1565C0").pack(anchor="w", pady=(2, 2))
        self.avis_aep_var = tk.StringVar(value="")
        f_btn_aep = tk.Frame(cadre_avis, bg=self.COULEURS["bg"]); f_btn_aep.pack(anchor="w")

        styles = {"favorable": ("#4CAF50", "✅ Favorable"), "favorable avec réserves": ("#FF9800", "⚠️ Fav. réserves"),
                  "défavorable": ("#f44336", "❌ Défavorable"), "incomplet": ("#9C27B0", "📋 Incomplet"), "refus": ("#607D8B", "🚫 Refus")}
        for i, (val, (coul, txt)) in enumerate(styles.items()):
            tk.Radiobutton(f_btn_aep, text=txt, variable=self.avis_aep_var, value=val, bg=self.COULEURS["bg"],
                           activebackground=coul, selectcolor=coul, font=("Arial", 10), indicatoron=0, width=18, pady=4,
                           command=lambda: self._demander_reserve("AEP", self.avis_aep_var)).grid(row=0, column=i, padx=4)

        self.avis_eu_var = tk.StringVar(value=""); self.contrat_var = tk.StringVar(value="")
        self.surf_anc_var = tk.StringVar(value=""); self.surf_nouv_var = tk.StringVar(value="")

        if societe in ["SAOM", "SAEM"]:
            ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
            cadre_avis_eu = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_avis_eu.pack(fill="x", padx=30, pady=2)
            tk.Label(cadre_avis_eu, text=f"🌊  AVIS EU ({societe})", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg=self.COULEURS.get(societe)).pack(anchor="w", pady=(2, 2))
            f_btn_eu = tk.Frame(cadre_avis_eu, bg=self.COULEURS["bg"]); f_btn_eu.pack(anchor="w")
            for i, (val, (coul, txt)) in enumerate(styles.items()):
                tk.Radiobutton(f_btn_eu, text=txt, variable=self.avis_eu_var, value=val, bg=self.COULEURS["bg"],
                               activebackground=coul, selectcolor=coul, font=("Arial", 10), indicatoron=0, width=18, pady=4,
                               command=lambda: self._demander_reserve("EU", self.avis_eu_var)).grid(row=0, column=i, padx=4)

            ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
            cadre_pfac = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_pfac.pack(fill="x", padx=30, pady=2)
            tk.Label(cadre_pfac, text="📝 ELIGIBILITÉ PFAC", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#8D6E63").pack(anchor="w", pady=(2, 4))
            f1 = tk.Frame(cadre_pfac, bg=self.COULEURS["bg"]); f1.pack(fill="x", pady=2)
            tk.Label(f1, text="N° Contrat :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold"), width=12, anchor="w").pack(side="left")
            tk.Entry(f1, textvariable=self.contrat_var, font=("Arial", 10), width=30).pack(side="left", padx=5)
            f2 = tk.Frame(cadre_pfac, bg=self.COULEURS["bg"]); f2.pack(fill="x", pady=2)
            tk.Label(f2, text="CERFA :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold"), width=12, anchor="w").pack(side="left")
            tk.Label(f2, text="Surface ancienne :", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")
            tk.Entry(f2, textvariable=self.surf_anc_var, font=("Arial", 10), width=8).pack(side="left", padx=5)
            tk.Label(f2, text="m²   -   Surface nouvelle :", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")
            tk.Entry(f2, textvariable=self.surf_nouv_var, font=("Arial", 10), width=8).pack(side="left", padx=5)
            tk.Label(f2, text="m²", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        cadre_press = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_press.pack(fill="x", padx=30, pady=2)
        tk.Label(cadre_press, text="⏱️ ESTIMATION DE LA PRESSION", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#00796B").pack(anchor="w", pady=(2, 4))
        f_press = tk.Frame(cadre_press, bg=self.COULEURS["bg"]); f_press.pack(fill="x", pady=2)
        tk.Label(f_press, text="Pression Mini :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_mini_var, font=("Arial", 10), width=10).pack(side="left", padx=(5, 15))
        tk.Label(f_press, text="Terrain Naturel :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_tn_var, font=("Arial", 10), width=10).pack(side="left", padx=5)
        tk.Button(f_press, text="🗜️ Calculer", command=self._calculer_pression, bg="#607D8B", fg="white", font=("Arial", 9, "bold"), padx=10, pady=2).pack(side="left", padx=15)
        tk.Label(f_press, text="=", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        champ_press_res = tk.Entry(f_press, textvariable=self.press_res_var, font=("Arial", 11, "bold"), fg="#1565C0", bg="white", width=8, bd=0)
        champ_press_res.configure(state="readonly", readonlybackground="white"); champ_press_res.pack(side="left", padx=5)
        tk.Label(f_press, text="Bars", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=10)
        f_cb = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); f_cb.pack(pady=5)
        tk.Checkbutton(f_cb, text="Nouveau Branchement", variable=self.nouveau_branchement_var, bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left", padx=20)
        tk.Checkbutton(f_cb, text="Extension logement", variable=self.extension_logement_var, bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left", padx=20)
        tk.Checkbutton(f_cb, text="Déjà consulté", variable=self.deja_consulte_var, bg=self.COULEURS["bg"], fg="#f44336", font=("Arial", 10, "bold")).pack(side="left", padx=20)

        cadre_actions = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_actions.pack(pady=10)
        tk.Button(cadre_actions, text="📤  Envoyer le dossier", command=self._valider_avis, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=22, pady=6).grid(row=0, column=0, padx=10)
        tk.Button(cadre_actions, text="📦  Mettre en attente", command=self._mettre_en_attente, bg="#FF9800", fg="white", font=("Arial", 10), width=22, pady=6).grid(row=0, column=1, padx=10)
        self.label_erreur_avis = tk.Label(self.cadre_contenu, text="", font=("Arial", 10), fg="#f44336", bg=self.COULEURS["bg"])
        self.label_erreur_avis.pack()

    def _valider_avis(self):
        if not self.avis_aep_var.get():
            return self.label_erreur_avis.config(text="⚠️  Veuillez sélectionner un avis AEP !")
        if self.societe_actuelle in ["SAOM", "SAEM"] and not self.avis_eu_var.get():
            return self.label_erreur_avis.config(text="⚠️  Veuillez sélectionner un avis EU !")

        if self.deja_consulte_var.get():
            popup = tk.Toplevel(self.fenetre)
            popup.title("Dossier déjà traité")
            popup.geometry("450x150")
            popup.configure(bg=self.COULEURS["bg"])
            popup.transient(self.fenetre); popup.grab_set()
            popup.geometry(f"+{self.fenetre.winfo_x() + 325}+{self.fenetre.winfo_y() + 300}")
            tk.Label(popup, text="Dossier déjà traité : Remplir tableau de suivi avant de transmettre le dossier.",
                     bg=self.COULEURS["bg"], fg="#f44336", font=("Arial", 11, "bold"), wraplength=400).pack(pady=20)

            def continuer():
                popup.destroy()
                self._finaliser_validation_avis()

            tk.Button(popup, text="Continuer", command=continuer, bg="#2196F3", fg="white", width=12, font=("Arial", 9, "bold")).pack(pady=10)
        else:
            self._finaliser_validation_avis()

    def _finaliser_validation_avis(self):
        self.resultat_avis.update({
            "avis_aep": self.avis_aep_var.get(), "avis_eu": self.avis_eu_var.get(),
            "pfac_contrat": self.contrat_var.get(), "pfac_surf_anc": self.surf_anc_var.get(), "pfac_surf_nouv": self.surf_nouv_var.get(),
            "nouveau_br": self.nouveau_branchement_var.get(), "ext_log": self.extension_logement_var.get(),
            "deja_consulte": self.deja_consulte_var.get(), "action": "ENVOYER"
        })
        self.fenetre.quit()

    def _mettre_en_attente(self):
        popup = tk.Toplevel(self.fenetre)
        popup.title("Mise en attente")
        popup.geometry("400x150")
        popup.configure(bg=self.COULEURS["bg"])
        popup.transient(self.fenetre); popup.grab_set(); popup.update_idletasks()
        popup.geometry(f"+{self.fenetre.winfo_x() + (self.fenetre.winfo_width()//2) - 200}+{self.fenetre.winfo_y() + (self.fenetre.winfo_height()//2) - 75}")

        tk.Label(popup, text="Motif de la mise en attente :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(pady=10)
        ent_motif = tk.Entry(popup, font=("Arial", 10), width=40); ent_motif.pack(pady=5); ent_motif.focus_set()

        def valider(event=None):
            self.resultat_avis["motif_attente"] = ent_motif.get().strip() or "En attente (sans motif)"
            self.resultat_avis["action"] = "ATTENTE"
            popup.destroy(); self.fenetre.quit()

        f_btn = tk.Frame(popup, bg=self.COULEURS["bg"]); f_btn.pack(pady=10)
        tk.Button(f_btn, text="Valider", command=valider, bg="#FF9800", fg="white", width=10, font=("Arial", 9, "bold")).pack(side="left", padx=10)
        tk.Button(f_btn, text="Annuler", command=popup.destroy, bg="#9E9E9E", fg="white", width=10, font=("Arial", 9, "bold")).pack(side="left", padx=10)
        popup.bind("<Return>", valider); popup.bind("<Escape>", lambda e: popup.destroy())

    def afficher_etape_traitement(self, numero_formate):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"⚙️  Envoi de l'avis — {numero_formate}", couleur="#BBDEFB")
        tk.Label(self.cadre_contenu, text="⚙️  Envoi de l'avis sur Avis'AU", font=("Arial", 13, "bold"), bg=self.COULEURS["bg"], fg="#1565C0").pack(pady=20)
        self.zone_logs = tk.Text(self.cadre_contenu, height=20, width=90, font=("Courier", 9), bg="#1E1E1E", fg="#FFFFFF", relief="flat")
        self.zone_logs.pack(padx=30, pady=5); self.zone_logs.config(state="disabled")
        self.fenetre.update()

    def ajouter_log(self, message):
        self.logger.info(message)
        if hasattr(self, "zone_logs"):
            self.zone_logs.config(state="normal")
            self.zone_logs.insert("end", message + "\n"); self.zone_logs.see("end")
            self.zone_logs.config(state="disabled"); self.fenetre.update()

    def relancer(self):
        self.fenetre.mainloop()

def verifier_et_connecter_avisau(page, config, logger):
    logger.info("🔍 Vérification connexion Avis'AU...")

    def _naviguer():
        page.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page.wait_for_load_state("domcontentloaded")
    avec_retry(_naviguer, logger=logger, description="Ouverture page Avis'AU")

    if "login" in page.url:
        logger.info("❌ Non connecté - Saisie des identifiants...")

        def _se_connecter():
            bouton_connexion = page.locator("button.fr-btn.fr-connect.cerbere-connect")
            if bouton_connexion.count() > 0:
                bouton_connexion.first.click()
                page.wait_for_load_state("domcontentloaded")
            page.fill("#login", config["identifiant"])
            page.fill("#password", config["mot_de_passe"])
            page.click("#btnConnexion")
            page.wait_for_url("**/consultation**", timeout=TIMEOUT_COURT * 3)
            page.wait_for_load_state("domcontentloaded")
        avec_retry(_se_connecter, logger=logger, description="Connexion Avis'AU")
        logger.succes("✅ Connecté à Avis'AU !")
    else:
        logger.succes("✅ Déjà connecté !")


def selectionner_service_et_telecharger_csv_avisau(page, config, logger):
    logger.info("📥 Lancement du téléchargement de la liste CSV Avis'AU...")
    if "consultation" not in page.url or "/" in page.url.split("consultation")[1]:
        page.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page.wait_for_load_state("domcontentloaded")
    try:
        select_locator = page.locator("#serviceConsultableSelect")
        select_locator.wait_for(state="visible", timeout=TIMEOUT_COURT)
        select_locator.select_option(label=config["service"])
        page.wait_for_timeout(1000)
    except Exception:
        pass

    chemin_csv = Path(config["dossier_telechargement"]) / "liste_consultations.csv"

    def _telecharger():
        with page.expect_download(timeout=TIMEOUT_LONG) as download_info:
            try:
                btn_dl = page.get_by_role("button", name=re.compile("télécharger", re.IGNORECASE))
                if btn_dl.count() > 0: btn_dl.first.click()
                else: page.locator("button:has-text('élécharger')").first.click()
            except Exception:
                page.click("#onglet-attente-metier-panel button:has-text('élécharger')")
        download_info.value.save_as(chemin_csv)
        # --- AJOUT : validation du contenu, sinon on force un retry ---
        if not chemin_csv.exists() or chemin_csv.stat().st_size == 0:
            raise Exception("Le fichier CSV téléchargé est vide ou introuvable — nouvelle tentative nécessaire.")
    avec_retry(_telecharger, logger=logger, description="Téléchargement CSV Avis'AU")


    logger.succes("✅ Fichier CSV téléchargé avec succès !")
    return chemin_csv


def lire_csv_avisau(chemin_csv, log):
    dossiers = []
    try:
        with open(chemin_csv, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=";")
            if not reader.fieldnames:
                log.warning("⚠️ Le fichier CSV téléchargé est vide.")
                return dossiers
            for ligne in reader:
                numero_brut = ligne.get("Numéro de dossier", "").strip()
                if not numero_brut: continue
                dossiers.append({
                    "date_depart": ligne.get("Date de départ du délai", "").strip(),
                    "identifiant": ligne.get("Identifiant de la consultation", "").strip(),
                    "numero_brut": numero_brut, "numero_formate": formater_numero_dossier_avisau(numero_brut),
                    "commune": ligne.get("Nom de la commune", "").strip()
                })
    except Exception as e:
        log.erreur(f"❌ Erreur lors de la lecture du CSV: {e}")
    return dossiers


def notifier_echeances_proches_avisau(dossiers_csv, logger, interface=None):
    compte = sum(1 for d in dossiers_csv if (calculer_urgence_avisau(d["date_depart"], get_societe_avisau(d["commune"]))[0] or 999) <= SEUIL_ALERTE_JOURS)
    if compte > 0:
        message = f"{compte} dossier(s) Avis'AU à échéance ≤ {SEUIL_ALERTE_JOURS} jour(s) (ou en retard)."
        logger.warning(f"⏰ {message}")
        afficher_notification("⏰ Échéances proches — Avis'AU", message, type_="warning", parent=interface.fenetre if interface else None)


def trouver_et_ouvrir_dossier_avisau(page, dossier, config, logger, service_cible=None):
    num = dossier["numero_brut"]
    service_actuel = service_cible if service_cible else config["service"]
    logger.info(f"🔍 Recherche du dossier {num} sur le tableau de bord ({service_actuel})...")

    if "consultation" not in page.url or "/" in page.url.split("consultation")[1]:
        page.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page.wait_for_load_state("domcontentloaded")
    try:
        select_locator = page.locator("#serviceConsultableSelect")
        select_locator.wait_for(state="visible", timeout=TIMEOUT_COURT)
        select_locator.select_option(label=service_actuel)
        page.wait_for_timeout(1000)
    except Exception:
        pass

    try:
        while True:
            page.wait_for_selector("table > tbody", timeout=TIMEOUT_MOYEN)
            ligne_cible = page.locator(f"table > tbody > tr:has-text('{num}')")
            try:
                ligne_cible.first.wait_for(state="visible", timeout=4000)
                ligne_cible.first.click()
                page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
                logger.succes(f"✅ Dossier {num} trouvé et ouvert !")
                return True, page.url
            except Exception:
                pass
            btn_suiv = page.locator("button[aria-label='Page suivante']")
            if btn_suiv.count() > 0 and btn_suiv.is_enabled():
                btn_suiv.click(); page.wait_for_timeout(1500)
            else:
                logger.warning(f"❌ Dossier {num} introuvable sur le service {service_actuel}.")
                return False, None
    except Exception as e:
        logger.erreur(f"⚠️ Erreur critique recherche dossier : {e}")
        return False, None


def extraire_donnees_dossier_avisau(page, config, logger):
    logger.info("📄 Attente de l'apparition des informations sur la page...")
    try:
        page.wait_for_selector("text='Numéro de dossier'", timeout=TIMEOUT_MOYEN)
    except Exception:
        logger.warning("⚠️ Le texte 'Numéro de dossier' n'est pas apparu à temps.")
    try:
        for i in range(page.get_by_text("Voir plus").count()):
            try: page.get_by_text("Voir plus").nth(i).click()
            except Exception: pass
        page.wait_for_timeout(500)
        selectors = config.get("selectors", {})

        def get_text(sel, first_line_only=False):
            if not sel: return ""
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    txt = loc.first.inner_text().strip()
                    return txt.split("\n")[0].strip() if first_line_only else txt.replace("\n", " - ")
            except Exception as e:
                logger.warning(f"Erreur sélecteur {sel} : {e}")
            return ""

        rue_pet, ville_pet = get_text(selectors.get("rue_petitionnaire")), get_text(selectors.get("ville_petitionnaire"))
        adresse_pet_finale = ", ".join([p for p in [rue_pet, ville_pet] if p])

        return {"numero_brut": "ERR", "numero_formate": "ERR", "adresse_travaux": get_text(selectors.get("adresse_travaux"), True),
                "nom_petitionnaire": get_text(selectors.get("petitionnaire")), "parcelle": get_text(selectors.get("parcelle")) or "N/A",
                "nature_travaux": get_text(selectors.get("nature_travaux")), "adresse_petitionnaire": adresse_pet_finale, "commune": ""}
    except Exception as e:
        logger.erreur(f"❌ Erreur globale extraction : {e}")
        return {"numero_brut": "ERR", "numero_formate": "ERR", "adresse_travaux": "", "nom_petitionnaire": "",
                "parcelle": "", "nature_travaux": "", "adresse_petitionnaire": "", "commune": ""}


def preparer_dossier_pipeline_avisau(page, d_brut, config, interface, dossiers_attente, logger):
    num_form = d_brut.get("numero_formate")
    is_attente = num_form in dossiers_attente
    infos_attente = dossiers_attente.get(num_form)

    trouve, _ = trouver_et_ouvrir_dossier_avisau(page, d_brut, config, logger)
    if not trouve: return None

    donnees = extraire_donnees_dossier_avisau(page, config, logger)
    donnees["numero_brut"], donnees["numero_formate"], donnees["commune"], donnees["date_depart"] = \
        d_brut["numero_brut"], num_form, d_brut["commune"], d_brut["date_depart"]
    interface.afficher_etape_preparation(donnees, get_societe_avisau(donnees["commune"]))

    dossier_p = form_p = None
    if is_attente and infos_attente:
        logger.info(f"📂 Dossier {num_form} repris de l'attente.")
        dossier_p = Path(infos_attente["chemin"])
        if dossier_p.exists():
            candidat = dossier_p / f"Formulaire_{num_form}.pdf"
            form_p = candidat if candidat.exists() else None
        else:
            logger.warning("⚠️ Dossier local en attente introuvable ! Re-téléchargement forcé.")
            is_attente = False

    if not is_attente:
        logger.info("📥 Lancement du téléchargement des pièces jointes (ZIP)...")
        try: page.click("#conteneur-pj-dossier > div > fieldset > div > div:nth-child(2) > label")
        except Exception: pass
        page.wait_for_timeout(1000)

        chemin_zip = Path(config["dossier_telechargement"]) / "pieces_temp.zip"

        def _telecharger_zip():
            with page.expect_download(timeout=TIMEOUT_TELECHARGEMENT) as dl_info:
                try: page.get_by_role("button", name="Télécharger").first.click()
                except Exception: page.click("#conteneur-pj-dossier > div > div > button")
            dl_info.value.save_as(chemin_zip)
        avec_retry(_telecharger_zip, logger=logger, description="Téléchargement ZIP pièces jointes")

        logger.succes("📦 ZIP téléchargé. Création du dossier local...")
        nom_dos = nettoyer_nom_dossier(f"{donnees['numero_formate']}-{donnees['adresse_travaux']}")
        dossier_p = Path(config["dossier_destination"]) / nom_dos
        dossier_p.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(chemin_zip, "r") as z: z.extractall(dossier_p)
        os.remove(chemin_zip)

        logger.info("📝 Pré-remplissage du formulaire PDF avec fillpdfs...")
        c_mod = str(Path(config["formulaire_pdf"]))
        c_rem = str(Path(config["formulaire_dossier_sortie"]) / f"Formulaire_{donnees['numero_formate']}.pdf")
        societe_pdf = get_societe_avisau(donnees.get("commune", ""))
        nom_commune_formatee = normaliser_commune(donnees.get("commune", "")).upper()

        donnees_pdf = {
            "numéro dossier": donnees.get("numero_formate", ""),
            "nom pétitionnaire": donnees.get("nom_petitionnaire", ""),
            "adresse pétitionnaire": donnees.get("adresse_petitionnaire", ""),
            "nature travaux": donnees.get("nature_travaux", ""),
            "adresse travaux": donnees.get("adresse_travaux", ""),
        }

        # On ne renseigne QUE le champ correspondant à la société détectée. Les deux
        # autres champs (CommuneSEMM/CommuneSAOM/CommuneSAEM) ne sont volontairement
        # pas ajoutés au dictionnaire, afin de conserver leur valeur par défaut "-"
        # telle que définie dans le modèle PDF.
        if societe_pdf in ["SEMM", "SAOM", "SAEM"]:
            donnees_pdf[f"Commune{societe_pdf}"] = nom_commune_formatee
        else:
            logger.warning(f"⚠️ Société non reconnue pour la commune '{donnees.get('commune','')}' : aucun champ Commune rempli.")

        try:
            avec_retry(lambda: remplir_pdf_champs_cibles(c_mod, c_rem, donnees_pdf, logger=logger), logger=logger, description="Pré-remplissage PDF")
            form_p = Path(c_rem)
        except Exception as e:
            logger.warning(f"⚠️ Erreur pré-remplissage PDF ({e}).")
            form_p = None

    return {"dossier_brut": d_brut, "donnees": donnees, "chemin_dossier": dossier_p, "formulaire": form_p, "etait_en_attente": is_attente}


def sauvegarder_ligne_pour_sheets_avisau(donnees, avis_aep, avis_eu, donnees_pfac, donnees_sup, type_export, logger):
    try:
        date_auj = datetime.now().strftime("%d/%m/%Y")
        comm_fmt = normaliser_commune(donnees.get("commune", "")).upper()
        try: date_dep = datetime.strptime(donnees.get("date_depart", ""), "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception: date_dep = donnees.get("date_depart", "")

        d_inc = date_auj if (avis_aep == "incomplet" or avis_eu == "incomplet") else ""
        d_def = date_auj if not d_inc else ""

        col = [""] * 20
        col[1], col[2], col[4] = type_export, comm_fmt, donnees.get("numero_formate", "")
        if donnees_sup:
            col[5] = "True" if donnees_sup.get("nouveau_br") else ""
            col[6] = "True" if donnees_sup.get("ext_log") else ""
        col[7], col[8], col[9] = date_dep, d_inc, d_def
        if get_societe_avisau(donnees.get("commune", "")) == "SEMM": col[14] = "Hors-contrat"
        if donnees_pfac:
            col[15], col[16], col[17] = donnees_pfac.get("surf_anc", ""), donnees_pfac.get("surf_nouv", ""), donnees_pfac.get("contrat", "")
        col[18], col[19] = donnees.get("adresse_travaux", ""), donnees.get("nom_petitionnaire", "")

        with open(FICHIER_EXPORT_SHEETS_AVISAU, "a", encoding="utf-8") as f:
            f.write("\t".join(col) + "\n")
        logger.succes(f"💾 Ligne ajoutée au fichier TSV avec succès (Dossier {col[4]}).")
    except Exception as e:
        logger.erreur(f"❌ Erreur lors de l'écriture TSV : {e}")


def refuser_prise_en_compte_avisau(page, interface, logger):
    interface.ajouter_log("🖱️ Clic sur 'Refuser la prise en compte métier'...")

    def _refuser():
        try: page.get_by_role("button", name="Refuser la prise en compte métier").click()
        except Exception: page.locator("button:has-text('Refuser')").first.click()
    avec_retry(_refuser, logger=logger, description="Refus de la prise en compte métier")

    page.wait_for_timeout(1000)
    interface.ajouter_log("⏳ En attente de votre confirmation manuelle sur le site...")
    while True:
        try:
            if not page.locator("button:has-text('Refuser')").first.is_visible(): break
        except Exception: break
        try: interface.fenetre.update()
        except tk.TclError: raise FenetreFermeeException("Fenêtre fermée pendant l'attente de confirmation du refus.")
        page.wait_for_timeout(500)
    interface.ajouter_log("✅ Refus détecté et validé !")


def emettre_avis_sur_page_avisau(page, avis, prefixe_texte, chemin_dossier, numero_formate, interface, logger,
                                  exclure_suffixe_pdf=None, suffixe_pdf_recherche=None):
    interface.ajouter_log("🖱️ Prise en charge du dossier...")

    def _accepter_pec():
        try: page.get_by_role("button", name="Accepter la prise en compte métier").click()
        except Exception: page.locator("text='Accepter la prise en compte métier'").first.click()
    avec_retry(_accepter_pec, logger=logger, description="Acceptation de la prise en compte métier")
    page.wait_for_timeout(1000)

    for _ in range(2):
        try:
            page.get_by_role("button", name="Accepter", exact=True).click(timeout=3000)
            page.wait_for_timeout(1000)
            page.get_by_role("button", name="Confirmer", exact=True).click(timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    interface.ajouter_log("⏳ Ouverture du formulaire 'Émettre un avis'...")

    def _ouvrir_formulaire():
        try: page.get_by_role("button", name="Émettre un avis").click(timeout=10000)
        except Exception: page.locator("text='Émettre un avis'").first.click()
    avec_retry(_ouvrir_formulaire, logger=logger, description="Ouverture du formulaire d'avis")
    page.wait_for_timeout(2000)

    interface.ajouter_log("📝 Remplissage du formulaire...")
    page.wait_for_selector("#nature", timeout=TIMEOUT_MOYEN)

    def _remplir():
        page.select_option("#nature", index=MAPPING_NATURE_INDEX_AVISAU[avis])
        page.select_option("#type", index=1)
        page.fill("#qualite", QUALITE_SIGNATAIRE)
        page.fill("#texteAvis", f"{avis.capitalize()} {prefixe_texte}")
    avec_retry(_remplir, logger=logger, description="Remplissage du formulaire d'avis")

    if avis in ["favorable avec réserves", "défavorable", "incomplet"]:
        texte_detail = interface.resultat_avis.get(f"detail_{prefixe_texte.lower()}", "")
        if texte_detail:
            try: page.fill("#fondement", texte_detail)
            except Exception as e: interface.ajouter_log(f"⚠️ Motif non renseigné automatiquement : {e}")

    def _chercher_pdf():
        if suffixe_pdf_recherche: return trouver_pdf_par_suffixe(chemin_dossier, suffixe_pdf_recherche)
        return trouver_pdf_avis(chemin_dossier, numero_formate, exclure_suffixe=exclure_suffixe_pdf)

    pdf_avis = _chercher_pdf()
    while not pdf_avis:
        try: interface.fenetre.update()
        except tk.TclError: raise FenetreFermeeException("Fenêtre fermée pendant l'attente du PDF d'avis.")
        page.wait_for_timeout(1000)
        pdf_avis = _chercher_pdf()

    interface.ajouter_log("📎 Téléversement du PDF...")

    def _uploader():
        with page.expect_file_chooser() as fc:
            try: page.get_by_text("Téléverser").click(timeout=3000)
            except Exception: page.locator("button:has-text('Téléverser')").click()
        fc.value.set_files(pdf_avis)
    avec_retry(_uploader, logger=logger, description="Téléversement du PDF d'avis")
    page.wait_for_timeout(2000)

    interface.ajouter_log("📤 PDF téléversé ! En attente de validation manuelle...")
    while True:
        try:
            if not page.locator("avisau-modal-emission-avis").is_visible(): break
        except Exception: break
        try: interface.fenetre.update()
        except tk.TclError: raise FenetreFermeeException("Fenêtre fermée pendant l'attente de validation de l'avis.")
        page.wait_for_timeout(500)
    interface.ajouter_log("✅ Validation détectée et enregistrée !")


def traiter_avis_complet_avisau(page, donnees, chemin_dossier, config, interface, logger):
    commune = donnees["commune"]
    societe = get_societe_avisau(commune)
    interface.afficher_etape_traitement(donnees["numero_formate"])

    avis_aep, avis_eu = interface.resultat_avis.get("avis_aep", ""), interface.resultat_avis.get("avis_eu", "")
    donnees_pfac = {"contrat": interface.resultat_avis.get("pfac_contrat", ""), "surf_anc": interface.resultat_avis.get("pfac_surf_anc", ""), "surf_nouv": interface.resultat_avis.get("pfac_surf_nouv", "")}
    donnees_sup = {"nouveau_br": interface.resultat_avis.get("nouveau_br", False), "ext_log": interface.resultat_avis.get("ext_log", False), "deja_consulte": interface.resultat_avis.get("deja_consulte", False)}
    type_export = "Avis'Au AEP & EU" if societe in ["SAOM", "SAEM"] else "Avis'Au AEP"

    interface.ajouter_log(f"\n📋 Traitement de l'avis AEP : {avis_aep}")
    if avis_aep == "refus":
        refuser_prise_en_compte_avisau(page, interface, logger)
    else:
        emettre_avis_sur_page_avisau(page, avis_aep, "AEP", chemin_dossier, donnees["numero_formate"], interface, logger, exclure_suffixe_pdf=" EU")

    if societe in ["SAOM", "SAEM"] and avis_eu:
        interface.ajouter_log(f"\n🔄 Changement de service vers {societe}...")
        trouve, _ = trouver_et_ouvrir_dossier_avisau(page, {"numero_brut": donnees["numero_brut"]}, config, logger, service_cible=SERVICES_AVISAU[societe])
        if trouve:
            interface.ajouter_log(f"📋 Traitement de l'avis EU : {avis_eu}")
            if avis_eu == "refus":
                refuser_prise_en_compte_avisau(page, interface, logger)
            else:
                emettre_avis_sur_page_avisau(page, avis_eu, "EU", chemin_dossier, donnees["numero_formate"], interface, logger, suffixe_pdf_recherche=" EU")
        else:
            interface.ajouter_log(f"⚠️ DOSSIER EU NON TROUVÉ DANS {societe} ! Export forcé en AEP.")
            type_export = "Avis'Au AEP"

        try:
            page.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
            page.wait_for_load_state("domcontentloaded")
            select_locator = page.locator("#serviceConsultableSelect")
            select_locator.wait_for(state="visible", timeout=TIMEOUT_COURT)
            select_locator.select_option(label=config["service"])
            page.wait_for_timeout(1000)
        except Exception:
            pass
    elif societe in ["SAOM", "SAEM"] and not avis_eu:
        type_export = "Avis'Au AEP"

    return avis_aep, avis_eu, donnees_pfac, donnees_sup, type_export


def finaliser_dossier_background_avisau(data, config, stats, dossiers_attente, logger):
    if not data: return

    deja_consulte = data.get('donnees_sup', {}).get('deja_consulte', False)
    if not deja_consulte:
        sauvegarder_ligne_pour_sheets_avisau(data['donnees'], data['avis_aep'], data['avis_eu'],
                                              data.get('donnees_pfac', {}), data.get('donnees_sup', {}),
                                              data.get('type_export', "Avis'Au AEP"), logger)
    else:
        logger.info("ℹ️ Dossier coché 'Déjà consulté' : Écriture TSV ignorée.")

    dest_folder = Path(config["dossier_a_upload"]) / normaliser_commune(data['donnees']['commune'])
    dest_folder.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(data['chemin'], str(dest_folder / Path(data['chemin']).name))
    except Exception as e:
        logger.warning(f"⚠️ Déplacement du dossier échoué : {e}")

    if data.get('etait_en_attente') and data['donnees']['numero_formate'] in dossiers_attente:
        del dossiers_attente[data['donnees']['numero_formate']]
        sauvegarder_attente(dossiers_attente)

    stats.traites += 1
    if data['avis_aep'] == "incomplet" or data['avis_eu'] == "incomplet":
        stats.incomplets += 1
    if CHEMIN_QUEUE_JSON.exists():
        try: CHEMIN_QUEUE_JSON.unlink()
        except Exception: pass


def executer_avisau(config_global):
    config = construire_config_outil(config_global, "Avis'Au")
    reserves = config_global.get("Réserves", {})
    verifier_dossiers_config(config)
    valider_section(config, CLES_REQUISES_AVISAU, "Avis'Au")
    logger = Logger(config.get("dossier_logs", "logs"), "AVISAU")
    stats = StatsSessionAvisAU()
    dossiers_attente = charger_attente()

    with open(FICHIER_EXPORT_SHEETS_AVISAU, "w", encoding="utf-8") as f:
        pass

    try:
        with sync_playwright() as p:
            nettoyer_historique_chrome(config["chrome_profile"], logger)  # <-- AJOUT

            context = p.chromium.launch_persistent_context(
                user_data_dir=config["chrome_profile"], channel="chrome", headless=False, accept_downloads=True,
                downloads_path=config["dossier_telechargement"],
                args=["--disable-features=DownloadBubble,DownloadBubbleV2,DownloadShelf"]
            )
            page = context.new_page()

            try:
                verifier_et_connecter_avisau(page, config, logger)
                reprise = charger_attente(CHEMIN_QUEUE_JSON)
                if reprise:
                    finaliser_dossier_background_avisau(reprise, config, stats, dossiers_attente, logger)

                chemin_csv = selectionner_service_et_telecharger_csv_avisau(page, config, logger)
                dossiers_csv = lire_csv_avisau(chemin_csv, logger)
                interface = InterfaceAvisAU(dossiers_csv, logger, reserves)
                notifier_echeances_proches_avisau(dossiers_csv, logger, interface)
                historique_bg = None
                stealth_actif = False

                while True:
                    if interface.stealth_mode and not stealth_actif:
                        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
                        stealth_actif = True
                    elif not interface.stealth_mode and stealth_actif:
                        page.unroute("**/*")
                        stealth_actif = False

                    interface.resultat = {"dossiers": [], "action": "QUIT"}
                    interface.afficher_etape_selection()
                    interface.relancer()

                    action = interface.resultat["action"]
                    if action == "QUIT":
                        if historique_bg:
                            finaliser_dossier_background_avisau(historique_bg, config, stats, dossiers_attente, logger)
                        break
                    if action == "REFRESH":
                        logger.info("🔄 Rafraîchissement de la liste...")
                        chemin_csv = selectionner_service_et_telecharger_csv_avisau(page, config, logger)
                        interface.dossiers_csv = lire_csv_avisau(chemin_csv, logger)
                        notifier_echeances_proches_avisau(interface.dossiers_csv, logger, interface)
                        continue

                    dossiers_selectionnes = interface.resultat["dossiers"]
                    if not dossiers_selectionnes: break

                    try:
                        prep_courante = preparer_dossier_pipeline_avisau(page, dossiers_selectionnes[0], config, interface, dossiers_attente, logger)

                        for idx, d_select in enumerate(dossiers_selectionnes):
                            if not prep_courante:
                                if idx + 1 < len(dossiers_selectionnes):
                                    prep_courante = preparer_dossier_pipeline_avisau(page, dossiers_selectionnes[idx + 1], config, interface, dossiers_attente, logger)
                                continue

                            donnees, dossier_p, d_brut, form_p, is_attente = prep_courante["donnees"], prep_courante["chemin_dossier"], prep_courante["dossier_brut"], prep_courante["formulaire"], prep_courante["etait_en_attente"]

                            if form_p and Path(form_p).exists():
                                try: ouvrir_fichier_os(form_p)
                                except Exception as e: logger.warning(f"⚠️ Impossible d'ouvrir le formulaire : {e}")
                            ouvrir_fichiers_dossier(dossier_p, logger)

                            prep_suivante = preparer_dossier_pipeline_avisau(page, dossiers_selectionnes[idx + 1], config, interface, dossiers_attente, logger) if idx + 1 < len(dossiers_selectionnes) else None

                            trouve, _ = trouver_et_ouvrir_dossier_avisau(page, d_brut, config, logger)
                            if trouve:
                                societe = get_societe_avisau(donnees["commune"])
                                interface.afficher_etape_avis(donnees, societe)
                                interface.relancer()

                                act_avis = interface.resultat_avis.get("action")
                                if act_avis == "ATTENTE":
                                    d_attente = Path(config["dossier_en_attente"])
                                    d_attente.mkdir(parents=True, exist_ok=True)
                                    motif = interface.resultat_avis.get("motif_attente", "En attente")
                                    with open(dossier_p / f"{donnees['numero_formate']} - Motif en attente.txt", "w", encoding="utf-8") as f:
                                        f.write(motif)
                                    try: shutil.move(str(dossier_p), str(d_attente / dossier_p.name))
                                    except Exception as e: logger.warning(f"⚠️ Déplacement vers l'attente échoué : {e}")

                                    dossiers_attente[donnees["numero_formate"]] = {"motif": motif, "chemin": str(d_attente / dossier_p.name)}
                                    sauvegarder_attente(dossiers_attente)
                                    stats.en_attente += 1
                                    logger.succes(f"📦 Dossier {donnees['numero_formate']} mis en attente. Motif: {motif}")

                                elif act_avis == "ENVOYER":
                                    try:
                                        av_aep, av_eu, d_pfac, d_sup, typ_exp = traiter_avis_complet_avisau(page, donnees, str(dossier_p), config, interface, logger)
                                        historique_bg = {"numero_formate": donnees["numero_formate"], "chemin": str(dossier_p), "donnees": donnees,
                                                          "type": donnees["numero_brut"][:2].upper(), "avis_aep": av_aep, "avis_eu": av_eu,
                                                          "donnees_pfac": d_pfac, "donnees_sup": d_sup, "type_export": typ_exp, "etait_en_attente": is_attente}
                                        sauvegarder_attente(historique_bg, CHEMIN_QUEUE_JSON)
                                        finaliser_dossier_background_avisau(historique_bg, config, stats, dossiers_attente, logger)
                                        historique_bg = None
                                    except FenetreFermeeException as e:
                                        logger.warning(f"⚠️ Traitement interrompu par fermeture de fenêtre : {e}")
                                    except Exception as e:
                                        logger.erreur(f"❌ Erreur lors de l'envoi de l'avis pour {donnees['numero_formate']} : {e}")
                                        afficher_notification("❌ Erreur d'envoi", f"{donnees['numero_formate']} : {str(e)[:150]}", type_="erreur", parent=interface.fenetre)

                            prep_courante = prep_suivante

                        logger.info("🔄 Fin de la série : actualisation de la liste...")
                        chemin_csv = selectionner_service_et_telecharger_csv_avisau(page, config, logger)
                        interface.dossiers_csv = lire_csv_avisau(chemin_csv, logger)
                        notifier_echeances_proches_avisau(interface.dossiers_csv, logger, interface)

                    except FenetreFermeeException as e:
                        logger.warning(f"⚠️ Série interrompue par fermeture de fenêtre : {e}")
                        break

                stats.generer_rapport()
            finally:
                try: context.close()
                except Exception: pass
    except Exception as e:
        logger.erreur(f"❌ Erreur critique dans l'exécution d'Avis'AU : {e}")
        afficher_notification("❌ Erreur critique — Avis'AU", str(e)[:200], type_="erreur")
        raise
    finally:
        logger.fermer()


# ============================================================
# ============================================================
# MODULE : OPEN ADS
# ============================================================
# ============================================================

class StatsSessionOpenADS:
    def __init__(self):
        self.debut = datetime.now()
        self.traites = self.incomplets = self.en_attente = 0

    def generer_rapport(self):
        duree = datetime.now() - self.debut
        rapport = (
            f"\n--- SESSION OPENADS DU {datetime.now().strftime('%d/%m/%Y')} ---\n"
            f"⏱ Durée : {int(duree.total_seconds() // 60)} min\n"
            f"✅ Finalisés : {self.traites} | 📋 Incomplets : {self.incomplets} | 📦 Attente : {self.en_attente}\n"
        )
        with open(RECAP_FILE_AVISAU, "a", encoding="utf-8") as f:
            f.write(rapport)


class InterfaceOpenADS:
    def __init__(self, dossiers_csv, logger, reserves_aep):
        self.dossiers_csv = dossiers_csv
        self.logger = logger
        self.reserves = reserves_aep  # dict {"favorable avec réserves": {...}, "incomplet": {...}, "défavorable": {...}}
        self.resultat = {"dossiers": [], "action": "QUIT"}
        self.COULEURS = {"bg": "#F5F5F5", "titre": "#1565C0", "SEMM": "#2196F3"}
        self.STYLES_AVIS = {"favorable": ("#4CAF50", "✅  Favorable"), "favorable avec réserves": ("#FF9800", "⚠️  Fav. réserves"),
                             "défavorable": ("#f44336", "❌  Défavorable"), "incomplet": ("#9C27B0", "📋  Incomplet"), "refus": ("#607D8B", "🚫  Refus")}
        self.etat_filtre = EtatFiltreTri()
        self.dossiers_affiches = []
        self.selection_persistante = set()

        self.fenetre = tk.Tk()
        self.fenetre.title("AUTOMATISATION OPEN ADS — SEMM")
        self.fenetre.geometry("1220x680")
        self.fenetre.configure(bg=self.COULEURS["bg"])
        self.resultat_avis = {"avis": None, "action": None}
        self.avis_var = None
        self.press_mini_var, self.press_tn_var, self.press_res_var = tk.StringVar(), tk.StringVar(), tk.StringVar()

        self._canvas_actif = None
        self.fenetre.bind_all("<MouseWheel>", self._on_mousewheel)
        self.fenetre.update_idletasks()
        self.fenetre.geometry(f"1220x680+{(self.fenetre.winfo_screenwidth()//2)-(1220//2)}+{(self.fenetre.winfo_screenheight()//2)-(680//2)}")

        cadre_titre = tk.Frame(self.fenetre, bg=self.COULEURS["titre"], pady=10)
        cadre_titre.pack(fill="x")
        tk.Label(cadre_titre, text="🏗️  AUTOMATISATION OPEN ADS — SEMM", font=("Arial", 14, "bold"), bg=self.COULEURS["titre"], fg="white").pack()
        self.label_sous_titre = tk.Label(cadre_titre, text=f"📋  {len(dossiers_csv)} dossier(s) en attente", font=("Arial", 10), bg=self.COULEURS["titre"], fg="#BBDEFB")
        self.label_sous_titre.pack()
        self.cadre_contenu = tk.Frame(self.fenetre, bg=self.COULEURS["bg"])
        self.cadre_contenu.pack(fill="both", expand=True)

        self.afficher_etape_selection()

    def _on_mousewheel(self, event):
        if self._canvas_actif:
            try: self._canvas_actif.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception: pass

    def vider_contenu(self):
        for widget in self.cadre_contenu.winfo_children():
            widget.destroy()

    def mettre_a_jour_sous_titre(self, texte, couleur="#BBDEFB"):
        self.label_sous_titre.config(text=texte, fg=couleur)

    def _dossiers_enrichis(self, dossiers_attente):
        enrichis = []
        for d in self.dossiers_csv:
            restant, texte_restant, couleur_restant = calculer_urgence_openads(d.get("date_limite", ""))
            en_attente = d["numero_formate"] in dossiers_attente
            motif = dossiers_attente.get(d["numero_formate"], {}).get("motif", "") if en_attente else ""
            enrichis.append({"dossier": d, "restant": restant, "texte_restant": texte_restant, "couleur_restant": couleur_restant,
                              "en_attente": en_attente, "motif": motif, "categorie_urgence": categorie_urgence(restant)})
        return enrichis

    def _filtrer_et_trier(self, enrichis):
        etat = self.etat_filtre
        resultat = []
        for e in enrichis:
            d = e["dossier"]
            if etat.recherche and etat.recherche not in d["numero_formate"].lower() and etat.recherche not in d.get("nom_petitionnaire", "").lower():
                continue
            if etat.commune != "Toutes" and d.get("commune", "") != etat.commune:
                continue
            if etat.urgence != "toutes" and e["categorie_urgence"] != etat.urgence:
                continue
            if etat.statut == "attente" and not e["en_attente"]:
                continue
            if etat.statut == "nouveau" and e["en_attente"]:
                continue
            resultat.append(e)

        if etat.colonne_tri:
            def cle(e):
                d = e["dossier"]
                mapping = {"numero": d["numero_formate"], "petitionnaire": d.get("nom_petitionnaire", ""),
                           "adresse": d.get("adresse_travaux", ""), "urgence": e["restant"] if e["restant"] is not None else 999999,
                           "statut": e["en_attente"]}
                return mapping.get(etat.colonne_tri, "")
            resultat.sort(key=cle, reverse=etat.tri_inverse)
        return resultat

    def _rafraichir_filtrage(self):
        self.afficher_etape_selection()

    def afficher_etape_selection(self):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"📋  {len(self.dossiers_csv)} dossier(s) en attente")

        dossiers_attente = charger_attente()
        enrichis = self._dossiers_enrichis(dossiers_attente)
        communes_disponibles = sorted({d["dossier"].get("commune", "") for d in enrichis if d["dossier"].get("commune")})

        creer_barre_filtres(self.cadre_contenu, self.COULEURS["bg"], self.etat_filtre, communes_disponibles, [], False, self._rafraichir_filtrage)

        lignes_affichees = self._filtrer_et_trier(enrichis)
        self.dossiers_affiches = [e["dossier"] for e in lignes_affichees]

        tk.Label(self.cadre_contenu, text=f"{len(lignes_affichees)} résultat(s) après filtrage", bg=self.COULEURS["bg"],
                 fg="#666666", font=("Arial", 8, "italic")).pack(anchor="w", padx=22, pady=(2, 0))

        cadre_entete = tk.Frame(self.cadre_contenu, bg="#37474F")
        cadre_entete.pack(fill="x", padx=20, pady=(6, 0))
        tk.Label(cadre_entete, text="", width=3, bg="#37474F").pack(side="left", padx=3)
        entete_colonne_triable(cadre_entete, "Numéro de permis", 24, "numero", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Pétitionnaire", 22, "petitionnaire", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Adresse travaux", 30, "adresse", self.etat_filtre, self._trier)
        tk.Label(cadre_entete, text="Parcelles", width=18, bg="#37474F", fg="white", font=("Arial", 9, "bold")).pack(side="left", padx=3)
        entete_colonne_triable(cadre_entete, "Délai", 12, "urgence", self.etat_filtre, self._trier)
        entete_colonne_triable(cadre_entete, "Statut", 33, "statut", self.etat_filtre, self._trier)

        cadre_scroll = tk.Frame(self.cadre_contenu)
        cadre_scroll.pack(fill="both", expand=True, padx=20)
        scrollbar = tk.Scrollbar(cadre_scroll)
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(cadre_scroll, yscrollcommand=scrollbar.set, bg=self.COULEURS["bg"])
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=canvas.yview)
        cadre_liste = tk.Frame(canvas, bg=self.COULEURS["bg"])
        canvas.create_window((0, 0), window=cadre_liste, anchor="nw")
        self._canvas_actif = canvas

        self.variables = []
        for i, e in enumerate(lignes_affichees):
            dossier = e["dossier"]
            numero = dossier["numero_formate"]
            var = tk.BooleanVar(value=(numero in self.selection_persistante))
            self.variables.append(var)

            def _maj(*_a, num=numero, v=var):
                if v.get(): self.selection_persistante.add(num)
                else: self.selection_persistante.discard(num)
            var.trace_add("write", _maj)

            couleur_fond = "#f9f9f9" if i % 2 == 0 else "white"
            texte_restant, couleur_restant = e["texte_restant"], e["couleur_restant"]

            statut_txt, statut_fg = "", "#333333"
            if e["en_attente"]:
                m = e["motif"] or "En attente"
                statut_txt, statut_fg = (f"📂 {m[:45]}..." if len(m) > 45 else f"📂 {m}"), "#FF9800"

            tk.Checkbutton(cadre_liste, variable=var, bg=couleur_fond).grid(row=i, column=0, padx=5, pady=1)
            tk.Label(cadre_liste, text=numero, width=24, bg=couleur_fond, anchor="center", font=("Courier", 9)).grid(row=i, column=1, padx=3, pady=1)
            tk.Label(cadre_liste, text=dossier["nom_petitionnaire"], width=22, bg=couleur_fond, anchor="center").grid(row=i, column=2, padx=3, pady=1)
            tk.Label(cadre_liste, text=dossier["adresse_travaux"], width=30, bg=couleur_fond, anchor="center").grid(row=i, column=3, padx=3, pady=1)
            tk.Label(cadre_liste, text=dossier["references_cadastrales"], width=18, bg=couleur_fond, anchor="center", font=("Courier", 8)).grid(row=i, column=4, padx=3, pady=1)
            tk.Label(cadre_liste, text=texte_restant, width=12, bg=couleur_fond, fg=couleur_restant, anchor="center", font=("Arial", 9, "bold")).grid(row=i, column=5, padx=3, pady=1)
            tk.Label(cadre_liste, text=statut_txt, width=33, bg=couleur_fond, fg=statut_fg, anchor="center", font=("Arial", 9, "bold")).grid(row=i, column=6, padx=3, pady=1)

        cadre_liste.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

        self.label_compteur = tk.Label(self.cadre_contenu, text=f"{len(self.selection_persistante)} dossier(s) sélectionné(s)", font=("Arial", 10), fg="#666666", bg=self.COULEURS["bg"])
        self.label_compteur.pack(pady=4)
        for var in self.variables:
            var.trace_add("write", lambda *a: self.label_compteur.config(
                text=f"{len(self.selection_persistante)} dossier(s) sélectionné(s)",
                fg="#2196F3" if self.selection_persistante else "#666666"))

        cadre_boutons = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_boutons.pack(pady=8)
        tk.Button(cadre_boutons, text="✅  Tout sélectionner", command=lambda: [v.set(True) for v in self.variables], bg="#4CAF50", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=0, padx=6)
        tk.Button(cadre_boutons, text="☐  Tout désélectionner", command=lambda: [v.set(False) for v in self.variables], bg="#f44336", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=1, padx=6)
        tk.Button(cadre_boutons, text="▶  Traiter la sélection", command=self._confirmer_selection, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=18, pady=5).grid(row=0, column=2, padx=6)
        tk.Button(cadre_boutons, text="🔄  Rafraîchir", command=self._rafraichir, bg="#607D8B", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=3, padx=6)
        tk.Button(cadre_boutons, text="✖  Quitter", command=self._quitter, bg="#9E9E9E", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=4, padx=6)
        self.fenetre.bind("<Escape>", lambda e: self._quitter())
        self.fenetre.bind("<Return>", lambda e: self._confirmer_selection())

    def _trier(self, colonne):
        self.etat_filtre.basculer_tri(colonne)
        self.afficher_etape_selection()

    def _confirmer_selection(self):
        index_par_numero = {d["numero_formate"]: d for d in self.dossiers_csv}
        dossiers = [index_par_numero[n] for n in self.selection_persistante if n in index_par_numero]
        if not dossiers:
            return self.mettre_a_jour_sous_titre("⚠️ Sélectionnez au moins un dossier !", couleur="#FF9800")
        self.resultat["dossiers"], self.resultat["action"] = dossiers, "TRAITER"
        self.fenetre.quit()

    def _rafraichir(self):
        self.resultat["action"] = "REFRESH"
        self.fenetre.quit()

    def _quitter(self):
        self.resultat["action"] = "QUIT"
        self.fenetre.quit()

    def _calculer_pression(self):
        try:
            val1, val2 = float(self.press_mini_var.get().replace(',', '.')), float(self.press_tn_var.get().replace(',', '.'))
            self.press_res_var.set(f"{(val1 - val2) * COEFFICIENT_PRESSION:.2f}")
        except Exception:
            self.press_res_var.set("Erreur")

    def _demander_reserve(self, var_radio):
        valeur = var_radio.get()
        if valeur not in ["favorable avec réserves", "défavorable", "incomplet", "refus"]:
            return
        titre = "Réserve" if valeur == "favorable avec réserves" else valeur.capitalize()
        reserves_categorie = self.reserves.get(valeur, {})
        valeur_initiale = self.resultat_avis.get("detail_avis", "")

        def _valider(texte):
            self.resultat_avis["detail_avis"] = texte

        def _annuler():
            var_radio.set("")

        construire_popup_reserve(self.fenetre, self.COULEURS["bg"], f"{titre} - OpenADS", valeur_initiale, reserves_categorie, _valider, _annuler)

    def afficher_attente_dossier(self, donnees, message_statut):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"⏳ {message_statut}", couleur="#FF9800")
        cadre_carte = tk.Frame(self.cadre_contenu, bg="white", pady=15, padx=20)
        cadre_carte.pack(fill="x", padx=30, pady=15)
        tk.Label(cadre_carte, text="  SEMM  ", bg=self.COULEURS["SEMM"], fg="white", font=("Arial", 12, "bold"), padx=12, pady=5).grid(row=0, column=0, sticky="w", padx=(0, 15))
        tk.Label(cadre_carte, text=donnees["numero_formate"], font=("Courier", 14, "bold"), bg="white", fg="#1565C0").grid(row=0, column=1, sticky="w")
        infos = [("👤 Pétitionnaire", donnees.get("nom_petitionnaire", "")), ("🏠 Adresse travaux", donnees.get("adresse_travaux", "")),
                 ("📅 Date dépôt", donnees.get("date_depart", "")), ("⏱️ Date limite", donnees.get("date_limite", "")),
                 ("🗺️ Parcelles", donnees.get("references_cadastrales", ""))]
        for row, (label, valeur) in enumerate(infos, 1):
            tk.Label(cadre_carte, text=label, font=("Arial", 9, "bold"), bg="white", fg="#666666", width=18, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
            tk.Label(cadre_carte, text=valeur, font=("Arial", 9), bg="white", fg="#333333", anchor="w").grid(row=row, column=1, sticky="w", pady=2)
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        tk.Label(self.cadre_contenu, text=message_statut, font=("Arial", 11, "italic"), bg=self.COULEURS["bg"], fg="#607D8B").pack(pady=20)
        self.fenetre.update()

    def afficher_etape_avis(self, donnees):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"🏗️  Instruction — {donnees['numero_formate']}", couleur="#BBDEFB")
        cadre_carte = tk.Frame(self.cadre_contenu, bg="white", pady=10, padx=20)
        cadre_carte.pack(fill="x", padx=30, pady=5)
        f_l1 = tk.Frame(cadre_carte, bg="white"); f_l1.pack(fill="x", pady=2)
        tk.Label(f_l1, text="  SEMM  ", bg=self.COULEURS["SEMM"], fg="white", font=("Arial", 11, "bold"), padx=10, pady=3).pack(side="left", padx=(0, 15))
        champ_num = tk.Entry(f_l1, font=("Courier", 14, "bold"), fg="#1565C0", bg="white", bd=0, width=23)
        champ_num.insert(0, donnees["numero_formate"]); champ_num.configure(state="readonly", readonlybackground="white"); champ_num.pack(side="left")
        champ_com = tk.Entry(f_l1, font=("Arial", 12, "bold"), fg="#333333", bg="white", bd=0, width=30)
        champ_com.insert(0, donnees.get("commune", "").upper()); champ_com.configure(state="readonly", readonlybackground="white"); champ_com.pack(side="left")

        def creer_ligne_info(parent, lg, vg, ld, vd):
            f_ligne = tk.Frame(parent, bg="white"); f_ligne.pack(fill="x", pady=2)
            tk.Label(f_ligne, text=lg, font=("Arial", 9, "bold"), bg="white", fg="#666666", width=16, anchor="w").pack(side="left")
            champ_g = tk.Entry(f_ligne, font=("Arial", 9), fg="#333333", bg="white", width=45, bd=0)
            champ_g.insert(0, re.sub(r'\s*\n\s*', ' - ', str(vg).strip())); champ_g.configure(state="readonly", readonlybackground="white"); champ_g.pack(side="left")
            if ld:
                f_d = tk.Frame(f_ligne, bg="white"); f_d.pack(side="right")
                tk.Label(f_d, text=ld, font=("Arial", 9, "bold"), bg="white", fg="#666666").pack(side="left")
                champ_d = tk.Entry(f_d, font=("Arial", 9, "bold"), fg="#333333", bg="white", width=12, bd=0, justify="right")
                champ_d.insert(0, vd); champ_d.configure(state="readonly", readonlybackground="white"); champ_d.pack(side="left")

        creer_ligne_info(cadre_carte, "👤 Pétitionnaire :", donnees.get("nom_petitionnaire", ""), "📅 Date dépôt :", donnees.get("date_depart", ""))
        creer_ligne_info(cadre_carte, "🏠 Adresse :", donnees.get("adresse_travaux", ""), "⏱️ Délai :", donnees.get("date_limite", ""))
        creer_ligne_info(cadre_carte, "🗺️ Parcelle :", donnees.get("references_cadastrales", ""), None, None)
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)

        cadre_avis = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_avis.pack(fill="x", padx=30, pady=5)
        tk.Label(cadre_avis, text="💧  AVIS AEP", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#1565C0").pack(anchor="w", pady=(5, 3))
        self.avis_var = tk.StringVar(value="")
        cadre_boutons_avis = tk.Frame(cadre_avis, bg=self.COULEURS["bg"]); cadre_boutons_avis.pack(anchor="w")
        for i, (valeur, (couleur, texte)) in enumerate(self.STYLES_AVIS.items()):
            tk.Radiobutton(cadre_boutons_avis, text=texte, variable=self.avis_var, value=valeur, bg=self.COULEURS["bg"],
                           activebackground=couleur, selectcolor=couleur, fg="#333333", font=("Arial", 10), indicatoron=0,
                           width=22, pady=6, relief="groove", bd=1, command=lambda: self._demander_reserve(self.avis_var)).grid(row=0, column=i, padx=4)

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        cadre_press = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_press.pack(fill="x", padx=30, pady=2)
        tk.Label(cadre_press, text="⏱️ ESTIMATION DE LA PRESSION", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#00796B").pack(anchor="w", pady=(2, 4))
        f_press = tk.Frame(cadre_press, bg=self.COULEURS["bg"]); f_press.pack(fill="x", pady=2)
        tk.Label(f_press, text="Pression Mini :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_mini_var, font=("Arial", 10), width=10).pack(side="left", padx=(5, 15))
        tk.Label(f_press, text="Terrain Naturel :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_tn_var, font=("Arial", 10), width=10).pack(side="left", padx=5)
        tk.Button(f_press, text="🗜️ Calculer", command=self._calculer_pression, bg="#607D8B", fg="white", font=("Arial", 9, "bold"), padx=10, pady=2).pack(side="left", padx=15)
        tk.Label(f_press, text="=", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        champ_press_res = tk.Entry(f_press, textvariable=self.press_res_var, font=("Arial", 11, "bold"), fg="#1565C0", bg="white", width=8, bd=0)
        champ_press_res.configure(state="readonly", readonlybackground="white"); champ_press_res.pack(side="left", padx=5)
        tk.Label(f_press, text="Bars", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=10)
        cadre_actions = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"]); cadre_actions.pack(pady=8)
        tk.Button(cadre_actions, text="📤  Envoyer le dossier", command=self._valider_avis, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=22, pady=8).grid(row=0, column=0, padx=10)
        tk.Button(cadre_actions, text="📦  Mettre en attente", command=self._mettre_en_attente, bg="#FF9800", fg="white", font=("Arial", 10), width=22, pady=8).grid(row=0, column=1, padx=10)
        tk.Button(cadre_actions, text="⏭️  Passer ce dossier", command=self._passer_dossier, bg="#9E9E9E", fg="white", font=("Arial", 10), width=22, pady=8).grid(row=0, column=2, padx=10)
        self.label_erreur_avis = tk.Label(self.cadre_contenu, text="", font=("Arial", 10), fg="#f44336", bg=self.COULEURS["bg"])
        self.label_erreur_avis.pack()

    def _valider_avis(self):
        avis = self.avis_var.get() if self.avis_var else ""
        if not avis:
            return self.label_erreur_avis.config(text="⚠️  Veuillez sélectionner un avis !")
        self.resultat_avis["avis"] = avis
        self.resultat_avis["action"] = "ENVOYER"
        self.fenetre.quit()

    def _mettre_en_attente(self):
        popup = tk.Toplevel(self.fenetre)
        popup.title("Mise en attente")
        popup.geometry("400x150")
        popup.configure(bg=self.COULEURS["bg"])
        popup.transient(self.fenetre); popup.grab_set()
        popup.geometry(f"+{self.fenetre.winfo_x() + 300}+{self.fenetre.winfo_y() + 250}")
        tk.Label(popup, text="Motif de la mise en attente :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(pady=10)
        ent_motif = tk.Entry(popup, font=("Arial", 10), width=40); ent_motif.pack(pady=5); ent_motif.focus_set()

        def valider(event=None):
            self.resultat_avis["motif_attente"] = ent_motif.get().strip() or "En attente (sans motif)"
            self.resultat_avis["action"] = "ATTENTE"
            popup.destroy(); self.fenetre.quit()

        f_btn = tk.Frame(popup, bg=self.COULEURS["bg"]); f_btn.pack(pady=10)
        tk.Button(f_btn, text="Valider", command=valider, bg="#FF9800", fg="white", width=10, font=("Arial", 9, "bold")).pack(side="left", padx=10)
        tk.Button(f_btn, text="Annuler", command=popup.destroy, bg="#9E9E9E", fg="white", width=10, font=("Arial", 9, "bold")).pack(side="left", padx=10)
        popup.bind("<Return>", valider); popup.bind("<Escape>", lambda e: popup.destroy())

    def _passer_dossier(self):
        self.resultat_avis["action"] = "PASSER"
        self.fenetre.quit()

    def relancer(self):
        self.fenetre.mainloop()

    def fermer(self):
        try: self.fenetre.destroy()
        except Exception: pass


def verifier_et_connecter_openads(page, config, log):
    log.info("🔍 Vérification de la connexion OpenADS...")

    def _naviguer():
        page.goto("https://openads.e-mrs.fr/app/index.php?module=tab&obj=demande_avis_encours", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
    avec_retry(_naviguer, logger=log, description="Ouverture page OpenADS")

    def _connexion_auth():
        if "auth.e-mrs.fr" not in page.url: return
        page.wait_for_selector("#userNameInput", timeout=TIMEOUT_MOYEN)
        page.fill("#userNameInput", config["identifiant"]); page.fill("#passwordInput", config["mot_de_passe"])
        page.click("#submitButton"); page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
    avec_retry(_connexion_auth, logger=log, description="Connexion auth.e-mrs.fr")

    def _connexion_login():
        if "login" not in page.url: return
        page.wait_for_selector("#login", timeout=TIMEOUT_MOYEN)
        page.fill("#login", config["identifiant"]); page.fill("#password", config["mot_de_passe"])
        page.click("#login_form > div.formControls.formControls-bottom > input")
        page.wait_for_url("**/index.php?module=tab**", timeout=TIMEOUT_MOYEN)
        page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
    if "login" in page.url:
        avec_retry(_connexion_login, logger=log, description="Connexion OpenADS")
        log.succes("✅ Connecté à OpenADS !")
    else:
        log.succes("✅ Déjà connecté !")


def reconnecter_si_besoin_openads(page, config, log):
    if "module=login" in page.url or "auth.e-mrs.fr" in page.url:
        log.warning("⚠️ Session OpenADS expirée. Reconnexion auto...")
        try:
            if "auth.e-mrs.fr" in page.url:
                page.wait_for_selector("#userNameInput", timeout=TIMEOUT_COURT)
                page.fill("#userNameInput", config["identifiant"]); page.fill("#passwordInput", config["mot_de_passe"])
                page.click("#submitButton"); page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(500)
            if "module=login" in page.url:
                page.wait_for_selector("#login", timeout=TIMEOUT_COURT)
                page.fill("#login", config["identifiant"]); page.fill("#password", config["mot_de_passe"])
                page.click("#login_form > div.formControls.formControls-bottom > input")
                page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(500)
            log.succes("✅ Reconnexion réussie !")
        except Exception as e:
            log.erreur(f"❌ Échec reconnexion : {e}")


def telecharger_csv_openads(page, config, log):
    log.info("📥 Téléchargement du CSV OpenADS...")
    if "module=tab&obj=demande_avis_encours" not in page.url or "&action=" in page.url:
        page.goto("https://openads.e-mrs.fr/app/index.php?module=tab&obj=demande_avis_encours", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
    chemin_csv = Path(config["dossier_telechargement"]) / "liste_openads.csv"

    def _telecharger():
        with page.expect_download() as download_info:
            page.click("#tab-demande_avis_encours > div.tab-container > div.tab-export > a > span")
        download_info.value.save_as(chemin_csv)
        # --- AJOUT ---
        if not chemin_csv.exists() or chemin_csv.stat().st_size == 0:
            raise Exception("Le fichier CSV téléchargé est vide ou introuvable — nouvelle tentative nécessaire.")
    avec_retry(_telecharger, logger=log, description="Téléchargement CSV OpenADS")

    return chemin_csv


def lire_csv_openads(chemin_csv, log):
    dossiers = []
    try:
        with open(chemin_csv, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f, delimiter=";")
            en_tetes = next(reader, None)
            if not en_tetes:
                log.warning("⚠️ Le fichier CSV téléchargé est vide.")
                return dossiers
            for ligne in reader:
                if len(ligne) < 13: continue
                numero_brut = ligne[0].strip()
                if not numero_brut: continue
                adresse_chantier = f"{ligne[4].strip()} {ligne[5].strip()}, {ligne[6].strip()} {ligne[7].strip()}".strip()
                dossiers.append({"numero_brut": numero_brut, "numero_formate": formater_numero_dossier_openads(numero_brut),
                                  "nom_petitionnaire": ligne[1].strip(), "adresse_petitionnaire": ligne[2].strip(),
                                  "adresse_travaux": adresse_chantier, "date_limite": ligne[8].strip(), "date_depart": ligne[9].strip(),
                                  "references_cadastrales": ligne[10].strip(), "nature_travaux": ligne[12].strip(), "commune": ligne[7].strip()})
    except Exception as e:
        log.erreur(f"❌ Erreur critique lors de la lecture du CSV: {e}")
    return dossiers


def notifier_echeances_proches_openads(dossiers_csv, logger, interface=None):
    compte = sum(1 for d in dossiers_csv if (calculer_urgence_openads(d.get("date_limite", ""))[0] or 999) <= SEUIL_ALERTE_JOURS)
    if compte > 0:
        message = f"{compte} dossier(s) OpenADS à échéance ≤ {SEUIL_ALERTE_JOURS} jour(s) (ou en retard)."
        logger.warning(f"⏰ {message}")
        afficher_notification("⏰ Échéances proches — OpenADS", message, type_="warning", parent=interface.fenetre if interface else None)


def trouver_et_ouvrir_dossier_openads(page, dossier, log):
    numero_formate = dossier["numero_formate"]

    def _naviguer():
        if "module=tab&obj=demande_avis_encours" not in page.url:
            page.goto("https://openads.e-mrs.fr/app/index.php?module=tab&obj=demande_avis_encours", wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
    avec_retry(_naviguer, logger=log, description="Navigation vers le tableau OpenADS")

    while True:
        page.wait_for_selector("#tab-demande_avis_encours > div.tab-container > section table > tbody", timeout=TIMEOUT_MOYEN)
        page.wait_for_timeout(500)
        lignes = page.locator("#tab-demande_avis_encours > div.tab-container > section table > tbody > tr")
        for i in range(lignes.count()):
            if dossier["numero_formate"] in lignes.nth(i).inner_text():
                page.locator(f"#tab-demande_avis_encours > div.tab-container > section > table > tbody > tr:nth-child({i + 1}) > td.col-2 > a").click()
                page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
                return True, page.url
        bouton_suivant = page.locator("button[aria-label='Page suivante'], a[aria-label='Page suivante'], a.next, button.next")
        if bouton_suivant.count() > 0 and bouton_suivant.first.is_enabled():
            bouton_suivant.first.click(); page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
        else:
            return False, None


def telecharger_pieces_openads(page, config, log):
    page.click(SELECTEURS_OPENADS["onglet_pieces"]); page.wait_for_timeout(500)
    page.wait_for_selector(SELECTEURS_OPENADS["zip_dl"], timeout=TIMEOUT_MOYEN)
    page.click(SELECTEURS_OPENADS["zip_dl"]); page.wait_for_timeout(500)
    page.wait_for_selector(SELECTEURS_OPENADS["zip_confirm"], timeout=TIMEOUT_MOYEN)
    page.click(SELECTEURS_OPENADS["zip_confirm"])
    page.wait_for_selector(SELECTEURS_OPENADS["archive_ready"], timeout=TIMEOUT_TELECHARGEMENT)
    chemin_zip = Path(config["dossier_telechargement"]) / "pieces_openads_temp.zip"

    def _telecharger():
        with page.expect_download(timeout=TIMEOUT_TELECHARGEMENT) as download_info:
            page.click(SELECTEURS_OPENADS["archive_ready"])
        download_info.value.save_as(chemin_zip)
    avec_retry(_telecharger, logger=log, description="Téléchargement ZIP pièces OpenADS")

    try: page.click(SELECTEURS_OPENADS["close_dialog"], timeout=TIMEOUT_COURT)
    except Exception: pass
    page.click(SELECTEURS_OPENADS["onglet_form"]); page.wait_for_timeout(500)
    return chemin_zip


def extraire_zip_et_creer_dossier_openads(chemin_zip, donnees, config, log):
    nom_dossier = nettoyer_nom_dossier(f"{donnees['numero_formate']}-{donnees['adresse_travaux']}")
    chemin_dossier = Path(config["dossier_destination"]) / nom_dossier
    chemin_dossier.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(chemin_zip, "r") as zip_ref:
        zip_ref.extractall(chemin_dossier)
    os.remove(chemin_zip)
    return chemin_dossier


def preremplir_formulaire_pdf_openads(donnees, config, log):
    chemin_modele = str(Path(config["formulaire_pdf"]))
    nom_fichier = f"Formulaire_{re.sub(r'[<>:/\\|?*]', '_', donnees['numero_formate'])}.pdf"
    chemin_rempli = str(Path(config["formulaire_dossier_sortie"]) / nom_fichier)
    donnees_pdf = {"numéro dossier": donnees.get("numero_formate", ""), "nom pétitionnaire": donnees.get("nom_petitionnaire", ""),
                    "adresse pétitionnaire": donnees.get("adresse_petitionnaire", ""), "nature travaux": donnees.get("nature_travaux", ""),
                    "adresse travaux": donnees.get("adresse_travaux", ""),
                    "CommuneSEMM": "MARSEILLE"}
    try:
        avec_retry(lambda: remplir_pdf_champs_cibles(chemin_modele, chemin_rempli, donnees_pdf, logger=log), logger=log, description="Pré-remplissage PDF OpenADS")
        return chemin_rempli
    except Exception as e:
        log.warning(f"⚠️ Remplissage du PDF échoué ({e}). Copie du fichier vierge.")
        shutil.copy(chemin_modele, chemin_rempli)
        return chemin_rempli

def attendre_confirmation_et_avis_openads(chemin_dossier, config, interface, donnees, page, url_en_cours, log, dossiers_attente):
    interface.afficher_etape_avis(donnees)
    interface.relancer()
    action = interface.resultat_avis.get("action")
    if action == "ATTENTE":
        try:
            bouton_marquer = page.get_by_text("Marquer le dossier")
            if bouton_marquer.count() > 0:
                bouton_marquer.first.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        dossier_en_attente = Path(config["dossier_en_attente"])
        dossier_en_attente.mkdir(parents=True, exist_ok=True)
        destination = dossier_en_attente / Path(chemin_dossier).name
        motif = interface.resultat_avis.get("motif_attente", "En attente")
        with open(Path(chemin_dossier) / f"{donnees['numero_formate']} - Motif en attente.txt", "w", encoding="utf-8") as f:
            f.write(motif)
        if Path(chemin_dossier).resolve() != destination.resolve():
            try:
                shutil.move(str(chemin_dossier), str(destination))
            except Exception as e:
                log.warning(f"⚠️ Déplacement vers l'attente échoué : {e}")
        dossiers_attente[donnees["numero_formate"]] = {"motif": motif, "chemin": str(destination)}
        sauvegarder_attente(dossiers_attente)
        page.goto("https://openads.e-mrs.fr/app/index.php?module=tab&obj=demande_avis_encours", wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        return False, None
    if action == "PASSER":
        return False, None
    return True, interface.resultat_avis.get("avis", "")


def soumettre_avis_openads(page, avis, chemin_dossier, numero_formate, log, interface):
    page.wait_for_selector(SELECTEURS_OPENADS["bouton_rendre_avis"], timeout=TIMEOUT_MOYEN)
    page.click(SELECTEURS_OPENADS["bouton_rendre_avis"])
    page.wait_for_timeout(500)
    page.wait_for_selector(SELECTEURS_OPENADS["select_avis"], timeout=TIMEOUT_MOYEN)

    label_avis = AVIS_OPTIONS_OPENADS_LABELS[avis]
    select_box = page.locator(SELECTEURS_OPENADS["select_avis"])
    try:
        select_box.select_option(label=label_avis)
        page.evaluate('() => { let evt = new Event("change", {bubbles: true}); document.querySelector("select#avis_consultation").dispatchEvent(evt); }')
    except Exception:
        select_box.select_option(label=re.compile(f"^{label_avis}$", re.IGNORECASE))
        page.evaluate('() => { let evt = new Event("change", {bubbles: true}); document.querySelector("select#avis_consultation").dispatchEvent(evt); }')

    page.wait_for_timeout(500)

    if avis in ["refus", "favorable avec réserves", "défavorable", "incomplet"]:
        texte_detail = interface.resultat_avis.get("detail_avis", "")
        if avis == "refus" and not texte_detail:
            texte_detail = "Refus de consultation du dossier par manque de pièces"
        if texte_detail:
            try:
                champ_mot = page.locator(SELECTEURS_OPENADS["champ_motivation"])
                champ_mot.wait_for(state="attached", timeout=1000)
                champ_mot.fill(texte_detail, force=True)
            except Exception:
                try:
                    page.evaluate('(txt) => { let el = document.querySelector("#motivation"); if(el) { el.value = txt; el.dispatchEvent(new Event("input", {bubbles: true})); el.dispatchEvent(new Event("change", {bubbles: true})); } }', texte_detail)
                except Exception as ex:
                    log.warning(f"⚠️ Motif non renseigné automatiquement : {ex}")

    if avis == "refus":
        try:
            page.wait_for_selector(SELECTEURS_OPENADS["input_cache_validation"], state="hidden", timeout=int(TIMEOUT_TELECHARGEMENT * 2.5))
        except Exception:
            pass
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(500)
        return

    pdf_avis = trouver_pdf_avis(chemin_dossier, numero_formate)
    while not pdf_avis:
        try:
            interface.fenetre.update()
        except tk.TclError:
            raise FenetreFermeeException("Fenêtre fermée pendant l'attente du PDF d'avis OpenADS.")
        page.wait_for_timeout(1000)
        pdf_avis = trouver_pdf_avis(chemin_dossier, numero_formate)

    page.wait_for_selector(SELECTEURS_OPENADS["bouton_upload"], timeout=TIMEOUT_MOYEN)
    page.click(SELECTEURS_OPENADS["bouton_upload"])
    page.wait_for_timeout(500)

    def _uploader():
        with page.expect_file_chooser(timeout=TIMEOUT_MOYEN) as fc_info:
            page.click(SELECTEURS_OPENADS["champ_fichier"])
        fc_info.value.set_files(pdf_avis)
    avec_retry(_uploader, logger=log, description="Téléversement du PDF OpenADS")
    page.wait_for_timeout(500)

    page.wait_for_selector(SELECTEURS_OPENADS["bouton_valider_upload"], timeout=TIMEOUT_MOYEN)
    page.click(SELECTEURS_OPENADS["bouton_valider_upload"])
    page.wait_for_timeout(1000)

    try:
        page.wait_for_selector(SELECTEURS_OPENADS["input_cache_validation"], state="hidden", timeout=int(TIMEOUT_TELECHARGEMENT * 2.5))
    except Exception:
        pass
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(500)

    try:
        bouton_retour = page.locator("[id^='sousform-action-demande_avis_encours-back']").first
        if bouton_retour.count() > 0:
            bouton_retour.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(500)
    except Exception:
        page.goto("https://openads.e-mrs.fr/app/index.php?module=tab&obj=demande_avis_encours", wait_until="domcontentloaded")
        page.wait_for_timeout(500)


def deplacer_vers_a_upload_openads(chemin_dossier, config, log):
    destination_base = Path(config["dossier_a_upload"])
    destination_base.mkdir(parents=True, exist_ok=True)
    destination = destination_base / Path(chemin_dossier).name
    try:
        shutil.move(str(chemin_dossier), str(destination))
        return destination
    except Exception as e:
        log.warning(f"⚠️ Déplacement vers 'A upload' échoué : {e}")
        return None


def executer_openads(config_global):
    config = construire_config_outil(config_global, "OpenADS")
    reserves_aep = config_global.get("Réserves", {}).get("AEP", {})
    verifier_dossiers_config(config)
    valider_section(config, CLES_REQUISES_OPENADS, "OpenADS")
    log = Logger(config.get("dossier_logs", "logs"), "OPENADS")
    stats = StatsSessionOpenADS()
    dossiers_attente = charger_attente()

    try:
        with sync_playwright() as p:
            nettoyer_historique_chrome(config["chrome_profile"], log)  # <-- AJOUT

            context = p.chromium.launch_persistent_context(
                user_data_dir=config["chrome_profile"], channel="chrome", headless=False, accept_downloads=True,
                timeout=60000, downloads_path=config["dossier_telechargement"],
                args=["--disable-features=DownloadBubble,DownloadBubbleV2,DownloadShelf"]
            )
            page = context.new_page()

            try:
                verifier_et_connecter_openads(page, config, log)
                chemin_csv = telecharger_csv_openads(page, config, log)
                dossiers_csv = lire_csv_openads(chemin_csv, log)
                if not dossiers_csv:
                    stats.generer_rapport()
                    return

                interface = InterfaceOpenADS(dossiers_csv, log, reserves_aep)
                notifier_echeances_proches_openads(dossiers_csv, log, interface)

                while True:
                    interface.resultat = {"dossiers": [], "action": "QUIT"}
                    interface.afficher_etape_selection()
                    interface.relancer()

                    action, dossiers = interface.resultat["action"], interface.resultat["dossiers"]

                    if action == "REFRESH":
                        interface.fermer()
                        chemin_csv = telecharger_csv_openads(page, config, log)
                        dossiers_csv = lire_csv_openads(chemin_csv, log)
                        interface = InterfaceOpenADS(dossiers_csv, log, reserves_aep)
                        notifier_echeances_proches_openads(dossiers_csv, log, interface)
                        continue

                    if action == "QUIT":
                        interface.fermer()
                        break
                    if action != "TRAITER" or not dossiers:
                        continue

                    dossier_en_cours = dossiers[0]
                    interface.afficher_attente_dossier(dossier_en_cours, "Préparation du premier dossier...")
                    trouve, url_en_cours = trouver_et_ouvrir_dossier_openads(page, dossier_en_cours, log)

                    if trouve:
                        is_attente = dossier_en_cours["numero_formate"] in dossiers_attente
                        chemin_doss_en_cours, chemin_form_en_cours = None, None
                        if is_attente:
                            chemin_temp = Path(dossiers_attente[dossier_en_cours["numero_formate"]]["chemin"])
                            if chemin_temp.exists():
                                chemin_doss_en_cours = chemin_temp
                                candidat = chemin_doss_en_cours / f"Formulaire_{dossier_en_cours['numero_formate']}.pdf"
                                chemin_form_en_cours = candidat if candidat.exists() else None
                            else:
                                is_attente = False
                        if not is_attente:
                            chemin_zip_en_cours = telecharger_pieces_openads(page, config, log)
                            chemin_doss_en_cours = extraire_zip_et_creer_dossier_openads(chemin_zip_en_cours, dossier_en_cours, config, log)
                            chemin_form_en_cours = preremplir_formulaire_pdf_openads(dossier_en_cours, config, log)
                    else:
                        continue

                    try:
                        for index, dossier in enumerate(dossiers, 1):
                            trouve_suiv = False
                            try:
                                if chemin_form_en_cours:
                                    try:
                                        ouvrir_fichier_os(chemin_form_en_cours)
                                        page.wait_for_timeout(1000)
                                    except Exception as e:
                                        log.warning(f"⚠️ Impossible d'ouvrir le formulaire : {e}")
                                ouvrir_fichiers_dossier(chemin_doss_en_cours, log)

                                msg = f"Préparation du dossier suivant ({dossiers[index]['numero_formate']})..." if index < len(dossiers) else "Dernier dossier de la liste en cours d'instruction."
                                interface.afficher_attente_dossier(dossier_en_cours, msg)

                                if index < len(dossiers):
                                    dossier_suivant = dossiers[index]
                                    trouve_suiv, url_suiv = trouver_et_ouvrir_dossier_openads(page, dossier_suivant, log)
                                    if trouve_suiv:
                                        is_attente_suiv = dossier_suivant["numero_formate"] in dossiers_attente
                                        chemin_doss_suiv, chemin_form_suiv = None, None
                                        if is_attente_suiv:
                                            chemin_temp_suiv = Path(dossiers_attente[dossier_suivant["numero_formate"]]["chemin"])
                                            if chemin_temp_suiv.exists():
                                                chemin_doss_suiv = chemin_temp_suiv
                                                candidat_suiv = chemin_doss_suiv / f"Formulaire_{dossier_suivant['numero_formate']}.pdf"
                                                chemin_form_suiv = candidat_suiv if candidat_suiv.exists() else None
                                            else:
                                                is_attente_suiv = False
                                        if not is_attente_suiv:
                                            chemin_zip_suiv = telecharger_pieces_openads(page, config, log)
                                            chemin_doss_suiv = extraire_zip_et_creer_dossier_openads(chemin_zip_suiv, dossier_suivant, config, log)
                                            chemin_form_suiv = preremplir_formulaire_pdf_openads(dossier_suivant, config, log)

                                page.goto(url_en_cours, wait_until="domcontentloaded")
                                page.wait_for_timeout(500)
                                interface.afficher_etape_avis(dossier_en_cours)
                                interface.resultat_avis = {"avis": None, "action": None}
                                pret, avis = attendre_confirmation_et_avis_openads(chemin_doss_en_cours, config, interface, dossier_en_cours, page, url_en_cours, log, dossiers_attente)

                                if not pret:
                                    if index < len(dossiers) and trouve_suiv:
                                        dossier_en_cours, url_en_cours, chemin_doss_en_cours, chemin_form_en_cours = dossier_suivant, url_suiv, chemin_doss_suiv, chemin_form_suiv
                                    continue

                                reconnecter_si_besoin_openads(page, config, log)
                                interface.afficher_attente_dossier(dossier_en_cours, "Envoi de l'avis en cours...")

                                envoi_reussi = False
                                for tentative in range(2):
                                    try:
                                        page.goto(url_en_cours, wait_until="domcontentloaded")
                                        page.wait_for_timeout(500)
                                        soumettre_avis_openads(page, avis, chemin_doss_en_cours, dossier_en_cours["numero_formate"], log, interface)
                                        envoi_reussi = True
                                        break
                                    except PlaywrightTimeoutError as e:
                                        if "module=login" in page.url or "auth.e-mrs.fr" in page.url:
                                            reconnecter_si_besoin_openads(page, config, log)
                                        else:
                                            raise e
                                if not envoi_reussi:
                                    raise Exception("Échec critique de l'envoi.")

                                if dossier_en_cours["numero_formate"] in dossiers_attente:
                                    del dossiers_attente[dossier_en_cours["numero_formate"]]
                                    sauvegarder_attente(dossiers_attente)

                                deplacer_vers_a_upload_openads(chemin_doss_en_cours, config, log)

                                stats.traites += 1
                                if avis == "incomplet":
                                    stats.incomplets += 1

                                if index < len(dossiers) and trouve_suiv:
                                    dossier_en_cours, url_en_cours, chemin_doss_en_cours, chemin_form_en_cours = dossier_suivant, url_suiv, chemin_doss_suiv, chemin_form_suiv

                            except FenetreFermeeException as e:
                                log.warning(f"⚠️ Traitement interrompu par fermeture de fenêtre : {e}")
                                break
                            except Exception as e:
                                log.erreur(f"❌ Erreur sur {dossier_en_cours['numero_formate']} : {e}")
                                afficher_notification("❌ Erreur — OpenADS", f"{dossier_en_cours['numero_formate']} : {str(e)[:150]}", type_="erreur", parent=interface.fenetre)
                                if index < len(dossiers) and trouve_suiv:
                                    dossier_en_cours, url_en_cours, chemin_doss_en_cours, chemin_form_en_cours = dossier_suivant, url_suiv, chemin_doss_suiv, chemin_form_suiv
                                continue

                    except FenetreFermeeException:
                        pass

                    interface.fermer()
                    chemin_csv = telecharger_csv_openads(page, config, log)
                    dossiers_csv = lire_csv_openads(chemin_csv, log)
                    interface = InterfaceOpenADS(dossiers_csv, log, reserves_aep)
                    notifier_echeances_proches_openads(dossiers_csv, log, interface)

                stats.generer_rapport()

            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception as e:
        log.erreur(f"❌ Erreur critique dans l'exécution d'OpenADS : {e}")
        afficher_notification("❌ Erreur critique — OpenADS", str(e)[:200], type_="erreur")
        raise
    finally:
        log.fermer()


# ============================================================
# ENTRY POINT & MENU
# ============================================================

def afficher_menu_choix():
    root = tk.Tk()
    root.title("Choix de l'Outil")
    root.geometry("400x120")
    root.configure(bg="#F5F5F5")
    root.resizable(False, False)
    root.update_idletasks()
    root.geometry(f"+{(root.winfo_screenwidth()//2)-200}+{(root.winfo_screenheight()//2)-60}")

    choix = {"outil": None}

    def set_choix(val):
        choix["outil"] = val
        root.destroy()

    f = tk.Frame(root, bg="#F5F5F5")
    f.pack(expand=True)
    tk.Button(f, text="Avis'AU", command=lambda: set_choix("AVISAU"), bg="#2196F3", fg="white", font=("Arial", 14, "bold"), width=12, pady=15).pack(side="left", padx=15)
    tk.Button(f, text="Open ADS", command=lambda: set_choix("OPENADS"), bg="#4CAF50", fg="white", font=("Arial", 14, "bold"), width=12, pady=15).pack(side="left", padx=15)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return choix["outil"]


def main():
    try:
        config_global = charger_configuration_unifiee()
        appliquer_configuration_globale(config_global)
    except ConfigurationInvalide as e:
        print(f"Erreur de configuration : {e}")
        afficher_notification("❌ Configuration invalide", str(e)[:200], type_="erreur")
        return

    outil = afficher_menu_choix()
    if not outil:
        return

    try:
        if outil == "AVISAU":
            executer_avisau(config_global)
        elif outil == "OPENADS":
            executer_openads(config_global)
    except ConfigurationInvalide as e:
        print(f"Erreur de configuration : {e}")
        afficher_notification("❌ Configuration invalide", str(e)[:200], type_="erreur")


if __name__ == "__main__":
    main()
