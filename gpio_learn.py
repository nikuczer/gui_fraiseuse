#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPIO Learn – Interface d'apprentissage des boutons du pupitre.
Affiche chaque action, attend l'appui physique, détecte le GPIO, sauve dans un JSON.
Nécessite pigpio (daemon pigpiod doit tourner).
"""

import json, os, time, threading
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import pigpio
    HAS_PIGPIO = True
except ImportError:
    HAS_PIGPIO = False

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "gpio_mapping.json")

# GPIOs utilisables sur Raspberry Pi (BCM) – on exclut 0,1 (I2C EEPROM)
SCAN_GPIOS = list(range(2, 28))

# Actions à mapper
ACTIONS = [
    ("x_gauche",       "X Gauche",            "Commutateur X → position Gauche"),
    ("x_droite",       "X Droite",            "Commutateur X → position Droite"),
    ("rapide_x",       "Vitesse Rapide X",    "Bouton vitesse rapide pour X"),
    ("z_haut",         "Z Haut",              "Bouton monter Z"),
    ("z_bas",          "Z Bas",               "Bouton descendre Z"),
    ("disable_drivers","Disable Drivers",     "Commutateur disable → coupe ENable drivers"),
    ("jog_a",          "Jog – Canal A",       "Molette jog : premier signal encodeur"),
    ("jog_b",          "Jog – Canal B",       "Molette jog : deuxième signal encodeur"),
]


class GpioLearnApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Apprentissage GPIO – Pupitre Fraiseuse")
        self.geometry("800x480")
        self.resizable(True, True)

        # mapping: {key: {"gpio": int, "active_low": bool}} ou ancien format {key: int}
        self.mapping: dict = {}
        self._load_mapping()

        self.pi = None
        if HAS_PIGPIO:
            try:
                self.pi = pigpio.pi()
                if not self.pi.connected:
                    self.pi = None
            except Exception:
                self.pi = None

        self._learning = False
        self._learn_key: str | None = None
        self._baseline: dict[int, int] = {}

        # Lire l'état repos de tous les GPIOs au démarrage
        self._idle_state: dict[int, int] = {}
        if self.pi:
            for gpio in SCAN_GPIOS:
                self.pi.set_mode(gpio, pigpio.INPUT)
                self.pi.set_pull_up_down(gpio, pigpio.PUD_UP)
            time.sleep(0.05)
            self._idle_state = {g: self.pi.read(g) for g in SCAN_GPIOS}

        self._build_ui()
        self._refresh_table()

    # ── UI ──────────────────────────────────────────────────────────
    def _build_ui(self):
        st = ttk.Style()
        try:
            st.theme_use('clam')
        except Exception:
            pass
        st.configure('Big.TButton', font=("Helvetica", 18), padding=10)
        st.configure('Banner.TLabel', font=("Helvetica", 18, "bold"),
                     foreground="#CC6600")
        st.configure('Status.TLabel', font=("Helvetica", 13, "bold"))
        # Treeview : lignes hautes pour écran tactile
        st.configure('Touch.Treeview', font=("Helvetica", 15), rowheight=40)
        st.configure('Touch.Treeview.Heading', font=("Helvetica", 14, "bold"))

        # ── Barre du haut ──
        top = ttk.Frame(self, padding=4)
        top.pack(fill="x")

        status_text = "pigpio OK" if self.pi else "pigpio NON connecté (démo)"
        self.lbl_status = ttk.Label(top, text=status_text, style='Status.TLabel')
        self.lbl_status.pack(side="left")

        ttk.Button(top, text="Sauvegarder", style='Big.TButton',
                   command=self._save_mapping).pack(side="right", padx=4)
        ttk.Button(top, text="Recharger", style='Big.TButton',
                   command=self._reload_mapping).pack(side="right", padx=4)

        # ── Banner apprentissage (caché par défaut) ──
        self.banner = ttk.Frame(self, padding=6)
        self.banner_label = ttk.Label(self.banner, text="", style='Banner.TLabel')
        self.banner_label.pack(side="left", expand=True)
        ttk.Button(self.banner, text="Annuler", style='Big.TButton',
                   command=self._cancel_learn).pack(side="right")

        # ── Table des actions ──
        container = ttk.Frame(self, padding=4)
        container.pack(fill="both", expand=True)

        cols = ("action", "gpio", "logique", "etat")
        self.tree = ttk.Treeview(container, columns=cols, show="headings",
                                  style='Touch.Treeview', height=8)
        self.tree.heading("action",  text="Action")
        self.tree.heading("gpio",    text="GPIO")
        self.tree.heading("logique", text="Logique")
        self.tree.heading("etat",    text="État")
        self.tree.column("action",  width=300)
        self.tree.column("gpio",    width=100, anchor="center", stretch=False)
        self.tree.column("logique", width=160, anchor="center", stretch=False)
        self.tree.column("etat",    width=100, anchor="center", stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        # ── Boutons en bas ──
        btn_frame = ttk.Frame(self, padding=6)
        btn_frame.pack(fill="x")

        for col in range(3):
            btn_frame.columnconfigure(col, weight=1)
        ttk.Button(btn_frame, text="Apprendre", style='Big.TButton',
                   command=self._start_learn_selected)\
            .grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        ttk.Button(btn_frame, text="Effacer", style='Big.TButton',
                   command=self._clear_selected)\
            .grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        ttk.Button(btn_frame, text="Tout apprendre", style='Big.TButton',
                   command=self._learn_all_sequential)\
            .grid(row=0, column=2, sticky="nsew", padx=4, pady=4)

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for key, label, desc in ACTIONS:
            entry = self.mapping.get(key)
            if entry is None:
                gpio_str, logique_str, etat = "—", "—", "—"
            elif isinstance(entry, dict):
                gpio_str = str(entry["gpio"])
                logique_str = "Actif BAS" if entry.get("active_low") else "Actif HAUT"
                idle = entry.get("idle_level", "?")
                logique_str += f"  (repos={idle})"
                etat = "OK"
            else:
                # Ancien format (juste un int) — rétro-compat
                gpio_str = str(entry)
                logique_str = "?"
                etat = "OK"
            self.tree.insert("", "end", iid=key,
                             values=(label, gpio_str, logique_str, etat))

    # ── Persistence ────────────────────────────────────────────────
    def _load_mapping(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.mapping = json.load(f)
            except Exception:
                self.mapping = {}
        else:
            self.mapping = {}

    def _save_mapping(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.mapping, f, indent=2, ensure_ascii=False)
        messagebox.showinfo("Sauvegardé", f"Mapping enregistré dans\n{CONFIG_PATH}")

    def _reload_mapping(self):
        self._load_mapping()
        self._refresh_table()

    # ── Apprentissage ──────────────────────────────────────────────
    def _selected_key(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _start_learn_selected(self):
        key = self._selected_key()
        if not key:
            messagebox.showwarning("Sélection", "Sélectionne une action dans la liste.")
            return
        self._start_learn(key)

    def _start_learn(self, key: str):
        if self._learning:
            return
        if not self.pi:
            # Mode démo : simuler avec un numéro saisi manuellement
            self._learn_demo(key)
            return

        label = next(l for k, l, _ in ACTIONS if k == key)
        self._learning = True
        self._learn_key = key

        self.banner.pack(fill="x", before=self.tree.master)
        self.banner_label.config(
            text=f"► Appuie sur le bouton pour : {label}  (en attente…)")

        # Extraire les GPIO déjà mappés (nouveau format dict ou ancien int)
        def _get_gpio(entry):
            return entry["gpio"] if isinstance(entry, dict) else entry

        already_mapped = set(_get_gpio(v) for v in self.mapping.values()
                            if v is not None)
        current_gpio = _get_gpio(self.mapping[key]) if key in self.mapping and self.mapping[key] else None
        self._scan_pins = [g for g in SCAN_GPIOS
                           if g not in already_mapped or g == current_gpio]

        # Les GPIOs sont déjà configurés en entrée pull-up au démarrage
        # Lire baseline (état actuel, devrait correspondre à idle)
        self._baseline = {g: self.pi.read(g) for g in self._scan_pins}

        # Lancer le scan dans un thread
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def _scan_loop(self):
        """Poll les GPIOs jusqu'à détecter un changement."""
        while self._learning:
            for gpio in self._scan_pins:
                current = self.pi.read(gpio)
                if current != self._baseline[gpio]:
                    # Détecté !
                    self.after(0, lambda g=gpio: self._on_gpio_detected(g))
                    return
            time.sleep(0.01)

    def _on_gpio_detected(self, gpio: int):
        if not self._learning:
            return
        key = self._learn_key
        label = next(l for k, l, _ in ACTIONS if k == key)

        # Vérifier si ce GPIO est déjà utilisé par une autre action
        for other_key, other_entry in self.mapping.items():
            other_gpio = other_entry["gpio"] if isinstance(other_entry, dict) else other_entry
            if other_gpio == gpio and other_key != key:
                other_label = next(l for k, l, _ in ACTIONS if k == other_key)
                messagebox.showwarning(
                    "Conflit",
                    f"GPIO {gpio} est déjà assigné à '{other_label}'.\n"
                    f"Efface-le d'abord si tu veux le réassigner.")
                self._stop_learn()
                return

        # Déterminer la logique : état au repos vs état détecté
        idle_level = self._idle_state.get(gpio, self._baseline.get(gpio, 1))
        current_level = self.pi.read(gpio)
        active_low = (current_level == 0 and idle_level == 1)

        self.mapping[key] = {
            "gpio": gpio,
            "active_low": active_low,
            "idle_level": idle_level,
        }
        self._stop_learn()
        self._refresh_table()

        # Sélectionner la ligne mise à jour
        self.tree.selection_set(key)
        self.tree.see(key)

        logique = "Actif BAS (repos=1)" if active_low else "Actif HAUT (repos=0)"
        messagebox.showinfo("Détecté",
                            f"{label} → GPIO {gpio}\n"
                            f"Logique : {logique}\n\n"
                            f"N'oublie pas de sauvegarder quand tu as fini.")

        # Si on est en mode séquentiel, passer au suivant
        if hasattr(self, '_seq_queue') and self._seq_queue:
            next_key = self._seq_queue.pop(0)
            self.after(500, lambda: self._start_learn(next_key))

    def _learn_demo(self, key: str):
        """Mode sans pigpio : saisie manuelle du numéro GPIO + logique."""
        label = next(l for k, l, _ in ACTIONS if k == key)

        dialog = tk.Toplevel(self)
        dialog.title(f"GPIO pour : {label}")
        dialog.geometry("400x220")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text=f"Action : {label}",
                  font=("Helvetica", 14, "bold")).pack(pady=(10, 4))
        ttk.Label(dialog, text="Numéro GPIO (BCM) :").pack()
        var_gpio = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=var_gpio, justify="center",
                          font=("Helvetica", 18))
        entry.pack(pady=4)
        entry.focus_set()

        var_logic = tk.BooleanVar(value=True)
        ttk.Checkbutton(dialog, text="Actif BAS (pull-up, repos=1)",
                        variable=var_logic).pack(pady=4)

        def ok():
            try:
                gpio = int(var_gpio.get())
                if gpio < 0 or gpio > 27:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Erreur", "Numéro GPIO invalide (0-27)")
                return
            active_low = var_logic.get()
            self.mapping[key] = {
                "gpio": gpio,
                "active_low": active_low,
                "idle_level": 1 if active_low else 0,
            }
            dialog.destroy()
            self._refresh_table()

        ttk.Button(dialog, text="OK", command=ok).pack(pady=6)
        dialog.bind("<Return>", lambda e: ok())

    def _cancel_learn(self):
        self._stop_learn()
        if hasattr(self, '_seq_queue'):
            self._seq_queue.clear()

    def _stop_learn(self):
        self._learning = False
        self._learn_key = None
        self.banner.pack_forget()

    def _clear_selected(self):
        key = self._selected_key()
        if not key:
            return
        self.mapping.pop(key, None)
        self._refresh_table()

    def _learn_all_sequential(self):
        """Lance l'apprentissage de toutes les actions non encore mappées, une par une."""
        self._seq_queue = [k for k, _, _ in ACTIONS if self.mapping.get(k) is None]
        if not self._seq_queue:
            messagebox.showinfo("Complet", "Toutes les actions sont déjà mappées.")
            return
        first = self._seq_queue.pop(0)
        self._start_learn(first)

    def destroy(self):
        if self.pi:
            try:
                self.pi.stop()
            except Exception:
                pass
        super().destroy()


if __name__ == "__main__":
    app = GpioLearnApp()
    app.mainloop()
