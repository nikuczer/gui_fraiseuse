"""
HX-K12 Macro Pad - Détection des touches
Lance ce script EN ADMIN puis appuie sur chaque touche/rotatif du pad.
Appuie sur ECHAP (clavier principal) pour quitter.
"""
import keyboard
import signal

# Ignorer Ctrl+C pour ne pas quitter quand le pad envoie Ctrl+C
signal.signal(signal.SIGINT, signal.SIG_IGN)

count = [0]

print("=" * 50)
print("  HX-K12 - Détection des touches")
print("  Appuie sur chaque touche du pad une par une")
print("  Appuie sur ECHAP (clavier principal) pour quitter")
print("=" * 50)
print()

def on_key(e):
    if e.event_type == "down":
        count[0] += 1
        print(f"  #{count[0]:<3}  name={e.name!s:<15} scancode={e.scan_code:<5} is_keypad={e.is_keypad}")

keyboard.on_press(on_key)
keyboard.wait("esc")
print("\nTerminé.")
