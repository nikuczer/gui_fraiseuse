#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MotorController — Daemon thread pour piloter les moteurs pas-a-pas
de la fraiseuse VM32L via pigpio wave_chain.

Architecture:
  GUI (main thread) ──cmd_queue──> MotorController (daemon thread)
                     <──status_queue──  (pigpio wave_chain)

Commandes: MOVE_X, MOVE_Z_START, MOVE_Z_STOP, STOP, SET_FEED, SHUTDOWN
Status:    POSITION, MOVE_DONE, ERROR, STOPPED

Sans pigpio (PC): mode simulation, compte les steps sans bouger.
"""

import math
import os
import json
import time
import threading
import queue
from enum import Enum, auto

try:
    import pigpio
    HAS_PIGPIO = True
except ImportError:
    HAS_PIGPIO = False

GPIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpio_mapping.json")

# ── Constantes mécaniques par défaut ──
DEFAULT_STEPS_REV = 200
DEFAULT_MICROSTEP = 16
DEFAULT_LEAD_MM = 5.0
DEFAULT_ACCEL = 300.0     # mm/s²
DEFAULT_DECEL = 300.0     # mm/s²
DEFAULT_RAPID = 1500.0    # mm/min
V_MIN_MM_S = 0.5          # vitesse min démarrage (évite 0 Hz)
MAX_WAVE_LOOP = 65535     # max loop count wave_chain (2 bytes)
N_RAMP_STEPS = 10         # paliers par rampe


# ════════════════════════════════════════════
#  Commandes et statuts
# ════════════════════════════════════════════

class CmdType(Enum):
    MOVE_X = auto()         # (distance_mm, feed_mm_min) — profil trapézoïdal
    MOVE_Z_START = auto()   # (direction: +1/-1, speed_mm_min) — hold-to-move
    MOVE_Z_STOP = auto()    # arrêter Z
    STOP = auto()           # arrêt immédiat (e-stop software)
    SET_FEED = auto()       # (feed_mm_min,) — jog wheel
    SHUTDOWN = auto()       # arrêter le thread proprement


class StatusType(Enum):
    POSITION = auto()       # (axis, pos_steps)
    MOVE_DONE = auto()      # (axis, steps_done)
    STOPPED = auto()        # (axis, reason)
    ERROR = auto()          # (message,)


class Cmd:
    __slots__ = ('type', 'args')

    def __init__(self, cmd_type, *args):
        self.type = cmd_type
        self.args = args


class Status:
    __slots__ = ('type', 'args')

    def __init__(self, status_type, *args):
        self.type = status_type
        self.args = args


# ════════════════════════════════════════════
#  Profil trapézoïdal (repris de test_trapeze.py)
# ════════════════════════════════════════════

def build_trapezoid(distance_mm, feed_mm_min, accel_mm_s2, decel_mm_s2,
                    steps_per_mm, n_paliers=N_RAMP_STEPS):
    """Construit un profil trapézoïdal. Retourne [(freq_hz, nb_steps), ...]."""
    if distance_mm <= 0 or feed_mm_min <= 0:
        return []

    total_steps = round(distance_mm * steps_per_mm)
    v_cruise = feed_mm_min / 60.0
    f_cruise = v_cruise * steps_per_mm
    v_min = V_MIN_MM_S
    f_min = v_min * steps_per_mm

    d_accel_mm = (v_cruise**2 - v_min**2) / (2 * accel_mm_s2)
    d_decel_mm = (v_cruise**2 - v_min**2) / (2 * decel_mm_s2)

    if v_cruise <= v_min:
        d_accel_mm = 0
        d_decel_mm = 0
    elif d_accel_mm + d_decel_mm > distance_mm:
        ratio = decel_mm_s2 / (accel_mm_s2 + decel_mm_s2)
        d_accel_mm = distance_mm * ratio
        d_decel_mm = distance_mm - d_accel_mm
        v_peak_sq = v_min**2 + 2 * accel_mm_s2 * d_accel_mm
        if v_peak_sq <= v_min**2:
            d_accel_mm = 0
            d_decel_mm = 0
            v_cruise = v_min
        else:
            v_cruise = math.sqrt(v_peak_sq)
        f_cruise = v_cruise * steps_per_mm

    d_cruise_mm = distance_mm - d_accel_mm - d_decel_mm
    if d_cruise_mm < 0:
        d_cruise_mm = 0

    steps_accel = round(d_accel_mm * steps_per_mm)
    steps_decel = round(d_decel_mm * steps_per_mm)
    steps_cruise = total_steps - steps_accel - steps_decel
    if steps_cruise < 0:
        steps_cruise = 0
        steps_accel = round(total_steps * (d_accel_mm / max(d_accel_mm + d_decel_mm, 1e-9)))
        steps_decel = total_steps - steps_accel

    actual_n_accel = min(n_paliers, steps_accel) if steps_accel > 0 else 0
    actual_n_decel = min(n_paliers, steps_decel) if steps_decel > 0 else 0

    ramp = []

    if steps_accel > 0 and actual_n_accel > 0:
        spp = max(1, steps_accel // actual_n_accel)
        for i in range(actual_n_accel):
            t = (i + 1) / actual_n_accel
            freq = f_min + (f_cruise - f_min) * t
            s = spp if i < actual_n_accel - 1 else (steps_accel - spp * (actual_n_accel - 1))
            if s > 0:
                ramp.append((round(freq, 1), s))

    remaining = steps_cruise
    while remaining > 0:
        chunk = min(remaining, MAX_WAVE_LOOP)
        ramp.append((round(f_cruise, 1), chunk))
        remaining -= chunk

    if steps_decel > 0 and actual_n_decel > 0:
        spp = max(1, steps_decel // actual_n_decel)
        for i in range(actual_n_decel):
            t = 1.0 - (i + 1) / actual_n_decel
            freq = f_min + (f_cruise - f_min) * t
            s = spp if i < actual_n_decel - 1 else (steps_decel - spp * (actual_n_decel - 1))
            if s > 0:
                ramp.append((round(freq, 1), s))

    return ramp


# ════════════════════════════════════════════
#  MotorController
# ════════════════════════════════════════════

class MotorController(threading.Thread):
    """
    Daemon thread qui pilote les axes X et Z via pigpio wave_chain.
    En mode simulation (pas de pigpio), compte les steps sans matériel.
    """

    def __init__(self, cmd_queue, status_queue, config=None):
        super().__init__(daemon=True, name="MotorController")
        self.cmd_queue = cmd_queue
        self.status_queue = status_queue
        self.e_stop = threading.Event()

        # Config mécanique
        cfg = config or {}
        self.steps_per_mm_x = self._calc_spmm(cfg, 'x')
        self.steps_per_mm_z = self._calc_spmm(cfg, 'z')
        self.accel_x = cfg.get('accel_x', DEFAULT_ACCEL)
        self.decel_x = cfg.get('decel_x', DEFAULT_DECEL)
        self.rapid_x = cfg.get('rapid_x_speed', DEFAULT_RAPID)
        self.z_vmin = cfg.get('z_vmin', 50.0)
        self.z_vmax = cfg.get('z_vmax', 500.0)
        self.z_accel = cfg.get('z_accel', DEFAULT_ACCEL)

        # Position en steps (source de vérité)
        self.pos_x_steps = cfg.get('pos_x_steps', 0)
        self.pos_z_steps = 0

        # GPIO pins (depuis gpio_mapping.json)
        self.gpio = self._load_gpio()
        self.step_pin_x = self.gpio.get('step_x', {}).get('gpio')
        self.dir_pin_x = self.gpio.get('dir_x', {}).get('gpio')
        self.enable_pin_x = self.gpio.get('enable_x', {}).get('gpio')
        self.step_pin_z = self.gpio.get('step_z', {}).get('gpio')
        self.dir_pin_z = self.gpio.get('dir_z', {}).get('gpio')
        self.enable_pin_z = self.gpio.get('enable_z', {}).get('gpio')

        # pigpio
        self.pi = None
        self.simulation = not HAS_PIGPIO

        # Z hold-to-move state
        self._z_running = False

    @staticmethod
    def _calc_spmm(cfg, axis):
        sr = cfg.get(f'steps_rev_{axis}', DEFAULT_STEPS_REV)
        ms = cfg.get(f'microstep_{axis}', DEFAULT_MICROSTEP)
        lead = cfg.get(f'lead_{axis}', DEFAULT_LEAD_MM)
        return (sr * ms) / max(lead, 1e-9)

    @staticmethod
    def _load_gpio():
        try:
            with open(GPIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # ── Démarrage ──
    def run(self):
        """Boucle principale du thread moteur."""
        self._init_hardware()
        print(f"[MotorController] Demarr{'e (SIMULATION)' if self.simulation else 'e (pigpio)'}"
              f" — X: {self.steps_per_mm_x:.0f} steps/mm, Z: {self.steps_per_mm_z:.0f} steps/mm")

        while True:
            try:
                cmd = self.cmd_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if cmd.type == CmdType.SHUTDOWN:
                break
            elif cmd.type == CmdType.STOP:
                self._emergency_stop()
            elif cmd.type == CmdType.MOVE_X:
                self._move_x(cmd.args[0], cmd.args[1])
            elif cmd.type == CmdType.MOVE_Z_START:
                self._z_start(cmd.args[0], cmd.args[1])
            elif cmd.type == CmdType.MOVE_Z_STOP:
                self._z_stop()
            elif cmd.type == CmdType.SET_FEED:
                pass  # réservé pour le jog wheel

        self._cleanup()
        print("[MotorController] Arrete.")

    # ── Hardware init ──
    def _init_hardware(self):
        if not HAS_PIGPIO:
            self.simulation = True
            return
        try:
            self.pi = pigpio.pi()
            if not self.pi.connected:
                print("[MotorController] pigpio non connecte -> simulation")
                self.simulation = True
                return
            self.simulation = False
            # Setup pins
            for pin in [self.step_pin_x, self.step_pin_z]:
                if pin is not None:
                    self.pi.set_mode(pin, pigpio.OUTPUT)
                    self.pi.write(pin, 0)
            for pin in [self.dir_pin_x, self.dir_pin_z]:
                if pin is not None:
                    self.pi.set_mode(pin, pigpio.OUTPUT)
            for pin in [self.enable_pin_x, self.enable_pin_z]:
                if pin is not None:
                    self.pi.set_mode(pin, pigpio.OUTPUT)
                    self.pi.write(pin, 0)  # enable (active low typiquement)
        except Exception as e:
            print(f"[MotorController] Erreur pigpio init: {e} -> simulation")
            self.simulation = True

    def _cleanup(self):
        if self.pi and not self.simulation:
            self.pi.wave_tx_stop()
            self.pi.wave_clear()
            # Disable drivers
            for pin in [self.enable_pin_x, self.enable_pin_z]:
                if pin is not None:
                    self.pi.write(pin, 1)  # disable
            self.pi.stop()

    # ════════════════════════════════════════════
    #  MOVE X (profil trapézoïdal)
    # ════════════════════════════════════════════

    def _move_x(self, distance_mm, feed_mm_min):
        """Déplacement X avec profil trapézoïdal."""
        if self.e_stop.is_set():
            return
        if abs(distance_mm) < 1e-6:
            self.status_queue.put(Status(StatusType.MOVE_DONE, 'x', 0))
            return

        direction = 1 if distance_mm > 0 else -1
        abs_dist = abs(distance_mm)

        ramp = build_trapezoid(
            abs_dist, feed_mm_min,
            self.accel_x, self.decel_x,
            self.steps_per_mm_x
        )
        if not ramp:
            self.status_queue.put(Status(StatusType.MOVE_DONE, 'x', 0))
            return

        total_steps = sum(s for _, s in ramp)

        if self.simulation:
            # Simulation : compter les steps, simuler le temps
            for freq, steps in ramp:
                if self.e_stop.is_set():
                    break
                t = steps / freq
                time.sleep(min(t, 0.05))  # accélérer la simu
            self.pos_x_steps += direction * total_steps
            self.status_queue.put(Status(StatusType.MOVE_DONE, 'x', direction * total_steps))
            self.status_queue.put(Status(StatusType.POSITION, 'x', self.pos_x_steps))
            return

        # ── Exécution réelle pigpio ──
        if self.dir_pin_x is not None:
            self.pi.write(self.dir_pin_x, 0 if direction > 0 else 1)
            time.sleep(0.001)  # settle dir pin

        if self.step_pin_x is None:
            self.status_queue.put(Status(StatusType.ERROR, "step_x pin non configure"))
            return

        self._execute_wave_chain(self.step_pin_x, ramp)

        # Attendre fin d'exécution
        steps_done = self._wait_wave_done(total_steps)
        self.pos_x_steps += direction * steps_done
        self.status_queue.put(Status(StatusType.MOVE_DONE, 'x', direction * steps_done))
        self.status_queue.put(Status(StatusType.POSITION, 'x', self.pos_x_steps))

    # ════════════════════════════════════════════
    #  MOVE Z (hold-to-move, boucle infinie)
    # ════════════════════════════════════════════

    def _z_start(self, direction, speed_mm_min):
        """Démarre Z en boucle infinie (hold-to-move)."""
        if self.e_stop.is_set():
            return
        if self._z_running:
            return
        self._z_running = True
        speed = max(self.z_vmin, min(self.z_vmax, speed_mm_min))
        freq = (speed / 60.0) * self.steps_per_mm_z
        freq = max(10, freq)  # min 10 Hz

        if self.simulation:
            # Simulation : boucle dans un sous-thread
            def sim_z():
                step_dir = 1 if direction > 0 else -1
                steps_per_tick = max(1, int(freq * 0.02))  # ~50 Hz update
                while self._z_running and not self.e_stop.is_set():
                    self.pos_z_steps += step_dir * steps_per_tick
                    self.status_queue.put(Status(StatusType.POSITION, 'z', self.pos_z_steps))
                    time.sleep(0.02)
                self.status_queue.put(Status(StatusType.STOPPED, 'z', 'released'))
            threading.Thread(target=sim_z, daemon=True).start()
            return

        # ── Réel : wave_chain avec boucle infinie ──
        if self.dir_pin_z is not None:
            self.pi.write(self.dir_pin_z, 0 if direction > 0 else 1)
            time.sleep(0.001)

        if self.step_pin_z is None:
            self.status_queue.put(Status(StatusType.ERROR, "step_z pin non configure"))
            self._z_running = False
            return

        self.pi.wave_clear()
        micros = int(500000 / freq)
        micros = max(5, micros)  # min 5µs (pigpio limite)
        self.pi.wave_add_generic([
            pigpio.pulse(1 << self.step_pin_z, 0, micros),
            pigpio.pulse(0, 1 << self.step_pin_z, micros),
        ])
        wid = self.pi.wave_create()
        # Boucle infinie : 255, 0, wid, 255, 3
        self.pi.wave_chain([255, 0, wid, 255, 3])

        # Thread pour compter les steps et surveiller l'arrêt
        def count_z():
            step_dir = 1 if direction > 0 else -1
            while self._z_running and not self.e_stop.is_set():
                # Estimer les steps basé sur le temps
                self.pos_z_steps += step_dir * int(freq * 0.02)
                self.status_queue.put(Status(StatusType.POSITION, 'z', self.pos_z_steps))
                time.sleep(0.02)
            # Arrêter la wave
            self.pi.wave_tx_stop()
            self.pi.wave_clear()
            self.status_queue.put(Status(StatusType.STOPPED, 'z', 'released'))
        threading.Thread(target=count_z, daemon=True).start()

    def _z_stop(self):
        """Arrêter Z (relâchement bouton)."""
        self._z_running = False

    # ════════════════════════════════════════════
    #  WAVE CHAIN EXECUTION
    # ════════════════════════════════════════════

    def _execute_wave_chain(self, step_pin, ramp):
        """Crée les waves et lance la chain. UNE SEULE chain active à la fois."""
        self.pi.wave_clear()
        wids = []
        for freq, steps in ramp:
            micros = int(500000 / freq)
            micros = max(5, micros)
            self.pi.wave_add_generic([
                pigpio.pulse(1 << step_pin, 0, micros),
                pigpio.pulse(0, 1 << step_pin, micros),
            ])
            wids.append(self.pi.wave_create())

        chain = []
        for wid, (_, steps) in zip(wids, ramp):
            x = steps & 0xFF
            y = (steps >> 8) & 0xFF
            chain += [255, 0, wid, 255, 1, x, y]

        self.pi.wave_chain(chain)

    def _wait_wave_done(self, expected_steps):
        """Attend la fin de la wave_chain. Retourne le nombre de steps effectués."""
        steps_done = 0
        while self.pi.wave_tx_busy():
            if self.e_stop.is_set():
                self.pi.wave_tx_stop()
                # Estimer les steps déjà faits (approximatif)
                # pigpio ne donne pas le count exact des pulses émises
                # On pourrait utiliser wave_get_cbs() mais c'est complexe
                # Pour l'instant on considère qu'on ne sait pas
                self.pi.wave_clear()
                self.status_queue.put(Status(StatusType.STOPPED, 'x', 'e-stop'))
                return steps_done  # approximatif
            time.sleep(0.01)
        steps_done = expected_steps  # si pas interrompu, tout est fait
        self.pi.wave_clear()
        return steps_done

    # ════════════════════════════════════════════
    #  E-STOP
    # ════════════════════════════════════════════

    def _emergency_stop(self):
        """Arrêt immédiat de tout mouvement. Appelé directement, PAS via la queue."""
        self.e_stop.set()
        self._z_running = False
        if self.pi and not self.simulation:
            self.pi.wave_tx_stop()
            self.pi.wave_clear()
        # Vider la queue de commandes
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except queue.Empty:
                break
        self.status_queue.put(Status(StatusType.STOPPED, 'all', 'e-stop'))

    def reset_estop(self):
        """Réarmer après e-stop (appelé depuis le GUI)."""
        self.e_stop.clear()

    # ════════════════════════════════════════════
    #  API publique (appelée depuis le GUI thread)
    # ════════════════════════════════════════════

    def emergency_stop(self):
        """E-stop depuis n'importe quel thread. NE PASSE PAS par la queue."""
        self._emergency_stop()

    def send(self, cmd_type, *args):
        """Envoyer une commande au motor thread."""
        self.cmd_queue.put(Cmd(cmd_type, *args))

    def get_pos_x_mm(self):
        return self.pos_x_steps / self.steps_per_mm_x

    def get_pos_z_mm(self):
        return self.pos_z_steps / self.steps_per_mm_z


# ════════════════════════════════════════════
#  GPIO Callbacks (pupitre physique)
# ════════════════════════════════════════════

class PupitreCallbacks:
    """
    Enregistre les callbacks pigpio pour les boutons du pupitre.
    Envoie les commandes dans la cmd_queue du MotorController.
    """

    def __init__(self, pi, motor_ctrl, gpio_mapping):
        self.pi = pi
        self.motor = motor_ctrl
        self.mapping = gpio_mapping
        self.callbacks = []
        self._x_dir = 0  # -1 gauche, 0 neutre, +1 droite
        self._rapid = False

    def setup(self):
        if not self.pi or not self.pi.connected:
            return

        for action, info in self.mapping.items():
            gpio = info.get('gpio')
            active_low = info.get('active_low', False)
            if gpio is None:
                continue

            self.pi.set_mode(gpio, pigpio.INPUT)
            self.pi.set_pull_up_down(gpio, pigpio.PUD_UP)

            # Callback sur changement d'état
            edge = pigpio.EITHER_EDGE
            cb = self.pi.callback(gpio, edge,
                                  self._make_handler(action, gpio, active_low))
            self.callbacks.append(cb)

    def _make_handler(self, action, gpio, active_low):
        def handler(pin, level, tick):
            pressed = (level == 0) if active_low else (level == 1)
            self._on_button(action, pressed)
        return handler

    def _on_button(self, action, pressed):
        if action == 'x_gauche':
            self._x_dir = -1 if pressed else 0
            self._update_x()
        elif action == 'x_droite':
            self._x_dir = 1 if pressed else 0
            self._update_x()
        elif action == 'rapide_x':
            self._rapid = pressed
            self._update_x()
        elif action == 'z_haut':
            if pressed:
                self.motor.send(CmdType.MOVE_Z_START, 1, self.motor.z_vmax)
            else:
                self.motor.send(CmdType.MOVE_Z_STOP)
        elif action == 'z_bas':
            if pressed:
                self.motor.send(CmdType.MOVE_Z_START, -1, self.motor.z_vmax)
            else:
                self.motor.send(CmdType.MOVE_Z_STOP)
        elif action == 'disable':
            if pressed:
                self.motor.emergency_stop()

    def _update_x(self):
        """Envoie un move X continu en petits incréments (le GUI s'occupe du clamp)."""
        # Pour le commutateur X : on envoie des petits moves
        # Le MotorController les exécute séquentiellement
        # On ne peut pas faire de boucle infinie comme Z car X a un profil trapézoïdal
        # → Le GUI doit gérer le hold-to-move X via un polling du commutateur
        pass

    def cleanup(self):
        for cb in self.callbacks:
            cb.cancel()
        self.callbacks.clear()


# ════════════════════════════════════════════
#  Factory
# ════════════════════════════════════════════

def create_motor_system(config=None):
    """Crée la cmd_queue, status_queue, et le MotorController. Retourne (motor, cmd_q, status_q)."""
    cmd_q = queue.Queue(maxsize=100)
    status_q = queue.Queue(maxsize=200)
    motor = MotorController(cmd_q, status_q, config)
    return motor, cmd_q, status_q
