import json
import csv
import os
import re
import zipfile
import unicodedata
import subprocess
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from fillpdf import fillpdfs

# Fichiers de suivi
QUEUE_FILE = "background_queue.json"
RECAP_FILE = "rapport_session.txt"

# ============================================================
# CONSTANTES : COMMUNES PAR SOCIÉTÉ
# ============================================================

COMMUNES_SEMM = [
    "marseille", "septemes les vallons", "plan de cuques",
    "le rove", "allauch", "gemenos", "carnoux en provence"
]
COMMUNES_SAOM = [
    "carry le rouet", "chateauneuf les martigues", "gignac la nerthe",
    "marignane", "sausset les pins", "ensues la redonne", "saint victoret"
]
COMMUNES_SAEM = [
    "cassis", "ceyreste", "la ciotat", "roquefort la bedoule"
]

SERVICES = {
    "SEMM" : "D_013_SEMM_SOCIETE EAU DE MARSEILLE METROPOLE",
    "SAOM" : "D_013_SAOM_SOCIETE D'ASSAINISSEMENT OUEST METROPOLE",
    "SAEM" : "D_013_SAEM_SOCIETE D'ASSAINISSEMENT EST METROPOLE"
}

# Mapping avis → option index
MAPPING_NATURE_INDEX = {
    "favorable"               : 1,  # option[2] = index 1
    "favorable avec réserves" : 2,  # option[3] = index 2
    "défavorable"             : 3,  # option[4] = index 3
    "incomplet"               : 5   # option[6] = index 5
}

# ============================================================
# CALCUL DATE LIMITE D'INSTRUCTION
# ============================================================

def calculer_date_limite(date_depart_str, societe):
    """
    Calcule la date limite d'instruction.
    SEMM → +30 jours
    SAOM / SAEM → +21 jours
    Entrée  : "2025-03-01" (format CSV)
    Sortie  : "31/03" (format JJ/MM)
    """
    try:
        date_depart = datetime.strptime(date_depart_str, "%Y-%m-%d")
        jours       = 30 if societe == "SEMM" else 21
        date_limite = date_depart + timedelta(days=jours)
        return date_limite.strftime("%d/%m")
    except:
        return "??/??"


def notifier_windows(titre, message):
    """Affiche une notification type 'Toast' en bas à droite de l'écran via Tkinter."""
    try:
        toast = tk.Toplevel()
        toast.title(titre)
        # Supprime les bordures de fenêtre
        toast.overrideredirect(True)
        # Reste au premier plan
        toast.attributes("-topmost", True)
        # Couleur et design
        toast.configure(bg="#333333")
        
        # Positionnement : en bas à droite
        largeur_ecran = toast.winfo_screenwidth()
        hauteur_ecran = toast.winfo_screenheight()
        # On la place à 20px du bord droit et 50px du bas
        toast.geometry(f"300x80+{largeur_ecran-320}+{hauteur_ecran-130}")

        # Contenu
        tk.Label(toast, text=titre, fg="#2196F3", bg="#333333", font=("Arial", 10, "bold")).pack(pady=(10, 0), padx=10, anchor="w")
        tk.Label(toast, text=message, fg="white", bg="#333333", font=("Arial", 9), wraplength=280, justify="left").pack(pady=5, padx=10, anchor="w")

        # S'auto-détruit après 5 secondes
        toast.after(5000, toast.destroy)
    except:
        # Si Tkinter n'est pas disponible à ce moment, on écrit au moins dans la console
        print(f"\n📢 NOTIFICATION : {titre} - {message}")

class StatsSession:
    def __init__(self):
        self.debut = datetime.now()
        self.traites = 0
        self.incomplets = 0
        self.en_attente = 0
    def generer_rapport(self):
        fin = datetime.now()
        duree = fin - self.debut
        rapport = (f"\n--- SESSION DU {fin.strftime('%d/%m/%Y')} ---\n"
                   f"⏱ Durée : {int(duree.total_seconds() // 60)} min\n"
                   f"✅ Finalisés : {self.traites} | 📋 Incomplets : {self.incomplets} | 📦 Attente : {self.en_attente}\n")
        with open(RECAP_FILE, "a", encoding="utf-8") as f: f.write(rapport)

def sauvegarder_queue(data):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)

def charger_queue():
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return None
    return None

# ============================================================
# INTERFACE GRAPHIQUE PERMANENTE
# ============================================================

class InterfaceAvisAU:
    """
    Interface tkinter permanente qui reste ouverte pendant
    toute la session et change de contenu selon l'étape.
    """

    def __init__(self, dossiers_csv):
        self.dossiers_csv = dossiers_csv
        self.resultat     = {"dossiers": [], "action": "QUIT"}

        # Résultats des avis (remplis à l'étape 3)
        self.avis_aep_var = None
        self.avis_eu_var  = None
        self.resultat_avis = {
            "dossier_pret" : None,   # True / False / "EN_ATTENTE"
            "avis_aep"     : None,
            "avis_eu"      : None,
            "action"       : None    # "ENVOYER" / "ATTENTE" / "ANNULER"
        }

        # Couleurs
        self.COULEURS = {
            "SEMM"    : "#2196F3",   # Bleu
            "SAOM"    : "#4CAF50",   # Vert
            "SAEM"    : "#FF9800",   # Orange
            None      : "#9E9E9E",   # Gris
            "bg"      : "#F5F5F5",
            "titre"   : "#1565C0",
            "success" : "#4CAF50",
            "warning" : "#FF9800",
            "danger"  : "#f44336",
            "info"    : "#2196F3",
        }

        self.AVIS_OPTIONS = [
            "favorable",
            "favorable avec réserves",
            "défavorable",
            "incomplet",
            "refus"
        ]

        # Créer la fenêtre principale
        self.fenetre = tk.Tk()
        self.fenetre.title("AUTOMATISATION AVIS'AU — SEMM")
        self.fenetre.geometry("1100x620")
        self.fenetre.resizable(True, True)
        self.fenetre.configure(bg=self.COULEURS["bg"])

        # Centrer
        self.fenetre.update_idletasks()
        x = (self.fenetre.winfo_screenwidth()  // 2) - (1100 // 2)
        y = (self.fenetre.winfo_screenheight() // 2) - (620  // 2)
        self.fenetre.geometry(f"1100x620+{x}+{y}")

        # --------------------------------------------------------
        # BANDEAU TITRE (permanent, ne change jamais)
        # --------------------------------------------------------
        self.cadre_titre = tk.Frame(
            self.fenetre, bg=self.COULEURS["titre"], pady=10
        )
        self.cadre_titre.pack(fill="x")

        self.label_titre = tk.Label(
            self.cadre_titre,
            text="🚰  AUTOMATISATION AVIS'AU — SEMM",
            font=("Arial", 14, "bold"),
            bg=self.COULEURS["titre"], fg="white"
        )
        self.label_titre.pack()

        self.label_sous_titre = tk.Label(
            self.cadre_titre,
            text=f"📋  {len(dossiers_csv)} dossier(s) en attente",
            font=("Arial", 10),
            bg=self.COULEURS["titre"], fg="#BBDEFB"
        )
        self.label_sous_titre.pack()

        # --------------------------------------------------------
        # ZONE DE CONTENU (change selon l'étape)
        # --------------------------------------------------------
        self.cadre_contenu = tk.Frame(
            self.fenetre, bg=self.COULEURS["bg"]
        )
        self.cadre_contenu.pack(fill="both", expand=True, padx=0, pady=0)

        # Afficher l'étape 1 au démarrage
        self.afficher_etape_selection()

    # ============================================================
    # UTILITAIRE : Vider le cadre de contenu
    # ============================================================

    def vider_contenu(self):
        """Supprime tous les widgets du cadre de contenu."""
        for widget in self.cadre_contenu.winfo_children():
            widget.destroy()

    def mettre_a_jour_sous_titre(self, texte, couleur="#BBDEFB"):
        """Met à jour le sous-titre du bandeau."""
        self.label_sous_titre.config(text=texte, fg=couleur)

    # ============================================================
    # ÉTAPE 1 : SÉLECTION DES DOSSIERS
    # ============================================================

    def afficher_etape_selection(self):
        """Affiche la liste des dossiers avec cases à cocher."""
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(
            f"📋  {len(self.dossiers_csv)} dossier(s) en attente"
        )

        # --------------------------------------------------------
        # LÉGENDE
        # --------------------------------------------------------
        cadre_legende = tk.Frame(
            self.cadre_contenu, bg="#ECEFF1", pady=6
        )
        cadre_legende.pack(fill="x", padx=20, pady=(8, 0))

        for societe, couleur in [
            ("SEMM (30j)", "#2196F3"),
            ("SAOM (21j)", "#4CAF50"),
            ("SAEM (21j)", "#FF9800"),
        ]:
            tk.Label(
                cadre_legende,
                text=f"  {societe}  ",
                bg=couleur, fg="white",
                font=("Arial", 9, "bold"),
                padx=8, pady=3
            ).pack(side="left", padx=4)

        # --------------------------------------------------------
        # EN-TÊTE COLONNES
        # --------------------------------------------------------
        cadre_entete = tk.Frame(self.cadre_contenu, bg="#37474F")
        cadre_entete.pack(fill="x", padx=20, pady=(6, 0))

        for texte, largeur in [
            ("",               3),
            ("Numéro",        22),
            ("Commune",       20),
            ("Société",        8),
            ("Date départ",   10),
            ("Date limite",   10),
            ("Délai restant", 14),
        ]:
            tk.Label(
                cadre_entete,
                text=texte, width=largeur,
                bg="#37474F", fg="white",
                font=("Arial", 9, "bold"),
                anchor="w", pady=5
            ).pack(side="left", padx=3)

        # --------------------------------------------------------
        # LISTE SCROLLABLE
        # --------------------------------------------------------
        cadre_scroll = tk.Frame(self.cadre_contenu)
        cadre_scroll.pack(fill="both", expand=True, padx=20)

        scrollbar = tk.Scrollbar(cadre_scroll)
        scrollbar.pack(side="right", fill="y")

        canvas = tk.Canvas(
            cadre_scroll, yscrollcommand=scrollbar.set,
            bg=self.COULEURS["bg"]
        )
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=canvas.yview)

        cadre_liste = tk.Frame(canvas, bg=self.COULEURS["bg"])
        canvas.create_window((0, 0), window=cadre_liste, anchor="nw")

        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        )

        self.variables = []

        for i, dossier in enumerate(self.dossiers_csv):
            var         = tk.BooleanVar(value=False)
            self.variables.append(var)

            societe      = get_societe(dossier["commune"])
            couleur_soc  = self.COULEURS.get(societe, "#9E9E9E")
            couleur_fond = "#f9f9f9" if i % 2 == 0 else "white"

            date_limite  = calculer_date_limite(
                dossier["date_depart"], societe
            )

            try:
                date_dep    = datetime.strptime(
                    dossier["date_depart"], "%Y-%m-%d"
                )
                jours_total = 30 if societe == "SEMM" else 21
                date_lim    = date_dep + timedelta(days=jours_total)
                restant     = (date_lim - datetime.now()).days

                if restant < 0:
                    texte_restant   = f"⛔ {abs(restant)}j dépassé"
                    couleur_restant = "#f44336"
                elif restant <= 5:
                    texte_restant   = f"⚠️ {restant}j restants"
                    couleur_restant = "#FF9800"
                else:
                    texte_restant   = f"✅ {restant}j restants"
                    couleur_restant = "#4CAF50"
            except:
                texte_restant   = "?"
                couleur_restant = "#9E9E9E"

            try:
                date_dep_fmt = datetime.strptime(
                    dossier["date_depart"], "%Y-%m-%d"
                ).strftime("%d/%m")
            except:
                date_dep_fmt = dossier["date_depart"]

            tk.Checkbutton(
                cadre_liste, variable=var, bg=couleur_fond
            ).grid(row=i, column=0, padx=5, pady=1)

            tk.Label(
                cadre_liste, text=dossier["numero_formate"],
                width=22, bg=couleur_fond,
                anchor="w", font=("Courier", 9)
            ).grid(row=i, column=1, padx=3, pady=1)

            tk.Label(
                cadre_liste, text=dossier["commune"],
                width=20, bg=couleur_fond, anchor="w"
            ).grid(row=i, column=2, padx=3, pady=1)

            tk.Label(
                cadre_liste, text=f" {societe or '?'} ",
                width=8, bg=couleur_soc,
                fg="white", anchor="center",
                font=("Arial", 8, "bold")
            ).grid(row=i, column=3, padx=3, pady=1)

            tk.Label(
                cadre_liste, text=date_dep_fmt,
                width=10, bg=couleur_fond, anchor="w"
            ).grid(row=i, column=4, padx=3, pady=1)

            tk.Label(
                cadre_liste, text=date_limite,
                width=10, bg=couleur_fond,
                anchor="w", font=("Arial", 9, "bold")
            ).grid(row=i, column=5, padx=3, pady=1)

            tk.Label(
                cadre_liste, text=texte_restant,
                width=14, bg=couleur_fond,
                fg=couleur_restant,
                anchor="w", font=("Arial", 9, "bold")
            ).grid(row=i, column=6, padx=3, pady=1)

        cadre_liste.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

        # --------------------------------------------------------
        # COMPTEUR
        # --------------------------------------------------------
        self.label_compteur = tk.Label(
            self.cadre_contenu,
            text="0 dossier(s) sélectionné(s)",
            font=("Arial", 10), fg="#666666",
            bg=self.COULEURS["bg"]
        )
        self.label_compteur.pack(pady=4)

        def mettre_a_jour_compteur(*args):
            n = sum(1 for v in self.variables if v.get())
            self.label_compteur.config(
                text=f"{n} dossier(s) sélectionné(s)",
                fg="#2196F3" if n > 0 else "#666666"
            )

        for var in self.variables:
            var.trace_add("write", mettre_a_jour_compteur)

        # --------------------------------------------------------
        # BOUTONS
        # --------------------------------------------------------
        cadre_boutons = tk.Frame(
            self.cadre_contenu, bg=self.COULEURS["bg"]
        )
        cadre_boutons.pack(pady=8)

        tk.Button(
            cadre_boutons, text="✅  Tout sélectionner",
            command=lambda: [v.set(True) for v in self.variables],
            bg="#4CAF50", fg="white",
            font=("Arial", 10), width=18, pady=5
        ).grid(row=0, column=0, padx=6)

        tk.Button(
            cadre_boutons, text="☐  Tout désélectionner",
            command=lambda: [v.set(False) for v in self.variables],
            bg="#f44336", fg="white",
            font=("Arial", 10), width=18, pady=5
        ).grid(row=0, column=1, padx=6)

        tk.Button(
            cadre_boutons, text="▶  Traiter la sélection",
            command=self._confirmer_selection,
            bg="#2196F3", fg="white",
            font=("Arial", 11, "bold"), width=18, pady=5
        ).grid(row=0, column=2, padx=6)

        tk.Button(
            cadre_boutons, text="🔄  Rafraîchir",
            command=self._rafraichir,
            bg="#607D8B", fg="white",
            font=("Arial", 10), width=18, pady=5
        ).grid(row=0, column=3, padx=6)

        tk.Button(
            cadre_boutons, text="✖  Quitter",
            command=self._quitter,
            bg="#9E9E9E", fg="white",
            font=("Arial", 10), width=18, pady=5
        ).grid(row=0, column=4, padx=6)

        self.fenetre.bind("<Escape>", lambda e: self._quitter())
        self.fenetre.bind("<Return>", lambda e: self._confirmer_selection())

    def _confirmer_selection(self):
        dossiers = [
            self.dossiers_csv[i]
            for i, v in enumerate(self.variables) if v.get()
        ]
        if not dossiers:
            self.mettre_a_jour_sous_titre(
                "⚠️  Sélectionnez au moins un dossier !",
                couleur="#FF9800"
            )
            return
        self.resultat["dossiers"] = dossiers
        self.resultat["action"]   = "TRAITER"
        self.fenetre.quit()

    def _rafraichir(self):
        self.resultat["action"] = "REFRESH"
        self.fenetre.quit()

    def _quitter(self):
        self.resultat["action"] = "QUIT"
        self.fenetre.quit()

    # ============================================================
    # ÉTAPE 2 : CONFIRMATION DOSSIER + SAISIE AVIS
    # ============================================================

    def afficher_etape_avis(self, donnees, societe):
        """
        Version restaurée de l'interface originale avec gros boutons colorés.
        Ajout de la ligne Parcelle.
        """
        self.vider_contenu()

        couleur_soc = self.COULEURS.get(societe, "#9E9E9E")
        jours       = 30 if societe == "SEMM" else 21

        self.mettre_a_jour_sous_titre(
            f"🏢  Traitement en cours — {donnees['numero_formate']}",
            couleur="#BBDEFB"
        )

        # --------------------------------------------------------
        # CARTE DOSSIER (Originale)
        # --------------------------------------------------------
        cadre_carte = tk.Frame(
            self.cadre_contenu,
            bg="white", pady=15, padx=20,
            relief="flat", bd=0
        )
        cadre_carte.pack(fill="x", padx=30, pady=15)

        # Badge société
        tk.Label(
            cadre_carte,
            text=f"  {societe}  ",
            bg=couleur_soc, fg="white",
            font=("Arial", 12, "bold"),
            padx=12, pady=5
        ).grid(row=0, column=0, sticky="w", padx=(0, 15))

        # Numéro dossier
        tk.Label(
            cadre_carte,
            text=donnees["numero_formate"],
            font=("Courier", 14, "bold"),
            bg="white", fg="#1565C0"
        ).grid(row=0, column=1, sticky="w")

        # Infos dossier (Inclusion de la Parcelle)
        infos = [
            ("📍 Commune",          donnees.get("commune", "")),
            ("🗺️ Parcelle",         donnees.get("parcelle", "")), # <-- Nouvelle ligne
            ("👤 Pétitionnaire",    donnees.get("nom_petitionnaire", "")),
            ("🏠 Adresse travaux",  donnees.get("adresse_travaux", "")),
            ("📅 Date départ",     donnees.get("date_depart", "")),
            (f"⏱️ Délai ({jours}j)", calculer_date_limite(
                donnees.get("date_depart", ""), societe
            )),
        ]

        for row, (label, valeur) in enumerate(infos, 1):
            tk.Label(
                cadre_carte, text=label,
                font=("Arial", 9, "bold"),
                bg="white", fg="#666666", width=18, anchor="w"
            ).grid(row=row, column=0, sticky="w", pady=2)

            tk.Label(
                cadre_carte, text=valeur,
                font=("Arial", 9),
                bg="white", fg="#333333", anchor="w"
            ).grid(row=row, column=1, sticky="w", pady=2)

        # --------------------------------------------------------
        # SECTION AVIS AEP (Gros boutons colorés originaux)
        # --------------------------------------------------------
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        
        cadre_avis = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_avis.pack(fill="x", padx=30, pady=5)

        tk.Label(
            cadre_avis, text="💧  AVIS AEP",
            font=("Arial", 12, "bold"),
            bg=self.COULEURS["bg"], fg="#1565C0"
        ).pack(anchor="w", pady=(5, 3))

        self.avis_aep_var = tk.StringVar(value="")
        cadre_boutons_aep = tk.Frame(cadre_avis, bg=self.COULEURS["bg"])
        cadre_boutons_aep.pack(anchor="w")

        STYLES_AVIS = {
            "favorable"               : ("#4CAF50", "✅  Favorable"),
            "favorable avec réserves" : ("#FF9800", "⚠️  Favorable avec réserves"),
            "défavorable"              : ("#f44336", "❌  Défavorable"),
            "incomplet"               : ("#9C27B0", "📋  Incomplet"),
            "refus"                   : ("#607D8B", "🚫  Refus"),
        }

        for i, (valeur, (couleur, texte)) in enumerate(STYLES_AVIS.items()):
            tk.Radiobutton(
                cadre_boutons_aep, text=texte, variable=self.avis_aep_var,
                value=valeur, bg=self.COULEURS["bg"], activebackground=couleur,
                selectcolor=couleur, fg="#333333", font=("Arial", 10),
                indicatoron=0, width=22, pady=6, relief="groove", bd=1
            ).grid(row=0, column=i, padx=4)

        # --------------------------------------------------------
        # SECTION AVIS EU (Seulement SAOM et SAEM)
        # --------------------------------------------------------
        self.avis_eu_var = tk.StringVar(value="")
        if societe in ["SAOM", "SAEM"]:
            ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
            
            cadre_avis_eu = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
            cadre_avis_eu.pack(fill="x", padx=30, pady=5)

            tk.Label(
                cadre_avis_eu, text=f"🌊  AVIS EU ({societe})",
                font=("Arial", 12, "bold"),
                bg=self.COULEURS["bg"], fg=couleur_soc
            ).pack(anchor="w", pady=(5, 3))

            cadre_boutons_eu = tk.Frame(cadre_avis_eu, bg=self.COULEURS["bg"])
            cadre_boutons_eu.pack(anchor="w")

            for i, (valeur, (couleur, texte)) in enumerate(STYLES_AVIS.items()):
                tk.Radiobutton(
                    cadre_boutons_eu, text=texte, variable=self.avis_eu_var,
                    value=valeur, bg=self.COULEURS["bg"], activebackground=couleur,
                    selectcolor=couleur, fg="#333333", font=("Arial", 10),
                    indicatoron=0, width=22, pady=6, relief="groove", bd=1
                ).grid(row=0, column=i, padx=4)

        # --------------------------------------------------------
        # BOUTONS D'ACTION (Bas de page)
        # --------------------------------------------------------
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=10)
        
        cadre_actions = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_actions.pack(pady=8)

        tk.Button(
            cadre_actions, text="📤  Envoyer le dossier",
            command=self._valider_avis, bg="#2196F3", fg="white",
            font=("Arial", 11, "bold"), width=22, pady=8
        ).grid(row=0, column=0, padx=10)

        tk.Button(
            cadre_actions, text="📦  Mettre en attente",
            command=self._mettre_en_attente, bg="#FF9800", fg="white",
            font=("Arial", 10), width=22, pady=8
        ).grid(row=0, column=1, padx=10)

        # Label d'erreur caché
        self.label_erreur_avis = tk.Label(
            self.cadre_contenu, text="", font=("Arial", 10), 
            fg="#f44336", bg=self.COULEURS["bg"]
        )
        self.label_erreur_avis.pack()

    def _valider_avis(self):
        """Valide les avis saisis et continue le traitement."""
        avis_aep = self.avis_aep_var.get() if self.avis_aep_var else ""
        avis_eu  = self.avis_eu_var.get()  if self.avis_eu_var  else ""

        if not avis_aep:
            self.label_erreur_avis.config(
                text="⚠️  Veuillez sélectionner un avis AEP !"
            )
            return

        self.resultat_avis["avis_aep"] = avis_aep
        self.resultat_avis["avis_eu"]  = avis_eu
        self.resultat_avis["action"]   = "ENVOYER"
        self.fenetre.quit()

    def _mettre_en_attente(self):
        self.resultat_avis["action"] = "ATTENTE"
        self.fenetre.quit()

    def _passer_dossier(self):
        self.resultat_avis["action"] = "PASSER"
        self.fenetre.quit()

    # ============================================================
    # ÉTAPE 3 : TRAITEMENT EN COURS
    # ============================================================

    def afficher_etape_traitement(self, numero_formate):
        """Affiche un écran 'traitement en cours' avec logs."""
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(
            f"⚙️  Traitement en cours — {numero_formate}",
            couleur="#BBDEFB"
        )

        tk.Label(
            self.cadre_contenu,
            text=f"⚙️  Traitement de {numero_formate}",
            font=("Arial", 13, "bold"),
            bg=self.COULEURS["bg"], fg="#1565C0"
        ).pack(pady=20)

        # Zone de logs
        self.zone_logs = tk.Text(
            self.cadre_contenu,
            height=20, width=90,
            font=("Courier", 9),
            bg="#1E1E1E", fg="#FFFFFF",
            relief="flat"
        )
        self.zone_logs.pack(padx=30, pady=5)
        self.zone_logs.config(state="disabled")

        self.fenetre.update()

    def ajouter_log(self, message):
        """Ajoute une ligne dans la zone de logs."""
        if hasattr(self, "zone_logs"):
            self.zone_logs.config(state="normal")
            self.zone_logs.insert("end", message + "\n")
            self.zone_logs.see("end")
            self.zone_logs.config(state="disabled")
            self.fenetre.update()

    # ============================================================
    # LANCER / ARRÊTER LA FENÊTRE
    # ============================================================

    def lancer(self):
        """Lance la boucle tkinter (bloque jusqu'à quit())."""
        self.fenetre.mainloop()

    def relancer(self):
        """Relance la boucle après un quit()."""
        self.fenetre.mainloop()

    def fermer(self):
        """Ferme définitivement la fenêtre."""
        try:
            self.fenetre.destroy()
        except:
            pass


# ============================================================
# MAPPING COMMUNES → FORMAT TABLEAU DE SUIVI SHEETS
# ============================================================

def formater_commune_sheets(commune):
    """
    Convertit le nom de la commune (format CSV) vers le format
    exact accepté par le menu déroulant du tableau de suivi.

    """
    # Normaliser la commune pour la comparaison
    commune_norm = normaliser_commune(commune)

    # Mapping normalisé → format Sheets
    MAPPING_COMMUNES_SHEETS = {
        # SEMM
        "allauch"                        : "ALLAUCH",
        "septemes les vallons"           : "SEPTEMES",
        "le rove"                        : "LE ROVE",
        "carnoux en provence"            : "CARNOUX",
        # SAOM
        "carry le rouet"                 : "CARRY LE ROUET",
        "chateauneuf les martigues"      : "CHATEAUNEUF",
        "ensues la redonne"              : "ENSUES",
        "gignac la nerthe"               : "GIGNAC LA NERTHE",
        "marignane"                      : "MARIGNANE",
        "saint victoret"                 : "SAINT-VICTORET",
        "sausset les pins"               : "SAUSSET",
        # SAEM
        "ceyreste"                       : "CEYRESTE",
        "cassis"                         : "CASSIS",
        "roquefort la bedoule"           : "ROQUEFORT LA BEDOULE",
        "la ciotat"                      : "LA CIOTAT",
    }

    # Chercher dans le mapping
    if commune_norm in MAPPING_COMMUNES_SHEETS:
        return MAPPING_COMMUNES_SHEETS[commune_norm]

    # Si pas trouvé exactement, chercher par inclusion
    for cle, valeur in MAPPING_COMMUNES_SHEETS.items():
        if cle in commune_norm or commune_norm in cle:
            return valeur

    # Si toujours pas trouvé → retourner la commune normalisée en majuscules
    print(f"⚠️ Commune '{commune}' non trouvée dans le mapping Sheets !")
    print(f"   Valeur brute utilisée : '{commune.upper()}'")
    return commune.upper()


# ============================================================
# CHARGEMENT DE LA CONFIGURATION
# ============================================================

def charger_config():
    """Charge le fichier de configuration config.json"""
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

# ============================================================
# FORMATAGE DU NUMÉRO DE DOSSIER
# ============================================================

def formater_numero_dossier(numero_brut):
    """
    Formate le numéro de dossier en ajoutant des espaces.
    PC0131062500036  →  PC 013 106 25 00036
    CU0130022600004  →  CU 013 002 26 00004
    DP013026 22H0135 →  DP 013 026 22 H0135
    Règle : XX XXX XXX XX [reste]
    """
    numero = numero_brut.strip().upper()
    pattern = r'^([A-Z]{2})(\d{3})(\d{3})(\d{2})(.+)$'
    match = re.match(pattern, numero)
    if match:
        return f"{match.group(1)} {match.group(2)} {match.group(3)} {match.group(4)} {match.group(5)}"
    else:
        print(f"⚠️ Format de numéro inattendu : {numero_brut}")
        return numero_brut

# ============================================================
# NORMALISATION DES COMMUNES
# ============================================================

def normaliser_commune(commune):
    """
    Normalise le nom d'une commune pour la comparaison interne.
    GIGNAC-LA-NERTHE           → gignac la nerthe
    Châteauneuf-les-Martigues  → chateauneuf les martigues
    SEPTEMES LES VALLONS       → septemes les vallons
    """
    commune = commune.lower().strip()
    commune = ''.join(
        c for c in unicodedata.normalize('NFD', commune)
        if unicodedata.category(c) != 'Mn'
    )
    commune = commune.replace('-', ' ')
    commune = ' '.join(commune.split())
    return commune

def normaliser_commune_drive(commune):
    """
    Normalise le nom de la commune pour Google Drive.
    MAJUSCULES, SANS TIRETS, AVEC ESPACES.
    """
    commune = commune.upper().strip()
    commune = ''.join(
        c for c in unicodedata.normalize('NFD', commune)
        if unicodedata.category(c) != 'Mn'
    )
    commune = commune.replace('-', ' ')
    commune = ' '.join(commune.split())
    return commune

# ============================================================
# DÉTERMINER LA SOCIÉTÉ SELON LA COMMUNE
# ============================================================

def get_societe(commune):
    """
    Retourne la société associée à la commune.
    SEMM / SAOM / SAEM
    Utilise la normalisation pour gérer les variantes d'écriture.
    """
    commune_norm = normaliser_commune(commune)

    for c in COMMUNES_SEMM:
        if c in commune_norm or commune_norm in c:
            return "SEMM"
    for c in COMMUNES_SAOM:
        if c in commune_norm or commune_norm in c:
            return "SAOM"
    for c in COMMUNES_SAEM:
        if c in commune_norm or commune_norm in c:
            return "SAEM"

    print(f"⚠️ Commune '{commune}' non reconnue dans les listes !")
    return None

# ============================================================
# CONNEXION AU SITE
# ============================================================

def verifier_et_connecter(page_avisau, config):
    """
    Vérifie si on est connecté à Avis'AU.
    Si non, effectue la connexion via Cerbère.
    """
    print("🔍 Vérification de la connexion...")

    page_avisau.goto(
        "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
    )
    page_avisau.wait_for_load_state("networkidle")

    if "login" in page_avisau.url:
        print("❌ Non connecté - Connexion en cours...")

        page_avisau.click("button.fr-btn.fr-connect.cerbere-connect")
        page_avisau.wait_for_load_state("networkidle")
        print("✅ Page Cerbère chargée")

        page_avisau.fill("#login", config["identifiant"])
        page_avisau.fill("#password", config["mot_de_passe"])
        page_avisau.click("#btnConnexion")

        page_avisau.wait_for_url("**/consultation**", timeout=15000)
        page_avisau.wait_for_load_state("networkidle")
        print("✅ Connecté avec succès !")
    else:
        print("✅ Déjà connecté !")

# ============================================================
# SÉLECTION DU SERVICE ET TÉLÉCHARGEMENT DU CSV
# ============================================================

def selectionner_service_et_telecharger_csv(page_avisau, config):
    """
    Sélectionne le service SEMM dans le menu déroulant
    et télécharge le fichier CSV de la liste des consultations.
    """
    print(f"📋 Sélection du service : {config['service']}")

    if "consultation" not in page_avisau.url:
        page_avisau.goto(
            "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
        )
        page_avisau.wait_for_load_state("networkidle")

    page_avisau.select_option("#select", label=config["service"])
    page_avisau.wait_for_load_state("networkidle")
    page_avisau.wait_for_timeout(2000)
    print("✅ Service sélectionné")

    print("📥 Téléchargement du CSV...")
    chemin_csv = Path(config["dossier_telechargement"]) / "liste_consultations.csv"

    with page_avisau.expect_download() as download_info:
        page_avisau.click(
            "#onglet-attente-metier-panel > div.fr-grid-row.fr-grid-row--middle.fr-mt-2w > div.fr-col-auto > button"
        )

    download = download_info.value
    download.save_as(chemin_csv)
    print(f"✅ CSV téléchargé")

    return chemin_csv

# ============================================================
# LECTURE DU CSV
# ============================================================

def lire_csv(chemin_csv):
    """
    Lit le fichier CSV et retourne TOUS les dossiers.
    - Encodage : UTF-8-sig (gère le BOM)
    - Séparateur : ; (point-virgule)
    - Ligne 1 : En-têtes (ignorée automatiquement)
    - Formatage du numéro de dossier avec espaces
    """
    dossiers = []

    with open(chemin_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")

        for ligne in reader:
            numero_brut = ligne["Numéro de dossier"].strip()
            dossiers.append({
                "date_depart"    : ligne["Date de départ du délai"].strip(),
                "identifiant"    : ligne["Identifiant de la consultation"].strip(),
                "service"        : ligne["Intitulé du service consultant"].strip(),
                "numero_brut"    : numero_brut,
                "numero_formate" : formater_numero_dossier(numero_brut),
                "commune"        : ligne["Nom de la commune"].strip(),
                "code_insee"     : ligne["Code Insee"].strip(),
                "date_limite"    : ligne["Date limite de réponse"].strip()
            })

    print(f"✅ CSV lu : {len(dossiers)} dossier(s) trouvé(s) au total")
    return dossiers

# ============================================================
# MENU DE DÉMARRAGE
# ============================================================

def menu_demarrage(dossiers_csv):
    """
    Affiche le menu de démarrage et retourne
    la liste des dossiers à traiter selon le choix.
    
    Retours possibles :
    - (dossiers, choix)  → traiter les dossiers
    - ("REFRESH", None)  → rafraîchir la liste
    - ("QUIT", None)     → quitter le programme
    - ("INVALID", None)  → choix invalide ou annulé → retour au menu
    """
    print("\n" + "=" * 60)
    print("  AUTOMATTON Avis'Au")
    print("=" * 60)
    print(f"\n📋 {len(dossiers_csv)} dossier(s) en attente au total\n")
    for d in dossiers_csv:
        print(f"  - {d['numero_formate']:<22} | {d['commune']:<30} | {d['date_depart']}")
    print("Que souhaitez-vous faire ?\n")
    print("  1. Traiter les dossiers les plus récents")
    print("  2. Traiter les dossiers d'une date précise")
    print("  3. Traiter un ou plusieurs dossier(s) spécifique(s)")
    print("  4. Traiter TOUS les dossiers en attente")
    print("  5. Rafraîchir la liste des dossiers")
    print("  6. Quitter")

    choix = input("\nVotre choix (1, 2, 3, 4, 5 ou 6) : ").strip()

    # --------------------------------------------------------
    # CHOIX 1 : Les N dossiers les plus récents
    # --------------------------------------------------------
    if choix == "1":
        while True:
            try:
                nombre = int(input(
                    f"\nCombien de dossiers ? (max {len(dossiers_csv)}) : "
                ).strip())
                if 1 <= nombre <= len(dossiers_csv):
                    break
                else:
                    print(f"⚠️ Entrez un nombre entre 1 et {len(dossiers_csv)}")
            except ValueError:
                print("⚠️ Entrez un nombre valide !")
        dossiers = dossiers_csv[:nombre]

    # --------------------------------------------------------
    # CHOIX 2 : Filtrer par date précise (JJ/MM seulement)
    # --------------------------------------------------------
    elif choix == "2":
        while True:
            date_saisie = input("\nDate à traiter (JJ/MM) : ").strip()
            try:
                annee_en_cours = datetime.now().year
                date_complete  = f"{date_saisie}/{annee_en_cours}"
                date_obj       = datetime.strptime(date_complete, "%d/%m/%Y")
                date_filtrage  = date_obj.strftime("%Y-%m-%d")
                print(f"   → Date complète : {date_complete}")
                break
            except ValueError:
                print("⚠️ Format invalide ! Utilisez JJ/MM (ex: 05/03)")

        dossiers = [d for d in dossiers_csv if d["date_depart"] == date_filtrage]

        if not dossiers:
            print(f"\n⚠️ Aucun dossier trouvé pour le {date_saisie}/{datetime.now().year}")
            return "INVALID", None  # ← Retour au menu

    # --------------------------------------------------------
    # CHOIX 3 : Traiter un dossier spécifique
    # --------------------------------------------------------
    elif choix == "3":
        print(f"\n📋 Dossiers disponibles :\n")
        for d in dossiers_csv:
            print(f"  - {d['numero_formate']:<22} | {d['commune']:<30} | {d['date_depart']}")

        while True:
            trois_derniers = input(
                "\nQuel dossier voulez-vous traiter ? (3 derniers chiffres) : "
            ).strip()

            dossiers_trouves = [
                d for d in dossiers_csv
                if d["numero_brut"].endswith(trois_derniers)
                or d["numero_formate"].replace(" ", "").endswith(trois_derniers)
            ]

            if len(dossiers_trouves) == 1:
                dossier_choisi = dossiers_trouves[0]
                print(f"\n✅ Dossier trouvé : {dossier_choisi['numero_formate']} | {dossier_choisi['commune']}")
                confirmation = input("Confirmer ? (O/N) : ").strip().upper()
                if confirmation == "O":
                    dossiers = [dossier_choisi]
                    break
                else:
                    print("❌ Annulé, réessayez.")

            elif len(dossiers_trouves) > 1:
                print(f"\n⚠️ Plusieurs dossiers trouvés :")
                for i, d in enumerate(dossiers_trouves, 1):
                    print(f"  {i}. {d['numero_formate']} | {d['commune']}")
                print("💡 Entrez plus de chiffres pour affiner.")

            else:
                print(f"⚠️ Aucun dossier trouvé avec '{trois_derniers}'")
                print("💡 Vérifiez les chiffres dans la liste ci-dessus.")

    # --------------------------------------------------------
    # CHOIX 4 : Tous les dossiers
    # --------------------------------------------------------
    elif choix == "4":
        dossiers = dossiers_csv

    # --------------------------------------------------------
    # CHOIX 5 : Rafraîchir
    # --------------------------------------------------------
    elif choix == "5":
        return "REFRESH", None

    # --------------------------------------------------------
    # CHOIX 6 : Quitter (référence péon Warcraft 3 !)
    # --------------------------------------------------------
    elif choix == "6":
        print("\nTravail terminé ! 🪓")
        return "QUIT", None  # ← Signal clair pour quitter

    # --------------------------------------------------------
    # CHOIX INVALIDE → retour au menu
    # --------------------------------------------------------
    else:
        print("\n⚠️ Choix invalide ! Entrez un nombre entre 1 et 6.")
        return "INVALID", None  # ← Retour au menu

    # --------------------------------------------------------
    # AFFICHAGE ET CONFIRMATION (sauf choix 3 qui a déjà confirmé)
    # --------------------------------------------------------
    if choix != "3":
        print(f"\n📋 {len(dossiers)} dossier(s) à traiter :\n")
        for d in dossiers:
            print(f"  - {d['numero_formate']:<22} | {d['commune']:<30} | {d['date_depart']}")

        confirmation = input(
            f"\nConfirmer le traitement de ces {len(dossiers)} dossier(s) ? (O/N) : "
        ).strip().upper()

        if confirmation != "O":
            print("\n❌ Traitement annulé. Retour au menu.")
            return "INVALID", None  # ← Retour au menu (pas de quit !)

    return dossiers, choix


# ============================================================
# TROUVER ET OUVRIR UN DOSSIER DANS LE TABLEAU
# ============================================================

def trouver_et_ouvrir_dossier(page_avisau, dossier, config):
    """
    Cherche le dossier dans le tableau (par numéro brut)
    et clique sur la ligne. Gère la pagination si nécessaire.
    Retourne (True, url) si trouvé, (False, None) sinon.
    """
    numero_brut    = dossier["numero_brut"]
    numero_formate = dossier["numero_formate"]
    print(f"🔍 Recherche du dossier : {numero_formate}")

    if "consultation" not in page_avisau.url or "/" in page_avisau.url.split("consultation")[1]:
        page_avisau.goto(
            "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
        )
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.select_option("#select", label=config["service"])
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.wait_for_timeout(2000)

    while True:
        page_avisau.wait_for_selector(
            "#onglet-attente-metier-panel > avisau-tab-attente-metier > div > table > tbody",
            timeout=10000
        )
        page_avisau.wait_for_timeout(2000)

        lignes = page_avisau.locator(
            "#onglet-attente-metier-panel > avisau-tab-attente-metier > div > table > tbody > tr"
        )
        nombre_lignes = lignes.count()

        for i in range(nombre_lignes):
            ligne = lignes.nth(i)
            if numero_brut in ligne.inner_text():
                print(f"✅ Dossier trouvé à la ligne {i + 1}")
                ligne.click()
                page_avisau.wait_for_load_state("networkidle")
                page_avisau.wait_for_timeout(2000)
                url_dossier = page_avisau.url
                return True, url_dossier

        bouton_suivant = page_avisau.locator(
            "button[aria-label='Page suivante'], a[aria-label='Page suivante']"
        )

        if bouton_suivant.count() > 0 and bouton_suivant.is_enabled():
            print("➡️ Page suivante...")
            bouton_suivant.click()
            page_avisau.wait_for_load_state("networkidle")
            page_avisau.wait_for_timeout(2000)
        else:
            print(f"⚠️ Dossier {numero_formate} non trouvé dans le tableau !")
            return False, None

# ============================================================
# EXTRACTION DE LA NATURE DES TRAVAUX
# ============================================================

def extraire_nature_travaux(page_avisau):
    """
    Extrait la nature des travaux en essayant plusieurs approches.
    ⚠️ Le texte est souvent tronqué avec un lien "Voir plus"
    → Il faut cliquer sur "Voir plus" pour obtenir le texte complet.
    """
    nature = ""

    # Essai 1 : Sélecteur précis avec conteneur-detail-consultation
    try:
        el = page_avisau.locator("#conteneur-detail-consultation #texte")
        if el.count() > 0:
            texte = el.first.inner_text().strip()
            if texte:
                nature = texte
                print(f"✅ Nature travaux (essai 1) : {nature[:50]}...")
    except:
        pass

    # Essai 2 : Cliquer sur "Voir plus" si texte vide ou tronqué
    if not nature:
        try:
            voir_plus = page_avisau.locator("text=Voir plus")
            if voir_plus.count() > 0:
                print("🖱️ Clic sur 'Voir plus'...")
                voir_plus.first.click()
                page_avisau.wait_for_timeout(1000)
                el = page_avisau.locator("#conteneur-detail-consultation #texte")
                if el.count() > 0:
                    texte = el.first.inner_text().strip()
                    if texte:
                        nature = texte
                        print(f"✅ Nature travaux (après Voir plus) : {nature[:50]}...")
        except:
            pass

    # Essai 3 : Sélecteurs alternatifs
    if not nature:
        try:
            selecteurs_alternatifs = [
                "#conteneur-info-dossier #texte",
                "[id='texte']",
                ".fr-text--lead",
                "p[id='texte']",
                "span[id='texte']",
                "div[id='texte']"
            ]
            for sel in selecteurs_alternatifs:
                el = page_avisau.locator(sel)
                if el.count() > 0:
                    texte = el.first.inner_text().strip()
                    if texte:
                        nature = texte
                        print(f"✅ Nature travaux (essai 3) : {nature[:50]}...")
                        break
        except:
            pass

    # Essai 4 : Chercher le texte après "Descriptif de la demande"
    if not nature:
        try:
            el = page_avisau.locator(
                "h2:has-text('Descriptif'), h3:has-text('Descriptif')"
            )
            if el.count() > 0:
                parent = el.locator("xpath=following-sibling::*[1]")
                texte = parent.inner_text().strip()
                if texte:
                    nature = texte
                    print(f"✅ Nature travaux (essai 4) : {nature[:50]}...")
        except:
            pass

    if not nature:
        print("⚠️ Nature des travaux non trouvée - champ laissé vide")
        nature = ""

    return nature

# ============================================================
# EXTRACTION DES DONNÉES DU DOSSIER
# ============================================================

def extraire_donnees_dossier(page_avisau):
    print("📖 Extraction approfondie des données...")
    try:
        # 1. Numéro de dossier
        num = page_avisau.locator("#conteneur-info-dossier div:nth-child(4) div div:nth-child(2) div").inner_text().strip()
        
        # 2. Adresse des travaux
        adr = page_avisau.locator("#conteneur-info-dossier div:nth-child(3) div div:nth-child(1) div:nth-child(2)").inner_text().strip()
        
        # 3. Nom du pétitionnaire
        pet = page_avisau.locator("#conteneur-info-dossier div:nth-child(8) div:nth-child(2) div:nth-child(1) div").first.inner_text().strip()
        
        # 4. Parcelle
        try:
            par = page_avisau.locator("#conteneur-info-dossier div:nth-child(3) div div:nth-child(2) dl dt").inner_text().strip()
        except:
            par = "N/A"

        # 5. NATURE DES TRAVAUX (Logique multi-tentatives)
        nature = ""
        
        # On essaie d'ouvrir tous les "Voir plus" de la page pour être sûr
        try:
            clics_voir_plus = page_avisau.get_by_text("Voir plus")
            for i in range(clics_voir_plus.count()):
                clics_voir_plus.nth(i).click()
            page_avisau.wait_for_timeout(500)
        except:
            pass

        # Tentative A : Le sélecteur standard
        try:
            nature = page_avisau.locator("#conteneur-detail-consultation #texte").first.inner_text().strip()
        except:
            pass

        # Tentative B (Si vide) : Chercher par le titre "Descriptif"
        if not nature:
            try:
                # On cherche le texte qui suit directement le titre "Descriptif de la demande"
                nature = page_avisau.locator("h2:has-text('Descriptif'), h3:has-text('Descriptif')").locator("xpath=following-sibling::*").first.inner_text().strip()
            except:
                pass

        # Tentative C : Chercher n'importe quel élément avec l'ID texte
        if not nature:
            try:
                nature = page_avisau.locator("[id='texte']").first.inner_text().strip()
            except:
                nature = "Non extraite (Vérifier manuellement)"

        # 6. ADRESSE PÉTITIONNAIRE
        adresse_pet = "Non extraite"
        try:
            rue_pet = page_avisau.locator("#conteneur-info-dossier div:nth-child(7) div div:nth-child(1) div:nth-child(2)").inner_text().strip()
            ville_pet = page_avisau.locator("#conteneur-info-dossier div:nth-child(7) div div:nth-child(1) div:nth-child(3)").inner_text().strip()
            adresse_pet = f"{rue_pet}, {ville_pet}"
        except:
            pass
            
        return {
            "numero_brut": num, 
            "numero_formate": formater_numero_dossier(num), 
            "adresse_travaux": adr, 
            "nom_petitionnaire": pet, 
            "parcelle": par,
            "nature_travaux": nature,
            "adresse_petitionnaire": adresse_pet
        }
    except Exception as e:
        print(f"❌ Erreur extraction : {e}")
        return {
            "numero_brut": "ERR", "numero_formate": "ERR", "adresse_travaux": "", 
            "nom_petitionnaire": "", "parcelle": "", "nature_travaux": "", "adresse_petitionnaire": ""
        }

# ============================================================
# TÉLÉCHARGEMENT DES PIÈCES
# ============================================================

def telecharger_pieces(page_avisau, config):
    """
    Clique sur l'onglet 'Toutes les pièces fournies'
    puis télécharge le ZIP de toutes les pièces.
    """
    print("📥 Téléchargement des pièces...")

    page_avisau.click(
        "#conteneur-pj-dossier > div > fieldset > div > div:nth-child(2) > label"
    )
    page_avisau.wait_for_timeout(2000)
    print("✅ Onglet 'Toutes les pièces fournies' sélectionné")

    page_avisau.wait_for_selector(
        "#conteneur-pj-dossier > div > div > button",
        timeout=10000
    )

    chemin_zip = Path(config["dossier_telechargement"]) / "pieces_temp.zip"

    with page_avisau.expect_download(timeout=120000) as download_info:
        page_avisau.click("#conteneur-pj-dossier > div > div > button")
        print("⏳ Compression en cours côté serveur...")

    download = download_info.value
    download.save_as(chemin_zip)
    print(f"✅ ZIP téléchargé")

    return chemin_zip

# ============================================================
# NETTOYAGE DU NOM DE DOSSIER
# ============================================================

def nettoyer_nom_dossier(nom):
    """
    Supprime les caractères interdits dans les noms
    de fichiers/dossiers Windows : < > : " / \\ | ? *
    """
    return re.sub(r'[<>:"/\\|?*]', "_", nom).strip()

# ============================================================
# EXTRACTION DU ZIP ET CRÉATION DU DOSSIER
# ============================================================

def extraire_zip_et_creer_dossier(chemin_zip, donnees, config):
    """
    Extrait le ZIP dans un dossier nommé :
    "{Numéro formaté}-{Adresse rue du projet}"
    """
    nom_dossier = nettoyer_nom_dossier(
        f"{donnees['numero_formate']}-{donnees['adresse_travaux']}"
    )

    chemin_dossier = Path(config["dossier_destination"]) / nom_dossier
    chemin_dossier.mkdir(parents=True, exist_ok=True)

    print(f"📁 Extraction dans : {chemin_dossier}")

    with zipfile.ZipFile(chemin_zip, "r") as zip_ref:
        zip_ref.extractall(chemin_dossier)

    print(f"✅ ZIP extrait avec succès !")
    os.remove(chemin_zip)

    return chemin_dossier

# ============================================================
# PRÉ-REMPLISSAGE DU FORMULAIRE PDF
# ============================================================

def preremplir_formulaire_pdf(donnees, config, avis_aep="", avis_eu=""):
    """
    Copie le formulaire PDF vierge et le pré-remplit.
    Nom du fichier : Formulaire_{numero_formate}.pdf
    Sauvegarde dans formulaire_dossier_sortie.
    Le modèle original reste intact.

    Champs remplis automatiquement (5 uniquement) :
    - numéro dossier       → numéro formaté
    - nom pétitionnaire    → nom(s) séparés par " et "
    - adresse pétitionnaire → rue + code postal + ville
    - nature travaux       → texte descriptif
    - adresse travaux      → rue uniquement

    Champs NON touchés (à remplir manuellement) :
    - SEMM, SAOM, SAEM (zones colorées)
    - AEP, EU
    - Cases à cocher : individualisation, servitudes, dispo_generales
    - Cases EU : servitudes_eu, dispo_generales_eu
    - alignement
    """
    print("📝 Pré-remplissage du formulaire PDF...")

    chemin_modele = str(Path(config["formulaire_pdf"]))
    nom_fichier   = f"Formulaire_{donnees['numero_formate']}.pdf"
    chemin_rempli = str(
        Path(config["formulaire_dossier_sortie"]) / nom_fichier
    )

    # Protection contre les valeurs None
    def safe_str(valeur):
        if valeur is None:
            return ""
        return str(valeur).strip()

    try:
        fillpdfs.write_fillable_pdf(
            chemin_modele,
            chemin_rempli,
            {
                "numéro dossier"        : safe_str(donnees.get("numero_formate")),
                "nom pétitionnaire"     : safe_str(donnees.get("nom_petitionnaire")),
                "adresse pétitionnaire" : safe_str(donnees.get("adresse_petitionnaire")),
                "nature travaux"        : safe_str(donnees.get("nature_travaux")),
                "adresse travaux"       : safe_str(donnees.get("adresse_travaux"))
            }
        )
        print(f"✅ Formulaire sauvegardé : {nom_fichier}")
        return chemin_rempli

    except Exception as e:
        print(f"❌ Erreur remplissage PDF : {e}")
        print(f"   Données utilisées :")
        print(f"   - numéro dossier        : '{safe_str(donnees.get('numero_formate'))}'")
        print(f"   - nom pétitionnaire     : '{safe_str(donnees.get('nom_petitionnaire'))}'")
        print(f"   - adresse pétitionnaire : '{safe_str(donnees.get('adresse_petitionnaire'))}'")
        print(f"   - nature travaux        : '{safe_str(donnees.get('nature_travaux'))}'")
        print(f"   - adresse travaux       : '{safe_str(donnees.get('adresse_travaux'))}'")
        return None

# ============================================================
# MISE À JOUR GOOGLE SHEETS
# ============================================================

def convertir_date(date_str):
    """Convertit AAAA-MM-JJ en JJ/MM/AAAA"""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    return date_obj.strftime("%d/%m/%Y")

def naviguer_vers_cellule_sheets(page_sheets, cellule):
    """
    Navigue vers une cellule spécifique dans Google Sheets
    via JavaScript sur la Name Box.
    """
    page_sheets.evaluate("""
        (cellule) => {
            const inputs = document.querySelectorAll('input');
            for (const input of inputs) {
                const val = input.value || '';
                if (/^[A-Z]+[0-9]+$/.test(val) ||
                    input.getAttribute('aria-label') === 'Name Box') {
                    input.click();
                    input.focus();
                    input.select();
                    input.value = cellule;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(
                        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
                    );
                    return true;
                }
            }
            return false;
        }
    """, cellule)
    page_sheets.wait_for_timeout(500)
    page_sheets.keyboard.press("Enter")
    page_sheets.wait_for_timeout(500)

def get_numero_ligne_sheets(page_sheets):
    """Récupère le numéro de ligne de la cellule active via JavaScript."""
    try:
        valeur = page_sheets.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input');
                for (const input of inputs) {
                    const val = input.value || '';
                    if (/^[A-Z]+[0-9]+$/.test(val)) return val;
                }
                return '';
            }
        """)
        if valeur:
            return ''.join(filter(str.isdigit, valeur))
    except:
        pass
    return ""

def ecrire_dans_cellule_sheets(page_sheets, cellule, valeur):
    """Navigue vers une cellule et écrit une valeur."""
    naviguer_vers_cellule_sheets(page_sheets, cellule)
    page_sheets.keyboard.type(str(valeur))
    page_sheets.keyboard.press("Enter")
    page_sheets.wait_for_timeout(300)

def verifier_doublon_sheets(page_sheets, numero_formate):
    """
    Vérifie si le numéro de dossier existe déjà dans le Google Sheets.

    Approche fiable :
    1. Naviguer vers K2 (cellule de référence connue)
    2. Ctrl+F → taper le numéro formaté
    3. Appuyer sur ENTRÉE
    4. Lire la cellule active APRÈS la recherche
    5. Si la cellule active a changé → Google Sheets a trouvé et navigué
       vers un résultat → dossier déjà présent !

    Retourne True si déjà présent, False sinon.
    """
    print(f"🔍 Vérification doublon pour : '{numero_formate}'...")

    # S'assurer qu'on est sur la bonne feuille
    try:
        page_sheets.click("text=Suivi intructions_Hors MARS")
        page_sheets.wait_for_timeout(2000)
    except:
        pass

    try:
        # Naviguer vers K2 comme cellule de référence connue
        naviguer_vers_cellule_sheets(page_sheets, "K2")
        page_sheets.wait_for_timeout(500)

        # Lire la cellule active AVANT la recherche
        cellule_avant = get_numero_ligne_sheets(page_sheets)
        print(f"   Cellule avant recherche : '{cellule_avant}'")

        # Ouvrir Ctrl+F
        page_sheets.evaluate("() => document.body.click()")
        page_sheets.wait_for_timeout(300)
        page_sheets.keyboard.press("Control+f")
        page_sheets.wait_for_timeout(1500)

        # Taper le numéro formaté (avec espaces, comme il est stocké dans le Sheets)
        page_sheets.keyboard.type(numero_formate)
        page_sheets.wait_for_timeout(2000)

        # Appuyer sur ENTRÉE pour naviguer vers le résultat
        page_sheets.keyboard.press("Enter")
        page_sheets.wait_for_timeout(1000)

        # Fermer la recherche
        page_sheets.keyboard.press("Escape")
        page_sheets.wait_for_timeout(500)

        # Lire la cellule active APRÈS la recherche
        cellule_apres = get_numero_ligne_sheets(page_sheets)
        print(f"   Cellule après recherche : '{cellule_apres}'")

        # Si la cellule active a changé → Google Sheets a trouvé un résultat
        # et a navigué vers lui
        if cellule_apres and cellule_apres != cellule_avant:
            print(f"⚠️ Dossier {numero_formate} déjà présent ! (cellule : {cellule_apres})")
            return True

        print(f"✅ Dossier non trouvé → Ajout en cours...")
        return False

    except Exception as e:
        print(f"⚠️ Erreur vérification doublon : {e}")
        try:
            page_sheets.keyboard.press("Escape")
        except:
            pass
        return False

def mettre_a_jour_sheets(page_sheets, donnees, config):
    """
    Met à jour le Google Sheets avec les données du dossier.
    La fenêtre Google Sheets reste ouverte entre les traitements.
    Feuille 1 : Suivi intructions_Hors MARS → colonnes D, K ou L, M
    Feuille 2 : suivi PFAC prérempli → colonnes F, G (même ligne)
    Retourne le numéro de ligne utilisé (pour insertion du lien Drive).
    """
    print("📊 Mise à jour du Google Sheets...")

    if "spreadsheets" not in page_sheets.url:
        page_sheets.goto(
            config["google_sheets_url"],
            timeout=60000,
            wait_until="domcontentloaded"
        )
        try:
            page_sheets.wait_for_selector(
                ".grid-container, .waffle, canvas", timeout=30000
            )
        except:
            page_sheets.wait_for_timeout(5000)
        page_sheets.wait_for_timeout(3000)

    print("✅ Google Sheets prêt !")

    # --------------------------------------------------------
    # VÉRIFICATION DOUBLON - APPROCHE FIABLE
    # --------------------------------------------------------
    deja_present = verifier_doublon_sheets(page_sheets, donnees["numero_formate"])

    if deja_present:
        print(f"⚠️ Dossier {donnees['numero_formate']} déjà présent dans le Sheets !")
        return None  # Retourne None si déjà présent

    # --------------------------------------------------------
    # FEUILLE 1 : Suivi intructions_Hors MARS
    # --------------------------------------------------------
    print("📋 Navigation vers 'Suivi intructions_Hors MARS'...")
    page_sheets.click("text=Suivi intructions_Hors MARS")
    page_sheets.wait_for_timeout(2000)

    naviguer_vers_cellule_sheets(page_sheets, "D2")
    page_sheets.wait_for_timeout(500)
    page_sheets.keyboard.press("Control+ArrowDown")
    page_sheets.wait_for_timeout(500)
    page_sheets.keyboard.press("ArrowDown")
    page_sheets.wait_for_timeout(500)

    numero_ligne = get_numero_ligne_sheets(page_sheets)
    if not numero_ligne:
        print("⚠️ Fallback : ligne 302")
        numero_ligne = "302"
    print(f"✅ Première ligne vide : ligne {numero_ligne}")

    # Colonne D : Commune
    commune_formatee = formater_commune_sheets(donnees["commune"])
    print(f"📝 D{numero_ligne} : {commune_formatee} (depuis : {donnees['commune']})")
    ecrire_dans_cellule_sheets(page_sheets, f"D{numero_ligne}", commune_formatee)


    # Colonne K ou L : Numéro dossier
    type_dossier   = donnees["numero_brut"][:2].upper()
    colonne_numero = "K" if type_dossier in ["PC", "PA"] else "L"
    ecrire_dans_cellule_sheets(
        page_sheets, f"{colonne_numero}{numero_ligne}", donnees["numero_formate"]
    )

    # Colonne M : Date
    date_formatee = convertir_date(donnees["date_depart"])
    ecrire_dans_cellule_sheets(page_sheets, f"M{numero_ligne}", date_formatee)
    print("✅ Feuille 1 remplie !")

    # --------------------------------------------------------
    # FEUILLE 2 : suivi PFAC prérempli
    # --------------------------------------------------------
    print("📋 Navigation vers 'suivi PFAC prérempli'...")
    page_sheets.click("text=suivi PFAC prérempli")
    page_sheets.wait_for_timeout(2000)

    ecrire_dans_cellule_sheets(
        page_sheets, f"F{numero_ligne}", donnees["adresse_travaux"]
    )
    ecrire_dans_cellule_sheets(
        page_sheets, f"G{numero_ligne}", donnees["nom_petitionnaire"]
    )
    print(f"✅ Google Sheets mis à jour ! (ligne {numero_ligne})")

    return numero_ligne  # Retourne le numéro de ligne pour le lien Drive

# ============================================================
# OUVRIR LES FICHIERS D'UN DOSSIER
# ============================================================

def ouvrir_fichiers_dossier(chemin_dossier):
    """
    Ouvre tous les fichiers du dossier
    avec les applications par défaut de Windows.
    """
    if not chemin_dossier or not chemin_dossier.exists():
        print("⚠️ Dossier introuvable, impossible d'ouvrir les fichiers")
        return

    fichiers = list(chemin_dossier.iterdir())

    if not fichiers:
        print("⚠️ Aucun fichier dans le dossier")
        return

    print(f"\n📂 Ouverture des fichiers du dossier : {chemin_dossier.name}")
    print(f"   {len(fichiers)} fichier(s) à ouvrir...")

    for fichier in fichiers:
        try:
            os.startfile(str(fichier))
            print(f"   ✅ Ouvert : {fichier.name}")
        except Exception as e:
            print(f"   ⚠️ Impossible d'ouvrir {fichier.name} : {e}")

# ============================================================
# ATTENDRE CONFIRMATION "DOSSIER TERMINÉ"
# ============================================================

def attendre_dossier_termine(chemin_dossier, config, interface=None, donnees=None, societe=None):
    """
    Affiche l'interface de saisie des avis si interface disponible,
    sinon fallback terminal.
    """
    # ── MODE GUI ──────────────────────────────────────────────
    if interface and donnees and societe:
        interface.afficher_etape_avis(donnees, societe)
        interface.relancer()

        action = interface.resultat_avis.get("action")

        if action == "ATTENTE":
            dossier_en_attente = Path(config["dossier_en_attente"])
            dossier_en_attente.mkdir(parents=True, exist_ok=True)
            nom_dossier = Path(chemin_dossier).name
            destination = dossier_en_attente / nom_dossier
            try:
                import shutil
                shutil.move(str(chemin_dossier), str(destination))
                print(f"✅ Dossier déplacé vers : {destination}")
            except Exception as e:
                print(f"❌ Erreur déplacement : {e}")
            return False

        if action == "PASSER":
            return False

        return True  # action == "ENVOYER"

    # ── MODE TERMINAL (fallback) ───────────────────────────────
    while True:
        pret = input("\nDossier prêt à être envoyé ? (O/N) : ").strip().upper()
        if pret == "O":
            confirmation = input("Confirmation (O/N) : ").strip().upper()
            if confirmation == "O":
                return True
        elif pret == "N":
            en_attente = input("Dossier mis en attente ? (O/N) : ").strip().upper()
            if en_attente == "O":
                dossier_en_attente = Path(config["dossier_en_attente"])
                dossier_en_attente.mkdir(parents=True, exist_ok=True)
                nom_dossier = Path(chemin_dossier).name
                destination = dossier_en_attente / nom_dossier
                try:
                    import shutil
                    shutil.move(str(chemin_dossier), str(destination))
                    print(f"✅ Dossier déplacé vers : {destination}")
                except Exception as e:
                    print(f"❌ Erreur déplacement : {e}")
                return False


def saisir_avis_gui(interface, type_avis):
    """
    Récupère l'avis depuis l'interface GUI.
    type_avis : "AEP" ou "EU"
    """
    if type_avis == "AEP":
        return interface.resultat_avis.get("avis_aep", "")
    else:
        return interface.resultat_avis.get("avis_eu", "")


# ============================================================
# SAISIE DE L'AVIS
# ============================================================

def saisir_avis(type_avis):
    """
    Demande à l'utilisateur de saisir un avis.
    type_avis : "AEP" ou "EU"
    Retourne la valeur normalisée.
    """
    print(f"\n{'=' * 60}")
    print(f"  AVIS {type_avis}")
    print(f"{'=' * 60}")
    print(f"\nValeurs acceptées :")
    print(f"  - favorable")
    print(f"  - favorable avec réserves")
    print(f"  - défavorable")
    print(f"  - incomplet")
    print(f"  - refus")

    while True:
        avis = input(f"\nAvis du service ({type_avis}) : ").strip().lower()

        if avis in ["favorable", "fav", "f"]:
            return "favorable"
        elif avis in [
            "favorable avec réserves", "favorable avec reserves",
            "avec réserves", "avec reserves", "réserves", "reserves",
            "fav avec réserves", "fav avec reserves", "far"
        ]:
            return "favorable avec réserves"
        elif avis in ["défavorable", "defavorable", "def", "d"]:
            return "défavorable"
        elif avis in ["incomplet", "inc", "i"]:
            return "incomplet"
        elif avis in ["refus", "ref", "r"]:
            return "refus"
        else:
            print(f"⚠️ Valeur invalide !")
            print(f"   Entrez : favorable / favorable avec réserves / défavorable / incomplet / refus")

# ============================================================
# TROUVER LE PDF D'AVIS DANS LE DOSSIER
# ============================================================

def trouver_pdf_avis(chemin_dossier, type_avis):
    """
    Cherche le PDF d'avis dans le dossier du dossier en cours.
    type_avis : "AEP" ou "EU"
    Le fichier doit se terminer par " AEP.pdf" ou " EU.pdf"
    Retourne le chemin complet du fichier ou None.
    """
    chemin = Path(chemin_dossier)
    for fichier in chemin.iterdir():
        if (fichier.suffix.lower() == ".pdf" and
                fichier.stem.endswith(f" {type_avis}")):
            return str(fichier)
    return None

# ============================================================
# TRAITEMENT D'UN AVIS (AEP ou EU)
# ============================================================

def traiter_avis(page_avisau, avis, type_avis, chemin_dossier):
    """
    Traite l'avis AEP ou EU sur Avis'AU.

    WORKFLOW COMPLET :
    1. Clic "Accepter la prise en compte métier"
    2. POPUP 1 - Confirmation 1 → Clic bouton
    3. POPUP 1 - Confirmation 2 → Clic bouton (même popup, étape suivante)
    4. Nouveau bouton "Émettre un avis" → Clic
    5. POPUP 2 (modal-emission-avis) → Remplir + Téléverser PDF
    6. Attendre votre clic sur "Valider"
    """
    print(f"\n📋 Traitement de l'avis {type_avis} : {avis}")

    # --------------------------------------------------------
    # CAS REFUS
    # --------------------------------------------------------
    if avis == "refus":
        print(f"🖱️ Clic sur 'Refuser la prise en compte métier'...")
        page_avisau.click(
            "#main-content > div > app-avisau-detail-consultation > div > div > "
            "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
            "div:nth-child(3) > ul > li:nth-child(2) > button"
        )
        page_avisau.wait_for_timeout(2000)
        print("✅ Bouton Refuser cliqué !")
        input("\n⏸️ PAUSE - Appuyez sur ENTRÉE pour continuer...")
        return

    # --------------------------------------------------------
    # CAS FAVORABLE / AVEC RÉSERVES / DÉFAVORABLE / INCOMPLET
    # --------------------------------------------------------

    # ÉTAPE 1 : Clic sur "Accepter la prise en compte métier"
    print(f"🖱️ Clic sur 'Accepter la prise en compte métier'...")
    page_avisau.click(
        "#main-content > div > app-avisau-detail-consultation > div > div > "
        "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
        "div:nth-child(3) > ul > li:nth-child(1) > button"
    )
    page_avisau.wait_for_timeout(2000)
    print("✅ 1er bouton cliqué !")

    # --------------------------------------------------------
    # ÉTAPE 2 : POPUP 1 - CONFIRMATION 1
    # Sélecteur générique (fonctionne pour dialog-0, dialog-1, etc.)
    # --------------------------------------------------------
    print(f"🖱️ POPUP 1 - Confirmation 1...")
    try:
        page_avisau.wait_for_selector(
            "[id^='mat-mdc-dialog'] avisau-modal-acceptation "
            "li.bouton-droite > button",
            timeout=10000
        )
        page_avisau.click(
            "[id^='mat-mdc-dialog'] avisau-modal-acceptation "
            "li.bouton-droite > button"
        )
        page_avisau.wait_for_timeout(2000)
        print("✅ POPUP 1 - Confirmation 1 cliquée !")
    except PlaywrightTimeoutError:
        print("⚠️ POPUP 1 Confirmation 1 non détectée, on continue...")

    # --------------------------------------------------------
    # ÉTAPE 3 : POPUP 1 - CONFIRMATION 2
    # Même popup, nouvelle étape de confirmation
    # Sélecteur : #mat-mdc-dialog-0 > ... > li.bouton-droite > button
    # On utilise le sélecteur générique pour robustesse
    # --------------------------------------------------------
    print(f"🖱️ POPUP 1 - Confirmation 2...")
    try:
        page_avisau.wait_for_selector(
            "[id^='mat-mdc-dialog'] avisau-modal-acceptation "
            "li.bouton-droite > button",
            timeout=10000
        )
        page_avisau.click(
            "[id^='mat-mdc-dialog'] avisau-modal-acceptation "
            "li.bouton-droite > button"
        )
        page_avisau.wait_for_timeout(2000)
        print("✅ POPUP 1 - Confirmation 2 cliquée ! POPUP 1 fermée.")
    except PlaywrightTimeoutError:
        print("⚠️ POPUP 1 Confirmation 2 non détectée, on continue...")

    # --------------------------------------------------------
#   ÉTAPE 4 : Attendre que le bouton change en "Émettre un avis"
#   puis cliquer dessus
#   --------------------------------------------------------
    print(f"⏳ Attente du changement du bouton en 'Émettre un avis'...")
    try:
        # Attendre que le bouton contienne le texte "Émettre"
        # Cela garantit que la POPUP 1 est bien fermée
        # et que le bouton a changé
        page_avisau.wait_for_selector(
            "#main-content > div > app-avisau-detail-consultation > div > div > "
            "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
            "div:nth-child(3) > ul > li:nth-child(1) > button:has-text('mettre')",
            timeout=15000
        )
        texte_btn = page_avisau.locator(
            "#main-content > div > app-avisau-detail-consultation > div > div > "
            "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
            "div:nth-child(3) > ul > li:nth-child(1) > button"
        ).inner_text().strip()
        print(f"✅ Bouton prêt ! Texte : '{texte_btn}'")

        page_avisau.click(
            "#main-content > div > app-avisau-detail-consultation > div > div > "
            "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
            "div:nth-child(3) > ul > li:nth-child(1) > button"
        )
        page_avisau.wait_for_timeout(2000)
        print("✅ Bouton 'Émettre un avis' cliqué ! POPUP 2 ouverte.")

    except PlaywrightTimeoutError:
        print("⚠️ Timeout attente bouton 'Émettre un avis'")
        print("   Tentative de clic quand même...")
        try:
            page_avisau.click(
                "#main-content > div > app-avisau-detail-consultation > div > div > "
                "div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > "
                "div:nth-child(3) > ul > li:nth-child(1) > button"
            )
            page_avisau.wait_for_timeout(2000)
            print("✅ Bouton cliqué !")
        except:
            print("❌ Impossible de cliquer sur le bouton !")
            return

    # --------------------------------------------------------
    # ÉTAPE 5 : Remplir le formulaire d'avis (POPUP 2)
    # --------------------------------------------------------
    print(f"📝 Remplissage du formulaire d'avis {type_avis}...")

    # Attendre que le formulaire soit bien chargé
    page_avisau.wait_for_selector("#nature", timeout=10000)
    page_avisau.wait_for_timeout(500)

    # Nature de l'avis - via select_option (menu déroulant)

    page_avisau.select_option("#nature", index=MAPPING_NATURE_INDEX[avis])
    page_avisau.wait_for_timeout(500)
    print(f"✅ Nature sélectionnée : {avis}")

    # Type - via select_option (menu déroulant)
    page_avisau.wait_for_selector("#type", timeout=5000)
    page_avisau.select_option("#type", index=1)  # option[2] = index 1
    page_avisau.wait_for_timeout(500)
    print("✅ Type sélectionné")

    # Qualité
    page_avisau.fill("#qualite", "Technicien urbanisme")
    page_avisau.wait_for_timeout(500)
    print("✅ Qualité : Technicien urbanisme")

    # Texte de l'avis
    texte_avis = f"{avis.capitalize()} {type_avis}"
    page_avisau.fill("#texteAvis", texte_avis)
    page_avisau.wait_for_timeout(500)
    print(f"✅ Texte avis : {texte_avis}")

    # --------------------------------------------------------
    # ÉTAPE 6 : Chercher et téléverser le PDF
    # --------------------------------------------------------
    pdf_avis = trouver_pdf_avis(chemin_dossier, type_avis)

    if not pdf_avis:
        print(f"\n⚠️ PDF se terminant par ' {type_avis}.pdf' non trouvé dans :")
        print(f"   {chemin_dossier}")
        print(f"\n💡 Créez le fichier PDF et placez-le dans ce dossier.")
        print(f"   Nom attendu : [nom] {type_avis}.pdf")

        while True:
            input(f"\nAppuyez sur ENTRÉE une fois le PDF créé...")
            pdf_avis = trouver_pdf_avis(chemin_dossier, type_avis)
            if pdf_avis:
                print(f"✅ PDF trouvé : {Path(pdf_avis).name}")
                break
            else:
                print(f"⚠️ Toujours pas trouvé ! Vérifiez le nom du fichier.")
    else:
        print(f"✅ PDF trouvé : {Path(pdf_avis).name}")

    print(f"📎 Téléversement automatique du PDF {type_avis}...")

    with page_avisau.expect_file_chooser() as fc_info:
        page_avisau.click(
            "[id^='mat-mdc-dialog'] avisau-modal-emission-avis "
            "versement-fichier div.actions label > button"
        )

    file_chooser = fc_info.value
    file_chooser.set_files(pdf_avis)
    page_avisau.wait_for_timeout(2000)
    print(f"✅ PDF téléversé : {Path(pdf_avis).name}")

    # --------------------------------------------------------
    # ÉTAPE 7 : Attendre votre clic sur "Valider" (POPUP 2)
    # --------------------------------------------------------
    print(f"\n⏳ En attente de votre clic sur 'Valider'...")
    print(f"   Vérifiez les informations puis cliquez sur 'Valider'.")

    try:
        page_avisau.wait_for_selector(
            "[id^='mat-mdc-dialog'] avisau-modal-emission-avis "
            "li.bouton-droite > button",
            state="hidden",
            timeout=300000
        )
    except PlaywrightTimeoutError:
        input("⏸️ Si le formulaire est validé, appuyez sur ENTRÉE...")

    page_avisau.wait_for_load_state("networkidle")
    page_avisau.wait_for_timeout(2000)
    print(f"✅ Avis {type_avis} '{avis}' transmis avec succès !")


# ============================================================
# TRAITEMENT COMPLET D'UN DOSSIER (AVIS AEP + EU si nécessaire)
# ============================================================

def traiter_avis_complet(page_avisau, donnees, chemin_dossier, config, interface=None):
    """
    Gère le traitement complet des avis pour un dossier.
    Utilise l'interface GUI si disponible.
    """
    commune = donnees["commune"]
    societe = get_societe(commune)

    if not societe:
        print(f"⚠️ Société non déterminée pour '{commune}'")
        return "", ""

    print(f"\n🏢 Société détectée : {societe} (commune : {commune})")

    # ÉTAPE 1 : Confirmation + saisie avis via GUI
    dossier_pret = attendre_dossier_termine(
        chemin_dossier, config,
        interface=interface,
        donnees=donnees,
        societe=societe
    )

    if not dossier_pret:
        action = interface.resultat_avis.get("action") if interface else None
        if action == "ATTENTE":
            return "EN_ATTENTE", "EN_ATTENTE"
        return "PASSE", "PASSE"

    # ÉTAPE 2 : Récupérer les avis
    if interface:
        avis_aep = saisir_avis_gui(interface, "AEP")
        avis_eu  = saisir_avis_gui(interface, "EU")
    else:
        avis_aep = saisir_avis("AEP")
        avis_eu  = ""

    # Réinitialiser pour le prochain dossier
    if interface:
        interface.resultat_avis = {
            "dossier_pret" : None,
            "avis_aep"     : None,
            "avis_eu"      : None,
            "action"       : None
        }

    # ÉTAPE 3 : Traiter l'avis AEP
    traiter_avis(page_avisau, avis_aep, "AEP", chemin_dossier)

    # ÉTAPE 4 : Traiter l'avis EU si SAOM/SAEM
    if societe in ["SAOM", "SAEM"] and avis_eu:

        print(f"\n🔄 Changement de service vers {societe}...")
        page_avisau.goto(
            "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
        )
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.select_option("#select", label=SERVICES[societe])
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.wait_for_timeout(2000)

        trouve, _ = trouver_et_ouvrir_dossier(page_avisau, donnees, config)

        if not trouve:
            print("\n⚠️ " * 10)
            print(f"⚠️ DOSSIER NON TROUVÉ DANS {societe} !")
            while True:
                mail = input("Avis EU transféré par mail ? (O/N) : ").strip().upper()
                if mail == "O":
                    break
        else:
            traiter_avis(page_avisau, avis_eu, "EU", chemin_dossier)

        # Retour SEMM
        page_avisau.goto(
            "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
        )
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.select_option("#select", label=config["service"])
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.wait_for_timeout(2000)

    else:
        page_avisau.goto(
            "https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier"
        )
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.select_option("#select", label=config["service"])
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.wait_for_timeout(2000)

    return avis_aep, avis_eu


# ============================================================
# UPLOAD DOSSIER VERS GOOGLE DRIVE
# ============================================================

def get_numero_mois():
    """Retourne le mois en cours en 2 digits. Ex: 3 → '03'"""
    return datetime.now().strftime("%m")

def naviguer_dossier_drive(page_drive, nom_dossier, timeout=10000):
    """
    Double-clique sur un dossier dans Google Drive pour l'ouvrir.
    Retourne True si trouvé, False sinon.
    """
    try:
        page_drive.wait_for_timeout(2000)

        # Essai 1 : par aria-label
        dossier = page_drive.locator(f"[aria-label='{nom_dossier}']").first
        if dossier.count() > 0:
            dossier.dblclick(timeout=timeout)
            page_drive.wait_for_timeout(2000)
            return True

        # Essai 2 : par data-tooltip
        dossier = page_drive.locator(f"[data-tooltip='{nom_dossier}']").first
        if dossier.count() > 0:
            dossier.dblclick(timeout=timeout)
            page_drive.wait_for_timeout(2000)
            return True

        # Essai 3 : par texte exact
        dossier = page_drive.get_by_text(nom_dossier, exact=True).first
        if dossier.count() > 0:
            dossier.dblclick(timeout=timeout)
            page_drive.wait_for_timeout(2000)
            return True

        return False
    except:
        return False

def uploader_dossier_google_drive(page_drive, chemin_dossier_local, donnees, config):
    """
    Upload le dossier local vers Google Drive.
    Structure : {annee} / {COMMUNE} / {MM} / {Dossier}
    Retourne l'URL du dossier uploadé dans Google Drive.
    """
    annee             = str(datetime.now().year)
    mois              = get_numero_mois()
    commune_drive     = normaliser_commune_drive(donnees["commune"])
    nom_dossier_local = Path(chemin_dossier_local).name

    print(f"\n📤 Upload vers Google Drive...")
    print(f"   Chemin : {annee} / {commune_drive} / {mois} / {nom_dossier_local}")

    # Ouvrir Google Drive si pas déjà dessus
    if "drive.google.com" not in page_drive.url:
        page_drive.goto(
            config["google_drive_url"],
            timeout=60000,
            wait_until="domcontentloaded"
        )
        page_drive.wait_for_timeout(3000)

    print("✅ Google Drive ouvert !")

    # Navigation vers le bon dossier
    print(f"📁 Navigation vers {annee}...")
    if not naviguer_dossier_drive(page_drive, annee):
        print(f"⚠️ Dossier {annee} non trouvé !")
        input(f"Naviguez manuellement vers le dossier {annee} puis appuyez sur ENTRÉE...")

    print(f"📁 Navigation vers {commune_drive}...")
    if not naviguer_dossier_drive(page_drive, commune_drive):
        print(f"⚠️ Dossier {commune_drive} non trouvé !")
        input(f"Naviguez manuellement vers le dossier {commune_drive} puis appuyez sur ENTRÉE...")

    print(f"📁 Navigation vers {mois}...")
    if not naviguer_dossier_drive(page_drive, mois):
        print(f"⚠️ Dossier {mois} non trouvé !")
        input(f"Naviguez manuellement vers le dossier {mois} puis appuyez sur ENTRÉE...")

    print(f"✅ Dans le bon dossier : {annee}/{commune_drive}/{mois}")

    # --------------------------------------------------------
    # UPLOAD DU DOSSIER VIA RACCOURCI CLAVIER Alt+C puis I
    # --------------------------------------------------------
    print(f"📤 Upload du dossier : {nom_dossier_local}...")

    try:
        # S'assurer que Google Drive a le focus
        page_drive.evaluate("() => document.body.click()")
        page_drive.wait_for_timeout(500)

        # Raccourci Alt+C pour ouvrir le menu "Nouveau"
        page_drive.keyboard.press("Alt+c")
        page_drive.wait_for_timeout(1000)

        # Puis appuyer sur I pour "Importer un dossier"
        # avec interception du file chooser
        with page_drive.expect_file_chooser(timeout=10000) as fc_info:
            page_drive.keyboard.press("i")

        # Sélectionner le dossier local
        fc = fc_info.value
        fc.set_files(str(chemin_dossier_local))
        print(f"✅ Dossier sélectionné : {nom_dossier_local}")

        print(f"⏳ Upload en cours...")
        page_drive.wait_for_timeout(5000)

        # Attendre la notification de fin d'upload
        try:
            page_drive.wait_for_selector(
                "[aria-label='Upload complete'], "
                "text=Upload terminé, "
                "text=Importation terminée, "
                "text=1 importation terminée",
                timeout=120000
            )
            page_drive.wait_for_timeout(2000)
            print(f"✅ Upload terminé !")
        except:
            print(f"⏳ Attente supplémentaire...")
            page_drive.wait_for_timeout(10000)
            print(f"✅ Upload probablement terminé !")

    except Exception as e:
        print(f"⚠️ Erreur upload automatique : {e}")
        print(f"\n{'=' * 60}")
        print(f"  ⏸️  PAUSE - UPLOAD MANUEL REQUIS")
        print(f"{'=' * 60}")
        print(f"\n📂 Uploadez manuellement ce dossier dans Google Drive :")
        print(f"   {chemin_dossier_local}")
        print(f"   → Dans : {annee}/{commune_drive}/{mois}/")
        input("\nAppuyez sur ENTRÉE une fois l'upload terminé...")

    # Récupérer l'URL du dossier uploadé
    print(f"🔗 Récupération de l'URL du dossier...")

    url_dossier_drive = None

    try:
        # ⚠️ Attendre que Google Drive rafraîchisse la liste
        # après l'upload (peut prendre quelques secondes)
        print(f"⏳ Attente du rafraîchissement de Google Drive...")
        page_drive.wait_for_timeout(5000)

        # Chercher et double-cliquer sur le dossier uploadé
        trouve = False
        tentatives = [
            f"[aria-label='{nom_dossier_local}']",
            f"[data-tooltip='{nom_dossier_local}']",
        ]

        for sel in tentatives:
            try:
                el = page_drive.locator(sel).first
                if el.count() > 0:
                    el.dblclick(timeout=5000)
                    page_drive.wait_for_timeout(2000)
                    trouve = True
                    break
            except:
                continue

        # Essai par texte si pas trouvé
        if not trouve:
            try:
                el = page_drive.get_by_text(nom_dossier_local, exact=True).first
                if el.count() > 0:
                    el.dblclick(timeout=5000)
                    page_drive.wait_for_timeout(2000)
                    trouve = True
            except:
                pass

        if trouve:
            # Lire directement l'URL depuis page_drive.url
            # C'est la méthode la plus simple et fiable
            url_dossier_drive = page_drive.url
            print(f"✅ URL récupérée : {url_dossier_drive[:60]}...")

            # Revenir en arrière (dossier du mois)
            page_drive.go_back()
            page_drive.wait_for_timeout(2000)

        else:
            # ⚠️ Dossier pas encore visible → attendre encore et réessayer
            print(f"⚠️ Dossier pas encore visible, nouvelle tentative...")
            page_drive.wait_for_timeout(5000)

            try:
                el = page_drive.get_by_text(nom_dossier_local, exact=True).first
                if el.count() > 0:
                    el.dblclick(timeout=5000)
                    page_drive.wait_for_timeout(2000)
                    url_dossier_drive = page_drive.url
                    print(f"✅ URL récupérée (2ème tentative) : {url_dossier_drive[:60]}...")
                    page_drive.go_back()
                    page_drive.wait_for_timeout(2000)
                else:
                    print(f"⚠️ Dossier toujours non trouvé")
                    print(f"   Utilisation de l'URL du dossier parent...")
                    url_dossier_drive = page_drive.url
            except:
                url_dossier_drive = page_drive.url

    except Exception as e:
        print(f"⚠️ Erreur récupération URL : {e}")
        try:
            url_dossier_drive = page_drive.url
            print(f"✅ URL fallback : {url_dossier_drive[:60]}...")
        except:
            url_dossier_drive = None

    print(f"🔙 Retour vers le dossier {annee}...")
    page_drive.goto(
        config["google_drive_url"],
        timeout=60000,
        wait_until="domcontentloaded"
    )
    page_drive.wait_for_timeout(2000)
    print(f"✅ Retour dans le dossier {annee} effectué !")

    return url_dossier_drive

# ============================================================
# INSÉRER LIEN DANS GOOGLE SHEETS
# ============================================================

def inserer_lien_sheets(page_sheets, numero_ligne, type_dossier, url_drive, avis_aep="", avis_eu=""):
    """
    Insère l'URL du dossier Google Drive dans le Google Sheets
    sous forme de lien cliquable (Ctrl+K → coller URL → Appliquer).
    Colonne K si PC/PA, colonne L si CU/DP/autre.
    
    Après insertion du lien :
    - Si UN DES DEUX avis est "incomplet" → date du jour en colonne O
    - Sinon → date du jour en colonne Q
    """
    if not url_drive:
        print(f"⚠️ URL Drive non disponible → lien non inséré dans Sheets")
        return

    print(f"🔗 Insertion du lien Drive dans Google Sheets...")

    colonne = "K" if type_dossier in ["PC", "PA"] else "L"
    cellule = f"{colonne}{numero_ligne}"

    print(f"   Cellule : {cellule}")
    print(f"   URL : {url_drive[:60]}...")

    # S'assurer qu'on est sur la bonne feuille
    try:
        page_sheets.click("text=Suivi intructions_Hors MARS")
        page_sheets.wait_for_timeout(2000)
    except:
        pass

    # Naviguer vers la cellule
    naviguer_vers_cellule_sheets(page_sheets, cellule)
    page_sheets.wait_for_timeout(500)

    # Ouvrir la popup "Insérer un lien" avec Ctrl+K
    page_sheets.keyboard.press("Control+k")
    page_sheets.wait_for_timeout(1500)

    try:
        champ_url = page_sheets.locator(
            "input[placeholder*='lien'], "
            "input[placeholder*='URL'], "
            "input[placeholder*='url'], "
            "input[placeholder*='Lien'], "
            "input[aria-label*='lien'], "
            "input[aria-label*='URL']"
        ).first

        champ_url.click()
        page_sheets.keyboard.press("Control+a")
        page_sheets.keyboard.type(url_drive)
        page_sheets.wait_for_timeout(500)

        try:
            page_sheets.click(
                "text=Appliquer, "
                "text=Apply, "
                "button:has-text('Appliquer'), "
                "button:has-text('Apply')",
                timeout=3000
            )
        except:
            page_sheets.keyboard.press("Enter")

        page_sheets.wait_for_timeout(1000)
        print(f"✅ Lien inséré dans la cellule {cellule} !")

    except Exception as e:
        print(f"⚠️ Erreur insertion lien via Ctrl+K : {e}")
        print(f"   Fallback : écriture de l'URL directement...")
        naviguer_vers_cellule_sheets(page_sheets, cellule)
        page_sheets.keyboard.type(url_drive)
        page_sheets.keyboard.press("Enter")
        page_sheets.wait_for_timeout(500)
        print(f"✅ URL écrite dans la cellule {cellule} (sans lien cliquable)")

    # --------------------------------------------------------
    # DATE DU JOUR : Colonne O ou Q selon les avis
    # --------------------------------------------------------
    date_aujourd_hui = datetime.now().strftime("%d/%m/%Y")

    # Si UN DES DEUX avis est "incomplet" → colonne O
    # Sinon → colonne Q
    if avis_aep == "incomplet" or avis_eu == "incomplet":
        colonne_date = "O"
        print(f"📅 Avis incomplet détecté → Date du jour en colonne O{numero_ligne}")
    else:
        colonne_date = "Q"
        print(f"📅 Date du jour en colonne Q{numero_ligne}")

    ecrire_dans_cellule_sheets(
        page_sheets,
        f"{colonne_date}{numero_ligne}",
        date_aujourd_hui
    )
    print(f"✅ Date '{date_aujourd_hui}' écrite en colonne {colonne_date}{numero_ligne} !")


def finaliser_dossier_background(page_drive, page_sheets, data, config, stats):
    """S'occupe de l'upload Drive et du lien Sheets du dossier précédent."""
    if not data: return
    print(f"\n⚙️ Arrière-plan : Finalisation de {data['numero_formate']}...")
    
    url_drive = uploader_dossier_google_drive(page_drive, data['chemin'], data['donnees'], config)
    
    if url_drive:
        inserer_lien_sheets(page_sheets, data['ligne'], data['type'], url_drive, data['avis_aep'], data['avis_eu'])
        notifier_windows("Dossier Terminé", f"Le dossier {data['numero_formate']} a été archivé.")
        stats.traites += 1
        if data['avis_aep'] == "incomplet" or data['avis_eu'] == "incomplet":
            stats.incomplets += 1
    
    if os.path.exists(QUEUE_FILE): os.remove(QUEUE_FILE)

# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    config = charger_config()
    stats = StatsSession()
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=config["chrome_profile"], channel="chrome", headless=False, accept_downloads=True
        )
        page_avisau = context.new_page()
        page_sheets = context.new_page()
        page_drive = context.new_page()

        try:
            verifier_et_connecter(page_avisau, config)
            
            # --- ÉTAPE A : REPRISE SI CRASH ---
            reprise = charger_queue()
            if reprise:
                print("📋 Dossier interrompu détecté, finalisation en cours...")
                finaliser_dossier_background(page_drive, page_sheets, reprise, config, stats)

            chemin_csv = selectionner_service_et_telecharger_csv(page_avisau, config)
            dossiers_csv = lire_csv(chemin_csv)
            interface = InterfaceAvisAU(dossiers_csv)

            historique_bg = None # C'est notre tampon (buffer)

            while True:
                interface.resultat = {"dossiers": [], "action": "QUIT"}
                interface.afficher_etape_selection()
                interface.relancer()
                
                if interface.resultat["action"] == "QUIT": 
                    if historique_bg: # On vide le dernier avant de partir
                        finaliser_dossier_background(page_drive, page_sheets, historique_bg, config, stats)
                    break
                
                dossiers_selectionnes = interface.resultat["dossiers"]
                for idx, d in enumerate(dossiers_selectionnes, 1):
                    # --- ÉTAPE 1 : PRIORITÉ OUVERTURE (Dossier N) ---
                    trouve, url_d = trouver_et_ouvrir_dossier(page_avisau, d, config)
                    if not trouve: continue
                    
                    donnees = extraire_donnees_dossier(page_avisau)
                    donnees["commune"], donnees["date_depart"] = d["commune"], d["date_depart"]
                    
                    zip_p = telecharger_pieces(page_avisau, config)
                    dossier_p = extraire_zip_et_creer_dossier(zip_p, donnees, config)
                    form_p = preremplir_formulaire_pdf(donnees, config)
                    
                    # On ouvre TOUT immédiatement
                    if form_p: os.startfile(str(form_p))
                    for f_item in dossier_p.iterdir(): 
                        if f_item.is_file(): os.startfile(str(f_item))

                    # --- ÉTAPE 2 : PRÉ-REMPLISSAGE SHEETS (Dossier N) ---
                    ligne = mettre_a_jour_sheets(page_sheets, donnees, config)

                    # --- ÉTAPE 3 : TRAVAIL ARRIÈRE-PLAN (Dossier N-1) ---
                    # Le script profite que tu lises les PDF pour uploader le précédent
                    if historique_bg:
                        finaliser_dossier_background(page_drive, page_sheets, historique_bg, config, stats)
                        historique_bg = None

                    # --- ÉTAPE 4 : SAISIE DES AVIS (Dossier N) ---
                    av_aep, av_eu = traiter_avis_complet(page_avisau, donnees, dossier_p, config, interface)
                    
                    if av_aep == "EN_ATTENTE":
                        stats.en_attente += 1
                        continue
                    if av_aep == "PASSE": continue
                    
                    # --- ÉTAPE 5 : MISE EN TAMPON ---
                    historique_bg = {
                        "numero_formate": donnees["numero_formate"], "chemin": str(dossier_p),
                        "donnees": donnees, "ligne": ligne, "type": donnees["numero_brut"][:2].upper(),
                        "avis_aep": av_aep, "avis_eu": av_eu
                    }
                    sauvegarder_queue(historique_bg) # Protection anti-crash
                    
                    # Si c'est le dernier dossier du lot, on ne peut pas attendre le suivant
                    if idx == len(dossiers_selectionnes):
                        finaliser_dossier_background(page_drive, page_sheets, historique_bg, config, stats)
                        historique_bg = None

            stats.generer_rapport()
        finally:
            context.close()

if __name__ == "__main__":
    main()
