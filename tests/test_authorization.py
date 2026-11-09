import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock

from semantic_entry.core import (
    Actor,
    CommandCatalog,
    CommandEffect,
    CommandModel,
    CommandRegistry,
    CommandSpec,
    PermissionDeniedError,
)


class EmptyData(CommandModel):
    pass


class GrantAuthorizer:
    def __init__(self, grants):
        self.grants = grants

    def is_allowed(self, actor, permission):
        return permission in self.grants.get(actor.actor_id, set())


class CommandAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.reader = Actor("reader")
        self.writer = Actor("writer")
        self.read = CommandSpec(
            "notes.read", 1, CommandEffect.READ, EmptyData, EmptyData,
            "Прочитать заметки", "notes.read",
        )
        self.write = CommandSpec(
            "notes.write", 1, CommandEffect.WRITE, EmptyData, EmptyData,
            "Добавить заметку", "notes.write",
        )
        self.registry = CommandRegistry()
        self.registry.register(self.read)
        self.registry.register(self.write)
        self.policy = GrantAuthorizer({
            "reader": {"notes.read"},
            "writer": {"notes.read", "notes.write"},
        })
        self.catalog = CommandCatalog(self.registry, self.policy)

    def test_catalog_only_lists_commands_allowed_to_actor(self):
        self.assertEqual(self.catalog.list(self.reader), (self.read,))
        self.assertEqual(self.catalog.list(self.writer), (self.read, self.write))
        self.assertEqual(self.catalog.list(Actor("unknown")), ())

    def test_direct_lookup_checks_write_and_read_permissions(self):
        self.assertIs(self.catalog.get("notes.read", 1, self.reader), self.read)
        self.assertIs(self.catalog.get("notes.write", 1, self.writer), self.write)
        with self.assertRaises(PermissionDeniedError):
            self.catalog.get("notes.write", 1, self.reader)
        with self.assertRaises(PermissionDeniedError):
            self.catalog.get("notes.read", 1, Actor("unknown"))

    def test_access_is_checked_again_after_grant_is_revoked(self):
        self.assertIs(self.catalog.get("notes.read", 1, self.reader), self.read)
        self.policy.grants["reader"].clear()
        self.assertEqual(self.catalog.list(self.reader), ())
        with self.assertRaises(PermissionDeniedError):
            self.catalog.get("notes.read", 1, self.reader)

    def test_versions_can_require_different_permissions(self):
        restricted = replace(self.read, version=2, permission="notes.read_sensitive")
        self.registry.register(restricted)
        self.assertEqual(self.catalog.list(self.reader), (self.read,))
        with self.assertRaises(PermissionDeniedError):
            self.catalog.get("notes.read", 2, self.reader)

    def test_authorizer_receives_trusted_actor_and_command_permission(self):
        authorizer = Mock()
        authorizer.is_allowed.return_value = True
        catalog = CommandCatalog(self.registry, authorizer)
        self.assertIs(catalog.get("notes.write", 1, self.writer), self.write)
        authorizer.is_allowed.assert_called_once_with(self.writer, "notes.write")

    def test_authorizer_failure_does_not_grant_access(self):
        authorizer = Mock()
        authorizer.is_allowed.side_effect = RuntimeError("Policy service unavailable")
        catalog = CommandCatalog(self.registry, authorizer)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            catalog.get("notes.read", 1, self.reader)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            catalog.list(self.reader)

    def test_only_explicit_true_grants_access(self):
        for response in (False, None, "yes", 1):
            with self.subTest(response=response):
                authorizer = Mock()
                authorizer.is_allowed.return_value = response
                catalog = CommandCatalog(self.registry, authorizer)
                self.assertEqual(catalog.list(self.reader), ())
                with self.assertRaises(PermissionDeniedError):
                    catalog.get("notes.read", 1, self.reader)

    def test_unknown_command_does_not_fall_back_to_an_allowed_one(self):
        with self.assertRaises(KeyError):
            self.catalog.get("missing", 1, self.writer)
        with self.assertRaises(KeyError):
            self.catalog.get("notes.read", 999, self.writer)

    def test_actor_is_immutable_and_requires_nonempty_identifier(self):
        for actor_id in (None, "", "   ", 123):
            with self.subTest(actor_id=actor_id):
                with self.assertRaises(ValueError):
                    Actor(actor_id)
        with self.assertRaises(FrozenInstanceError):
            self.reader.actor_id = "writer"


if __name__ == "__main__":
    unittest.main()
