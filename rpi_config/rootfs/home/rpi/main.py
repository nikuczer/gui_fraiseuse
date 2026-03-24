#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fraiseuse GUI – pigpio PWM pur (sans fallback) – lisse et rapide
- X: STEP=13, DIR=16, EN=19 (BCM)
- Z: STEP=12, DIR=6,  EN=5  (BCM)
- STEP via pigpio.hardware_PWM (DMA), duty 50%
- Trapèze (dt=5 ms) pour jog continu; déplacements discrets intégrés
- MàJ position tous les 0,1 mm
- I/O: entrées/sorties via pigpio, entrées sans pull interne (PUD_OFF)
- Sécurité: "Désactiver sorties" => OUT en INPUT (haute impédance)
- UI: 800×480, plein écran, overlay NumPad, config persistante
"""

import math, time, threading, os, json
from typing import Optional, Dict, Any
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import pigpio  # nécessite pigpiod (sudo systemctl start pigpiod)
except Exception:
    import sys
    print("⚠️ pigpio non trouvé : mode SIMULATION", file=sys.stderr)
    class _FakeCallback:
        def __init__(self, pin, edge, func):
            self.pin, self.edge, self.func = pin, edge, func
        def cancel(self):
            self.func = None
    class _FakePi:
        INPUT=0; OUTPUT=1; EITHER_EDGE=2; RISING_EDGE=0; FALLING_EDGE=1
        PUD_OFF=0; PUD_UP=1; PUD_DOWN=2
        def __init__(self):
            self.connected = 1
            self._levels = {}
            self._pwm = {}
            self._cbs = []
        def stop(self): return 0
        def set_mode(self, pin, mode): return 0
        def write(self, pin, val):
            self._levels[pin] = 1 if val else 0
            return 0
        def read(self, pin): return int(self._levels.get(pin, 0))
        def set_pull_up_down(self, pin, pud): return 0
        def set_glitch_filter(self, pin, us): return 0
        def hardware_PWM(self, pin, freq, duty):
            self._pwm[pin] = (int(freq), int(duty))
            self._levels[pin] = 1 if (freq and duty) else 0
            return 0
        def callback(self, pin, edge, func):
            cb = _FakeCallback(pin, edge, func)
            self._cbs.append(cb)
            return cb
    class pigpio:  # shim module
        pi = _FakePi
        INPUT=_FakePi.INPUT; OUTPUT=_FakePi.OUTPUT; EITHER_EDGE=_FakePi.EITHER_EDGE
        RISING_EDGE=_FakePi.RISING_EDGE; FALLING_EDGE=_FakePi.FALLING_EDGE
        PUD_OFF=_FakePi.PUD_OFF; PUD_UP=_FakePi.PUD_UP; PUD_DOWN=_FakePi.PUD_DOWN
# nécessite pigpiod (sudo systemctl start pigpiod)

APP_TITLE = "Fraiseuse – Outils & Positions"
DEFAULT_SPINDLE_MAX_RPM = 3000
DEFAULT_SPINDLE_MIN_RPM = 500

PWM_DUTY = 500000  # 50% pour hardware_PWM (0..1_000_000)

def interp_ascending(d, dmin, dmax, small_d_val, large_d_val):
    if d<=dmin: return small_d_val
    if d>=dmax: return large_d_val
    return small_d_val + (large_d_val-small_d_val)*(d-dmin)/(dmax-dmin)

# ===== Bas niveau pigpio PWM =====
class LowLevelStepper:
    """
    STEP via hardware_PWM, DIR/EN en GPIO.
    EN supposé actif bas (0 = enable). Adapter si besoin.
    """
    def __init__(self, pi: pigpio.pi, step_pin:int, dir_pin:int, en_pin:int, steps_per_mm:float, outputs_disabled_callable):
        self.pi = pi
        self.step_pin = step_pin
        self.dir_pin  = dir_pin
        self.en_pin   = en_pin
        self.steps_per_mm = max(steps_per_mm, 1e-6)
        self.enabled = False
        self.outputs_disabled_callable = outputs_disabled_callable
        self.last_freq_hz = 0.0
        self.last_dir_positive = True

        self.pi.set_mode(self.dir_pin, pigpio.OUTPUT)
        self.pi.set_mode(self.en_pin,  pigpio.OUTPUT)
        self.pi.set_mode(self.step_pin, pigpio.OUTPUT)
        # EN à HIGH = désactivé au repos (actif bas)
        self.pi.write(self.en_pin, 1)
        self.pi.write(self.dir_pin, 0)
        # stop PWM
        self.pi.hardware_PWM(self.step_pin, 0, 0)

    def set_enabled(self, state: bool):
        self.enabled = state and (not self.outputs_disabled_callable())
        self.pi.write(self.en_pin, 0 if self.enabled else 1)

    def set_dir(self, positive: bool):
        self.last_dir_positive = bool(positive)
        self.pi.write(self.dir_pin, 1 if positive else 0)

    def set_frequency(self, freq_hz: float) -> float:
        """Fixe la fréquence STEP (0 = stop) et retourne la fréquence réellement appliquée."""
        if self.outputs_disabled_callable() or not self.enabled or freq_hz <= 0.0:
            self.pi.hardware_PWM(self.step_pin, 0, 0)
            self.last_freq_hz = 0.0
            return 0.0
        else:
            f = int(max(1, min(200_000, freq_hz)))
            self.pi.hardware_PWM(self.step_pin, f, PWM_DUTY)
            self.last_freq_hz = float(f)
            return float(f)

    def stop_pwm(self):
        self.pi.hardware_PWM(self.step_pin, 0, 0)
        self.last_freq_hz = 0.0

# ===== Mouvement continu (rampe PWM) =====
class AxisContinuous:
    def __init__(self, motor: LowLevelStepper, accel_mm_s2: float, decel_mm_s2: float,
                 post_delta_cb, name="X"):
        self.m = motor
        self.acc = max(10.0, float(accel_mm_s2))
        self.dec = max(10.0, float(decel_mm_s2))
        self.post_delta_cb = post_delta_cb
        self._lock = threading.Lock()
        self._target_mm_min = 0.0
        self._dir_pos = True
        self._current_mm_min = 0.0
        self._running = False
        self._thr: Optional[threading.Thread] = None
        self.name = name

    def set_target(self, dir_positive: Optional[bool], v_mm_min: float):
        with self._lock:
            if dir_positive is not None:
                self._dir_pos = bool(dir_positive)
            self._target_mm_min = max(0.0, float(v_mm_min))

    def start(self):
        if self._thr and self._thr.is_alive(): return
        self._running = True
        self._thr = threading.Thread(target=self._loop, daemon=True); self._thr.start()

    def stop(self):
        with self._lock:
            self._target_mm_min = 0.0
        self._running = False

    def replace_motor(self, new_motor: LowLevelStepper):
        self.stop()
        self.m = new_motor

    def _loop(self):
        self.m.set_enabled(True)
        dt = 0.005  # 5 ms => fluide
        last_dir = None
        acc_steps = 0.0
        steps_per_tenth = max(int(round(self.m.steps_per_mm * 0.1)), 1)
        try:
            while True:
                with self._lock:
                    vt = self._target_mm_min
                    dpos = self._dir_pos

                # Rampe trapezoïdale (sur mm/min)
                if self._current_mm_min < vt:
                    self._current_mm_min = min(self._current_mm_min + self.acc * dt * 60.0, vt)
                elif self._current_mm_min > vt:
                    self._current_mm_min = max(self._current_mm_min - self.dec * dt * 60.0, vt)

                v = self._current_mm_min  # mm/min

                if last_dir != dpos:
                    self.m.set_dir(dpos); last_dir = dpos

                # PWM fréquence = pas/s = (v mm/min) * steps/mm / 60
                freq_cmd = (v / 60.0) * self.m.steps_per_mm
                freq_eff = self.m.set_frequency(freq_cmd)
                # Intégration pour mise à jour UI tous les 0.1 mm
                acc_steps += freq_eff * dt
                while acc_steps >= steps_per_tenth:
                    acc_steps -= steps_per_tenth
                    self.post_delta_cb(0.1 if dpos else -0.1)

                if v <= 0.0 and vt <= 0.0 and not self._running:
                    self.m.set_frequency(0)
                    break
                time.sleep(dt)
        finally:
            self.m.set_enabled(False)
            self.m.stop_pwm()

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
        lbl.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,6))
        e = ttk.Entry(card, textvariable=self.value_var, justify="center", font=("Helvetica", 24))
        e.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0,8))
        e.focus_set()

        def add(ch):
            if getattr(self, "_first", True):
                self.value_var.set(""); self._first=False
            self.value_var.set(self.value_var.get()+ch)

        def clear():
            self.value_var.set(""); self._first=True

        def ok():
            txt=self.value_var.get().strip().replace(",",".")
            try:
                v=float(txt); v=round(v,self.decimals)
                self.destroy()
                self.on_validate(v)
            except Exception:
                messagebox.showerror("Erreur","Valeur invalide")

        def cancel():
            self.destroy()
            self.on_cancel()

        btns=[("7",2,0),("8",2,1),("9",2,2),
              ("4",3,0),("5",3,1),("6",3,2),
              ("1",4,0),("2",4,1),("3",4,2),
              (",",5,0),("0",5,1),(".",5,2)]
        for t,r,c in btns:
            ttk.Button(card, text=t, command=lambda T=t: add(T)).grid(row=r,column=c,sticky="nsew",padx=4,pady=4,ipady=10)
        ttk.Button(card, text="Effacer", command=clear).grid(row=6,column=0,sticky="nsew",padx=4,pady=(6,0),ipady=10)
        ttk.Button(card, text="OK", command=ok).grid(row=6,column=1,sticky="nsew",padx=4,pady=(6,0),ipady=10)
        ttk.Button(card, text="Annuler", command=cancel).grid(row=6,column=2,sticky="nsew",padx=4,pady=(6,0),ipady=10)

        for r in range(7): card.rowconfigure(r, weight=1)
        for c in range(3): card.columnconfigure(c, weight=1)

# ===== Scrollable helper =====
class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

# ===== App =====
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE); self.geometry("800x480+0+0")
        try: self.attributes('-fullscreen', True); self.overrideredirect(True)
        except Exception: pass
        self.bind('<F11>', lambda e:self._toggle_fullscreen())
        self.bind('<Escape>', lambda e:self._end_fullscreen())
        self.config_path = os.path.join(os.path.dirname(__file__), "fraiseuse_config.json")

        # Fonts/styles compacts
        base_font=("Helvetica",14); btn_font=("Helvetica",20); jog_font=("Helvetica",18)
        med_btn_font=("Helvetica",16); small_font=("Helvetica",11)
        big_radio_font=("Helvetica",22); mat_radio_font=("Helvetica",20); tab_font=("Helvetica",16)
        self.option_add("*Font", base_font); self.option_add("*Button.Font", btn_font); self.option_add("*TButton.Font", btn_font)
        try:
            st=ttk.Style(); st.theme_use('clam')
            st.configure('TNotebook.Tab', font=tab_font, padding=(12,4))
            st.configure('Big.TRadiobutton', font=big_radio_font, padding=4)
            st.configure('Mat.TRadiobutton', font=mat_radio_font, padding=4)
            st.configure('XL.TButton', font=btn_font, padding=10)
            st.configure('Med.TButton', font=med_btn_font, padding=6)
            st.configure('Jog.TButton', font=jog_font, padding=4)
            st.configure('Small.TLabel', font=small_font)
            st.configure('Warn.TLabel', foreground="#AA0000")
            st.configure('Overlay.TFrame', background='', relief='raised')
            st.configure('Card.TFrame', relief='raised')
        except Exception: pass

        # pigpio
        self.pi = pigpio.pi()
        if not self.pi or not self.pi.connected:
            messagebox.showerror("pigpio", "Impossible de se connecter à pigpiod.\nLance: sudo systemctl start pigpiod")
            raise SystemExit(1)

        # ===== Cutting params =====
        self.selected_diameter = tk.DoubleVar(value=6.0)
        self.selected_teeth    = tk.IntVar(value=4)
        self.selected_material = tk.StringVar(value="alu")
        self.var_rpm  = tk.StringVar(value="―")
        self.var_feed = tk.StringVar(value="―")
        self.var_details = tk.StringVar(value="")
        self.var_limit   = tk.StringVar(value="")

        # Vc (m/min): min (petit Ø) → max (gros Ø)
        self.vc_alu_min=tk.DoubleVar(value=160.0); self.vc_alu_max=tk.DoubleVar(value=250.0)
        self.vc_acier_min=tk.DoubleVar(value=60.0); self.vc_acier_max=tk.DoubleVar(value=100.0)
        # fz (mm/dent): min (petit Ø) → max (gros Ø)
        self.fz_alu_min=tk.DoubleVar(value=0.020); self.fz_alu_max=tk.DoubleVar(value=0.050)
        self.fz_acier_min=tk.DoubleVar(value=0.010); self.fz_acier_max=tk.DoubleVar(value=0.025)
        # Broche min/max
        self.spindle_max_rpm = tk.IntVar(value=DEFAULT_SPINDLE_MAX_RPM)
        self.spindle_min_rpm = tk.IntVar(value=DEFAULT_SPINDLE_MIN_RPM)

        # ===== Mechanics (editable) =====
        self.steps_rev_x = tk.IntVar(value=200); self.microstep_x = tk.IntVar(value=16); self.lead_x = tk.DoubleVar(value=5.0)
        self.steps_rev_z = tk.IntVar(value=200); self.microstep_z = tk.IntVar(value=16); self.lead_z = tk.DoubleVar(value=5.0)

        # ===== Positions & speeds =====
        self.pos_x_mm=tk.DoubleVar(value=0.0); self.pos_x_str=tk.StringVar(value="0.0")
        self.pos_x_mm.trace_add('write', lambda *a:self.pos_x_str.set(f"{self.pos_x_mm.get():.1f}"))
        self.home_x_mm=tk.DoubleVar(value=0.0)
        self.jog_speed_x=tk.DoubleVar(value=300.0)
        self.rapid_x_speed=tk.DoubleVar(value=1500.0)
        self.accel_x=tk.DoubleVar(value=300.0); self.decel_x=tk.DoubleVar(value=300.0)

        self.pos_z_mm=tk.DoubleVar(value=0.0); self.home_z_mm=tk.DoubleVar(value=0.0)
        self.z_vmin=tk.DoubleVar(value=50.0); self.z_vmax=tk.DoubleVar(value=500.0); self.z_accel=tk.DoubleVar(value=300.0)

        # ===== GPIO config (editable) =====
        self.outputs_disabled=tk.BooleanVar(value=False)
        self.gpio_cfg: Dict[str, Any] = {
            "OUT": {
                "X_STEP":{"pin":13,"active_high":True},
                "X_DIR": {"pin":16,"active_high":True},
                "X_EN":  {"pin":19,"active_high":False},
                "Z_STEP":{"pin":12,"active_high":True},
                "Z_DIR": {"pin":6,"active_high":True},
                "Z_EN":  {"pin":5,"active_high":False}
            },
            "IN": {
                "MANUAL":{"pin":26,"active_high":True},   # à adapter à ton câblage
                "X_PLUS":{"pin":20,"active_high":True},
                "X_MINUS":{"pin":21,"active_high":True},
                "X_RAPID":{"pin":19+20,"active_high":True},  # exemple 39 si dispo, adapte
                "Z_PLUS":{"pin":23,"active_high":True},
                "Z_MINUS":{"pin":24,"active_high":True},
                "ENC_CLK":{"pin":17,"active_high":True},
                "ENC_DT": {"pin":27,"active_high":True},
                "ENC_SW": {"pin":22,"active_high":True},
            }
        }

        # Load config first
        self._load_params(silent=True)

        # Instantiate motors with current config
        self._rebuild_motors_from_state()

        # UI
        self._build_ui()
        self._compute_tools()

        # IO monitor & encoder
        self._start_io_monitor()
        self._setup_encoder()

    # ---------- Thread-safe UI updates ----------
    def _post_pos_x_delta(self, delta_mm: float):
        if abs(delta_mm) < 1e-9: return
        self.after(0, lambda d=delta_mm: self.pos_x_mm.set(round(self.pos_x_mm.get() + d, 1)))

    def _post_pos_z_delta(self, delta_mm: float):
        if abs(delta_mm) < 1e-9: return
        self.after(0, lambda d=delta_mm: self.pos_z_mm.set(round(self.pos_z_mm.get() + d, 1)))

    # ===== UI builders =====
    def _build_ui(self):
        nb=ttk.Notebook(self); nb.pack(fill="both", expand=True)
        t_tools=ttk.Frame(nb); t_pos=ttk.Frame(nb); t_params=ttk.Frame(nb); t_trap=ttk.Frame(nb); t_io=ttk.Frame(nb); t_mech=ttk.Frame(nb)
        nb.add(t_tools, text="Outils"); nb.add(t_pos, text="Positions"); nb.add(t_params, text="Paramètres coupe")
        nb.add(t_trap, text="Trapèzes & Z"); nb.add(t_io, text="I/O"); nb.add(t_mech, text="Mécanique")

        self._build_tools_tab(t_tools)
        self._build_positions_tab(t_pos)
        self._build_params_tab(t_params)
        self._build_trapezes_tab(t_trap)
        self._build_io_tab(t_io)
        self._build_mechanics_tab(t_mech)

    def _num_entry(self, parent, var, width=8, decimals=0, **grid_kwargs):
        e=ttk.Entry(parent, textvariable=var, width=width, state="readonly", justify="left")
        def _show_numpad(evn=None):
            try: init = f"{float(var.get()):.{decimals}f}" if decimals>0 else str(int(float(var.get())))
            except: init=str(var.get())
            def on_ok(v):
                if decimals==0: var.set(int(v))
                else: var.set(round(float(v),decimals))
                try: self._apply_params()
                except: pass
            OverlayNumpad(self, on_validate=on_ok, on_cancel=lambda: None, decimals=decimals, initial=init)
        e.bind("<Button-1>", _show_numpad)
        if grid_kwargs: e.grid(**grid_kwargs)
        return e

    # --- Tools
    def _build_tools_tab(self, tab):
        STANDARD_DIAMETERS=[2,3,4,5,6,8,10,12,14,16,18,20]
        root=ttk.Frame(tab, padding=6); root.pack(fill="both", expand=True)
        left=ttk.Labelframe(root,text="Diamètre (mm)"); right=ttk.Labelframe(root,text="Dents & Matériau")
        left.grid(row=0,column=0,sticky="nsew",padx=(0,4)); right.grid(row=0,column=1,sticky="nsew",padx=(4,0))
        root.columnconfigure(0,weight=1); root.columnconfigure(1,weight=1)

        li=ttk.Frame(left); li.pack(fill="both", expand=True, padx=4, pady=4)
        self.listbox=tk.Listbox(li, selectmode=tk.SINGLE, height=8, font=("Helvetica",16))
        for d in STANDARD_DIAMETERS: self.listbox.insert(tk.END, f"{d:.0f}")
        self.listbox.pack(fill="both", expand=True); self.listbox.bind("<<ListboxSelect>>", self._on_list_select)
        ttk.Button(li, text="Diamètre personnalisé…", style='Med.TButton', command=self._open_numpad).pack(fill="x", pady=(6,0))

        ri=ttk.Frame(right); ri.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Label(ri, text="Dents :").pack(anchor="w")
        rb=ttk.Frame(ri); rb.pack(fill="x", pady=(2,4))
        for z in range(1,8):
            ttk.Radiobutton(rb, text=str(z), value=z, variable=self.selected_teeth, command=self._compute_tools, style='Big.TRadiobutton').pack(side=tk.LEFT,expand=True,fill="x",padx=3)
        ttk.Label(ri, text="Matériau :").pack(anchor="w")
        mats=ttk.Frame(ri); mats.pack(fill="x", pady=(2,4))
        for label,key in (("Aluminium","alu"),("Acier","acier")):
            ttk.Radiobutton(mats, text=label, value=key, variable=self.selected_material, command=self._compute_tools, style='Mat.TRadiobutton').pack(side=tk.LEFT,expand=True,fill="x",padx=4)

        res=ttk.Labelframe(ri, text="Résultats"); res.pack(fill="both", expand=True)
        big=("Helvetica",22,"bold")
        ttk.Label(res, text="Vitesse de rotation (tr/min) :").pack(anchor="w", padx=6, pady=(6,0))
        ttk.Label(res, textvariable=self.var_rpm, font=big).pack(anchor="center", pady=(0,6))
        ttk.Label(res, text="Vitesse d'avance (mm/min) :").pack(anchor="w", padx=6, pady=(6,0))
        ttk.Label(res, textvariable=self.var_feed, font=big).pack(anchor="center", pady=(0,6))
        ttk.Label(res, textvariable=self.var_details, style='Small.TLabel', wraplength=280, justify="left").pack(anchor="w", padx=6, pady=(2,0))
        ttk.Label(res, textvariable=self.var_limit, style='Warn.TLabel').pack(anchor="w", padx=6, pady=(0,6))

    # --- Positions
    def _build_positions_tab(self, tab):
        root=ttk.Frame(tab,padding=6); root.pack(fill="both", expand=True)
        left=ttk.Labelframe(root,text="Déplacements X / Z"); left.grid(row=0,column=0,sticky="nsew"); root.columnconfigure(0,weight=1)
        pr=ttk.Frame(left); pr.pack(fill="x", pady=(6,6), padx=6)
        ttk.Label(pr, text="Position X (mm) :", font=("Helvetica",16,"bold")).pack(side=tk.LEFT)
        ttk.Label(pr, textvariable=self.pos_x_str, font=("Helvetica",30,"bold")).pack(side=tk.LEFT, padx=(10,0))
        btns=ttk.Frame(left); btns.pack(fill="x", padx=6, pady=6)
        labels=[("−50",-50),("−10",-10),("−1",-1),("+1",1),("+10",10),("+50",50)]
        for c,(txt,delta) in enumerate(labels):
            ttk.Button(btns, text=txt, style='Jog.TButton', command=lambda d=delta:self._jog_x(d)).grid(row=0,column=c,sticky="nsew",padx=3,pady=3,ipady=4); btns.columnconfigure(c,weight=1)
        ctl=ttk.Frame(left); ctl.pack(fill="x", padx=6, pady=(0,6))
        ttk.Button(ctl, text="Définir HOME X (ici)", style='XL.TButton', command=self._set_home_x).grid(row=0,column=0,sticky="nsew",padx=4)
        ttk.Button(ctl, text="GO HOME X", style='XL.TButton', command=self._go_home_x).grid(row=0,column=1,sticky="nsew",padx=4)
        ttk.Button(ctl, text="STOP X", style='XL.TButton', command=self._stop_x).grid(row=0,column=2,sticky="nsew",padx=4)
        for c in range(3): ctl.columnconfigure(c,weight=1)
        zrow=ttk.Frame(left); zrow.pack(fill="x", padx=6, pady=(6,6))
        ttk.Button(zrow, text="HOME Z (ici)", style='XL.TButton', command=self._set_home_z).grid(row=0,column=0,sticky="nsew",padx=4)
        ttk.Button(zrow, text="GO HOME Z (rapide)", style='XL.TButton', command=self._go_home_z).grid(row=0,column=1,sticky="nsew",padx=4)
        for c in range(2): zrow.columnconfigure(c,weight=1)
        self.manual_label=ttk.Label(left, text="", style='Warn.TLabel'); self.manual_label.pack(anchor="w", padx=10, pady=(4,0))

    # --- Paramètres coupe
    def _build_params_tab(self, tab):
        root=ttk.Frame(tab,padding=6); root.pack(fill="both", expand=True)
        alu=ttk.Labelframe(root,text="Aluminium"); ac=ttk.Labelframe(root,text="Acier")
        alu.grid(row=0,column=0,sticky="nsew",padx=(0,4),pady=(0,4)); ac.grid(row=0,column=1,sticky="nsew",padx=(4,0),pady=(0,4))
        root.columnconfigure(0,weight=1); root.columnconfigure(1,weight=1)
        ttk.Label(alu,text="Vc min (petit Ø) m/min").grid(row=0,column=0,sticky="e"); self._num_entry(alu,self.vc_alu_min,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(alu,text="Vc max (gros Ø) m/min").grid(row=1,column=0,sticky="e"); self._num_entry(alu,self.vc_alu_max,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Label(ac,text="Vc min (petit Ø) m/min").grid(row=0,column=0,sticky="e"); self._num_entry(ac,self.vc_acier_min,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(ac,text="Vc max (gros Ø) m/min").grid(row=1,column=0,sticky="e"); self._num_entry(ac,self.vc_acier_max,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Separator(alu).grid(row=2,column=0,columnspan=2,sticky="ew",pady=4)
        ttk.Label(alu,text="fz min (petit Ø) mm/dent").grid(row=3,column=0,sticky="e"); self._num_entry(alu,self.fz_alu_min,width=8,decimals=3,row=3,column=1,sticky="w")
        ttk.Label(alu,text="fz max (gros Ø) mm/dent").grid(row=4,column=0,sticky="e"); self._num_entry(alu,self.fz_alu_max,width=8,decimals=3,row=4,column=1,sticky="w")
        ttk.Separator(ac).grid(row=2,column=0,columnspan=2,sticky="ew",pady=4)
        ttk.Label(ac,text="fz min (petit Ø) mm/dent").grid(row=3,column=0,sticky="e"); self._num_entry(ac,self.fz_acier_min,width=8,decimals=3,row=3,column=1,sticky="w")
        ttk.Label(ac,text="fz max (gros Ø) mm/dent").grid(row=4,column=0,sticky="e"); self._num_entry(ac,self.fz_acier_max,width=8,decimals=3,row=4,column=1,sticky="w")
        sp=ttk.Labelframe(root,text="Broche"); sp.grid(row=1,column=0,columnspan=2,sticky="nsew")
        ttk.Label(sp,text="Vitesse de rotation max (tr/min)").grid(row=0,column=0,sticky="e"); self._num_entry(sp,self.spindle_max_rpm,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(sp,text="Vitesse de rotation min (tr/min)").grid(row=1,column=0,sticky="e"); self._num_entry(sp,self.spindle_min_rpm,width=8,decimals=0,row=1,column=1,sticky="w")
        rowbtn=ttk.Frame(root); rowbtn.grid(row=2,column=0,columnspan=2,sticky="ew",pady=(6,0))
        ttk.Button(rowbtn,text="Appliquer",style='Med.TButton',command=self._apply_params).pack(side=tk.LEFT,expand=True,fill="x",padx=4)
        ttk.Button(rowbtn,text="Sauvegarder",style='Med.TButton',command=self._save_params).pack(side=tk.LEFT,expand=True,fill="x",padx=4)
        ttk.Button(rowbtn,text="Charger",style='Med.TButton',command=self._load_params).pack(side=tk.LEFT,expand=True,fill="x",padx=4)

    # --- Trapèzes & Z
    def _build_trapezes_tab(self, tab):
        root=ttk.Frame(tab,padding=6); root.pack(fill="both", expand=True)
        xbox=ttk.Labelframe(root,text="Axe X – Profil & Rapide"); xbox.grid(row=0,column=0,sticky="nsew",padx=(0,4),pady=(0,4))
        ttk.Label(xbox,text="Accélération X (mm/s²)").grid(row=0,column=0,sticky="e"); self._num_entry(xbox,self.accel_x,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(xbox,text="Décélération X (mm/s²)").grid(row=1,column=0,sticky="e"); self._num_entry(xbox,self.decel_x,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Label(xbox,text="Vitesse RAPIDE X (mm/min)").grid(row=2,column=0,sticky="e"); self._num_entry(xbox,self.rapid_x_speed,width=8,decimals=0,row=2,column=1,sticky="w")

        zbox=ttk.Labelframe(root,text="Axe Z – Maintenir (step/dir)"); zbox.grid(row=0,column=1,sticky="nsew",padx=(4,0),pady=(0,4))
        ttk.Label(zbox,text="Vitesse minimale Z (mm/min)").grid(row=0,column=0,sticky="e"); self._num_entry(zbox,self.z_vmin,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(zbox,text="Vitesse maximale Z (mm/min)").grid(row=1,column=0,sticky="e"); self._num_entry(zbox,self.z_vmax,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Label(zbox,text="Accélération Z (mm/s²)").grid(row=2,column=0,sticky="e"); self._num_entry(zbox,self.z_accel,width=8,decimals=0,row=2,column=1,sticky="w")
        btns=ttk.Frame(zbox); btns.grid(row=3,column=0,columnspan=2,sticky="ew",pady=(6,0))
        up=ttk.Button(btns,text="Z HAUT (maintenir)",style='Med.TButton')
        dn=ttk.Button(btns,text="Z BAS (maintenir)",style='Med.TButton')
        up.grid(row=0,column=0,sticky="nsew",padx=4,ipady=4); dn.grid(row=0,column=1,sticky="nsew",padx=4,ipady=4)
        btns.columnconfigure(0,weight=1); btns.columnconfigure(1,weight=1)
        up.bind("<ButtonPress-1>",lambda e:self._z_hold(True)); up.bind("<ButtonRelease-1>",lambda e:self._z_release(True))
        dn.bind("<ButtonPress-1>",lambda e:self._z_hold(False)); dn.bind("<ButtonRelease-1>",lambda e:self._z_release(False))
        root.columnconfigure(0,weight=1); root.columnconfigure(1,weight=1)

    # --- I/O
    def _build_io_tab(self, tab):
        root=ttk.Frame(tab,padding=6); root.pack(fill="both", expand=True)
        ttk.Label(root, text="(Entrées sans pull interne — résistances externes)").pack(anchor="w")
        ttk.Checkbutton(root, text="Désactiver TOUTES les sorties (sécurité)", variable=self.outputs_disabled,
                        command=self._apply_outputs_disabled).pack(anchor="w", pady=(0,6))

        sub=ttk.Notebook(root); sub.pack(fill="both", expand=True)
        out_tab=ttk.Frame(sub); in_tab=ttk.Frame(sub)
        sub.add(out_tab, text="Sorties"); sub.add(in_tab, text="Entrées")
        out_sf=ScrollFrame(out_tab); out_sf.pack(fill="both", expand=True)
        in_sf =ScrollFrame(in_tab);  in_sf.pack(fill="both", expand=True)
        self._build_gpio_table(out_sf.inner, "OUT", show_state=True)
        self._build_gpio_table(in_sf.inner,  "IN",  show_state=True)
        btns=ttk.Frame(root); btns.pack(fill="x", pady=(6,0))
        ttk.Button(btns, text="Appliquer GPIO (ré-instancier)", style='Med.TButton', command=self._apply_gpio_and_rebuild).pack(side=tk.LEFT, expand=True, fill="x", padx=4)
        ttk.Button(btns, text="Sauvegarder config", style='Med.TButton', command=self._save_params).pack(side=tk.LEFT, expand=True, fill="x", padx=4)
        ttk.Button(btns, text="Recharger config", style='Med.TButton', command=self._load_params).pack(side=tk.LEFT, expand=True, fill="x", padx=4)

    def _build_gpio_table(self, parent, group: str, show_state: bool=False):
        headers=["Signal","Pin","Actif haut ?"]
        for c,h in enumerate(headers): ttk.Label(parent, text=h, font=("Helvetica",14,"bold")).grid(row=0, column=c, sticky="w", padx=6, pady=2)
        rows=list(self.gpio_cfg[group].keys())
        self._gpio_widgets=getattr(self, "_gpio_widgets", {}); self._gpio_widgets.setdefault(group, {})
        for r, key in enumerate(rows, start=1):
            ttk.Label(parent, text=key).grid(row=r,column=0,sticky="w",padx=6,pady=2)
            pin_var=tk.IntVar(value=int(self.gpio_cfg[group][key]["pin"]))
            self._gpio_widgets[group][key]={"pin_var":pin_var}
            e=ttk.Entry(parent, textvariable=pin_var, width=8, state="readonly"); e.grid(row=r,column=1,sticky="w",padx=6,pady=2)
            e.bind("<Button-1>", lambda ev, v=pin_var: self._edit_int(v,"Pin GPIO"))
            pol_var=tk.BooleanVar(value=bool(self.gpio_cfg[group][key]["active_high"]))
            self._gpio_widgets[group][key]["pol_var"]=pol_var
            def _make_trace(g=group, k=key, v=pol_var):
                def _on_change(*a):
                    try: self.gpio_cfg[g][k]["active_high"] = bool(v.get())
                    except Exception: pass
                return _on_change
            pol_var.trace_add('write', _make_trace())
            ttk.Checkbutton(parent, variable=pol_var).grid(row=r,column=2,sticky="w",padx=18)
            if show_state:
                stv=tk.StringVar(value="—"); self._gpio_widgets[group][key]["state_var"]=stv
                lbl=ttk.Label(parent, textvariable=stv); lbl.grid(row=r,column=3,sticky="w",padx=12)

    def _edit_int(self, var: tk.IntVar, title: str):
        OverlayNumpad(self, on_validate=lambda v: var.set(int(v)), on_cancel=lambda: None, decimals=0, initial=str(var.get()))

    # ===== Tools logic =====
    def _on_list_select(self, e):
        sel=self.listbox.curselection()
        if sel: self.selected_diameter.set(float([2,3,4,5,6,8,10,12,14,16,18,20][sel[0]])); self._compute_tools()

    def _open_numpad(self):
        OverlayNumpad(self, on_validate=lambda v: (self.selected_diameter.set(round(float(v),1)), self.listbox.selection_clear(0,tk.END), self._compute_tools()),
                      on_cancel=lambda: None, decimals=1, initial=f"{self.selected_diameter.get():.1f}")

    def _apply_params(self):
        try:
            if hasattr(self,'axis_x'):
                self.axis_x.acc = float(self.accel_x.get())
                self.axis_x.dec = float(self.decel_x.get())
        except: pass
        self._compute_tools()

    def _compute_tools(self):
        try:
            d=float(self.selected_diameter.get()); z=int(self.selected_teeth.get()); mat=self.selected_material.get()
            if d<=0 or z<=0: raise ValueError
        except:
            self.var_rpm.set("―"); self.var_feed.set("―"); self.var_details.set(""); self.var_limit.set(""); return
        dmin=2; dmax=20
        if mat=='alu':
            vc=interp_ascending(d,dmin,dmax,self.vc_alu_min.get(),self.vc_alu_max.get())
            fz=interp_ascending(d,dmin,dmax,self.fz_alu_min.get(),self.fz_alu_max.get())
        else:
            vc=interp_ascending(d,dmin,dmax,self.vc_acier_min.get(),self.vc_acier_max.get())
            fz=interp_ascending(d,dmin,dmax,self.fz_acier_min.get(),self.fz_acier_max.get())
        rpm_th=(1000.0*vc)/(math.pi*d)
        rpm = max(float(self.spindle_min_rpm.get()), min(rpm_th, float(self.spindle_max_rpm.get())))
        feed=fz*z*rpm
        self.var_rpm.set(f"{rpm:,.0f}".replace(","," ")); self.var_feed.set(f"{feed:,.0f}".replace(","," "))
        self.var_details.set(f"D={d:.1f} mm  |  z={z}  |  Vc≈{vc:.0f} m/min  |  fz≈{fz:.3f} mm/dent")
        if rpm < rpm_th:
            self.var_limit.set("Limité par Spindle Max RPM")
        elif rpm > rpm_th:
            self.var_limit.set("Forcé au RPM min")
        else:
            self.var_limit.set("")

    # ===== X moves (discret PWM) =====
    def _set_home_x(self): self.home_x_mm.set(self.pos_x_mm.get()); messagebox.showinfo("Home X","Home X mémorisé.")
    def _go_home_x(self):
        delta=self.home_x_mm.get()-self.pos_x_mm.get()
        if abs(delta)<1e-6: return
        self._move_x_discrete(delta, self.jog_speed_x.get())

    def _move_x_discrete(self, dist_mm: float, speed_mm_min: float):
        if getattr(self, "_x_discrete_running", False): return
        steps_per_mm = self.motor_x.steps_per_mm
        target_steps = abs(dist_mm) * steps_per_mm
        if target_steps <= 0 or speed_mm_min <= 0: return
        dir_pos = dist_mm > 0

        def run_pwm():
            self._x_discrete_running = True
            self._x_discrete_stop = False
            try:
                self.motor_x.set_enabled(True); self.motor_x.set_dir(dir_pos)
                dt=0.005; acc_steps=0.0; moved_steps=0.0
                v=0.0; vt=float(speed_mm_min)
                while True:
                    if getattr(self, "_x_discrete_stop", False): break
                    # montée puis maintien vitesse (simple)
                    if v < vt: v = min(v + self.accel_x.get()*dt*60.0, vt)
                    freq=(v/60.0)*steps_per_mm
                    self.motor_x.set_frequency(freq)
                    step_inc = freq*dt
                    acc_steps += step_inc; moved_steps += step_inc
                    while acc_steps >= max(int(round(steps_per_mm*0.1)),1):
                        acc_steps -= max(int(round(steps_per_mm*0.1)),1)
                        self._post_pos_x_delta(0.1 if dir_pos else -0.1)
                    if moved_steps >= target_steps: break
                    time.sleep(dt)
            finally:
                self.motor_x.stop_pwm()
                self.motor_x.set_enabled(False)
                self._x_discrete_running = False
                self._x_discrete_stop = False

        threading.Thread(target=run_pwm, daemon=True).start()

    def _jog_x(self, delta: float): self._move_x_discrete(delta, self.jog_speed_x.get())
    def _stop_x(self):
        if hasattr(self,'axis_x'): self.axis_x.stop()
        if getattr(self, "_x_discrete_running", False): self._x_discrete_stop = True

    # ===== Z hold (PWM) =====
    def _set_home_z(self): self.home_z_mm.set(self.pos_z_mm.get()); messagebox.showinfo("Home Z","Home Z mémorisé.")
    def _go_home_z(self):
        delta=self.home_z_mm.get()-self.pos_z_mm.get()
        if abs(delta)<1e-6: return
        up = delta>0
        self._z_hold(up, speed=self.z_vmax.get())
        t = abs(delta)/max(self.z_vmax.get(),1e-6)*60.0
        self.after(int(t*1000), lambda: self._z_release(up))

    def _z_hold(self, up: bool, speed: Optional[float]=None):
        if getattr(self,'_z_running',False): return
        self._z_running=True
        feed = speed if speed is not None else self.z_vmax.get()
        steps_per_mm=self.motor_z.steps_per_mm
        def run_pwm():
            try:
                self.motor_z.set_enabled(True); self.motor_z.set_dir(up)
                dt=0.005; acc_steps=0.0
                v = max(self.z_vmin.get(), min(feed, self.z_vmax.get()))
                while self._z_running:
                    freq_cmd=(v/60.0)*steps_per_mm
                    freq_eff=self.motor_z.set_frequency(freq_cmd)
                    acc_steps += freq_eff*dt
                    while acc_steps >= max(int(round(steps_per_mm*0.1)),1):
                        acc_steps -= max(int(round(steps_per_mm*0.1)),1)
                        self._post_pos_z_delta(0.1 if up else -0.1)
                    time.sleep(dt)
            finally:
                self.motor_z.stop_pwm()
                self.motor_z.set_enabled(False)
        threading.Thread(target=run_pwm,daemon=True).start()

    def _z_release(self, up: bool): self._z_running=False

    # ===== Encoder (KY-040) via polling pigpio (simple)
    def _setup_encoder(self):
        try:
            clk=self.gpio_cfg["IN"]["ENC_CLK"]["pin"]; dtp=self.gpio_cfg["IN"]["ENC_DT"]["pin"]; sw=self.gpio_cfg["IN"]["ENC_SW"]["pin"]
            for p in (clk,dtp,sw): self.pi.set_mode(p, pigpio.INPUT)  # pas de pull interne
            # Polling léger dans le thread I/O (on lit simplement l'état pour détecter une rotation)
            self._enc_last_a = self.pi.read(clk); self._enc_last_b = self.pi.read(dtp)
            self._enc_clk = clk; self._enc_dt = dtp; self._enc_sw = sw
        except Exception: pass

    # ===== IO monitor & logic =====
    
    def _is_output_active(self, group_key: str) -> bool:
        try:
            cfg=self.gpio_cfg["OUT"][group_key]
            hv = self.pi.read(cfg["pin"]) == 1
            ah = bool(cfg.get("active_high", True))
            return hv if ah else (not hv)
        except Exception:
            return False

    def _apply_outputs_disabled(self):
        try:
            if self.outputs_disabled.get():
                if hasattr(self,'axis_x'): self.axis_x.stop()
                self._z_release(True); self._z_release(False)
                self.motor_x.stop_pwm(); self.motor_z.stop_pwm()
                # high-Z sur OUT
                for key,cfg in self.gpio_cfg["OUT"].items():
                    self.pi.set_mode(cfg["pin"], pigpio.INPUT)
            else:
                self._reinit_gpio_dirs()
        except Exception as e:
            messagebox.showerror("GPIO", f"Erreur bascule sorties: {e}")

    def _apply_gpio_and_rebuild(self):
        for grp in ("OUT","IN"):
            for key, rec in self._gpio_widgets[grp].items():
                self.gpio_cfg[grp][key]["pin"] = int(rec["pin_var"].get())
                self.gpio_cfg[grp][key]["active_high"] = bool(rec["pol_var"].get())
        self._reinit_gpio_dirs()
        self._rebuild_motors_from_state()
        self._setup_encoder()
        messagebox.showinfo("GPIO", "GPIO appliqués et moteurs ré-instanciés.")

    def _apply_mechanics_and_rebuild(self):
        self._rebuild_motors_from_state()
        messagebox.showinfo("Mécanique", "Paramètres mécaniques appliqués et moteurs ré-instanciés.")

    def _reinit_gpio_dirs(self):
        # IN: inputs sans pull interne
        for key,cfg in self.gpio_cfg["IN"].items():
            self.pi.set_mode(cfg["pin"], pigpio.INPUT)
        # OUT: outputs init
        for key,cfg in self.gpio_cfg["OUT"].items():
            self.pi.set_mode(cfg["pin"], pigpio.OUTPUT)
        # EN à HIGH (désactivé), DIR 0, STEP PWM off
        self.pi.write(self.gpio_cfg["OUT"]["X_EN"]["pin"], 1)
        self.pi.write(self.gpio_cfg["OUT"]["Z_EN"]["pin"], 1)
        self.pi.write(self.gpio_cfg["OUT"]["X_DIR"]["pin"], 0)
        self.pi.write(self.gpio_cfg["OUT"]["Z_DIR"]["pin"], 0)
        self.pi.hardware_PWM(self.gpio_cfg["OUT"]["X_STEP"]["pin"], 0, 0)
        self.pi.hardware_PWM(self.gpio_cfg["OUT"]["Z_STEP"]["pin"], 0, 0)

    def _rebuild_motors_from_state(self):
        sx = (max(1, self.steps_rev_x.get()) * max(1, self.microstep_x.get())) / max(self.lead_x.get(), 1e-6)
        sz = (max(1, self.steps_rev_z.get()) * max(1, self.microstep_z.get())) / max(self.lead_z.get(), 1e-6)
        self.motor_x = LowLevelStepper(self.pi, self.gpio_cfg["OUT"]["X_STEP"]["pin"], self.gpio_cfg["OUT"]["X_DIR"]["pin"],
                                       self.gpio_cfg["OUT"]["X_EN"]["pin"], sx, outputs_disabled_callable=lambda: self.outputs_disabled.get())
        self.motor_z = LowLevelStepper(self.pi, self.gpio_cfg["OUT"]["Z_STEP"]["pin"], self.gpio_cfg["OUT"]["Z_DIR"]["pin"],
                                       self.gpio_cfg["OUT"]["Z_EN"]["pin"], sz, outputs_disabled_callable=lambda: self.outputs_disabled.get())
        self.axis_x = AxisContinuous(self.motor_x, self.accel_x.get(), self.decel_x.get(), self._post_pos_x_delta, name="X")

    def _start_io_monitor(self):
        def raw_read(pin):
            try: return self.pi.read(pin) == 1
            except Exception: return False
        def is_active(key):
            cfg=self.gpio_cfg["IN"][key]
            hv = raw_read(cfg["pin"])  # True si haut
            active_high = bool(cfg.get("active_high", True))
            return hv if active_high else (not hv)
        def loop():
            while True:
                try:
                    # encoder (basique)
                    if hasattr(self, "_enc_clk"):
                        a=self.pi.read(self._enc_clk); b=self.pi.read(self._enc_dt)
                        if a != getattr(self, "_enc_last_a", a):
                            delta = 1 if a!=b else -1
                            self.after(0, lambda d=delta: self.jog_speed_x.set(max(30.0, min(10000.0, self.jog_speed_x.get()+d*50.0))))
                        self._enc_last_a=a; self._enc_last_b=b
                        # bouton
                        if self.pi.read(self._enc_sw)==0:
                            self.after(0, lambda: self.jog_speed_x.set(300.0))

                    # push live states to UI
                    if hasattr(self, "_gpio_widgets") and "IN" in self._gpio_widgets:
                        for k, w in self._gpio_widgets["IN"].items():
                            if "state_var" in w:
                                st = "ACTIF" if is_active(k) else "—"
                                self.after(0, lambda v=st, wv=w["state_var"]: wv.set(v))

                    # push live OUT states too
                    if hasattr(self, "_gpio_widgets") and "OUT" in self._gpio_widgets:
                        for k2, w2 in self._gpio_widgets["OUT"].items():
                            if "state_var" in w2:
                                if k2.endswith("_DIR") or k2.endswith("_EN"):
                                    st2 = "ACTIF" if self._is_output_active(k2) else "—"
                                elif k2.endswith("_STEP"):
                                    freq2 = 0.0
                                    if k2.startswith("X_") and hasattr(self, "motor_x"):
                                        freq2 = getattr(self.motor_x, "last_freq_hz", 0.0)
                                    elif k2.startswith("Z_") and hasattr(self, "motor_z"):
                                        freq2 = getattr(self.motor_z, "last_freq_hz", 0.0)
                                    st2 = f"{freq2:.0f} Hz"
                                else:
                                    st2 = "—"
                                self.after(0, lambda v=st2, wv=w2["state_var"]: wv.set(v))

                    manual = is_active("MANUAL") or self.outputs_disabled.get()
                    if manual:
                        if hasattr(self,'axis_x'): self.axis_x.stop()
                        self._z_release(True); self._z_release(False)
                        self.motor_x.stop_pwm(); self.motor_z.stop_pwm()
                    else:
                        xp=is_active("X_PLUS"); xm=is_active("X_MINUS"); xr=is_active("X_RAPID")
                        if xp and not xm:
                            self.axis_x.set_target(True, self.rapid_x_speed.get() if xr else self.jog_speed_x.get()); self.axis_x.start()
                        elif xm and not xp:
                            self.axis_x.set_target(False, self.rapid_x_speed.get() if xr else self.jog_speed_x.get()); self.axis_x.start()
                        else:
                            self.axis_x.set_target(None, 0.0)
                    time.sleep(0.05)
                except Exception:
                    time.sleep(0.1)
        threading.Thread(target=loop, daemon=True).start()

    # ===== Save/Load =====
    def _save_params(self):
        data = {
            "vc_alu_min": float(self.vc_alu_min.get()), "vc_alu_max": float(self.vc_alu_max.get()),
            "vc_acier_min": float(self.vc_acier_min.get()), "vc_acier_max": float(self.vc_acier_max.get()),
            "fz_alu_min": float(self.fz_alu_min.get()), "fz_alu_max": float(self.fz_alu_max.get()),
            "fz_acier_min": float(self.fz_acier_min.get()), "fz_acier_max": float(self.fz_acier_max.get()),
            "spindle_max_rpm": int(self.spindle_max_rpm.get()),
            "spindle_min_rpm": int(self.spindle_min_rpm.get()),
            "jog_speed_x": float(self.jog_speed_x.get()), "rapid_x_speed": float(self.rapid_x_speed.get()),
            "accel_x": float(self.accel_x.get()), "decel_x": float(self.decel_x.get()),
            "z_vmin": float(self.z_vmin.get()), "z_vmax": float(self.z_vmax.get()), "z_accel": float(self.z_accel.get()),
            "gpio_cfg": self.gpio_cfg, "outputs_disabled": bool(self.outputs_disabled.get()),
            "mechanics": {
                "steps_rev_x": int(self.steps_rev_x.get()), "microstep_x": int(self.microstep_x.get()), "lead_x": float(self.lead_x.get()),
                "steps_rev_z": int(self.steps_rev_z.get()), "microstep_z": int(self.microstep_z.get()), "lead_z": float(self.lead_z.get()),
            },
        }
        try:
            with open(self.config_path,"w",encoding="utf-8") as f: json.dump(data,f,indent=2)
            messagebox.showinfo("Sauvegarde", f"Paramètres enregistrés dans\n{self.config_path}")
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'enregistrer : {e}")

    def _load_params(self, silent=False):
        try:
            with open(self.config_path,"r",encoding="utf-8") as f: data=json.load(f)
            self.vc_alu_min.set(float(data.get("vc_alu_min", self.vc_alu_min.get())))
            self.vc_alu_max.set(float(data.get("vc_alu_max", self.vc_alu_max.get())))
            self.vc_acier_min.set(float(data.get("vc_acier_min", self.vc_acier_min.get())))
            self.vc_acier_max.set(float(data.get("vc_acier_max", self.vc_acier_max.get())))
            self.fz_alu_min.set(float(data.get("fz_alu_min", self.fz_alu_min.get())))
            self.fz_alu_max.set(float(data.get("fz_alu_max", self.fz_alu_max.get())))
            self.fz_acier_min.set(float(data.get("fz_acier_min", self.fz_acier_min.get())))
            self.fz_acier_max.set(float(data.get("fz_acier_max", self.fz_acier_max.get())))
            self.spindle_max_rpm.set(int(data.get("spindle_max_rpm", self.spindle_max_rpm.get())))
            self.spindle_min_rpm.set(int(data.get("spindle_min_rpm", self.spindle_min_rpm.get())))
            self.jog_speed_x.set(float(data.get("jog_speed_x", self.jog_speed_x.get())))
            self.rapid_x_speed.set(float(data.get("rapid_x_speed", self.rapid_x_speed.get())))
            self.accel_x.set(float(data.get("accel_x", self.accel_x.get())))
            self.decel_x.set(float(data.get("decel_x", self.decel_x.get())))
            self.z_vmin.set(float(data.get("z_vmin", self.z_vmin.get())))
            self.z_vmax.set(float(data.get("z_vmax", self.z_vmax.get())))
            self.z_accel.set(float(data.get("z_accel", self.z_accel.get())))
            self.outputs_disabled.set(bool(data.get("outputs_disabled", self.outputs_disabled.get())))
            if "gpio_cfg" in data: self.gpio_cfg = data["gpio_cfg"]
            mech=data.get("mechanics", {})
            self.steps_rev_x.set(int(mech.get("steps_rev_x", self.steps_rev_x.get())))
            self.microstep_x.set(int(mech.get("microstep_x", self.microstep_x.get())))
            self.lead_x.set(float(mech.get("lead_x", self.lead_x.get())))
            self.steps_rev_z.set(int(mech.get("steps_rev_z", self.steps_rev_z.get())))
            self.microstep_z.set(int(mech.get("microstep_z", self.microstep_z.get())))
            self.lead_z.set(float(mech.get("lead_z", self.lead_z.get())))
            if not silent: messagebox.showinfo("Chargement","Paramètres chargés.")
        except FileNotFoundError:
            if not silent: messagebox.showwarning("Fichier introuvable","Aucun fichier de paramètres trouvé.")
        except Exception as e:
            if not silent: messagebox.showerror("Erreur", f"Chargement impossible : {e}")

    # ===== Fullscreen helpers =====
    def _toggle_fullscreen(self):
        try:
            is_fs=self.attributes('-fullscreen'); self.attributes('-fullscreen', not is_fs); self.overrideredirect(not is_fs)
        except Exception: pass
    def _end_fullscreen(self):
        try: self.attributes('-fullscreen', False); self.overrideredirect(False)
        except Exception: pass

    def _build_mechanics_tab(self, tab):
        root=ttk.Frame(tab,padding=6); root.pack(fill="both", expand=True)
        x=ttk.Labelframe(root,text="Axe X"); z=ttk.Labelframe(root,text="Axe Z")
        x.grid(row=0,column=0,sticky="nsew",padx=(0,4)); z.grid(row=0,column=1,sticky="nsew",padx=(4,0))
        root.columnconfigure(0,weight=1); root.columnconfigure(1,weight=1)
        ttk.Label(x,text="Steps par tour").grid(row=0,column=0,sticky="e"); self._num_entry(x,self.steps_rev_x,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(x,text="Microstep").grid(row=1,column=0,sticky="e"); self._num_entry(x,self.microstep_x,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Label(x,text="Lead (mm)").grid(row=2,column=0,sticky="e"); self._num_entry(x,self.lead_x,width=8,decimals=2,row=2,column=1,sticky="w")
        ttk.Label(z,text="Steps par tour").grid(row=0,column=0,sticky="e"); self._num_entry(z,self.steps_rev_z,width=8,decimals=0,row=0,column=1,sticky="w")
        ttk.Label(z,text="Microstep").grid(row=1,column=0,sticky="e"); self._num_entry(z,self.microstep_z,width=8,decimals=0,row=1,column=1,sticky="w")
        ttk.Label(z,text="Lead (mm)").grid(row=2,column=0,sticky="e"); self._num_entry(z,self.lead_z,width=8,decimals=2,row=2,column=1,sticky="w")
        rowbtn=ttk.Frame(root); rowbtn.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(6,0))
        ttk.Button(rowbtn,text="Appliquer mécanique (ré-instancier moteurs)",style='Med.TButton',command=self._apply_mechanics_and_rebuild).pack(side=tk.LEFT,expand=True,fill="x",padx=4)

    def destroy(self):
        try:
            self.pi.stop()
        except Exception: pass
        super().destroy()

# ====== Entrée ======
if __name__=="__main__":
    try:
        app=App(); app.mainloop()
    except KeyboardInterrupt:
        pass



# ================== MINI-PATCH v6.4 (IRQ + feed fiable + Z fixe) ==================
SOFTWARE_VERSION = "v6.4"

import re
import math
import tkinter as tk
from tkinter import ttk

# ---------- Utils: logs courts à l'écran (sans casser la mise en page) ----------
def _dbg(self, msg):
    try:
        if not hasattr(self, "_dbg_lbl"):
            # place en bas-droite du conteneur de l'onglet de l'axe (sans modifier layout)
            parent = self
            try:
                # chercher un frame d'onglet mécanique via un bouton X+ s'il existe
                def _walk(w):
                    yield w
                    for c in w.winfo_children():
                        for d in _walk(c): 
                            yield d
                target = None
                for w in _walk(self):
                    if isinstance(w, tk.Button):
                        try: t = w.cget("text")
                        except Exception: t = ""
                        if isinstance(t,str) and ("X" in t and "+" in t):
                            p = w.nametowidget(w.winfo_parent())
                            while p is not None and p is not self:
                                try: parent2 = p.nametowidget(p.winfo_parent())
                                except Exception: parent2 = None
                                if isinstance(parent2, ttk.Notebook): target = p; break
                                p = parent2
                            break
                if target is not None: parent = target
            except Exception: pass
            self._dbg_lbl = tk.Label(parent, text="", font=("TkDefaultFont", 8), fg="#555")
            self._dbg_lbl.place(relx=0.01, rely=0.98, anchor="sw")
        self._dbg_lbl.configure(text=str(msg)[:120])
        self._dbg_lbl.lift()
    except Exception:
        pass
    try: print(f"[DBG] {msg}")
    except Exception: pass

# ---------- Polarité: lire prioritairement la case UI si dispo ----------
def _active_high_for_pin(self, pin: int) -> bool:
    try:
        for k, row in self._gpio_widgets.get("OUT", {}).items():
            if int(row.get("pin")) == int(pin):
                pv = row.get("pol_var")
                if pv is not None:
                    return bool(pv.get())
    except Exception:
        pass
    try:
        for name, cfg in self.gpio_cfg.get("OUT", {}).items():
            if int(cfg.get("pin", -1)) == int(pin):
                return bool(cfg.get("active_high", True))
    except Exception:
        pass
    return True

# ---------- Wrappers LowLevelStepper pour polarité + PWM lissé ----------
try:
    _LL = LowLevelStepper
    _orig_dir = _LL.set_dir
    _orig_en  = _LL.set_enabled
    _orig_fq  = _LL.set_frequency
except Exception:
    _LL = None
    _orig_dir = _orig_en = _orig_fq = None

def _wrap_set_dir(self, positive: bool):
    app = getattr(self, "_owner_app", None)
    if app is None or not hasattr(app, "_active_high_for_pin"):
        return _orig_dir(self, positive) if _orig_dir else None
    ah = bool(app._active_high_for_pin(self.dir_pin))
    level = 1 if (bool(positive) == ah) else 0
    self.pi.write(self.dir_pin, level)
    try: self.last_dir_positive = bool(positive)
    except Exception: pass

def _wrap_set_enabled(self, state: bool):
    try: state = bool(state) and (not self.outputs_disabled_callable())
    except Exception: state = bool(state)
    app = getattr(self, "_owner_app", None)
    if app is None or not hasattr(app, "_active_high_for_pin"):
        return _orig_en(self, state) if _orig_en else None
    ah = bool(app._active_high_for_pin(self.en_pin))
    level = 1 if (state == ah) else 0
    self.enabled = state
    self.pi.write(self.en_pin, level)

def _wrap_set_frequency(self, freq_hz: float):
    if not hasattr(self, "last_freq_hz"): self.last_freq_hz = 0.0
    try: disabled = self.outputs_disabled_callable()
    except Exception: disabled = False
    if disabled or not getattr(self, "enabled", False) or float(freq_hz) <= 0.0:
        if self.last_freq_hz != 0.0:
            self.pi.hardware_PWM(self.step_pin, 0, 0)
            self.last_freq_hz = 0.0
        return 0.0
    f = float(max(1.0, min(200_000.0, float(freq_hz))))
    if abs(f - float(self.last_freq_hz)) < 1.0:
        return float(self.last_freq_hz)
    self.pi.hardware_PWM(self.step_pin, int(f), PWM_DUTY)
    self.last_freq_hz = float(f)
    try:
        name = getattr(self, "name", "")
        if name in ("X","Z"):
            _dbg(getattr(self,"_owner_app",None), f"{name} PWM={f:.0f} Hz")
    except Exception: pass
    return float(self.last_freq_hz)

if _LL:
    if _orig_dir: _LL.set_dir = _wrap_set_dir
    if _orig_en:  _LL.set_enabled = _wrap_set_enabled
    if _orig_fq:  _LL.set_frequency = _wrap_set_frequency

# ---------- Lecture robuste de l'avance "Outils" ----------
def _tools_feed_mm_min(self) -> float:
    # 1) calcul direct si variables existent
    try:
        d = float(self.selected_diameter.get())
        z = int(self.selected_teeth.get())
        mat = self.selected_material.get()
        if d > 0 and z > 0:
            dmin, dmax = 2.0, 20.0
            if mat == "alu":
                vc = interp_ascending(d, dmin, dmax, self.vc_alu_min.get(),   self.vc_alu_max.get())
                fz = interp_ascending(d, dmin, dmax, self.fz_alu_min.get(),   self.fz_alu_max.get())
            else:
                vc = interp_ascending(d, dmin, dmax, self.vc_acier_min.get(), self.vc_acier_max.get())
                fz = interp_ascending(d, dmin, dmax, self.fz_acier_min.get(), self.fz_acier_max.get())
            rpm_th = (1000.0 * vc) / (math.pi * d)
            rpm = max(float(self.spindle_min_rpm.get()), min(rpm_th, float(self.spindle_max_rpm.get())))
            v = float(fz * z * rpm)
            _dbg(self, f"Feed (calc) ≈ {v:.0f} mm/min")
            return v
    except Exception:
        pass
    # 2) scan widgets pour trouver un label/entry contenant 'mm/min'
    try:
        def _walk(w):
            yield w
            for c in w.winfo_children():
                for d in _walk(c): 
                    yield d
        best = None
        for w in _walk(self):
            if isinstance(w, (tk.Label, ttk.Label, tk.Entry)):
                try:
                    txt = w.cget("text") if not isinstance(w, tk.Entry) else w.get()
                except Exception:
                    txt = ""
                if not isinstance(txt, str): 
                    continue
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mm\s*/\s*min", txt.replace(",", ".").lower())
                if m:
                    val = float(m.group(1))
                    best = val
        if best is not None:
            _dbg(self, f"Feed (UI) ≈ {best:.0f} mm/min")
            return float(best)
    except Exception:
        pass
    # 3) secours: jog_speed_x si dispo
    try:
        v = float(self.jog_speed_x.get()); _dbg(self, f"Feed (fallback jog) {v:.0f} mm/min"); return v
    except Exception:
        pass
    _dbg(self, "Feed introuvable → 100 mm/min")
    return 100.0

# ---------- Actions axes : utilisent TOUJOURS feed outils à l'appui ----------
def _x_apply_from_tools(self, dir_positive: bool):
    v = float(self.rapid_x_speed.get()) if bool(getattr(self, "_x_rapid_active", False)) else float(self._tools_feed_mm_min())
    s = float(self.motor_x.steps_per_mm)
    self.motor_x.set_enabled(True)
    self.motor_x.set_dir(bool(dir_positive))
    self.motor_x.set_frequency((v/60.0)*s)

def _z_apply_like_buttons(self, up: bool):
    try: v = float(self.z_vmax.get())
    except Exception: v = 1000.0
    s = float(self.motor_z.steps_per_mm)
    self.motor_z.set_enabled(True)
    self.motor_z.set_dir(bool(up))
    self.motor_z.set_frequency((v/60.0)*s)

def _stop_x(self):
    try: self.motor_x.stop_pwm()
    except Exception: pass

def _stop_z(self):
    try: self.motor_z.stop_pwm()
    except Exception: pass

# ---------- Fuzzy mapping des entrées (tolère Z+, ZPLUS, etc.) ----------
def _find_in_key(self, *cands):
    IN = self.gpio_cfg.get("IN", {})
    if not IN: return None
    keys = list(IN.keys())
    low = {k.lower(): k for k in keys}
    for c in cands:
        if c in IN: return c
        if c.lower() in low: return low[c.lower()]
    # heuristique : tokens
    tokens = [c.lower() for c in cands]
    for k in keys:
        kl = k.lower()
        ok = True
        for t in tokens:
            if t not in kl:
                ok = False; break
        if ok: return k
    return None

# ---------- IRQ strictes ----------
def _setup_irq_inputs(self):
    def is_active_from_level(key, level):
        if level == 2: return None
        cfg = self.gpio_cfg["IN"][key]
        ah  = bool(cfg.get("active_high", True))
        return (level == 1) if ah else (level == 0)
    def add_cb(key, func, glitch_us=3000):
        if key is None: return
        pin = self.gpio_cfg["IN"][key]["pin"]
        try:
            self.pi.set_mode(pin, pigpio.INPUT)
            if hasattr(self.pi, "set_glitch_filter"): self.pi.set_glitch_filter(pin, glitch_us)
        except Exception: pass
        try: self.pi.callback(pin, pigpio.EITHER_EDGE, func)
        except Exception: pass

    Xp = _find_in_key(self, "X_PLUS","X+","XPLUS","PLUS","+","xp")
    Xm = _find_in_key(self, "X_MINUS","X-","XMINUS","MINUS","-","xm")
    Zp = _find_in_key(self, "Z_PLUS","Z+","ZPLUS","PLUS","+","zp")
    Zm = _find_in_key(self, "Z_MINUS","Z-","ZMINUS","MINUS","-","zm")
    Xr = _find_in_key(self, "X_RAPID","RAPID","RAPIDE")
    Man= _find_in_key(self, "MANUAL","MANUEL")

    def _x_plus_cb(pin, level, tick):
        a = is_active_from_level(Xp, level) if Xp else None
        if a is None: return
        if a: _dbg(self, "GPIO X+"); self._x_apply_from_tools(True)
        else:
            other = is_active_from_level(Xm, self.pi.read(self.gpio_cfg["IN"][Xm]["pin"])) if Xm else False
            if not other: self._stop_x()
    def _x_minus_cb(pin, level, tick):
        a = is_active_from_level(Xm, level) if Xm else None
        if a is None: return
        if a: _dbg(self, "GPIO X-"); self._x_apply_from_tools(False)
        else:
            other = is_active_from_level(Xp, self.pi.read(self.gpio_cfg["IN"][Xp]["pin"])) if Xp else False
            if not other: self._stop_x()
    def _z_plus_cb(pin, level, tick):
        a = is_active_from_level(Zp, level) if Zp else None
        if a is None: return
        if a: _dbg(self, "GPIO Z+"); self._z_apply_like_buttons(True)
        else:
            other = is_active_from_level(Zm, self.pi.read(self.gpio_cfg["IN"][Zm]["pin"])) if Zm else False
            if not other: self._stop_z()
    def _z_minus_cb(pin, level, tick):
        a = is_active_from_level(Zm, level) if Zm else None
        if a is None: return
        if a: _dbg(self, "GPIO Z-"); self._z_apply_like_buttons(False)
        else:
            other = is_active_from_level(Zp, self.pi.read(self.gpio_cfg["IN"][Zp]["pin"])) if Zp else False
            if not other: self._stop_z()
    def _x_rapid_cb(pin, level, tick):
        a = is_active_from_level(Xr, level) if Xr else None
        if a is None: return
        self._x_rapid_active = bool(a)
        _dbg(self, f"RAPID={'ON' if a else 'OFF'}")
        ap = is_active_from_level(Xp, self.pi.read(self.gpio_cfg["IN"][Xp]["pin"])) if Xp else False
        am = is_active_from_level(Xm, self.pi.read(self.gpio_cfg["IN"][Xm]["pin"])) if Xm else False
        if ap and not am: self._x_apply_from_tools(True)
        elif am and not ap: self._x_apply_from_tools(False)
        elif not ap and not am: self._stop_x()
    def _manual_cb(pin, level, tick):
        a = is_active_from_level(Man, level) if Man else None
        if a is None: return
        if a: _dbg(self, "MANUAL ON → STOP"); self._stop_x(); self._stop_z()

    add_cb(Xp, _x_plus_cb);  add_cb(Xm, _x_minus_cb)
    add_cb(Zp, _z_plus_cb);  add_cb(Zm, _z_minus_cb)
    add_cb(Xr, _x_rapid_cb); add_cb(Man, _manual_cb)

# ---------- Label version, discret sous les boutons ----------
def _place_version_label(self):
    try:
        if not hasattr(self, "_version_label"):
            self._version_label = tk.Label(self, text=f"v{SOFTWARE_VERSION}", font=("TkDefaultFont", 9, "bold"), fg="#444")
        # en overlay global (suffit pour le voir), ne change pas les layouts existants
        self._version_label.place(relx=1.0, rely=1.0, x=-8, y=-8, anchor="se")
        self._version_label.lift()
    except Exception:
        pass

# ---------- Resync polarités régulièrement (assure effet checkbox immédiat) ----------
def _resync_polarity_periodic(self):
    try:
        # réapplique l'état courant selon les nouvelles polarités
        for axis in ("motor_x","motor_z"):
            m = getattr(self, axis, None)
            if not m: continue
            try:
                m.set_enabled(getattr(m, "enabled", True))
                m.set_dir(getattr(m, "last_dir_positive", True))
                m.set_frequency(getattr(m, "last_freq_hz", 0.0))
            except Exception: pass
    finally:
        try: self.after(800, lambda: _resync_polarity_periodic(self))
        except Exception: pass

# ---------- Brancher sans toucher ta construction UI ----------
_old_init = App.__init__
def _patched_init(self, *a, **k):
    _old_init(self, *a, **k)
    try:
        App._active_high_for_pin = _active_high_for_pin
        App._tools_feed_mm_min   = _tools_feed_mm_min
        App._x_apply_from_tools  = _x_apply_from_tools
        App._z_apply_like_buttons= _z_apply_like_buttons
        App._stop_x              = _stop_x
        App._stop_z              = _stop_z
        App._setup_irq_inputs    = _setup_irq_inputs
        App._place_version_label = _place_version_label
        App._resync_polarity     = _resync_polarity_periodic
        App._dbg                 = _dbg
        if hasattr(self, "motor_x"): setattr(self.motor_x, "_owner_app", self); setattr(self.motor_x, "name", "X")
        if hasattr(self, "motor_z"): setattr(self.motor_z, "_owner_app", self); setattr(self.motor_z, "name", "Z")
    except Exception:
        pass
    try: self._x_rapid_active = False
    except Exception: pass
    try: self._setup_irq_inputs()
    except Exception: pass
    try: self.after(0, lambda: self._place_version_label())
    except Exception: pass
    try: self.after(300, lambda: _resync_polarity_periodic(self))
    except Exception: pass
    _dbg(self, "Patch v6.4 chargé")

App.__init__ = _patched_init
# ================== FIN MINI-PATCH v6.4 ==================
