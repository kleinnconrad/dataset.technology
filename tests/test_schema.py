"""Checks of the record models that keep the schema consistent over the years."""
import pytest

from schema import DATASET_MODELS, RecordBase


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_no_model_redefines_a_common_field(dataset: str) -> None:
    model = DATASET_MODELS[dataset]
    for name, field in RecordBase.model_fields.items():
        assert model.model_fields[name].annotation == field.annotation, f"{dataset}.{name} redefines a common field"
        assert model.model_fields[name].description == field.description, f"{dataset}.{name} redefines a common field"


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_every_field_and_model_is_described(dataset: str) -> None:
    model = DATASET_MODELS[dataset]
    assert (model.__doc__ or "").strip(), f"{dataset} has no docstring"
    assert all(field.description for field in model.model_fields.values())


def test_dataset_names_are_snake_case() -> None:
    assert all(name.replace("_", "").isalnum() and name == name.lower() for name in DATASET_MODELS)
