# Lunyx

Cliente de escritorio para ver una biblioteca de anime propia (share SMB o
carpeta local), sin las convenciones de nombres que piden Jellyfin o Plex
para que su scraper reconozca una serie. La idea es simple: metés una
carpeta con la serie, le ponés una carátula, y Lunyx la lee y la reproduce
con **libVLC embebido** — sin fichas de metadata que armar ni un servidor
que mantener.

Proyecto experimental, en desarrollo activo. Pensado para Linux de
escritorio (probado en Debian + Plasma).

## Hecho con IA

Lunyx está hecho con inteligencia artificial, y quiero que quede claro desde
el principio. No soy desarrollador/a: tengo las ideas, decido cómo se ve y
cómo se comporta cada cosa, y lo pruebo. El código lo escribió Claude
(Anthropic), con Claude Code, a partir de esas decisiones.

Todo se probó en mi PC y en máquinas virtuales, pero nadie lo revisó como lo
haría un equipo de desarrollo, ni hubo una auditoría de seguridad. Si lo vas
a usar, probalo primero con una biblioteca y una máquina que no te importen.
Como dice la licencia, se entrega sin garantías. Si encontrás un error, abrí
un Issue.

## Por qué existe

Es una alternativa simple a Jellyfin/Plex. Esos sistemas son excelentes,
pero esperan que la biblioteca siga un formato específico — nombres de
carpeta/archivo que un scraper pueda matchear contra una base de datos
externa (TheTVDB, TMDB) para reconocer series y temporadas. Con packs de
fansub reales eso no siempre es así: nombres de release larguísimos,
temporadas sueltas sin ninguna convención, sub-series sin distinguir entre
sí. Lunyx no intenta adivinar nada contra una base externa — lee la
carpeta tal cual está, y si hace falta, le podés poner un nombre propio a
mano (a la serie o a una temporada puntual) sin tocar los archivos reales.

Como reproduce con **libVLC embebido** en vez de transcodear, tampoco hay
compatibilidad de codec que resolver del lado del servidor: HEVC 10-bit,
subtítulos ASS con estilos (karaoke, fuentes custom), dual audio — todo se
reproduce tal cual es.

## Principios de diseño

- **Independiente**: no le pega a ninguna API externa, no asume que otro
  servidor exista o esté corriendo. Tiene su propio scanner y lee la
  biblioteca directo del share/carpeta.
- **La biblioteca real es siempre de solo lectura.** Lunyx nunca renombra,
  mueve ni borra series, temporadas, episodios o carátulas existentes. El
  único archivo que escribe en el share es el suyo propio
  (`lunyx-metadata.json`), un sidecar nuevo que crea en la raíz de la
  biblioteca — nunca toca nada preexistente del usuario.

## Qué hace

- **Galería**: bibliotecas de tipo series o películas, en shares SMB o
  carpetas locales — se pueden tener varias a la vez, cada una con su
  propia conexión y credenciales (contraseña guardada en el gestor de
  credenciales del sistema, nunca en un archivo plano). Buscador, orden
  A-Z/Z-A, posters con hover, todo client-side sobre lo ya escaneado.
- **Lista de episodios**: agrupa por temporada (subcarpetas reales o
  parseando `SxxExx`/`NxNN` del nombre de archivo cuando todo está suelto),
  con carátulas de temporada si existen.
- **Nombres personalizados**: podés ponerle nombre propio a una serie o
  temporada (los packs de fansub no siempre tienen nombres claros) y
  corregir manualmente la clasificación de un episodio puntual. Se guarda
  en el sidecar de la biblioteca, así que es portable entre instalaciones.
- **Reproductor**: controles tipo Netflix/Jellyfin — scrubber con seek al
  click, anterior/siguiente episodio (inteligente: reinicia el actual si
  llevás poco visto), atajos de teclado (espacio, flechas, F para
  fullscreen), fullscreen real de solo-video con overlay auto-ocultable.
- **Audio/subtítulos**: selección de pista por episodio, con opción de
  "usar para toda la serie" (busca la pista que matchee por texto, no por
  índice — cada archivo puede tener el orden distinto).
- **Progreso de reproducción**: retoma automáticamente donde quedaste, y
  un estante "Continuar viendo" en la galería.
- **Progreso centralizable** (opcional, por biblioteca): en vez de guardar
  el progreso solo en esta máquina, se puede guardar en un share/carpeta
  aparte — para retomar un episodio en otra PC o en un HTPC. La identidad
  de biblioteca que hace esto posible vive en el sidecar de la biblioteca
  (no en un id local), así que sobrevive a que borres y reagregues la
  biblioteca en cualquier instalación.
- Tema oscuro propio, sin depender del estilo default de Qt.

## Cómo funciona por debajo

- **Sin montar nada**: el escaneo/listado/posters hablan SMB2/3 directo
  desde Python (`smbprotocol`), sin `mount`/`gvfs`/sudo. La reproducción va
  aparte: libVLC abre la URL `smb://` de forma nativa.
- **Contraseñas**: nunca en el JSON de config — van al keyring del sistema
  (KDE Wallet en Plasma, vía `keyring`).
- **Progreso/preferencias**: JSON local en `~/.config/lunyx/` por defecto;
  opcionalmente centralizables (ver arriba).

## Instalar

### Flatpak (recomendado)

**Compilar el manifiesto**:

```bash
cd flatpak
flatpak-builder --user --install --force-clean build-dir io.github.antarest47.Lunyx.yml
flatpak install flathub org.freedesktop.Platform.ffmpeg-full//24.08
flatpak run io.github.antarest47.Lunyx
```

El manifiesto compila VLC, Samba (para `libsmbclient`), libass y
libebml/libmatroska desde fuente junto con el resto del runtime — el build
completo tarda bastante la primera vez.

**El segundo comando (`ffmpeg-full`) no es opcional.** El runtime base
(`org.kde.Platform` 6.9, que corre sobre `freedesktop-sdk 24.08`) trae los
decoders de video con patentes deshabilitados por licencia — sin esta
extensión, la app abre y el audio anda, pero el video queda en negro con
`DecodeFrame failed` en cada frame (OpenH264, el único decoder H.264 que
queda, no soporta el perfil 10-bit `Hi10` típico del anime de fansub). Está
declarada en el manifiesto (`add-extensions`), pero en la práctica no se
instala sola al hacer `flatpak-builder --install` — instalarla a mano, una
sola vez por máquina, es la única forma confirmada de que funcione.

### Desde el código fuente

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Requiere `libvlc` instalado en el sistema (`sudo apt install vlc
libvlc-dev` en Debian/Ubuntu).

**Si la ventana queda en negro sin reproducir video**: es casi siempre
Wayland nativo — libVLC embebe video vía X11 (`set_xwindow()`), así que
necesita XWayland. Forzalo explícito:

```bash
QT_QPA_PLATFORM=xcb python main.py
```

## Uso básico

Al abrir la app por primera vez no hay ninguna biblioteca configurada —
"Gestionar bibliotecas" → "Agregar biblioteca nueva", elegís tipo (series o
películas) y conexión (SMB o carpeta local). Podés agregar varias.

## Plataforma

Pensado y probado en Linux de escritorio (Debian + Plasma). No hay soporte
de Windows/Mac ni mobile — no es un objetivo del proyecto.

## Licencia

GPL-3.0-only — ver [`LICENSE`](LICENSE). El Flatpak empaqueta
`libsmbclient` (Samba, GPLv3) para que libVLC pueda abrir `smb://`
directamente, lo que determina la licencia del binario distribuido; ver
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) para el detalle de
cada dependencia de terceros.

"VLC" y su logo son marcas registradas de VideoLAN — Lunyx usa libVLC como
librería pero no es un producto oficial de VideoLAN.
