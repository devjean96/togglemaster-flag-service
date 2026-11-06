import os
import sys
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from functools import wraps
import logging
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from telemetry import init_telemetry

# Configura o logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Carrega .env para desenvolvimento local
load_dotenv()

app = Flask(__name__)
init_telemetry(app, default_service_name="flag-service")

tracer = trace.get_tracer("togglemaster/flag-service")
meter = metrics.get_meter("togglemaster/flag-service")
operation_counter = meter.create_counter(
    "togglemaster.feature_flag.operations",
    unit="1",
    description="Operações realizadas sobre feature flags",
)
auth_counter = meter.create_counter(
    "togglemaster.feature_flag.auth_checks",
    unit="1",
    description="Validações de autenticação solicitadas pelo flag-service",
)

# --- Configuração ---
DATABASE_URL = os.getenv("DATABASE_URL")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL")

if not DATABASE_URL or not AUTH_SERVICE_URL:
    log.critical("Erro: DATABASE_URL e AUTH_SERVICE_URL devem ser definidos.")
    sys.exit(1)

# --- Pool de Conexão com o Banco ---
# Inicializa o pool de conexões (Mín: 1, Máx: 5 conexões)
try:
    pool = SimpleConnectionPool(1, 5, dsn=DATABASE_URL)
    log.info("Pool de conexões com o PostgreSQL inicializado.")
except psycopg2.OperationalError as e:
    log.critical(f"Erro fatal ao conectar ao PostgreSQL: {e}")
    sys.exit(1)


# --- Middleware de Autenticação ---
def require_auth(f):
    """Middleware para validar a chave de API contra o auth-service"""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            auth_counter.add(1, {"auth.result": "missing_credentials"})
            return jsonify({"error": "Authorization header obrigatório"}), 401

        try:
            # Chama o /validate do auth-service
            validate_url = f"{AUTH_SERVICE_URL}/validate"
            response = requests.get(validate_url, headers={"Authorization": auth_header}, timeout=3)

            if response.status_code != 200:
                auth_counter.add(1, {"auth.result": "invalid"})
                log.warning(f"Falha na validação da chave (status: {response.status_code})")
                return jsonify({"error": "Chave de API inválida"}), 401

            auth_counter.add(1, {"auth.result": "valid"})

        except requests.exceptions.Timeout:
            auth_counter.add(1, {"auth.result": "timeout"})
            log.error("Timeout ao conectar com o auth-service")
            return jsonify(
                {"error": "Serviço de autenticação indisponível (timeout)"}
            ), 504  # Gateway Timeout
        except requests.exceptions.RequestException as e:
            auth_counter.add(1, {"auth.result": "unavailable"})
            log.error(f"Erro ao conectar com o auth-service: {e}")
            return jsonify(
                {"error": "Serviço de autenticação indisponível"}
            ), 503  # Service Unavailable

        # Se a chave for válida, continua para a rota
        return f(*args, **kwargs)

    return decorated


def observe_flag_operation(operation):
    """Cria um span de negócio sem registrar payloads ou credenciais."""

    def decorator(f):
        @wraps(f)
        def observed(*args, **kwargs):
            with tracer.start_as_current_span(f"feature_flag.{operation}") as span:
                if "name" in kwargs:
                    span.set_attribute("feature_flag.name", kwargs["name"])
                try:
                    response = f(*args, **kwargs)
                    status_code = (
                        response[1]
                        if isinstance(response, tuple) and len(response) > 1
                        else getattr(response, "status_code", 200)
                    )
                    span.set_attribute("feature_flag.operation", operation)
                    span.set_attribute("http.response.status_code", status_code)
                    operation_counter.add(
                        1,
                        {
                            "feature_flag.operation": operation,
                            "http.response.status_code": status_code,
                        },
                    )
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR, "operation failed"))
                    return response
                except Exception as error:
                    span.record_exception(error)
                    span.set_status(Status(StatusCode.ERROR, "unhandled operation error"))
                    raise

        return observed

    return decorator


# --- Endpoints da API ---


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/flags", methods=["POST"])
@require_auth
@observe_flag_operation("create")
def create_flag():
    """Cria uma nova definição de feature flag"""
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"error": "'name' é obrigatório"}), 400

    name = data["name"]
    description = data.get("description", "")
    is_enabled = data.get("is_enabled", False)

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "INSERT INTO flags (name, description, is_enabled, created_at, updated_at) "
            "VALUES (%s, %s, %s, NOW(), NOW()) RETURNING *",
            (name, description, is_enabled),
        )
        new_flag = cur.fetchone()
        conn.commit()
        log.info(f"Flag '{name}' criada com sucesso.")
        return jsonify(new_flag), 201
    except psycopg2.IntegrityError:
        if conn:
            conn.rollback()
        log.warning(f"Tentativa de criar flag duplicada: '{name}'")
        return jsonify({"error": f"Flag '{name}' já existe"}), 409
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao criar flag: {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route("/flags", methods=["GET"])
@require_auth
@observe_flag_operation("list")
def get_flags():
    """Lista todas as feature flags"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM flags ORDER BY name")
        flags = cur.fetchall()
        return jsonify(flags)
    except Exception as e:
        log.error(f"Erro ao buscar flags: {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route("/flags/<string:name>", methods=["GET"])
@require_auth
@observe_flag_operation("get")
def get_flag(name):
    """Busca uma feature flag específica pelo nome"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM flags WHERE name = %s", (name,))
        flag = cur.fetchone()
        if not flag:
            return jsonify({"error": "Flag não encontrada"}), 404
        return jsonify(flag)
    except Exception as e:
        log.error(f"Erro ao buscar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route("/flags/<string:name>", methods=["PUT"])
@require_auth
@observe_flag_operation("update")
def update_flag(name):
    """Atualiza uma feature flag (descrição ou status 'is_enabled')"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Corpo da requisição obrigatório"}), 400

    fields = []
    values = []

    # Constrói a query dinamicamente
    if "description" in data:
        fields.append("description = %s")
        values.append(data["description"])
    if "is_enabled" in data:
        fields.append("is_enabled = %s")
        values.append(data["is_enabled"])

    if not fields:
        return jsonify(
            {"error": "Pelo menos um campo ('description', 'is_enabled') é obrigatório"}
        ), 400

    values.append(name)  # Adiciona o 'name' para a cláusula WHERE

    query = f"UPDATE flags SET {', '.join(fields)} WHERE name = %s RETURNING *"

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, tuple(values))

        if cur.rowcount == 0:
            return jsonify({"error": "Flag não encontrada"}), 404

        updated_flag = cur.fetchone()
        conn.commit()
        log.info(f"Flag '{name}' atualizada com sucesso.")
        return jsonify(updated_flag), 200
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao atualizar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


@app.route("/flags/<string:name>", methods=["DELETE"])
@require_auth
@observe_flag_operation("delete")
def delete_flag(name):
    """Deleta uma feature flag"""
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("DELETE FROM flags WHERE name = %s", (name,))

        if cur.rowcount == 0:
            return jsonify({"error": "Flag não encontrada"}), 404

        conn.commit()
        log.info(f"Flag '{name}' deletada com sucesso.")
        return "", 204  # 204 No Content
    except Exception as e:
        if conn:
            conn.rollback()
        log.error(f"Erro ao deletar flag '{name}': {e}")
        return jsonify({"error": "Erro interno do servidor", "details": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8002))
    app.run(host="0.0.0.0", port=port, debug=False)
