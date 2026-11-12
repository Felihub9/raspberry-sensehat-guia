#!/usr/bin/env python3
# Copia de seguretat INCREMENTAL de historic.db al USB (/mnt/backup).
#
# Hi ha un unic fitxer de copia (historic_backup.db) que va creixent: cada
# execucio hi afegeix al final les files que encara no hi son, sense esborrar
# mai res. Es pot executar tantes vegades com es vulgui: les files que ja hi
# son s'ignoren (restriccio UNIQUE sobre data + hora).
#
# - Comprova que el USB esta realment muntat (si no, escriuria a la SD).
# - Comprova la integritat de la copia existent abans de tocar-la.
# - Si alguna cosa falla, no modifica res i surt amb codi d'error.

import os
import sqlite3
import sys
from contextlib import closing

DB = os.environ.get("DB", "/home/pi/historic.db")
DEST = os.environ.get("DEST", "/mnt/backup")
BACKUP_NAME = "historic_backup.db"

SCHEMA = """
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


def log(msg, err=False):
    print(f"[backup] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def backup(db_path, dest_dir):
    if not os.path.ismount(dest_dir):
        log(f"ERROR: {dest_dir} no esta muntat (USB absent?). No es fa copia.", err=True)
        return 1
    if not os.path.isfile(db_path):
        log(f"ERROR: no existeix {db_path}", err=True)
        return 1

    backup_path = os.path.join(dest_dir, BACKUP_NAME)
    try:
        with closing(sqlite3.connect(backup_path, timeout=10)) as conn:
            # Abans de tocar res, comprova que la copia existent esta sana.
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                log(f"ERROR: {backup_path} no passa la comprovacio d'integritat: {result}",
                    err=True)
                return 1

            conn.execute(SCHEMA)
            conn.execute("ATTACH DATABASE ? AS src", (db_path,))

            # Afegeix al final les files que falten (transaccio: tot o res).
            with conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO main.mostres
                        (data, hora, temperatura, humitat, sensacio_termica)
                    SELECT data, hora, temperatura, humitat, sensacio_termica
                    FROM src.mostres
                    ORDER BY data, hora
                    """
                )
                added = cur.rowcount

            total = conn.execute("SELECT COUNT(*) FROM main.mostres").fetchone()[0]
            source_total = conn.execute("SELECT COUNT(*) FROM src.mostres").fetchone()[0]
    except sqlite3.Error as e:
        log(f"ERROR: {e}", err=True)
        return 1

    log(f"OK: {added} files noves afegides; total a la copia: {total}; "
        f"a l'original: {source_total}")
    return 0


if __name__ == "__main__":
    sys.exit(backup(DB, DEST))
