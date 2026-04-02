#!/usr/bin/env python3
"""
app.py  — Serveur Flask pour ERD Explorer
==========================================
Lance avec :   python app.py
Puis ouvre :   http://localhost:5050

Dépendances :
    pip install flask psycopg2-binary
"""

import json
import queue
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

try:
    import psycopg2
except ImportError:
    print("ERREUR : pip install psycopg2-binary")
    raise

app = Flask(__name__)

# ─────────────────────────────────────────────
#  PostgreSQL helpers (repris de pg_erd_generator)
# ─────────────────────────────────────────────

def pg_connect(host, port, dbname, user, password):
    return psycopg2.connect(host=host, port=int(port),
                            dbname=dbname, user=user, password=password)


def extract_tables(cur, schema):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema=%s AND table_type='BASE TABLE'
        ORDER BY table_name;
    """, (schema,))
    return [r[0] for r in cur.fetchall()]


def extract_columns(cur, schema, table):
    cur.execute("""
        SELECT c.column_name, c.data_type, c.character_maximum_length,
               c.is_nullable, c.column_default,
               CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END,
               CASE WHEN uq.column_name IS NOT NULL THEN true ELSE false END
        FROM information_schema.columns c
        LEFT JOIN (
            SELECT kcu.column_name FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
            WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema=%s AND tc.table_name=%s
        ) pk ON c.column_name=pk.column_name
        LEFT JOIN (
            SELECT kcu.column_name FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
            WHERE tc.constraint_type='UNIQUE' AND tc.table_schema=%s AND tc.table_name=%s
        ) uq ON c.column_name=uq.column_name
        WHERE c.table_schema=%s AND c.table_name=%s
        ORDER BY c.ordinal_position;
    """, (schema, table, schema, table, schema, table))
    result = []
    for r in cur.fetchall():
        t = r[1]
        if r[2]: t += f"({r[2]})"
        result.append({
            "name": r[0], "type": t,
            "nullable": r[3] == "YES", "default": r[4],
            "primary_key": bool(r[5]), "unique": bool(r[6]),
        })
    return result


def extract_foreign_keys(cur, schema):
    cur.execute("""
        SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name, tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON tc.constraint_name=ccu.constraint_name AND tc.table_schema=ccu.table_schema
        WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s
        ORDER BY tc.table_name, kcu.column_name;
    """, (schema,))
    return [{"from_table": r[0], "from_column": r[1],
             "to_table": r[2], "to_column": r[3], "constraint": r[4]}
            for r in cur.fetchall()]


# ─────────────────────────────────────────────
#  Génération ERD HTML (repris de pg_erd_generator)
# ─────────────────────────────────────────────

def build_erd_html(schema_data, db_name, schema_name):
    tables_json = json.dumps(schema_data["tables"], ensure_ascii=False)
    fks_json    = json.dumps(schema_data["foreign_keys"], ensure_ascii=False)
    generated   = datetime.now().strftime("%d/%m/%Y a %H:%M")
    tpl = open("templates/erd.html").read()
    tpl = tpl.replace("__DB_NAME__",     db_name)
    tpl = tpl.replace("__SCHEMA__",      schema_name)
    tpl = tpl.replace("__GENERATED__",   generated)
    tpl = tpl.replace("__TABLES_JSON__", tables_json)
    tpl = tpl.replace("__FKS_JSON__",    fks_json)
    return tpl


# ─────────────────────────────────────────────
#  Routes Flask
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    """
    SSE endpoint : reçoit les paramètres de connexion,
    stream les événements de progression, puis renvoie le HTML final.
    """
    data = request.json or {}
    host     = data.get("host", "localhost")
    port     = data.get("port", 5432)
    dbname   = data.get("dbname", "")
    user     = data.get("user", "")
    password = data.get("password", "")
    schema   = data.get("schema", "public")

    def event_stream():
        def emit(pct, msg, status="running"):
            payload = json.dumps({"pct": pct, "msg": msg, "status": status})
            return f"data: {payload}\n\n"

        try:
            # ── Étape 1 : Connexion
            yield emit(5, f"Connexion à {host}:{port}/{dbname}...")
            time.sleep(0.2)
            try:
                conn = pg_connect(host, port, dbname, user, password)
            except Exception as e:
                yield emit(0, f"Echec de connexion : {e}", "error")
                return
            yield emit(10, f"Connexion établie ✓")
            time.sleep(0.1)

            schema_data = {"tables": {}, "foreign_keys": []}

            with conn.cursor() as cur:
                # ── Étape 2 : Liste des tables
                yield emit(15, f"Lecture du schéma « {schema} »...")
                time.sleep(0.1)
                tables = extract_tables(cur, schema)
                if not tables:
                    yield emit(0, f"Aucune table trouvée dans le schéma « {schema} »", "error")
                    conn.close()
                    return
                yield emit(20, f"{len(tables)} table(s) trouvée(s) ✓")
                time.sleep(0.1)

                # ── Étape 3 : Colonnes table par table
                total = len(tables)
                for i, tbl in enumerate(tables):
                    pct = 20 + int((i / total) * 55)
                    yield emit(pct, f"Analyse de {tbl}...")
                    cols = extract_columns(cur, schema, tbl)
                    schema_data["tables"][tbl] = cols

                yield emit(77, f"Colonnes extraites pour {total} table(s) ✓")
                time.sleep(0.1)

                # ── Étape 4 : Clés étrangères
                yield emit(80, "Extraction des clés étrangères (FK)...")
                time.sleep(0.1)
                fks = extract_foreign_keys(cur, schema)
                schema_data["foreign_keys"] = fks
                yield emit(90, f"{len(fks)} relation(s) FK trouvée(s) ✓")
                time.sleep(0.1)

            conn.close()

            # ── Étape 5 : Génération HTML
            yield emit(93, "Génération du diagramme ERD...")
            time.sleep(0.15)
            html_content = build_erd_html(schema_data, dbname, schema)
            yield emit(98, "Diagramme ERD généré ✓")
            time.sleep(0.2)

            # ── Fin : envoyer le HTML en base64
            import base64
            b64 = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
            payload = json.dumps({"pct": 100, "msg": "Terminé !", "status": "done", "html_b64": b64})
            yield f"data: {payload}\n\n"

        except Exception as e:
            yield emit(0, f"Erreur inattendue : {e}", "error")

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


if __name__ == "__main__":
    print("\n  ERD Explorer — http://localhost:5050\n")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
