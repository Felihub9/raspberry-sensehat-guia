#!/usr/bin/env python3
# Monitor de T i HR amb Raspberry Pi + Sense HAT.
# Cada 15 min: llegeix sensors, guarda la mostra a SQLite i mostra frase + T + HR.
# Verd dins de rang, vermell per sobre, blau per sota.
#
# Entre lectures, mostra un rellotge binari (hores/minuts/segons) a la
# matriu, en violeta i low_light per minimitzar l'escalfor dels LEDs.

import math
import os
import random
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from sense_hat import SenseHat

ROTATION = 180              # pantalla girada
SCROLL_SPEED = 0.07         # velocitat text blanc i missatge taronja
VALUE_SCROLL_SPEED = 0.12   # velocitat valor en color
INTERVAL_MINUTES = 15       # cicle T/H als quarts fixos (:00 :15 :30 :45)
REPEAT_COUNT = 1            # nomes 1 mostra cada 15 min
PAUSE_BETWEEN_MSGS = 1      # pausa entre T i HR
PAUSE_BETWEEN_REPEATS = 60  # pausa entre repeticions (sense efecte si REPEAT_COUNT=1)
SHOW_MESSAGE = True         # True per mostrar el missatge taronja abans de T
INIT_RETRY_SECONDS = 5      # espera entre reintents
INIT_MAX_RETRIES = 12       # reintents a l'arrencada

# Base de dades SQLite: un sol fitxer, al costat del script (/home/pi/historic.db).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historic.db")
SAVE_TO_DB = True           # False per desactivar el guardat sense tocar res mes

# Brillantor per color (0.0-1.0). El blau va mes alt perque es veu pitjor.
def dim(c, f):
    return tuple(int(x * f) for x in c)

WHITE = dim((255, 255, 255), 0.40)
GREEN = dim((0, 255, 0), 0.45)
RED = dim((255, 0, 0), 0.45)
BLUE = dim((0, 0, 255), 0.50)
# Taronja calibrat per brillar com el blanc actual (perceptualment similar).
ORANGE = dim((255, 140, 0), 0.50)
# Violeta pel rellotge binari (fosc pero saturat, blau ben dominant).
VIOLET = dim((120, 0, 230), 0.25)
OFF = (0, 0, 0)

# Rangs de confort, fixos tot l'any i a qualsevol lloc.
TEMP_MIN, TEMP_MAX = 20.0, 26.0
HUM_MIN, HUM_MAX = 40.0, 60.0

# Calibratge del sensor. Es resten a la lectura crua del Sense HAT.
TEMP_OFFSET = -3.6
HUM_OFFSET = -5.7

# Missatges aleatoris segons la sensacio termica (en majuscules, sense accents).
# Es poden canviar per qualsevol text; s'hi poden afegir mes frases a cada llista.
# De moment nomes n'hi ha 1 per estat, pero l'estructura de llista permet
# afegir-ne mes en el futur i s'escolliran aleatoriament.
MSGS_COLD = [
    "SENSACIO DE FRED",
]
MSGS_COMFORT = [
    "TEMPERATURA AGRADABLE",
]
MSGS_HOT = [
    "SENSACIO DE CALOR",
]

# --- Rellotge binari: files i alineacio ------------------------------------
# Fila 2 = hores (5 bits), fila 4 = minuts (6 bits), fila 6 = segons (6 bits).
# Files 0, 1, 3, 5, 7 buides (marge dalt/baix i separacio entre files).
# Totes alineades pel bit menys significatiu a la columna 7 (l'ultima,
# sense marge dret; columnes 0 i 1 sempre buides; hores nomes fa servir
# columnes 3-7, deixant la 2 buida addicional per ser 1 bit menys).
CLOCK_ROW_HOURS = 2
CLOCK_ROW_MINUTES = 4
CLOCK_ROW_SECONDS = 6
CLOCK_LSB_COLUMN = 7         # columna del bit menys significatiu (2^0)


# --- Base de dades -----------------------------------------------------

def init_db():
    # Crea el fitxer i la taula si no existeixen. Si ja existeixen, no toca res.
    # Ordre de columnes: data, hora, temperatura, humitat relativa, sensacio termica.
    # Data en format any-mes-dia (YYYY-MM-DD) i hora en 24 h (HH:MM): aixi
    # s'ordenen i es filtren be per data. Temperatura i humitat son els valors
    # REALS, ja amb l'offset aplicat (els mateixos que es mostren per pantalla);
    # la sensacio termica es la que es calcula per al missatge.
    # UNIQUE (data, hora): una sola mostra per quart d'hora. Si el servei es
    # reinicia dins el mateix quart, la segona mostra s'ignora (no es duplica).
    # Aquesta mateixa taula (mateixes columnes) es la que fa servir la copia
    # de seguretat, aixi que restaurar-la es copiar el fitxer.
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mostres (
                    id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL,
                    hora TEXT NOT NULL,
                    temperatura REAL NOT NULL,
                    humitat INTEGER NOT NULL,
                    sensacio_termica REAL NOT NULL,
                    UNIQUE (data, hora)
                )
                """
            )


def save_sample(when, t, h, at):
    # Obre la connexio, escriu una fila i tanca. Nomes passa cada 15 min.
    # 't' i 'h' son els valors ja corregits (els de la pantalla) i 'at' la
    # sensacio termica ja calculada per al missatge (no es recalcula).
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mostres (
                    data, hora, temperatura, humitat, sensacio_termica
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    when.strftime("%Y-%m-%d"),
                    when.strftime("%H:%M"),
                    t,
                    h,
                    round(at, 1),
                ),
            )


def apparent_temperature(t, h):
    # Australian AT (sense vent, aplicable a interior sense corrents d'aire).
    # AT = T + 0.33*e - 4.00,  on e = HR/100 * 6.105 * exp(17.27*T/(237.7+T))
    e = (h / 100.0) * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return t + 0.33 * e - 4.00


def pick_message(t, h):
    # Tria un missatge aleatori de la llista segons la sensacio termica,
    # no segons la temperatura seca.
    at = apparent_temperature(t, h)
    if at < TEMP_MIN:
        return random.choice(MSGS_COLD), at
    if at > TEMP_MAX:
        return random.choice(MSGS_HOT), at
    return random.choice(MSGS_COMFORT), at


def get_value_color(v, low, high):
    if v < low:
        return BLUE
    if v > high:
        return RED
    return GREEN


def read_sensors(sense):
    # Mitjana dels dos sensors de temperatura. Sense compensacio de CPU
    # perque el Sense HAT esta separat de la placa.
    t = (sense.get_temperature_from_humidity()
         + sense.get_temperature_from_pressure()) / 2 + TEMP_OFFSET
    # Humitat amb offset de calibratge, limitada a 0-100 %.
    # Sensor HTS221: precisio de +/-3,5 % HR, aixi que l'offset es una
    # aproximacio i la lectura es mostra sencera.
    h = max(0.0, min(100.0, sense.get_humidity() + HUM_OFFSET))
    return round(t, 1), round(h)


def show(sense, prefix, value, unit, color):
    # Tres crides perque show_message nomes admet un color cadascuna.
    sense.show_message(prefix, scroll_speed=SCROLL_SPEED, text_colour=WHITE)
    sense.show_message(value, scroll_speed=VALUE_SCROLL_SPEED, text_colour=color)
    sense.show_message(unit, scroll_speed=SCROLL_SPEED, text_colour=WHITE)


def read_sensors_retry(sense, attempts=2, wait=0.5):
    # Reintenta la lectura si falla (I2C ocasionalment es capriciós).
    for i in range(attempts):
        try:
            return read_sensors(sense)
        except Exception as e:
            print(f"  [avis] lectura {i+1}/{attempts} fallida: {e}")
            if i < attempts - 1:
                time.sleep(wait)
    return None


def display_cycle(sense):
    # Llegeix els sensors nomes al principi. La segona repeticio reutilitza
    # els mateixos valors, pero amb una frase aleatoria diferent.
    result = read_sensors_retry(sense)
    if result is None:
        print("  [avis] no s'han pogut llegir els sensors, s'omet la mostra")
        return
    t, h = result

    now = datetime.now()
    print(f"  [{now:%H:%M:%S}] T={t}C  HR={h}%")

    for i in range(REPEAT_COUNT):
        msg, at = pick_message(t, h)
        print(f"    repet {i+1}/{REPEAT_COUNT}  AT={at:.1f}C  msg='{msg}'")

        # Guarda la mostra una sola vegada (a la primera repeticio), amb la
        # sensacio termica que s'acaba de calcular per al missatge. Es fa abans
        # del scroll, que triga. Si la base de dades falla, nomes s'avisa: la
        # pantalla segueix igual.
        if i == 0 and SAVE_TO_DB:
            try:
                save_sample(now, t, h, at)
            except Exception as e:
                print(f"  [avis] no s'ha pogut guardar a la base de dades: {e}")

        # Missatge introductori aleatori (taronja) segons la sensacio termica.
        # Es pot desactivar amb SHOW_MESSAGE = False.
        if SHOW_MESSAGE:
            sense.show_message(msg, scroll_speed=SCROLL_SPEED, text_colour=ORANGE)
        show(sense, "T:", f"{t}", "oC", get_value_color(t, TEMP_MIN, TEMP_MAX))
        time.sleep(PAUSE_BETWEEN_MSGS)
        show(sense, "HR:", f"{h}", "%", get_value_color(h, HUM_MIN, HUM_MAX))

        # Entre repeticions, apaga i espera 1 minut.
        if i < REPEAT_COUNT - 1:
            sense.clear()
            time.sleep(PAUSE_BETWEEN_REPEATS)


# --- Rellotge binari ---------------------------------------------------

def draw_binary_row(pixels, row, value, num_bits, color):
    # Dibuixa 'value' en binari a la fila indicada, alineat pel bit menys
    # significatiu a CLOCK_LSB_COLUMN, MSB a l'esquerra.
    for i in range(num_bits):
        col = CLOCK_LSB_COLUMN - i
        bit_on = (value >> i) & 1
        pixels[row * 8 + col] = color if bit_on else OFF


def draw_clock(sense):
    now = datetime.now()
    pixels = [OFF] * 64
    draw_binary_row(pixels, CLOCK_ROW_HOURS, now.hour, 5, VIOLET)
    draw_binary_row(pixels, CLOCK_ROW_MINUTES, now.minute, 6, VIOLET)
    draw_binary_row(pixels, CLOCK_ROW_SECONDS, now.second, 6, VIOLET)
    sense.set_pixels(pixels)


def init_sense_hat():
    # A l'arrencada l'I2C pot no estar llest encara: reintenta 1 minut.
    for i in range(1, INIT_MAX_RETRIES + 1):
        try:
            sense = SenseHat()
            sense.set_rotation(ROTATION)
            sense.low_light = True
            sense.clear()
            return sense
        except Exception as e:
            print(f"[init] intent {i}/{INIT_MAX_RETRIES} fallit: {e}")
            if i < INIT_MAX_RETRIES:
                time.sleep(INIT_RETRY_SECONDS)
    raise RuntimeError("Sense HAT no disponible. Revisa el cable i l'I2C.")


def main():
    sense = init_sense_hat()

    # Si la base de dades no es pot crear, el monitor segueix funcionant
    # igualment (sense guardar), en comptes d'aturar-se.
    if SAVE_TO_DB:
        try:
            init_db()
            print(f"Base de dades: {DB_PATH}")
        except Exception as e:
            print(f"[avis] no s'ha pogut inicialitzar la base de dades: {e}")

    print("Monitor iniciat. Ctrl+C per aturar.")

    last_slot = None  # ultim quart d'hora (0-95, unic per dia) ja mostrat

    try:
        while True:
            try:
                now = datetime.now()
                slot = now.hour * 4 + now.minute // INTERVAL_MINUTES

                if now.minute % INTERVAL_MINUTES == 0 and slot != last_slot:
                    print(f"[{now:%Y-%m-%d %H:%M:%S}] --- mostra T/H ---")
                    sense.low_light = False
                    try:
                        display_cycle(sense)
                    except Exception as e:
                        print(f"[error] {e}")
                    try:
                        sense.clear()
                    except Exception:
                        pass
                    sense.low_light = True
                    last_slot = slot
                else:
                    draw_clock(sense)

            except Exception as e:
                # Cap error (I2C, sensor, el que sigui) atura el programa:
                # es registra i es continua amb la propera volta.
                print(f"[error bucle principal] {e}")

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nAturat per l'usuari.")
    finally:
        try:
            sense.clear()
        except Exception:
            pass


if __name__ == "__main__":
    main()
