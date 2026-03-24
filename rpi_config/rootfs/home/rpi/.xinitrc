#!/bin/sh
# Désactiver l’économiseur d’écran/DPMS
xset s off
xset -dpms
xset s noblank

# Cacher le curseur (en option)
unclutter -idle 0 &

# Optionnel : forcer la résolution si besoin (sinon commente)
# xrandr --output HDMI-1 --mode 800x480

# Lancer ton app Tkinter en plein écran
exec python3 /home/rpi/main.py

