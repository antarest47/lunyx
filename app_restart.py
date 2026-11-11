"""
Reinicio de la app para aplicar un cambio de modo de interfaz (desktop <->
TV, ver app_settings.get_ui_mode/set_ui_mode y tv_main.py). Módulo aparte
de main.py para que gallery.py (donde vive el botón de cambio de modo, en
ManageLibrariesDialog) lo pueda importar sin depender de main.py — hoy es
main.py el que importa gallery.py, nunca al revés.

request_restart() NO llama a os.execv() directo: solo persiste el modo
nuevo y dispara un quit() normal. El reinicio real (os.execv) pasa recién
en main.py, DESPUÉS de que app.exec() retorna — disparar os.execv desde
adentro de un slot de Qt, con el event loop todavía corriendo, es mucho
más frágil (reemplaza la imagen del proceso a mitad de código C++/Qt que
asume que va a seguir ejecutándose).
"""

from PySide6.QtWidgets import QApplication

import app_settings

_pending_restart_mode = None


def request_restart(new_mode):
    global _pending_restart_mode
    app_settings.set_ui_mode(new_mode)
    _pending_restart_mode = new_mode
    QApplication.instance().quit()


def pending_restart_mode():
    return _pending_restart_mode
