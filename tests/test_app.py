import psycopg2
import requests


def test_health_check_does_not_require_auth(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_protected_endpoint_requires_authorization_header(client):
    response = client.get("/flags")

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authorization header obrigatório"}


def test_rejects_invalid_api_key(client, app_module, auth_headers):
    app_module.requests.get.return_value.status_code = 401

    response = client.get("/flags", headers=auth_headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Chave de API inválida"}
    app_module.requests.get.assert_called_once_with(
        "http://auth-service/validate",
        headers={"Authorization": "Bearer valid-key"},
        timeout=3,
    )


def test_returns_gateway_timeout_when_auth_service_times_out(client, app_module, auth_headers):
    app_module.requests.get.side_effect = requests.exceptions.Timeout

    response = client.get("/flags", headers=auth_headers)

    assert response.status_code == 504
    assert response.get_json() == {"error": "Serviço de autenticação indisponível (timeout)"}


def test_returns_service_unavailable_when_auth_service_fails(client, app_module, auth_headers):
    app_module.requests.get.side_effect = requests.exceptions.ConnectionError("offline")

    response = client.get("/flags", headers=auth_headers)

    assert response.status_code == 503
    assert response.get_json() == {"error": "Serviço de autenticação indisponível"}


def test_create_flag_requires_name(client, auth_headers):
    response = client.post("/flags", json={"description": "missing name"}, headers=auth_headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "'name' é obrigatório"}


def test_create_flag(client, database, pool, auth_headers):
    connection, cursor = database
    created_flag = {
        "id": 1,
        "name": "new-dashboard",
        "description": "New UI",
        "is_enabled": True,
    }
    cursor.fetchone.return_value = created_flag

    response = client.post(
        "/flags",
        json={"name": "new-dashboard", "description": "New UI", "is_enabled": True},
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.get_json() == created_flag
    cursor.execute.assert_called_once()
    assert cursor.execute.call_args.args[1] == ("new-dashboard", "New UI", True)
    connection.commit.assert_called_once_with()
    cursor.close.assert_called_once_with()
    pool.putconn.assert_called_once_with(connection)


def test_create_duplicate_flag_rolls_back(client, database, auth_headers):
    connection, cursor = database
    cursor.execute.side_effect = psycopg2.IntegrityError

    response = client.post("/flags", json={"name": "duplicate"}, headers=auth_headers)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Flag 'duplicate' já existe"}
    connection.rollback.assert_called_once_with()


def test_create_flag_returns_internal_error(client, database, auth_headers):
    connection, cursor = database
    cursor.execute.side_effect = RuntimeError("write failed")

    response = client.post("/flags", json={"name": "checkout"}, headers=auth_headers)

    assert response.status_code == 500
    assert response.get_json()["details"] == "write failed"
    connection.rollback.assert_called_once_with()


def test_list_flags(client, database, auth_headers):
    _, cursor = database
    flags = [{"name": "alpha"}, {"name": "beta"}]
    cursor.fetchall.return_value = flags

    response = client.get("/flags", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json() == flags
    cursor.execute.assert_called_once_with("SELECT * FROM flags ORDER BY name")


def test_list_flags_returns_internal_error(client, database, auth_headers):
    _, cursor = database
    cursor.execute.side_effect = RuntimeError("read failed")

    response = client.get("/flags", headers=auth_headers)

    assert response.status_code == 500
    assert response.get_json()["details"] == "read failed"


def test_get_flag(client, database, auth_headers):
    _, cursor = database
    cursor.fetchone.return_value = {"name": "checkout", "is_enabled": False}

    response = client.get("/flags/checkout", headers=auth_headers)

    assert response.status_code == 200
    assert response.get_json()["name"] == "checkout"
    cursor.execute.assert_called_once_with("SELECT * FROM flags WHERE name = %s", ("checkout",))


def test_get_missing_flag(client, database, auth_headers):
    _, cursor = database
    cursor.fetchone.return_value = None

    response = client.get("/flags/unknown", headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Flag não encontrada"}


def test_get_flag_returns_internal_error(client, database, auth_headers):
    _, cursor = database
    cursor.execute.side_effect = RuntimeError("read failed")

    response = client.get("/flags/checkout", headers=auth_headers)

    assert response.status_code == 500
    assert response.get_json()["details"] == "read failed"


def test_update_flag_requires_body(client, auth_headers):
    response = client.put("/flags/checkout", json={}, headers=auth_headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Corpo da requisição obrigatório"}


def test_update_flag_requires_supported_field(client, auth_headers):
    response = client.put("/flags/checkout", json={"name": "renamed"}, headers=auth_headers)

    assert response.status_code == 400
    assert "Pelo menos um campo" in response.get_json()["error"]


def test_update_flag(client, database, auth_headers):
    connection, cursor = database
    cursor.rowcount = 1
    cursor.fetchone.return_value = {
        "name": "checkout",
        "description": "Gradual release",
        "is_enabled": True,
    }

    response = client.put(
        "/flags/checkout",
        json={"description": "Gradual release", "is_enabled": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    cursor.execute.assert_called_once_with(
        "UPDATE flags SET description = %s, is_enabled = %s WHERE name = %s RETURNING *",
        ("Gradual release", True, "checkout"),
    )
    connection.commit.assert_called_once_with()


def test_update_missing_flag(client, database, auth_headers):
    connection, cursor = database
    cursor.rowcount = 0

    response = client.put("/flags/unknown", json={"is_enabled": True}, headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Flag não encontrada"}
    connection.commit.assert_not_called()


def test_update_flag_returns_internal_error(client, database, auth_headers):
    connection, cursor = database
    cursor.execute.side_effect = RuntimeError("update failed")

    response = client.put("/flags/checkout", json={"is_enabled": True}, headers=auth_headers)

    assert response.status_code == 500
    assert response.get_json()["details"] == "update failed"
    connection.rollback.assert_called_once_with()


def test_delete_flag(client, database, auth_headers):
    connection, cursor = database
    cursor.rowcount = 1

    response = client.delete("/flags/checkout", headers=auth_headers)

    assert response.status_code == 204
    assert response.data == b""
    cursor.execute.assert_called_once_with("DELETE FROM flags WHERE name = %s", ("checkout",))
    connection.commit.assert_called_once_with()


def test_delete_missing_flag(client, database, auth_headers):
    connection, cursor = database
    cursor.rowcount = 0

    response = client.delete("/flags/unknown", headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Flag não encontrada"}
    connection.commit.assert_not_called()


def test_database_error_rolls_back_and_releases_connection(client, database, pool, auth_headers):
    connection, cursor = database
    cursor.execute.side_effect = RuntimeError("database unavailable")

    response = client.delete("/flags/checkout", headers=auth_headers)

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Erro interno do servidor",
        "details": "database unavailable",
    }
    connection.rollback.assert_called_once_with()
    cursor.close.assert_called_once_with()
    pool.putconn.assert_called_once_with(connection)
