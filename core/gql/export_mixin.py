import json
import logging
import types
from functools import lru_cache
from typing import Dict, List, Callable

import graphene
from django.core.exceptions import ValidationError
from graphene.types.generic import GenericScalar
from pandas import DataFrame

from core.custom_filters import CustomFilterWizardStorage
from core.models import ExportableQueryModel


logger = logging.getLogger(__file__)


class ExportableQueryMixin:
    export_patches: Dict[str, List[Callable[[DataFrame], DataFrame]]] = {}
    module_name: str
    object_type: str
    related_field: str

    @classmethod
    def get_module_name(cls) -> str:
        if not hasattr(cls, "module_name") or getattr(cls, "module_name") is None:
            error_msg = (
                "The class using `ExportableQueryMixin` must specify the `module_name` property "
                "when the customFilters argument is specified. Please override the `module_name` "
                "property in the Query schema to specify how to append custom filters for the specific object."
            )
            raise NotImplementedError(error_msg)
        return cls.module_name

    @classmethod
    def get_object_type(cls) -> str:
        if not hasattr(cls, "object_type") or getattr(cls, "object_type") is None:
            error_msg = (
                "The class using `ExportableQueryMixin` must specify the `object_type` property "
                "when the customFilters argument is specified. Please override the `object_type` "
                "property in the Query schema to specify how to append custom filters for the specific object."
            )
            raise NotImplementedError(error_msg)
        return cls.object_type

    @classmethod
    def get_related_field(cls):
        if not hasattr(cls, "related_field") or getattr(cls, "related_field") is None:
            return None
        return cls.related_field

    @classmethod
    def get_exportable_fields(cls):
        if not hasattr(cls, "exportable_fields"):
            raise NotImplementedError(
                "Class using `ExportableQueryMixin` has to provide either exportable_fields "
                "or overwrite`get_exportable_fields` to provide list of fields that can be exported."
            )
        return cls.exportable_fields

    @classmethod
    def get_exportable_columns(cls, field_name):
        """Allow declared scalar columns and forward to-one relation columns.

        Reverse and many-to-many joins can escape a scoped root queryset. Never
        infer export access to those collections from their GraphQL exposure.
        Bound traversal to four relations (including Location.parent chains).
        Override this method to return a narrower explicit set for a query.
        """
        source = getattr(cls, field_name)
        root_type = source.type
        while hasattr(root_type, "of_type"):
            root_type = root_type.of_type
        # Connection fields expose their node type rather than a model directly.
        if not hasattr(root_type._meta, "model"):
            root_type = root_type._meta.node

        @lru_cache(maxsize=None)
        def columns(gql_type, depth):
            allowed = set()
            model = gql_type._meta.model
            forward = {field.name: field for field in model._meta.fields}
            for name, gql_field in gql_type._meta.fields.items():
                if isinstance(gql_field, graphene.Dynamic):
                    gql_field = gql_field.get_type()
                if gql_field is None:
                    continue
                output_type = gql_field.type
                while isinstance(output_type, graphene.NonNull):
                    output_type = output_type.of_type
                if isinstance(output_type, graphene.List):
                    continue
                model_field = forward.get(name)
                if model_field is not None and model_field.is_relation:
                    if depth and hasattr(getattr(output_type, "_meta", None), "model"):
                        allowed.update(
                            f"{name}__{column}" for column in columns(output_type, depth - 1)
                        )
                elif isinstance(output_type, type) and issubclass(output_type, (graphene.Scalar, graphene.Enum)):
                    # Includes explicitly declared scalar annotations on the root.
                    # Reverse relation fields are never scalar ORM columns.
                    if model_field is not None or name not in {
                        field.name for field in model._meta.get_fields() if field.is_relation
                    }:
                        allowed.add(name)
            return allowed

        return columns(root_type, 4)

    @classmethod
    def validate_export_columns(cls, field_name, fields):
        if not isinstance(fields, (list, tuple)) or not fields or any(
            not isinstance(field, str) or not field for field in fields
        ):
            raise ValidationError("Export requires a non-empty list of column names.")
        normalized = [cls._adjust_notation(field) for field in fields]
        allowed = cls.get_exportable_columns(field_name)
        if any(field not in allowed for field in normalized):
            raise ValidationError("One or more requested export columns are not allowed.")
        return normalized

    @classmethod
    def get_patches_for_field(cls, field_type):
        if field_type not in cls.get_exportable_fields():
            raise AttributeError(f"Field {field_type} is not being exported.")
        return cls.export_patches.get(field_type)

    @classmethod
    def __init_subclass_with_meta__(cls, **meta_options):
        cls._setup_exportable_fields(**meta_options)
        return super(ExportableQueryMixin, cls).__init_subclass_with_meta__(
            **meta_options
        )

    @classmethod
    def _setup_exportable_fields(cls, **meta_options):
        for field in cls.get_exportable_fields():
            field_name = f"{field}_export"
            fld = getattr(cls, field)
            new_args = fld.args
            new_args["file_format"] = graphene.String()
            new_args["fields"] = graphene.List(of_type=graphene.String)
            new_args["fields_columns"] = GenericScalar()
            setattr(cls, field_name, graphene.Field(graphene.String, args=new_args))
            cls.create_export_function(field)

    @classmethod
    def _adjust_notation(cls, field):
        from graphene.utils.str_converters import to_snake_case

        return to_snake_case(field.replace(".", "__"))

    @classmethod
    def create_export_function(cls, field_name):
        new_function_name = f"resolve_{field_name}_export"
        default_resolve = getattr(cls, f"resolve_{field_name}", None)

        if not default_resolve:
            raise AttributeError(
                f"Query {cls} doesn't provide resolve function for {field_name}. "
                f"CSV export cannot be created"
            )

        def exporter(cls, self, info, **kwargs):
            custom_filters = kwargs.pop("customFilters", None)
            export_fields = cls.validate_export_columns(field_name, kwargs.pop("fields", None))
            fields_mapping = json.loads(kwargs.pop("fields_columns"))
            file_format = kwargs.pop("file_format", "csv")

            source_field = getattr(cls, field_name)
            filter_kwargs = {
                k: v for k, v in kwargs.items() if k in source_field.filtering_args
            }

            qs = default_resolve(None, info, **kwargs)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs)
            export_file = ExportableQueryModel.create_csv_export(
                qs,
                export_fields,
                info.context.user,
                column_names=fields_mapping,
                patches=cls.get_patches_for_field(field_name),
                file_format=file_format,
            )

            return export_file.name

        setattr(cls, new_function_name, types.MethodType(exporter, cls))

    @classmethod
    def __append_custom_filters(cls, custom_filters, queryset):
        if custom_filters:
            module_name = cls.get_module_name()
            object_type = cls.get_object_type()
            related_field = cls.get_related_field()
            queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                module_name,
                object_type,
                custom_filters,
                queryset,
                relation=related_field,
            )
        return queryset
