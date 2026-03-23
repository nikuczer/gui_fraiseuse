#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fraiseuse VM32L — Application Raspberry Pi
Interface Tkinter tactile 800x480, navigation clavier.

Onglets:
  - Outil      : sélection outil (PageUp/PageDown), raccourcis s/c/r/f
  - Trapèzes & Z : réglage accel/decel, Z hold-to-move
  - Mécanique  : paramètres axes (steps, microstep, lead)
  - I/O        : état des entrées GPIO (fonctions désactivées)
"""

import math, os, json, time, threading
from typing import Optional
import tkinter as tk
from tkinter import ttk, messagebox

APP_TITLE   = "Fraiseuse VM32L"
APP_VERSION = "2.1.0"

DEFAULT_SPINDLE_MAX_RPM = 3000
DEFAULT_SPINDLE_MIN_RPM = 500

TOOL_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_list.json")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fraiseuse_config.json")
GPIO_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpio_mapping.json")

# ── Catégories d'outils pour les raccourcis clavier ──
CATEGORY_SURFACER   = {"Fraise à surfacer"}
CATEGORY_CHANFREIN  = {"Fraise à chanfreiner"}
CATEGORY_FRAISES    = {"Fraise 2 tailles", "Fraise 3 tailles", "Fraise hémisphérique"}
CATEGORY_FORETS     = {"Foret", "Foret à centrer"}

def calc_cutting_params(tool, piece_mat="acier"):
    """Lit les paramètres pré-calculés par tool_builder directement depuis l'outil."""
    diam = tool.get("diametre", 10.0)
    nb_dents = tool.get("nb_dents", 2)
    fz = tool.get(f"fz_max_{piece_mat}", 0.0)
    vc = tool.get(f"vc_{piece_mat}", 0)
    rpm = tool.get(f"rpm_{piece_mat}", 0)
    feed = tool.get(f"vf_{piece_mat}", 0.0)
    # Détecter si le RPM a été bridé
    rpm_ideal = int((vc * 1000) / (math.pi * diam)) if diam > 0 and vc > 0 else 0
    return {
        "vc": vc, "fz": fz, "rpm_ideal": rpm_ideal, "rpm": rpm,
        "feed": feed, "diam": diam, "nb_dents": nb_dents,
        "limited": rpm < rpm_ideal, "boosted": rpm > rpm_ideal,
    }


# ===== Overlay NumPad =====
class OverlayNumpad(ttk.Frame):
    def __init__(self, master, on_validate, on_cancel, decimals=0, initial=""):
        super().__init__(master, style='Overlay.TFrame')
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()
        self.configure(padding=10)
        self.decimals = int(decimals)
        self.on_validate = on_validate
        self.on_cancel = on_cancel
        self.value_var = tk.StringVar(value=str(initial))
        self.bg = tk.Frame(self, bg="#000000", highlightthickness=0)
        self.bg.place(relx=0, rely=0, relwidth=1, relheight=1)

        card = ttk.Frame(self, padding=10, style='Card.TFrame')
        card.place(relx=0.1, rely=0.12, relwidth=0.8, relheight=0.76)

        lbl = ttk.Label(card, text="Saisir une valeur", anchor="center", font=("Helvetica", 16, "bold"))
        lbl.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        e = ttk.Entry(card, textvariable=self.value_var, justify="center", font=("Helvetica", 24))
        e.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        e.focus_set()

        def add(ch):
            if getattr(self, "_first", True):
                self.value_var.set(""); self._first = False
            self.value_var.set(self.value_var.get() + ch)

        def clear():
            self.value_var.set(""); self._first = True

        def ok():
            txt = self.value_var.get().strip().replace(",", ".")
            try:
                v = float(txt); v = round(v, self.decimals)
                self.destroy()
                self.on_validate(v)
            except Exception:
                messagebox.showerror("Erreur", "Valeur invalide")

        def cancel():
            self.destroy()
            self.on_cancel()

        btns = [("7", 2, 0), ("8", 2, 1), ("9", 2, 2),
                ("4", 3, 0), ("5", 3, 1), ("6", 3, 2),
                ("1", 4, 0), ("2", 4, 1), ("3", 4, 2),
                (",", 5, 0), ("0", 5, 1), (".", 5, 2)]
        for t, r, c in btns:
            ttk.Button(card, text=t, command=lambda T=t: add(T)).grid(
                row=r, column=c, sticky="nsew", padx=4, pady=4, ipady=10)
        ttk.Button(card, text="Effacer", command=clear).grid(
            row=6, column=0, sticky="nsew", padx=4, pady=(6, 0), ipady=10)
        ttk.Button(card, text="OK", command=ok).grid(
            row=6, column=1, sticky="nsew", padx=4, pady=(6, 0), ipady=10)
        ttk.Button(card, text="Annuler", command=cancel).grid(
            row=6, column=2, sticky="nsew", padx=4, pady=(6, 0), ipady=10)

        for r in range(7):
            card.rowconfigure(r, weight=1)
        for c in range(3):
            card.columnconfigure(c, weight=1)


# ===== Application principale =====
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("800x480+0+0")
        try:
            self.attributes('-fullscreen', True)
            self.overrideredirect(True)
        except Exception:
            pass

        # ── Données outils ──
        self.tools = []
        self.filtered_tools = []
        self.current_index = 0
        self.current_category = "all"
        self.piece_mat = tk.StringVar(value="acier")

        # ── Mécanique ──
        self.steps_rev_x = tk.IntVar(value=200)
        self.microstep_x = tk.IntVar(value=16)
        self.lead_x = tk.DoubleVar(value=5.0)
        self.steps_rev_z = tk.IntVar(value=200)
        self.microstep_z = tk.IntVar(value=16)
        self.lead_z = tk.DoubleVar(value=5.0)

        # ── Trapèzes ──
        self.accel_x = tk.DoubleVar(value=300.0)
        self.decel_x = tk.DoubleVar(value=300.0)
        self.rapid_x_speed = tk.DoubleVar(value=1500.0)
        self.z_vmin = tk.DoubleVar(value=50.0)
        self.z_vmax = tk.DoubleVar(value=500.0)
        self.z_accel = tk.DoubleVar(value=300.0)

        # ── Position X (tracking en steps, affiché en mm) ──
        self.pos_x_steps = 0  # position absolue en steps (entier, source de vérité)
        self.pos_x_mm = tk.DoubleVar(value=0.0)  # affiché
        self.pos_x_str = tk.StringVar(value="0.000")
        self.pos_x_mm.trace_add('write', lambda *a: self.pos_x_str.set(
            f"{self.pos_x_mm.get():.3f}"))

        # ── Butées logicielles X ──
        self.limit_x_enabled = tk.BooleanVar(value=False)
        self.limit_x_left = tk.DoubleVar(value=-100.0)   # mm (négatif = gauche)
        self.limit_x_right = tk.DoubleVar(value=100.0)    # mm (positif = droite)
        self.limit_x_left_set = False   # butée gauche définie ?
        self.limit_x_right_set = False  # butée droite définie ?

        # ── Position Z (pour hold-to-move simulation) ──
        self.pos_z_mm = tk.DoubleVar(value=0.0)
        self.home_z_mm = tk.DoubleVar(value=0.0)

        # ── I/O ──
        self.io_active = False
        self.gpio_mapping = {}

        # ── Styles ──
        self._setup_styles()

        # ── UI ──
        self._build_ui()
        self._load_tools()
        self._load_config(silent=True)

        # ── Keyboard bindings ──
        self.bind('<Prior>', lambda e: self._tool_navigate(-1))    # PageUp
        self.bind('<Next>', lambda e: self._tool_navigate(1))      # PageDown
        self.bind('<s>', lambda e: self._filter_category("surfacer"))
        self.bind('<S>', lambda e: self._filter_category("surfacer"))
        self.bind('<c>', lambda e: self._filter_category("chanfrein"))
        self.bind('<C>', lambda e: self._filter_category("chanfrein"))
        self.bind('<r>', lambda e: self._filter_category("fraises"))
        self.bind('<R>', lambda e: self._filter_category("fraises"))
        self.bind('<f>', lambda e: self._filter_category("forets"))
        self.bind('<F>', lambda e: self._filter_category("forets"))
        self.bind('<a>', lambda e: self._filter_category("all"))
        self.bind('<A>', lambda e: self._filter_category("all"))
        self.bind('<F11>', lambda e: self._toggle_fullscreen())
        self.bind('<Escape>', lambda e: self._end_fullscreen())

    # ── Styles ──
    def _setup_styles(self):
        base_font = ("Helvetica", 14)
        btn_font = ("Helvetica", 18)
        med_btn_font = ("Helvetica", 16)
        small_font = ("Helvetica", 11)
        tab_font = ("Helvetica", 16)
        self.option_add("*Font", base_font)
        try:
            st = ttk.Style()
            st.theme_use('clam')
            st.configure('TNotebook.Tab', font=tab_font, padding=(12, 4))
            st.configure('XL.TButton', font=btn_font, padding=10)
            st.configure('Med.TButton', font=med_btn_font, padding=6)
            st.configure('Jog.TButton', font=("Helvetica", 18), padding=4)
            st.configure('Small.TLabel', font=small_font)
            st.configure('Warn.TLabel', foreground="#AA0000", font=base_font)
            st.configure('Info.TLabel', foreground="#0066AA", font=base_font)
            st.configure('Overlay.TFrame', background='', relief='raised')
            st.configure('Card.TFrame', relief='raised')
            st.configure('Big.TLabel', font=("Helvetica", 28, "bold"))
            st.configure('Title.TLabel', font=("Helvetica", 20, "bold"))
            st.configure('Cat.TLabel', font=("Helvetica", 14, "bold"), foreground="#555555")
            st.configure('IOon.TLabel', font=("Helvetica", 16, "bold"), foreground="#00AA00")
            st.configure('IOoff.TLabel', font=("Helvetica", 16), foreground="#888888")
            # Active tab highlight
            st.map('TNotebook.Tab', background=[('selected', '#4a90d9')],
                   foreground=[('selected', 'white')])
        except Exception:
            pass

    # ════════════════════════════════════════════
    #  BUILD UI
    # ════════════════════════════════════════════
    def _build_ui(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.tab_outil = ttk.Frame(self.notebook)
        self.tab_pos = ttk.Frame(self.notebook)
        self.tab_trap = ttk.Frame(self.notebook)
        self.tab_mech = ttk.Frame(self.notebook)
        self.tab_io = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_outil, text=" Outil ")
        self.notebook.add(self.tab_pos, text=" Position ")
        self.notebook.add(self.tab_trap, text=" Trapeze & Z ")
        self.notebook.add(self.tab_mech, text=" Mecanique ")
        self.notebook.add(self.tab_io, text=" I/O ")

        self._build_outil_tab(self.tab_outil)
        self._build_position_tab(self.tab_pos)
        self._build_trapezes_tab(self.tab_trap)
        self._build_mechanics_tab(self.tab_mech)
        self._build_io_tab(self.tab_io)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── Outil tab ──
    def _build_outil_tab(self, tab):
        root = ttk.Frame(tab, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # Barre catégorie
        cat_bar = ttk.Frame(root)
        cat_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.cat_label = ttk.Label(cat_bar, text="TOUS LES OUTILS", style='Cat.TLabel')
        self.cat_label.pack(side="left")
        hint = ttk.Label(cat_bar, text="s=Surfacer  c=Chanfrein  r=Fraises  f=Forets  a=Tous  PgUp/PgDn=Defiler",
                         style='Small.TLabel')
        hint.pack(side="right")

        # Zone outil principal
        tool_frame = ttk.LabelFrame(root, text="Outil selectionne", padding=10)
        tool_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 4))
        root.rowconfigure(1, weight=1)
        tool_frame.columnconfigure(0, weight=1)
        tool_frame.columnconfigure(1, weight=1)

        # Colonne gauche : info outil
        left = ttk.Frame(tool_frame)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)

        self.lbl_tool_name = ttk.Label(left, text="---", style='Title.TLabel')
        self.lbl_tool_name.grid(row=0, column=0, sticky="w")

        self.lbl_tool_type = ttk.Label(left, text="", style='Cat.TLabel')
        self.lbl_tool_type.grid(row=1, column=0, sticky="w", pady=(2, 6))

        info_grid = ttk.Frame(left)
        info_grid.grid(row=2, column=0, sticky="ew")
        labels_left = [
            ("Diametre :", "lbl_diam"),
            ("Nb dents :", "lbl_dents"),
            ("Matiere :", "lbl_matiere"),
            ("Revetement :", "lbl_revet"),
        ]
        for i, (txt, attr) in enumerate(labels_left):
            ttk.Label(info_grid, text=txt, font=("Helvetica", 14)).grid(row=i, column=0, sticky="e", padx=(0, 6))
            lbl = ttk.Label(info_grid, text="---", font=("Helvetica", 14, "bold"))
            lbl.grid(row=i, column=1, sticky="w")
            setattr(self, attr, lbl)

        self.lbl_tool_notes = ttk.Label(left, text="", style='Small.TLabel', wraplength=300)
        self.lbl_tool_notes.grid(row=3, column=0, sticky="w", pady=(6, 0))

        self.lbl_tool_counter = ttk.Label(left, text="0 / 0", font=("Helvetica", 12))
        self.lbl_tool_counter.grid(row=4, column=0, sticky="w", pady=(6, 0))

        # Colonne droite : résultats coupe
        right = ttk.LabelFrame(tool_frame, text="Parametres de coupe", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        # Sélection matériau pièce
        mat_frame = ttk.Frame(right)
        mat_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(mat_frame, text="Piece :", font=("Helvetica", 14)).pack(side="left")
        for label, key in (("Acier", "acier"), ("Alu", "alu")):
            ttk.Radiobutton(mat_frame, text=label, value=key, variable=self.piece_mat,
                            command=self._update_tool_display).pack(side="left", padx=8)

        ttk.Label(right, text="Vitesse broche (tr/min) :").grid(row=1, column=0, sticky="w")
        self.lbl_rpm = ttk.Label(right, text="---", style='Big.TLabel')
        self.lbl_rpm.grid(row=2, column=0, sticky="w", pady=(0, 4))

        ttk.Label(right, text="Avance (mm/min) :").grid(row=3, column=0, sticky="w")
        self.lbl_feed = ttk.Label(right, text="---", style='Big.TLabel')
        self.lbl_feed.grid(row=4, column=0, sticky="w", pady=(0, 4))

        self.lbl_details = ttk.Label(right, text="", style='Small.TLabel', wraplength=300)
        self.lbl_details.grid(row=5, column=0, sticky="w", pady=(2, 0))

        self.lbl_limit = ttk.Label(right, text="", style='Warn.TLabel')
        self.lbl_limit.grid(row=6, column=0, sticky="w", pady=(2, 0))

        self.lbl_foret_info = ttk.Label(right, text="", style='Info.TLabel')
        self.lbl_foret_info.grid(row=7, column=0, sticky="w", pady=(2, 0))

        # Boutons nav en bas
        nav_frame = ttk.Frame(root)
        nav_frame.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(nav_frame, text="<< Precedent", style='Med.TButton',
                   command=lambda: self._tool_navigate(-1)).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(nav_frame, text="Suivant >>", style='Med.TButton',
                   command=lambda: self._tool_navigate(1)).pack(side="left", expand=True, fill="x", padx=(4, 0))

    # ── Position tab ──
    def _build_position_tab(self, tab):
        root = ttk.Frame(tab, padding=6)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        # ── Colonne gauche : position X + jog ──
        left = ttk.LabelFrame(root, text="Position X", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 4))
        left.columnconfigure(0, weight=1)

        pos_frame = ttk.Frame(left)
        pos_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(pos_frame, text="X =", font=("Helvetica", 18, "bold")).pack(side="left")
        self.lbl_pos_x = ttk.Label(pos_frame, textvariable=self.pos_x_str,
                                    font=("Helvetica", 32, "bold"))
        self.lbl_pos_x.pack(side="left", padx=(6, 0))
        ttk.Label(pos_frame, text="mm", font=("Helvetica", 18)).pack(side="left", padx=(4, 0))

        # Jog buttons
        jog_frame = ttk.Frame(left)
        jog_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        jog_values = [("-10", -10), ("-1", -1), ("-0.1", -0.1),
                      ("+0.1", 0.1), ("+1", 1), ("+10", 10)]
        for c, (txt, delta) in enumerate(jog_values):
            ttk.Button(jog_frame, text=txt, style='Jog.TButton',
                       command=lambda d=delta: self._jog_x(d)).grid(
                row=0, column=c, sticky="nsew", padx=2, pady=2, ipady=4)
            jog_frame.columnconfigure(c, weight=1)

        # Zero + Home buttons
        ctl_frame = ttk.Frame(left)
        ctl_frame.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(ctl_frame, text="ZERO X (ici)", style='Med.TButton',
                   command=self._zero_x).grid(row=0, column=0, sticky="nsew", padx=2, ipady=4)
        ctl_frame.columnconfigure(0, weight=1)

        # Status butée
        self.lbl_limit_status = ttk.Label(left, text="", style='Info.TLabel')
        self.lbl_limit_status.grid(row=3, column=0, sticky="w", pady=(2, 0))

        # ── Colonne droite : butées logicielles ──
        right = ttk.LabelFrame(root, text="Butees logicielles X", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=(0, 4))
        right.columnconfigure(0, weight=1)
        right.columnconfigure(1, weight=1)

        # Enable/disable
        chk = ttk.Checkbutton(right, text="Activer les butees",
                               variable=self.limit_x_enabled,
                               command=self._update_limit_display)
        chk.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # Butée gauche
        ttk.Label(right, text="Butee GAUCHE :", font=("Helvetica", 14)).grid(
            row=1, column=0, sticky="w", pady=(0, 2))
        self.lbl_limit_left = ttk.Label(right, text="Non definie",
                                         font=("Helvetica", 16, "bold"))
        self.lbl_limit_left.grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Button(right, text="Definir ICI", style='Med.TButton',
                   command=self._set_limit_left).grid(
            row=3, column=0, sticky="ew", padx=(0, 2), ipady=4)

        # Butée droite
        ttk.Label(right, text="Butee DROITE :", font=("Helvetica", 14)).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=(0, 2))
        self.lbl_limit_right = ttk.Label(right, text="Non definie",
                                          font=("Helvetica", 16, "bold"))
        self.lbl_limit_right.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(0, 4))
        ttk.Button(right, text="Definir ICI", style='Med.TButton',
                   command=self._set_limit_right).grid(
            row=3, column=1, sticky="ew", padx=(2, 0), ipady=4)

        # Saisie manuelle des limites
        ttk.Separator(right).grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)
        manual_frame = ttk.Frame(right)
        manual_frame.grid(row=5, column=0, columnspan=2, sticky="ew")
        manual_frame.columnconfigure(1, weight=1)
        manual_frame.columnconfigure(3, weight=1)

        ttk.Label(manual_frame, text="G:").grid(row=0, column=0, sticky="e", padx=(0, 2))
        self._num_entry(manual_frame, self.limit_x_left, width=7, decimals=1,
                        row=0, column=1, sticky="ew")
        ttk.Label(manual_frame, text="mm   D:").grid(row=0, column=2, sticky="e", padx=(8, 2))
        self._num_entry(manual_frame, self.limit_x_right, width=7, decimals=1,
                        row=0, column=3, sticky="ew")
        ttk.Label(manual_frame, text="mm").grid(row=0, column=4, sticky="w", padx=(2, 0))

        # Effacer butées
        ttk.Button(right, text="Effacer les butees", style='Med.TButton',
                   command=self._clear_limits).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0), ipady=4)

        # Barre info en bas
        info_frame = ttk.Frame(root)
        info_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.lbl_pos_info = ttk.Label(info_frame, text="Butees software = protection, pas un remplacement de fin de course hardware",
                                       style='Small.TLabel')
        self.lbl_pos_info.pack(anchor="center")

    # ── Position X helpers ──
    def _steps_per_mm(self):
        return (self.steps_rev_x.get() * self.microstep_x.get()) / max(self.lead_x.get(), 1e-9)

    def _update_pos_x_from_steps(self):
        self.pos_x_mm.set(round(self.pos_x_steps / self._steps_per_mm(), 3))

    def _zero_x(self):
        # Recaler les butées par rapport au nouveau zéro avant de remettre à 0
        old_pos = self.pos_x_mm.get()
        if self.limit_x_left_set:
            self.limit_x_left.set(round(self.limit_x_left.get() - old_pos, 3))
        if self.limit_x_right_set:
            self.limit_x_right.set(round(self.limit_x_right.get() - old_pos, 3))
        self.pos_x_steps = 0
        self._update_pos_x_from_steps()
        self._update_limit_display()

    def _set_limit_left(self):
        self.limit_x_left.set(round(self.pos_x_mm.get(), 3))
        self.limit_x_left_set = True
        self.limit_x_enabled.set(True)
        self._update_limit_display()

    def _set_limit_right(self):
        self.limit_x_right.set(round(self.pos_x_mm.get(), 3))
        self.limit_x_right_set = True
        self.limit_x_enabled.set(True)
        self._update_limit_display()

    def _clear_limits(self):
        self.limit_x_left_set = False
        self.limit_x_right_set = False
        self.limit_x_enabled.set(False)
        self.limit_x_left.set(-100.0)
        self.limit_x_right.set(100.0)
        self._update_limit_display()

    def _update_limit_display(self):
        if self.limit_x_left_set:
            self.lbl_limit_left.config(text=f"{self.limit_x_left.get():.3f} mm")
        else:
            self.lbl_limit_left.config(text="Non definie")
        if self.limit_x_right_set:
            self.lbl_limit_right.config(text=f"{self.limit_x_right.get():.3f} mm")
        else:
            self.lbl_limit_right.config(text="Non definie")

        if self.limit_x_enabled.get() and (self.limit_x_left_set or self.limit_x_right_set):
            self.lbl_limit_status.config(text="Butees ACTIVES", style='IOon.TLabel')
        else:
            self.lbl_limit_status.config(text="Butees inactives", style='IOoff.TLabel')

    def _clamp_move_x(self, delta_mm):
        """Tronque un déplacement X pour respecter les butées logicielles.
        Retourne le delta effectif (peut être 0 si bloqué)."""
        if not self.limit_x_enabled.get():
            return delta_mm

        current = self.pos_x_mm.get()
        target = current + delta_mm

        if delta_mm < 0 and self.limit_x_left_set:
            left = self.limit_x_left.get()
            if target < left:
                target = left
        if delta_mm > 0 and self.limit_x_right_set:
            right = self.limit_x_right.get()
            if target > right:
                target = right

        return target - current

    def _jog_x(self, delta_mm):
        """Déplacement X simulé avec respect des butées."""
        if self.io_active:
            return
        clamped = self._clamp_move_x(delta_mm)
        if abs(clamped) < 1e-6:
            # Bloqué par butée
            self.lbl_limit_status.config(text="BUTEE ATTEINTE !", style='Warn.TLabel')
            self.after(1500, self._update_limit_display)
            return
        clamped_steps = round(clamped * self._steps_per_mm())
        self.pos_x_steps += clamped_steps
        self._update_pos_x_from_steps()
        self._update_limit_display()

    # ── Trapèzes & Z tab ──
    def _build_trapezes_tab(self, tab):
        root = ttk.Frame(tab, padding=6)
        root.pack(fill="both", expand=True)

        xbox = ttk.LabelFrame(root, text="Axe X - Profil & Rapide")
        xbox.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 4))
        ttk.Label(xbox, text="Acceleration X (mm/s2)").grid(row=0, column=0, sticky="e", padx=4)
        self._num_entry(xbox, self.accel_x, width=8, decimals=0, row=0, column=1, sticky="w")
        ttk.Label(xbox, text="Deceleration X (mm/s2)").grid(row=1, column=0, sticky="e", padx=4)
        self._num_entry(xbox, self.decel_x, width=8, decimals=0, row=1, column=1, sticky="w")
        ttk.Label(xbox, text="Vitesse RAPIDE X (mm/min)").grid(row=2, column=0, sticky="e", padx=4)
        self._num_entry(xbox, self.rapid_x_speed, width=8, decimals=0, row=2, column=1, sticky="w")

        zbox = ttk.LabelFrame(root, text="Axe Z - Maintenir")
        zbox.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=(0, 4))
        ttk.Label(zbox, text="Vitesse min Z (mm/min)").grid(row=0, column=0, sticky="e", padx=4)
        self._num_entry(zbox, self.z_vmin, width=8, decimals=0, row=0, column=1, sticky="w")
        ttk.Label(zbox, text="Vitesse max Z (mm/min)").grid(row=1, column=0, sticky="e", padx=4)
        self._num_entry(zbox, self.z_vmax, width=8, decimals=0, row=1, column=1, sticky="w")
        ttk.Label(zbox, text="Acceleration Z (mm/s2)").grid(row=2, column=0, sticky="e", padx=4)
        self._num_entry(zbox, self.z_accel, width=8, decimals=0, row=2, column=1, sticky="w")

        btns = ttk.Frame(zbox)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        up = ttk.Button(btns, text="Z HAUT (maintenir)", style='Med.TButton')
        dn = ttk.Button(btns, text="Z BAS (maintenir)", style='Med.TButton')
        up.grid(row=0, column=0, sticky="nsew", padx=4, ipady=4)
        dn.grid(row=0, column=1, sticky="nsew", padx=4, ipady=4)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        up.bind("<ButtonPress-1>", lambda e: self._z_hold(True))
        up.bind("<ButtonRelease-1>", lambda e: self._z_release(True))
        dn.bind("<ButtonPress-1>", lambda e: self._z_hold(False))
        dn.bind("<ButtonRelease-1>", lambda e: self._z_release(False))

        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        rowbtn = ttk.Frame(root)
        rowbtn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(rowbtn, text="Sauvegarder", style='Med.TButton',
                   command=self._save_config).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Button(rowbtn, text="Recharger", style='Med.TButton',
                   command=lambda: self._load_config(silent=False)).pack(side="left", expand=True, fill="x", padx=4)

    # ── Mécanique tab ──
    def _build_mechanics_tab(self, tab):
        root = ttk.Frame(tab, padding=6)
        root.pack(fill="both", expand=True)

        x = ttk.LabelFrame(root, text="Axe X")
        z = ttk.LabelFrame(root, text="Axe Z")
        x.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        z.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        ttk.Label(x, text="Steps par tour").grid(row=0, column=0, sticky="e", padx=4)
        self._num_entry(x, self.steps_rev_x, width=8, decimals=0, row=0, column=1, sticky="w")
        ttk.Label(x, text="Microstep").grid(row=1, column=0, sticky="e", padx=4)
        self._num_entry(x, self.microstep_x, width=8, decimals=0, row=1, column=1, sticky="w")
        ttk.Label(x, text="Lead (mm)").grid(row=2, column=0, sticky="e", padx=4)
        self._num_entry(x, self.lead_x, width=8, decimals=2, row=2, column=1, sticky="w")

        ttk.Label(z, text="Steps par tour").grid(row=0, column=0, sticky="e", padx=4)
        self._num_entry(z, self.steps_rev_z, width=8, decimals=0, row=0, column=1, sticky="w")
        ttk.Label(z, text="Microstep").grid(row=1, column=0, sticky="e", padx=4)
        self._num_entry(z, self.microstep_z, width=8, decimals=0, row=1, column=1, sticky="w")
        ttk.Label(z, text="Lead (mm)").grid(row=2, column=0, sticky="e", padx=4)
        self._num_entry(z, self.lead_z, width=8, decimals=2, row=2, column=1, sticky="w")

        ver_frame = ttk.Frame(root)
        ver_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(ver_frame, text=f"Version : v{APP_VERSION}",
                  font=("Helvetica", 12, "bold")).pack(anchor="center")

        rowbtn = ttk.Frame(root)
        rowbtn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(rowbtn, text="Sauvegarder", style='Med.TButton',
                   command=self._save_config).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Button(rowbtn, text="Recharger", style='Med.TButton',
                   command=lambda: self._load_config(silent=False)).pack(side="left", expand=True, fill="x", padx=4)

    # ── I/O tab ──
    def _build_io_tab(self, tab):
        root = ttk.Frame(tab, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text="Etat des entrees GPIO",
                  style='Title.TLabel').grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(root, text="(Fonctions moteur desactivees sur cet onglet)",
                  style='Warn.TLabel').grid(row=1, column=0, sticky="w", pady=(0, 10))

        self.io_frame = ttk.Frame(root)
        self.io_frame.grid(row=2, column=0, sticky="nsew")
        root.rowconfigure(2, weight=1)

        self.io_labels = {}
        self._build_io_entries()

    def _build_io_entries(self):
        for w in self.io_frame.winfo_children():
            w.destroy()
        self.io_labels = {}

        # Charger le mapping GPIO
        self._load_gpio_mapping()

        if not self.gpio_mapping:
            # Afficher les entrées par défaut du pupitre
            default_inputs = [
                ("X Gauche", None),
                ("X Droite", None),
                ("Vitesse Rapide X", None),
                ("Z Haut", None),
                ("Z Bas", None),
                ("Disable Drivers", None),
                ("Jog Encodeur A", None),
                ("Jog Encodeur B", None),
            ]
        else:
            default_inputs = [(name, info.get("gpio"))
                              for name, info in self.gpio_mapping.items()]

        self.io_frame.columnconfigure(0, weight=1)
        self.io_frame.columnconfigure(1, weight=0)
        self.io_frame.columnconfigure(2, weight=0)

        for i, (name, gpio) in enumerate(default_inputs):
            ttk.Label(self.io_frame, text=name, font=("Helvetica", 15)).grid(
                row=i, column=0, sticky="w", padx=4, pady=3)
            gpio_txt = f"GPIO {gpio}" if gpio is not None else "Non assigne"
            ttk.Label(self.io_frame, text=gpio_txt, font=("Helvetica", 13)).grid(
                row=i, column=1, sticky="e", padx=(4, 10), pady=3)
            state_lbl = ttk.Label(self.io_frame, text="---", style='IOoff.TLabel')
            state_lbl.grid(row=i, column=2, sticky="e", padx=4, pady=3)
            self.io_labels[name] = state_lbl

    # ════════════════════════════════════════════
    #  TOOL NAVIGATION
    # ════════════════════════════════════════════
    def _load_tools(self):
        self.tools = []
        try:
            with open(TOOL_FILE, "r", encoding="utf-8") as f:
                self.tools = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Erreur chargement {TOOL_FILE}: {e}")
        self._apply_filter()

    def _filter_category(self, cat):
        self.current_category = cat
        self.current_index = 0
        self._apply_filter()

    def _apply_filter(self):
        if self.current_category == "surfacer":
            self.filtered_tools = [t for t in self.tools if t.get("type") in CATEGORY_SURFACER]
            self.cat_label.config(text="FRAISES A SURFACER  [S]")
        elif self.current_category == "chanfrein":
            self.filtered_tools = [t for t in self.tools if t.get("type") in CATEGORY_CHANFREIN]
            self.cat_label.config(text="FRAISES A CHANFREINER  [C]")
        elif self.current_category == "fraises":
            self.filtered_tools = [t for t in self.tools if t.get("type") in CATEGORY_FRAISES]
            self.cat_label.config(text="AUTRES FRAISES  [R]")
        elif self.current_category == "forets":
            self.filtered_tools = [t for t in self.tools if t.get("type") in CATEGORY_FORETS]
            self.cat_label.config(text="FORETS  [F]")
        else:
            self.filtered_tools = list(self.tools)
            self.cat_label.config(text="TOUS LES OUTILS  [A]")

        if self.current_index >= len(self.filtered_tools):
            self.current_index = max(0, len(self.filtered_tools) - 1)
        self._update_tool_display()

    def _tool_navigate(self, direction):
        if not self.filtered_tools:
            return
        self.current_index = (self.current_index + direction) % len(self.filtered_tools)
        self._update_tool_display()

    def _update_tool_display(self):
        if not self.filtered_tools:
            self.lbl_tool_name.config(text="Aucun outil")
            self.lbl_tool_type.config(text="")
            self.lbl_diam.config(text="---")
            self.lbl_dents.config(text="---")
            self.lbl_matiere.config(text="---")
            self.lbl_revet.config(text="---")
            self.lbl_tool_notes.config(text="")
            self.lbl_tool_counter.config(text="0 / 0")
            self.lbl_rpm.config(text="---")
            self.lbl_feed.config(text="---")
            self.lbl_details.config(text="")
            self.lbl_limit.config(text="")
            self.lbl_foret_info.config(text="")
            return

        tool = self.filtered_tools[self.current_index]
        nom = tool.get("nom", "Sans nom") or "Sans nom"
        tool_type = tool.get("type", "")
        diam = tool.get("diametre", 0)
        nb_dents = tool.get("nb_dents", 0)
        matiere = tool.get("matiere", "")
        revet = tool.get("revetement", "Aucun")
        notes = tool.get("notes", "")
        is_foret = tool_type in CATEGORY_FORETS

        self.lbl_tool_name.config(text=nom)
        self.lbl_tool_type.config(text=tool_type)
        self.lbl_diam.config(text=f"{diam} mm")
        self.lbl_dents.config(text=str(nb_dents))
        self.lbl_matiere.config(text=matiere)
        self.lbl_revet.config(text=revet)
        self.lbl_tool_notes.config(text=notes if notes else "")
        self.lbl_tool_counter.config(
            text=f"{self.current_index + 1} / {len(self.filtered_tools)}")

        # Calcul paramètres de coupe
        piece = self.piece_mat.get()
        params = calc_cutting_params(tool, piece)

        if is_foret:
            # Foret : RPM affiché, mais avance X = rapide (juste pour positionner)
            self.lbl_rpm.config(text=f"{params['rpm']:,}".replace(",", " "))
            rapid = self.rapid_x_speed.get()
            self.lbl_feed.config(text=f"{rapid:,.0f}".replace(",", " "))
            self.lbl_details.config(
                text=f"D={diam} mm | Vc={params['vc']} m/min | fz={params['fz']:.3f} mm/dent")
            self.lbl_foret_info.config(text="Foret : vitesse X = rapide (positionnement)")
        else:
            self.lbl_rpm.config(text=f"{params['rpm']:,}".replace(",", " "))
            self.lbl_feed.config(text=f"{params['feed']:,.0f}".replace(",", " "))
            self.lbl_details.config(
                text=f"D={diam} mm | z={nb_dents} | Vc={params['vc']} m/min | fz={params['fz']:.3f} mm/dent")
            self.lbl_foret_info.config(text="")

        if params["limited"]:
            self.lbl_limit.config(text=f"Limite par broche max ({DEFAULT_SPINDLE_MAX_RPM} RPM)")
        elif params["boosted"]:
            self.lbl_limit.config(text=f"Force au RPM min ({DEFAULT_SPINDLE_MIN_RPM} RPM)")
        else:
            self.lbl_limit.config(text="")

    # ════════════════════════════════════════════
    #  Z HOLD-TO-MOVE (simulation)
    # ════════════════════════════════════════════
    def _z_hold(self, up):
        if self.io_active:
            return  # Désactivé sur l'onglet I/O
        if getattr(self, '_z_running', False):
            return
        self._z_running = True
        speed = max(self.z_vmin.get(), min(self.z_vmax.get(), self.z_vmax.get()))
        v = speed / 60.0 * (1 if up else -1)

        def run():
            try:
                last = time.time()
                while self._z_running:
                    now = time.time()
                    dt = now - last
                    last = now
                    new = self.pos_z_mm.get() + v * dt
                    self.after(0, lambda val=new: self.pos_z_mm.set(round(val, 1)))
                    time.sleep(0.02)
            finally:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _z_release(self, up):
        self._z_running = False

    # ════════════════════════════════════════════
    #  I/O TAB MANAGEMENT
    # ════════════════════════════════════════════
    def _on_tab_changed(self, event):
        current = self.notebook.index(self.notebook.select())
        io_tab_index = self.notebook.index(self.tab_io)
        was_active = self.io_active
        self.io_active = (current == io_tab_index)

        if self.io_active and not was_active:
            self._start_io_polling()
        elif not self.io_active and was_active:
            self._stop_io_polling()

    def _start_io_polling(self):
        self._io_polling = True
        self._poll_io()

    def _stop_io_polling(self):
        self._io_polling = False

    def _poll_io(self):
        if not getattr(self, '_io_polling', False):
            return
        # Essayer de lire les GPIO via pigpio
        try:
            import pigpio
            if not hasattr(self, '_pi') or self._pi is None:
                self._pi = pigpio.pi()
            if self._pi.connected:
                for name, info in self.gpio_mapping.items():
                    gpio = info.get("gpio")
                    if gpio is not None and name in self.io_labels:
                        level = self._pi.read(gpio)
                        active_low = info.get("active_low", False)
                        is_active = (level == 0) if active_low else (level == 1)
                        if is_active:
                            self.io_labels[name].config(text="ACTIF", style='IOon.TLabel')
                        else:
                            self.io_labels[name].config(text="inactif", style='IOoff.TLabel')
            else:
                for name in self.io_labels:
                    self.io_labels[name].config(text="pigpio off", style='IOoff.TLabel')
        except ImportError:
            # Pas de pigpio (mode PC/simulation)
            for name in self.io_labels:
                self.io_labels[name].config(text="(simulation)", style='IOoff.TLabel')
        except Exception:
            for name in self.io_labels:
                self.io_labels[name].config(text="erreur", style='IOoff.TLabel')

        if getattr(self, '_io_polling', False):
            self.after(200, self._poll_io)

    def _load_gpio_mapping(self):
        self.gpio_mapping = {}
        try:
            with open(GPIO_FILE, "r", encoding="utf-8") as f:
                self.gpio_mapping = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # ════════════════════════════════════════════
    #  HELPERS
    # ════════════════════════════════════════════
    def _num_entry(self, parent, var, width=8, decimals=0, **grid_kwargs):
        e = ttk.Entry(parent, textvariable=var, width=width, state="readonly", justify="left")

        def _show_numpad(evn=None):
            if self.io_active:
                return
            try:
                init = f"{float(var.get()):.{decimals}f}" if decimals > 0 else str(int(float(var.get())))
            except Exception:
                init = str(var.get())

            def on_ok(v):
                if decimals == 0:
                    var.set(int(v))
                else:
                    var.set(round(float(v), decimals))
            OverlayNumpad(self, on_validate=on_ok, on_cancel=lambda: None,
                          decimals=decimals, initial=init)
        e.bind("<Button-1>", _show_numpad)
        if grid_kwargs:
            e.grid(**grid_kwargs)
        return e

    # ════════════════════════════════════════════
    #  SAVE / LOAD CONFIG
    # ════════════════════════════════════════════
    def _save_config(self):
        data = {
            "accel_x": float(self.accel_x.get()),
            "decel_x": float(self.decel_x.get()),
            "rapid_x_speed": float(self.rapid_x_speed.get()),
            "z_vmin": float(self.z_vmin.get()),
            "z_vmax": float(self.z_vmax.get()),
            "z_accel": float(self.z_accel.get()),
            "mechanics": {
                "steps_rev_x": int(self.steps_rev_x.get()),
                "microstep_x": int(self.microstep_x.get()),
                "lead_x": float(self.lead_x.get()),
                "steps_rev_z": int(self.steps_rev_z.get()),
                "microstep_z": int(self.microstep_z.get()),
                "lead_z": float(self.lead_z.get()),
            },
            "limits": {
                "enabled": bool(self.limit_x_enabled.get()),
                "left": float(self.limit_x_left.get()),
                "right": float(self.limit_x_right.get()),
                "left_set": self.limit_x_left_set,
                "right_set": self.limit_x_right_set,
            },
            "position": {
                "pos_x_steps": self.pos_x_steps,
            },
            "app_version": APP_VERSION,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            messagebox.showinfo("Sauvegarde", f"Config enregistree dans\n{CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'enregistrer : {e}")

    def _load_config(self, silent=False):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.accel_x.set(float(data.get("accel_x", self.accel_x.get())))
            self.decel_x.set(float(data.get("decel_x", self.decel_x.get())))
            self.rapid_x_speed.set(float(data.get("rapid_x_speed", self.rapid_x_speed.get())))
            self.z_vmin.set(float(data.get("z_vmin", self.z_vmin.get())))
            self.z_vmax.set(float(data.get("z_vmax", self.z_vmax.get())))
            self.z_accel.set(float(data.get("z_accel", self.z_accel.get())))
            mech = data.get("mechanics", {})
            self.steps_rev_x.set(int(mech.get("steps_rev_x", self.steps_rev_x.get())))
            self.microstep_x.set(int(mech.get("microstep_x", self.microstep_x.get())))
            self.lead_x.set(float(mech.get("lead_x", self.lead_x.get())))
            self.steps_rev_z.set(int(mech.get("steps_rev_z", self.steps_rev_z.get())))
            self.microstep_z.set(int(mech.get("microstep_z", self.microstep_z.get())))
            self.lead_z.set(float(mech.get("lead_z", self.lead_z.get())))
            lim = data.get("limits", {})
            self.limit_x_enabled.set(bool(lim.get("enabled", False)))
            self.limit_x_left.set(float(lim.get("left", -100.0)))
            self.limit_x_right.set(float(lim.get("right", 100.0)))
            self.limit_x_left_set = bool(lim.get("left_set", False))
            self.limit_x_right_set = bool(lim.get("right_set", False))
            pos = data.get("position", {})
            self.pos_x_steps = int(pos.get("pos_x_steps", 0))
            self._update_pos_x_from_steps()
            self._update_limit_display()
            if not silent:
                messagebox.showinfo("Chargement", "Config chargee.")
        except FileNotFoundError:
            if not silent:
                messagebox.showwarning("Fichier introuvable", "Aucun fichier de config trouve.")
        except Exception as e:
            if not silent:
                messagebox.showerror("Erreur", f"Chargement impossible : {e}")

    # ── Fullscreen ──
    def _toggle_fullscreen(self):
        try:
            is_fs = self.attributes('-fullscreen')
            self.attributes('-fullscreen', not is_fs)
            self.overrideredirect(not is_fs)
        except Exception:
            pass

    def _end_fullscreen(self):
        try:
            self.attributes('-fullscreen', False)
            self.overrideredirect(False)
        except Exception:
            pass


# ====== Entrée ======
if __name__ == "__main__":
    try:
        app = App()
        app.mainloop()
    except KeyboardInterrupt:
        pass
