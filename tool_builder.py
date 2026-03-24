#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool Builder GUI — Création / édition de tool_list.json
pour la fraiseuse VM32L.
"""

import json, os, copy, math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, filedialog
import customtkinter as ctk

# ── Apparence ──────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

TOOL_FILE = Path(__file__).parent / "tool_list.json"

# ── Constantes de choix ────────────────────────────────────
TOOL_TYPES = [
    "Fraise 2 tailles",
    "Fraise 3 tailles",
    "Fraise à surfacer",
    "Fraise à chanfreiner",
    "Foret",
    "Foret à centrer"
]

TOOL_MATERIALS = ["HSS", "HSS-Co", "Carbure", "Carbure revêtu"]

COATINGS = ["Aucun", "TiN", "TiAlN", "AlCrN", "DLC", "ZrN", "Autre"]

ANGLES = ["90°", "45°", "60°", "30°", "Autre"]


# ── Tables fz max (mm/dent) — FRAISES — interpolation linéaire ──
# Format: [(diametre_mm, fz_max), ...]
FZ_FRAISE = {
    ("HSS", "acier"): [(4, 0.02), (6, 0.03), (8, 0.04), (10, 0.05),
                       (12, 0.06), (16, 0.07), (20, 0.08)],
    ("Carbure", "acier"): [(4, 0.04), (6, 0.05), (8, 0.06), (10, 0.08),
                           (12, 0.10), (16, 0.12), (20, 0.15)],
    ("HSS", "alu"): [(6, 0.06), (8, 0.08), (10, 0.10),
                     (12, 0.12), (16, 0.15), (20, 0.18)],
    ("Carbure", "alu"): [(6, 0.08), (8, 0.10), (10, 0.13),
                         (12, 0.15), (16, 0.20), (20, 0.25)],
    ("plaquettes", "acier"): [(10, 0.15), (100, 0.15)],
    ("plaquettes", "alu"): [(10, 0.25), (100, 0.25)],
}

# ── Tables fz — FORETS — valeurs = f_per_rev / 2 (2 arêtes) ──
# Source: abaques forets HSS standard
FZ_FORET = {
    ("HSS", "acier"): [(3, 0.025), (5, 0.045), (8, 0.075), (10, 0.09),
                       (12, 0.11), (13, 0.12), (16, 0.14), (20, 0.16)],
    ("Carbure", "acier"): [(3, 0.04), (5, 0.06), (8, 0.10), (10, 0.12),
                           (12, 0.14), (13, 0.15), (16, 0.18), (20, 0.20)],
    ("HSS", "alu"): [(3, 0.04), (5, 0.07), (8, 0.10), (10, 0.13),
                     (12, 0.15), (13, 0.16), (16, 0.18), (20, 0.20)],
    ("Carbure", "alu"): [(3, 0.06), (5, 0.09), (8, 0.14), (10, 0.16),
                         (12, 0.19), (13, 0.20), (16, 0.22), (20, 0.25)],
}

# ── Vc recommandées (m/min) ──
VC_FRAISE = {
    ("HSS", "acier"): 25,
    ("Carbure", "acier"): 80,
    ("HSS", "alu"): 200,
    ("Carbure", "alu"): 400,
}

# Forets : Vc plus basses (arêtes enfermées dans le trou, pas d'évacuation libre)
VC_FORET = {
    ("HSS", "acier"): 25,
    ("Carbure", "acier"): 60,
    ("HSS", "alu"): 60,
    ("Carbure", "alu"): 150,
}

SPINDLE_MAX_RPM = 3000
SPINDLE_MIN_RPM = 500

# Vf max réaliste VM32L (mm/min) — au-delà, steppers perdent du couple / bâti vibre
VF_MAX_VM32L = 800

# Facteur conservateur forets alu (débourrage, rigidité limitée)
DRILL_ALU_VF_FACTOR = 0.55


def _interp_fz(table: list[tuple], diam: float) -> float:
    """Interpolation linéaire dans une table (Ø, fz)."""
    if not table:
        return 0.0
    if diam <= table[0][0]:
        return table[0][1]
    if diam >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        d0, f0 = table[i]
        d1, f1 = table[i + 1]
        if d0 <= diam <= d1:
            t = (diam - d0) / (d1 - d0)
            return round(f0 + t * (f1 - f0), 4)
    return table[-1][1]


def calc_fz_and_rpm(matiere: str, diametre: float,
                    a_plaquettes: bool = False, angle_kr: float = 90.0,
                    tool_type: str = "") -> dict:
    """Calcule fz_max et RPM pour acier et alu.

    Sélectionne les tables foret vs fraise selon tool_type.
    Si a_plaquettes=True, utilise les tables fz plaquettes et applique
    le facteur d'amincissement du copeau selon l'angle κr.
    """
    is_drill = tool_type in ("Foret", "Foret à centrer")
    mat_key = "Carbure" if "Carbure" in matiere or "Cermet" in matiere else "HSS"

    # Facteur d'amincissement copeau (chip thinning) — fraises à plaquettes
    if a_plaquettes and 0 < angle_kr < 90:
        sin_kr = math.sin(math.radians(angle_kr))
        chip_thin = 1.0 / sin_kr
    else:
        chip_thin = 1.0

    # Sélection des tables selon type d'outil
    if a_plaquettes:
        fz_tables = FZ_FRAISE
        vc_tables = VC_FRAISE
    elif is_drill:
        fz_tables = FZ_FORET
        vc_tables = VC_FORET
    else:
        fz_tables = FZ_FRAISE
        vc_tables = VC_FRAISE

    result = {}
    for piece_mat in ("acier", "alu"):
        if a_plaquettes:
            key_fz = ("plaquettes", piece_mat)
            key_vc = ("Carbure", piece_mat)
        else:
            key_fz = (mat_key, piece_mat)
            key_vc = (mat_key, piece_mat)

        table = fz_tables.get(key_fz, fz_tables.get(("HSS", piece_mat), []))
        fz_base = _interp_fz(table, diametre)
        fz_eff = round(fz_base * chip_thin, 4)

        vc = vc_tables.get(key_vc, 25)
        rpm = int((vc * 1000) / (math.pi * diametre)) if diametre > 0 else 0
        rpm_clamp = max(SPINDLE_MIN_RPM, min(SPINDLE_MAX_RPM, rpm))
        vc_reelle = round(math.pi * diametre * rpm_clamp / 1000, 1) if diametre > 0 else 0
        result[piece_mat] = {
            "fz_base": fz_base,
            "fz_max": fz_eff,
            "chip_thin": round(chip_thin, 3),
            "vc": vc,
            "vc_reelle": vc_reelle,
            "rpm_ideal": rpm,
            "rpm_vm32l": rpm_clamp,
        }
    return result


# ── Template outil vide ────────────────────────────────────
EMPTY_TOOL = {
    "nom": "",
    "type": TOOL_TYPES[0],
    "matiere": TOOL_MATERIALS[0],
    "revetement": COATINGS[0],
    "diametre": 10.0,
    "nb_dents": 2,
    "a_plaquettes": False,
    "angle": ANGLES[0],
    "notes": "",
}


# ══════════════════════════════════════════════════════════
#  Classe principale
# ══════════════════════════════════════════════════════════
class ToolBuilderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("🔧 Tool Builder — Fraiseuse VM32L")
        self.geometry("1300x720")
        self.minsize(1050, 600)

        self.tools: list[dict] = []
        self.selected_idx: int | None = None
        self.modified = False
        self._loading_form = False
        self._redraw_pending = None

        self._build_ui()
        self._load_file(TOOL_FILE)
        self.preview_canvas.bind("<Configure>", lambda e: self._schedule_redraw())

    # ── Construction UI ────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ─── Panneau gauche : liste outils ─────────────────
        left = ctk.CTkFrame(self, width=280, corner_radius=0)
        left.grid(row=0, column=0, sticky="nswe")
        left.grid_rowconfigure(1, weight=1)

        # Titre + boutons
        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        ctk.CTkLabel(hdr, text="Outils", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="＋", width=36, command=self._add_tool).pack(side="right", padx=(4, 0))
        ctk.CTkButton(hdr, text="🗑", width=36, fg_color="#c0392b", hover_color="#e74c3c",
                       command=self._delete_tool).pack(side="right")

        # Liste scrollable
        self.tool_list_frame = ctk.CTkScrollableFrame(left)
        self.tool_list_frame.grid(row=1, column=0, sticky="nswe", padx=8, pady=8)

        # Boutons bas
        bot = ctk.CTkFrame(left, fg_color="transparent")
        bot.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkButton(bot, text="💾 Sauvegarder", command=self._save_file).pack(fill="x", pady=(0, 4))
        ctk.CTkButton(bot, text="📂 Ouvrir…", fg_color="#2c3e50", hover_color="#34495e",
                       command=self._open_file).pack(fill="x", pady=(0, 4))
        ctk.CTkButton(bot, text="📄 Nouveau", fg_color="#2c3e50", hover_color="#34495e",
                       command=self._new_file).pack(fill="x")

        # ─── Panneau central : formulaire ─────────────────
        self.grid_columnconfigure(2, weight=0)

        self.right = ctk.CTkScrollableFrame(self, corner_radius=0)
        self.right.grid(row=0, column=1, sticky="nswe")
        self.right.grid_columnconfigure(1, weight=1)

        self._build_form()

        # ─── Panneau droit : preview outil ─────────────────
        preview_frame = ctk.CTkFrame(self, width=300, corner_radius=0)
        preview_frame.grid(row=0, column=2, sticky="nswe")
        preview_frame.grid_rowconfigure(1, weight=1)
        preview_frame.grid_propagate(False)

        ctk.CTkLabel(preview_frame, text="Aperçu",
                      font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        self.preview_canvas = tk.Canvas(preview_frame, bg="#1a1a2e",
                                         highlightthickness=0)
        self.preview_canvas.grid(row=1, column=0, sticky="nswe", padx=8, pady=(0, 8))
        preview_frame.grid_columnconfigure(0, weight=1)

        # Légende sous le canvas
        self.preview_label = ctk.CTkLabel(preview_frame, text="",
                                           font=ctk.CTkFont(size=12),
                                           text_color="#888888")
        self.preview_label.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))

    def _build_form(self):
        f = self.right
        row = 0

        # Titre section
        def section(text, r):
            lbl = ctk.CTkLabel(f, text=text, font=ctk.CTkFont(size=15, weight="bold"),
                                text_color="#3498db")
            lbl.grid(row=r, column=0, columnspan=3, sticky="w", padx=12, pady=(16, 4))
            sep = ctk.CTkFrame(f, height=2, fg_color="#3498db")
            sep.grid(row=r + 1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 8))
            return r + 2

        # ── Section Identification ──
        row = section("Identification", row)

        row = self._add_entry(f, row, "Nom / Référence", "nom")
        row = self._add_combo(f, row, "Type d'outil", "type", TOOL_TYPES)
        row = self._add_combo(f, row, "Matière outil", "matiere", TOOL_MATERIALS)
        row = self._add_combo(f, row, "Revêtement", "revetement", COATINGS)

        # ── Section Géométrie ──
        row = section("Géométrie", row)

        row = self._add_diameter(f, row)
        row = self._add_spinbox(f, row, "Nombre de dents", "nb_dents", 1, 20, 1, is_int=True)
        row = self._add_switch(f, row, "À plaquettes", "a_plaquettes")
        row = self._add_combo(f, row, "Angle", "angle", ANGLES)

        # ── Section Paramètres calculés ──
        row = section("Paramètres calculés (VM32L)", row)

        # Acier
        acier_frame = ctk.CTkFrame(f, fg_color="#1a2332", corner_radius=8)
        acier_frame.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 6))
        ctk.CTkLabel(acier_frame, text="⚙ ACIER", font=ctk.CTkFont(size=13, weight="bold"),
                      text_color="#e74c3c").pack(anchor="w", padx=10, pady=(6, 2))
        self._lbl_acier = ctk.CTkLabel(acier_frame, text="—", font=ctk.CTkFont(size=12, family="Consolas"),
                                        text_color="#cccccc", justify="left")
        self._lbl_acier.pack(anchor="w", padx=10, pady=(0, 6))
        row += 1

        # Alu
        alu_frame = ctk.CTkFrame(f, fg_color="#1a2332", corner_radius=8)
        alu_frame.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 6))
        ctk.CTkLabel(alu_frame, text="⚙ ALUMINIUM", font=ctk.CTkFont(size=13, weight="bold"),
                      text_color="#3498db").pack(anchor="w", padx=10, pady=(6, 2))
        self._lbl_alu = ctk.CTkLabel(alu_frame, text="—", font=ctk.CTkFont(size=12, family="Consolas"),
                                      text_color="#cccccc", justify="left")
        self._lbl_alu.pack(anchor="w", padx=10, pady=(0, 6))
        row += 1

        # ── Notes ──
        row = section("Notes", row)

        self.notes_var = ctk.CTkTextbox(f, height=80)
        self.notes_var.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 12))
        self.notes_var.bind("<KeyRelease>", lambda e: self._on_field_change("notes", None))

        # ── Bouton dupliquer ──
        row += 1
        ctk.CTkButton(f, text="📋 Dupliquer cet outil", fg_color="#27ae60", hover_color="#2ecc71",
                       command=self._duplicate_tool).grid(row=row, column=0, columnspan=3,
                                                            sticky="ew", padx=12, pady=(4, 16))

    # ── Widgets helpers ────────────────────────────────────
    def _add_entry(self, parent, row, label, key):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        var = ctk.StringVar()
        ent = ctk.CTkEntry(parent, textvariable=var, width=280)
        ent.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=4)
        var.trace_add("write", lambda *_: self._on_field_change(key, var))
        setattr(self, f"_var_{key}", var)
        return row + 1

    def _add_combo(self, parent, row, label, key, values):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        var = ctk.StringVar()
        combo = ctk.CTkComboBox(parent, values=values, variable=var, width=220,
                                command=lambda _v: self._on_field_change(key, var))
        combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=4)
        var.trace_add("write", lambda *_: self._on_field_change(key, var))
        setattr(self, f"_var_{key}", var)
        return row + 1

    def _add_spinbox(self, parent, row, label, key, lo, hi, step, is_int=False):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        var = ctk.StringVar()
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 12), pady=4)

        def _inc():
            try:
                v = int(var.get()) if is_int else float(var.get())
            except ValueError:
                v = lo
            v = min(v + step, hi)
            var.set(str(int(v)) if is_int else str(round(v, 2)))

        def _dec():
            try:
                v = int(var.get()) if is_int else float(var.get())
            except ValueError:
                v = lo
            v = max(v - step, lo)
            var.set(str(int(v)) if is_int else str(round(v, 2)))

        ctk.CTkButton(frame, text="−", width=36, command=_dec).pack(side="left")
        ent = ctk.CTkEntry(frame, textvariable=var, width=90, justify="center")
        ent.pack(side="left", padx=4)
        ctk.CTkButton(frame, text="＋", width=36, command=_inc).pack(side="left")

        var.trace_add("write", lambda *_: self._on_field_change(key, var))
        setattr(self, f"_var_{key}", var)
        return row + 1

    def _add_diameter(self, parent, row):
        ctk.CTkLabel(parent, text="Diamètre (mm)").grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)

        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=4)

        var = ctk.StringVar()
        ent = ctk.CTkEntry(frame, textvariable=var, width=80, justify="center")
        ent.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(frame, text="mm").pack(side="left", padx=(0, 12))

        # Boutons raccourcis diamètres courants
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(side="left", fill="x")
        for d in [6, 8, 10, 12, 16, 20, 25, 32]:
            ctk.CTkButton(btn_frame, text=str(d), width=34, height=28,
                           fg_color="#2c3e50", hover_color="#3498db",
                           command=lambda d=d: var.set(str(float(d)))).pack(side="left", padx=1)

        var.trace_add("write", lambda *_: self._on_field_change("diametre", var))
        setattr(self, "_var_diametre", var)
        return row + 1

    def _add_switch(self, parent, row, label, key):
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, sticky="w", padx=(12, 8), pady=4)
        var = ctk.BooleanVar()
        sw = ctk.CTkSwitch(parent, text="Oui", variable=var, onvalue=True, offvalue=False)
        sw.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        var.trace_add("write", lambda *_: self._on_field_change(key, var))
        setattr(self, f"_var_{key}", var)
        return row + 1

    # ── Mise à jour labels calculés ──────────────────────
    def _update_calc_labels(self):
        tool = self._form_to_tool()
        angle_kr = self._parse_angle(tool.get("angle", "90"), default=90)
        params = calc_fz_and_rpm(tool["matiere"], tool["diametre"],
                                 tool["a_plaquettes"], angle_kr,
                                 tool.get("type", ""))
        nb_z = max(tool["nb_dents"], 1)

        is_foret = "Foret" in tool.get("type", "")

        for mat, lbl in [("acier", self._lbl_acier), ("alu", self._lbl_alu)]:
            p = params[mat]
            vf_raw = round(p["fz_max"] * nb_z * p["rpm_vm32l"], 1)

            # Facteur conservateur forets alu (débourrage)
            if is_foret and mat == "alu":
                vf_raw = round(vf_raw * DRILL_ALU_VF_FACTOR, 1)

            # Cap Vf réaliste
            vf_capped = vf_raw > VF_MAX_VM32L
            vf = min(vf_raw, VF_MAX_VM32L) if vf_capped else vf_raw

            rpm_warn = "  ⚠ bridé" if p["rpm_ideal"] > SPINDLE_MAX_RPM else ""
            rpm_low = "  ⚠ trop bas" if p["rpm_ideal"] < SPINDLE_MIN_RPM else ""

            # Warning Vc réelle quand RPM bridé en bas
            vc_warn = ""
            if p["rpm_ideal"] < SPINDLE_MIN_RPM and p["vc_reelle"] > p["vc"] * 1.2:
                vc_warn = f"\n⚠ Vc réelle = {p['vc_reelle']} m/min (cible {p['vc']})"

            # Ligne fz : avec ou sans chip thinning
            if p["chip_thin"] > 1.01:
                fz_line = (f"fz base = {p['fz_base']:.3f}  →  "
                           f"fz eff = {p['fz_max']:.3f} mm/dent  "
                           f"(×{p['chip_thin']:.2f} amincissement {angle_kr:.0f}°)")
            else:
                fz_line = f"fz max = {p['fz_max']:.3f} mm/dent"

            # Ligne Vf
            if is_foret and mat == "alu":
                vf_line = f"Vf = {vf} mm/min ({nb_z}L, ×{DRILL_ALU_VF_FACTOR} débourrage)"
            elif vf_capped:
                vf_line = f"Vf = {vf} mm/min ⚠ cappé (théo {vf_raw})"
            else:
                vf_line = f"Vf = {vf} mm/min ({nb_z}Z)"

            lbl.configure(text=(
                f"{fz_line}\n"
                f"Vc = {p['vc']} m/min  →  RPM idéal = {p['rpm_ideal']}{rpm_warn}{rpm_low}\n"
                f"RPM VM32L = {p['rpm_vm32l']}  →  {vf_line}"
                f"{vc_warn}"
            ))

    # ── Dessin preview outil ─────────────────────────────
    def _schedule_redraw(self):
        if self._redraw_pending:
            self.after_cancel(self._redraw_pending)
        self._redraw_pending = self.after(30, self._draw_tool)

    def _draw_tool(self):
        self._redraw_pending = None
        c = self.preview_canvas
        c.delete("all")
        c.update_idletasks()
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 20 or H < 20:
            return

        tool = self._form_to_tool()
        ttype = tool.get("type", "")
        diam = max(tool.get("diametre", 10), 2)
        nb_z = max(tool.get("nb_dents", 2), 1)
        angle_str = tool.get("angle", "90°")
        plaq = tool.get("a_plaquettes", False)
        matiere = tool.get("matiere", "HSS")

        # Couleurs selon matière
        if "Carbure" in matiere:
            body_color = "#5b6abf"
            edge_color = "#8892e0"
            shank_color = "#3d3d5c"
        elif "Cermet" in matiere:
            body_color = "#7b5ea7"
            edge_color = "#a888cc"
            shank_color = "#4a3d5c"
        else:  # HSS
            body_color = "#7a8a9a"
            edge_color = "#a0b4c8"
            shank_color = "#4a5568"

        insert_color = "#e8c848"

        # Proportions
        cx = W // 2
        max_radius = min(W, H) * 0.35
        # Diamètre proportionnel (clamped)
        radius = max(max_radius * 0.3, min(max_radius, max_radius * diam / 32))
        shank_r = radius * 0.55
        cut_len = radius * 2.2
        shank_len = radius * 1.8

        # Détection type
        is_drill = "Foret" in ttype
        is_tap = "Taraud" in ttype
        is_ball = "hémisphérique" in ttype.lower() or "hémisphérique" in angle_str.lower()
        is_chamfer = "chanfrein" in ttype.lower()
        is_face = "surfacer" in ttype.lower()

        # ════════════════════════════════════════
        #  VUE DE CÔTÉ (haut) — profil outil
        # ════════════════════════════════════════
        side_cy = H * 0.35
        tip_y = side_cy + cut_len + 10

        # Queue (shank)
        shank_top = side_cy - shank_len
        c.create_rectangle(cx - shank_r, shank_top, cx + shank_r, side_cy,
                           fill=shank_color, outline="#222", width=1)
        # Hachures queue
        for yy in range(int(shank_top) + 8, int(side_cy), 12):
            c.create_line(cx - shank_r + 3, yy, cx + shank_r - 3, yy,
                          fill="#666", width=1, dash=(3, 3))

        if is_drill:
            # Pointe de foret (triangle)
            angle_val = self._parse_angle(angle_str, default=118)
            half_a = math.radians(angle_val / 2)
            tip_extend = radius / math.tan(half_a) if half_a > 0.1 else radius
            # Corps
            c.create_rectangle(cx - radius, side_cy, cx + radius, tip_y - tip_extend,
                               fill=body_color, outline="#222", width=1)
            # Goujures (lignes hélicoïdales simulées)
            for yy in range(int(side_cy), int(tip_y - tip_extend), 8):
                off = (yy % 24) / 24 * radius * 0.6
                c.create_line(cx - radius + 4 + off, yy, cx - radius + 4 + off, yy + 5,
                              fill=edge_color, width=2)
                c.create_line(cx + radius - 4 - off, yy, cx + radius - 4 - off, yy + 5,
                              fill=edge_color, width=2)
            # Pointe
            c.create_polygon(cx - radius, tip_y - tip_extend,
                             cx + radius, tip_y - tip_extend,
                             cx, tip_y,
                             fill=body_color, outline="#222", width=1)
            c.create_line(cx, tip_y - tip_extend, cx, tip_y, fill=edge_color, width=1)

        elif is_tap:
            # Taraud — corps avec filets
            c.create_rectangle(cx - radius, side_cy, cx + radius, tip_y,
                               fill=body_color, outline="#222", width=1)
            for yy in range(int(side_cy) + 4, int(tip_y), 6):
                c.create_line(cx - radius - 3, yy, cx + radius + 3, yy,
                              fill=edge_color, width=1)
            # Chanfrein d'attaque
            chamf = radius * 0.4
            c.create_polygon(cx - radius, tip_y, cx + radius, tip_y,
                             cx + radius - chamf, tip_y + chamf,
                             cx - radius + chamf, tip_y + chamf,
                             fill=body_color, outline="#222")

        elif is_ball:
            # Corps + bout hémisphérique
            c.create_rectangle(cx - radius, side_cy, cx + radius, tip_y - radius,
                               fill=body_color, outline="#222", width=1)
            # Demi-cercle
            c.create_arc(cx - radius, tip_y - 2 * radius, cx + radius, tip_y,
                         start=180, extent=180, fill=body_color, outline="#222", width=1)
            # Arêtes
            for i in range(nb_z):
                a = i * 180 / nb_z
                ox = radius * 0.8 * math.cos(math.radians(a))
                c.create_line(cx + ox, side_cy + 4, cx + ox * 0.3, tip_y - radius * 0.3,
                              fill=edge_color, width=2)

        elif is_chamfer:
            # Corps + pointe en V
            angle_val = self._parse_angle(angle_str, default=45)
            v_height = radius / math.tan(math.radians(angle_val / 2)) if angle_val > 10 else radius
            v_height = min(v_height, cut_len * 0.6)
            c.create_rectangle(cx - radius, side_cy, cx + radius, tip_y - v_height,
                               fill=body_color, outline="#222", width=1)
            c.create_polygon(cx - radius, tip_y - v_height,
                             cx + radius, tip_y - v_height,
                             cx, tip_y,
                             fill=body_color, outline="#222", width=1)
            # Arêtes du V
            c.create_line(cx - radius, tip_y - v_height, cx, tip_y, fill=edge_color, width=2)
            c.create_line(cx + radius, tip_y - v_height, cx, tip_y, fill=edge_color, width=2)

        elif is_face:
            # Fraise à surfacer — corps large + plaquettes
            face_r = radius * 1.3
            body_h = cut_len * 0.6
            c.create_rectangle(cx - face_r, side_cy, cx + face_r, side_cy + body_h,
                               fill=body_color, outline="#222", width=1)
            # Plaquettes sur le bas
            for i in range(nb_z):
                px = cx - face_r + (2 * face_r) * (i + 0.5) / nb_z
                c.create_rectangle(px - 5, side_cy + body_h - 4, px + 5, side_cy + body_h + 8,
                                   fill=insert_color if plaq else edge_color,
                                   outline="#222", width=1)
            # Plaquettes sur les côtés
            c.create_rectangle(cx - face_r - 6, side_cy + body_h * 0.3,
                               cx - face_r, side_cy + body_h * 0.7,
                               fill=insert_color if plaq else edge_color, outline="#222")
            c.create_rectangle(cx + face_r, side_cy + body_h * 0.3,
                               cx + face_r + 6, side_cy + body_h * 0.7,
                               fill=insert_color if plaq else edge_color, outline="#222")

        else:
            # Fraise générique (2T, 3T, rainurer…)
            c.create_rectangle(cx - radius, side_cy, cx + radius, tip_y,
                               fill=body_color, outline="#222", width=1)
            # Arêtes de coupe
            for i in range(nb_z):
                x_off = -radius + (2 * radius) * (i + 0.5) / nb_z
                if plaq:
                    # Plaquettes
                    pw, ph = 8, 12
                    c.create_rectangle(cx + x_off - pw // 2, tip_y - ph - 2,
                                       cx + x_off + pw // 2, tip_y - 2,
                                       fill=insert_color, outline="#333", width=1)
                    c.create_rectangle(cx + x_off - pw // 2, side_cy + 4,
                                       cx + x_off + pw // 2, side_cy + 4 + ph,
                                       fill=insert_color, outline="#333", width=1)
                else:
                    # Goujures hélicoïdales
                    for yy in range(int(side_cy), int(tip_y), 7):
                        shift = ((yy - side_cy) / cut_len) * radius * 0.8
                        lx = cx + x_off + shift
                        if cx - radius < lx < cx + radius:
                            c.create_line(lx, yy, lx, min(yy + 5, tip_y),
                                          fill=edge_color, width=2)
            # Fond plat
            c.create_line(cx - radius, tip_y, cx + radius, tip_y, fill=edge_color, width=2)

        # ════════════════════════════════════════
        #  VUE DE DESSOUS (bas) — section / bout
        # ════════════════════════════════════════
        bot_cy = H * 0.82
        bot_r = min(radius * 1.1, (H - tip_y - 20) * 0.4, W * 0.25)
        if bot_r < 10:
            bot_r = 10

        # Cercle extérieur
        c.create_oval(cx - bot_r, bot_cy - bot_r, cx + bot_r, bot_cy + bot_r,
                      fill="#16213e", outline=body_color, width=2)

        if is_drill:
            # Deux lèvres
            c.create_line(cx, bot_cy - bot_r + 3, cx, bot_cy + bot_r - 3,
                          fill=edge_color, width=2)
            # Âme
            c.create_oval(cx - 3, bot_cy - 3, cx + 3, bot_cy + 3,
                          fill=edge_color, outline="")
        elif is_tap:
            # Filets concentriques
            for rr in [bot_r * 0.5, bot_r * 0.75, bot_r * 0.95]:
                c.create_oval(cx - rr, bot_cy - rr, cx + rr, bot_cy + rr,
                              outline=edge_color, width=1)
        elif is_ball:
            # Cercles concentriques pour la sphère
            c.create_oval(cx - bot_r * 0.6, bot_cy - bot_r * 0.6,
                          cx + bot_r * 0.6, bot_cy + bot_r * 0.6,
                          outline=edge_color, width=1)
            c.create_oval(cx - bot_r * 0.25, bot_cy - bot_r * 0.25,
                          cx + bot_r * 0.25, bot_cy + bot_r * 0.25,
                          outline=edge_color, width=1)
            # Dents
            for i in range(nb_z):
                a = math.radians(i * 360 / nb_z)
                c.create_line(cx, bot_cy,
                              cx + bot_r * 0.9 * math.cos(a),
                              bot_cy + bot_r * 0.9 * math.sin(a),
                              fill=edge_color, width=2)
        else:
            # Dents radiales
            for i in range(nb_z):
                a = math.radians(i * 360 / nb_z)
                ex = cx + bot_r * 0.92 * math.cos(a)
                ey = bot_cy + bot_r * 0.92 * math.sin(a)
                c.create_line(cx, bot_cy, ex, ey, fill=edge_color, width=2)

                if plaq:
                    # Petits carrés plaquette au bout
                    pa = a + math.radians(15)
                    px = cx + bot_r * 0.75 * math.cos(pa)
                    py = bot_cy + bot_r * 0.75 * math.sin(pa)
                    c.create_rectangle(px - 4, py - 4, px + 4, py + 4,
                                       fill=insert_color, outline="#333")

                # Arc de goujure
                ga = math.degrees(a)
                arc_r = bot_r * 0.7
                c.create_arc(cx - arc_r, bot_cy - arc_r, cx + arc_r, bot_cy + arc_r,
                             start=ga, extent=360 / nb_z * 0.5,
                             outline=edge_color, style="arc", width=1)

        # Labels
        c.create_text(cx, shank_top - 8, text="Queue", fill="#666", font=("Helvetica", 9))
        c.create_text(cx, bot_cy + bot_r + 14, text="Vue de dessous",
                      fill="#666", font=("Helvetica", 9))

        # Cotes diamètre (flèche)
        dim_y = side_cy + cut_len * 0.5
        c.create_line(cx - radius - 15, dim_y, cx + radius + 15, dim_y,
                      fill="#e74c3c", width=1, arrow="both")
        c.create_text(cx, dim_y - 10, text=f"Ø{diam} mm",
                      fill="#e74c3c", font=("Helvetica", 10, "bold"))

        # Légende
        self.preview_label.configure(
            text=f"{ttype}  •  {matiere}  •  {nb_z}Z  •  Ø{diam}"
        )

    @staticmethod
    def _parse_angle(s: str, default=90) -> float:
        try:
            return float(s.replace("°", "").strip())
        except ValueError:
            return default

    # ── Gestion données ↔ formulaire ──────────────────────
    def _tool_to_form(self, tool: dict):
        """Charge un dict outil dans le formulaire."""
        self._loading_form = True
        for key, val in tool.items():
            if key == "notes":
                self.notes_var.delete("1.0", "end")
                self.notes_var.insert("1.0", val or "")
                continue
            var = getattr(self, f"_var_{key}", None)
            if var is None:
                continue
            if isinstance(var, ctk.BooleanVar):
                var.set(bool(val))
            elif isinstance(val, (int, float)):
                var.set(str(val))
            else:
                var.set(str(val))
        self._loading_form = False
        self._schedule_redraw()
        self._update_calc_labels()

    def _form_to_tool(self) -> dict:
        """Lit le formulaire et renvoie un dict outil."""
        t = {}
        for key, default_val in EMPTY_TOOL.items():
            if key == "notes":
                t[key] = self.notes_var.get("1.0", "end").strip()
                continue
            var = getattr(self, f"_var_{key}", None)
            if var is None:
                t[key] = default_val
                continue
            raw = var.get()
            if isinstance(default_val, bool):
                t[key] = bool(var.get()) if isinstance(var, ctk.BooleanVar) else raw.lower() in ("1", "true", "oui")
            elif isinstance(default_val, int):
                try:
                    t[key] = int(float(raw))
                except ValueError:
                    t[key] = default_val
            elif isinstance(default_val, float):
                try:
                    t[key] = round(float(raw), 2)
                except ValueError:
                    t[key] = default_val
            else:
                t[key] = raw
        # Ajout des paramètres calculés
        angle_kr = self._parse_angle(t.get("angle", "90"), default=90)
        params = calc_fz_and_rpm(t["matiere"], t["diametre"],
                                 t["a_plaquettes"], angle_kr,
                                 t.get("type", ""))
        nb_z = max(t["nb_dents"], 1)
        is_foret = "Foret" in t.get("type", "")
        for mat in ("acier", "alu"):
            p = params[mat]
            t[f"fz_max_{mat}"] = p["fz_max"]
            t[f"vc_{mat}"] = p["vc"]
            t[f"vc_reelle_{mat}"] = p["vc_reelle"]
            t[f"rpm_{mat}"] = p["rpm_vm32l"]
            vf_raw = round(p["fz_max"] * nb_z * p["rpm_vm32l"], 1)
            # Facteur conservateur forets alu
            if is_foret and mat == "alu":
                vf_raw = round(vf_raw * DRILL_ALU_VF_FACTOR, 1)
            t[f"vf_{mat}"] = min(vf_raw, VF_MAX_VM32L)
        return t

    def _on_field_change(self, key, var):
        """Appelé à chaque modif d'un champ — met à jour le dict courant."""
        if self._loading_form:
            return
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.tools):
            self.tools[self.selected_idx] = self._form_to_tool()
            self.modified = True
            self._refresh_list()
            self._schedule_redraw()
            self._update_calc_labels()

    # ── Liste outils ──────────────────────────────────────
    def _refresh_list(self):
        for w in self.tool_list_frame.winfo_children():
            w.destroy()

        for i, tool in enumerate(self.tools):
            name = tool.get("nom") or f"Outil {i + 1}"
            sub = f"{tool.get('type', '')} — Ø{tool.get('diametre', '?')}  {tool.get('nb_dents', '?')}Z"
            is_sel = (i == self.selected_idx)

            btn = ctk.CTkButton(
                self.tool_list_frame,
                text=f"{name}\n{sub}",
                anchor="w",
                height=50,
                fg_color="#2980b9" if is_sel else "#2c3e50",
                hover_color="#3498db",
                font=ctk.CTkFont(size=13),
                command=lambda idx=i: self._select_tool(idx),
            )
            btn.pack(fill="x", pady=2)

    def _select_tool(self, idx):
        # Sauvegarde outil courant
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.tools):
            self.tools[self.selected_idx] = self._form_to_tool()

        self.selected_idx = idx
        if 0 <= idx < len(self.tools):
            self._tool_to_form(self.tools[idx])
        self._refresh_list()
        self._schedule_redraw()

    # ── Actions outils ─────────────────────────────────────
    def _add_tool(self):
        new_tool = copy.deepcopy(EMPTY_TOOL)
        new_tool["nom"] = f"Outil {len(self.tools) + 1}"
        self.tools.append(new_tool)
        self.modified = True
        self._select_tool(len(self.tools) - 1)

    def _delete_tool(self):
        if self.selected_idx is None or not self.tools:
            return
        name = self.tools[self.selected_idx].get("nom", "cet outil")
        if not messagebox.askyesno("Supprimer", f"Supprimer « {name} » ?"):
            return
        self.tools.pop(self.selected_idx)
        self.modified = True
        if self.tools:
            self._select_tool(max(0, self.selected_idx - 1))
        else:
            self.selected_idx = None
            self._tool_to_form(EMPTY_TOOL)
        self._refresh_list()

    def _duplicate_tool(self):
        if self.selected_idx is None:
            return
        dup = copy.deepcopy(self.tools[self.selected_idx])
        dup["nom"] = dup.get("nom", "") + " (copie)"
        self.tools.insert(self.selected_idx + 1, dup)
        self.modified = True
        self._select_tool(self.selected_idx + 1)

    # ── Fichier ────────────────────────────────────────────
    def _load_file(self, path: Path):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.tools = data
                elif isinstance(data, dict) and "tools" in data:
                    self.tools = data["tools"]
                else:
                    self.tools = []
            except Exception as e:
                messagebox.showerror("Erreur", f"Impossible de lire {path}:\n{e}")
                self.tools = []
        else:
            self.tools = []

        self.modified = False
        if self.tools:
            self._select_tool(0)
        else:
            self._tool_to_form(EMPTY_TOOL)
        self._refresh_list()

    def _save_file(self):
        # Met à jour l'outil courant
        if self.selected_idx is not None and 0 <= self.selected_idx < len(self.tools):
            self.tools[self.selected_idx] = self._form_to_tool()

        try:
            with open(TOOL_FILE, "w", encoding="utf-8") as f:
                json.dump(self.tools, f, indent=2, ensure_ascii=False)
            self.modified = False
            messagebox.showinfo("Sauvegardé", f"{len(self.tools)} outil(s) sauvegardé(s)\n→ {TOOL_FILE}")
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'écrire:\n{e}")

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Ouvrir un fichier d'outils",
            filetypes=[("JSON", "*.json"), ("Tous", "*.*")],
            initialdir=str(TOOL_FILE.parent),
        )
        if path:
            self._load_file(Path(path))

    def _new_file(self):
        if self.modified:
            if not messagebox.askyesno("Nouveau", "Modifications non sauvegardées. Continuer ?"):
                return
        self.tools = []
        self.selected_idx = None
        self._tool_to_form(EMPTY_TOOL)
        self._refresh_list()
        self.modified = False


# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ToolBuilderApp()
    app.mainloop()
