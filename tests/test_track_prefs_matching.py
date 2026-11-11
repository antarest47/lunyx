"""
Reproduce un bug real reportado en vivo: la preferencia de audio/
subtítulo elegida en la lista de episodios se guardaba bien, pero nunca
se aplicaba al abrir un capítulo en otra instalación — sin excepción,
sea cual sea la pista elegida.

Causa real, confirmada con el `track_prefs.json` real del usuario: libVLC
arma la descripción de cada pista como "<nombre> - [<idioma>]", con el
nombre de idioma TRADUCIDO según el locale de la sesión que sondeó la
serie (ver track_probe.py) — una serie sondeada bajo un locale en español
queda con "... - [Inglés]" guardado para siempre en el sidecar, aunque
la reproducción real después ocurra bajo un locale en inglés, donde la
MISMA pista se describe en vivo como "... - [English]". find_matching_track
buscaba el texto completo como substring, así que ese corchete distinto
rompía el match siempre — coincide exacto con "recuerda una pista que
elegí a mano en el player en un episodio anterior" que reportó el
usuario: al no encontrar match nunca, quedaba lo que VLC ya tenía
seleccionado (la última pista tocada manualmente), no lo guardado.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import track_prefs

# Extraído tal cual de un track_prefs.json real — el sondeo original
# ocurrió bajo una sesión en español ("Inglés"), la reproducción real en
# otra instalación da estas mismas pistas en inglés ("English").
REAL_PREFERRED_AUDIO = "[Grupo] Japanese Stereo (Opus 112Kbps) - [Inglés]"
REAL_PREFERRED_SUBTITLE = "English (Full) - [Inglés]"
REAL_LIVE_AUDIO_DESCRIPTIONS = [
    (1, b"[Grupo] Japanese Stereo (Opus 112Kbps) - [English]"),
    (2, b"[Grupo] English Stereo (Opus 112Kbps) - [English]"),
]
REAL_LIVE_SUBTITLE_DESCRIPTIONS = [
    (-1, b"Disable"),
    (3, b"English (Full) - [English]"),
    (4, b"English (Signs/Songs) - [English]"),
]


class FindMatchingTrackLocaleMismatchTest(unittest.TestCase):
    def test_locale_mismatched_bracket_still_matches_audio(self):
        track_id = track_prefs.find_matching_track(REAL_LIVE_AUDIO_DESCRIPTIONS, REAL_PREFERRED_AUDIO)
        self.assertEqual(track_id, 1, "no matcheó la pista de audio real pese al corchete de idioma distinto")

    def test_locale_mismatched_bracket_still_matches_subtitle(self):
        track_id = track_prefs.find_matching_track(REAL_LIVE_SUBTITLE_DESCRIPTIONS, REAL_PREFERRED_SUBTITLE)
        self.assertEqual(track_id, 3, "no matcheó la pista de subtítulo real pese al corchete de idioma distinto")

    def test_no_false_positive_entre_pistas_con_mismo_corchete(self):
        # Las dos pistas de audio comparten el mismo corchete de idioma
        # ("[English]") — el recorte no debe volverlas indistinguibles,
        # la parte estable del nombre ("Japanese Stereo" vs "English
        # Stereo") sigue siendo lo que decide.
        preferred_english_dub = "[Grupo] English Stereo (Opus 112Kbps) - [Inglés]"
        track_id = track_prefs.find_matching_track(REAL_LIVE_AUDIO_DESCRIPTIONS, preferred_english_dub)
        self.assertEqual(track_id, 2)

    def test_exact_match_sin_locale_de_por_medio_sigue_funcionando(self):
        # Camino normal (locale no cambió desde que se guardó) — no debe
        # romperse por agregar el fallback.
        descriptions = [(0, b"Japanese"), (1, b"English")]
        self.assertEqual(track_prefs.find_matching_track(descriptions, "English"), 1)

    def test_texto_genuinamente_ausente_sigue_sin_matchear(self):
        track_id = track_prefs.find_matching_track(REAL_LIVE_AUDIO_DESCRIPTIONS, "Korean Dub - [Inglés]")
        self.assertIsNone(track_id)

    def test_sin_preferencia_guardada_no_matchea_nada(self):
        self.assertIsNone(track_prefs.find_matching_track(REAL_LIVE_AUDIO_DESCRIPTIONS, None))
        self.assertIsNone(track_prefs.find_matching_track(REAL_LIVE_AUDIO_DESCRIPTIONS, ""))


if __name__ == "__main__":
    unittest.main()
