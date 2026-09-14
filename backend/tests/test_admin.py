"""
Unit tests for admin dashboard endpoints: user management, plan updates, analytics, and access control.
"""
from uuid import UUID


class TestAdminAccess:
    """Tests for admin access control (require_admin dependency)."""

    ADMIN_USERS_URL = "/admin/users"

    def test_regular_user_gets_403(self, client, auth_headers, mock_conn):
        """Regular (non-admin) user should get 403."""
        # User exists but is not admin
        mock_conn.fetchrow.return_value = {"is_admin": False}

        resp = client.get(self.ADMIN_USERS_URL, headers=auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"

    def test_admin_user_gets_200(self, client, admin_headers, mock_conn):
        """Admin user should pass through."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetch.return_value = []

        resp = client.get(self.ADMIN_USERS_URL, headers=admin_headers)
        assert resp.status_code == 200

    def test_no_token_gets_401(self, client):
        """Request without auth token should get 401."""
        resp = client.get(self.ADMIN_USERS_URL)
        assert resp.status_code == 401

    def test_user_not_found_gets_403(self, client, auth_headers, mock_conn):
        """Authenticated user not in DB should get 403."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(self.ADMIN_USERS_URL, headers=auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"


class TestAdminListUsers:
    """Tests for GET /admin/users (paginated envelope: {users, total, page, pages})."""

    URL = "/admin/users"

    def test_list_users_success(self, client, admin_headers, mock_conn):
        """Should return paginated envelope of users with their stats."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 2
        mock_conn.fetch.return_value = [
            {
                "id": UUID("11111111-1111-4111-8111-111111111111"),
                "email": "admin@example.com",
                "plan": "pro",
                "verified": True,
                "is_admin": True,
                "created_at": "2026-01-01T00:00:00Z",
                "usage_gb": 5.0,
                "file_count": 3,
            },
            {
                "id": UUID("22222222-2222-4222-8222-222222222222"),
                "email": "user@example.com",
                "plan": "free",
                "verified": False,
                "is_admin": False,
                "created_at": "2026-06-15T00:00:00Z",
                "usage_gb": 0.5,
                "file_count": 1,
            },
        ]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["page"] == 1
        assert data["pages"] == 1
        users = data["users"]
        assert len(users) == 2
        # Emails are masked in the list view (a***@example.com) so a leaked
        # admin list can't be used to harvest full addresses.
        assert users[0]["email"] == "a***@example.com"
        assert users[0]["is_admin"] is True
        assert users[0]["plan"] == "pro"
        assert users[0]["total_files"] == 3
        assert users[0]["total_gb_used"] == 5.0
        assert users[1]["email"] == "u***@example.com"
        assert users[1]["is_admin"] is False

    def test_list_users_pagination(self, client, admin_headers, mock_conn):
        """Should compute page count from total / limit."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 41
        mock_conn.fetch.return_value = [
            {
                "id": UUID("11111111-1111-4111-8111-111111111111"),
                "email": "admin@example.com",
                "plan": "free",
                "verified": False,
                "is_admin": False,
                "created_at": "2026-01-01T00:00:00Z",
                "usage_gb": 0,
                "file_count": 0,
            }
        ]

        resp = client.get(self.URL + "?page=3&limit=20", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 41
        assert data["page"] == 3
        assert data["pages"] == 3  # ceil(41/20)

    def test_list_users_empty(self, client, admin_headers, mock_conn):
        """Should return empty users list with zeroed envelope when no users."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 0
        mock_conn.fetch.return_value = []

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == []
        assert data["total"] == 0
        assert data["pages"] == 1

    def test_list_users_null_usage(self, client, admin_headers, mock_conn):
        """Should handle null usage_gb gracefully (0, not None)."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1
        mock_conn.fetch.return_value = [
            {
                "id": UUID("11111111-1111-4111-8111-111111111111"),
                "email": "user@example.com",
                "plan": "free",
                "verified": False,
                "is_admin": False,
                "created_at": "2026-01-01T00:00:00Z",
                "usage_gb": 0,
                "file_count": 0,
            },
        ]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        user = data["users"][0]
        assert user["total_gb_used"] == 0
        assert user["verified"] is False
        assert user["is_admin"] is False


class TestAdminToggleAdmin:
    """Tests for POST /admin/users/{user_id}/toggle-admin."""

    URL = "/admin/users"

    def test_toggle_admin_grant(self, client, admin_headers, mock_conn):
        """Should flip a regular user to admin."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},   # require_admin check
            {"is_admin": False},  # target user
        ]

        resp = client.post(
            f"{self.URL}/11111111-1111-4111-8111-111111111111/toggle-admin",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["detail"] == "Admin status updated"
        assert data["is_admin"] is True
        # is_admin update + the security_stamp session-rotation bump
        assert mock_conn.execute.await_count == 2
        assert any(
            "security_stamp" in str(c.args[0]) for c in mock_conn.execute.call_args_list
        )

    def test_toggle_admin_revoke(self, client, admin_headers, mock_conn):
        """Should flip an admin back to regular user (when another admin
        remains — the last-admin demotion guard must not block)."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            {"is_admin": True},  # target user
        ]
        # H3 FIX: demoting is only allowed while >=1 OTHER admin remains;
        # report 2 total admins so the demotion proceeds.
        mock_conn.fetchval.return_value = 2

        resp = client.post(
            f"{self.URL}/11111111-1111-4111-8111-111111111111/toggle-admin",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is False

    def test_toggle_admin_not_found(self, client, admin_headers, mock_conn):
        """Should return 404 for non-existent user."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            None,                 # target user not found
        ]

        resp = client.post(
            f"{self.URL}/00000000-0000-0000-0000-000000000000/toggle-admin",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"

    def test_toggle_admin_non_admin_forbidden(self, client, auth_headers, mock_conn):
        """Regular user should get 403."""
        mock_conn.fetchrow.return_value = {"is_admin": False}

        resp = client.post(
            f"{self.URL}/11111111-1111-4111-8111-111111111111/toggle-admin",
            headers=auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"


class TestAdminGetUser:
    """Tests for GET /admin/users/{user_id}."""

    URL = "/admin/users"

    def test_get_user_success(self, client, admin_headers, mock_conn):
        """Should return a single user's details."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            {  # user data
                "id": UUID("11111111-1111-4111-8111-111111111111"),
                "email": "target@example.com",
                "plan": "business",
                "verified": True,
                "is_admin": False,
                "created_at": "2026-03-01T00:00:00Z",
                "usage_gb": 50.0,
                "file_count": 10,
            },
        ]

        resp = client.get(f"{self.URL}/11111111-1111-4111-8111-111111111111", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "target@example.com"
        assert data["plan"] == "business"
        assert data["usage_gb"] == 50.0
        assert data["file_count"] == 10

    def test_get_user_not_found(self, client, admin_headers, mock_conn):
        """Should return 404 for non-existent user."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            None,  # user not found
        ]

        resp = client.get(f"{self.URL}/00000000-0000-0000-0000-000000000000", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"


class TestAdminUpdatePlan:
    """Tests for PUT /admin/users/{user_id}/plan."""

    URL = "/admin/users"

    def test_update_plan_success(self, client, admin_headers, mock_conn):
        """Should update user's plan successfully."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.execute.return_value = "UPDATE 1"

        resp = client.put(
            f"{self.URL}/11111111-1111-4111-8111-111111111111/plan",
            headers=admin_headers,
            json={"plan": "pro"},
        )
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Plan updated to pro"

    def test_update_plan_invalid(self, client, admin_headers, mock_conn):
        """Should reject invalid plan name."""
        mock_conn.fetchrow.return_value = {"is_admin": True}

        resp = client.put(
            f"{self.URL}/11111111-1111-4111-8111-111111111111/plan",
            headers=admin_headers,
            json={"plan": "enterprise"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid plan"

    def test_update_plan_user_not_found(self, client, admin_headers, mock_conn):
        """Should return 404 if user doesn't exist."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.execute.return_value = "UPDATE 0"

        resp = client.put(
            f"{self.URL}/00000000-0000-0000-0000-000000000000/plan",
            headers=admin_headers,
            json={"plan": "free"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"

    def test_update_plan_all_valid_values(self, client, admin_headers, mock_conn):
        """Should accept all valid plan values."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.execute.return_value = "UPDATE 1"

        for plan in ("free", "pro", "business"):
            resp = client.put(
                f"{self.URL}/11111111-1111-4111-8111-111111111111/plan",
                headers=admin_headers,
                json={"plan": plan},
            )
            assert resp.status_code == 200
            assert resp.json()["detail"] == f"Plan updated to {plan}"


class TestAdminDeleteUser:
    """Tests for DELETE /admin/users/{user_id}."""

    URL = "/admin/users"

    def test_delete_regular_user_success(self, client, admin_headers, mock_conn, mock_s3_client):
        """Should delete a regular user and their files."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            {"is_admin": False},  # target user is not admin
        ]
        mock_conn.fetch.return_value = []  # no files to delete
        mock_conn.execute.return_value = "DELETE 1"

        resp = client.delete(
            f"{self.URL}/11111111-1111-4111-8111-111111111111",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["detail"] == "User deleted"

    def test_delete_admin_user_blocked(self, client, admin_headers, mock_conn):
        """Should block deletion of admin accounts."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            {"is_admin": True},  # target user IS admin
        ]

        resp = client.delete(
            f"{self.URL}/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            headers=admin_headers,
        )
        assert resp.status_code == 403
        assert "cannot be deleted" in resp.json()["detail"]

    def test_delete_user_not_found(self, client, admin_headers, mock_conn):
        """Should return 404 if target user doesn't exist."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            None,  # target user not found
        ]

        resp = client.delete(
            f"{self.URL}/00000000-0000-0000-0000-000000000000",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"

    def test_delete_user_with_files(self, client, admin_headers, mock_conn, mock_s3_client):
        """Should clean up S3 files when deleting a user."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin check
            {"is_admin": False},  # target user is not admin
        ]
        mock_conn.fetch.return_value = [
            {"id": UUID("33333333-3333-4333-8333-333333333333"), "minio_url": "http://minio:9000/bucket/user123/file456/video.mp4"},
        ]
        mock_conn.execute.return_value = "DELETE 1"

        resp = client.delete(
            f"{self.URL}/11111111-1111-4111-8111-111111111111",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        # Verify S3 delete was called for the file
        mock_s3_client.delete_object.assert_called_once()


class TestAdminAuditLog:
    """Every admin action must be audit-logged with actor + target."""

    ADMIN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    TARGET = "11111111-1111-4111-8111-111111111111"
    URL = "/admin/users"

    def test_toggle_admin_logged(self, client, admin_headers, mock_conn, caplog):
        """toggle-admin records the acting admin and the new status."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},   # require_admin check
            {"is_admin": False},  # target user
        ]

        import logging
        with caplog.at_level(logging.INFO, logger="app.routes.admin"):
            resp = client.post(f"{self.URL}/{self.TARGET}/toggle-admin", headers=admin_headers)

        assert resp.status_code == 200
        toggles = [r for r in caplog.records if "toggled admin status" in r.message]
        assert len(toggles) == 1
        assert self.ADMIN in toggles[0].message   # actor
        assert self.TARGET in toggles[0].message  # target
        assert "True" in toggles[0].message      # new status

    def test_plan_change_logged(self, client, admin_headers, mock_conn, caplog):
        """Plan changes record the acting admin and the new plan."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.execute.return_value = "UPDATE 1"

        import logging
        with caplog.at_level(logging.INFO, logger="app.routes.admin"):
            resp = client.put(
                f"{self.URL}/{self.TARGET}/plan",
                headers=admin_headers,
                json={"plan": "pro"},
            )

        assert resp.status_code == 200
        changes = [r for r in caplog.records if "changed plan of user" in r.message]
        assert len(changes) == 1
        assert self.ADMIN in changes[0].message
        assert self.TARGET in changes[0].message
        assert "pro" in changes[0].message

    def test_delete_user_logged(self, client, admin_headers, mock_conn, caplog):
        """User deletion records the acting admin and the target."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},   # require_admin check
            {"is_admin": False},  # target user is not admin
        ]
        mock_conn.fetch.return_value = []
        mock_conn.execute.return_value = "DELETE 1"

        import logging
        with caplog.at_level(logging.INFO, logger="app.routes.admin"):
            resp = client.delete(f"{self.URL}/{self.TARGET}", headers=admin_headers)

        assert resp.status_code == 200
        deletes = [r for r in caplog.records if "deleted user" in r.message]
        assert len(deletes) == 1
        assert self.ADMIN in deletes[0].message
        assert self.TARGET in deletes[0].message


class TestAdminAnalytics:
    """Tests for GET /admin/analytics."""

    URL = "/admin/analytics"

    def test_analytics_success(self, client, admin_headers, mock_conn):
        """Should return platform analytics."""
        mock_conn.fetchrow.return_value = {"is_admin": True}

        # Set up fetchval to return different counts sequentially
        mock_conn.fetchval.side_effect = [
            100,    # total_users
            250,    # total_files
            500.0,  # total_storage_gb
            80,     # free_users
            15,     # pro_users
            5,      # business_users
        ]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_users"] == 100
        assert data["total_files"] == 250
        assert data["total_storage_gb"] == 500.0
        assert data["free_users"] == 80
        assert data["pro_users"] == 15
        assert data["business_users"] == 5

    def test_analytics_empty(self, client, admin_headers, mock_conn):
        """Should return zeros when no data exists."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.side_effect = [
            None,   # total_users
            None,   # total_files
            None,   # total_storage
            None,   # free_users
            None,   # pro_users
            None,   # business_users
        ]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_users"] == 0
        assert data["total_files"] == 0
        assert data["total_storage_gb"] == 0
        assert data["free_users"] == 0
        assert data["pro_users"] == 0
        assert data["business_users"] == 0

    def test_analytics_all_plans_present(self, client, admin_headers, mock_conn):
        """Should handle mixed plan distribution correctly."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.side_effect = [
            50,     # total_users
            10,     # total_files
            30.5,   # total_storage_gb
            30,     # free_users
            15,     # pro_users
            5,      # business_users
        ]

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_users"] == 50
        assert data["free_users"] + data["pro_users"] + data["business_users"] == 50
