LAN Chat 2.0 — мессенджер для компьютеров в одной Wi-Fi сети
=============================================================

Готовые программы собирает GitHub автоматически при каждом изменении:
вкладка Actions -> последний запуск «Build LAN Chat» -> Artifacts.

  LANChat-Windows — распакуйте папку LANChat и запускайте LANChat.exe
  LANChat-Mac     — перетащите LANChat.app в «Программы»

Первый запуск на Mac: Системные настройки -> Конфиденциальность и
безопасность -> «Всё равно открыть» (или в Терминале:
xattr -cr /Applications/LANChat.app).

Запуск из исходника: pip install pywebview, затем python lanchat.py
