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

import math, os, json
from typing import Optional
import tkinter as tk
from tkinter import ttk, messagebox
from motor_controller import (
    MotorController, CmdType, StatusType, Status, Cmd,
    create_motor_system
)

APP_TITLE   = "Fraiseuse VM32L"
APP_VERSION = "2.2.0"

DEFAULT_SPINDLE_MAX_RPM = 3000
DEFAULT_SPINDLE_MIN_RPM = 500

# Vf max réaliste (mm/min) — steppers + rigidité bâti
VF_MAX_STEPPER = 800

TOOL_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_list.json")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fraiseuse_config.json")
GPIO_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpio_mapping.json")

# ── Catégories d'outils pour les raccourcis clavier ──
CATEGORY_SURFACER   = {"Fraise à surfacer"}
CATEGORY_CHANFREIN  = {"Fraise à chanfreiner"}
CATEGORY_FRAISES    = {"Fraise 2 tailles", "Fraise 3 tailles", "Fraise hémisphérique"}
CATEGORY_FORETS     = {"Foret", "Foret à centrer"}

# ── Tables ap max (Ø mm, ap_max mm) — ébauche VM32L ──
# Acier : conservateur (machine légère, efforts importants)
AP_MAX_ACIER = [(4, 1.0), (6, 1.5), (8, 2.0), (10, 2.5), (12, 3.0), (16, 4.0), (20, 5.0)]
# Alu : bien plus permissif (efforts faibles, bonne évacuation copeaux)
AP_MAX_ALU   = [(4, 3.0), (6, 4.0), (8, 5.0), (10, 6.0), (12, 7.0), (16, 8.0), (20, 10.0)]
AP_MAX_PLAQ  = [(20, 2.0), (50, 3.0), (80, 4.0)]


def _interp_table(table, val):
    if not table:
        return 1.0
    if val <= table[0][0]:
        return table[0][1]
    if val >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        x0, y0 = table[i]
        x1, y1 = table[i + 1]
        if x0 <= val <= x1:
            t = (val - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return table[-1][1]


def _fz_correction(ae_ratio, ap_ratio):
    """Correction fz selon engagement ae/Ø et ap/ap_max.
    Plus l'engagement est fort, plus on réduit le fz pour protéger la machine."""
    # Correction radiale (ae)
    if ae_ratio >= 0.9:
        corr_ae = 0.7     # rainurage
    elif ae_ratio >= 0.7:
        corr_ae = 0.85
    elif ae_ratio >= 0.4:
        corr_ae = 1.0     # référence
    elif ae_ratio >= 0.15:
        corr_ae = 1.15    # chip thinning
    else:
        corr_ae = 1.25

    # Correction axiale (ap) — réduire quand ap est élevé
    if ap_ratio >= 1.2:
        corr_ap = 0.5     # très agressif, diviser par 2
    elif ap_ratio >= 1.0:
        corr_ap = 0.7     # au-dessus du max recommandé
    elif ap_ratio >= 0.8:
        corr_ap = 0.85    # approche du max
    elif ap_ratio >= 0.5:
        corr_ap = 1.0     # zone confort
    else:
        corr_ap = 1.1     # passes légères = on peut pousser un peu

    return corr_ae * corr_ap


def calc_cutting_params(tool, piece_mat="acier"):
    """Lit les paramètres pré-calculés par tool_builder directement depuis l'outil."""
    diam = tool.get("diametre", 10.0)
    nb_dents = tool.get("nb_dents", 2)
    fz = tool.get(f"fz_max_{piece_mat}", 0.0)
    vc = tool.get(f"vc_{piece_mat}", 0)
    vc_reelle = tool.get(f"vc_reelle_{piece_mat}", 0)
    rpm = tool.get(f"rpm_{piece_mat}", 0)
    feed = tool.get(f"vf_{piece_mat}", 0.0)
    # Détecter si le RPM a été bridé
    rpm_ideal = int((vc * 1000) / (math.pi * diam)) if diam > 0 and vc > 0 else 0
    # Vc réelle (depuis JSON ou recalculée)
    if not vc_reelle and diam > 0 and rpm > 0:
        vc_reelle = round(math.pi * diam * rpm / 1000, 1)
    return {
        "vc": vc, "vc_reelle": vc_reelle, "fz": fz,
        "rpm_ideal": rpm_ideal, "rpm": rpm,
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

        # ── Engagement (ae/ap) ──
        self.ae_mm = tk.DoubleVar(value=5.0)
        self.ap_mm = tk.DoubleVar(value=1.0)
        self._last_tool_id = None  # pour détecter changement d'outil

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

        # ── MotorController ──
        self._init_motor()

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
        self.bind('<plus>', lambda e: self._adjust_ae(1))
        self.bind('<minus>', lambda e: self._adjust_ae(-1))
        self.bind('<KP_Add>', lambda e: self._adjust_ae(1))
        self.bind('<KP_Subtract>', lambda e: self._adjust_ae(-1))
        self.bind('<equal>', lambda e: self._adjust_ae(1))  # + sans shift
        self.bind('<9>', lambda e: self._adjust_ap(1))
        self.bind('<6>', lambda e: self._adjust_ap(-1))
        self.bind('<KP_9>', lambda e: self._adjust_ap(1))
        self.bind('<KP_6>', lambda e: self._adjust_ap(-1))
        self.bind('<5>', lambda e: self._reset_engagement())
        self.bind('<KP_5>', lambda e: self._reset_engagement())
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
            st.configure('Mat.TRadiobutton', font=("Helvetica", 16, "bold"), padding=(10, 8))
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
        root = ttk.Frame(tab, padding=4)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=210)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(2, weight=0, minsize=220)
        root.rowconfigure(1, weight=1)

        # Row 0: category bar
        cat_bar = ttk.Frame(root)
        cat_bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        self.cat_label = ttk.Label(cat_bar, text="TOUS LES OUTILS", style='Cat.TLabel')
        self.cat_label.pack(side="left")
        ttk.Label(cat_bar, text="s/c/r/f/a  PgUp/Dn  +/-=ae  9/6=ap  5=reset",
                  style='Small.TLabel').pack(side="right")

        # Col 0: tool info
        left = ttk.LabelFrame(root, text="Outil", padding=4)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 2))

        self.lbl_tool_name = ttk.Label(left, text="---", font=("Helvetica", 15, "bold"),
                                        wraplength=200)
        self.lbl_tool_name.pack(anchor="w")
        self.lbl_tool_type = ttk.Label(left, text="", style='Cat.TLabel')
        self.lbl_tool_type.pack(anchor="w")

        info = ttk.Frame(left)
        info.pack(fill="x", pady=(4, 0))
        for i, (txt, attr) in enumerate([
            ("D:", "lbl_diam"), ("Z:", "lbl_dents"),
            ("Mat:", "lbl_matiere"), ("Rev:", "lbl_revet"),
        ]):
            ttk.Label(info, text=txt).grid(row=i, column=0, sticky="e", padx=(0, 3))
            lbl = ttk.Label(info, text="---", font=("Helvetica", 12, "bold"))
            lbl.grid(row=i, column=1, sticky="w")
            setattr(self, attr, lbl)

        self.lbl_tool_notes = ttk.Label(left, text="", style='Small.TLabel', wraplength=195)
        self.lbl_tool_notes.pack(anchor="w", pady=(3, 0))
        self.lbl_tool_counter = ttk.Label(left, text="0/0", font=("Helvetica", 11))
        self.lbl_tool_counter.pack(anchor="w", pady=(2, 0))

        mat_frame = ttk.Frame(left)
        mat_frame.pack(fill="x", pady=(6, 0))
        for label, key in (("ACIER", "acier"), ("ALU", "alu")):
            ttk.Radiobutton(mat_frame, text=label, value=key, variable=self.piece_mat,
                            command=self._on_engagement_changed,
                            style='Mat.TRadiobutton').pack(
                side="left", padx=4, expand=True, fill="x")

        # Col 1: Canvas
        canvas_frame = ttk.LabelFrame(root, text="Engagement", padding=2)
        canvas_frame.grid(row=1, column=1, sticky="nsew", padx=2)
        self.eng_canvas = tk.Canvas(canvas_frame, bg="white", highlightthickness=0)
        self.eng_canvas.pack(fill="both", expand=True)
        self.eng_canvas.bind("<Configure>", lambda e: self._draw_engagement())

        # Col 2: Cutting params
        right = ttk.LabelFrame(root, text="Coupe", padding=4)
        right.grid(row=1, column=2, sticky="nsew", padx=(2, 0))

        ttk.Label(right, text="RPM :").pack(anchor="w")
        self.lbl_rpm = ttk.Label(right, text="---", font=("Helvetica", 22, "bold"))
        self.lbl_rpm.pack(anchor="w")

        ttk.Label(right, text="Avance (mm/min) :").pack(anchor="w")
        self.lbl_feed = ttk.Label(right, text="---", font=("Helvetica", 22, "bold"))
        self.lbl_feed.pack(anchor="w")

        ttk.Separator(right).pack(fill="x", pady=3)

        ae_f = ttk.Frame(right)
        ae_f.pack(fill="x")
        ttk.Label(ae_f, text="ae:").pack(side="left")
        self.lbl_ae = ttk.Label(ae_f, text="---", font=("Helvetica", 13, "bold"))
        self.lbl_ae.pack(side="left", padx=(3, 0))
        self.lbl_ae_pct = ttk.Label(ae_f, text="", style='Small.TLabel')
        self.lbl_ae_pct.pack(side="left", padx=(3, 0))

        ap_f = ttk.Frame(right)
        ap_f.pack(fill="x")
        ttk.Label(ap_f, text="ap:").pack(side="left")
        self.lbl_ap = ttk.Label(ap_f, text="---", font=("Helvetica", 13, "bold"))
        self.lbl_ap.pack(side="left", padx=(3, 0))
        self.lbl_ap_info = ttk.Label(ap_f, text="", style='Small.TLabel')
        self.lbl_ap_info.pack(side="left", padx=(3, 0))

        q_f = ttk.Frame(right)
        q_f.pack(fill="x", pady=(2, 0))
        ttk.Label(q_f, text="Q:").pack(side="left")
        self.lbl_q = ttk.Label(q_f, text="---", font=("Helvetica", 12))
        self.lbl_q.pack(side="left", padx=(3, 0))

        ttk.Separator(right).pack(fill="x", pady=3)

        self.lbl_details = ttk.Label(right, text="", style='Small.TLabel', wraplength=210)
        self.lbl_details.pack(anchor="w")
        self.lbl_limit = ttk.Label(right, text="", style='Warn.TLabel', wraplength=210)
        self.lbl_limit.pack(anchor="w")
        self.lbl_foret_info = ttk.Label(right, text="", style='Info.TLabel', wraplength=210)
        self.lbl_foret_info.pack(anchor="w")

        # Row 2: nav
        nav_frame = ttk.Frame(root)
        nav_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        ttk.Button(nav_frame, text="<< Prec", style='Med.TButton',
                   command=lambda: self._tool_navigate(-1)).pack(
            side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(nav_frame, text="Suivant >>", style='Med.TButton',
                   command=lambda: self._tool_navigate(1)).pack(
            side="left", expand=True, fill="x", padx=(2, 0))

    # ════════════════════════════════════════════
    #  ENGAGEMENT (ae/ap)
    # ════════════════════════════════════════════
    def _get_ap_max(self, diam, piece_mat, plaquettes=False):
        """ap max de base (reference: ae ~50% du diametre)."""
        if plaquettes:
            return _interp_table(AP_MAX_PLAQ, diam)
        if piece_mat == "alu":
            return _interp_table(AP_MAX_ALU, diam)
        return _interp_table(AP_MAX_ACIER, diam)

    def _get_effective_ap_max(self, diam, piece_mat, plaquettes, ae_ratio):
        """ap max ajuste selon ae: faible ae = on peut monter en ap (contournage).
        Reference = 50% ae. Cap a 3x le max de base."""
        base = self._get_ap_max(diam, piece_mat, plaquettes)
        if ae_ratio < 0.5:
            factor = min(3.0, 0.5 / max(ae_ratio, 0.05))
        else:
            factor = 1.0
        return base * factor

    def _calc_default_engagement(self):
        """Meilleure config par defaut pour l'outil/machine."""
        if not self.filtered_tools:
            return
        tool = self.filtered_tools[self.current_index]
        diam = tool.get("diametre", 10.0)
        piece_mat = self.piece_mat.get()
        plaq = tool.get("a_plaquettes", False)
        is_foret = tool.get("type", "") in CATEGORY_FORETS

        if is_foret:
            self.ae_mm.set(diam)
            self.ap_mm.set(0)
            return

        # ae par defaut: 50% acier, 60% alu, arrondi a 1mm, min 1mm
        ratio = 0.60 if piece_mat == "alu" else 0.50
        ae = max(1.0, round(diam * ratio))
        self.ae_mm.set(min(diam, ae))

        # ap par defaut: 70% du max table
        ap_max = self._get_ap_max(diam, piece_mat, plaq)
        ap = round(ap_max * 0.7 * 4) / 4  # arrondi 0.25mm
        self.ap_mm.set(max(0.25, ap))

    def _reset_engagement(self):
        """Touche 5: 75% ae + ap recommande."""
        if self.notebook.index(self.notebook.select()) != self.notebook.index(self.tab_outil):
            return
        if not self.filtered_tools:
            return
        tool = self.filtered_tools[self.current_index]
        if tool.get("type", "") in CATEGORY_FORETS:
            return
        diam = tool.get("diametre", 10.0)
        piece_mat = self.piece_mat.get()
        plaq = tool.get("a_plaquettes", False)

        ae = max(1.0, round(diam * 0.75))
        self.ae_mm.set(min(diam, ae))

        ap_max = self._get_ap_max(diam, piece_mat, plaq)
        ap = round(ap_max * 0.8 * 4) / 4
        self.ap_mm.set(max(0.25, ap))
        self._on_engagement_changed()

    def _adjust_ae(self, direction):
        """+/- : ae par pas de ~10% du diametre, arrondi 1mm."""
        if self.notebook.index(self.notebook.select()) != self.notebook.index(self.tab_outil):
            return
        if not self.filtered_tools:
            return
        tool = self.filtered_tools[self.current_index]
        if tool.get("type", "") in CATEGORY_FORETS:
            return
        diam = tool.get("diametre", 10.0)
        step = max(1.0, round(diam * 0.1))
        new_ae = self.ae_mm.get() + step * direction
        new_ae = max(1.0, min(diam, round(new_ae)))
        self.ae_mm.set(new_ae)
        self._on_engagement_changed()

    def _adjust_ap(self, direction):
        """9=augmenter ap, 6=diminuer. Pas de 0.25mm."""
        if self.notebook.index(self.notebook.select()) != self.notebook.index(self.tab_outil):
            return
        if not self.filtered_tools:
            return
        tool = self.filtered_tools[self.current_index]
        if tool.get("type", "") in CATEGORY_FORETS:
            return
        diam = tool.get("diametre", 10.0)
        piece_mat = self.piece_mat.get()
        plaq = tool.get("a_plaquettes", False)
        ae_ratio = self.ae_mm.get() / max(diam, 0.1)
        ap_max_eff = self._get_effective_ap_max(diam, piece_mat, plaq, ae_ratio)

        new_ap = self.ap_mm.get() + 0.25 * direction
        new_ap = round(new_ap * 4) / 4
        new_ap = max(0.25, min(ap_max_eff * 1.5, new_ap))
        self.ap_mm.set(new_ap)
        self._on_engagement_changed()

    def _on_engagement_changed(self):
        self._update_tool_display(keep_engagement=True)

    def _recalc_with_engagement(self, tool, piece_mat):
        """Recalcule Vf effectif avec correction ae + ap, retourne params enrichis."""
        params = calc_cutting_params(tool, piece_mat)
        diam = params["diam"]
        if diam <= 0:
            return params
        ae = self.ae_mm.get()
        ap = self.ap_mm.get()
        ae_ratio = ae / diam
        plaq = tool.get("a_plaquettes", False)
        ap_max_base = self._get_ap_max(diam, piece_mat, plaq)
        ap_max_eff = self._get_effective_ap_max(diam, piece_mat, plaq, ae_ratio)
        ap_ratio = ap / max(ap_max_eff, 0.01)

        corr = _fz_correction(ae_ratio, ap_ratio)
        fz_eff = params["fz"] * corr
        feed_raw = fz_eff * params["nb_dents"] * params["rpm"]
        feed_capped = feed_raw > VF_MAX_STEPPER
        feed_eff = min(feed_raw, VF_MAX_STEPPER) if feed_capped else feed_raw
        q = ap * ae * feed_eff / 1000  # cm3/min

        params.update({
            "fz_eff": round(fz_eff, 4), "feed_eff": round(feed_eff, 1),
            "feed_raw": round(feed_raw, 1), "feed_capped": feed_capped,
            "ae": ae, "ap": ap, "ae_ratio": ae_ratio, "ap_ratio": ap_ratio,
            "ap_max_eff": round(ap_max_eff, 2), "ap_max_base": round(ap_max_base, 2),
            "q": round(q, 2), "correction": corr,
        })
        return params

    def _check_conditions(self, params, tool, piece_mat):
        """Retourne (status, message). status = 'ok'|'warning'|'block'."""
        diam = params.get("diam", 0)
        if diam <= 0:
            return "ok", ""
        ap = params.get("ap", 0)
        ae_ratio = params.get("ae_ratio", 0.5)
        q = params.get("q", 0)
        ap_max_eff = params.get("ap_max_eff", 2.0)

        msgs = []
        blocked = False

        if ap > ap_max_eff * 1.3:
            msgs.append(f"ap={ap:.2f} DEPASSE ({ap_max_eff:.1f}mm max)")
            blocked = True
        elif ap > ap_max_eff:
            msgs.append(f"ap eleve ({ap:.2f} > {ap_max_eff:.1f}mm)")

        if ae_ratio > 0.85 and ap > ap_max_eff * 0.6:
            msgs.append("Rainurage + ap eleve")
            if ap > ap_max_eff * 0.8:
                blocked = True

        if piece_mat == "acier":
            if q > 3.0:
                msgs.append(f"Q={q:.1f} cm3/min EXCESSIF")
                blocked = True
            elif q > 1.5:
                msgs.append(f"Q={q:.1f} cm3/min (limite)")
        else:
            if q > 15.0:
                msgs.append(f"Q={q:.1f} cm3/min EXCESSIF")
                blocked = True
            elif q > 8.0:
                msgs.append(f"Q={q:.1f} cm3/min (limite)")

        if blocked:
            return "block", " | ".join(msgs)
        elif msgs:
            return "warning", " | ".join(msgs)
        return "ok", ""

    def _draw_engagement(self):
        c = self.eng_canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 20 or ch < 20:
            return

        if not self.filtered_tools:
            return
        tool = self.filtered_tools[self.current_index]

        if tool.get("type", "") in CATEGORY_FORETS:
            c.create_text(cw // 2, ch // 2, text="Foret\n(pas d'engagement ae/ap)",
                          font=("Helvetica", 13), justify="center", fill="#888")
            return

        diam = tool.get("diametre", 10.0)
        ae = self.ae_mm.get()
        ap = self.ap_mm.get()
        if diam <= 0:
            return

        margin = 15
        mid_y = ch // 2

        # ── VUE DE DESSUS (moitie haute) ──
        tv_h = mid_y - margin - 5
        tv_w = cw - 2 * margin
        # Echelle : le diametre doit tenir dans la vue
        sc = min(tv_w / (diam * 2.2), tv_h / (diam * 1.3))

        # Piece (rectangle gris, cote gauche)
        wp_w = diam * 1.4 * sc
        wp_h = diam * 1.0 * sc
        wp_x = margin + 5
        wp_cy = margin + tv_h / 2
        wp_y1 = wp_cy - wp_h / 2
        wp_y2 = wp_cy + wp_h / 2
        c.create_rectangle(wp_x, wp_y1, wp_x + wp_w, wp_y2,
                           fill="#D0D0D0", outline="#999")

        # Outil (cercle bleu, chevauche la piece de ae)
        r_px = diam / 2 * sc
        ae_px = ae * sc
        # Centre du cercle: le bord gauche du cercle est a (wp_x+wp_w - ae_px)
        tool_cx = wp_x + wp_w - ae_px + r_px
        tool_cy = wp_cy
        c.create_oval(tool_cx - r_px, tool_cy - r_px,
                      tool_cx + r_px, tool_cy + r_px,
                      outline="#2196F3", width=2)

        # Zone de coupe (orange) = intersection approchee
        ol = wp_x + wp_w - ae_px
        orr = wp_x + wp_w
        ot = max(wp_y1, tool_cy - r_px)
        ob = min(wp_y2, tool_cy + r_px)
        if orr > ol and ob > ot:
            c.create_rectangle(ol, ot, orr, ob, fill="#FF8C00", outline="")

        # Cote ae
        ay = tool_cy + r_px + 10
        c.create_line(ol, ay, orr, ay, fill="black")
        c.create_line(ol, ay - 3, ol, ay + 3, fill="black")
        c.create_line(orr, ay - 3, orr, ay + 3, fill="black")
        pct = ae / diam * 100
        c.create_text((ol + orr) / 2, ay + 10,
                      text=f"ae={ae:.0f}mm ({pct:.0f}%)", font=("Helvetica", 9))

        # Fleche feed
        fy = wp_y1 - 6
        c.create_line(wp_x + 5, fy, wp_x + wp_w - 5, fy,
                      fill="#666", arrow="last")
        c.create_text(wp_x + wp_w / 2, fy - 8, text="feed",
                      font=("Helvetica", 8), fill="#666")

        c.create_text(margin, margin - 2, text="Vue dessus", anchor="nw",
                      font=("Helvetica", 8, "italic"), fill="#AAA")

        # ── Separateur ──
        c.create_line(margin, mid_y, cw - margin, mid_y, fill="#CCC", dash=(4, 4))

        # ── VUE DE COTE (moitie basse) ──
        sv_top = mid_y + 8
        sv_h = ch - sv_top - margin
        # Echelle: ap doit etre visible (min 20px)
        ap_max_vis = max(ap, diam * 0.3)
        sc2 = min(tv_w / (diam * 2), sv_h / (ap_max_vis * 2.5))
        sc2 = min(sc2, sc)  # coherent avec vue dessus

        surface_y = sv_top + 25
        ap_px = max(8, ap * sc2)  # min 8px pour visibilite
        tool_w_px = diam * sc2

        # Surface piece
        c.create_line(margin + 5, surface_y, cw - margin - 5, surface_y,
                      fill="#666", width=2)

        # Piece sous la surface
        c.create_rectangle(margin + 5, surface_y, cw - margin - 5,
                           surface_y + sv_h - 15, fill="#D0D0D0", outline="#999")

        # Zone coupee (orange)
        cut_x = cw / 2 - tool_w_px / 2
        c.create_rectangle(cut_x, surface_y, cut_x + tool_w_px,
                           surface_y + ap_px, fill="#FF8C00", outline="#CC6600", width=2)

        # Profil outil (bleu pointille au-dessus)
        c.create_rectangle(cut_x, surface_y - 20, cut_x + tool_w_px,
                           surface_y + ap_px, outline="#2196F3", width=2, dash=(3, 3))

        # Cote ap
        ax = cut_x + tool_w_px + 8
        c.create_line(ax, surface_y, ax, surface_y + ap_px, fill="black")
        c.create_line(ax - 3, surface_y, ax + 3, surface_y, fill="black")
        c.create_line(ax - 3, surface_y + ap_px, ax + 3, surface_y + ap_px, fill="black")
        c.create_text(ax + 5, surface_y + ap_px / 2,
                      text=f"ap={ap:.2f}mm", font=("Helvetica", 9), anchor="w")

        c.create_text(margin, sv_top - 2, text="Vue cote", anchor="nw",
                      font=("Helvetica", 8, "italic"), fill="#AAA")

    # ── Position tab ──
    def _build_position_tab(self, tab):
        root = ttk.Frame(tab, padding=6)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # ── Haut : position X + jog + retour butées ──
        top = ttk.LabelFrame(root, text="Position X", padding=6)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        top.columnconfigure(0, weight=1)

        # Position display + ZERO sur la même ligne
        pos_line = ttk.Frame(top)
        pos_line.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(pos_line, text="X =", font=("Helvetica", 18, "bold")).pack(side="left")
        self.lbl_pos_x = ttk.Label(pos_line, textvariable=self.pos_x_str,
                                    font=("Helvetica", 32, "bold"))
        self.lbl_pos_x.pack(side="left", padx=(6, 10))
        ttk.Label(pos_line, text="mm", font=("Helvetica", 18)).pack(side="left")
        ttk.Button(pos_line, text="ZERO X", style='Med.TButton',
                   command=self._zero_x).pack(side="right", padx=(10, 0))
        self.lbl_limit_status = ttk.Label(pos_line, text="", style='Info.TLabel')
        self.lbl_limit_status.pack(side="right", padx=(10, 0))

        # Jog buttons + retour butées sur 2 lignes
        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=1, column=0, sticky="ew")
        jog_values = [("<<G", "left_lim"), ("-10", -10), ("-1", -1), ("-0.1", -0.1),
                      ("+0.1", 0.1), ("+1", 1), ("+10", 10), ("D>>", "right_lim")]
        for c, (txt, val) in enumerate(jog_values):
            if val == "left_lim":
                cmd = lambda: self._goto_limit("left")
            elif val == "right_lim":
                cmd = lambda: self._goto_limit("right")
            else:
                cmd = lambda d=val: self._jog_x(d)
            ttk.Button(btn_frame, text=txt, style='Jog.TButton',
                       command=cmd).grid(row=0, column=c, sticky="nsew", padx=2, pady=2, ipady=2)
            btn_frame.columnconfigure(c, weight=1)

        # ── Bas : butées logicielles ──
        bot = ttk.LabelFrame(root, text="Butees logicielles X", padding=6)
        bot.grid(row=1, column=0, sticky="nsew", pady=(0, 4))
        root.rowconfigure(1, weight=1)
        bot.columnconfigure(0, weight=1)
        bot.columnconfigure(1, weight=0)
        bot.columnconfigure(2, weight=1)

        # Checkbox activer
        ttk.Checkbutton(bot, text="Activer les butees",
                        variable=self.limit_x_enabled,
                        command=self._update_limit_display).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        # Gauche | séparateur | Droite
        # Gauche
        lf = ttk.Frame(bot)
        lf.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(lf, text="GAUCHE", font=("Helvetica", 13, "bold")).pack(anchor="w")
        self.lbl_limit_left = ttk.Label(lf, text="Non definie",
                                         font=("Helvetica", 18, "bold"))
        self.lbl_limit_left.pack(anchor="w", pady=(2, 4))
        btn_lf = ttk.Frame(lf)
        btn_lf.pack(fill="x")
        ttk.Button(btn_lf, text="Poser ICI", style='Med.TButton',
                   command=self._set_limit_left).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._num_entry(lf, self.limit_x_left, width=7, decimals=1)
        lf.winfo_children()[-1].pack(fill="x", pady=(4, 0))

        # Séparateur vertical
        ttk.Separator(bot, orient="vertical").grid(row=1, column=1, sticky="ns", padx=8)

        # Droite
        rf = ttk.Frame(bot)
        rf.grid(row=1, column=2, sticky="nsew", padx=(4, 0))
        ttk.Label(rf, text="DROITE", font=("Helvetica", 13, "bold")).pack(anchor="w")
        self.lbl_limit_right = ttk.Label(rf, text="Non definie",
                                          font=("Helvetica", 18, "bold"))
        self.lbl_limit_right.pack(anchor="w", pady=(2, 4))
        btn_rf = ttk.Frame(rf)
        btn_rf.pack(fill="x")
        ttk.Button(btn_rf, text="Poser ICI", style='Med.TButton',
                   command=self._set_limit_right).pack(side="left", expand=True, fill="x", padx=(0, 2))
        self._num_entry(rf, self.limit_x_right, width=7, decimals=1)
        rf.winfo_children()[-1].pack(fill="x", pady=(4, 0))

        # Effacer
        ttk.Button(bot, text="Effacer les butees", style='Med.TButton',
                   command=self._clear_limits).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0), ipady=2)

        # Info
        ttk.Label(root, text="Butees = protection software, pas un remplacement de fin de course",
                  style='Small.TLabel').grid(row=2, column=0, sticky="w", pady=(2, 0))

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
        """Déplacement X avec respect des butées. Envoie la commande au MotorController."""
        if self.io_active:
            return
        clamped = self._clamp_move_x(delta_mm)
        if abs(clamped) < 1e-6:
            self.lbl_limit_status.config(text="BUTEE ATTEINTE !", style='Warn.TLabel')
            self.after(1500, self._update_limit_display)
            return
        # Avance = depuis l'outil sélectionné, ou rapide si foret/pas d'outil
        feed = self._current_feed_x()
        self.motor.send(CmdType.MOVE_X, clamped, feed)

    def _goto_limit(self, side):
        """Retour rapide vers la butée gauche ou droite via MotorController."""
        if self.io_active:
            return
        if side == "left":
            if not self.limit_x_left_set:
                self.lbl_limit_status.config(text="Butee gauche non definie", style='Warn.TLabel')
                self.after(1500, self._update_limit_display)
                return
            target = self.limit_x_left.get()
        else:
            if not self.limit_x_right_set:
                self.lbl_limit_status.config(text="Butee droite non definie", style='Warn.TLabel')
                self.after(1500, self._update_limit_display)
                return
            target = self.limit_x_right.get()

        delta = target - self.pos_x_mm.get()
        if abs(delta) < 1e-6:
            return
        rapid = self.rapid_x_speed.get()
        self.motor.send(CmdType.MOVE_X, delta, rapid)

    def _current_feed_x(self):
        """Retourne l'avance X courante : feed outil ou rapide si foret/pas d'outil."""
        if not self.filtered_tools:
            return self.rapid_x_speed.get()
        tool = self.filtered_tools[self.current_index]
        if tool.get("type", "") in CATEGORY_FORETS:
            return self.rapid_x_speed.get()
        params = self._recalc_with_engagement(tool, self.piece_mat.get())
        return params.get("feed_eff", self.rapid_x_speed.get())

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

    def _update_tool_display(self, keep_engagement=False):
        if not self.filtered_tools:
            self.lbl_tool_name.config(text="Aucun outil")
            self.lbl_tool_type.config(text="")
            self.lbl_diam.config(text="---")
            self.lbl_dents.config(text="---")
            self.lbl_matiere.config(text="---")
            self.lbl_revet.config(text="---")
            self.lbl_tool_notes.config(text="")
            self.lbl_tool_counter.config(text="0/0")
            self.lbl_rpm.config(text="---")
            self.lbl_feed.config(text="---")
            self.lbl_ae.config(text="---"); self.lbl_ae_pct.config(text="")
            self.lbl_ap.config(text="---"); self.lbl_ap_info.config(text="")
            self.lbl_q.config(text="---")
            self.lbl_details.config(text="")
            self.lbl_limit.config(text="")
            self.lbl_foret_info.config(text="")
            self._draw_engagement()
            return

        tool = self.filtered_tools[self.current_index]
        tool_id = id(tool)
        nom = tool.get("nom", "Sans nom") or "Sans nom"
        tool_type = tool.get("type", "")
        diam = tool.get("diametre", 0)
        nb_dents = tool.get("nb_dents", 0)
        matiere = tool.get("matiere", "")
        revet = tool.get("revetement", "Aucun")
        notes = tool.get("notes", "")
        is_foret = tool_type in CATEGORY_FORETS
        piece = self.piece_mat.get()

        # Info outil
        self.lbl_tool_name.config(text=nom)
        self.lbl_tool_type.config(text=tool_type)
        self.lbl_diam.config(text=f"{diam} mm")
        self.lbl_dents.config(text=str(nb_dents))
        self.lbl_matiere.config(text=matiere)
        self.lbl_revet.config(text=revet)
        self.lbl_tool_notes.config(text=notes if notes else "")
        self.lbl_tool_counter.config(
            text=f"{self.current_index + 1} / {len(self.filtered_tools)}")

        # Recalculer engagement par defaut si outil a change
        if not keep_engagement or self._last_tool_id != tool_id:
            self._last_tool_id = tool_id
            self._calc_default_engagement()

        if is_foret:
            params = calc_cutting_params(tool, piece)
            self.lbl_rpm.config(text=f"{params['rpm']:,}".replace(",", " "))
            self.lbl_feed.config(text=f"{params['feed']:,.0f}".replace(",", " "))
            self.lbl_ae.config(text="---"); self.lbl_ae_pct.config(text="")
            self.lbl_ap.config(text="---"); self.lbl_ap_info.config(text="")
            self.lbl_q.config(text="---")
            vc_r = params.get("vc_reelle", 0)
            vc_c = params.get("vc", 0)
            if vc_r and vc_c and vc_r > vc_c * 1.2:
                vc_txt = f"Vc={vc_r:.0f} (cible {vc_c})"
            else:
                vc_txt = f"Vc={vc_c}"
            self.lbl_details.config(
                text=f"D={diam} | {vc_txt} m/min | fz={params['fz']:.3f}")
            self.lbl_foret_info.config(text=f"Foret: avance Z = {params['feed']:.0f} mm/min")
            # Warnings forets
            foret_warns = []
            if params["limited"]:
                foret_warns.append(f"RPM bride ({DEFAULT_SPINDLE_MAX_RPM})")
            elif params["boosted"]:
                foret_warns.append(f"RPM min ({DEFAULT_SPINDLE_MIN_RPM})")
            if vc_r and vc_c and vc_r > vc_c * 1.2:
                foret_warns.append(f"Vc reelle {vc_r:.0f}>{vc_c}")
            self.lbl_limit.config(
                text=" | ".join(foret_warns) if foret_warns else "",
                style='Warn.TLabel' if foret_warns else 'TLabel')
            self._draw_engagement()
            return

        # Fraise: recalcul avec engagement
        params = self._recalc_with_engagement(tool, piece)
        status, msg = self._check_conditions(params, tool, piece)

        self.lbl_rpm.config(text=f"{params['rpm']:,}".replace(",", " "))

        if status == "block":
            self.lbl_feed.config(text="BLOQUE")
        else:
            self.lbl_feed.config(text=f"{params['feed_eff']:,.0f}".replace(",", " "))

        # ae / ap display
        ae_pct = params["ae_ratio"] * 100
        self.lbl_ae.config(text=f"{params['ae']:.0f} mm")
        self.lbl_ae_pct.config(text=f"({ae_pct:.0f}% D)")

        self.lbl_ap.config(text=f"{params['ap']:.2f} mm")
        self.lbl_ap_info.config(text=f"(max {params['ap_max_eff']:.1f})")

        self.lbl_q.config(text=f"{params['q']:.1f} cm3/min")

        corr = params.get("correction", 1.0)
        corr_txt = f" fz x{corr:.2f}" if abs(corr - 1.0) > 0.01 else ""
        vc_reelle = params.get("vc_reelle", 0)
        vc_cible = params.get("vc", 0)
        vc_txt = f"Vc={vc_cible}"
        if vc_reelle and vc_cible and vc_reelle > vc_cible * 1.2:
            vc_txt = f"Vc={vc_reelle:.0f} (cible {vc_cible})"
        self.lbl_details.config(
            text=f"{vc_txt} | fz={params['fz']:.3f}{corr_txt}")
        self.lbl_foret_info.config(text="")

        # Warnings / limits
        limit_parts = []
        if params["limited"]:
            limit_parts.append(f"RPM bride ({DEFAULT_SPINDLE_MAX_RPM})")
        elif params["boosted"]:
            limit_parts.append(f"RPM min ({DEFAULT_SPINDLE_MIN_RPM})")

        # Warning Vc réelle trop haute (gros outils, RPM plancher)
        if vc_reelle and vc_cible and vc_reelle > vc_cible * 1.2:
            limit_parts.append(f"Vc reelle {vc_reelle:.0f}>{vc_cible}")

        # Warning Vf cappé par vitesse stepper
        if params.get("feed_capped"):
            limit_parts.append(f"Vf cappe {VF_MAX_STEPPER} (theo {params['feed_raw']:.0f})")

        if status == "block":
            limit_parts.append(f"STOP: {msg}")
            self.lbl_limit.config(text=" | ".join(limit_parts), style='Warn.TLabel')
        elif status == "warning" or params.get("feed_capped") or (vc_reelle and vc_cible and vc_reelle > vc_cible * 1.2):
            if msg:
                limit_parts.append(msg)
            self.lbl_limit.config(text=" | ".join(limit_parts), style='Warn.TLabel')
        else:
            self.lbl_limit.config(text=" | ".join(limit_parts) if limit_parts else "")

        self._draw_engagement()

    # ════════════════════════════════════════════
    #  Z HOLD-TO-MOVE (simulation)
    # ════════════════════════════════════════════
    def _z_hold(self, up):
        if self.io_active:
            return
        direction = 1 if up else -1
        speed = max(self.z_vmin.get(), min(self.z_vmax.get(), self.z_vmax.get()))
        self.motor.send(CmdType.MOVE_Z_START, direction, speed)

    def _z_release(self, up):
        self.motor.send(CmdType.MOVE_Z_STOP)

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
    # ════════════════════════════════════════════
    #  MOTOR CONTROLLER
    # ════════════════════════════════════════════
    def _init_motor(self):
        """Crée et démarre le MotorController."""
        motor_cfg = {
            'steps_rev_x': self.steps_rev_x.get(),
            'microstep_x': self.microstep_x.get(),
            'lead_x': self.lead_x.get(),
            'steps_rev_z': self.steps_rev_z.get(),
            'microstep_z': self.microstep_z.get(),
            'lead_z': self.lead_z.get(),
            'accel_x': self.accel_x.get(),
            'decel_x': self.decel_x.get(),
            'rapid_x_speed': self.rapid_x_speed.get(),
            'z_vmin': self.z_vmin.get(),
            'z_vmax': self.z_vmax.get(),
            'z_accel': self.z_accel.get(),
            'pos_x_steps': self.pos_x_steps,
        }
        self.motor, self.cmd_queue, self.status_queue = create_motor_system(motor_cfg)
        self.motor.start()
        self._poll_motor_status()

    def _poll_motor_status(self):
        """Polling status_queue depuis le main thread (Tkinter-safe)."""
        try:
            while True:
                st = self.status_queue.get_nowait()
                if st.type == StatusType.POSITION:
                    axis, steps = st.args[0], st.args[1]
                    if axis == 'x':
                        self.pos_x_steps = steps
                        self._update_pos_x_from_steps()
                    elif axis == 'z':
                        self.pos_z_mm.set(round(steps / self.motor.steps_per_mm_z, 1))
                elif st.type == StatusType.MOVE_DONE:
                    axis, steps_done = st.args[0], st.args[1]
                    if axis == 'x':
                        self.pos_x_steps = self.motor.pos_x_steps
                        self._update_pos_x_from_steps()
                        self._update_limit_display()
                elif st.type == StatusType.STOPPED:
                    axis, reason = st.args[0], st.args[1]
                    if reason == 'e-stop':
                        self.lbl_limit_status.config(text="E-STOP !", style='Warn.TLabel')
                elif st.type == StatusType.ERROR:
                    print(f"[Motor Error] {st.args[0]}")
        except Exception:
            pass  # queue vide
        self.after(50, self._poll_motor_status)

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

    def destroy(self):
        # Arrêter le MotorController proprement
        if hasattr(self, 'motor') and self.motor.is_alive():
            self.motor.send(CmdType.SHUTDOWN)
            self.motor.join(timeout=2)
        super().destroy()


# ====== Entrée ======
if __name__ == "__main__":
    try:
        app = App()
        app.mainloop()
    except KeyboardInterrupt:
        pass
