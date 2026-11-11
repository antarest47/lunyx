"""
Guardado de contraseñas SMB vía keyring (KDE Wallet en Plasma, a través del
backend SecretService que kwalletd expone por D-Bus).

Nunca se persiste la contraseña en libraries.json — solo el username. La
contraseña se guarda con una service key propia por biblioteca, así borrar
una biblioteca no pisa credenciales de otra aunque compartan usuario/server.
"""

import keyring

_SERVICE_PREFIX = "lunyx"
# Nombre viejo del proyecto (Paso 22) — bibliotecas ya configuradas tienen
# su contraseña guardada bajo este prefijo. Se lee como fallback la
# primera vez y se copia (nunca se borra la entrada vieja) bajo el
# prefijo nuevo, así los próximos accesos son directos.
_OLD_SERVICE_PREFIX = "anime-vlc-client"


def _service_name(library_id):
    return f"{_SERVICE_PREFIX}:{library_id}"


def _old_service_name(library_id):
    return f"{_OLD_SERVICE_PREFIX}:{library_id}"


def set_password(library_id, username, password):
    keyring.set_password(_service_name(library_id), username, password)


def get_password(library_id, username):
    password = keyring.get_password(_service_name(library_id), username)
    if password:
        return password
    password = keyring.get_password(_old_service_name(library_id), username)
    if password:
        set_password(library_id, username, password)
    return password


def delete_password(library_id, username):
    try:
        keyring.delete_password(_service_name(library_id), username)
    except keyring.errors.PasswordDeleteError:
        pass
