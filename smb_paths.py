"""
Construcción de paths para las dos formas en que tocamos un share SMB:

- UNC (\\\\server\\share\\sub\\path) para smbclient (smbprotocol), que es
  quien lista carpetas y lee archivos chicos (posters).
- URL smb:// para libVLC, que resuelve la reproducción por su cuenta sin
  pasar por smbclient/Python en absoluto.
"""

from urllib.parse import quote


def _norm_parts(*parts):
    """Junta subpaths (con / o \\ mezclados) en una lista de componentes."""
    joined = "/".join(p.strip("/\\") for p in parts if p)
    return [p for p in joined.replace("\\", "/").split("/") if p]


def _split_share(share, *subparts):
    """El campo 'share' de una biblioteca no siempre es un nombre de share
    SMB plano: en la práctica la gente le mete la ruta completa (ej.
    'qshared-wdc/anime-library/wdc-anime-series'). Para no depender de que
    esté bien separado en el formulario, se normaliza share+subpaths juntos
    y se toma el primer componente como el share real."""
    parts = _norm_parts(share, *subparts)
    if not parts:
        raise ValueError("share vacío")
    return parts[0], parts[1:]


def unc_path(server, share, *subparts):
    share_name, rest = _split_share(share, *subparts)
    tail = "\\".join(rest)
    base = f"\\\\{server}\\{share_name}"
    return f"{base}\\{tail}" if tail else base


def vlc_url(server, share, *subparts):
    """URL smb:// SIN credenciales embebidas a propósito: libVLC's
    libsmb_plugin.so soporta usuario/contraseña como opciones de media
    separadas (ver vlc_credential_options), así la contraseña nunca queda
    escrita en la URL ni, por lo tanto, en los logs de error de VLC."""
    share_name, rest = _split_share(share, *subparts)
    path_enc = "/".join(quote(p, safe="") for p in rest)
    url = f"smb://{server}/{quote(share_name, safe='')}"
    return f"{url}/{path_enc}" if path_enc else url


def vlc_credential_options(username, password, domain="WORKGROUP"):
    """Opciones de vlc.Media para autenticar contra libsmb_plugin.so sin
    pasar por la URL (ver vlc_url). smb-domain es necesario explícito: sin
    él, libsmbclient devuelve EINVAL en vez de un error de auth claro."""
    return [
        f":smb-user={username}",
        f":smb-pwd={password}",
        f":smb-domain={domain}",
    ]
