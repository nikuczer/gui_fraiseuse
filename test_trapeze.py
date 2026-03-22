#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test / simulation de génération de profil trapézoïdal pour stepper.
Vérifie que les calculs de rampe (accel → cruise → decel) sont corrects
avant de passer sur pigpio wave_chain réel.

Mécanique VM32L : 200 steps/tour × 16 microsteps ÷ 5mm lead = 640 steps/mm
"""

import math


# ── Paramètres mécaniques ──
STEPS_REV = 200
MICROSTEP = 16
LEAD_MM = 5.0
STEPS_PER_MM = (STEPS_REV * MICROSTEP) / LEAD_MM  # 640


def build_trapezoid(distance_mm, feed_mm_min, accel_mm_s2, decel_mm_s2, n_paliers=10):
    """
    Construit un profil trapézoïdal pour un déplacement donné.

    Retourne une liste de paliers : [(freq_hz, nb_steps), ...]
    qui correspond exactement à ce qu'on passerait à generate_ramp / wave_chain.

    Args:
        distance_mm:   distance à parcourir (>0)
        feed_mm_min:   vitesse de croisière (mm/min)
        accel_mm_s2:   accélération (mm/s²)
        decel_mm_s2:   décélération (mm/s²)
        n_paliers:     nombre de paliers par rampe (accel et decel)

    Returns:
        list of (freq_hz, nb_steps) tuples
    """
    if distance_mm <= 0 or feed_mm_min <= 0:
        return []

    total_steps = round(distance_mm * STEPS_PER_MM)
    v_cruise = feed_mm_min / 60.0       # mm/s
    f_cruise = v_cruise * STEPS_PER_MM  # Hz max

    # Vitesse min (démarrage) — on ne peut pas partir de 0 Hz en wave_chain
    v_min = 0.5  # mm/s (= 320 Hz, raisonnable pour démarrer)
    f_min = v_min * STEPS_PER_MM

    # ── Calcul des distances de rampe ──
    # d_accel = (v² - v_min²) / (2 * a)
    d_accel_mm = (v_cruise**2 - v_min**2) / (2 * accel_mm_s2)
    d_decel_mm = (v_cruise**2 - v_min**2) / (2 * decel_mm_s2)

    # Si v_cruise <= v_min, pas besoin de rampe
    if v_cruise <= v_min:
        d_accel_mm = 0
        d_decel_mm = 0
    # Si la distance est trop courte pour atteindre v_cruise → profil triangulaire
    elif d_accel_mm + d_decel_mm > distance_mm:
        # Recalculer la vitesse max atteignable
        # d_accel/d_decel = decel/accel (ratio inverse des accélérations)
        ratio = decel_mm_s2 / (accel_mm_s2 + decel_mm_s2)
        d_accel_mm = distance_mm * ratio
        d_decel_mm = distance_mm - d_accel_mm
        v_peak_sq = v_min**2 + 2 * accel_mm_s2 * d_accel_mm
        if v_peak_sq <= v_min**2:
            # Distance trop courte même pour accélérer, aller à v_min
            d_accel_mm = 0
            d_decel_mm = 0
            v_cruise = v_min
        else:
            v_cruise = math.sqrt(v_peak_sq)
        f_cruise = v_cruise * STEPS_PER_MM

    d_cruise_mm = distance_mm - d_accel_mm - d_decel_mm
    if d_cruise_mm < 0:
        d_cruise_mm = 0

    steps_accel = round(d_accel_mm * STEPS_PER_MM)
    steps_decel = round(d_decel_mm * STEPS_PER_MM)
    steps_cruise = total_steps - steps_accel - steps_decel
    if steps_cruise < 0:
        steps_cruise = 0
        steps_accel = round(total_steps * (d_accel_mm / max(d_accel_mm + d_decel_mm, 1e-9)))
        steps_decel = total_steps - steps_accel

    # Adapter le nombre de paliers aux steps disponibles
    actual_n_accel = min(n_paliers, steps_accel) if steps_accel > 0 else 0
    actual_n_decel = min(n_paliers, steps_decel) if steps_decel > 0 else 0

    ramp = []

    # ── Rampe d'accélération ──
    if steps_accel > 0 and actual_n_accel > 0:
        steps_per_palier = max(1, steps_accel // actual_n_accel)
        for i in range(actual_n_accel):
            t = (i + 1) / actual_n_accel
            freq = f_min + (f_cruise - f_min) * t
            s = steps_per_palier if i < actual_n_accel - 1 else (steps_accel - steps_per_palier * (actual_n_accel - 1))
            if s > 0:
                ramp.append((round(freq, 1), s))

    # ── Croisière (découper si > 65535 steps pour wave_chain) ──
    MAX_LOOP = 65535
    remaining_cruise = steps_cruise
    while remaining_cruise > 0:
        chunk = min(remaining_cruise, MAX_LOOP)
        ramp.append((round(f_cruise, 1), chunk))
        remaining_cruise -= chunk

    # ── Rampe de décélération ──
    if steps_decel > 0 and actual_n_decel > 0:
        steps_per_palier = max(1, steps_decel // actual_n_decel)
        for i in range(actual_n_decel):
            t = 1.0 - (i + 1) / actual_n_decel
            freq = f_min + (f_cruise - f_min) * t
            s = steps_per_palier if i < actual_n_decel - 1 else (steps_decel - steps_per_palier * (actual_n_decel - 1))
            if s > 0:
                ramp.append((round(freq, 1), s))

    return ramp


def simulate_ramp(ramp):
    """
    Simule l'exécution d'une rampe et retourne les stats.
    """
    total_steps = 0
    total_time = 0.0
    max_freq = 0
    min_freq = float('inf')

    for freq, steps in ramp:
        total_steps += steps
        t = steps / freq  # temps pour ce palier
        total_time += t
        max_freq = max(max_freq, freq)
        if freq > 0:
            min_freq = min(min_freq, freq)

    distance_mm = total_steps / STEPS_PER_MM
    v_max_mm_s = max_freq / STEPS_PER_MM
    v_min_mm_s = min_freq / STEPS_PER_MM if min_freq < float('inf') else 0

    return {
        "total_steps": total_steps,
        "total_time_s": total_time,
        "distance_mm": distance_mm,
        "n_paliers": len(ramp),
        "max_freq_hz": max_freq,
        "min_freq_hz": min_freq,
        "v_max_mm_s": v_max_mm_s,
        "v_min_mm_s": v_min_mm_s,
        "v_max_mm_min": v_max_mm_s * 60,
    }


def wave_chain_bytes(ramp):
    """
    Génère les bytes wave_chain (sans les wave IDs réels — utilise des IDs fictifs).
    Utile pour vérifier le format et la taille.
    """
    chain = []
    for wid, (freq, steps) in enumerate(ramp):
        x = steps & 0xFF
        y = (steps >> 8) & 0xFF
        chain += [255, 0, wid, 255, 1, x, y]
    return chain


# ════════════════════════════════════════════════
#  Tests
# ════════════════════════════════════════════════

def test_case(name, distance_mm, feed_mm_min, accel, decel):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  Distance={distance_mm} mm, Feed={feed_mm_min} mm/min, Accel={accel} mm/s², Decel={decel} mm/s²")
    print(f"{'='*60}")

    ramp = build_trapezoid(distance_mm, feed_mm_min, accel, decel)

    if not ramp:
        print("  ERREUR: rampe vide!")
        return False

    stats = simulate_ramp(ramp)
    chain = wave_chain_bytes(ramp)

    print(f"\n  Rampe ({stats['n_paliers']} paliers):")
    for i, (freq, steps) in enumerate(ramp):
        v_mm_s = freq / STEPS_PER_MM
        t_ms = (steps / freq) * 1000
        print(f"    [{i:2d}] {freq:8.1f} Hz  ({v_mm_s:6.2f} mm/s = {v_mm_s*60:7.1f} mm/min)  "
              f"x {steps:6d} steps  ({t_ms:7.1f} ms)")

    print(f"\n  Resultat:")
    print(f"    Total steps:    {stats['total_steps']}  (attendu: {round(distance_mm * STEPS_PER_MM)})")
    print(f"    Distance:       {stats['distance_mm']:.3f} mm  (attendu: {distance_mm} mm)")
    print(f"    Temps total:    {stats['total_time_s']:.3f} s")
    print(f"    V max:          {stats['v_max_mm_s']:.2f} mm/s  ({stats['v_max_mm_min']:.0f} mm/min)")
    print(f"    V min:          {stats['v_min_mm_s']:.2f} mm/s")
    print(f"    Max freq:       {stats['max_freq_hz']:.0f} Hz")
    print(f"    Chain bytes:    {len(chain)} bytes ({stats['n_paliers']} waves)")

    # ── Vérifications ──
    ok = True
    expected_steps = round(distance_mm * STEPS_PER_MM)
    if stats['total_steps'] != expected_steps:
        print(f"  ** ERREUR STEPS: {stats['total_steps']} != {expected_steps}")
        ok = False

    if stats['v_max_mm_min'] > feed_mm_min * 1.01:  # 1% tolérance
        print(f"  ** ERREUR VITESSE: dépasse la consigne ({stats['v_max_mm_min']:.0f} > {feed_mm_min})")
        ok = False

    # Vérifier qu'aucune fréquence ne dépasse le raisonnable (disons 50 kHz pour pigpio)
    if stats['max_freq_hz'] > 50000:
        print(f"  ** WARNING: fréquence élevée {stats['max_freq_hz']:.0f} Hz (pigpio wave max ~100kHz)")

    # Vérifier pas de steps = 0
    for i, (freq, steps) in enumerate(ramp):
        if steps <= 0:
            print(f"  ** ERREUR: palier {i} a {steps} steps")
            ok = False
        if freq <= 0:
            print(f"  ** ERREUR: palier {i} a {freq} Hz")
            ok = False

    # Vérifier encodage wave_chain (steps max par boucle = 65535)
    for i, (freq, steps) in enumerate(ramp):
        if steps > 65535:
            print(f"  ** ERREUR: palier {i} a {steps} steps (max wave_chain loop = 65535)")
            ok = False

    if ok:
        print("  => OK")
    return ok


if __name__ == "__main__":
    print("=" * 52)
    print("   Simulation profils trapezoidaux -- VM32L")
    print(f"   {STEPS_PER_MM:.0f} steps/mm ({STEPS_REV}x{MICROSTEP}/{LEAD_MM}mm)")
    print("=" * 52)

    results = []

    # Cas 1 : Déplacement normal (avance outil, 10mm à 300 mm/min)
    results.append(test_case(
        "Avance outil 10mm @ 300 mm/min",
        distance_mm=10, feed_mm_min=300, accel=300, decel=300))

    # Cas 2 : Déplacement rapide (50mm à 1500 mm/min)
    results.append(test_case(
        "Rapide 50mm @ 1500 mm/min",
        distance_mm=50, feed_mm_min=1500, accel=300, decel=300))

    # Cas 3 : Petit déplacement (1mm — profil triangulaire probable)
    results.append(test_case(
        "Micro deplacement 1mm @ 300 mm/min",
        distance_mm=1, feed_mm_min=300, accel=300, decel=300))

    # Cas 4 : Très court (0.1mm — quasi triangulaire)
    results.append(test_case(
        "Tres court 0.1mm @ 300 mm/min",
        distance_mm=0.1, feed_mm_min=300, accel=300, decel=300))

    # Cas 5 : Longue course (200mm rapide)
    results.append(test_case(
        "Longue course 200mm @ 1500 mm/min",
        distance_mm=200, feed_mm_min=1500, accel=300, decel=300))

    # Cas 6 : Avance lente (finition, 50 mm/min)
    results.append(test_case(
        "Finition lente 20mm @ 50 mm/min",
        distance_mm=20, feed_mm_min=50, accel=300, decel=300))

    # Cas 7 : Accel != Decel
    results.append(test_case(
        "Accel rapide / Decel lente 30mm @ 600 mm/min",
        distance_mm=30, feed_mm_min=600, accel=500, decel=200))

    print("\n" + "=" * 60)
    print(f"BILAN : {sum(results)}/{len(results)} tests OK")
    if all(results):
        print("Tous les profils sont valides.")
    else:
        print("ATTENTION : certains tests ont echoue !")
    print("=" * 60)
