#!/bin/sh
# Esta sesión Qt no usa XWayland por defecto (backend nativo "wayland", donde
# set_xwindow() de libVLC no tiene efecto — ver Paso 1 de CONTEXT.md, mismo
# comportamiento fuera del sandbox). Se fuerza xcb explícito, igual que ya
# documenta el README del proyecto para la versión venv.
export QT_QPA_PLATFORM=xcb
exec python3 /app/lib/lunyx/main.py "$@"
