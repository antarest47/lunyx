# Licencias de terceros

Lunyx (el código en este repo, bajo `LICENSE`) está licenciado GPL-3.0-only.
Esa elección no es arbitraria: el Flatpak empaqueta y distribuye binarios
compilados de varias librerías de terceros, y una de ellas —`libsmbclient`,
parte de Samba— está bajo GPL-3.0-or-later. Cuando un binario distribuido
enlaza (o empaqueta) código GPL, el resultado combinado tiene que licenciarse
bajo términos compatibles con GPL — de ahí que todo el proyecto sea GPL
también. GPL-3.0-only es compatible recibiendo código GPL-3.0-or-later (la
cláusula "or later" da la opción de usarlo bajo v3, no la exige), así que no
hace falta heredar el "-or-later" para el proyecto entero.

Este archivo lista cada componente de terceros que Lunyx usa en tiempo de
ejecución (embebido en el Flatpak, o como dependencia de Python), qué
licencia tiene, y para qué se usa. Ninguno de estos proyectos está afiliado
con Lunyx ni lo respalda.

## Bibliotecas C/C++ (compiladas dentro del Flatpak)

| Componente | Licencia | Uso en Lunyx |
|---|---|---|
| [VLC / libVLC](https://www.videolan.org/vlc/libvlc.html) (VideoLAN) | LGPL-2.1-or-later | Motor de reproducción embebido (`python-vlc`) — decodifica y muestra el video dentro de la ventana de la app. |
| [Samba / libsmbclient](https://www.samba.org/) | **GPL-3.0-or-later** | Usado por el módulo de acceso SMB de VLC (`samba.c`) para que libVLC pueda abrir archivos vía `smb://` directamente. Es el componente que determina la licencia GPL de todo el proyecto — ver arriba. |
| [libass](https://github.com/libass/libass) | ISC | Renderiza subtítulos `.ass`/`.ssa` con estilos (karaoke, posición, fuentes custom) usados por releases de fansub reales. |
| [libebml](https://github.com/Matroska-Org/libebml) | LGPL-2.1 | Parseo de bajo nivel del formato binario EBML en el que se basa Matroska (`.mkv`). |
| [libmatroska](https://github.com/Matroska-Org/libmatroska) | LGPL-2.1 | Demuxer nativo de `.mkv` para VLC — sin esto, VLC cae al demuxer genérico de ffmpeg, que mapea mal las pistas de subtítulos. |
| [Parse::Yapp](https://metacpan.org/pod/Parse::Yapp) (Perl) | misma licencia que Perl (Artistic License / GPL) | Solo se usa en TIEMPO DE BUILD, para generar el parser de Samba — no queda linkeado en el binario final. |
| [flatpak-pip-generator](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip) (`flatpak/flatpak-pip-generator.py`, copia sin cambios) | MIT | Solo para armar el Flatpak: genera `python3-requirements.yaml` con las dependencias de Python. |

## Paquetes de Python

| Paquete | Licencia | Uso en Lunyx |
|---|---|---|
| [PySide6](https://doc.qt.io/qtforpython/) (Qt for Python, The Qt Company) | LGPLv3 | Toda la interfaz gráfica (ventanas, diálogos, widgets). |
| [python-vlc](https://github.com/oaubert/python-vlc) | LGPL-2.1-or-later | Bindings de Python para libVLC. |
| [smbprotocol](https://github.com/jborean93/smbprotocol) | MIT | Habla SMB2/3 directo desde Python (sin montar nada) para escanear bibliotecas y leer/escribir los sidecars de metadata/progreso. |
| [keyring](https://github.com/jaraco/keyring) | MIT | Guarda las contraseñas SMB en el gestor de credenciales del sistema (KDE Wallet vía SecretService) — nunca en un archivo de config plano. |
| [pyspnego](https://github.com/jborean93/pyspnego) | MIT | Autenticación NTLM/Kerberos para smbprotocol. |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 o BSD-3-Clause | Cifrado usado por smbprotocol y pyspnego. |
| [cffi](https://github.com/python-cffi/cffi) | MIT | Dependencia de cryptography. |
| [pycparser](https://github.com/eliben/pycparser) | BSD-3-Clause | Dependencia de cffi. |
| [SecretStorage](https://github.com/mitya57/secretstorage) | BSD-3-Clause | Backend SecretService de keyring. |
| [jeepney](https://gitlab.com/takluyver/jeepney) | MIT | D-Bus para SecretStorage. |
| [jaraco.classes](https://github.com/jaraco/jaraco.classes), [jaraco.context](https://github.com/jaraco/jaraco.context), [jaraco.functools](https://github.com/jaraco/jaraco.functools) | MIT | Dependencias de keyring. |
| [more-itertools](https://github.com/more-itertools/more-itertools) | MIT | Dependencia de jaraco.functools. |

Cada uno trae su licencia dentro del paquete (`*.dist-info`).

## Runtime y assets del sistema

El Flatpak corre sobre `org.kde.Platform`/`org.kde.Sdk` (runtime de KDE) y la
extensión `org.freedesktop.Platform.ffmpeg-full` (decoders con patentes de
ffmpeg, necesarios para H.264/HEVC 10-bit) — ambos con sus propias licencias,
gestionadas por sus respectivos proyectos (KDE, freedesktop-sdk), no por
Lunyx.

"VLC" y el logo del cono son marcas registradas de VideoLAN. Este proyecto
usa libVLC como librería pero no es un producto oficial de VideoLAN ni está
afiliado con el proyecto VLC.
