<p align="center">
  <img src="assets/icons/hicolor/scalable/apps/b6ing-launcher.svg" alt="Logo B6ing" width="104">
</p>

# B6ing — Be Seeing

B6ing est une application de bureau pour voir et piloter des caméras solaires **Cinnado B6** sur Ubuntu. Elle affiche les directs de ton compte Wansview Cloud dans une grille GTK 4 et libadwaita, sans téléphone, Wine ni Waydroid.

## Fonctionnalités

- **Direct** en grille ou en plein écran, profils Haute et Éco, tentative de reconnexion après une coupure.
- **Commandes** sur chaque vignette et en plein écran. Oriente la caméra au joystick ou avec les flèches du clavier. Pilote la lumière, la sirène et la vision jour/nuit.
- **Audio** pour écouter la caméra et parler en maintenant le bouton microphone.
- **Captures et enregistrements** en PNG et MKV, avec deux dossiers configurables. Les noms de fichiers reprennent le nom de la caméra et l’heure de début, avec la date.
- **État des caméras** avec niveau de batterie, signal Wi-Fi et notifications de mouvement, selon les données renvoyées par Wansview Cloud.
- **Intégration Ubuntu** avec mémorisation facultative du compte dans le trousseau, icône de zone de notification et démarrage réduit à l’ouverture de session.

## Installation sur Ubuntu

Environnement testé sur **Ubuntu 26.04 avec GNOME**. Le direct utilise GStreamer. Le trousseau de session sert à mémoriser le compte.

```sh
sudo apt install git python3-gi python3-venv gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-gstreamer-1.0 gir1.2-secret-1 gstreamer1.0-gtk4 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav

git clone git@github.com:Chrisnox-209/B6ing.git
cd B6ing
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-cinnado.txt
/usr/bin/python3 install.py
```

Ouvre **B6ing** depuis le menu Applications ou lance `.venv/bin/python -m cinnado_live` depuis le dossier du projet. Le raccourci utilise ce dossier. Relance l’installateur après l’avoir déplacé.

## Utilisation

1. Connecte-toi avec le compte Wansview Cloud auquel les caméras sont déjà associées. Active **Mémoriser le compte sur ce PC** pour utiliser le trousseau Ubuntu.
2. Clique sur **Voir le direct**. Pour piloter une caméra au clavier, sélectionne sa vignette puis maintiens **↑**, **↓**, **←** ou **→**. En plein écran, les flèches pilotent la caméra affichée.
3. Ouvre **Réglages** pour renommer les caméras, choisir les dossiers des photos et vidéos, recevoir les alertes de mouvement ou activer le démarrage réduit. Utilise le menu de l’icône de notification pour réafficher la fenêtre ou quitter B6ing.

La lecture des enregistrements de la carte microSD reste disponible dans Wansview Cloud. La compatibilité de B6ing a été vérifiée avec des Cinnado B6. La disponibilité du direct et des commandes dépend du service Wansview et du modèle de caméra.

## Développement

```sh
.venv/bin/python -m unittest discover -s tests -v
```

B6ing est un projet indépendant. Le module de connexion embarqué dérive de [papou-github/wansview-r2](https://github.com/papou-github/wansview-r2). Les détails de l’intégration et sa licence MIT figurent dans [UPSTREAM.md](cinnado_live/vendor_r2/UPSTREAM.md) et [LICENSE](cinnado_live/vendor_r2/LICENSE).
