"""Export column boundaries, including paths that escape scoped root rows."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import graphene
from django.core.exceptions import ValidationError
from django.db import models
from django.test import SimpleTestCase
from graphene_django import DjangoObjectType
from graphene_django.registry import Registry

from core.gql.export_mixin import ExportableQueryMixin
from core.models import ExportableQueryModel


class ExportParent(models.Model):
    name = models.CharField(max_length=30)
    secret = models.CharField(max_length=30)
    parent = models.ForeignKey('self', null=True, on_delete=models.CASCADE)

    class Meta:
        app_label = 'core'
        managed = False


class ExportRow(models.Model):
    name = models.CharField(max_length=30)
    parent = models.ForeignKey(ExportParent, related_name='rows', on_delete=models.CASCADE)
    tags = models.ManyToManyField(ExportParent, related_name='tagged_rows')

    class Meta:
        app_label = 'core'
        managed = False


registry = Registry()


class ExportParentType(DjangoObjectType):
    rows = graphene.List(lambda: ExportRowType)

    class Meta:
        model = ExportParent
        fields = ('name', 'parent', 'rows')
        registry = registry


class ExportRowType(DjangoObjectType):
    assigned_count = graphene.Int()

    class Meta:
        model = ExportRow
        fields = ('id', 'name', 'parent', 'tags')
        registry = registry


class ExportQuery(ExportableQueryMixin):
    exportable_fields = ['rows']
    rows = SimpleNamespace(type=ExportRowType, filtering_args={})
    resolve_rows = Mock()


ExportQuery.create_export_function('rows')


class ExportColumnTests(SimpleTestCase):
    def test_declared_scalars_annotations_and_forward_relations_are_allowed(self):
        self.assertEqual(ExportQuery.validate_export_columns('rows', [
            'name', 'assignedCount', 'parent.name', 'parent.parent.name',
        ]), ['name', 'assigned_count', 'parent__name', 'parent__parent__name'])

    def test_unknown_hidden_reverse_and_many_to_many_paths_are_rejected(self):
        for column in [
            'secret', 'parent.secret', 'parent.rows.name', 'parent__rows__name',
            'tags.name', 'parent.taggedRows.name', 'name__icontains',
            'parent.parent.parent.parent.parent.name',
        ]:
            with self.subTest(column=column), self.assertRaises(ValidationError):
                ExportQuery.validate_export_columns('rows', [column])

    def test_missing_empty_and_invalid_columns_are_rejected(self):
        for columns in [None, [], [''], [None], [123], 'name', {'name': 'Name'}]:
            with self.subTest(columns=columns), self.assertRaises(ValidationError):
                ExportQuery.validate_export_columns('rows', columns)

    def test_queries_can_narrow_the_allowlist(self):
        with patch.object(ExportQuery, 'get_exportable_columns', return_value={'name'}):
            self.assertEqual(ExportQuery.validate_export_columns('rows', ['name']), ['name'])
            with self.assertRaises(ValidationError):
                ExportQuery.validate_export_columns('rows', ['parent.name'])

    @patch.object(ExportableQueryModel, 'create_csv_export')
    def test_rejected_export_does_not_call_resolver_or_write_file(self, create):
        ExportQuery.resolve_rows.reset_mock()
        # Rebind so the exporter closes over this test's resolver mock.
        ExportQuery.create_export_function('rows')
        with self.assertRaises(ValidationError):
            ExportQuery.resolve_rows_export(None, Mock(), fields=['parent.rows.name'])
        ExportQuery.resolve_rows.assert_not_called()
        create.assert_not_called()

    @patch.object(ExportableQueryModel, 'create_csv_export')
    def test_validated_columns_reach_csv_writer(self, create):
        ExportQuery.create_export_function('rows')
        create.return_value.name = 'export.csv'
        info = SimpleNamespace(context=SimpleNamespace(user=Mock()))
        result = ExportQuery.resolve_rows_export(
            None, info, fields=['name', 'parent.name'], fields_columns=json.dumps({}),
        )
        self.assertEqual(result, 'export.csv')
        self.assertEqual(create.call_args.args[1], ['name', 'parent__name'])
        self.assertEqual(create.call_args.args[2], info.context.user)
