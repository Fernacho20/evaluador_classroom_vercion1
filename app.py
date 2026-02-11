from flask import Flask, request, redirect, session, render_template
from datetime import timedelta
from cryptography.fernet import Fernet
from dotenv import load_dotenv
load_dotenv()
import bcrypt
import uuid
import sqlite3, uuid
import json
import uuid
import os
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-cambia-esto")
FERNET_KEY = os.environ.get("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY no definida en variables de entorno")
fernet = Fernet(FERNET_KEY)
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax"
)
DB = "database.db"

RUTAS_CUESTIONARIOS = {
    "Autoestima Rosenberg": "autoestima",
    "Estilos de aprendizaje": "estilos",
    "Habilidades": "habilidades",
    "Batería de Tamizaje": "tamizaje",
    "Cuestionario de Salud": "salud"
}

def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    return con

MAX_INTENTOS = 5
BLOQUEO_MINUTOS = 10

def esta_bloqueado(ip):
    with db() as con:
        fila = con.execute(
            "SELECT bloqueado_hasta FROM intentos_admin WHERE ip=?",
            (ip,)
        ).fetchone()

    return fila and fila["bloqueado_hasta"] and fila["bloqueado_hasta"] > int(time.time())

def registrar_fallo(ip):
    ahora = int(time.time())
    with db() as con:
        fila = con.execute(
            "SELECT intentos FROM intentos_admin WHERE ip=?",
            (ip,)
        ).fetchone()

        if fila:
            intentos = fila["intentos"] + 1
            if intentos >= MAX_INTENTOS:
                con.execute("""
                    UPDATE intentos_admin
                    SET intentos=?, bloqueado_hasta=?
                    WHERE ip=?
                """, (intentos, ahora + BLOQUEO_MINUTOS * 60, ip))
            else:
                con.execute("""
                    UPDATE intentos_admin
                    SET intentos=?
                    WHERE ip=?
                """, (intentos, ip))
        else:
            con.execute("""
                INSERT INTO intentos_admin (ip, intentos)
                VALUES (?, 1)
            """, (ip,))

with db() as con:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS clases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT,
        codigo TEXT UNIQUE
    );

    CREATE TABLE IF NOT EXISTS clase_cuestionarios(
        clase_id INTEGER,
        cuestionario TEXT
    );

    CREATE TABLE IF NOT EXISTS estudiantes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT,
        matricula TEXT,
        grupo TEXT,
        carrera TEXT NOT NULL,
        clase_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS resultados(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        estudiante_id INTEGER,
        cuestionario TEXT,
        resultado TEXT
    );

    CREATE TABLE IF NOT EXISTS intentos_admin (
        ip TEXT PRIMARY KEY,
        intentos INTEGER DEFAULT 0,
        bloqueado_hasta INTEGER
    );
                      
    CREATE TABLE IF NOT EXISTS sesiones (
        estudiante_id INTEGER PRIMARY KEY,
        token TEXT NOT NULL,
        user_agent TEXT,
        creada_en INTEGER DEFAULT (strftime('%s','now')),
        FOREIGN KEY(estudiante_id) REFERENCES estudiantes(id)
    );
    """)

@app.route("/")
def inicio():
    return redirect("/orientacion")

@app.route("/orientacion", methods=["GET","POST"])
def login():
    ip = request.remote_addr

    if esta_bloqueado(ip):
        return "Demasiados intentos. Intenta más tarde.", 429

    if request.method == "POST":
        u = request.form["u"]
        p = request.form["p"]

        admin_user = os.getenv("ADMIN_USER")
        admin_hash = os.getenv("ADMIN_PASS_HASH")

        if u == admin_user and bcrypt.checkpw(p.encode(), admin_hash.encode()):
            limpiar_intentos(ip)
            session["admin"] = True
            return redirect("/dashboard")

        registrar_fallo(ip)
        return "Credenciales incorrectas"

    return render_template("login.html")

def limpiar_intentos(ip):
    with db() as con:
        con.execute("DELETE FROM intentos_admin WHERE ip=?", (ip,))

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/orientacion")

@app.route("/dashboard", methods=["GET","POST"])
def dashboard():
    if not session.get("admin"):
        return redirect("/orientacion")
    
    clase_seleccionada = request.args.get("clase_id")
    stats_grupo = None

    with db() as con:

        if request.method == "POST":
            nombre = request.form["nombre"]
            codigo = uuid.uuid4().hex[:6].upper()

            con.execute(
                "INSERT INTO clases (nombre, codigo) VALUES (?,?)",
                (nombre, codigo)
            )

            clase_id = con.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

            for c in request.form.getlist("cuestionarios"):
                con.execute(
                    "INSERT INTO clase_cuestionarios VALUES (?,?)",
                    (clase_id, c)
                )

        clases = con.execute(
            "SELECT * FROM clases"
        ).fetchall()

        resultados = con.execute("""
            SELECT clases.nombre, resultados.cuestionario,
                   resultados.resultado, COUNT(*) total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            GROUP BY clases.nombre, resultados.cuestionario, resultados.resultado
        """).fetchall()

        autoestima = con.execute("""
            SELECT clases.nombre AS clase,
                   resultados.resultado,
                   COUNT(*) AS total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario = 'Autoestima Rosenberg'
            GROUP BY clases.nombre, resultados.resultado
        """).fetchall()

        autoestima_riesgo = con.execute("""
            SELECT clases.nombre,
                   ROUND(
                       100.0 * SUM(
                           CASE WHEN resultados.resultado = 'Autoestima Baja'
                           THEN 1 ELSE 0 END
                       ) / COUNT(*), 1
                   ) AS porcentaje_baja
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario = 'Autoestima Rosenberg'
            GROUP BY clases.nombre
        """).fetchall()

        salud_riesgo = con.execute("""
            SELECT clases.nombre,
                ROUND(
                    100.0 * SUM(
                        CASE WHEN resultados.resultado IN
                        ('Riesgo moderado','Riesgo alto')
                        THEN 1 ELSE 0 END
                    ) / COUNT(*),1
                ) porcentaje
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario='Cuestionario de Salud'
            GROUP BY clases.nombre
        """).fetchall()

        habilidades = con.execute("""
            SELECT clases.nombre,
                   resultados.resultado,
                   COUNT(*) total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario = 'Habilidades'
            GROUP BY clases.nombre, resultados.resultado
        """).fetchall()

        estilos = con.execute("""
            SELECT clases.nombre,
                resultados.resultado AS estilo,
                ROUND(
                    100.0 * COUNT(*) /
                    (
                        SELECT COUNT(*)
                        FROM resultados r2
                        JOIN estudiantes e2 ON e2.id = r2.estudiante_id
                        WHERE e2.clase_id = clases.id
                        AND r2.cuestionario = 'Estilos de aprendizaje'
                    ), 1
                ) porcentaje
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario = 'Estilos de aprendizaje'
            GROUP BY clases.nombre, estilo
        """).fetchall()

        tamizaje = con.execute("""
            SELECT DISTINCT clases.nombre
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario LIKE 'Tamizaje -%'
            AND resultados.resultado IN ('Requiere evaluación','Consumo de riesgo','Elevado','Moderado')
        """).fetchall()

        salud = con.execute("""
            SELECT clases.nombre,
                   COUNT(*) total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            JOIN clases ON clases.id = estudiantes.clase_id
            WHERE resultados.cuestionario = 'Cuestionario de Salud'
            GROUP BY clases.nombre
        """).fetchall()

        entregas = con.execute("""
        SELECT clases.nombre,
            COUNT(DISTINCT estudiantes.id) AS registrados,
            COUNT(DISTINCT resultados.estudiante_id) AS entregados
        FROM clases
        LEFT JOIN estudiantes ON estudiantes.clase_id = clases.id
        LEFT JOIN resultados ON resultados.estudiante_id = estudiantes.id
        GROUP BY clases.id
        """).fetchall()

        if clase_seleccionada:
            stats_grupo = {}

            total_grupo = con.execute("""
                SELECT COUNT(DISTINCT estudiantes.id) total
                FROM estudiantes
                JOIN resultados ON resultados.estudiante_id = estudiantes.id
                WHERE estudiantes.clase_id=?
            """, (clase_seleccionada,)).fetchone()["total"]

            stats_grupo["total"] = total_grupo

            stats_grupo["estilos"] = con.execute("""
                SELECT resultados.resultado AS estilo,
                    COUNT(*) total,
                    ROUND(100.0 * COUNT(*) /
                        (SELECT COUNT(*)
                        FROM resultados r2
                        JOIN estudiantes e2 ON e2.id = r2.estudiante_id
                        WHERE e2.clase_id=?
                        AND r2.cuestionario='Estilos de aprendizaje'), 1
                    ) porcentaje
                FROM resultados
                JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
                WHERE estudiantes.clase_id=?
                AND resultados.cuestionario='Estilos de aprendizaje'
                GROUP BY resultados.resultado
            """, (clase_seleccionada, clase_seleccionada)).fetchall()

            stats_grupo["autoestima"] = con.execute("""
                SELECT resultados.resultado,
                    COUNT(*) total,
                    ROUND(100.0 * COUNT(*) / ?, 1) porcentaje
                FROM resultados
                JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
                WHERE estudiantes.clase_id=?
                AND resultados.cuestionario='Autoestima Rosenberg'
                GROUP BY resultados.resultado
            """, (total_grupo, clase_seleccionada)).fetchall()

            stats_grupo["habilidades"] = con.execute("""
                SELECT resultados.resultado,
                    COUNT(*) total,
                    ROUND(100.0 * COUNT(*) / ?, 1) porcentaje
                FROM resultados
                JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
                WHERE estudiantes.clase_id=?
                AND resultados.cuestionario='Habilidades'
                GROUP BY resultados.resultado
            """, (total_grupo, clase_seleccionada)).fetchall()

            stats_grupo["tamizaje"] = {}

            for area in [
                "Tamizaje - Depresión",
                "Tamizaje - Ansiedad",
                "Tamizaje - Alcohol",
                "Tamizaje - Neurodivergencia"
            ]:
                stats_grupo["tamizaje"][area] = con.execute("""
                    SELECT resultado,
                        COUNT(*) total,
                        ROUND(100.0 * COUNT(*) / ?, 1) porcentaje
                    FROM resultados
                    JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
                    WHERE estudiantes.clase_id=?
                    AND resultados.cuestionario=?
                    GROUP BY resultado
                """, (total_grupo, clase_seleccionada, area)).fetchall()

            stats_grupo["total"] = total_grupo

            stats_grupo["salud"] = con.execute("""
                SELECT
                    CASE
                        WHEN instr(resultados.resultado, ' | ') > 0
                        THEN substr(resultados.resultado, 1, instr(resultados.resultado, ' | ') - 1)
                        ELSE resultados.resultado
                    END AS resultado,
                    COUNT(*) total,
                    ROUND(100.0 * COUNT(*) / ?, 1) porcentaje
                FROM resultados
                JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
                WHERE estudiantes.clase_id=?
                AND resultados.cuestionario='Cuestionario de Salud'
                GROUP BY resultado
            """, (total_grupo, clase_seleccionada)).fetchall()

    return render_template(
        "dashboard.html",
        clases=clases,
        datos=resultados,
        autoestima=autoestima,
        autoestima_riesgo=autoestima_riesgo,
        salud_riesgo=salud_riesgo,
        habilidades=habilidades,
        estilos=estilos,
        entregas=entregas,
        stats_grupo=stats_grupo,
        clase_seleccionada=clase_seleccionada,
        tamizaje=tamizaje
    )

def limpiar_resultado(texto):
    if not texto:
        return texto
    if " | " in texto:
        return texto.split(" | ", 1)[0]
    return texto

@app.route("/c/<codigo>")
def acceso_clase(codigo):

    if "estudiante" in session:
        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    with db() as con:
        clase = con.execute(
            "SELECT * FROM clases WHERE codigo=?",
            (codigo,)
        ).fetchone()

        if not clase:
            return "Clase no encontrada"

        tests = con.execute(
            "SELECT cuestionario FROM clase_cuestionarios WHERE clase_id=?",
            (clase["id"],)
        ).fetchall()

    return render_template(
        "acceso_clase.html",
        clase=clase,
        clase_id=clase["id"],
        tests=[t["cuestionario"] for t in tests]
    )

@app.route("/registro", methods=["POST"])
def registro():

    if "estudiante" in session:
        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    token = uuid.uuid4().hex
    user_agent = request.headers.get("User-Agent")

    with db() as con:
        con.execute("""
            INSERT INTO estudiantes
            (nombre, matricula, grupo, carrera, clase_id)
            VALUES (?,?,?,?,?)
        """, (
            request.form["nombre"],
            request.form["matricula"],
            request.form.get("grupo", ""),
            request.form["carrera"],
            request.form["clase_id"]
        ))

        estudiante_id = con.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        con.execute("""
            INSERT INTO sesiones (estudiante_id, token, user_agent)
            VALUES (?,?,?)
        """, (estudiante_id, token, user_agent))

    session["estudiante"] = estudiante_id
    session["token"] = token

    return redirect("/" + siguiente_cuestionario(estudiante_id))

@app.route("/eliminar_clase/<int:id>", methods=["POST"])
def eliminar_clase(id):
    if not session.get("admin"):
        return redirect("/orientacion")

    with db() as con:
        con.execute("DELETE FROM resultados WHERE estudiante_id IN (SELECT id FROM estudiantes WHERE clase_id=?)", (id,))
        con.execute("DELETE FROM estudiantes WHERE clase_id=?", (id,))
        con.execute("DELETE FROM clase_cuestionarios WHERE clase_id=?", (id,))
        con.execute("DELETE FROM clases WHERE id=?", (id,))

    return redirect("/dashboard")

@app.route("/salud_detalle/<nombre>")
def salud_detalle(nombre):
    if not session.get("admin"):
        return redirect("/orientacion")

    with db() as con:
        fila = con.execute("""
            SELECT resultado
            FROM resultados r
            JOIN estudiantes e ON e.id = r.estudiante_id
            WHERE e.nombre=?
            AND r.cuestionario='Cuestionario de Salud'
            ORDER BY r.id DESC
            LIMIT 1
        """, (nombre,)).fetchone()

    if not fila:
        return "Sin datos"

    texto = fernet.decrypt(fila["resultado"].encode()).decode()

    if " | " in texto:
        nivel, respuestas = texto.split(" | ", 1)
        respuestas = json.loads(respuestas)
    else:
        nivel = texto
        respuestas = {"Información": "Registro antiguo sin respuestas guardadas"}

    respuestas = {k: v for k, v in respuestas.items() if k != "alumno"}

    return render_template(
        "salud_detalle.html",
        alumno=nombre,
        nivel=nivel,
        respuestas=respuestas
    )

@app.route("/clase/<int:clase_id>/resultados")
def resultados_clase(clase_id):
    if not session.get("admin"):
        return redirect("/orientacion")

    with db() as con:
        clase = con.execute(
            "SELECT * FROM clases WHERE id=?",
            (clase_id,)
        ).fetchone()

        estilos = con.execute("""
            SELECT resultados.resultado AS estilo,
                   COUNT(*) total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            WHERE estudiantes.clase_id=?
            AND resultados.cuestionario='Estilos de aprendizaje'
            GROUP BY resultados.resultado
        """, (clase_id,)).fetchall()

        autoestima = con.execute("""
            SELECT resultados.resultado,
                   COUNT(*) total
            FROM resultados
            JOIN estudiantes ON estudiantes.id = resultados.estudiante_id
            WHERE estudiantes.clase_id=?
            AND resultados.cuestionario='Autoestima Rosenberg'
            GROUP BY resultados.resultado
        """, (clase_id,)).fetchall()

    return render_template(
        "resultados_clase.html",
        clase=clase,
        estilos=estilos,
        autoestima=autoestima
    )

@app.route("/clase/<int:clase_id>/alumnos")
def alumnos_clase(clase_id):
    if not session.get("admin"):
        return redirect("/orientacion")

    alumno = request.args.get("alumno", "")
    carrera = request.args.get("carrera", "")
    cuestionario = request.args.get("cuestionario", "")
    resultado = request.args.get("resultado", "")

    with db() as con:
        clase = con.execute(
            "SELECT * FROM clases WHERE id=?",
            (clase_id,)
        ).fetchone()

        alumnos = con.execute("""
            SELECT e.nombre, e.carrera,
                r.cuestionario,
                CASE
                    WHEN r.cuestionario = 'Cuestionario de Salud'
                    THEN substr(r.resultado, 1, instr(r.resultado, ' |') - 1)
                    ELSE r.resultado
                END AS resultado
            FROM estudiantes e
            LEFT JOIN resultados r ON r.estudiante_id = e.id
            WHERE e.clase_id=?
            AND e.nombre LIKE ?
            AND e.carrera LIKE ?
            AND IFNULL(r.cuestionario,'') LIKE ?
            AND IFNULL(r.resultado,'') LIKE ?
            ORDER BY e.nombre
        """, (
            clase_id,
            f"%{alumno}%",
            f"%{carrera}%",
            f"%{cuestionario}%",
            f"%{resultado}%"
        )).fetchall()

    return render_template(
        "alumnos_clase.html",
        clase=clase,
        alumnos=alumnos
    )

@app.route("/habilidades", methods=["GET","POST"])
def habilidades():
    if "estudiante" not in session:
        return redirect("/")

    if not validar_sesion_alumno():
        session.clear()
        return redirect("/")
    
    if request.method == "POST":
        no = sum(1 for v in request.form.values() if v == "no")

        nivel = (
            "Muy bajo" if no >= 16 else
            "Bajo" if no >= 13 else
            "Promedio" if no >= 10 else
            "Adecuado"
        )

        with db() as con:
            con.execute(
                "INSERT INTO resultados (estudiante_id, cuestionario, resultado) VALUES (?,?,?)",
                (session["estudiante"], "Habilidades", nivel)
            )

        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    return render_template("habilidades.html", alumno=alumno_actual())

def siguiente_cuestionario(estudiante_id):
    with db() as con:
        fila = con.execute(
            "SELECT clase_id FROM estudiantes WHERE id=?",
            (estudiante_id,)
        ).fetchone()

        if not fila:
            return None

        clase_id = fila["clase_id"]

        hechos = con.execute("""
            SELECT cuestionario
            FROM resultados
            WHERE estudiante_id=?
        """, (estudiante_id,)).fetchall()

        hechos = {h["cuestionario"] for h in hechos}

        pendientes = con.execute("""
            SELECT cuestionario
            FROM clase_cuestionarios
            WHERE clase_id=?
            ORDER BY rowid
        """, (clase_id,)).fetchall()

        for p in pendientes:
            nombre = p["cuestionario"]

            if nombre == "Batería de Tamizaje":
                if any(h.startswith("Tamizaje -") for h in hechos):
                    continue
                return "tamizaje"

            if nombre not in hechos:
                return RUTAS_CUESTIONARIOS[nombre]

    return None

def alumno_actual():
    if "estudiante" not in session:
        return None

    with db() as con:
        return con.execute("""
            SELECT nombre, matricula, grupo, carrera
            FROM estudiantes
            WHERE id=?
        """, (session["estudiante"],)).fetchone()

def validar_sesion_alumno():
    if "estudiante" not in session or "token" not in session:
        return False

    with db() as con:
        fila = con.execute("""
            SELECT token
            FROM sesiones
            WHERE estudiante_id=?
        """, (session["estudiante"],)).fetchone()

    return fila and fila["token"] == session["token"]

@app.route("/historial_salud")
def historial_salud():
    if not session.get("admin"):
        return redirect("/orientacion")

    with db() as con:
        datos = con.execute("""
            SELECT c.nombre AS clase,
                   e.nombre AS alumno,
                   r.resultado
            FROM resultados r
            JOIN estudiantes e ON e.id = r.estudiante_id
            JOIN clases c ON c.id = e.clase_id
            WHERE r.cuestionario = 'Cuestionario de Salud'
            ORDER BY c.nombre, e.nombre
        """).fetchall()

    return render_template("historial_salud.html", datos=datos)

@app.route("/estilos", methods=["GET","POST"])
def estilos():
    if "estudiante" not in session:
        return redirect("/")

    if request.method == "POST":
        respuestas = {
            k: int(v)
            for k, v in request.form.items()
            if k.startswith("p")
        }

        estilos_calc = {
            "Activo": respuestas["p1"] + respuestas["p5"] + respuestas["p9"] + respuestas["p13"] + respuestas["p17"],
            "Reflexivo": respuestas["p2"] + respuestas["p6"] + respuestas["p10"] + respuestas["p14"] + respuestas["p18"],
            "Teórico": respuestas["p3"] + respuestas["p7"] + respuestas["p11"] + respuestas["p15"] + respuestas["p19"],
            "Pragmático": respuestas["p4"] + respuestas["p8"] + respuestas["p12"] + respuestas["p16"] + respuestas["p20"]
        }

        estilo_principal = max(estilos_calc, key=estilos_calc.get)

        with db() as con:
            con.execute(
                "INSERT INTO resultados (estudiante_id, cuestionario, resultado) VALUES (?,?,?)",
                (session["estudiante"], "Estilos de aprendizaje", estilo_principal)
            )

        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    return render_template("estilos.html")

@app.route("/autoestima", methods=["GET","POST"])
def autoestima():
    if "estudiante" not in session:
        return redirect("/")

    if request.method == "POST":
        respuestas = {}

        for k, v in request.form.items():
            if k.startswith("p") and v.isdigit():
                respuestas[k] = int(v)

        if len(respuestas) != 10:
            return "Error: faltan respuestas en el cuestionario", 400

        puntaje = sum(respuestas.values())

        nivel = (
            "Autoestima Baja" if puntaje <= 15 else
            "Autoestima Media" if puntaje <= 25 else
            "Autoestima Alta"
        )

        with db() as con:
            con.execute(
                "INSERT INTO resultados VALUES (NULL,?,?,?)",
                (session["estudiante"], "Autoestima Rosenberg", nivel)
            )

        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    return render_template("autoestima.html")

@app.route("/tamizaje", methods=["GET", "POST"])
def tamizaje():
    if "estudiante" not in session:
        return redirect("/")

    if request.method == "POST":
        dep = sum(int(request.form[f"d{i}"]) for i in range(1, 3))
        dep_resultado = "Sin indicios" if dep <= 2 else "Requiere evaluación"

        anx = sum(int(request.form[f"a{i}"]) for i in range(1, 3))
        anx_resultado = "Sin indicios" if anx <= 2 else "Requiere evaluación"

        alcohol = sum(int(request.form[f"al{i}"]) for i in range(1, 4))
        alcohol_resultado = "Consumo de riesgo" if alcohol >= 4 else "Sin riesgo"

        neuro = sum(int(request.form[f"n{i}"]) for i in range(1, 18))

        if neuro <= 16:
            neuro_resultado = "No significativo"
        elif neuro <= 28:
            neuro_resultado = "Leve"
        elif neuro <= 40:
            neuro_resultado = "Moderado"
        else:
            neuro_resultado = "Elevado"

        resultado_final = (
            f"Depresión: {dep_resultado} | "
            f"Ansiedad: {anx_resultado} | "
            f"Alcohol: {alcohol_resultado} | "
            f"Neurodivergencia: {neuro_resultado}"
        )

        with db() as con:
            con.executemany(
                "INSERT INTO resultados (estudiante_id, cuestionario, resultado) VALUES (?,?,?)",
                [
                    (session["estudiante"], "Tamizaje - Depresión", dep_resultado),
                    (session["estudiante"], "Tamizaje - Ansiedad", anx_resultado),
                    (session["estudiante"], "Tamizaje - Alcohol", alcohol_resultado),
                    (session["estudiante"], "Tamizaje - Neurodivergencia", neuro_resultado),
                ]
            )

        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    return render_template("tamizaje.html")

@app.route("/salud", methods=["GET","POST"])
def salud():
    if not validar_sesion_alumno():
        session.clear()
        return redirect("/")

    alumno = alumno_actual()

    if request.method == "POST":

        puntos = 0
        for v in request.form.values():
            if v.lower() == "si":
                puntos += 1

        if puntos <= 2:
            nivel = "Salud adecuada"
        elif puntos <= 5:
            nivel = "Riesgo leve"
        elif puntos <= 8:
            nivel = "Riesgo moderado"
        else:
            nivel = "Riesgo alto"

        respuestas = dict(request.form)

        texto = nivel + " | " + json.dumps(respuestas)
        resultado_cifrado = fernet.encrypt(texto.encode()).decode()

        with db() as con:
            con.execute("""
                INSERT INTO resultados (estudiante_id, cuestionario, resultado)
                VALUES (?,?,?)
            """, (
                session["estudiante"],
                "Cuestionario de Salud",
                resultado_cifrado
            ))

        ruta = siguiente_cuestionario(session["estudiante"])
        return redirect(f"/{ruta}" if ruta else "/final")

    return render_template("salud.html", alumno=alumno)

@app.route("/final")
def final():
    session.pop("estudiante", None)
    return render_template("final.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
