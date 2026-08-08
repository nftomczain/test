#!/bin/bash
# Buduje GhostPoster.flatpak — paczkę Flatpak obok istniejącego AppImage.
# Uruchom w katalogu głównym repo:
#
#   ./scripts/build_flatpak.sh
#
# Wymaga flatpak i flatpak-builder (na Ubuntu/Debian: `sudo apt install
# flatpak flatpak-builder`) oraz dostępu do Flathub — pierwsze uruchomienie
# pobiera runtime KDE Platform/Sdk i bazowy obraz io.qt.PySide.BaseApp
# (kilkaset MB, jednorazowo, później cache'owane lokalnie przez flatpak).
#
# Wynik: GhostPoster.flatpak w katalogu głównym repo — do zainstalowania
# lokalnie przez `flatpak install --user GhostPoster.flatpak`, albo do
# wysłania na Flathub.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Dodaję zdalne repozytorium Flathub (jeśli jeszcze nie dodane)..."
flatpak remote-add --if-not-exists --user flathub https://flathub.org/repo/flathub.flatpakrepo

echo "Pobieram runtime i bazowy obraz PySide (jednorazowo, może chwilę potrwać)..."
flatpak install --user -y flathub org.kde.Platform//6.8 org.kde.Sdk//6.8
flatpak install --user -y flathub io.qt.PySide.BaseApp//6.8

echo "Czyszczę poprzedni build..."
rm -rf .flatpak-builder build-dir GhostPoster.flatpak

echo "Buduję Flatpaka..."
flatpak-builder --user --force-clean build-dir \
  flatpak/io.github.nftomczain.GhostPoster.yml

echo "Pakuję do pojedynczego pliku .flatpak..."
flatpak-builder --user --force-clean --repo=flatpak-repo build-dir \
  flatpak/io.github.nftomczain.GhostPoster.yml
flatpak build-bundle flatpak-repo GhostPoster.flatpak \
  io.github.nftomczain.GhostPoster

echo ""
echo "Gotowe: GhostPoster.flatpak"
echo "Instalacja lokalna:  flatpak install --user GhostPoster.flatpak"
echo "Uruchomienie:        flatpak run io.github.nftomczain.GhostPoster"
