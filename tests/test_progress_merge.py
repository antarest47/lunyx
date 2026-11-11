"""
Tests de la pieza más importante del rediseño de progreso centralizado
"un archivo por dispositivo" (ver CONTEXT.md, Paso 38+): la fusión de
lectura, `centralized_store.merge_device_payloads`.

Función PURA — sin Qt, sin I/O, sin red, sin threads — así que estos
tests no necesitan QApplication ni QT_QPA_PLATFORM=offscreen (a
diferencia del resto de tests/, que sí lo necesitan por instanciar
widgets reales). Es justamente el punto: la lógica que decide qué dato
sobrevive y cuál se descarta tiene que poder probarse aislada, sin
levantar media app para ejercitarla.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import centralized_store as cs


def entry(position_ms, updated_at):
    return {"position_ms": position_ms, "duration_ms": 1_440_000, "updated_at": updated_at}


def envelope(entries=None, tombstones=None, prefix_tombstones=None, device_id="dev"):
    return {
        "schema": 2,
        "device_id": device_id,
        "entries": entries or {},
        "tombstones": tombstones or {},
        "prefix_tombstones": prefix_tombstones or [],
    }


class MergeDevicePayloadsTest(unittest.TestCase):
    def test_series_distintas_en_dispositivos_distintos_sobreviven_las_dos(self):
        """El bug original que motivó todo el rediseño: dos dispositivos
        guardando progreso de series DISTINTAS, sin haberse leído entre
        sí, no deberían pisarse."""
        device_a = envelope({"uid|SerieA/ep01.mkv": entry(60_000, 100.0)})
        device_b = envelope({"uid|SerieB/ep01.mkv": entry(120_000, 100.0)})

        merged = cs.merge_device_payloads([device_a, device_b])

        self.assertEqual(set(merged), {"uid|SerieA/ep01.mkv", "uid|SerieB/ep01.mkv"})
        self.assertEqual(merged["uid|SerieA/ep01.mkv"]["position_ms"], 60_000)
        self.assertEqual(merged["uid|SerieB/ep01.mkv"]["position_ms"], 120_000)

    def test_misma_clave_gana_el_updated_at_mas_reciente(self):
        key = "uid|Serie/ep01.mkv"
        viejo = envelope({key: entry(1_000, 50.0)})
        nuevo = envelope({key: entry(700_000, 100.0)})

        merged = cs.merge_device_payloads([viejo, nuevo])
        self.assertEqual(merged[key]["position_ms"], 700_000)

        # el orden de la lista no debería importar
        merged_reverso = cs.merge_device_payloads([nuevo, viejo])
        self.assertEqual(merged_reverso[key]["position_ms"], 700_000)

    def test_tombstone_mas_nuevo_borra_entrada_viva_mas_vieja(self):
        """Episodio terminado (>=95%) en un dispositivo, otro dispositivo
        todavía tiene una copia vieja sin terminar guardada — el borrado
        más reciente tiene que ganar."""
        key = "uid/Serie/ep01.mkv"
        vivo_viejo = envelope({key: entry(50_000, 50.0)})
        borrado_nuevo = envelope(tombstones={key: 100.0})

        merged = cs.merge_device_payloads([vivo_viejo, borrado_nuevo])
        self.assertNotIn(key, merged)

    def test_entrada_viva_mas_nueva_gana_sobre_tombstone_viejo(self):
        """Lo inverso: alguien vuelve a mirar un episodio que ya se había
        marcado como terminado — la entrada viva más nueva "revive" la
        clave, a propósito (no es una resurrección accidental, es un
        replay real más nuevo que el borrado)."""
        key = "uid|Serie/ep01.mkv"
        borrado_viejo = envelope(tombstones={key: 50.0})
        vivo_nuevo = envelope({key: entry(1_000, 100.0)})

        merged = cs.merge_device_payloads([borrado_viejo, vivo_nuevo])
        self.assertIn(key, merged)
        self.assertEqual(merged[key]["position_ms"], 1_000)

    def test_empate_exacto_de_updated_at_gana_el_borrado(self):
        """Conservador a propósito: ante la duda entre vivo y muerto al
        mismo instante exacto, nunca resucitar por accidente."""
        key = "uid|Serie/ep01.mkv"
        vivo = envelope({key: entry(1_000, 100.0)})
        muerto = envelope(tombstones={key: 100.0})

        self.assertNotIn(key, cs.merge_device_payloads([vivo, muerto]))
        self.assertNotIn(key, cs.merge_device_payloads([muerto, vivo]))  # orden no importa

    def test_prefix_tombstone_tapa_entrada_de_otro_dispositivo_que_nunca_la_conocio(self):
        """Caso difícil de "marcar serie como vista" (ver CONTEXT.md,
        diseño D): el dispositivo A tiene progreso de un episodio que el
        dispositivo B JAMÁS sincronizó. B marca la serie entera como
        vista (prefix_tombstone en SU propio archivo) — el episodio de A
        tiene que desaparecer en el próximo merge de cualquiera, aunque B
        nunca haya tenido una entrada ni un tombstone puntual para esa
        clave específica."""
        key_conocida_solo_por_a = "uid|Serie/ep05.mkv"
        device_a = envelope({key_conocida_solo_por_a: entry(50_000, 50.0)})
        device_b = envelope(
            prefix_tombstones=[{"prefix": "uid|Serie/", "updated_at": 100.0}]
        )

        merged = cs.merge_device_payloads([device_a, device_b])
        self.assertNotIn(key_conocida_solo_por_a, merged)

    def test_prefix_tombstone_no_tapa_entrada_mas_nueva(self):
        """Alguien retoma un episodio DESPUÉS de que la serie se marcó
        como vista — el tombstone de prefijo no debe tapar algo más
        nuevo que él mismo."""
        key = "uid|Serie/ep05.mkv"
        device_a = envelope({key: entry(80_000, 150.0)})  # más nuevo que el tombstone
        device_b = envelope(prefix_tombstones=[{"prefix": "uid|Serie/", "updated_at": 100.0}])

        merged = cs.merge_device_payloads([device_a, device_b])
        self.assertIn(key, merged)
        self.assertEqual(merged[key]["position_ms"], 80_000)

    def test_prefix_tombstone_no_afecta_otras_series(self):
        key_otra_serie = "uid|OtraSerie/ep01.mkv"
        device_a = envelope({key_otra_serie: entry(1_000, 50.0)})
        device_b = envelope(prefix_tombstones=[{"prefix": "uid|Serie/", "updated_at": 999999.0}])

        merged = cs.merge_device_payloads([device_a, device_b])
        self.assertIn(key_otra_serie, merged)

    def test_sin_payloads_da_vacio(self):
        self.assertEqual(cs.merge_device_payloads([]), {})

    def test_payload_incompleto_no_revienta(self):
        """merge_device_payloads no debería asumir que todo payload pasó
        por _normalize_envelope — un dict con solo "entries" (sin
        tombstones/prefix_tombstones) tiene que andar igual."""
        merged = cs.merge_device_payloads([{"entries": {"k": entry(1, 1.0)}}])
        self.assertIn("k", merged)


class EnvelopeNormalizationTest(unittest.TestCase):
    def test_envelope_vacio(self):
        env = cs._empty_envelope("dev123")
        self.assertEqual(env["schema"], 2)
        self.assertEqual(env["device_id"], "dev123")
        self.assertEqual(env["entries"], {})
        self.assertEqual(env["tombstones"], {})
        self.assertEqual(env["prefix_tombstones"], [])

    def test_normaliza_none_a_envelope_vacio(self):
        env = cs._normalize_envelope(None, "dev123")
        self.assertEqual(env, cs._empty_envelope("dev123"))

    def test_normaliza_dict_plano_viejo_envolviendo_como_entries(self):
        """Formato de antes del Paso 38: {clave: entrada}, sin "schema".
        No debería perderse ningún dato al envolverlo."""
        viejo = {"uid|Serie/ep01.mkv": entry(1_000, 50.0)}
        env = cs._normalize_envelope(viejo, "dev123")
        self.assertEqual(env["schema"], 2)
        self.assertEqual(env["entries"], viejo)
        self.assertEqual(env["tombstones"], {})
        self.assertEqual(env["prefix_tombstones"], [])

    def test_envelope_v2_ya_armado_se_preserva(self):
        original = envelope(
            entries={"k": entry(1, 1.0)},
            tombstones={"k2": 2.0},
            prefix_tombstones=[{"prefix": "p", "updated_at": 3.0}],
            device_id="dev-original",
        )
        env = cs._normalize_envelope(original, "dev-otro")
        self.assertEqual(env["device_id"], "dev-original")  # el device_id embebido manda
        self.assertEqual(env["entries"], original["entries"])
        self.assertEqual(env["tombstones"], original["tombstones"])
        self.assertEqual(env["prefix_tombstones"], original["prefix_tombstones"])

    def test_envelope_v2_con_campos_faltantes_no_revienta(self):
        """Defensivo ante un archivo escrito por una versión futura con
        menos campos, o corrupción parcial."""
        env = cs._normalize_envelope({"schema": 2}, "dev123")
        self.assertEqual(env["entries"], {})
        self.assertEqual(env["tombstones"], {})
        self.assertEqual(env["prefix_tombstones"], [])


if __name__ == "__main__":
    unittest.main()
