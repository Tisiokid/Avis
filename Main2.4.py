import json
import csv
import os
import re
import zipfile
import unicodedata
import subprocess
import shutil
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from fillpdf import fillpdfs

# Fichiers de suivi
QUEUE_FILE = "background_queue.json"
RECAP_FILE = "rapport_session.txt"
FICHIER_EXPORT_SHEETS = "lignes_a_copier.txt"

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

MAPPING_NATURE_INDEX = {
    "favorable"               : 1, 
    "favorable avec réserves" : 2, 
    "défavorable"             : 3, 
    "incomplet"               : 5  
}

# ============================================================
# CALCULS ET OUTILS
# ============================================================

def calculer_date_limite(date_depart_str, societe):
    try:
        date_depart = datetime.strptime(date_depart_str, "%Y-%m-%d")
        jours       = 30 if societe == "SEMM" else 21
        date_limite = date_depart + timedelta(days=jours)
        return date_limite.strftime("%d/%m")
    except:
        return "??/??"

def notifier_windows(titre, message):
    try:
        toast = tk.Toplevel()
        toast.title(titre)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg="#333333")
        largeur_ecran = toast.winfo_screenwidth()
        hauteur_ecran = toast.winfo_screenheight()
        toast.geometry(f"300x80+{largeur_ecran-320}+{hauteur_ecran-130}")
        tk.Label(toast, text=titre, fg="#2196F3", bg="#333333", font=("Arial", 10, "bold")).pack(pady=(10, 0), padx=10, anchor="w")
        tk.Label(toast, text=message, fg="white", bg="#333333", font=("Arial", 9), wraplength=280, justify="left").pack(pady=5, padx=10, anchor="w")
        toast.after(5000, toast.destroy)
    except:
        pass

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
    def __init__(self, dossiers_csv):
        self.dossiers_csv = dossiers_csv
        self.resultat     = {"dossiers": [], "action": "QUIT"}

        self.COULEURS = {
            "SEMM"    : "#2196F3",
            "SAOM"    : "#4CAF50",
            "SAEM"    : "#FF9800",
            None      : "#9E9E9E",
            "bg"      : "#F5F5F5",
            "titre"   : "#1565C0"
        }

        # 1. D'abord créer la fenêtre principale Tkinter
        self.fenetre = tk.Tk()
        self.fenetre.title("AUTOMATISATION AVIS'AU")
        self.fenetre.geometry("1100x780")
        self.fenetre.resizable(True, True)
        self.fenetre.configure(bg=self.COULEURS["bg"])

        # 2. ENSUITE créer les variables Tkinter (le bug venait d'ici)
        self.avis_aep_var = None
        self.avis_eu_var  = None
        self.contrat_var = None
        self.surf_anc_var = None
        self.surf_nouv_var = None
        
        # Variables pour le calcul de pression
        self.press_mini_var = tk.StringVar(value="")
        self.press_tn_var = tk.StringVar(value="")
        self.press_res_var = tk.StringVar(value="")

        self.resultat_avis = {
            "dossier_pret" : None,
            "avis_aep"     : None,
            "avis_eu"      : None,
            "pfac_contrat" : None,
            "pfac_surf_anc": None,
            "pfac_surf_nouv": None,
            "action"       : None
        }

        self.fenetre.update_idletasks()
        x = (self.fenetre.winfo_screenwidth()  // 2) - (1100 // 2)
        y = (self.fenetre.winfo_screenheight() // 2) - (780  // 2)
        self.fenetre.geometry(f"1100x780+{x}+{y}")

        self.cadre_titre = tk.Frame(self.fenetre, bg=self.COULEURS["titre"], pady=10)
        self.cadre_titre.pack(fill="x")

        self.label_titre = tk.Label(self.cadre_titre, text="🚰  AUTOMATISATION AVIS'AU", font=("Arial", 14, "bold"), bg=self.COULEURS["titre"], fg="white")
        self.label_titre.pack()

        self.label_sous_titre = tk.Label(self.cadre_titre, text=f"📋  {len(dossiers_csv)} dossier(s) en attente", font=("Arial", 10), bg=self.COULEURS["titre"], fg="#BBDEFB")
        self.label_sous_titre.pack()

        self.cadre_contenu = tk.Frame(self.fenetre, bg=self.COULEURS["bg"])
        self.cadre_contenu.pack(fill="both", expand=True, padx=0, pady=0)

        self.afficher_etape_selection()

    def vider_contenu(self):
        for widget in self.cadre_contenu.winfo_children():
            widget.destroy()

    def mettre_a_jour_sous_titre(self, texte, couleur="#BBDEFB"):
        self.label_sous_titre.config(text=texte, fg=couleur)

    # ------------------ ÉTAPE 1 : SÉLECTION ------------------
    def afficher_etape_selection(self):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"📋  {len(self.dossiers_csv)} dossier(s) en attente")

        cadre_legende = tk.Frame(self.cadre_contenu, bg="#ECEFF1", pady=6)
        cadre_legende.pack(fill="x", padx=20, pady=(8, 0))
        for societe, couleur in [("SEMM (30j)", "#2196F3"), ("SAOM (21j)", "#4CAF50"), ("SAEM (21j)", "#FF9800")]:
            tk.Label(cadre_legende, text=f"  {societe}  ", bg=couleur, fg="white", font=("Arial", 9, "bold"), padx=8, pady=3).pack(side="left", padx=4)

        cadre_entete = tk.Frame(self.cadre_contenu, bg="#37474F")
        cadre_entete.pack(fill="x", padx=20, pady=(6, 0))
        for texte, largeur in [("", 3), ("Numéro", 22), ("Commune", 20), ("Société", 8), ("Date départ", 10), ("Date limite", 10), ("Délai restant", 14)]:
            tk.Label(cadre_entete, text=texte, width=largeur, bg="#37474F", fg="white", font=("Arial", 9, "bold"), anchor="w", pady=5).pack(side="left", padx=3)

        cadre_scroll = tk.Frame(self.cadre_contenu)
        cadre_scroll.pack(fill="both", expand=True, padx=20)
        scrollbar = tk.Scrollbar(cadre_scroll)
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(cadre_scroll, yscrollcommand=scrollbar.set, bg=self.COULEURS["bg"])
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=canvas.yview)
        cadre_liste = tk.Frame(canvas, bg=self.COULEURS["bg"])
        canvas.create_window((0, 0), window=cadre_liste, anchor="nw")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        self.variables = []
        for i, dossier in enumerate(self.dossiers_csv):
            var = tk.BooleanVar(value=False)
            self.variables.append(var)
            societe = get_societe(dossier["commune"])
            couleur_soc = self.COULEURS.get(societe, "#9E9E9E")
            couleur_fond = "#f9f9f9" if i % 2 == 0 else "white"
            date_limite = calculer_date_limite(dossier["date_depart"], societe)

            try:
                date_dep = datetime.strptime(dossier["date_depart"], "%Y-%m-%d")
                jours_total = 30 if societe == "SEMM" else 21
                restant = (date_dep + timedelta(days=jours_total) - datetime.now()).days
                if restant < 0: texte_restant, couleur_restant = f"⛔ {abs(restant)}j dépassé", "#f44336"
                elif restant <= 5: texte_restant, couleur_restant = f"⚠️ {restant}j restants", "#FF9800"
                else: texte_restant, couleur_restant = f"✅ {restant}j restants", "#4CAF50"
            except: texte_restant, couleur_restant = "?", "#9E9E9E"

            date_dep_fmt = datetime.strptime(dossier["date_depart"], "%Y-%m-%d").strftime("%d/%m") if "-" in dossier["date_depart"] else dossier["date_depart"]

            tk.Checkbutton(cadre_liste, variable=var, bg=couleur_fond).grid(row=i, column=0, padx=5, pady=1)
            tk.Label(cadre_liste, text=dossier["numero_formate"], width=22, bg=couleur_fond, anchor="w", font=("Courier", 9)).grid(row=i, column=1, padx=3, pady=1)
            tk.Label(cadre_liste, text=dossier["commune"], width=20, bg=couleur_fond, anchor="w").grid(row=i, column=2, padx=3, pady=1)
            tk.Label(cadre_liste, text=f" {societe or '?'} ", width=8, bg=couleur_soc, fg="white", font=("Arial", 8, "bold")).grid(row=i, column=3, padx=3, pady=1)
            tk.Label(cadre_liste, text=date_dep_fmt, width=10, bg=couleur_fond, anchor="w").grid(row=i, column=4, padx=3, pady=1)
            tk.Label(cadre_liste, text=date_limite, width=10, bg=couleur_fond, anchor="w", font=("Arial", 9, "bold")).grid(row=i, column=5, padx=3, pady=1)
            tk.Label(cadre_liste, text=texte_restant, width=14, bg=couleur_fond, fg=couleur_restant, anchor="w", font=("Arial", 9, "bold")).grid(row=i, column=6, padx=3, pady=1)

        cadre_liste.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

        self.label_compteur = tk.Label(self.cadre_contenu, text="0 dossier(s) sélectionné(s)", font=("Arial", 10), fg="#666666", bg=self.COULEURS["bg"])
        self.label_compteur.pack(pady=4)

        def mettre_a_jour_compteur(*args):
            n = sum(1 for v in self.variables if v.get())
            self.label_compteur.config(text=f"{n} dossier(s) sélectionné(s)", fg="#2196F3" if n > 0 else "#666666")
        for var in self.variables: var.trace_add("write", mettre_a_jour_compteur)

        cadre_boutons = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_boutons.pack(pady=8)
        tk.Button(cadre_boutons, text="✅  Tout sélectionner", command=lambda: [v.set(True) for v in self.variables], bg="#4CAF50", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=0, padx=6)
        tk.Button(cadre_boutons, text="☐  Tout désélectionner", command=lambda: [v.set(False) for v in self.variables], bg="#f44336", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=1, padx=6)
        tk.Button(cadre_boutons, text="▶  Traiter la sélection", command=self._confirmer_selection, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=18, pady=5).grid(row=0, column=2, padx=6)
        tk.Button(cadre_boutons, text="🔄  Rafraîchir", command=self._rafraichir, bg="#607D8B", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=3, padx=6)
        tk.Button(cadre_boutons, text="✖  Quitter", command=self._quitter, bg="#9E9E9E", fg="white", font=("Arial", 10), width=18, pady=5).grid(row=0, column=4, padx=6)
        self.fenetre.bind("<Escape>", lambda e: self._quitter())
        self.fenetre.bind("<Return>", lambda e: self._confirmer_selection())

    def _confirmer_selection(self):
        dossiers = [self.dossiers_csv[i] for i, v in enumerate(self.variables) if v.get()]
        if not dossiers:
            self.mettre_a_jour_sous_titre("⚠️  Sélectionnez au moins un dossier !", couleur="#FF9800")
            return
        self.resultat["dossiers"] = dossiers
        self.resultat["action"] = "TRAITER"
        self.fenetre.quit()

    def _rafraichir(self):
        self.resultat["action"] = "REFRESH"
        self.fenetre.quit()

    def _quitter(self):
        self.resultat["action"] = "QUIT"
        self.fenetre.quit()
        
    # ------------------ ÉTAPE 2 : PIPELINE / ATTENTE ------------------
    def afficher_etape_chargement(self, numero_courant, numero_suivant):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"⏳ Anticipation du téléchargement...", couleur="#FF9800")

        cadre_carte = tk.Frame(self.cadre_contenu, bg="white", pady=25, padx=20, relief="flat", bd=0)
        cadre_carte.pack(fill="x", padx=30, pady=20)

        tk.Label(cadre_carte, text=f"📖 Lisez les documents du dossier :", font=("Arial", 11), bg="white", fg="#666666").pack()
        tk.Label(cadre_carte, text=f"{numero_courant}", font=("Courier", 16, "bold"), bg="white", fg="#1565C0").pack(pady=5)
        
        tk.Label(self.cadre_contenu, text="Le formulaire d'avis s'affichera ici dans quelques secondes.", font=("Arial", 11, "italic"), bg=self.COULEURS["bg"], fg="#333333").pack(pady=10)
        
        if numero_suivant:
            tk.Label(self.cadre_contenu, text=f"(Le robot pré-télécharge le dossier suivant : {numero_suivant}...)", font=("Arial", 9), bg=self.COULEURS["bg"], fg="#9E9E9E").pack(pady=5)
            
        self.fenetre.update()

    # ------------------ FONCTION DE CALCUL ------------------
    def _calculer_pression(self):
        """Fonction déclenchée par le bouton de calcul de pression"""
        try:
            val1 = float(self.press_mini_var.get().replace(',', '.'))
            val2 = float(self.press_tn_var.get().replace(',', '.'))
            res = (val1 - val2) * 0.065
            self.press_res_var.set(f"{res:.2f}")
        except ValueError:
            self.press_res_var.set("Erreur")

    # ------------------ ÉTAPE 3 : AVIS (NOUVEAU DESIGN AFFINÉ) ------------------
    def afficher_etape_avis(self, donnees, societe):
        self.vider_contenu()
        couleur_soc = self.COULEURS.get(societe, "#9E9E9E")
        jours = 30 if societe == "SEMM" else 21
        self.mettre_a_jour_sous_titre(f"🏢  Instruction — {donnees['numero_formate']}", couleur="#BBDEFB")

        # ENCART D'INFORMATIONS AFFINÉ (Nouveau Design)
        cadre_carte = tk.Frame(self.cadre_contenu, bg="white", pady=10, padx=20, relief="flat", bd=0)
        cadre_carte.pack(fill="x", padx=30, pady=5) 

        # --- Ligne 1 : Badge | N° Dossier | Commune ---
        f_l1 = tk.Frame(cadre_carte, bg="white")
        f_l1.pack(fill="x", pady=2)
        tk.Label(f_l1, text=f"  {societe}  ", bg=couleur_soc, fg="white", font=("Arial", 11, "bold"), padx=10, pady=3).pack(side="left", padx=(0, 15))
        
        champ_num = tk.Entry(f_l1, font=("Courier", 14, "bold"), fg="#1565C0", bg="white", bd=0, highlightthickness=0, width=23)
        champ_num.insert(0, donnees["numero_formate"])
        champ_num.configure(state="readonly", readonlybackground="white")
        champ_num.pack(side="left")

        champ_com = tk.Entry(f_l1, font=("Arial", 12, "bold"), fg="#333333", bg="white", bd=0, highlightthickness=0, width=30)
        champ_com.insert(0, donnees.get("commune", "").upper())
        champ_com.configure(state="readonly", readonlybackground="white")
        champ_com.pack(side="left")

        # Fonction locale pour générer les lignes compactes (Gauche / Droite)
        def creer_ligne_info(parent, label_gauche, val_gauche, label_droite, val_droite):
            f_ligne = tk.Frame(parent, bg="white")
            f_ligne.pack(fill="x", pady=2)
            
            tk.Label(f_ligne, text=label_gauche, font=("Arial", 9, "bold"), bg="white", fg="#666666", width=16, anchor="w").pack(side="left")
            val_g_str = re.sub(r'\s*\n\s*', ' - ', str(val_gauche).strip())
            champ_g = tk.Entry(f_ligne, font=("Arial", 9), fg="#333333", bg="white", width=45, bd=0, highlightthickness=0)
            champ_g.insert(0, val_g_str)
            champ_g.configure(state="readonly", readonlybackground="white")
            champ_g.pack(side="left")

            if label_droite:
                f_droite = tk.Frame(f_ligne, bg="white")
                f_droite.pack(side="right")
                tk.Label(f_droite, text=label_droite, font=("Arial", 9, "bold"), bg="white", fg="#666666").pack(side="left")
                champ_d = tk.Entry(f_droite, font=("Arial", 9, "bold"), fg="#333333", bg="white", width=12, bd=0, highlightthickness=0, justify="right")
                champ_d.insert(0, val_droite)
                champ_d.configure(state="readonly", readonlybackground="white")
                champ_d.pack(side="left")

        # --- Ligne 2 : Pétitionnaire (Gauche) | Date départ (Droite) ---
        creer_ligne_info(cadre_carte, "👤 Pétitionnaire :", donnees.get("nom_petitionnaire", ""), "📅 Date départ :", donnees.get("date_depart", ""))
        
        # --- Ligne 3 : Adresse travaux (Gauche) | Délai (Droite) ---
        delai_str = calculer_date_limite(donnees.get("date_depart", ""), societe)
        creer_ligne_info(cadre_carte, "🏠 Adresse :", donnees.get("adresse_travaux", ""), f"⏱️ Délai ({jours}j) :", delai_str)

        # --- Ligne 4 : Parcelle ---
        creer_ligne_info(cadre_carte, "🗺️ Parcelle :", donnees.get("parcelle", ""), None, None)

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        
        # AVIS AEP
        cadre_avis = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_avis.pack(fill="x", padx=30, pady=2)
        tk.Label(cadre_avis, text="💧  AVIS AEP", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#1565C0").pack(anchor="w", pady=(2, 2))
        self.avis_aep_var = tk.StringVar(value="")
        cadre_boutons_aep = tk.Frame(cadre_avis, bg=self.COULEURS["bg"])
        cadre_boutons_aep.pack(anchor="w")

        STYLES_AVIS = {
            "favorable": ("#4CAF50", "✅  Favorable"), "favorable avec réserves": ("#FF9800", "⚠️  Favorable avec réserves"),
            "défavorable": ("#f44336", "❌  Défavorable"), "incomplet": ("#9C27B0", "📋  Incomplet"), "refus": ("#607D8B", "🚫  Refus")
        }
        for i, (valeur, (couleur, texte)) in enumerate(STYLES_AVIS.items()):
            tk.Radiobutton(cadre_boutons_aep, text=texte, variable=self.avis_aep_var, value=valeur, bg=self.COULEURS["bg"], activebackground=couleur, selectcolor=couleur, fg="#333333", font=("Arial", 10), indicatoron=0, width=22, pady=4, relief="groove", bd=1).grid(row=0, column=i, padx=4)

        self.avis_eu_var = tk.StringVar(value="")
        self.contrat_var = tk.StringVar(value="")
        self.surf_anc_var = tk.StringVar(value="")
        self.surf_nouv_var = tk.StringVar(value="")

        # AVIS EU + PFAC (Si concerné)
        if societe in ["SAOM", "SAEM"]:
            ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
            cadre_avis_eu = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
            cadre_avis_eu.pack(fill="x", padx=30, pady=2)
            tk.Label(cadre_avis_eu, text=f"🌊  AVIS EU ({societe})", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg=couleur_soc).pack(anchor="w", pady=(2, 2))
            cadre_boutons_eu = tk.Frame(cadre_avis_eu, bg=self.COULEURS["bg"])
            cadre_boutons_eu.pack(anchor="w")
            for i, (valeur, (couleur, texte)) in enumerate(STYLES_AVIS.items()):
                tk.Radiobutton(cadre_boutons_eu, text=texte, variable=self.avis_eu_var, value=valeur, bg=self.COULEURS["bg"], activebackground=couleur, selectcolor=couleur, fg="#333333", font=("Arial", 10), indicatoron=0, width=22, pady=4, relief="groove", bd=1).grid(row=0, column=i, padx=4)

            ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
            cadre_pfac = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
            cadre_pfac.pack(fill="x", padx=30, pady=2)
            tk.Label(cadre_pfac, text="📝 ELIGIBILITÉ PFAC", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#8D6E63").pack(anchor="w", pady=(2, 4))
            
            f1 = tk.Frame(cadre_pfac, bg=self.COULEURS["bg"])
            f1.pack(fill="x", pady=2)
            tk.Label(f1, text="N° Contrat :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold"), width=12, anchor="w").pack(side="left")
            tk.Entry(f1, textvariable=self.contrat_var, font=("Arial", 10), width=30).pack(side="left", padx=5)

            f2 = tk.Frame(cadre_pfac, bg=self.COULEURS["bg"])
            f2.pack(fill="x", pady=2)
            tk.Label(f2, text="CERFA :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold"), width=12, anchor="w").pack(side="left")
            tk.Label(f2, text="Surface ancienne :", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")
            tk.Entry(f2, textvariable=self.surf_anc_var, font=("Arial", 10), width=8).pack(side="left", padx=5)
            tk.Label(f2, text="m²   -   Surface nouvelle :", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")
            tk.Entry(f2, textvariable=self.surf_nouv_var, font=("Arial", 10), width=8).pack(side="left", padx=5)
            tk.Label(f2, text="m²", bg=self.COULEURS["bg"], font=("Arial", 10)).pack(side="left")

        # ESTIMATION DE LA PRESSION (Affiché pour tout le monde, placé APRES le PFAC)
        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=5)
        cadre_press = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_press.pack(fill="x", padx=30, pady=2)
        tk.Label(cadre_press, text="⏱️ ESTIMATION DE LA PRESSION", font=("Arial", 12, "bold"), bg=self.COULEURS["bg"], fg="#00796B").pack(anchor="w", pady=(2, 4))
        
        f_press = tk.Frame(cadre_press, bg=self.COULEURS["bg"])
        f_press.pack(fill="x", pady=2)
        
        tk.Label(f_press, text="Pression Mini :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_mini_var, font=("Arial", 10), width=10).pack(side="left", padx=(5, 15))
        
        tk.Label(f_press, text="Terrain Naturel :", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        tk.Entry(f_press, textvariable=self.press_tn_var, font=("Arial", 10), width=10).pack(side="left", padx=5)
        
        tk.Button(f_press, text="🗜️ Calculer", command=self._calculer_pression, bg="#607D8B", fg="white", font=("Arial", 9, "bold"), padx=10, pady=2).pack(side="left", padx=15)
        
        tk.Label(f_press, text="=", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")
        
        champ_press_res = tk.Entry(f_press, textvariable=self.press_res_var, font=("Arial", 11, "bold"), fg="#1565C0", bg="white", width=8, bd=0, highlightthickness=0)
        champ_press_res.configure(state="readonly", readonlybackground="white")
        champ_press_res.pack(side="left", padx=5)
        tk.Label(f_press, text="Bars", bg=self.COULEURS["bg"], font=("Arial", 10, "bold")).pack(side="left")

        ttk.Separator(self.cadre_contenu, orient="horizontal").pack(fill="x", padx=30, pady=10)
        cadre_actions = tk.Frame(self.cadre_contenu, bg=self.COULEURS["bg"])
        cadre_actions.pack(pady=5)
        tk.Button(cadre_actions, text="📤  Envoyer le dossier", command=self._valider_avis, bg="#2196F3", fg="white", font=("Arial", 11, "bold"), width=22, pady=6).grid(row=0, column=0, padx=10)
        tk.Button(cadre_actions, text="📦  Mettre en attente", command=self._mettre_en_attente, bg="#FF9800", fg="white", font=("Arial", 10), width=22, pady=6).grid(row=0, column=1, padx=10)

        self.label_erreur_avis = tk.Label(self.cadre_contenu, text="", font=("Arial", 10), fg="#f44336", bg=self.COULEURS["bg"])
        self.label_erreur_avis.pack()

    def _valider_avis(self):
        avis_aep = self.avis_aep_var.get() if self.avis_aep_var else ""
        if not avis_aep:
            self.label_erreur_avis.config(text="⚠️  Veuillez sélectionner un avis AEP !")
            return
        
        self.resultat_avis["avis_aep"] = avis_aep
        self.resultat_avis["avis_eu"]  = self.avis_eu_var.get() if self.avis_eu_var else ""
        self.resultat_avis["pfac_contrat"] = self.contrat_var.get() if self.contrat_var else ""
        self.resultat_avis["pfac_surf_anc"] = self.surf_anc_var.get() if self.surf_anc_var else ""
        self.resultat_avis["pfac_surf_nouv"] = self.surf_nouv_var.get() if self.surf_nouv_var else ""
        self.resultat_avis["action"]   = "ENVOYER"
        self.fenetre.quit()

    def _mettre_en_attente(self):
        self.resultat_avis["action"] = "ATTENTE"
        self.fenetre.quit()

    # ------------------ ÉTAPE 4 : LOGS ------------------
    def afficher_etape_traitement(self, numero_formate):
        self.vider_contenu()
        self.mettre_a_jour_sous_titre(f"⚙️  Envoi de l'avis — {numero_formate}", couleur="#BBDEFB")

        tk.Label(self.cadre_contenu, text=f"⚙️  Envoi de l'avis sur Avis'AU", font=("Arial", 13, "bold"), bg=self.COULEURS["bg"], fg="#1565C0").pack(pady=20)

        self.zone_logs = tk.Text(self.cadre_contenu, height=20, width=90, font=("Courier", 9), bg="#1E1E1E", fg="#FFFFFF", relief="flat")
        self.zone_logs.pack(padx=30, pady=5)
        self.zone_logs.config(state="disabled")
        self.fenetre.update()

    def ajouter_log(self, message):
        print(message)
        if hasattr(self, "zone_logs"):
            self.zone_logs.config(state="normal")
            self.zone_logs.insert("end", message + "\n")
            self.zone_logs.see("end")
            self.zone_logs.config(state="disabled")
            self.fenetre.update()

    def relancer(self):
        self.fenetre.mainloop()

# ============================================================
# FORMATAGES / NORMALISATIONS
# ============================================================

def normaliser_commune_drive(commune):
    commune = commune.upper().strip()
    commune = ''.join(c for c in unicodedata.normalize('NFD', commune) if unicodedata.category(c) != 'Mn')
    commune = commune.replace('-', ' ')
    return ' '.join(commune.split())

def formater_commune_sheets(commune):
    return normaliser_commune_drive(commune)

def formater_numero_dossier(numero_brut):
    numero = numero_brut.strip().upper()
    match = re.match(r'^([A-Z]{2})(\d{3})(\d{3})(\d{2})(.+)$', numero)
    return f"{match.group(1)} {match.group(2)} {match.group(3)} {match.group(4)} {match.group(5)}" if match else numero_brut

def normaliser_commune(commune):
    commune = commune.lower().strip()
    commune = ''.join(c for c in unicodedata.normalize('NFD', commune) if unicodedata.category(c) != 'Mn')
    commune = commune.replace('-', ' ')
    return ' '.join(commune.split())

def get_societe(commune):
    commune_norm = normaliser_commune(commune)
    for c in COMMUNES_SEMM:
        if c in commune_norm or commune_norm in c: return "SEMM"
    for c in COMMUNES_SAOM:
        if c in commune_norm or commune_norm in c: return "SAOM"
    for c in COMMUNES_SAEM:
        if c in commune_norm or commune_norm in c: return "SAEM"
    return None

def charger_config():
    with open("config.json", "r", encoding="utf-8") as f: return json.load(f)

# ============================================================
# AUTOMATISATION WEB (CONNEXION, CSV, EXTRACTION, ETC.)
# ============================================================

def verifier_et_connecter(page_avisau, config):
    print("🔍 Vérification de la connexion...")
    page_avisau.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
    page_avisau.wait_for_load_state("networkidle")
    if "login" in page_avisau.url:
        print("❌ Non connecté - Connexion en cours...")
        page_avisau.click("button.fr-btn.fr-connect.cerbere-connect")
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.fill("#login", config["identifiant"])
        page_avisau.fill("#password", config["mot_de_passe"])
        page_avisau.click("#btnConnexion")
        page_avisau.wait_for_url("**/consultation**", timeout=15000)
        page_avisau.wait_for_load_state("networkidle")
        print("✅ Connecté avec succès !")
    else: print("✅ Déjà connecté !")

def selectionner_service_et_telecharger_csv(page_avisau, config):
    if "consultation" not in page_avisau.url:
        page_avisau.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page_avisau.wait_for_load_state("networkidle")
    page_avisau.select_option("#select", label=config["service"])
    page_avisau.wait_for_load_state("networkidle")
    page_avisau.wait_for_timeout(2000)
    chemin_csv = Path(config["dossier_telechargement"]) / "liste_consultations.csv"
    with page_avisau.expect_download() as download_info:
        page_avisau.click("#onglet-attente-metier-panel > div.fr-grid-row > div.fr-col-auto > button")
    download_info.value.save_as(chemin_csv)
    return chemin_csv

def lire_csv(chemin_csv):
    dossiers = []
    with open(chemin_csv, "r", encoding="utf-8-sig") as f:
        for ligne in csv.DictReader(f, delimiter=";"):
            numero_brut = ligne["Numéro de dossier"].strip()
            dossiers.append({
                "date_depart"    : ligne["Date de départ du délai"].strip(),
                "identifiant"    : ligne["Identifiant de la consultation"].strip(),
                "numero_brut"    : numero_brut,
                "numero_formate" : formater_numero_dossier(numero_brut),
                "commune"        : ligne["Nom de la commune"].strip(),
            })
    return dossiers

def trouver_et_ouvrir_dossier(page_avisau, dossier, config, service_cible=None):
    numero_brut = dossier["numero_brut"]
    service_actuel = service_cible if service_cible else config["service"]
    
    if "consultation" not in page_avisau.url or "/" in page_avisau.url.split("consultation")[1]:
        page_avisau.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page_avisau.wait_for_load_state("networkidle")
    
    try:
        page_avisau.select_option("#select", label=service_actuel)
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.wait_for_timeout(2000)
    except:
        pass

    try:
        while True:
            page_avisau.wait_for_selector("table > tbody", timeout=10000)
            lignes = page_avisau.locator("table > tbody > tr")
            for i in range(lignes.count()):
                ligne = lignes.nth(i)
                if numero_brut in ligne.inner_text():
                    ligne.click()
                    page_avisau.wait_for_load_state("networkidle")
                    page_avisau.wait_for_timeout(2000)
                    return True, page_avisau.url
            
            bouton_suivant = page_avisau.locator("button[aria-label='Page suivante']")
            if bouton_suivant.count() > 0 and bouton_suivant.is_enabled():
                bouton_suivant.click()
                page_avisau.wait_for_load_state("networkidle")
                page_avisau.wait_for_timeout(2000)
            else:
                return False, None
    except Exception as e:
        print(f"⚠️ Erreur ou timeout lors de la recherche du dossier : {e}")
        return False, None

def extraire_donnees_dossier(page_avisau):
    try:
        try: num = page_avisau.locator("#conteneur-info-dossier div:nth-child(4) div div:nth-child(2) div").inner_text().strip()
        except: num = "ERR"
        
        try: adr = page_avisau.locator("#conteneur-info-dossier div:nth-child(3) div div:nth-child(1) div:nth-child(2)").inner_text().strip()
        except: adr = ""
        
        try: 
            pet_loc = page_avisau.locator("#conteneur-info-dossier div:nth-child(8) div:nth-child(2) div:nth-child(1) div")
            if pet_loc.count() > 0:
                pet = " et ".join([pet_loc.nth(i).inner_text().strip() for i in range(pet_loc.count()) if pet_loc.nth(i).inner_text().strip()])
            else: pet = ""
        except: pet = ""
        
        try: 
            par_loc = page_avisau.locator("#conteneur-info-dossier div:nth-child(3) div div:nth-child(2) dl dt")
            if par_loc.count() > 0:
                par = ", ".join([par_loc.nth(i).inner_text().strip() for i in range(par_loc.count())])
            else: par = "N/A"
        except: par = "N/A"
        
        try:
            for i in range(page_avisau.get_by_text("Voir plus").count()): page_avisau.get_by_text("Voir plus").nth(i).click()
        except: pass
        
        nature = ""
        try: nature = page_avisau.locator("#conteneur-detail-consultation #texte").first.inner_text().strip()
        except: pass
        if not nature:
            try: nature = page_avisau.locator("[id='texte']").first.inner_text().strip()
            except: nature = ""

        adresse_pet = ""
        try:
            rue_pet = page_avisau.locator("#conteneur-info-dossier div:nth-child(7) div div:nth-child(1) div:nth-child(2)").inner_text().strip()
            ville_pet = page_avisau.locator("#conteneur-info-dossier div:nth-child(7) div div:nth-child(1) div:nth-child(3)").inner_text().strip()
            adresse_pet = f"{rue_pet}, {ville_pet}"
        except: pass
            
        return {
            "numero_brut": num, "numero_formate": formater_numero_dossier(num), 
            "adresse_travaux": adr, "nom_petitionnaire": pet, "parcelle": par,
            "nature_travaux": nature, "adresse_petitionnaire": adresse_pet
        }
    except Exception as e:
        return {"numero_brut": "ERR", "numero_formate": "ERR", "adresse_travaux": "", "nom_petitionnaire": "", "parcelle": "", "nature_travaux": "", "adresse_petitionnaire": ""}

def telecharger_pieces(page_avisau, config):
    page_avisau.click("#conteneur-pj-dossier > div > fieldset > div > div:nth-child(2) > label")
    page_avisau.wait_for_timeout(2000)
    chemin_zip = Path(config["dossier_telechargement"]) / "pieces_temp.zip"
    with page_avisau.expect_download(timeout=120000) as download_info:
        page_avisau.click("#conteneur-pj-dossier > div > div > button")
    download_info.value.save_as(chemin_zip)
    return chemin_zip

def extraire_zip_et_creer_dossier(chemin_zip, donnees, config):
    nom_dossier = re.sub(r'[<>:"/\\|?*]', "_", f"{donnees['numero_formate']}-{donnees['adresse_travaux']}").strip()
    chemin_dossier = Path(config["dossier_destination"]) / nom_dossier
    chemin_dossier.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(chemin_zip, "r") as zip_ref: zip_ref.extractall(chemin_dossier)
    os.remove(chemin_zip)
    return chemin_dossier

def preremplir_formulaire_pdf(donnees, config):
    chemin_modele = str(Path(config["formulaire_pdf"]))
    nom_fichier   = f"Formulaire_{donnees['numero_formate']}.pdf"
    chemin_rempli = str(Path(config["formulaire_dossier_sortie"]) / nom_fichier)
    safe_str = lambda v: str(v).strip() if v else ""
    try:
        fillpdfs.write_fillable_pdf(chemin_modele, chemin_rempli, {
            "numéro dossier"        : safe_str(donnees.get("numero_formate")),
            "nom pétitionnaire"     : safe_str(donnees.get("nom_petitionnaire")),
            "adresse pétitionnaire" : safe_str(donnees.get("adresse_petitionnaire")),
            "nature travaux"        : safe_str(donnees.get("nature_travaux")),
            "adresse travaux"       : safe_str(donnees.get("adresse_travaux"))
        })
        return chemin_rempli
    except: return None

def preparer_dossier_pipeline(page_avisau, d_brut, config):
    trouve, _ = trouver_et_ouvrir_dossier(page_avisau, d_brut, config)
    if not trouve:
        return None
        
    donnees = extraire_donnees_dossier(page_avisau)
    donnees["commune"], donnees["date_depart"] = d_brut["commune"], d_brut["date_depart"]
    
    zip_p = telecharger_pieces(page_avisau, config)
    dossier_p = extraire_zip_et_creer_dossier(zip_p, donnees, config)
    form_p = preremplir_formulaire_pdf(donnees, config)
    
    return {
        "dossier_brut": d_brut,
        "donnees": donnees,
        "chemin_dossier": dossier_p,
        "formulaire": form_p
    }

# ============================================================
# EXPORT VERS FICHIER TSV (POUR COPIER-COLLER DANS SHEETS)
# ============================================================

def sauvegarder_ligne_pour_sheets(donnees, avis_aep, avis_eu, donnees_pfac):
    date_aujourd_hui = datetime.now().strftime("%d/%m/%Y")
    commune_formatee = formater_commune_sheets(donnees["commune"])
    
    try:
        date_formatee = datetime.strptime(donnees["date_depart"], "%Y-%m-%d").strftime("%d/%m/%Y")
    except:
        date_formatee = donnees["date_depart"]

    date_inc = date_aujourd_hui if (avis_aep == "incomplet" or avis_eu == "incomplet") else ""
    date_def = date_aujourd_hui if not date_inc else ""

    colonnes = [""] * 20
    colonnes[2] = commune_formatee                        # C 
    colonnes[4] = donnees.get("numero_formate", "")       # E 
    colonnes[7] = date_formatee                           # H 
    colonnes[8] = date_inc                                # I 
    colonnes[9] = date_def                                # J 
    
    if donnees_pfac:
        colonnes[15] = donnees_pfac.get("surf_anc", "")   # P (INDEX MODIFIÉ)
        colonnes[16] = donnees_pfac.get("surf_nouv", "")  # Q (INDEX MODIFIÉ)
        colonnes[17] = donnees_pfac.get("contrat", "")    # R 
        
    colonnes[18] = donnees.get("adresse_travaux", "")     # S 
    colonnes[19] = donnees.get("nom_petitionnaire", "")   # T 

    ligne_tsv = "\t".join(colonnes) + "\n"
    with open(FICHIER_EXPORT_SHEETS, "a", encoding="utf-8") as f:
        f.write(ligne_tsv)

# ============================================================
# TRI ET RANGEMENT LOCAL
# ============================================================

def deplacer_vers_a_upload(chemin_dossier_local, donnees, config):
    dossier_base = Path(config.get("dossier_a_upload", r"C:\Users\matton_a\Documents\Permis\A upload"))
    commune_dossier = normaliser_commune_drive(donnees["commune"])
    
    dossier_dest = dossier_base / commune_dossier
    dossier_dest.mkdir(parents=True, exist_ok=True)
    
    nom_dossier = Path(chemin_dossier_local).name
    chemin_final = dossier_dest / nom_dossier

    try:
        shutil.move(str(chemin_dossier_local), str(chemin_final))
    except Exception as e:
        print(f"❌ Erreur lors du déplacement : {e}")

# ============================================================
# TRAITEMENT AVIS
# ============================================================

def trouver_pdf_avis(chemin_dossier, type_avis):
    for fichier in Path(chemin_dossier).iterdir():
        if fichier.suffix.lower() == ".pdf" and fichier.stem.endswith(f" {type_avis}"):
            return str(fichier)
    return None

def traiter_avis(page_avisau, avis, type_avis, chemin_dossier, interface):
    interface.ajouter_log(f"\n📋 Traitement de l'avis {type_avis} : {avis}")
    
    BTN_ACCEPTER = "#main-content > div > app-avisau-detail-consultation > div > div > div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > div:nth-child(3) > ul > li:nth-child(1) > button"
    BTN_REFUSER = "#main-content > div > app-avisau-detail-consultation > div > div > div.fr-col-md-4.fr-col-xs-12 > avisau-etat-consultation > div > div:nth-child(3) > ul > li:nth-child(2) > button"

    if avis == "refus":
        interface.ajouter_log("🖱️ Clic sur 'Refuser'...")
        page_avisau.click(BTN_REFUSER)
        page_avisau.wait_for_timeout(2000)
        interface.ajouter_log("✅ Bouton Refuser cliqué ! (Action manuelle requise sur la page)")
        input("\n⏸️ PAUSE - Appuyez sur ENTRÉE dans la console pour continuer...")
        return

    interface.ajouter_log("🖱️ Clic sur 'Accepter la prise en compte'...")
    page_avisau.click(BTN_ACCEPTER)
    page_avisau.wait_for_timeout(2000)
    
    for _ in range(2):
        try:
            page_avisau.wait_for_selector("[id^='mat-mdc-dialog'] avisau-modal-acceptation li.bouton-droite > button", timeout=5000)
            page_avisau.click("[id^='mat-mdc-dialog'] avisau-modal-acceptation li.bouton-droite > button")
            page_avisau.wait_for_timeout(2000)
        except: pass

    interface.ajouter_log("⏳ Attente du bouton 'Émettre un avis'...")
    try:
        page_avisau.wait_for_selector(BTN_ACCEPTER + ":has-text('mettre')", timeout=15000)
        page_avisau.click(BTN_ACCEPTER)
        page_avisau.wait_for_timeout(2000)
    except: page_avisau.click(BTN_ACCEPTER)

    interface.ajouter_log("📝 Remplissage du formulaire...")
    page_avisau.wait_for_selector("#nature", timeout=10000)
    page_avisau.select_option("#nature", index=MAPPING_NATURE_INDEX[avis])
    page_avisau.select_option("#type", index=1)
    page_avisau.fill("#qualite", "Chargé d'affaires AMO")
    page_avisau.fill("#texteAvis", f"{avis.capitalize()} {type_avis}")

    pdf_avis = trouver_pdf_avis(chemin_dossier, type_avis)
    while not pdf_avis:
        interface.ajouter_log(f"⚠️ Créez le PDF '{type_avis}.pdf' et appuyez sur ENTRÉE dans la console...")
        input(f"")
        pdf_avis = trouver_pdf_avis(chemin_dossier, type_avis)

    interface.ajouter_log("📎 Téléversement du PDF...")
    with page_avisau.expect_file_chooser() as fc_info:
        page_avisau.click("[id^='mat-mdc-dialog'] avisau-modal-emission-avis versement-fichier div.actions label > button")
    fc_info.value.set_files(pdf_avis)
    page_avisau.wait_for_timeout(2000)

    interface.ajouter_log("⏳ En attente de votre clic sur 'Valider' dans Avis'AU...")
    try: page_avisau.wait_for_selector("[id^='mat-mdc-dialog'] avisau-modal-emission-avis li.bouton-droite > button", state="hidden", timeout=300000)
    except: pass
    interface.ajouter_log("✅ Avis transmis !")
    page_avisau.wait_for_timeout(2000)

def traiter_avis_complet(page_avisau, donnees, chemin_dossier, config, interface):
    commune = donnees["commune"]
    societe = get_societe(commune)

    interface.afficher_etape_traitement(donnees["numero_formate"])

    avis_aep = interface.resultat_avis.get("avis_aep", "")
    avis_eu  = interface.resultat_avis.get("avis_eu", "")
    
    donnees_pfac = {
        "contrat": interface.resultat_avis.get("pfac_contrat", ""),
        "surf_anc": interface.resultat_avis.get("pfac_surf_anc", ""),
        "surf_nouv": interface.resultat_avis.get("pfac_surf_nouv", "")
    }

    interface.resultat_avis = {k: None for k in interface.resultat_avis}

    traiter_avis(page_avisau, avis_aep, "AEP", chemin_dossier, interface)

    if societe in ["SAOM", "SAEM"] and avis_eu:
        interface.ajouter_log(f"🔄 Changement de service vers {societe}...")
        
        trouve, _ = trouver_et_ouvrir_dossier(page_avisau, {"numero_brut": donnees["numero_brut"]}, config, service_cible=SERVICES[societe])
        if trouve: 
            traiter_avis(page_avisau, avis_eu, "EU", chemin_dossier, interface)
        else:
            interface.ajouter_log(f"⚠️ DOSSIER NON TROUVÉ DANS {societe} !")
        
        interface.ajouter_log("🔙 Retour vers le service par défaut...")
        page_avisau.goto("https://avisau.cohesion-territoires.gouv.fr/consultation?onglet=PecMetier")
        page_avisau.wait_for_load_state("networkidle")
        page_avisau.select_option("#select", label=config["service"])
        page_avisau.wait_for_load_state("networkidle")

    return avis_aep, avis_eu, donnees_pfac

def finaliser_dossier_background(data, config, stats):
    if not data: return
    
    sauvegarder_ligne_pour_sheets(
        data['donnees'], 
        data['avis_aep'], 
        data['avis_eu'], 
        data.get('donnees_pfac', {})
    )
    
    deplacer_vers_a_upload(data['chemin'], data['donnees'], config)
    
    stats.traites += 1
    if data['avis_aep'] == "incomplet" or data['avis_eu'] == "incomplet": stats.incomplets += 1
    if os.path.exists(QUEUE_FILE): os.remove(QUEUE_FILE)

# ============================================================
# MAIN : WORKFLOW PIPELINE
# ============================================================

def main():
    config = charger_config()
    stats = StatsSession()
    
    with open(FICHIER_EXPORT_SHEETS, "w", encoding="utf-8") as f:
        pass 
    print(f"📄 Fichier '{FICHIER_EXPORT_SHEETS}' initialisé.")
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=config["chrome_profile"], channel="chrome", headless=False, accept_downloads=True
        )
        page_avisau = context.new_page()

        try:
            verifier_et_connecter(page_avisau, config)
            
            reprise = charger_queue()
            if reprise:
                print("📋 Dossier interrompu détecté, finalisation en cours...")
                finaliser_dossier_background(reprise, config, stats)

            chemin_csv = selectionner_service_et_telecharger_csv(page_avisau, config)
            dossiers_csv = lire_csv(chemin_csv)
            interface = InterfaceAvisAU(dossiers_csv)
            historique_bg = None

            while True:
                interface.resultat = {"dossiers": [], "action": "QUIT"}
                interface.afficher_etape_selection()
                interface.relancer()
                
                if interface.resultat["action"] == "QUIT": 
                    if historique_bg: finaliser_dossier_background(historique_bg, config, stats)
                    break
                
                dossiers_selectionnes = interface.resultat["dossiers"]
                if not dossiers_selectionnes:
                    break

                interface.afficher_etape_chargement("...", dossiers_selectionnes[0]["numero_formate"])
                prep_courante = preparer_dossier_pipeline(page_avisau, dossiers_selectionnes[0], config)

                for idx, d_select in enumerate(dossiers_selectionnes):
                    if not prep_courante:
                        if idx + 1 < len(dossiers_selectionnes):
                            interface.afficher_etape_chargement("...", dossiers_selectionnes[idx+1]["numero_formate"])
                            prep_courante = preparer_dossier_pipeline(page_avisau, dossiers_selectionnes[idx+1], config)
                        continue
                        
                    donnees = prep_courante["donnees"]
                    dossier_p = prep_courante["chemin_dossier"]
                    dossier_brut = prep_courante["dossier_brut"]
                    form_p = prep_courante["formulaire"]
                    
                    if form_p: os.startfile(str(form_p))
                    for f_item in dossier_p.iterdir(): 
                        if f_item.is_file(): os.startfile(str(f_item))
                        
                    prep_suivante = None
                    if idx + 1 < len(dossiers_selectionnes):
                        d_suiv = dossiers_selectionnes[idx+1]
                        interface.afficher_etape_chargement(donnees["numero_formate"], d_suiv["numero_formate"])
                        prep_suivante = preparer_dossier_pipeline(page_avisau, d_suiv, config)
                    else:
                        interface.afficher_etape_chargement(donnees["numero_formate"], None)
                        
                    trouve, _ = trouver_et_ouvrir_dossier(page_avisau, dossier_brut, config)
                    
                    if trouve:
                        societe = get_societe(donnees["commune"])
                        interface.afficher_etape_avis(donnees, societe)
                        interface.relancer() 
                        
                        action = interface.resultat_avis.get("action")
                        
                        if action == "ATTENTE":
                            dossier_en_attente = Path(config["dossier_en_attente"])
                            dossier_en_attente.mkdir(parents=True, exist_ok=True)
                            try: shutil.move(str(dossier_p), str(dossier_en_attente / Path(dossier_p).name))
                            except: pass
                            stats.en_attente += 1
                        elif action == "ENVOYER":
                            av_aep, av_eu, donnees_pfac = traiter_avis_complet(page_avisau, donnees, dossier_p, config, interface)
                            
                            historique_bg = {
                                "numero_formate": donnees["numero_formate"], "chemin": str(dossier_p),
                                "donnees": donnees, "type": donnees["numero_brut"][:2].upper(),
                                "avis_aep": av_aep, "avis_eu": av_eu, "donnees_pfac": donnees_pfac
                            }
                            finaliser_dossier_background(historique_bg, config, stats)
                    else:
                        print(f"⚠️ Impossible de retourner sur le dossier {donnees['numero_formate']} pour soumission.")

                    prep_courante = prep_suivante

            stats.generer_rapport()
        finally:
            context.close()
            
        print("\n" + "="*60)
        print("🎉 SESSION TERMINÉE ! N'oubliez pas d'ouvrir le fichier :")
        print(f"   >>>  {FICHIER_EXPORT_SHEETS}  <<<")
        print("   Copiez son contenu (Ctrl+A puis Ctrl+C) et collez-le")
        print("   dans la première case vide de la COLONNE A de Sheets.")
        print("="*60)

if __name__ == "__main__":
    main()